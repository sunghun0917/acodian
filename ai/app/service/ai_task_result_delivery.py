"""일반 AI 작업의 저장된 최종 결과를 API에 전달한다."""

import asyncio
import logging
from pathlib import Path

import httpx

from app.client.ai_task_client import AiTaskClient
from app.config.settings import settings
from app.store.ai_task_result_store import PendingAiTaskResult, list_ai_task_results

logger = logging.getLogger(__name__)


async def report_ai_task_result(record: PendingAiTaskResult, client: AiTaskClient) -> bool:
    """성공·stale ACK 뒤 파일을 지우며 전송 실패는 다음 스캔에서 다시 보낸다."""
    folder = Path(settings.worklog_light_index_result_dir) / "general-v2"
    try:
        response = await asyncio.wait_for(client.result(record.worklog_id, record.payload), timeout=10)
        if type(response.get("accepted")) is not bool:
            raise ValueError("Missing task result acknowledgement")
        await asyncio.to_thread((folder / record.filename).unlink, missing_ok=True)
        return response["accepted"]
    except Exception as exc:
        if isinstance(exc, httpx.HTTPStatusError) and 400 <= exc.response.status_code < 500 and exc.response.status_code not in (408, 429):
            await asyncio.to_thread((folder / record.filename).unlink, missing_ok=True)
            logger.warning("Task callback rejected: worklogId=%s status=%s", record.worklog_id, exc.response.status_code)
            return False
        logger.warning("Task callback retained: worklogId=%s errorType=%s", record.worklog_id, type(exc).__name__)
        return False


async def deliver_ai_task_results_once(after: str = "") -> str:
    """배치 안의 손상 또는 실패가 다른 결과 전달을 막지 않게 한다."""
    records, cursor = await list_ai_task_results(after)
    if records:
        async with AiTaskClient() as client:
            await asyncio.gather(*(report_ai_task_result(record, client) for record in records))
    return cursor


async def run_ai_task_result_delivery_loop() -> None:
    cursor = ""
    while True:
        try:
            cursor = await deliver_ai_task_results_once(cursor)
        except Exception:
            logger.exception("Task callback replay scan failed")
        await asyncio.sleep(5)
