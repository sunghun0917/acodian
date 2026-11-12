"""API의 실행 권위를 확인한 일반 AI 작업만 수행한다."""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import uuid4

import httpx

from app.client.ai_task_client import AiTaskClient
from app.service.ai_task_result_delivery import report_ai_task_result
from app.store.ai_task_result_store import find_ai_task_result, persist_ai_task_result
from app.task.retry_policy import is_retryable_ai_error


async def execute_ai_task(
    worklog_id: int, metadata: dict[str, Any], execute: Callable[[], Awaitable[dict[str, Any]]],
) -> dict[str, Any]:
    """실행 선점과 결과 보관을 거쳐 중복·늦은 결과의 반영을 막는다."""
    async with AiTaskClient() as client:
        retained = await find_ai_task_result(worklog_id, metadata["requestId"])
        if retained is not None:
            await report_ai_task_result(retained, client)
            return {"status": "RESULT_RETAINED"}

        identity = {**metadata, "executionId": str(uuid4())}
        start = await asyncio.wait_for(client.start(worklog_id, identity), timeout=10)
        if type(start.get("execute")) is not bool:
            raise ValueError("Invalid AI task start response")
        if not start["execute"]:
            return {"status": "SKIPPED"}

        try:
            result = await execute()
        except Exception as exc:
            error_code = type(exc).__name__[:100]
            if isinstance(exc, httpx.TransportError) or is_retryable_ai_error(exc):
                response = await asyncio.wait_for(client.attempt_result(
                    worklog_id, {**identity, "retryable": True, "errorCode": error_code},
                ), timeout=10)
                if response.get("accepted") is not True:
                    return {"status": "AUTHORITY_LOST"}
                if response.get("retryScheduled") is True:
                    return {"status": "RETRY_SCHEDULED"}
                if response.get("retryScheduled") is not False:
                    raise ValueError("Invalid retry transition response")
            result = {"status": "FAILED", "errorCode": error_code}

        payload = {**identity, **result, "status": result.get("status", "COMPLETED")}
        retained = await persist_ai_task_result(worklog_id, payload)
        await report_ai_task_result(retained, client)
        return {"status": payload["status"]}
