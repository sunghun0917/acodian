"""보관한 색인 결과는 성공 응답을 받을 때까지 재전송한다."""

import asyncio
import logging

import httpx

from app.client.worklog_index_client import WorklogIndexClient
from app.store.worklog_index_result_store import (
    PendingWorklogIndexResult, list_pending_light_index_results, acknowledge_light_index_result,
)

logger = logging.getLogger(__name__)
RESULT_DELIVERY_INTERVAL_SECONDS = 5
RESULT_DELIVERY_TIMEOUT_SECONDS = 10


async def report_stored_result(record: PendingWorklogIndexResult, client: WorklogIndexClient) -> bool:
    """전달 실패는 AI 재실행으로 바꾸지 않고 결과 파일을 그대로 남긴다."""
    try:
        await asyncio.wait_for(client.report_result(
            worklog_id=record.worklog_id, request_id=record.request_id,
            execution_id=record.execution_id, status=record.status, error=record.error,
        ), timeout=RESULT_DELIVERY_TIMEOUT_SECONDS)
        await acknowledge_light_index_result(record)
        return True
    except Exception as exc:
        if isinstance(exc, httpx.HTTPStatusError) and 400 <= exc.response.status_code < 500 and exc.response.status_code not in (408, 429):
            await acknowledge_light_index_result(record)
            logger.warning("Index callback rejected: worklogId=%s status=%s", record.worklog_id, exc.response.status_code)
            return False
        logger.warning("Index callback retained: worklogId=%s errorType=%s", record.worklog_id, type(exc).__name__)
        return False


async def deliver_pending_results_once(after: str = "") -> str:
    """실패·손상 파일이 뒤쪽 정상 결과 전달을 막지 않도록 커서를 이동한다."""
    records, cursor = await list_pending_light_index_results(after)
    if records:
        async with WorklogIndexClient() as client:
            await asyncio.gather(*(report_stored_result(record, client) for record in records))
    return cursor


async def run_result_delivery_loop() -> None:
    """기존 AI 서버에서 Redis와 독립적으로 미전달 콜백을 재시도한다."""
    cursor = ""
    while True:
        try:
            cursor = await deliver_pending_results_once(cursor)
        except Exception:
            logger.exception("Index callback replay scan failed")
        await asyncio.sleep(RESULT_DELIVERY_INTERVAL_SECONDS)
