import asyncio
import pytest
import httpx
from app.client.worklog_index_client import WorklogIndexClient
from app.config.settings import settings


def test_worklog_index_client_start_success(monkeypatch):
    monkeypatch.setattr(settings, "ai_internal_token", "test-token")
    monkeypatch.setattr(settings, "api_base_url", "http://backend-api:8080")

    recorded_requests = []

    def handler(request: httpx.Request):
        recorded_requests.append(request)
        assert request.headers["X-AI-Internal-Token"] == "test-token"
        assert request.url.path == "/api/internal/worklogs/101/light-index/start"
        import json
        body = json.loads(request.content)
        assert body == {"requestId": "req-1", "executionId": "exec-1"}
        return httpx.Response(200, json={"execute": True})

    transport = httpx.MockTransport(handler)

    async def run():
        async with WorklogIndexClient() as client:
            client._client = httpx.AsyncClient(transport=transport, base_url="http://backend-api:8080")
            return await client.report_start(101, "req-1", "exec-1")

    execute = asyncio.run(run())
    assert execute is True
    assert len(recorded_requests) == 1


def test_worklog_index_client_start_skip(monkeypatch):
    monkeypatch.setattr(settings, "ai_internal_token", "test-token")
    monkeypatch.setattr(settings, "api_base_url", "http://backend-api:8080")

    def handler(request: httpx.Request):
        return httpx.Response(200, json={"execute": False})

    transport = httpx.MockTransport(handler)

    async def run():
        async with WorklogIndexClient() as client:
            client._client = httpx.AsyncClient(transport=transport, base_url="http://backend-api:8080")
            return await client.report_start(101, "req-1", "exec-1")

    execute = asyncio.run(run())
    assert execute is False


def test_worklog_index_client_result_completed(monkeypatch):
    monkeypatch.setattr(settings, "ai_internal_token", "test-token")
    monkeypatch.setattr(settings, "api_base_url", "http://backend-api:8080")

    recorded_requests = []

    def handler(request: httpx.Request):
        recorded_requests.append(request)
        assert request.headers["X-AI-Internal-Token"] == "test-token"
        assert request.url.path == "/api/internal/worklogs/101/light-index/result"
        import json
        body = json.loads(request.content)
        assert body == {"requestId": "req-1", "executionId": "exec-1", "status": "COMPLETED", "error": None}
        return httpx.Response(200, json={})

    transport = httpx.MockTransport(handler)

    async def run():
        async with WorklogIndexClient() as client:
            client._client = httpx.AsyncClient(transport=transport, base_url="http://backend-api:8080")
            await client.report_result(101, "req-1", "exec-1", "COMPLETED")

    asyncio.run(run())
    assert len(recorded_requests) == 1


def test_worklog_index_client_result_failed(monkeypatch):
    monkeypatch.setattr(settings, "ai_internal_token", "test-token")
    monkeypatch.setattr(settings, "api_base_url", "http://backend-api:8080")

    recorded_requests = []

    def handler(request: httpx.Request):
        recorded_requests.append(request)
        import json
        body = json.loads(request.content)
        assert body == {"requestId": "req-1", "executionId": "exec-1", "status": "FAILED", "error": "SOME_ERROR"}
        return httpx.Response(200, json={})

    transport = httpx.MockTransport(handler)

    async def run():
        async with WorklogIndexClient() as client:
            client._client = httpx.AsyncClient(transport=transport, base_url="http://backend-api:8080")
            await client.report_result(101, "req-1", "exec-1", "FAILED", error="SOME_ERROR")

    asyncio.run(run())
    assert len(recorded_requests) == 1


def test_worklog_index_client_requires_internal_token(monkeypatch):
    monkeypatch.setattr(settings, "ai_internal_token", "")

    async def run():
        async with WorklogIndexClient() as client:
            await client.report_start(101, "req-1", "exec-1")

    with pytest.raises(RuntimeError, match="AI_INTERNAL_TOKEN"):
        asyncio.run(run())
