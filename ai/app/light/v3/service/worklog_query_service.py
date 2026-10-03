"""LightRAG v3 업무일지 query orchestration service 경계."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
import json
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


class LightWorklogQueryService:
    """내부 검증용 LightRAG 업무일지 query 유스케이스 진입점."""

    def __init__(
        self,
        *,
        settings_obj: Settings = settings,
        adapter_factory: Callable[..., Any] = get_lightrag_worklog_index_adapter,
    ) -> None:
        """settings와 LightRAG adapter factory를 구성한다."""
        self._settings = settings_obj
        self._adapter_factory = adapter_factory

    async def query_worklogs(
        self,
        request: WorklogLightQueryRequest,
    ) -> WorklogLightQueryResponse:
        """LightRAG query를 실행하고 native answer와 references를 정규화한다."""
        options = LightRagQueryOptions(
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
        )
        result = await self._adapter_factory().query_worklogs(options)
        raw = result.raw

        return WorklogLightQueryResponse(
            answer=_extract_answer(raw),
            references=_extract_references(raw),
            internalOnly=True,
        )

    async def query_worklogs_stream(
        self,
        request: WorklogLightQueryRequest,
    ) -> AsyncIterator[dict[str, Any]]:
        """LightRAG streaming query를 실행하고 SSE 이벤트를 yield하는 generator를 반환한다."""
        options = LightRagQueryOptions(
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
            stream=True,
        )
        start_time = asyncio.get_running_loop().time()
        timeout_seconds = float(self._settings.lightrag_query_timeout_seconds)
        deadline = start_time + timeout_seconds

        response_iterator, raw = await self._adapter_factory().query_worklogs_stream(options)

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

        references = [
            item.model_dump(by_alias=True)
            for item in _extract_references(raw)
        ]
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
