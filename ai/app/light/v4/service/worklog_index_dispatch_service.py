"""LightRAG v4 업무일지 index Celery 작업 발행 서비스."""

import logging
from typing import Any
from uuid import UUID
from fastapi import HTTPException, status

from app.light.v4.model.worklog_index import (
    WorklogLightDeleteItem,
    WorklogLightDeleteResponse,
    WorklogLightIndexItem,
    WorklogLightIndexResponse,
)
from app.task.celery_app import celery_app

logger = logging.getLogger(__name__)


class WorklogIndexDispatchService:
    """색인·재색인·삭제 작업을 Redis/Celery worklog_index 큐에 발행한다."""

    async def dispatch_index(
        self,
        worklog_ids: list[int],
        request_id: UUID,
        operation: str,
    ) -> WorklogLightIndexResponse:
        """업무일지 색인 또는 재색인 작업을 큐에 비동기 발행한다."""
        if operation not in ("INDEX", "REINDEX"):
            raise ValueError(f"Invalid operation for dispatch_index: {operation}")

        items = self._dispatch(worklog_ids, request_id, operation)
        return WorklogLightIndexResponse(
            items=[
                WorklogLightIndexItem(
                    worklogId=item["worklog_id"],
                    requestId=item["request_id"],
                    taskId=item["task_id"],
                )
                for item in items
            ]
        )

    async def dispatch_delete(
        self,
        worklog_ids: list[int],
        request_id: UUID,
    ) -> WorklogLightDeleteResponse:
        """업무일지 삭제 작업을 큐에 비동기 발행한다."""
        items = self._dispatch(worklog_ids, request_id, "DELETE")
        return WorklogLightDeleteResponse(
            items=[
                WorklogLightDeleteItem(
                    worklogId=item["worklog_id"],
                    requestId=item["request_id"],
                    taskId=item["task_id"],
                )
                for item in items
            ]
        )

    def _dispatch(
        self,
        worklog_ids: list[int],
        request_id: UUID,
        operation: str,
    ) -> list[dict[str, Any]]:
        """작업을 건별로 Celery에 발행하며 실패 시 503을 발생시킨다."""
        items: list[dict[str, Any]] = []
        try:
            for worklog_id in worklog_ids:
                async_result = celery_app.send_task(
                    "worklog.light_index",
                    args=[worklog_id, str(request_id), operation],
                    queue="worklog_index",
                )
                items.append({
                    "worklog_id": worklog_id,
                    "request_id": request_id,
                    "task_id": async_result.id,
                })
            return items
        except Exception as exc:
            logger.exception("Failed to dispatch Celery light_index task. requestId=%s", request_id)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="CELERY_DISPATCH_FAILED",
            ) from exc
