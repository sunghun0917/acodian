"""LightRAG v4 업무일지 index adapter와 lifecycle factory.

LightRAG v1.4.10 문서 기준으로 `LightRAG` instance를 만들고,
`initialize_storages()` 이후 `ainsert(..., ids=..., file_paths=...)`를 호출한다.
기본 테스트는 이 module의 dependency injection 경계를 통해 실제 LLM/API key를
사용하지 않는다.
"""

from __future__ import annotations

import asyncio
import configparser
import logging
import os
import random
from functools import partial
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol, TypeAlias

import aiohttp
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

from app.config.settings import Settings, settings
from app.client.langfuse_client import configure_lightrag_tracing, restore_lightrag_context
from app.light.v4.chain.worklog_extraction import complete_body_extraction, is_body_extraction
from app.light.v4.store.worklog_extraction_cache import ValidatedWorklogExtractionCache
from app.light.v4.service.worklog_custom_kg_builder import LightRagWorklogCustomKgDocument
from app.light.v4.service.worklog_document_builder import LightRagWorklogDocument
from app.light.v4.store.worklog_custom_kg_tracking import insert_tracked_worklog_custom_kg
from app.light.v4.service.worklog_mutation_lock import worklog_mutation_locks, workspace_mutation_key
from app.light.v4.store.worklog_team_vector_store import (
    finalize_worklog_team_vector_storage,
    initialize_worklog_team_vector_storage,
)
from app.task.retry_policy import is_retryable_ai_error

_QDRANT_VECTOR_STORAGE = "QdrantVectorDBStorage"
_NEO4J_GRAPH_STORAGE = "Neo4JStorage"
_PG_STORAGE_NAMES = (
    "full_docs", "text_chunks", "full_entities", "full_relations", "entity_chunks",
    "relation_chunks", "llm_response_cache", "doc_status",
)
_SCOPE_STORAGE_NAMES = (
    *_PG_STORAGE_NAMES, "entities_vdb",
    "relationships_vdb", "chunks_vdb", "chunk_entity_relation_graph",
)

EmbeddingFunc: TypeAlias = Callable[..., Awaitable[Any]]
EmbeddingWrapper: TypeAlias = Callable[..., Callable[[EmbeddingFunc], EmbeddingFunc]]

logger = logging.getLogger(__name__)


class LightRagConfigurationError(RuntimeError):
    """LightRAG import, API key, 설정 구성 실패를 나타내는 예외."""


class LightRagInsertTimeoutError(RuntimeError):
    """LightRAG document insert가 설정된 제한 시간을 초과했음을 나타내는 예외."""


class LightRagInsertFailedError(RuntimeError):
    """LightRAG document insert 또는 storage 초기화가 실패했음을 나타내는 예외."""


class LightRagDeleteTimeoutError(RuntimeError):
    """LightRAG document delete가 설정된 제한 시간을 초과했음을 나타내는 예외."""


class LightRagDeleteFailedError(RuntimeError):
    """LightRAG document delete가 실패했음을 나타내는 예외."""


class LightRagQueryTimeoutError(RuntimeError):
    """LightRAG query가 설정된 제한 시간을 초과했음을 나타내는 예외."""


class LightRagQueryFailedError(RuntimeError):
    """LightRAG query 실행 또는 응답 해석이 실패했음을 나타내는 예외."""


class LightRagProviderUnavailableError(LightRagQueryFailedError):
    """Gemini provider 일시 장애가 모든 retry/fallback 후에도 해소되지 않았음을 나타내는 예외."""


class WorklogLightIndexAdapter(Protocol):
    """업무일지 문서를 LightRAG에 index하는 adapter interface."""

    async def index_documents(self, documents: list[LightRagWorklogDocument]) -> None:
        """업무일지 문서 목록을 LightRAG에 insert한다."""

    async def index_custom_kg_documents(
        self, documents: list[LightRagWorklogCustomKgDocument]
    ) -> None:
        """업무일지 확정 관계 custom KG document 목록을 LightRAG에 insert한다."""

    async def delete_document(self, document_id: str) -> None:
        """단일 업무일지 문서를 LightRAG에서 삭제한다."""

    async def save_indexed_team_ids(self, teams_by_worklog_id: dict[int, int]) -> None:
        """Record team ownership before scoped writes."""

    async def get_document_team_id(self, document_id: str, *, allow_missing: bool = False) -> int | None:
        """Read the last indexed team for deletion."""


@dataclass(frozen=True)
class LightRagDependencies:
    """LightRAG runtime import 결과.

    테스트는 이 dataclass에 fake class/function을 주입해 외부 dependency와 API key
    호출 없이 adapter boundary만 검증한다.
    """

    lightrag_class: type[Any]
    gemini_model_complete: Callable[..., Awaitable[str]]
    gemini_embed: Any
    embedding_wrapper: EmbeddingWrapper
    query_param_class: type[Any]
    tei_rerank_api: Callable[..., Awaitable[Any]] | None = None


@dataclass(frozen=True)
class LightRagQueryOptions:
    """LightRAG 업무일지 query 실행 옵션."""

    query: str
    top_k: int
    chunk_top_k: int
    response_type: str
    enable_rerank: bool = False
    rerank_model: str | None = None
    system_prompt: str | None = None
    stream: bool = False


@dataclass(frozen=True)
class LightRagQueryResult:
    """LightRAG query raw 결과 wrapper."""

    raw: Any


class LightRagWorklogIndexAdapter:
    """LightRAG instance를 lazy 초기화하고 업무일지 document batch를 insert한다."""

    def __init__(
        self,
        *,
        settings_obj: Settings = settings,
        dependencies: LightRagDependencies | None = None,
        expected_workspace: str | None = None,
    ) -> None:
        """adapter 설정과 optional fake dependency를 구성한다."""
        self._settings = settings_obj
        self._dependencies = dependencies
        self._expected_workspace = expected_workspace
        self._rag: Any | None = None
        self._initialized = False
        # _initialize_lock : 동시요청와도 싱글톤 반환되도록
        self._initialize_lock = asyncio.Lock()

    async def index_documents(self, documents: list[LightRagWorklogDocument]) -> None:
        """문서를 batch로 LightRAG에 insert한다.

        `document_id`는 LightRAG `ids`로, `file_path`는 `file_paths`로 전달해 후속
        search 단계가 업무일지 ID를 회수할 수 있게 한다.
        """
        if not documents:
            return

        try:
            rag = await self._get_initialized_rag()
            async with worklog_mutation_locks([workspace_mutation_key(rag)]):
                await asyncio.wait_for(
                    rag.ainsert(
                        [document.text for document in documents],
                        ids=[document.document_id for document in documents],
                        file_paths=[document.file_path for document in documents],
                    ),
                    timeout=self._settings.lightrag_insert_timeout_seconds,
                )
                statuses = await asyncio.wait_for(
                    rag.doc_status.get_by_ids([doc.document_id for doc in documents]),
                    timeout=self._settings.lightrag_insert_timeout_seconds,
                )
                if len(statuses) != len(documents) or any(
                    not isinstance(status, dict) or status.get("status") != "processed"
                    for status in statuses
                ):
                    raise LightRagInsertFailedError("LightRAG document processing is incomplete")
        except LightRagConfigurationError:
            raise
        except TimeoutError as exc:
            raise LightRagInsertTimeoutError("LightRAG insert timed out") from exc
        except Exception as exc:
            raise LightRagInsertFailedError("LightRAG insert failed") from exc

    async def index_custom_kg_documents(
        self, documents: list[LightRagWorklogCustomKgDocument]
    ) -> None:
        """확정 관계 custom KG document 목록을 LightRAG에 insert한다.

        `document_id`는 LightRAG `full_doc_id`로 전달해 chunk provenance를 문서 단위로
        묶는다. payload 내부 `source_id`는 LightRAG가 chunk id로 재매핑한다.
        """
        if not documents:
            return

        try:
            rag = await self._get_initialized_rag()
            for document in documents:
                await asyncio.wait_for(
                    insert_tracked_worklog_custom_kg(rag, document),
                    timeout=self._settings.lightrag_insert_timeout_seconds,
                )
        except LightRagConfigurationError:
            raise
        except TimeoutError as exc:
            raise LightRagInsertTimeoutError("LightRAG custom KG insert timed out") from exc
        except Exception as exc:
            raise LightRagInsertFailedError("LightRAG custom KG insert failed") from exc

    async def delete_document(self, document_id: str) -> None:
        """단일 업무일지 문서를 LightRAG에서 삭제한다.

        `adelete_by_doc_id`를 호출하여 문서의 doc_status JSON 상태, Qdrant 청크 벡터,
        Neo4j 노드/관계 중 해당 문서 전용 참조를 삭제한다.
        """
        if not document_id:
            return

        try:
            rag = await self._get_initialized_rag()
            async with asyncio.timeout(self._settings.lightrag_insert_timeout_seconds), worklog_mutation_locks([workspace_mutation_key(rag)]):
                pending = getattr(rag.doc_status, "_worklog_pending_chunks", {}).get(document_id, [])
                if pending:
                    record = await rag.doc_status.get_by_id(document_id)
                    status_missing = record is None
                    if status_missing:
                        record = getattr(rag.doc_status, "_worklog_pending_status", {}).get(document_id)
                    if not isinstance(record, dict):
                        raise LightRagDeleteFailedError("Document chunk tracking is incomplete; recovery required")
                    if status_missing or not set(pending).issubset(record.get("chunks_list", [])):
                        chunks = list(dict.fromkeys([*record.get("chunks_list", []), *pending]))
                        await rag.doc_status.upsert({document_id: {
                            **record, "chunks_list": chunks, "chunks_count": len(chunks),
                            "metadata": {**record.get("metadata", {}), "indexing_status": "STAGING"},
                        }})
                        await rag.doc_status.index_done_callback()
                        saved = await rag.doc_status.get_by_id(document_id)
                        if (not isinstance(saved, dict)
                                or not set(pending).issubset(saved.get("chunks_list", []))
                                or saved.get("metadata", {}).get("indexing_status") != "STAGING"):
                            raise LightRagDeleteFailedError("Document chunk tracking recovery could not be verified")
                result = await asyncio.wait_for(
                    rag.adelete_by_doc_id(document_id),
                    timeout=self._settings.lightrag_insert_timeout_seconds,
                )
                result_status = result.get("status") if isinstance(result, dict) else getattr(result, "status", None)
                if result_status == "not_found":
                    await asyncio.wait_for(
                        self._delete_statusless_document(rag, document_id),
                        timeout=self._settings.lightrag_insert_timeout_seconds,
                    )
            status = result.get("status") if isinstance(result, dict) else getattr(result, "status", None)
            if status not in {"success", "not_found"}:
                raise LightRagDeleteFailedError(f"LightRAG returned deletion status: {status}")
            getattr(rag.doc_status, "_worklog_pending_chunks", {}).pop(document_id, None)
            getattr(rag.doc_status, "_worklog_pending_status", {}).pop(document_id, None)
        except LightRagConfigurationError:
            raise
        except TimeoutError as exc:
            raise LightRagDeleteTimeoutError("LightRAG document delete timed out") from exc
        except Exception as exc:
            raise LightRagDeleteFailedError(f"LightRAG document delete failed: {exc}") from exc

    async def _delete_statusless_document(self, rag: Any, document_id: str) -> None:
        """native not_found가 놓치는 full_docs-only 쓰기를 정리하고 추적 유실은 차단한다."""
        for name in ("full_entities", "full_relations"):
            if await getattr(rag, name).get_by_id(document_id):
                raise LightRagDeleteFailedError("Statusless document retains graph tracking; manual recovery required")
        if await rag.full_docs.get_by_id(document_id) is not None:
            await rag.full_docs.delete([document_id])
            await rag.full_docs.index_done_callback()
        if await rag.full_docs.get_by_id(document_id) is not None:
            raise LightRagDeleteFailedError("Statusless full document cleanup is incomplete")

    async def set_document_indexing_status(self, document_id: str, state: str) -> None:
        """native 상태를 보존하며 전역 완료 게이트를 영속화하고 읽어 확인한다."""
        async def save() -> None:
            if not document_id or state not in {"STAGING", "COMPLETED"}:
                raise ValueError("Invalid document indexing state")
            rag = await self._get_initialized_rag()
            async with worklog_mutation_locks([workspace_mutation_key(rag)]):
                record = await rag.doc_status.get_by_id(document_id)
                if record is None and state == "STAGING":
                    return
                if not isinstance(record, dict):
                    raise ValueError("Index publication requires an existing document")
                metadata = record.get("metadata", {})
                if not isinstance(metadata, dict):
                    raise ValueError("Invalid document metadata")
                team_id = metadata.get("indexed_team_id")
                if state == "COMPLETED" and (
                    record.get("status") != "processed" or type(team_id) is not int or team_id <= 0
                ):
                    raise ValueError("Index publication requires processed document ownership")
                await rag.doc_status.upsert({document_id: {
                    **record, "metadata": {**metadata, "indexing_status": state},
                }})
                await rag.doc_status.index_done_callback()
                saved = await rag.doc_status.get_by_id(document_id)
                if not isinstance(saved, dict) or saved.get("metadata", {}).get("indexing_status") != state:
                    raise ValueError("Index publication could not be verified")

        try:
            await asyncio.wait_for(save(), timeout=self._settings.lightrag_insert_timeout_seconds)
        except LightRagConfigurationError:
            raise
        except TimeoutError as exc:
            raise LightRagInsertTimeoutError("LightRAG indexing state save timed out") from exc
        except Exception as exc:
            raise LightRagInsertFailedError("LightRAG indexing state save failed") from exc

    async def save_indexed_team_ids(self, teams_by_worklog_id: dict[int, int]) -> None:
        """Preserve native status fields and record the indexed team before team writes."""
        if not teams_by_worklog_id:
            return
        try:
            if any(type(worklog) is not int or worklog <= 0 or type(team) is not int or team <= 0
                   for worklog, team in teams_by_worklog_id.items()):
                raise ValueError("Invalid indexed ownership")
            rag = await self._get_initialized_rag()
            async with worklog_mutation_locks([workspace_mutation_key(rag)]):
                records = {}
                for worklog, team in teams_by_worklog_id.items():
                    doc_id = f"worklog-{worklog}"
                    record = await rag.doc_status.get_by_id(doc_id)
                    if not isinstance(record, dict) or record.get("status") != "processed":
                        raise ValueError("Indexed ownership requires a processed document")
                    metadata = record.get("metadata", {})
                    if not isinstance(metadata, dict):
                        raise ValueError("Invalid document metadata")
                    records[doc_id] = {**record, "metadata": {**metadata, "indexed_team_id": team}}
                await rag.doc_status.upsert(records)
                await rag.doc_status.index_done_callback()
        except LightRagConfigurationError:
            raise
        except Exception as exc:
            raise LightRagInsertFailedError("LightRAG indexed ownership save failed") from exc

    async def get_document_team_id(self, document_id: str, *, allow_missing: bool = False) -> int | None:
        """Recover indexed ownership independently of the current source database."""
        try:
            rag = await self._get_initialized_rag()
            record = await rag.doc_status.get_by_id(document_id)
            if record is None:
                return None
            metadata = record.get("metadata") if isinstance(record, dict) else None
            if allow_missing and isinstance(record, dict) and (
                metadata is None or isinstance(metadata, dict) and "indexed_team_id" not in metadata
            ):
                return None
            team_id = metadata.get("indexed_team_id") if isinstance(metadata, dict) else None
            if type(team_id) is not int or team_id <= 0:
                raise ValueError("Invalid indexed team ownership")
            return team_id
        except LightRagConfigurationError:
            raise
        except Exception as exc:
            raise LightRagDeleteFailedError("LightRAG indexed team lookup failed") from exc

    async def get_processed_worklog_ids(
        self, worklog_ids: list[int], *, require_completed: bool = True,
    ) -> set[int]:
        """완료된 전역 ID만 반환한다. 팀 native 상태는 공통 게이트와 별도로 확인한다."""
        if not worklog_ids:
            return set()
        try:
            if any(type(item) is not int or item <= 0 for item in worklog_ids):
                raise ValueError("Invalid worklog ID")
            rag = await self._get_initialized_rag()
            statuses = await rag.doc_status.get_by_ids([f"worklog-{item}" for item in worklog_ids])
            # PGDocStatusStorage 1.4.10은 전부 미존재이면 []를 반환한다.
            if statuses == []:
                return set()
            if not isinstance(statuses, list) or len(statuses) != len(worklog_ids):
                raise ValueError("Invalid status batch")
            result = set()
            for worklog_id, record in zip(worklog_ids, statuses):
                if record is None:
                    continue
                if not isinstance(record, dict) or record.get("status") not in {
                    "pending", "processing", "preprocessed", "processed", "failed",
                }:
                    raise ValueError("Invalid document status")
                metadata = record.get("metadata", {})
                if not isinstance(metadata, dict):
                    raise ValueError("Invalid document metadata")
                if record["status"] == "processed" and (
                    not require_completed or metadata.get("indexing_status") == "COMPLETED"
                ):
                    result.add(worklog_id)
            return result
        except LightRagConfigurationError:
            raise
        except Exception as exc:
            raise LightRagQueryFailedError("LightRAG processed document lookup failed") from exc

    async def query_worklogs(self, options: LightRagQueryOptions) -> LightRagQueryResult:
        """LightRAG `mode="mix"` query를 실행하고 raw 결과를 반환한다."""
        try:
            if options.enable_rerank:
                if not self._rerank_endpoint_configured():
                    raise LightRagConfigurationError(
                        "Reranking is requested but LIGHTRAG_RERANK_BINDING_HOST is not configured"
                    )
                if (
                    options.rerank_model
                    and options.rerank_model != self._settings.lightrag_rerank_model
                ):
                    raise LightRagConfigurationError(
                        "TEI loads one reranker model per container; request model must match LIGHTRAG_RERANK_MODEL"
                    )
            rag = await self._get_initialized_rag()
            param = self._build_query_param(options)
            raw = await asyncio.wait_for(
                rag.aquery_llm(
                    options.query, param=param, system_prompt=options.system_prompt
                ),
                timeout=self._settings.lightrag_query_timeout_seconds,
            )
            return LightRagQueryResult(raw=raw)
        except LightRagConfigurationError:
            raise
        except LightRagProviderUnavailableError:
            raise
        except TimeoutError as exc:
            raise LightRagQueryTimeoutError("LightRAG query timed out") from exc
        except Exception as exc:
            raise LightRagQueryFailedError("LightRAG query failed") from exc

    async def query_data(self, options: LightRagQueryOptions) -> dict[str, Any]:
        """LLM 응답 생성 없이 LightRAG 검색 후보만 회수한다."""
        try:
            rag = await self._get_initialized_rag()
            data = await asyncio.wait_for(
                rag.aquery_data(options.query, param=self._build_query_param(options)),
                timeout=self._settings.lightrag_query_timeout_seconds,
            )
            if not isinstance(data, dict):
                raise LightRagQueryFailedError("LightRAG query returned invalid retrieval data")
            return data
        except (LightRagConfigurationError, LightRagProviderUnavailableError, LightRagQueryFailedError):
            raise
        except TimeoutError as exc:
            raise LightRagQueryTimeoutError("LightRAG retrieval timed out") from exc
        except Exception as exc:
            raise LightRagQueryFailedError("LightRAG retrieval failed") from exc

    async def complete_scoped(
        self, query: str, *, system_prompt: str, stream: bool = False
    ) -> Any:
        """권한 확인을 마친 문맥으로만 Gemini 답변을 생성한다."""
        try:
            return await asyncio.wait_for(
                self._complete_answer(query, system_prompt=system_prompt, stream=stream),
                timeout=self._settings.lightrag_query_timeout_seconds,
            )
        except LightRagProviderUnavailableError:
            raise
        except TimeoutError as exc:
            raise LightRagQueryTimeoutError("LightRAG generation timed out") from exc
        except Exception as exc:
            raise LightRagQueryFailedError("LightRAG generation failed") from exc

    async def _complete_answer(self, prompt: str, **kwargs: Any) -> Any:
        """답변 호출에도 native LLM 동시성·타임아웃 제한을 유지한다."""
        rag = await self._get_initialized_rag()
        return await rag.llm_model_func(prompt, answer_generation=True, **kwargs)

    async def embed_query(self, query: str) -> list[float]:
        """현재 LightRAG embedding 모델로 캐시 검색용 query vector를 만든다."""
        try:
            rag = await self._get_initialized_rag()
            vectors = await rag.embedding_func([query])
            vector = vectors[0]
            if len(vector) != self._settings.embedding_dim:
                raise ValueError("LightRAG embedding dimension mismatch")
            return list(vector)
        except LightRagProviderUnavailableError:
            raise
        except Exception as exc:
            raise LightRagQueryFailedError("LightRAG query embedding failed") from exc

    async def query_worklogs_stream(
        self, options: LightRagQueryOptions
    ) -> tuple[Any, Any]:
        """LightRAG `mode="mix"` query를 stream 모드로 실행하고 (response_iterator, raw)를 반환한다."""
        try:
            if options.enable_rerank:
                if not self._rerank_endpoint_configured():
                    raise LightRagConfigurationError(
                        "Reranking is requested but LIGHTRAG_RERANK_BINDING_HOST is not configured"
                    )
                if (
                    options.rerank_model
                    and options.rerank_model != self._settings.lightrag_rerank_model
                ):
                    raise LightRagConfigurationError(
                        "TEI loads one reranker model per container; request model must match LIGHTRAG_RERANK_MODEL"
                    )
            rag = await self._get_initialized_rag()
            param = self._build_query_param(options)
            raw = await asyncio.wait_for(
                rag.aquery_llm(
                    options.query, param=param, system_prompt=options.system_prompt
                ),
                timeout=self._settings.lightrag_query_timeout_seconds,
            )
            llm_response = raw.get("llm_response") if isinstance(raw, dict) else {}
            response_iterator = llm_response.get("response_iterator")
            if response_iterator is None or not hasattr(response_iterator, "__anext__"):
                raise LightRagQueryFailedError(
                    "LightRAG stream query returned an invalid response iterator"
                )
            return response_iterator, raw
        except LightRagConfigurationError:
            raise
        except LightRagProviderUnavailableError:
            raise
        except TimeoutError as exc:
            raise LightRagQueryTimeoutError("LightRAG stream query timed out") from exc
        except Exception as exc:
            raise LightRagQueryFailedError("LightRAG stream query failed") from exc

    async def close(self) -> None:
        """초기화된 LightRAG storage를 finalize하고 singleton 재사용 상태를 초기화한다."""
        rag = self._rag
        if rag is not None and self._initialized:
            await rag.finalize_storages()
        self._rag = None
        self._initialized = False

    async def _get_initialized_rag(self) -> Any:
        """LightRAG instance를 lazy singleton 방식으로 초기화한다."""
        if self._rag is not None and self._initialized:
            return self._rag

        async with self._initialize_lock:
            if self._rag is not None and self._initialized:
                return self._rag

            rag = self._build_lightrag()
            try:
                await rag.initialize_storages()
                self._verify_storage_workspaces(rag)
            except BaseException:
                # LightRAG는 전체 초기화 전에는 finalize_storages를 생략한다.
                results = await asyncio.gather(*(
                    storage.finalize() for name in _SCOPE_STORAGE_NAMES
                    if (storage := getattr(rag, name, None)) is not None
                    and callable(getattr(storage, "finalize", None))
                ), return_exceptions=True)
                if any(isinstance(result, BaseException) for result in results):
                    logger.error("Workspace cleanup failed after initialization failure")
                raise
            self._rag = rag
            self._initialized = True
            return rag

    def _build_lightrag(self) -> Any:
        """현재 settings로 LightRAG instance를 생성한다."""
        if not self._settings.lightrag_workspace.strip():
            raise LightRagConfigurationError("LightRAG requires an explicit workspace")
        if self._expected_workspace is not None:
            if self._settings.lightrag_workspace.strip() != self._expected_workspace:
                raise LightRagConfigurationError("Team workspace configuration mismatch")
            override = os.environ.get("NEO4J_WORKSPACE", "").strip()
            if override and override != self._expected_workspace:
                raise LightRagConfigurationError("NEO4J_WORKSPACE overrides team isolation")
        self._ensure_required_config()
        _configure_postgres_environment(self._settings)
        _configure_qdrant_environment(self._settings)
        _configure_neo4j_environment(self._settings)
        dependencies = self._dependencies or _load_lightrag_dependencies()
        embedding_func = _build_gemini_embedding_func(
            settings_obj=self._settings,
            dependencies=dependencies,
        )
        llm_model_func = _build_gemini_llm_model_func(
            settings_obj=self._settings,
            dependencies=dependencies,
        )
        lightrag_kwargs = {
            "working_dir": self._settings.lightrag_working_dir,
            "llm_model_func": llm_model_func,
            "llm_model_name": self._settings.lightrag_llm_model,
            "embedding_func": embedding_func,
            "kv_storage": "PGKVStorage",
            "doc_status_storage": "PGDocStatusStorage",
            "vector_storage": self._settings.lightrag_vector_storage.strip(),
            "graph_storage": self._settings.lightrag_graph_storage.strip(),
            "vector_db_storage_cls_kwargs": {},
            "addon_params": {"language": self._settings.lightrag_kg_language},
        }
        if self._rerank_endpoint_configured():
            lightrag_kwargs["rerank_model_func"] = self._build_rerank_model_func(
                dependencies
            )
        workspace = self._settings.lightrag_workspace.strip()
        if workspace:
            lightrag_kwargs["workspace"] = workspace
        rag = dependencies.lightrag_class(**lightrag_kwargs)
        configure_lightrag_tracing(rag)
        self._verify_storage_workspaces(rag)
        for name in _PG_STORAGE_NAMES:
            _configure_pg_delete(getattr(rag, name))
        _configure_worklog_status_tracking(rag.doc_status)
        # 새 전역 workspace도 기존 vector 데이터를 자동 이관하지 않는다.
        for name in ("entities_vdb", "relationships_vdb", "chunks_vdb"):
            storage = getattr(rag, name)
            storage.initialize = partial(initialize_worklog_team_vector_storage, storage, self._settings)
            storage.finalize = partial(finalize_worklog_team_vector_storage, storage)
        rag.llm_response_cache = ValidatedWorklogExtractionCache(
            rag.llm_response_cache,
        )
        return rag

    def _verify_storage_workspaces(self, rag: Any) -> None:
        """네트워크 초기화 전에 vector/graph/KV의 실제 namespace가 같은지 확인한다."""
        expected = self._expected_workspace or self._settings.lightrag_workspace.strip()
        if getattr(rag, "workspace", None) != expected:
            raise LightRagConfigurationError("LightRAG workspace mismatch")
        for name in _SCOPE_STORAGE_NAMES:
            storage = getattr(rag, name, None)
            if getattr(storage, "workspace", None) != expected:
                raise LightRagConfigurationError(f"LightRAG {name} workspace mismatch")
            if name.endswith("_vdb") and getattr(storage, "effective_workspace", None) != expected:
                raise LightRagConfigurationError(f"LightRAG {name} effective workspace mismatch")

    def _build_query_param(self, options: LightRagQueryOptions) -> Any:
        """LightRAG QueryParam을 내부 정책값으로 구성한다."""
        if options.enable_rerank and not self._rerank_endpoint_configured():
            raise LightRagConfigurationError(
                "Reranking is requested but LIGHTRAG_RERANK_BINDING_HOST is not configured"
            )
        if (options.enable_rerank and options.rerank_model
                and options.rerank_model != self._settings.lightrag_rerank_model):
            raise LightRagConfigurationError(
                "TEI request model must match LIGHTRAG_RERANK_MODEL"
            )
        dependencies = self._dependencies or _load_lightrag_dependencies()
        async def answer_model_func(prompt: str, **kwargs: Any) -> Any:
            return await self._complete_answer(prompt, **kwargs)

        return dependencies.query_param_class(
            mode="mix",
            stream=bool(options.stream),
            include_references=True,
            top_k=options.top_k,
            chunk_top_k=options.chunk_top_k,
            response_type=options.response_type,
            enable_rerank=(
                bool(options.enable_rerank)
            ),
            # 함수 closure는 native asdict가 adapter와 live storage 전체를 deepcopy하지 않는다.
            model_func=answer_model_func,
        )

    def _rerank_endpoint_configured(self) -> bool:
        """Return whether an API endpoint is configured for reranking."""
        return bool(
            self._settings.lightrag_rerank_binding_host.strip()
        )

    def _build_rerank_model_func(
        self, dependencies: LightRagDependencies
    ) -> Callable[..., Awaitable[Any]]:
        """Build a TEI reranker compatible with its native request and response format."""
        rerank_api = dependencies.tei_rerank_api or _call_tei_rerank_api

        async def rerank_model_func(
            query: str,
            documents: list[str],
            top_n: int | None = None,
            extra_body: dict[str, Any] | None = None,
        ) -> Any:
            return await rerank_api(
                query=query,
                documents=documents,
                base_url=self._settings.lightrag_rerank_binding_host.strip(),
                api_key=self._settings.lightrag_rerank_api_key.strip() or None,
                top_n=top_n,
            )

        return rerank_model_func


    def _ensure_required_config(self) -> None:
        """LightRAG runtime에 필요한 설정이 있는지 확인한다."""
        if not self._settings.gemini_api_key_candidates:
            raise LightRagConfigurationError("GEMINI_API_KEY is required for LightRAG")
        if self._settings.lightrag_vector_storage.strip() != _QDRANT_VECTOR_STORAGE:
            raise LightRagConfigurationError(
                "LightRAG v4 requires QdrantVectorDBStorage for vector storage"
            )
        if not self._settings.lightrag_qdrant_url.strip():
            raise LightRagConfigurationError("LIGHTRAG_QDRANT_URL is required for Qdrant")
        if self._settings.lightrag_graph_storage.strip() != _NEO4J_GRAPH_STORAGE:
            raise LightRagConfigurationError(
                "LightRAG v4 requires Neo4JStorage for graph storage"
            )
        if not self._settings.neo4j_uri.strip():
            raise LightRagConfigurationError("NEO4J_URI is required for Neo4j")
        if not self._settings.neo4j_user.strip():
            raise LightRagConfigurationError("NEO4J_USER is required for Neo4j")
        if not self._settings.neo4j_password.strip():
            raise LightRagConfigurationError("NEO4J_PASSWORD is required for Neo4j")
        if not self._settings.lightrag_neo4j_database.strip():
            raise LightRagConfigurationError("LIGHTRAG_NEO4J_DATABASE is required for Neo4j")



async def _call_tei_rerank_api(
    *,
    query: str,
    documents: list[str],
    base_url: str,
    api_key: str | None,
    top_n: int | None = None,
) -> list[dict[str, Any]]:
    """Call TEI's native `/rerank` endpoint and adapt its ranks for LightRAG."""
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    payload = {
        "query": query,
        "texts": documents,
        "raw_scores": False,
        "truncate": True,
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(base_url, json=payload, headers=headers) as response:
            if response.status >= 400:
                raise RuntimeError(f"TEI rerank API returned HTTP {response.status}")
            ranks = await response.json()
    if not isinstance(ranks, list):
        raise ValueError("TEI rerank API response must be a list")
    normalized = [
        {"index": rank["index"], "relevance_score": rank["score"]}
        for rank in ranks
    ]
    normalized.sort(key=lambda rank: rank["relevance_score"], reverse=True)
    return normalized[:top_n] if top_n is not None else normalized

    """방어로직"""

def _load_lightrag_dependencies() -> LightRagDependencies:
    """LightRAG package와 Gemini wrapper를 지연 import한다."""
    try:
        from lightrag import LightRAG
        from lightrag.base import QueryParam
        from lightrag.llm.gemini import gemini_embed, gemini_model_complete
        from lightrag.utils import wrap_embedding_func_with_attrs
        from app.light.v4.service.taskgroup_search import apply_taskgroup_search_patch

        apply_taskgroup_search_patch()
    except Exception as exc:  # pragma: no cover - 환경별 import 실패는 config error로만 노출한다.
        raise LightRagConfigurationError("LightRAG dependency import failed") from exc

    return LightRagDependencies(
        lightrag_class=LightRAG,
        gemini_model_complete=gemini_model_complete,
        gemini_embed=gemini_embed,
        embedding_wrapper=wrap_embedding_func_with_attrs,
        query_param_class=QueryParam,
        tei_rerank_api=_call_tei_rerank_api,
    )


def _build_gemini_llm_model_func(
    *,
    settings_obj: Settings,
    dependencies: LightRagDependencies,
) -> Callable[..., Awaitable[str]]:
    """Settings 기반 Gemini completion wrapper를 만든다.

    LightRAG query 중 Gemini가 503/429 같은 일시 장애를 반환하면 같은 모델에서
    API key 후보를 먼저 순회한다. lower-model fallback은 명시 설정된 경우에만
    모든 key/attempt 소진 후 escape hatch로 사용한다. prompt/API key/raw 응답은 로그에 남기지 않는다.
    """

    async def provider_complete(
        prompt: str,
        system_prompt: str | None = None,
        history_messages: list[dict[str, Any]] | None = None,
        keyword_extraction: bool = False,
        answer_generation: bool = False,
        **kwargs: Any,
    ) -> str:
        candidates = (settings_obj.lightrag_answer_llm_model_candidates if answer_generation
                      else settings_obj.lightrag_llm_model_candidates)
        if answer_generation:
            # native Gemini helper의 전역 model_name이 별도 답변 모델을 덮어쓰지 않게 한다.
            kwargs.pop("hashing_kv", None)
        key_candidates = settings_obj.gemini_api_key_candidates
        max_attempts = settings_obj.lightrag_query_llm_retry_attempts_per_model
        last_retryable_exc: BaseException | None = None

        for model_name in candidates:
            for attempt_index in range(max_attempts):
                for key_index, api_key in enumerate(key_candidates):
                    try:
                        return await dependencies.gemini_model_complete(
                            prompt,
                            system_prompt=system_prompt,
                            history_messages=history_messages or [],
                            keyword_extraction=keyword_extraction,
                            api_key=api_key,
                            model_name=model_name,
                            **kwargs,
                        )
                    except Exception as exc:
                        if not is_retryable_ai_error(exc):
                            raise

                        last_retryable_exc = exc
                        next_key_exists = key_index + 1 < len(key_candidates)
                        if next_key_exists:
                            logger.warning(
                                "LightRAG Gemini query key failover scheduled "
                                "model=%s attempt=%s/%s key_index=%s next_key_index=%s "
                                "error=%s status=%s",
                                model_name,
                                attempt_index + 1,
                                max_attempts,
                                key_index,
                                key_index + 1,
                                exc.__class__.__name__,
                                _extract_ai_error_status(exc),
                            )
                            continue
                        continue
                next_attempt_exists = attempt_index + 1 < max_attempts
                if next_attempt_exists:
                    delay = _compute_lightrag_llm_retry_delay(
                        settings_obj,
                        attempt_index=attempt_index,
                    )
                    logger.warning(
                        "LightRAG Gemini query retry scheduled after all API keys "
                        "model=%s attempt=%s/%s delay=%.2fs error=%s status=%s",
                        model_name,
                        attempt_index + 2,
                        max_attempts,
                        delay,
                        last_retryable_exc.__class__.__name__ if last_retryable_exc else None,
                        _extract_ai_error_status(last_retryable_exc) if last_retryable_exc else None,
                    )
                    if delay > 0:
                        await asyncio.sleep(delay)

        logger.error(
            "LightRAG Gemini query exhausted all API key/model candidates "
            "models=%s key_count=%s last_error=%s status=%s",
            candidates,
            len(key_candidates),
            last_retryable_exc.__class__.__name__ if last_retryable_exc else None,
            _extract_ai_error_status(last_retryable_exc) if last_retryable_exc else None,
        )
        raise LightRagProviderUnavailableError(
            "Gemini provider unavailable after LightRAG API key/model fallbacks"
        ) from last_retryable_exc

    async def llm_model_func(
        prompt: str,
        system_prompt: str | None = None,
        history_messages: list[dict[str, Any]] | None = None,
        keyword_extraction: bool = False,
        answer_generation: bool = False,
        **kwargs: Any,
    ) -> str:
        if answer_generation and not keyword_extraction:
            return await provider_complete(
                prompt, system_prompt=system_prompt, history_messages=history_messages,
                answer_generation=True, **kwargs,
            )
        if is_body_extraction(system_prompt, keyword_extraction):
            return await complete_body_extraction(
                provider_complete, prompt, history_messages=history_messages,
                system_prompt=system_prompt,
                keyword_extraction=False, **kwargs,
            )
        return await provider_complete(
            prompt, system_prompt=system_prompt, history_messages=history_messages,
            keyword_extraction=keyword_extraction, **kwargs,
        )

    return restore_lightrag_context(llm_model_func)


def _compute_lightrag_llm_retry_delay(
    settings_obj: Settings,
    *,
    attempt_index: int,
) -> float:
    """LightRAG query LLM retry delay를 exponential backoff + jitter로 계산한다."""
    initial_delay = settings_obj.lightrag_query_llm_retry_initial_delay
    if initial_delay <= 0:
        return 0.0

    max_delay = settings_obj.lightrag_query_llm_retry_max_delay
    base_delay = min(max_delay, initial_delay * (2**attempt_index))
    jitter = random.uniform(0, min(base_delay * 0.25, 0.25))
    return min(max_delay, base_delay + jitter)


def _extract_ai_error_status(exc: BaseException) -> int | None:
    """Provider 예외의 status code를 로그용으로 추출한다."""
    for attr_name in ("status_code", "code"):
        value = getattr(exc, attr_name, None)
        if value is None:
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def _build_gemini_embedding_func(
    *,
    settings_obj: Settings,
    dependencies: LightRagDependencies,
) -> Callable[[list[str]], Awaitable[Any]]:
    """Settings 기반 Gemini embedding wrapper를 만든다."""

    @dependencies.embedding_wrapper(
        embedding_dim=settings_obj.embedding_dim,
        max_token_size=settings_obj.lightrag_embedding_max_token_size,
        model_name=settings_obj.lightrag_embedding_model,
    )
    @restore_lightrag_context
    async def embedding_func(texts: list[str]) -> Any:
        key_candidates = settings_obj.gemini_api_key_candidates
        last_retryable_exc: BaseException | None = None

        for key_index, api_key in enumerate(key_candidates):
            try:
                return await dependencies.gemini_embed.func(
                    texts,
                    api_key=api_key,
                    model=settings_obj.lightrag_embedding_model,
                    embedding_dim=settings_obj.embedding_dim,
                    max_token_size=settings_obj.lightrag_embedding_max_token_size,
                )
            except Exception as exc:
                if not is_retryable_ai_error(exc):
                    raise

                last_retryable_exc = exc
                next_key_exists = key_index + 1 < len(key_candidates)
                if next_key_exists:
                    logger.warning(
                        "LightRAG Gemini embedding key failover scheduled "
                        "key_index=%s next_key_index=%s error=%s status=%s",
                        key_index,
                        key_index + 1,
                        exc.__class__.__name__,
                        _extract_ai_error_status(exc),
                    )
                    continue
                break

        logger.error(
            "LightRAG Gemini embedding exhausted all API key candidates "
            "key_count=%s last_error=%s status=%s",
            len(key_candidates),
            last_retryable_exc.__class__.__name__ if last_retryable_exc else None,
            _extract_ai_error_status(last_retryable_exc) if last_retryable_exc else None,
        )
        raise last_retryable_exc or LightRagProviderUnavailableError(
            "Gemini embedding provider unavailable after API key failover"
        )

    return embedding_func


_lightrag_worklog_index_adapter: LightRagWorklogIndexAdapter | None = None


def get_lightrag_worklog_index_adapter() -> LightRagWorklogIndexAdapter:
    """LightRAG 업무일지 index adapter singleton을 반환한다."""
    global _lightrag_worklog_index_adapter
    if _lightrag_worklog_index_adapter is None:
        _lightrag_worklog_index_adapter = LightRagWorklogIndexAdapter()
    return _lightrag_worklog_index_adapter


async def close_lightrag_worklog_index_adapter() -> None:
    """FastAPI shutdown/lifespan에서 호출할 수 있는 LightRAG finalize 경계.
        lightRAG가 여러 DB들에 connect 열어놓은거 닫는 함수. FastAPI 종료될 때 호출 되어야함.
    """
    global _lightrag_worklog_index_adapter
    if _lightrag_worklog_index_adapter is not None:
        await _lightrag_worklog_index_adapter.close()
    _lightrag_worklog_index_adapter = None


def _configure_worklog_status_tracking(storage: Any) -> None:
    """native FAILED upsert가 보상 삭제용 chunks_list를 비우는 것을 막는다."""
    if getattr(storage, "_worklog_status_tracking", False):
        return
    original = storage.upsert
    pending: dict[str, list[str]] = {}
    storage._worklog_pending_chunks = pending
    pending_status: dict[str, dict[str, Any]] = {}
    storage._worklog_pending_status = pending_status

    async def upsert(records: dict[str, dict[str, Any]]) -> None:
        """동시 청크 쓰기 전에 받은 ID를 상태 쓰기 실패 후에도 보존한다."""
        for key, record in records.items():
            if "chunks_list" in record:
                pending[key] = list(dict.fromkeys([*pending.get(key, []), *record["chunks_list"]]))
        merged = {}
        for key, record in records.items():
            previous = await storage.get_by_id(key) or {}
            chunks = list(dict.fromkeys([*previous.get("chunks_list", []), *pending.get(key, [])]))
            metadata = {**previous.get("metadata", {}), **record.get("metadata", {})}
            if record.get("status") != "processed":
                metadata["indexing_status"] = "STAGING"
            merged[key] = {**record, "metadata": metadata, "chunks_list": chunks, "chunks_count": len(chunks)}
        pending_status.update(merged)
        await original(merged)
        for key, record in merged.items():
            if record.get("status") == "processed":
                pending.pop(key, None)
                pending_status.pop(key, None)

    storage.upsert = upsert
    storage._worklog_status_tracking = True


def _configure_pg_delete(storage: Any) -> None:
    """1.4.10 PG delete의 예외 은폐를 보완하고 실제 삭제 후조건을 확인한다."""
    async def delete(ids: list[str]) -> None:
        if not ids:
            return
        from lightrag.kg.postgres_impl import namespace_to_table_name

        table = namespace_to_table_name(storage.namespace)
        if not table:
            raise LightRagConfigurationError("Unknown PostgreSQL storage namespace")
        # native와 같은 SQL·client를 사용하되 DB 예외를 삼키지 않는다.
        await storage.db.execute(
            f"DELETE FROM {table} WHERE workspace=$1 AND id = ANY($2)",
            {"workspace": storage.workspace, "ids": ids},
        )
        records = await storage.get_by_ids(ids)
        if (not isinstance(records, list) or (records and len(records) != len(ids))
                or any(record is not None for record in records)):
            raise LightRagDeleteFailedError("PostgreSQL records remain after deletion")

    # 함수 closure는 native asdict가 live DB 연결을 deepcopy하지 않게 한다.
    storage.delete = delete


def _configure_postgres_environment(settings_obj: Settings) -> None:
    """기존 업무 DB와 분리된 접속 정보만 native PG 저장소에 전달한다."""
    config = configparser.ConfigParser()
    try:
        config.read("config.ini", encoding="utf-8")
        override = config.get("postgres", "workspace", fallback="").strip()
    except (configparser.Error, OSError):
        raise LightRagConfigurationError("Invalid PostgreSQL config.ini") from None
    if override or os.environ.get("POSTGRES_WORKSPACE", "").strip():
        raise LightRagConfigurationError("POSTGRES_WORKSPACE/config.ini workspace overrides isolation")
    try:
        url = make_url(settings_obj.lightrag_pg_dsn)
        source = make_url(settings_obj.pgvector_dsn)
        if (url.drivername not in {"postgresql", "postgresql+asyncpg"}
                or not url.host or not url.username or not url.password or not url.database
                or url.database == source.database or url.query):
            raise ValueError
        port = url.port or 5432
        if not 0 < port < 65536:
            raise ValueError
    except (ArgumentError, ValueError, TypeError):
        # URL 파서 예외는 원본 DSN을 포함할 수 있으므로 그대로 노출하지 않는다.
        raise LightRagConfigurationError("LIGHTRAG_PG_DSN must specify a separate PostgreSQL database") from None
    os.environ.update({
        "POSTGRES_HOST": url.host, "POSTGRES_PORT": str(port),
        "POSTGRES_USER": url.username, "POSTGRES_PASSWORD": url.password,
        "POSTGRES_DATABASE": url.database, "POSTGRES_ENABLE_VECTOR": "false",
    })


def _configure_qdrant_environment(settings_obj: Settings) -> None:
    """Bridge Settings values into LightRAG v1.4.10's Qdrant env contract."""
    os.environ["QDRANT_URL"] = settings_obj.lightrag_qdrant_url.strip()
    os.environ.pop("QDRANT_WORKSPACE", None)
    qdrant_api_key = settings_obj.lightrag_qdrant_api_key.strip()
    if qdrant_api_key:
        os.environ["QDRANT_API_KEY"] = qdrant_api_key
    else:
        os.environ.pop("QDRANT_API_KEY", None)


def _configure_neo4j_environment(settings_obj: Settings) -> None:
    """Bridge project NEO4J_USER settings into LightRAG's NEO4J_USERNAME contract."""
    os.environ["NEO4J_URI"] = settings_obj.neo4j_uri.strip()
    os.environ["NEO4J_USERNAME"] = settings_obj.neo4j_user.strip()
    os.environ["NEO4J_PASSWORD"] = settings_obj.neo4j_password
    os.environ["NEO4J_DATABASE"] = settings_obj.lightrag_neo4j_database.strip()
