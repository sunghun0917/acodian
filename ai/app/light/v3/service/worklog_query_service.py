"""LightRAG v3 업무일지 query orchestration service 경계."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
import json
import re
from typing import Any

from app.config.settings import Settings, settings
from app.light.v3.model.worklog_query import (
    WorklogLightQueryRequest,
    WorklogLightQueryResponse,
    WorklogLightReferenceItem,
)
from app.light.v3.service.lightrag_adapter import (
    LightRagQueryFailedError,
    LightRagQueryOptions,
    LightRagQueryTimeoutError,
    get_lightrag_worklog_index_adapter,
)
from app.light.v3.prompt.worklog_query_prompt import build_worklog_query_system_prompt
from app.light.v3.service.worklog_source_row_reader import WorklogLightSourceRowReader


from app.light.v3.service.worklog_query_cache import WorklogQueryCache


_WORKLOG_PATH = re.compile(r"worklog://([1-9][0-9]*)")
_WORKLOG_ENTITY = re.compile(r"^Worklog:([1-9][0-9]*)$")


class LightWorklogQueryService:
    """내부 검증용 LightRAG 업무일지 query 유스케이스 진입점."""

    def __init__(
        self,
        *,
        settings_obj: Settings = settings,
        adapter_factory: Callable[..., Any] = get_lightrag_worklog_index_adapter,
        source_reader: Callable[[list[int]], Awaitable[dict[int, Any]]] | None = None,
        cache: WorklogQueryCache | None = None,
        embedder: Callable[[str], Awaitable[list[float]]] | None = None,
    ) -> None:
        """settings와 LightRAG adapter/source reader, 캐시를 구성한다."""
        self._settings = settings_obj
        self._adapter_factory = adapter_factory
        self._source_reader = source_reader or WorklogLightSourceRowReader().fetch_source_rows
        self._cache = cache
        self._embedder = embedder

    async def _get_embedding(self, text: str) -> list[float] | None:
        """질의 임베딩을 구한다."""
        if self._embedder is not None:
            return await self._embedder(text)
        try:
            from app.client.gemini_client import get_gemini_client
            results = await get_gemini_client().embed([text])
            return results[0] if results else None
        except Exception:
            return None

    async def query_worklogs(
        self,
        request: WorklogLightQueryRequest,
    ) -> WorklogLightQueryResponse:
        """LightRAG query를 실행하고 native answer와 references를 정규화한다."""
        if request.allowed_team_ids == []:
            return WorklogLightQueryResponse(answer="", references=[], internalOnly=True)

        scope = WorklogQueryCache.scope_key(request.allowed_team_ids)
        signature = WorklogQueryCache.signature(
            request.query,
            top_k=self._settings.lightrag_query_top_k,
            chunk_top_k=self._settings.lightrag_query_chunk_top_k,
            response_type=self._settings.lightrag_query_response_type,
            enable_rerank=(
                request.enable_rerank
                if request.enable_rerank is not None
                else self._settings.lightrag_rerank_enabled
            ),
            rerank_model=request.rerank_model,
            llm_model=self._settings.gemini_model,
            embedding_model=self._settings.embedding_model,
        )

        # 1. Exact Cache Check
        if self._cache is not None:
            try:
                exact_hit = await self._cache.get_exact(scope, signature)
                if exact_hit is not None:
                    return exact_hit
            except Exception:
                pass

        # 2. Semantic Cache Check
        embedding = None
        if self._cache is not None:
            try:
                embedding = await self._get_embedding(request.query)
                if embedding is not None:
                    semantic_hit = await self._cache.get_semantic(scope, embedding)
                    if semantic_hit is not None:
                        return semantic_hit
            except Exception:
                pass

        options = self._build_options(request)
        adapter = self._adapter_factory()
        if request.allowed_team_ids is not None:
            scoped = await self._authorized_context(request, options, adapter)
            if scoped is None:
                return WorklogLightQueryResponse(answer="", references=[], internalOnly=True)
            prompt, references = scoped
            answer = await adapter.complete_scoped(request.query, system_prompt=prompt)
            if not isinstance(answer, str) or not answer.strip():
                raise LightRagQueryFailedError("LightRAG query returned empty LLM response")
            response = WorklogLightQueryResponse(
                answer=answer.strip(), references=references, internalOnly=True,
            )
        else:
            result = await adapter.query_worklogs(options)
            raw = result.raw
            response = WorklogLightQueryResponse(
                answer=_extract_answer(raw),
                references=_extract_references(raw),
                internalOnly=True,
            )

        # 3. Put into Cache
        if self._cache is not None:
            try:
                if embedding is None:
                    embedding = await self._get_embedding(request.query)
                if embedding is not None:
                    await self._cache.put(scope, signature, embedding, response)
            except Exception:
                pass

        return response

    def _build_options(
        self, request: WorklogLightQueryRequest, *, stream: bool = False
    ) -> LightRagQueryOptions:
        """요청과 서버 설정을 LightRAG 실행 옵션으로 고정한다."""
        return LightRagQueryOptions(
            query=request.query,
            top_k=self._settings.lightrag_query_top_k,
            chunk_top_k=self._settings.lightrag_query_chunk_top_k,
            response_type=self._settings.lightrag_query_response_type,
            enable_rerank=(
                request.enable_rerank
                if request.enable_rerank is not None
                else self._settings.lightrag_rerank_enabled
            ),
            rerank_model=request.rerank_model,
            system_prompt=build_worklog_query_system_prompt(
                worklog_detail_base_url=self._settings.worklog_detail_base_url,
            ),
            stream=stream,
        )

    async def _authorized_context(
        self, request: WorklogLightQueryRequest, options: LightRagQueryOptions, adapter: Any
    ) -> tuple[str, list[WorklogLightReferenceItem]] | None:
        """KG는 후보 ID만 제공하고, 실제 LLM 입력은 권한 확인된 원본만 사용한다."""
        retrieved = await adapter.query_data(options)
        return await self._authorized_context_from_data(request, options, retrieved)

    async def _authorized_context_from_data(
        self, request: WorklogLightQueryRequest, options: LightRagQueryOptions,
        retrieved: dict[str, Any],
    ) -> tuple[str, list[WorklogLightReferenceItem]] | None:
        """검색 후보의 원본을 재조회해 요청자의 팀 범위만 LLM 문맥으로 구성한다."""
        if not isinstance(retrieved, dict):
            raise LightRagQueryFailedError("LightRAG query returned invalid retrieval data")
        if retrieved.get("status") == "failure":
            return None
        data = retrieved.get("data")
        if not isinstance(data, dict):
            raise LightRagQueryFailedError("LightRAG query returned invalid retrieval data")
        candidate_ids = _candidate_worklog_ids(data)
        if not candidate_ids:
            return None
        rows = await self._source_reader(candidate_ids)
        allowed = set(request.allowed_team_ids or [])
        selected = [
            rows[worklog_id] for worklog_id in candidate_ids
            if worklog_id in rows and rows[worklog_id].team_id in allowed
        ]
        if not selected:
            return None
        context = "\n\n".join(
            f"worklog_id: {row.worklog_id}\nfile_path: worklog://{row.worklog_id}"
            f"\ntitle: {row.title}\nrequest_content: {row.request_content or ''}"
            f"\nwork_content: {row.work_content}"
            for row in selected
        )
        prompt = (options.system_prompt or "").format(
            response_type=options.response_type,
            user_prompt=request.query,
            context_data=context,
        )
        references = [
            WorklogLightReferenceItem(
                referenceId=f"worklog-{row.worklog_id}",
                filePath=f"worklog://{row.worklog_id}",
            )
            for row in selected
        ]
        return prompt, references

    async def query_worklogs_stream(
        self,
        request: WorklogLightQueryRequest,
    ) -> AsyncIterator[dict[str, Any]]:
        """LightRAG streaming query를 실행하고 SSE 이벤트를 yield하는 generator를 반환한다."""
        if request.allowed_team_ids == []:
            yield {"event": "done", "data": json.dumps({"references": [], "done": True})}
            return
        options = self._build_options(request, stream=True)
        start_time = asyncio.get_running_loop().time()
        timeout_seconds = float(self._settings.lightrag_query_timeout_seconds)
        deadline = start_time + timeout_seconds

        adapter = self._adapter_factory()
        if request.allowed_team_ids is not None:
            scoped = await adapter.query_data(options)
            # _authorized_context performs source re-read; reuse the same retrieved data.
            authorized = await self._authorized_context_from_data(request, options, scoped)
            if authorized is None:
                yield {"event": "done", "data": json.dumps({"references": [], "done": True})}
                return
            prompt, reference_items = authorized
            response_iterator = await adapter.complete_scoped(
                request.query, system_prompt=prompt, stream=True,
            )
            references = [item.model_dump(by_alias=True) for item in reference_items]
        else:
            response_iterator, raw = await adapter.query_worklogs_stream(options)
            references = [item.model_dump(by_alias=True) for item in _extract_references(raw)]

        if response_iterator is None or not hasattr(response_iterator, "__anext__"):
            raise LightRagQueryFailedError(
                "LightRAG stream query returned an invalid response iterator"
            )

        try:
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise LightRagQueryTimeoutError("LightRAG stream token iteration timed out")
                try:
                    token = await asyncio.wait_for(
                        anext(response_iterator),
                        timeout=remaining,
                    )
                    if token:
                        yield {"event": "token", "data": token}
                except StopAsyncIteration:
                    break
        except TimeoutError as exc:
            raise LightRagQueryTimeoutError("LightRAG stream timed out") from exc

        yield {"event": "done", "data": json.dumps({"references": references, "done": True})}


def _extract_answer(raw: Any) -> str:
    """LightRAG raw payload에서 non-empty LLM answer를 안전하게 추출한다."""
    if not isinstance(raw, dict):
        raise LightRagQueryFailedError("LightRAG query returned invalid raw response")

    llm_response = raw.get("llm_response")
    if not isinstance(llm_response, dict):
        raise LightRagQueryFailedError("LightRAG query returned invalid LLM response")

    content = llm_response.get("content")
    if not isinstance(content, str):
        raise LightRagQueryFailedError("LightRAG query returned empty LLM response")

    answer = content.strip()
    if not answer:
        raise LightRagQueryFailedError("LightRAG query returned empty LLM response")
    return answer


def _candidate_worklog_ids(data: dict[str, Any]) -> list[int]:
    """LightRAG 검색 결과의 provenance에서 원본 업무일지 ID를 순서대로 회수한다."""
    found: dict[int, None] = {}
    for section in ("chunks", "entities", "relationships", "references"):
        items = data.get(section)
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            for field in ("file_path", "full_doc_id"):
                value = item.get(field)
                if not isinstance(value, str):
                    continue
                for match in _WORKLOG_PATH.finditer(value):
                    found[int(match.group(1))] = None
                if field == "full_doc_id" and value.startswith("worklog-"):
                    suffix = value.removeprefix("worklog-")
                    if suffix.isdecimal() and int(suffix) > 0:
                        found[int(suffix)] = None
            for field in ("entity_name", "src_id", "tgt_id"):
                value = item.get(field)
                match = _WORKLOG_ENTITY.fullmatch(value) if isinstance(value, str) else None
                if match:
                    found[int(match.group(1))] = None
    return list(found)


def _extract_references(raw: Any) -> list[WorklogLightReferenceItem]:
    """LightRAG raw payload의 data.references를 업무일지 reference로 변환한다."""
    if not isinstance(raw, dict):
        return []

    data = raw.get("data")
    if not isinstance(data, dict):
        return []

    references = data.get("references")
    if not isinstance(references, list):
        return []

    return [
        _build_reference_item(reference)
        for reference in references
        if isinstance(reference, dict)
    ]


def _build_reference_item(raw_reference: dict[str, Any]) -> WorklogLightReferenceItem:
    """Raw reference 1건을 응답 item으로 변환한다."""
    return WorklogLightReferenceItem(
        referenceId=str(raw_reference.get("reference_id", "")),
        filePath=str(raw_reference.get("file_path", "")),
    )
