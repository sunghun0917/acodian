"""AI 결과 보관함의 콜백만 재전송하며 업무 DB는 Spring만 변경한다."""

import asyncio
import logging

from app.client.worklog_index_client import WorklogIndexClient
from app.store.worklog_index_result_store import (
    PendingWorklogIndexResult,
    acknowledge_light_index_result,
    list_pending_light_index_results,
)

logger = logging.getLogger(__name__)
RESULT_DELIVERY_INTERVAL_SECONDS = 5
RESULT_DELIVERY_TIMEOUT_SECONDS = 10


async def report_stored_result(record: PendingWorklogIndexResult, client: WorklogIndexClient) -> bool:
    """콜백 실패·취소 시 보관 기록을 남겨 다음 재전송에서 사용한다."""
    try:
        await asyncio.wait_for(client.report_result(
            worklog_id=record.worklog_id, request_id=record.request_id,
            execution_id=record.execution_id, status=record.status, error=record.error,
        ), timeout=RESULT_DELIVERY_TIMEOUT_SECONDS)
        await acknowledge_light_index_result(record)
        return True
    except Exception as exc:
        logger.warning("Index callback retained for retry: worklogId=%s requestId=%s executionId=%s errorType=%s",
                       record.worklog_id, record.request_id, record.execution_id, type(exc).__name__)
        return False


async def deliver_pending_results_once(after: str = "") -> str:
    """실패 기록 다음의 정상 결과도 처리하도록 배치 커서를 전진시킨다."""
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
