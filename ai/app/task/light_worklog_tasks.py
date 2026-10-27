"""LightRAG v4 업무일지 index Celery 작업."""

import asyncio
import logging
from typing import Any
import uuid
import httpx
from celery.exceptions import Reject

from app.client.langfuse_client import trace_worklog_operation
from app.client.worklog_index_client import WorklogIndexClient
from app.config.settings import settings
from app.store.worklog_index_result_store import (
    PendingWorklogIndexResult, find_pending_light_index_result, persist_light_index_result,
)
from app.service.worklog_index_result_delivery import report_stored_result
from app.light.v4.service.lightrag_adapter import LightRagConfigurationError
from app.light.v4.service.worklog_index_service import (
    ERROR_WORKLOG_NOT_FOUND,
    LightWorklogIndexService,
)
from app.task.celery_app import celery_app
from app.task.worker_runner import worker_runner

logger = logging.getLogger(__name__)

MAX_INDEX_RETRIES = 5
DELIVERY_ATTEMPTS = 3
DELIVERY_TIMEOUT_SECONDS = 10


async def _request_start(client: WorklogIndexClient, worklog_id: int, request_id: str, execution_id: str) -> bool:
    """시작 응답이 유실돼도 같은 실행 ID로 일시적 통신 장애만 제한 재시도한다."""
    for attempt in range(DELIVERY_ATTEMPTS):
        try:
            return await asyncio.wait_for(
                client.report_start(worklog_id, request_id, execution_id), timeout=DELIVERY_TIMEOUT_SECONDS)
        except (httpx.TransportError, httpx.HTTPStatusError, TimeoutError) as exc:
            retryable = not isinstance(exc, httpx.HTTPStatusError) or exc.response.status_code == 429 or exc.response.status_code >= 500
            if not retryable or attempt == DELIVERY_ATTEMPTS - 1:
                raise
            logger.warning("Light start retry: worklogId=%s requestId=%s attempt=%s", worklog_id, request_id, attempt + 1)
            await asyncio.sleep(0.5 * 2**attempt)
    raise AssertionError("Unreachable start retry state")


async def _persist_and_report(worklog_id: int, request_id: str, execution_id: str, status: str, error: str | None = None) -> PendingWorklogIndexResult:
    """AI 볼륨에 결과를 보관하고 DB 반영은 Spring 콜백 트랜잭션에만 맡긴다."""
    try:
        record = await asyncio.wait_for(
            persist_light_index_result(worklog_id, request_id, execution_id, status, error),
            timeout=DELIVERY_TIMEOUT_SECONDS,
        )
    except Exception:
        logger.exception("AI result retention failed: worklogId=%s requestId=%s executionId=%s", worklog_id, request_id, execution_id)
        raise
    async with WorklogIndexClient() as client:
        await report_stored_result(record, client)
    return record


def _is_non_retryable_error(exc: BaseException) -> bool:
    """설정 또는 인증 관련 치명적 오류인지 판별한다."""
    if isinstance(exc, LightRagConfigurationError):
        return True
    if "AI_INTERNAL_TOKEN" in str(exc):
        return True
    return False


@celery_app.task(name="worklog.light_index", bind=True, max_retries=MAX_INDEX_RETRIES)
def execute_worklog_light_index(
    self,
    worklog_id: int,
    request_id: str,
    operation: str,
) -> dict[str, Any]:
    """업무일지 색인·재색인·삭제 작업을 수행한다."""
    execution_id = str(uuid.uuid4())
    return worker_runner.run(
        _execute_worklog_light_index_async(
            task=self,
            worklog_id=worklog_id,
            request_id=request_id,
            execution_id=execution_id,
            operation=operation,
        )
    )


async def _execute_worklog_light_index_async(
    *,
    task: Any,
    worklog_id: int,
    request_id: str,
    execution_id: str,
    operation: str,
) -> dict[str, Any]:
    """비동기 실행기 내부에서 시작 확인, 색인 실행, 결과 통보를 수행한다."""
    retained = await find_pending_light_index_result(worklog_id, request_id)
    if retained is not None:
        async with WorklogIndexClient() as client:
            await report_stored_result(retained, client)
        return retained.task_result()
    # 1. 시작 API 승인 확인
    async with WorklogIndexClient() as client:
        should_execute = await _request_start(client, worklog_id, request_id, execution_id)

    if not should_execute:
        logger.info(
            "Light index execution skipped: worklogId=%s requestId=%s executionId=%s",
            worklog_id,
            request_id,
            execution_id,
        )
        return {
            "worklog_id": worklog_id,
            "request_id": request_id,
            "execution_id": execution_id,
            "status": "SKIPPED",
        }

    # 2. 색인 실행
    index_service = LightWorklogIndexService()
    try:
        async with asyncio.timeout(settings.worklog_light_index_timeout_seconds):
            await _perform_index_operation(index_service, worklog_id, request_id, execution_id, operation)

    except Exception as exc:
        current_retries = getattr(task.request, "retries", 0)
        if not _is_non_retryable_error(exc) and current_retries < MAX_INDEX_RETRIES:
            countdown = min(60, 2**current_retries)
            logger.warning("Light index retry scheduled: worklogId=%s retry=%s/%s countdown=%ss",
                           worklog_id, current_retries + 1, MAX_INDEX_RETRIES, countdown)
            try:
                raise task.retry(exc=exc, countdown=countdown, max_retries=MAX_INDEX_RETRIES)
            except Reject:
                logger.exception("Light retry publication failed; Outbox recovery required: worklogId=%s requestId=%s", worklog_id, request_id)
                raise
        await _persist_and_report(worklog_id, request_id, execution_id, "FAILED", (str(exc) or type(exc).__name__)[:1000])
        raise

    record = await _persist_and_report(worklog_id, request_id, execution_id, "COMPLETED")
    return record.task_result()


async def _perform_index_operation(index_service: LightWorklogIndexService, worklog_id: int, request_id: str, execution_id: str, operation: str) -> None:
    """기존 색인·삭제 및 원본 삭제 수렴을 실행 기한 안에서 수행한다."""
    with trace_worklog_operation(
        f"light-index-{operation.lower()}",
        input={
            "worklog_id": worklog_id,
            "request_id": request_id,
            "execution_id": execution_id,
            "operation": operation,
        },
    ):
        if operation == "DELETE":
            del_resp = await index_service.delete_worklogs([worklog_id])
            if not del_resp.items or not del_resp.items[0].deleted:
                err = del_resp.items[0].error if del_resp.items else "DELETE_FAILED"
                raise RuntimeError(f"LightRAG deletion failed: {err}")
        elif operation in {"INDEX", "REINDEX"}:
            index = index_service.index_worklogs if operation == "INDEX" else index_service.reindex_worklogs
            response = await index([worklog_id])
            item = response.items[0] if response.items else None
            if item is None:
                raise RuntimeError(f"LightRAG {operation.lower()} returned no result")
            if not item.indexed:
                if item.error == ERROR_WORKLOG_NOT_FOUND:
                    cleanup_resp = await index_service.delete_worklogs([worklog_id])
                    if not cleanup_resp.items or not cleanup_resp.items[0].deleted:
                        err = cleanup_resp.items[0].error if cleanup_resp.items else "CLEANUP_FAILED"
                        raise RuntimeError(f"LightRAG convergence cleanup failed: {err}")
                else:
                    raise RuntimeError(f"LightRAG {operation.lower()} failed: {item.error}")
        else:
            raise ValueError(f"Unsupported operation: {operation}")

@celery_app.task(name="worklog.light_index_result", bind=True, max_retries=MAX_INDEX_RETRIES)
def send_worklog_light_index_result(
    self,
    worklog_id: int,
    request_id: str,
    execution_id: str,
    status: str,
    error: str | None = None,
) -> dict[str, Any]:
    """색인 결과 통보 전용 재시도 작업."""
    record = worker_runner.run(_persist_and_report(worklog_id, request_id, execution_id, status, error))
    return record.task_result()
