import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.config.settings import settings

client = TestClient(app)


@pytest.fixture(autouse=True)
def internal_auth(monkeypatch):
    monkeypatch.setattr(settings, "ai_internal_token", "test-secret")
    client.headers["X-AI-Internal-Token"] = "test-secret"
    yield
    del client.headers["X-AI-Internal-Token"]


class DummyTask:
    def __init__(self, task_id: str) -> None:
        self.id = task_id


def test_unidentified_legacy_pipeline_is_rejected() -> None:
    response = client.post(
        "/ai/pipeline/worklog",
        json={
            "worklogId": 1,
            "requestContent": "재고 동기화 개선 요청",
            "workContent": "배치 병렬 처리 구조를 적용했다.",
            "authorId": 1,
            "teamId": 1,
            "departmentId": 1,
        },
    )

    assert response.status_code == 422


@pytest.mark.parametrize("task_kind", ["SUMMARY", "TAG"])
def test_pipeline_dispatches_only_the_requested_task(monkeypatch, task_kind: str) -> None:
    from app.service import pipeline_service

    dispatched: list[str] = []

    def summary(*args) -> DummyTask:
        dispatched.append("SUMMARY")
        return DummyTask("summary-task-id")

    def tags(*args) -> DummyTask:
        dispatched.append("TAG")
        return DummyTask("tagging-task-id")

    monkeypatch.setattr(pipeline_service.generate_worklog_summary, "delay", summary)
    monkeypatch.setattr(pipeline_service.generate_worklog_tags, "delay", tags)

    response = client.post(
        "/ai/pipeline/worklog",
        json={
            "worklogId": 1,
            "requestContent": "재고 동기화 개선 요청",
            "workContent": "배치 병렬 처리 구조를 적용했다.",
            "authorId": 1,
            "teamId": 1,
            "taskKind": task_kind,
            "requestId": "7fa9e1ec-0a93-49fa-8ca7-6d55050a4381",
            "inputHash": "a" * 64,
        },
    )

    assert response.status_code == 202
    assert dispatched == [task_kind]
    assert response.json()["summaryTaskId"] == ("summary-task-id" if task_kind == "SUMMARY" else None)
    assert response.json()["taggingTaskId"] == ("tagging-task-id" if task_kind == "TAG" else None)


def test_pipeline_rejects_unknown_task_kind(monkeypatch) -> None:
    from app.service import pipeline_service

    def unexpected_dispatch(*args):
        pytest.fail("유효하지 않은 작업은 큐에 등록하면 안 됩니다")

    monkeypatch.setattr(pipeline_service.generate_worklog_summary, "delay", unexpected_dispatch)
    monkeypatch.setattr(pipeline_service.generate_worklog_tags, "delay", unexpected_dispatch)

    response = client.post(
        "/ai/pipeline/worklog",
        json={"worklogId": 1, "workContent": "업무 내용", "authorId": 1, "teamId": 1, "taskKind": "UNKNOWN"},
    )

    assert response.status_code == 422


@pytest.mark.parametrize("task_kind", ["SUMMARY", "TAG"])
def test_identified_task_passes_execution_identity_to_exactly_one_worker(monkeypatch, task_kind):
    from app.service import pipeline_service

    calls = []
    def dispatch(*args):
        calls.append(args)
        return DummyTask("queued")

    monkeypatch.setattr(pipeline_service.generate_worklog_summary, "delay", dispatch)
    monkeypatch.setattr(pipeline_service.generate_worklog_tags, "delay", dispatch)
    request_id = "7fa9e1ec-0a93-49fa-8ca7-6d55050a4381"
    response = client.post("/ai/pipeline/worklog", json={
        "worklogId": 1, "workContent": "업무 내용", "authorId": 1, "teamId": 1,
        "taskKind": task_kind, "requestId": request_id, "inputHash": "a" * 64,
    })
    assert response.status_code == 202
    assert response.json()["requestId"] == request_id
    assert len(calls) == 1
    assert calls[0][-1] == {"requestId": request_id, "inputHash": "a" * 64,
                            "taskKind": task_kind, "targetId": 1}


@pytest.mark.parametrize("identity", [
    {"requestId": "7fa9e1ec-0a93-49fa-8ca7-6d55050a4381"},
    {"requestId": "bad", "inputHash": "a" * 64},
    {"requestId": "7fa9e1ec-0a93-49fa-8ca7-6d55050a4381", "inputHash": None},
    {"requestId": "7fa9e1ec-0a93-49fa-8ca7-6d55050a4381", "inputHash": "bad"},
])
def test_partial_or_invalid_identity_is_not_queued(monkeypatch, identity):
    from app.service import pipeline_service
    monkeypatch.setattr(pipeline_service.generate_worklog_summary, "delay", lambda *args: pytest.fail("queued"))
    response = client.post("/ai/pipeline/worklog", json={
        "worklogId": 1, "workContent": "업무 내용", "authorId": 1, "teamId": 1,
        "taskKind": "SUMMARY", **identity,
    })
    assert response.status_code == 422


def test_targeted_task_without_identity_is_not_queued(monkeypatch):
    from app.service import pipeline_service
    monkeypatch.setattr(pipeline_service.generate_worklog_summary, "delay", lambda *args: pytest.fail("queued"))
    response = client.post("/ai/pipeline/worklog", json={
        "worklogId": 1, "workContent": "업무 내용", "authorId": 1, "teamId": 1, "taskKind": "SUMMARY",
    })
    assert response.status_code == 422
