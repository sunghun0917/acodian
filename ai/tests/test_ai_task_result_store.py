import asyncio
from uuid import uuid4

import pytest

from app.config.settings import settings
from app.store.ai_task_result_store import persist_ai_task_result, find_ai_task_result


def test_terminal_result_is_durable_and_first_result_wins(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "worklog_light_index_result_dir", tmp_path)
    request_id, execution_id = str(uuid4()), str(uuid4())
    identity = {"requestId": request_id, "executionId": execution_id, "taskKind": "SUMMARY",
                "targetId": 101, "inputHash": "a" * 64, "token": str(uuid4())}
    payload = {**identity, "status": "COMPLETED", "aiSummary": "생성된 요약"}

    first = asyncio.run(persist_ai_task_result(101, payload))
    second = asyncio.run(persist_ai_task_result(101, {**payload, "aiSummary": "덮어쓰기"}))

    assert first == second
    assert first.payload["aiSummary"] == "생성된 요약"
    assert asyncio.run(find_ai_task_result(101, request_id)) == first
    assert (tmp_path / "general-v2" / first.filename).exists()


def test_result_store_rejects_invalid_identifiers(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "worklog_light_index_result_dir", tmp_path)
    with pytest.raises(ValueError):
        asyncio.run(persist_ai_task_result(101, {"requestId": "../bad", "executionId": str(uuid4()), "status": "COMPLETED"}))
    assert not list(tmp_path.rglob("*.json"))
