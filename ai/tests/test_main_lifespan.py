from fastapi.testclient import TestClient

import app.main as main


def test_app_lifespan_closes_lightrag_adapter(monkeypatch) -> None:
    close_calls: list[str] = []

    async def fake_close_lightrag_worklog_index_adapter() -> None:
        close_calls.append("closed")

    monkeypatch.setattr(
        main,
        "close_lightrag_worklog_index_adapter",
        fake_close_lightrag_worklog_index_adapter,
    )

    with TestClient(main.create_app()) as client:
        response = client.get("/ai/health")

    assert response.status_code == 200
    assert close_calls == ["closed"]


def test_app_lifespan_cancels_result_delivery_loop(monkeypatch):
    import asyncio
    events = []

    async def delivery():
        events.append("started")
        try:
            await asyncio.Event().wait()
        finally:
            events.append("cancelled")

    monkeypatch.setattr(main, "run_result_delivery_loop", delivery)
    with TestClient(main.create_app()) as client:
        assert client.get("/ai/health").status_code == 200
    assert events == ["started", "cancelled"]


def test_app_lifespan_initializes_tracing_and_flushes_after_storage_error(monkeypatch):
    import pytest

    events = []
    monkeypatch.setattr(main, "initialize_langfuse", lambda: events.append("initialize"), raising=False)
    monkeypatch.setattr(main, "flush_langfuse", lambda: events.append("flush"), raising=False)

    async def fail_close():
        raise RuntimeError("storage close failed")

    monkeypatch.setattr(main, "close_lightrag_worklog_index_adapter", fail_close)
    with pytest.raises(RuntimeError, match="storage close failed"):
        with TestClient(main.create_app()) as client:
            assert client.get("/ai/health").status_code == 200
    assert events == ["initialize", "flush"]
