import asyncio
from unittest.mock import AsyncMock, patch
from uuid import uuid4
import httpx

from app.config.settings import settings
from app.service.ai_task_executor import execute_ai_task
from app.store.ai_task_result_store import find_ai_task_result


def _metadata():
    return {"requestId": str(uuid4()), "inputHash": "a" * 64,
            "taskKind": "SUMMARY", "targetId": 101}

def test_denied_start_never_runs_ai(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "worklog_light_index_result_dir", tmp_path)
    execute = AsyncMock()
    with patch("app.service.ai_task_executor.AiTaskClient") as clients:
        client = AsyncMock()
        client.start.return_value = {"execute": False}
        clients.return_value.__aenter__.return_value = client
        assert asyncio.run(execute_ai_task(101, _metadata(), execute))["status"] == "SKIPPED"
    execute.assert_not_awaited()

def test_success_is_persisted_before_result_callback(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "worklog_light_index_result_dir", tmp_path)
    metadata = _metadata()
    execute = AsyncMock(return_value={"aiSummary": "요약"})
    with patch("app.service.ai_task_executor.AiTaskClient") as clients:
        client = AsyncMock()
        client.start.return_value = {"execute": True, "token": str(uuid4())}

        async def receive(worklog_id, payload):
            saved = await find_ai_task_result(worklog_id, metadata["requestId"])
            assert saved is not None
            assert saved.payload == payload
            return {"accepted": True}

        client.result.side_effect = receive
        clients.return_value.__aenter__.return_value = client
        assert asyncio.run(execute_ai_task(101, metadata, execute))["status"] == "COMPLETED"
    execute.assert_awaited_once()
    client.result.assert_awaited_once()


def test_retryable_execution_failure_requests_server_retry(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "worklog_light_index_result_dir", tmp_path)
    metadata = _metadata()
    execute = AsyncMock(side_effect=TimeoutError("provider unavailable"))
    with patch("app.service.ai_task_executor.AiTaskClient") as clients:
        client = AsyncMock()
        client.start.return_value = {"execute": True, "token": str(uuid4())}
        client.attempt_result.return_value = {"accepted": True, "retryScheduled": True}
        clients.return_value.__aenter__.return_value = client
        assert asyncio.run(execute_ai_task(101, metadata, execute))["status"] == "RETRY_SCHEDULED"
    assert client.attempt_result.await_args.args[1]["retryable"] is True
    client.result.assert_not_awaited()


def test_exhausted_execution_persists_and_reports_terminal_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "worklog_light_index_result_dir", tmp_path)
    metadata = _metadata()
    execute = AsyncMock(side_effect=TimeoutError("provider unavailable"))
    with patch("app.service.ai_task_executor.AiTaskClient") as clients:
        client = AsyncMock()
        client.start.return_value = {"execute": True, "token": str(uuid4())}
        client.attempt_result.return_value = {"accepted": True, "retryScheduled": False}
        client.result.return_value = {"accepted": True}
        clients.return_value.__aenter__.return_value = client
        assert asyncio.run(execute_ai_task(101, metadata, execute))["status"] == "FAILED"
    assert client.result.await_args.args[1]["status"] == "FAILED"
    assert client.result.await_args.args[1]["errorCode"] == "TimeoutError"


def test_rejected_attempt_does_not_report_stale_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "worklog_light_index_result_dir", tmp_path)
    with patch("app.service.ai_task_executor.AiTaskClient") as clients:
        client = AsyncMock()
        client.start.return_value = {"execute": True, "token": str(uuid4())}
        client.attempt_result.return_value = {"accepted": False, "retryScheduled": False}
        clients.return_value.__aenter__.return_value = client
        assert asyncio.run(execute_ai_task(101, _metadata(), AsyncMock(side_effect=TimeoutError()))) == {"status": "AUTHORITY_LOST"}
    client.result.assert_not_awaited()
