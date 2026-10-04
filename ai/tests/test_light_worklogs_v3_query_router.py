from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from app.config.settings import Settings
from app.light.v3.model.worklog_query import (
    WorklogLightQueryResponse,
    WorklogLightReferenceItem,
)
from app.light.v3.service.lightrag_adapter import (
    LightRagConfigurationError,
    LightRagProviderUnavailableError,
    LightRagQueryFailedError,
    LightRagQueryTimeoutError,
)
from app.main import app

client = TestClient(app)


class FakeQueryService:
    def __init__(self, response: WorklogLightQueryResponse | Exception | None = None) -> None:
        self.calls: list[Any] = []
        self.response = response or WorklogLightQueryResponse(
            answer="native answer",
            references=[
                WorklogLightReferenceItem(
                    referenceId="ref-1",
                    filePath="worklog://123",
                )
            ],
            internalOnly=True,
        )

    async def query_worklogs(self, request: Any) -> WorklogLightQueryResponse:
        self.calls.append(request)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response

    async def query_worklogs_stream(self, request: Any):
        self.calls.append(request)
        if isinstance(self.response, Exception):
            raise self.response
        yield {"event": "token", "data": "chunk-1"}
        yield {"event": "done", "data": "{\"references\": [], \"done\": true}"}


def install_fake_service(monkeypatch, service: FakeQueryService) -> None:
    from app.light.v3.router import worklog_query

    monkeypatch.setattr(worklog_query, "worklog_query_service", service)
    monkeypatch.setattr(
        worklog_query, "settings", Settings(_env_file=None, ai_internal_token="test-token"),
        raising=False,
    )


def post_query(payload: dict[str, Any] | None = None):
    return client.post(
        "/ai/light/worklogs-v3/query",
        json=({"query": "업무일지 요약", "allowedTeamIds": None}
              if payload is None else {"allowedTeamIds": None, **payload}),
        headers={"X-AI-Internal-Token": "test-token"},
    )


def test_light_worklogs_v3_query_rejects_missing_internal_token(monkeypatch) -> None:
    service = FakeQueryService()
    install_fake_service(monkeypatch, service)

    response = client.post(
        "/ai/light/worklogs-v3/query",
        json={"query": "ok", "allowedTeamIds": [101]},
    )

    assert response.status_code == 403
    assert service.calls == []


def test_light_worklogs_v3_query_rejects_missing_allowed_team_ids(monkeypatch) -> None:
    service = FakeQueryService()
    install_fake_service(monkeypatch, service)

    response = client.post(
        "/ai/light/worklogs-v3/query",
        json={"query": "ok"}, headers={"X-AI-Internal-Token": "test-token"},
    )

    assert response.status_code == 422
    assert service.calls == []


def test_light_worklogs_v3_query_contract(monkeypatch) -> None:
    service = FakeQueryService()
    install_fake_service(monkeypatch, service)

    response = post_query(
        payload={"query": "최근 재고 예측 모델 관련 진행상황 요약해줘"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "answer": "native answer",
        "references": [
            {
                "referenceId": "ref-1",
                "filePath": "worklog://123",
            }
        ],
        "mode": "mix",
        "internalOnly": True,
    }
    assert len(service.calls) == 1
    assert service.calls[0].query == "최근 재고 예측 모델 관련 진행상황 요약해줘"


def test_light_worklogs_v3_query_accepts_allowed_team_ids(monkeypatch) -> None:
    service = FakeQueryService()
    install_fake_service(monkeypatch, service)

    response = post_query(payload={"query": "ok", "allowedTeamIds": [106]})

    assert response.status_code == 200
    assert len(service.calls) == 1
    assert service.calls[0].allowed_team_ids == [106]


def test_light_worklogs_v3_query_accepts_null_allowed_team_ids(monkeypatch) -> None:
    service = FakeQueryService()
    install_fake_service(monkeypatch, service)

    response = post_query(payload={"query": "ok", "allowedTeamIds": None})

    assert response.status_code == 200
    assert len(service.calls) == 1
    assert service.calls[0].allowed_team_ids is None


def test_light_worklogs_v3_query_rejects_option_override(monkeypatch) -> None:
    service = FakeQueryService()
    install_fake_service(monkeypatch, service)

    response = post_query(payload={"query": "ok", "topK": 40})

    assert response.status_code == 422
    assert service.calls == []


def test_light_worklogs_v3_query_rejects_empty_query(monkeypatch) -> None:
    service = FakeQueryService()
    install_fake_service(monkeypatch, service)

    response = post_query(payload={"query": ""})

    assert response.status_code == 422
    assert service.calls == []


def test_light_worklogs_v3_query_rejects_extra_field(monkeypatch) -> None:
    service = FakeQueryService()
    install_fake_service(monkeypatch, service)

    response = post_query(payload={"query": "ok", "extra": "x"})

    assert response.status_code == 422
    assert service.calls == []


def test_light_worklogs_v3_query_rejects_stream_field(monkeypatch) -> None:
    service = FakeQueryService()
    install_fake_service(monkeypatch, service)

    response = post_query(payload={"query": "ok", "stream": True})

    assert response.status_code == 422
    assert service.calls == []


def test_light_worklogs_v3_query_maps_configuration_error(monkeypatch) -> None:
    install_fake_service(monkeypatch, FakeQueryService(LightRagConfigurationError("missing")))

    response = post_query()

    assert response.status_code == 500
    assert response.json() == {"detail": "LIGHTRAG_CONFIGURATION_ERROR"}


def test_light_worklogs_v3_query_maps_timeout(monkeypatch) -> None:
    install_fake_service(monkeypatch, FakeQueryService(LightRagQueryTimeoutError("timeout")))

    response = post_query()

    assert response.status_code == 504
    assert response.json() == {"detail": "LIGHTRAG_QUERY_TIMEOUT"}


def test_light_worklogs_v3_query_maps_failed_error(monkeypatch) -> None:
    install_fake_service(monkeypatch, FakeQueryService(LightRagQueryFailedError("boom")))

    response = post_query()

    assert response.status_code == 500
    assert response.json() == {"detail": "LIGHTRAG_QUERY_FAILED"}


def test_light_worklogs_v3_query_maps_provider_unavailable(monkeypatch) -> None:
    install_fake_service(
        monkeypatch,
        FakeQueryService(LightRagProviderUnavailableError("provider unavailable")),
    )

    response = post_query()

    assert response.status_code == 503
    assert response.json() == {"detail": "LIGHTRAG_PROVIDER_UNAVAILABLE"}


def test_light_worklogs_v3_query_stream_success(monkeypatch) -> None:
    service = FakeQueryService()
    install_fake_service(monkeypatch, service)

    response = client.post(
        "/ai/light/worklogs-v3/query/stream",
        json={"query": "요약해줘", "allowedTeamIds": None},
        headers={"X-AI-Internal-Token": "test-token"},
    )

    assert response.status_code == 200
    assert "event: token" in response.text
    assert "chunk-1" in response.text
    assert "event: done" in response.text


def test_light_worklogs_v3_query_stream_timeout(monkeypatch) -> None:
    install_fake_service(monkeypatch, FakeQueryService(LightRagQueryTimeoutError("timeout")))

    response = client.post(
        "/ai/light/worklogs-v3/query/stream",
        json={"query": "요약해줘", "allowedTeamIds": None},
        headers={"X-AI-Internal-Token": "test-token"},
    )

    assert response.status_code == 200
    assert "event: error" in response.text
    assert "LIGHTRAG_QUERY_TIMEOUT" in response.text
