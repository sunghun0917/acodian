import asyncio
import json

import httpx
import pytest

from app.client.ai_task_client import AiTaskClient
from app.config.settings import settings


def test_start_and_result_include_request_identity_and_internal_token(monkeypatch):
    monkeypatch.setattr(settings, "ai_internal_token", "test-token")
    monkeypatch.setattr(settings, "api_base_url", "http://backend-api:8080")
    identity = {"requestId": "request-id", "executionId": "execution-id",
                "inputHash": "a" * 64, "taskKind": "SUMMARY", "targetId": 101}
    requests = []

    def handler(request):
        requests.append((request.url.path, request.headers["X-AI-Internal-Token"], json.loads(request.content)))
        if request.url.path.endswith("/start"):
            return httpx.Response(200, json={"execute": True})
        return httpx.Response(200, json={})

    async def run():
        async with AiTaskClient() as client:
            await client._client.aclose()
            client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=client.base_url)
            assert await client.start(101, identity) == {"execute": True}
            await client.result(101, {**identity, "status": "COMPLETED", "aiSummary": "완료"})

    asyncio.run(run())
    assert [item[0] for item in requests] == ["/api/internal/worklogs/101/ai-tasks/start", "/api/internal/worklogs/101/ai-tasks/result"]
    assert all(item[1] == "test-token" for item in requests)
    assert requests[0][2] == identity
    assert requests[1][2]["requestId"] == identity["requestId"]
    assert "token" not in requests[1][2]


def test_internal_task_client_refuses_missing_token(monkeypatch):
    monkeypatch.setattr(settings, "ai_internal_token", "")

    async def run():
        async with AiTaskClient() as client:
            await client.start(101, {})

    with pytest.raises(RuntimeError, match="AI_INTERNAL_TOKEN"):
        asyncio.run(run())
