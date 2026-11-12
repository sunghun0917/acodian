import asyncio

import pytest
from unittest.mock import AsyncMock
from uuid import uuid4

from app.task import tagging_tasks, worklog_tasks
from tests.test_tagging_tasks import FakeTaggingClient, FakeTaggingService


def identity(kind):
    return {"requestId": str(uuid4()), "inputHash": "a" * 64,
            "taskKind": kind, "targetId": 10}


def test_identified_summary_uses_authorized_executor_without_legacy_callback(monkeypatch):
    metadata = identity("SUMMARY")
    seen = []

    async def authorized(worklog_id, received, execute):
        seen.append((worklog_id, received, await execute()))
        return {"status": "COMPLETED"}

    class Summary:
        generate_summary = AsyncMock(return_value="요약")

    monkeypatch.setattr(worklog_tasks, "execute_ai_task", authorized)
    monkeypatch.setattr(worklog_tasks, "SummaryService", Summary)
    assert not hasattr(worklog_tasks, "WorklogClient")
    assert worklog_tasks.generate_worklog_summary.run(10, "요청", "내용", metadata)["status"] == "COMPLETED"
    assert seen == [(10, metadata, {"aiSummary": "요약"})]


def test_identified_tagging_returns_results_without_legacy_callback(monkeypatch):
    metadata = identity("TAG")
    client = FakeTaggingClient()
    seen = []

    async def authorized(worklog_id, received, execute):
        seen.append((worklog_id, received, await execute()))
        return {"status": "COMPLETED"}

    monkeypatch.setattr(tagging_tasks, "execute_ai_task", authorized)
    monkeypatch.setattr(tagging_tasks, "TaggingClient", lambda: client)
    monkeypatch.setattr(tagging_tasks, "TaggingService", FakeTaggingService)
    assert tagging_tasks.generate_worklog_tags.run(10, "재고 배치 자동화 작업", metadata)["status"] == "COMPLETED"
    assert seen[0][2] == {"existingTagIds": [1], "newTags": [{"tagName": "배치자동화",
                          "description": "정기 배치 자동화 개선 업무에 사용하는 태그"}]}
    assert client.created_request is None


def test_queue_identity_must_match_task_and_target():
    metadata = {"requestId": str(uuid4()), "inputHash": "a" * 64,
                "taskKind": "TAG", "targetId": 10}
    with pytest.raises(ValueError, match="identity"):
        worklog_tasks.generate_worklog_summary.run(10, "요청", "내용", metadata)
    metadata["taskKind"] = "SUMMARY"
    metadata["targetId"] = 11
    with pytest.raises(ValueError, match="identity"):
        worklog_tasks.generate_worklog_summary.run(10, "요청", "내용", metadata)
