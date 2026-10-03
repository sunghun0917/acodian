"""LightRAG v3 업무일지 query 라우터."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sse_starlette.sse import EventSourceResponse

from app.light.v3.model.worklog_query import (
    WorklogLightQueryRequest,
    WorklogLightQueryResponse,
)
from app.light.v3.service.lightrag_adapter import (
    LightRagConfigurationError,
    LightRagProviderUnavailableError,
    LightRagQueryFailedError,
    LightRagQueryTimeoutError,
)
from app.light.v3.service.taskgroup_search import (
    apply_taskgroup_search_patch,
    remove_taskgroup_search_patch,
)
from app.light.v3.service.worklog_query_service import LightWorklogQueryService

router = APIRouter(prefix="/light/worklogs-v3", tags=["light-worklogs-v3"])

worklog_query_service = LightWorklogQueryService()


def get_worklog_query_service() -> LightWorklogQueryService:
    """LightRAG v3 업무일지 query service dependency를 반환한다."""
    return worklog_query_service


@router.post(
    "/query",
    response_model=WorklogLightQueryResponse,
)
async def query_worklogs(
    request: WorklogLightQueryRequest,
    req: Request,
    service: Annotated[LightWorklogQueryService, Depends(get_worklog_query_service)],
) -> WorklogLightQueryResponse:
    """내부 검증용 LightRAG `mode="mix"` query를 실행한다."""
    disabled = req.headers.get("x-disable-taskgroup") == "true"
    if disabled:
        remove_taskgroup_search_patch()
    try:
        return await service.query_worklogs(request)
    except LightRagConfigurationError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="LIGHTRAG_CONFIGURATION_ERROR",
        ) from exc
    except LightRagQueryTimeoutError as exc:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail="LIGHTRAG_QUERY_TIMEOUT",
        ) from exc
    except LightRagProviderUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="LIGHTRAG_PROVIDER_UNAVAILABLE",
        ) from exc
    except LightRagQueryFailedError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="LIGHTRAG_QUERY_FAILED",
        ) from exc
    finally:
        if disabled:
            apply_taskgroup_search_patch()


@router.post("/query/stream")
async def query_worklogs_stream(
    request: WorklogLightQueryRequest,
    req: Request,
    service: Annotated[LightWorklogQueryService, Depends(get_worklog_query_service)],
) -> EventSourceResponse:
    """내부 검증용 LightRAG `mode="mix"` query를 SSE 스트리밍으로 실행한다."""
    disabled = req.headers.get("x-disable-taskgroup") == "true"
    if disabled:
        remove_taskgroup_search_patch()

    async def event_generator():
        try:
            async for event in service.query_worklogs_stream(request):
                if await req.is_disconnected():
                    break
                yield event
        except LightRagConfigurationError:
            yield {"event": "error", "data": "LIGHTRAG_CONFIGURATION_ERROR"}
        except LightRagQueryTimeoutError:
            yield {"event": "error", "data": "LIGHTRAG_QUERY_TIMEOUT"}
        except LightRagProviderUnavailableError:
            yield {"event": "error", "data": "LIGHTRAG_PROVIDER_UNAVAILABLE"}
        except Exception:
            yield {"event": "error", "data": "LIGHTRAG_QUERY_FAILED"}
        finally:
            if disabled:
                apply_taskgroup_search_patch()

    return EventSourceResponse(
        event_generator(),
        headers={
            "X-Accel-Buffering": "no",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )
