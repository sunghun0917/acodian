"""LightRAG v3 업무일지 index 라우터."""

from hmac import compare_digest
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.config.settings import settings
from app.light.v3.model.worklog_index import (
    WorklogLightDeleteRequest,
    WorklogLightDeleteResponse,
    WorklogLightIndexRequest,
    WorklogLightIndexResponse,
)
from app.light.v3.service.lightrag_adapter import LightRagConfigurationError
from app.light.v3.service.worklog_index_service import LightWorklogIndexService

router = APIRouter(prefix="/light/worklogs-v3", tags=["light-worklogs-v3"])


# FastAPI의 싱글톤 주입 방식
worklog_index_service = LightWorklogIndexService()


def get_worklog_index_service() -> LightWorklogIndexService:
    """LightRAG v3 업무일지 index service dependency를 반환한다."""
    return worklog_index_service


def validate_index_batch_size(worklog_ids: list[int]) -> None:
    """LightRAG index 요청 batch 크기 제한을 검증한다."""
    if len(worklog_ids) > settings.lightrag_index_max_batch_size:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="worklogIds exceeds LIGHTRAG_INDEX_MAX_BATCH_SIZE",
        )


@router.post("/index", response_model=WorklogLightIndexResponse)
async def index_worklogs(
    request: WorklogLightIndexRequest,
    service: Annotated[LightWorklogIndexService, Depends(get_worklog_index_service)],
) -> WorklogLightIndexResponse:
    """업무일지 text document와 confirmed relation custom KG를 함께 index한다."""
    worklog_ids = [int(worklog_id) for worklog_id in request.worklog_ids]
    validate_index_batch_size(worklog_ids)

    try:
        return await service.index_worklogs(worklog_ids)
    except LightRagConfigurationError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="LIGHTRAG_CONFIGURATION_ERROR",
        ) from exc


@router.post("/reindex", response_model=WorklogLightIndexResponse)
async def reindex_worklogs(
    request: WorklogLightIndexRequest,
    req: Request,
    service: Annotated[LightWorklogIndexService, Depends(get_worklog_index_service)],
) -> WorklogLightIndexResponse:
    """인증된 내부 요청에서 최신 DB 상태로 업무일지 index를 교체한다."""
    _require_internal_token(req)
    worklog_ids = [int(worklog_id) for worklog_id in request.worklog_ids]
    validate_index_batch_size(worklog_ids)
    try:
        return await service.reindex_worklogs(worklog_ids)
    except LightRagConfigurationError as exc:
        raise HTTPException(status_code=500, detail="LIGHTRAG_CONFIGURATION_ERROR") from exc


def _require_internal_token(req: Request) -> None:
    """재색인·삭제 변경 경로를 내부 서비스 토큰으로 보호한다."""
    expected_token = settings.ai_internal_token
    if not expected_token:
        raise HTTPException(status_code=503, detail="AI_INTERNAL_TOKEN_NOT_CONFIGURED")
    if not compare_digest(req.headers.get("x-ai-internal-token", ""), expected_token):
        raise HTTPException(status_code=403, detail="AI_INTERNAL_ACCESS_DENIED")


@router.post("/delete", response_model=WorklogLightDeleteResponse)
async def delete_worklogs(
    request: WorklogLightDeleteRequest,
    req: Request,
    service: Annotated[LightWorklogIndexService, Depends(get_worklog_index_service)],
) -> WorklogLightDeleteResponse:
    """인증된 내부 요청에서만 업무일지 index를 삭제한다."""
    _require_internal_token(req)

    worklog_ids = [int(worklog_id) for worklog_id in request.worklog_ids]
    validate_index_batch_size(worklog_ids)
    try:
        return await service.delete_worklogs(worklog_ids)
    except LightRagConfigurationError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="LIGHTRAG_CONFIGURATION_ERROR",
        ) from exc
