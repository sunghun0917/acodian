import asyncio
from unittest.mock import AsyncMock
from uuid import uuid4
import httpx
import pytest

from app.config.settings import settings
from app.service.ai_task_result_delivery import report_ai_task_result
from app.store.ai_task_result_store import persist_ai_task_result


@pytest.mark.parametrize("status, retained", [(400, False), (422, False), (408, True), (429, True), (500, True)])
def test_http_failure_retention(monkeypatch, tmp_path, status, retained):
    monkeypatch.setattr(settings, "worklog_light_index_result_dir", tmp_path)
    payload = {"requestId": str(uuid4()), "executionId": str(uuid4()), "taskKind": "TAG", "targetId": 101, "inputHash": "a" * 64, "status": "FAILED"}
    record = asyncio.run(persist_ai_task_result(101, payload))
    client = AsyncMock()
    response = httpx.Response(status, request=httpx.Request("POST", "https://example.test"))
    client.result.side_effect = httpx.HTTPStatusError("failure", request=response.request, response=response)
    assert asyncio.run(report_ai_task_result(record, client)) is False
    assert (tmp_path / "general-v2" / record.filename).exists() is retained


def test_result_delivery_retries_and_keeps_failed_payload(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "worklog_light_index_result_dir", tmp_path)
    payload = {"requestId": str(uuid4()), "executionId": str(uuid4()), "taskKind": "SUMMARY", "targetId": 101, "inputHash": "a" * 64,
               "status": "COMPLETED", "aiSummary": "요약"}
    record = asyncio.run(persist_ai_task_result(101, payload))
    client = AsyncMock()
    client.result.side_effect = OSError("API unavailable")

    for _ in range(6):
        assert asyncio.run(report_ai_task_result(record, client)) is False

    assert client.result.await_count == 6
    assert (tmp_path / "general-v2" / record.filename).exists()


def test_stale_result_acknowledgement_does_not_resend(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "worklog_light_index_result_dir", tmp_path)
    payload = {"requestId": str(uuid4()), "executionId": str(uuid4()), "taskKind": "TAG", "targetId": 101, "inputHash": "a" * 64,
               "status": "FAILED", "errorCode": "TEMPORARY_ERROR"}
    record = asyncio.run(persist_ai_task_result(101, payload))
    client = AsyncMock()
    client.result.return_value = {"accepted": False}

    assert asyncio.run(report_ai_task_result(record, client)) is False
    assert not (tmp_path / "general-v2" / record.filename).exists()
    assert client.result.await_count == 1


def test_malformed_acknowledgement_keeps_result(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "worklog_light_index_result_dir", tmp_path)
    payload = {"requestId": str(uuid4()), "executionId": str(uuid4()), "taskKind": "SUMMARY", "targetId": 101, "inputHash": "a" * 64,
               "status": "COMPLETED", "aiSummary": "요약"}
    record = asyncio.run(persist_ai_task_result(101, payload))
    client = AsyncMock()
    client.result.return_value = {}

    assert asyncio.run(report_ai_task_result(record, client)) is False
    assert (tmp_path / "general-v2" / record.filename).exists()
