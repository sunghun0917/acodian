"""LightRAG v3 업무일지 query 라우터."""

from __future__ import annotations

from hmac import compare_digest
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sse_starlette.sse import EventSourceResponse

from app.config.settings import settings
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
from app.light.v3.service.worklog_query_cache import WorklogQueryCache
from app.light.v3.service.worklog_query_service import LightWorklogQueryService

router = APIRouter(prefix="/light/worklogs-v3", tags=["light-worklogs-v3"])


def _create_default_service() -> LightWorklogQueryService:
    try:
        cache = WorklogQueryCache(settings_obj=settings)
    except Exception:
        cache = None
    return LightWorklogQueryService(settings_obj=settings, cache=cache)


worklog_query_service = _create_default_service()


def get_worklog_query_service() -> LightWorklogQueryService:
    """LightRAG v3 업무일지 query service dependency를 반환한다."""
    return worklog_query_service


def _require_internal_token(req: Request) -> None:
    """API가 검증한 팀 범위만 수락하도록 내부 서비스 토큰을 확인한다."""
    expected = settings.ai_internal_token
    if not expected:
        raise HTTPException(status_code=503, detail="AI_INTERNAL_TOKEN_NOT_CONFIGURED")
    supplied = req.headers.get("x-ai-internal-token", "")
    if not compare_digest(supplied, expected):
        raise HTTPException(status_code=403, detail="AI_INTERNAL_ACCESS_DENIED")


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
    _require_internal_token(req)
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
    _require_internal_token(req)
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
