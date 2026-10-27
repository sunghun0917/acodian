"""LightRAG v4 업무일지 index 라우터."""

from hmac import compare_digest
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.config.settings import settings
from app.light.v4.model.worklog_index import (
    WorklogLightDeleteRequest,
    WorklogLightDeleteResponse,
    WorklogLightIndexRequest,
    WorklogLightIndexResponse,
)
from app.light.v4.service.worklog_index_dispatch_service import WorklogIndexDispatchService

router = APIRouter(prefix="/light/worklogs-v4", tags=["light-worklogs-v4"])

worklog_index_dispatch_service = WorklogIndexDispatchService()


def get_worklog_index_dispatch_service() -> WorklogIndexDispatchService:
    """LightRAG v4 업무일지 index dispatch service dependency를 반환한다."""
    return worklog_index_dispatch_service


def validate_index_batch_size(worklog_ids: list[int]) -> None:
    """LightRAG index 요청 batch 크기 제한을 검증한다."""
    if len(worklog_ids) > settings.lightrag_index_max_batch_size:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="worklogIds exceeds LIGHTRAG_INDEX_MAX_BATCH_SIZE",
        )


def _require_internal_token(req: Request) -> None:
    """색인·재색인·삭제 변경 경로를 내부 서비스 토큰으로 보호한다."""
    expected_token = settings.ai_internal_token
    if not expected_token:
        raise HTTPException(status_code=503, detail="AI_INTERNAL_TOKEN_NOT_CONFIGURED")
    if not compare_digest(req.headers.get("x-ai-internal-token", ""), expected_token):
        raise HTTPException(status_code=403, detail="AI_INTERNAL_ACCESS_DENIED")


@router.post("/index", response_model=WorklogLightIndexResponse, status_code=status.HTTP_202_ACCEPTED)
async def index_worklogs(
    request: WorklogLightIndexRequest,
    req: Request,
    service: Annotated[WorklogIndexDispatchService, Depends(get_worklog_index_dispatch_service)],
) -> WorklogLightIndexResponse:
    """인증된 내부 요청에서 업무일지 색인 작업을 큐에 비동기 접수한다."""
    _require_internal_token(req)
    worklog_ids = [int(worklog_id) for worklog_id in request.worklog_ids]
    validate_index_batch_size(worklog_ids)
    return await service.dispatch_index(worklog_ids, request.request_id, "INDEX")


@router.post("/reindex", response_model=WorklogLightIndexResponse, status_code=status.HTTP_202_ACCEPTED)
async def reindex_worklogs(
    request: WorklogLightIndexRequest,
    req: Request,
    service: Annotated[WorklogIndexDispatchService, Depends(get_worklog_index_dispatch_service)],
) -> WorklogLightIndexResponse:
    """인증된 내부 요청에서 업무일지 재색인 작업을 큐에 비동기 접수한다."""
    _require_internal_token(req)
    worklog_ids = [int(worklog_id) for worklog_id in request.worklog_ids]
    validate_index_batch_size(worklog_ids)
    return await service.dispatch_index(worklog_ids, request.request_id, "REINDEX")


@router.post("/delete", response_model=WorklogLightDeleteResponse, status_code=status.HTTP_202_ACCEPTED)
async def delete_worklogs(
    request: WorklogLightDeleteRequest,
    req: Request,
    service: Annotated[WorklogIndexDispatchService, Depends(get_worklog_index_dispatch_service)],
) -> WorklogLightDeleteResponse:
    """인증된 내부 요청에서만 업무일지 색인 삭제 작업을 큐에 비동기 접수한다."""
    _require_internal_token(req)
    worklog_ids = [int(worklog_id) for worklog_id in request.worklog_ids]
    validate_index_batch_size(worklog_ids)
    return await service.dispatch_delete(worklog_ids, request.request_id)
