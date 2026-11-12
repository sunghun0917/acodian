"""색인 실행은 한 번 수행하고 자동 재시도 판단은 API Outbox에 맡긴다."""

import asyncio
from typing import Any
import uuid

from app.client.langfuse_client import trace_worklog_operation
from app.client.worklog_index_client import WorklogIndexClient
from app.config.settings import settings
from app.light.v4.service.lightrag_adapter import LightRagConfigurationError
from app.light.v4.service.worklog_index_service import ERROR_WORKLOG_NOT_FOUND, LightWorklogIndexService
from app.service.worklog_index_result_delivery import report_stored_result
from app.store.worklog_index_result_store import PendingWorklogIndexResult, find_pending_light_index_result, persist_light_index_result
from app.task.celery_app import celery_app
from app.task.worker_runner import worker_runner


async def _persist_and_report(worklog_id: int, request_id: str, execution_id: str, status: str,
                              error: str | None = None) -> PendingWorklogIndexResult:
    """결과를 먼저 보관해 콜백 장애가 색인 재실행으로 이어지지 않게 한다."""
    record = await persist_light_index_result(worklog_id, request_id, execution_id, status, error)
    async with WorklogIndexClient() as client:
        await report_stored_result(record, client)
    return record


@celery_app.task(name="worklog.light_index", max_retries=0)
def execute_worklog_light_index(worklog_id: int, request_id: str, operation: str) -> dict[str, Any]:
    """최신 요청인지 확인한 뒤 색인·재색인·삭제 중 한 작업을 실행한다."""
    return worker_runner.run(_execute_worklog_light_index_async(
        worklog_id=worklog_id, request_id=request_id, execution_id=str(uuid.uuid4()), operation=operation,
    ))


async def _execute_worklog_light_index_async(*, worklog_id: int, request_id: str, execution_id: str,
                                              operation: str) -> dict[str, Any]:
    retained = await find_pending_light_index_result(worklog_id, request_id)
    if retained is not None:
        async with WorklogIndexClient() as client:
            await report_stored_result(retained, client)
        return retained.task_result()
    async with WorklogIndexClient() as client:
        execute = await asyncio.wait_for(client.report_start(worklog_id, request_id, execution_id), timeout=10)
        if execute is not True:
            return {"worklog_id": worklog_id, "request_id": request_id, "execution_id": execution_id, "status": "SKIPPED"}
        try:
            async with asyncio.timeout(settings.worklog_light_index_timeout_seconds):
                await _perform_index_operation(LightWorklogIndexService(), worklog_id, request_id, execution_id, operation)
        except Exception as exc:
            if not isinstance(exc, LightRagConfigurationError):
                retry = await asyncio.wait_for(client.report_attempt_failed(worklog_id, request_id, execution_id), timeout=10)
                if retry:
                    return {"worklog_id": worklog_id, "request_id": request_id, "execution_id": execution_id, "status": "RETRY_PENDING"}
            status, error = "FAILED", str(exc)
        else:
            status, error = "COMPLETED", None
    record = await _persist_and_report(worklog_id, request_id, execution_id, status, error)
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


@celery_app.task(name="worklog.light_index_result", max_retries=0)
def send_worklog_light_index_result(worklog_id: int, request_id: str, execution_id: str,
                                    status: str, error: str | None = None) -> dict[str, Any]:
    """결과 통보 재실행도 같은 실행의 보관 결과만 사용한다."""
    return worker_runner.run(_persist_and_report(worklog_id, request_id, execution_id, status, error)).task_result()
