import uuid
import pytest
from fastapi.testclient import TestClient

from app.light.v4.model.worklog_index import (
    WorklogLightIndexItem,
    WorklogLightIndexResponse,
)
from app.main import app

client = TestClient(app)

TEST_REQUEST_ID = "00000000-0000-0000-0000-000000000001"


@pytest.fixture(autouse=True)
def internal_token(monkeypatch):
    from app.light.v4.router import worklog_index
    monkeypatch.setattr(worklog_index.settings, "ai_internal_token", "test-internal-token")


def test_light_worklogs_v4_index_accepted_contract(monkeypatch) -> None:
    from app.light.v4.router import worklog_index

    async def fake_dispatch_index(worklog_ids: list[int], request_id: uuid.UUID, operation: str) -> WorklogLightIndexResponse:
        assert worklog_ids == [101, 102]
        assert str(request_id) == TEST_REQUEST_ID
        assert operation == "INDEX"
        return WorklogLightIndexResponse(
            items=[
                WorklogLightIndexItem(worklogId=101, requestId=request_id, taskId="task-101"),
                WorklogLightIndexItem(worklogId=102, requestId=request_id, taskId="task-102"),
            ]
        )

    monkeypatch.setattr(
        worklog_index.worklog_index_dispatch_service,
        "dispatch_index",
        fake_dispatch_index,
    )

    response = client.post(
        "/ai/light/worklogs-v4/index",
        headers={"X-AI-Internal-Token": "test-internal-token"},
        json={"worklogIds": [101, 102], "requestId": TEST_REQUEST_ID},
    )

    assert response.status_code == 202
    assert response.json() == {
        "items": [
            {"worklogId": 101, "requestId": TEST_REQUEST_ID, "taskId": "task-101"},
            {"worklogId": 102, "requestId": TEST_REQUEST_ID, "taskId": "task-102"},
        ]
    }


def test_light_worklogs_v4_index_allows_single_item_list(monkeypatch) -> None:
    from app.light.v4.router import worklog_index

    async def fake_dispatch_index(worklog_ids: list[int], request_id: uuid.UUID, operation: str) -> WorklogLightIndexResponse:
        assert worklog_ids == [101]
        return WorklogLightIndexResponse(
            items=[WorklogLightIndexItem(worklogId=101, requestId=request_id, taskId="task-101")]
        )

    monkeypatch.setattr(
        worklog_index.worklog_index_dispatch_service,
        "dispatch_index",
        fake_dispatch_index,
    )

    response = client.post(
        "/ai/light/worklogs-v4/index",
        headers={"X-AI-Internal-Token": "test-internal-token"},
        json={"worklogIds": [101], "requestId": TEST_REQUEST_ID},
    )

    assert response.status_code == 202
    assert response.json() == {
        "items": [{"worklogId": 101, "requestId": TEST_REQUEST_ID, "taskId": "task-101"}]
    }


def test_light_worklogs_v4_index_rejects_missing_request_id() -> None:
    response = client.post(
        "/ai/light/worklogs-v4/index",
        headers={"X-AI-Internal-Token": "test-internal-token"},
        json={"worklogIds": [101]},
    )

    assert response.status_code == 422


def test_light_worklogs_v4_index_rejects_invalid_uuid_request_id() -> None:
    response = client.post(
        "/ai/light/worklogs-v4/index",
        headers={"X-AI-Internal-Token": "test-internal-token"},
        json={"worklogIds": [101], "requestId": "not-a-valid-uuid"},
    )

    assert response.status_code == 422


def test_light_worklogs_v4_index_rejects_singular_field_name() -> None:
    response = client.post(
        "/ai/light/worklogs-v4/index",
        headers={"X-AI-Internal-Token": "test-internal-token"},
        json={"worklogId": 101, "requestId": TEST_REQUEST_ID},
    )

    assert response.status_code == 422


def test_light_worklogs_v4_index_rejects_snake_case_field_name() -> None:
    response = client.post(
        "/ai/light/worklogs-v4/index",
        headers={"X-AI-Internal-Token": "test-internal-token"},
        json={"worklog_ids": [101], "request_id": TEST_REQUEST_ID},
    )

    assert response.status_code == 422


def test_light_worklogs_v4_index_rejects_extra_fields() -> None:
    response = client.post(
        "/ai/light/worklogs-v4/index",
        headers={"X-AI-Internal-Token": "test-internal-token"},
        json={"worklogIds": [101], "requestId": TEST_REQUEST_ID, "keyword": "검색어"},
    )

    assert response.status_code == 422


def test_light_worklogs_v4_index_rejects_non_positive_worklog_id() -> None:
    response = client.post(
        "/ai/light/worklogs-v4/index",
        headers={"X-AI-Internal-Token": "test-internal-token"},
        json={"worklogIds": [0], "requestId": TEST_REQUEST_ID},
    )

    assert response.status_code == 422


def test_light_worklogs_v4_index_rejects_batch_size_over_default_limit() -> None:
    response = client.post(
        "/ai/light/worklogs-v4/index",
        headers={"X-AI-Internal-Token": "test-internal-token"},
        json={"worklogIds": list(range(1, 12)), "requestId": TEST_REQUEST_ID},
    )

    assert response.status_code == 422


def test_light_worklogs_v4_index_rejects_batch_size_over_configured_limit(
    monkeypatch,
) -> None:
    from app.light.v4.router import worklog_index

    monkeypatch.setattr(worklog_index.settings, "lightrag_index_max_batch_size", 1)

    response = client.post(
        "/ai/light/worklogs-v4/index",
        headers={"X-AI-Internal-Token": "test-internal-token"},
        json={"worklogIds": [101, 102], "requestId": TEST_REQUEST_ID},
    )

    assert response.status_code == 422
    assert response.json() == {
        "detail": "worklogIds exceeds LIGHTRAG_INDEX_MAX_BATCH_SIZE"
    }


def test_light_worklogs_v4_index_publish_failure_returns_503(monkeypatch) -> None:
    from app.light.v4.router import worklog_index
    from fastapi import HTTPException

    async def fail_dispatch(worklog_ids: list[int], request_id: uuid.UUID, operation: str) -> WorklogLightIndexResponse:
        raise HTTPException(status_code=503, detail="CELERY_DISPATCH_FAILED")

    monkeypatch.setattr(
        worklog_index.worklog_index_dispatch_service,
        "dispatch_index",
        fail_dispatch,
    )

    response = client.post(
        "/ai/light/worklogs-v4/index",
        headers={"X-AI-Internal-Token": "test-internal-token"},
        json={"worklogIds": [101], "requestId": TEST_REQUEST_ID},
    )

    assert response.status_code == 503
    assert response.json() == {"detail": "CELERY_DISPATCH_FAILED"}


def test_light_worklogs_v4_custom_kg_index_endpoint_removed() -> None:
    response = client.post(
        "/ai/light/worklogs-v4/custom-kg/index",
        json={"worklogIds": [101], "requestId": TEST_REQUEST_ID},
    )

    assert response.status_code == 404


def test_light_worklogs_v4_delete_requires_internal_token(monkeypatch) -> None:
    from app.light.v4.router import worklog_index

    monkeypatch.setattr(worklog_index.settings, "ai_internal_token", "test-internal-token")
    response = client.post(
        "/ai/light/worklogs-v4/delete", json={"worklogIds": [101], "requestId": TEST_REQUEST_ID}
    )
    assert response.status_code == 403
    assert response.json() == {"detail": "AI_INTERNAL_ACCESS_DENIED"}


def test_light_worklogs_v4_delete_accepted_contract(monkeypatch) -> None:
    from app.light.v4.router import worklog_index

    async def fake_dispatch_delete(worklog_ids: list[int], request_id: uuid.UUID) -> WorklogLightIndexResponse:
        assert worklog_ids == [101]
        assert str(request_id) == TEST_REQUEST_ID
        return WorklogLightIndexResponse(
            items=[WorklogLightIndexItem(worklogId=101, requestId=request_id, taskId="task-del-101")]
        )

    monkeypatch.setattr(worklog_index.settings, "ai_internal_token", "test-internal-token")
    monkeypatch.setattr(
        worklog_index.worklog_index_dispatch_service,
        "dispatch_delete",
        fake_dispatch_delete,
        raising=False,
    )
    response = client.post(
        "/ai/light/worklogs-v4/delete",
        json={"worklogIds": [101], "requestId": TEST_REQUEST_ID},
        headers={"X-AI-Internal-Token": "test-internal-token"},
    )
    assert response.status_code == 202
    assert response.json() == {
        "items": [{"worklogId": 101, "requestId": TEST_REQUEST_ID, "taskId": "task-del-101"}]
    }


def test_light_worklogs_v4_delete_rejects_extra_fields(monkeypatch) -> None:
    from app.light.v4.router import worklog_index

    monkeypatch.setattr(worklog_index.settings, "ai_internal_token", "test-internal-token")
    response = client.post(
        "/ai/light/worklogs-v4/delete",
        json={"worklogIds": [101], "requestId": TEST_REQUEST_ID, "force": True},
        headers={"X-AI-Internal-Token": "test-internal-token"},
    )
    assert response.status_code == 422


def test_light_worklogs_v4_reindex_requires_internal_token(monkeypatch) -> None:
    from app.light.v4.router import worklog_index

    monkeypatch.setattr(worklog_index.settings, "ai_internal_token", "test-internal-token")
    response = client.post(
        "/ai/light/worklogs-v4/reindex", json={"worklogIds": [101], "requestId": TEST_REQUEST_ID}
    )
    assert response.status_code == 403


def test_light_worklogs_v4_reindex_accepted_contract(monkeypatch) -> None:
    from app.light.v4.router import worklog_index

    async def fake_dispatch_reindex(worklog_ids: list[int], request_id: uuid.UUID, operation: str) -> WorklogLightIndexResponse:
        assert worklog_ids == [101]
        assert str(request_id) == TEST_REQUEST_ID
        assert operation == "REINDEX"
        return WorklogLightIndexResponse(
            items=[WorklogLightIndexItem(worklogId=101, requestId=request_id, taskId="task-reindex-101")]
        )

    monkeypatch.setattr(worklog_index.settings, "ai_internal_token", "test-internal-token")
    monkeypatch.setattr(
        worklog_index.worklog_index_dispatch_service,
        "dispatch_index",
        fake_dispatch_reindex,
        raising=False,
    )
    response = client.post(
        "/ai/light/worklogs-v4/reindex",
        json={"worklogIds": [101], "requestId": TEST_REQUEST_ID},
        headers={"X-AI-Internal-Token": "test-internal-token"},
    )
    assert response.status_code == 202
    assert response.json() == {
        "items": [{"worklogId": 101, "requestId": TEST_REQUEST_ID, "taskId": "task-reindex-101"}]
    }


@pytest.mark.parametrize("token", [None, "wrong-token"])
def test_light_worklogs_v4_index_denies_missing_or_wrong_token(monkeypatch, token):
    from app.light.v4.router import worklog_index

    async def forbidden_index(*args, **kwargs):
        pytest.fail("unauthorized request reached index dispatch")

    monkeypatch.setattr(worklog_index.worklog_index_dispatch_service, "dispatch_index", forbidden_index, raising=False)
    response = client.post(
        "/ai/light/worklogs-v4/index",
        json={"worklogIds": [101], "requestId": TEST_REQUEST_ID},
        headers={"X-AI-Internal-Token": token} if token else {},
    )
    assert response.status_code == 403
    assert response.json() == {"detail": "AI_INTERNAL_ACCESS_DENIED"}


def test_light_worklogs_v4_index_fails_closed_without_configured_token(monkeypatch):
    from app.light.v4.router import worklog_index
    monkeypatch.setattr(worklog_index.settings, "ai_internal_token", "")

    async def forbidden_index(*args, **kwargs):
        pytest.fail("unconfigured request reached index dispatch")

    monkeypatch.setattr(worklog_index.worklog_index_dispatch_service, "dispatch_index", forbidden_index, raising=False)
    response = client.post(
        "/ai/light/worklogs-v4/index",
        json={"worklogIds": [101], "requestId": TEST_REQUEST_ID},
        headers={"X-AI-Internal-Token": "test-internal-token"},
    )
    assert response.status_code == 503
    assert response.json() == {"detail": "AI_INTERNAL_TOKEN_NOT_CONFIGURED"}
