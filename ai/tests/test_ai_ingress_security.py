from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.config.settings import settings
from app.main import app


@pytest.mark.parametrize("path,body", [
    ("/ai/pipeline/worklog", {"worklogId": 1, "workContent": "내용", "authorId": 1, "teamId": 1,
                              "taskKind": "SUMMARY", "requestId": str(uuid4()), "inputHash": "a" * 64}),
    ("/ai/summarize/files", {"fileId": 2, "worklogId": 1, "storageKey": "key", "originalName": "a.pdf",
                             "fileExtension": "pdf", "requestId": str(uuid4()), "inputHash": "a" * 64}),
])
def test_ingress_rejects_missing_or_wrong_token_before_dispatch(monkeypatch, path, body):
    from app.service import pipeline_service, file_summary_service
    monkeypatch.setattr(settings, "ai_internal_token", "secret")
    def unexpected(*_args):
        pytest.fail("unauthorized task was queued")
    monkeypatch.setattr(pipeline_service.generate_worklog_summary, "delay", unexpected)
    monkeypatch.setattr(file_summary_service.generate_file_summary, "delay", unexpected)
    client = TestClient(app)
    assert client.post(path, json=body).status_code == 403
    assert client.post(path, json=body, headers={"X-AI-Internal-Token": "wrong"}).status_code == 403
    monkeypatch.setattr(settings, "ai_internal_token", "")
    assert client.post(path, json=body, headers={"X-AI-Internal-Token": "secret"}).status_code == 503


@pytest.mark.parametrize("path,body", [
    ("/ai/pipeline/worklog", {"worklogId": 1, "workContent": "내용", "authorId": 1, "teamId": 1}),
    ("/ai/summarize/files", {"fileId": 2, "worklogId": 1, "storageKey": "key", "originalName": "a.pdf", "fileExtension": "pdf"}),
])
def test_authenticated_ingress_rejects_unidentified_legacy_request(monkeypatch, path, body):
    monkeypatch.setattr(settings, "ai_internal_token", "secret")
    assert TestClient(app).post(path, json=body, headers={"X-AI-Internal-Token": "secret"}).status_code == 422


@pytest.mark.parametrize("path,body", [
    ("/ai/pipeline/worklog", {"worklogId": 1, "workContent": "내용", "authorId": 1, "teamId": 1,
                              "taskKind": "SUMMARY", "requestId": str(uuid4()), "inputHash": "a" * 64}),
    ("/ai/summarize/files", {"fileId": 2, "worklogId": 1, "storageKey": "key", "originalName": "a.pdf",
                             "fileExtension": "pdf", "requestId": str(uuid4()), "inputHash": "a" * 64}),
])
def test_authenticated_identified_request_is_accepted(monkeypatch, path, body):
    from app.service import pipeline_service, file_summary_service
    monkeypatch.setattr(settings, "ai_internal_token", "secret")
    class Queued:
        id = "task-1"
    monkeypatch.setattr(pipeline_service.generate_worklog_summary, "delay", lambda *_args: Queued())
    monkeypatch.setattr(file_summary_service.generate_file_summary, "delay", lambda *_args: Queued())
    response = TestClient(app).post(path, json=body, headers={"X-AI-Internal-Token": "secret"})
    assert response.status_code == 202
    assert response.json()["requestId"] == body["requestId"]
