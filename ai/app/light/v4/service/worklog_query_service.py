"""LightRAG v4 업무일지 query orchestration service 경계."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
import json
import re
from typing import Any

from app.config.settings import Settings, settings
from app.light.v4.model.worklog_query import (
    WorklogLightQueryRequest,
    WorklogLightQueryResponse,
    WorklogLightReferenceItem,
)
from app.light.v4.service.lightrag_adapter import (
    LightRagQueryFailedError,
    LightRagQueryOptions,
    LightRagQueryTimeoutError,
)
from app.light.v4.prompt.worklog_query_prompt import build_worklog_query_system_prompt
from app.light.v4.service.worklog_team_scope import get_lightrag_team_scope_adapter
from app.light.v4.store.worklog_team_copy_store import TeamCopy


_WORKLOG_PATH = re.compile(r"worklog://([1-9][0-9]*)")
_WORKLOG_ENTITY = re.compile(r"^Worklog:([1-9][0-9]*)$")
_MAX_SCOPED_PREDECESSOR_IDS = 100
_MAX_SCOPED_RELATION_CHARS = 12_000


class LightWorklogQueryService:
    """내부 검증용 LightRAG 업무일지 query 유스케이스 진입점."""

    def __init__(
        self,
        *,
        settings_obj: Settings = settings,
        team_adapter_factory: Callable[..., Any] = get_lightrag_team_scope_adapter,
        cache: Any = None,
        embedder: Any = None,
    ) -> None:
        """settings와 검색 adapter를 구성하며 응답 캐시는 사용하지 않는다."""
        self._settings = settings_obj
        self._team_adapter_factory = team_adapter_factory
        # shortcut: 응답 캐시는 DB 최신성 검증을 지원할 때까지 모든 검색에서 비활성화한다.

    async def query_worklogs(
        self,
        request: WorklogLightQueryRequest,
    ) -> WorklogLightQueryResponse:
        """검색 범위와 최신 DB 문맥 검증을 마친 뒤에만 답변을 생성한다."""
        options = self._build_options(request)
        try:
            async with asyncio.timeout(self._settings.lightrag_query_timeout_seconds):
                scoped = await self._indexed_context(request, options)
        except TimeoutError as exc:
            raise LightRagQueryTimeoutError("LightRAG query context timed out") from exc
        if scoped is None:
            return WorklogLightQueryResponse(answer="", references=[], internalOnly=True)
        prompt, references, adapter = scoped
        answer = await adapter.complete_scoped(request.query, system_prompt=prompt)
        if not isinstance(answer, str) or not answer.strip():
            raise LightRagQueryFailedError("LightRAG query returned empty LLM response")
        return WorklogLightQueryResponse(
            answer=answer.strip(), references=references, internalOnly=True,
        )

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

    async def _indexed_context(
        self, request: WorklogLightQueryRequest, options: LightRagQueryOptions
    ) -> tuple[str, list[WorklogLightReferenceItem], Any] | None:
        """토큰을 유지한 사본에서 검색과 권한 확인 및 스냅샷 문맥 생성을 마친다."""
        pool = self._team_adapter_factory()
        async with pool.read_copies(tuple(request.allowed_team_ids)) as copies:
            retrieved = await pool.query_data(options, copies)
            context = await self._indexed_context_from_data(request, options, retrieved, pool, copies)
            if context is None:
                return None
            team = min(copies)
            prompt, references = context
            return prompt, references, pool.adapter_for(team, copies[team])

    async def _indexed_context_from_data(
        self, request: WorklogLightQueryRequest, options: LightRagQueryOptions,
        retrieved: dict[str, Any], pool: Any, copies: dict[int, TeamCopy],
    ) -> tuple[str, list[WorklogLightReferenceItem]] | None:
        """권한이 확인된 사본 스냅샷 본문과 직접 선행 관계로 문맥을 구성한다."""
        if not isinstance(retrieved, dict):
            raise LightRagQueryFailedError("LightRAG query returned invalid retrieval data")
        if retrieved.get("status") == "failure":
            metadata = retrieved.get("metadata")
            if isinstance(metadata, dict) and metadata.get("failure_reason") == "no_results":
                return None
            raise LightRagQueryFailedError("LightRAG retrieval failed")
        data = retrieved.get("data")
        if not isinstance(data, dict):
            raise LightRagQueryFailedError("LightRAG query returned invalid retrieval data")
        candidate_ids = _candidate_worklog_ids(data)
        if not candidate_ids:
            return None
        rows = await pool.read_source_rows(candidate_ids, copies)
        selected = [
            rows[worklog_id] for worklog_id in candidate_ids
            if worklog_id in rows and rows[worklog_id].team_id in copies
        ]
        if not selected:
            return None

        authorized_rows = {row.worklog_id: row for row in selected}
        predecessor_ids = list(dict.fromkeys(
            predecessor.worklog_id
            for row in selected
            for predecessor in row.direct_predecessors
            if predecessor.worklog_id not in authorized_rows
        ))
        if len(predecessor_ids) > _MAX_SCOPED_PREDECESSOR_IDS:
            raise LightRagQueryFailedError("Scoped worklog relation limit exceeded")
        if predecessor_ids:
            predecessor_rows = await pool.read_source_rows(predecessor_ids, copies)
            authorized_rows.update(
                (worklog_id, predecessor_rows[worklog_id])
                for worklog_id in predecessor_ids
                if worklog_id in predecessor_rows
                and predecessor_rows[worklog_id].team_id in copies
            )

        body_context = "\n\n".join(
            f"worklog_id: {row.worklog_id}\nfile_path: worklog://{row.worklog_id}"
            f"\ntitle: {row.title}\nrequest_content: {row.request_content or ''}"
            f"\nwork_content: {row.work_content}"
            for row in selected
        )
        relation_lines = []
        for row in authorized_rows.values():
            relation_lines.extend((
                f"Worklog:{row.worklog_id} AUTHORED_BY {row.author_name} (User:{row.author_id})",
                f"Worklog:{row.worklog_id} BELONGS_TO {row.team_name} (Team:{row.team_id})",
            ))
            relation_lines.extend(
                f"Worklog:{row.worklog_id} HAS_TAG Tag:{tag.tag_id} ({tag.tag_name})"
                for tag in row.tags
            )
        relation_lines.extend(
            f"Worklog:{row.worklog_id} DEPENDS_ON Worklog:{predecessor.worklog_id} "
            f"({authorized_rows[predecessor.worklog_id].title})"
            for row in selected
            for predecessor in row.direct_predecessors
            if predecessor.worklog_id in authorized_rows
        )
        relation_context = "\n".join(relation_lines)
        if len(relation_context) > _MAX_SCOPED_RELATION_CHARS:
            raise LightRagQueryFailedError("Scoped worklog relation context limit exceeded")
        context = f"{body_context}\n\nconfirmed_relations:\n{relation_context}"
        prompt = (options.system_prompt or "").format(
            response_type=options.response_type,
            user_prompt=request.query,
            context_data=context,
        )
        prompt += (
            "\n\nThe confirmed_relations are permission-filtered. Do not infer missing "
            "relationships from similar text, and do not treat an absent relation as proof "
            "that no relation exists."
            " When answering who authored a worklog or which team owns it, include the "
            "exact display name and canonical ID as name (User:ID) or name (Team:ID). "
            "Use only the matching confirmed_relations; if the name is absent, say it is unknown."
        )
        references = [
            WorklogLightReferenceItem(
                referenceId=f"worklog-{row.worklog_id}",
                filePath=f"worklog://{row.worklog_id}",
            )
            for row in authorized_rows.values()
        ]
        return prompt, references

    async def query_worklogs_stream(
        self,
        request: WorklogLightQueryRequest,
    ) -> AsyncIterator[dict[str, Any]]:
        """LightRAG streaming query를 실행하고 SSE 이벤트를 yield하는 generator를 반환한다."""
        options = self._build_options(request, stream=True)
        start_time = asyncio.get_running_loop().time()
        timeout_seconds = float(self._settings.lightrag_query_timeout_seconds)
        deadline = start_time + timeout_seconds

        try:
            async with asyncio.timeout_at(deadline):
                authorized = await self._indexed_context(request, options)
                if authorized is not None:
                    prompt, reference_items, adapter = authorized
                    response_iterator = await adapter.complete_scoped(
                        request.query, system_prompt=prompt, stream=True,
                    )
        except TimeoutError as exc:
            raise LightRagQueryTimeoutError("LightRAG stream context or generation timed out") from exc
        if authorized is None:
            yield {"event": "done", "data": json.dumps({"references": [], "done": True})}
            return
        references = [item.model_dump(by_alias=True) for item in reference_items]

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
