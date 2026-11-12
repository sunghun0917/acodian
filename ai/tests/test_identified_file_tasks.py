from unittest.mock import AsyncMock
from uuid import uuid4
import asyncio
import threading

from fastapi.testclient import TestClient
from app.main import app
from app.config.settings import settings
from app.task import file_tasks


def test_identified_file_request_dispatches_identity(monkeypatch):
    from app.service import file_summary_service
    monkeypatch.setattr(settings, "ai_internal_token", "test-secret")
    calls = []
    class Task:
        id = "task-1"
    monkeypatch.setattr(file_summary_service.generate_file_summary, "delay", lambda *args: calls.append(args) or Task())
    request_id = str(uuid4())
    response = TestClient(app).post("/ai/summarize/files", headers={"X-AI-Internal-Token": "test-secret"}, json={
        "fileId": 9, "worklogId": 10, "storageKey": "key", "originalName": "x.pdf", "fileExtension": "pdf",
        "requestId": request_id, "inputHash": "a" * 64,
    })
    assert response.status_code == 202
    assert response.json()["requestId"] == request_id
    assert calls[0][-1] == {"requestId": request_id, "inputHash": "a" * 64,
                            "taskKind": "FILE_SUMMARY", "targetId": 9}


def test_partial_file_identity_is_rejected_without_queuing(monkeypatch):
    from app.service import file_summary_service
    monkeypatch.setattr(settings, "ai_internal_token", "test-secret")
    monkeypatch.setattr(file_summary_service.generate_file_summary, "delay", lambda *args: (_ for _ in ()).throw(AssertionError("queued")))
    response = TestClient(app).post("/ai/summarize/files", headers={"X-AI-Internal-Token": "test-secret"}, json={
        "fileId": 9, "worklogId": 10, "storageKey": "key", "originalName": "x.pdf", "fileExtension": "pdf",
        "requestId": str(uuid4()),
    })
    assert response.status_code == 422


def test_identified_file_runs_authorized_without_unidentified_callback(monkeypatch, tmp_path):
    source = tmp_path / "files" / "x.pdf"
    source.parent.mkdir()
    source.write_bytes(b"pdf")
    class Downloader:
        def download_to_tempfile(self, storage_key, original_name):
            return source
    metadata = {"requestId": str(uuid4()), "inputHash": "a" * 64,
                "taskKind": "FILE_SUMMARY", "targetId": 9}
    seen = []
    async def authorized(worklog_id, received, execute):
        seen.append((worklog_id, received, await execute()))
        return {"status": "COMPLETED"}
    monkeypatch.setattr(file_tasks, "execute_ai_task", authorized)
    monkeypatch.setattr(file_tasks, "S3Downloader", Downloader)
    monkeypatch.setattr(file_tasks, "get_gemini_client", lambda: object())
    monkeypatch.setattr(file_tasks, "summarize_by_source", AsyncMock(return_value="파일 요약"))
    assert not hasattr(file_tasks, "FileClient")
    assert file_tasks.generate_file_summary.run(9, 10, "key", "x.pdf", "pdf", metadata)["status"] == "COMPLETED"
    assert seen == [(10, metadata, {"aiSummary": "파일 요약"})]
    assert not source.exists()


def test_cancelled_file_download_is_cleaned_after_background_thread_finishes(monkeypatch, tmp_path):
    source = tmp_path / "files" / "x.pdf"
    source.parent.mkdir()
    source.write_bytes(b"pdf")
    started = threading.Event()
    release = threading.Event()
    class Downloader:
        def download_to_tempfile(self, storage_key, original_name):
            started.set()
            release.wait(5)
            return source
    monkeypatch.setattr(file_tasks, "S3Downloader", Downloader)

    async def run():
        task = asyncio.create_task(file_tasks._generate_identified_file_summary("key", "x.pdf", "pdf"))
        assert await asyncio.to_thread(started.wait, 3)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        finally:
            release.set()
        for _ in range(50):
            if not source.exists():
                return
            await asyncio.sleep(0.02)
        assert not source.exists()

    asyncio.run(run())
