"""모든 요청 종류가 최신 원본으로 수렴하고 실패를 항목별로 반환한다."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.light.v4.service import worklog_index_service as module
from app.light.v4.service.lightrag_adapter import (
    LightRagConfigurationError, LightRagDeleteFailedError, LightRagDeleteTimeoutError,
    LightRagInsertFailedError, LightRagInsertTimeoutError,
)
from app.light.v4.service.worklog_team_copy_apply import CopyBrokenError
from app.light.v4.service.worklog_team_copy_lock import LostWriteLockError
from tests.test_light_worklog_team_copy_writer import Scenario, row


@pytest.mark.parametrize("operation", ["index_worklogs", "reindex_worklogs", "delete_worklogs"])
def test_all_operations_sync_latest_source(operation):
    s = Scenario(); s.seed_both(row()); s.current[7] = row(title="latest")
    response = asyncio.run(getattr(module.LightWorklogIndexService(writer=s.writer), operation)([7]))
    assert response.items[0].error is None
    assert s.snapshot(3, "b").title == "latest"


@pytest.mark.parametrize("operation", ["index_worklogs", "reindex_worklogs"])
def test_missing_source_converges_to_delete_and_returns_indexed_success(operation):
    s = Scenario(); s.seed_both(row()); s.current.clear()
    response = asyncio.run(getattr(module.LightWorklogIndexService(writer=s.writer), operation)([7]))
    assert response.items[0].indexed and response.items[0].error is None
    assert (3, "b", 7) not in s.store.data


@pytest.mark.parametrize("error,expected", [
    (LightRagInsertTimeoutError("insert"), "LIGHTRAG_INSERT_TIMEOUT"),
    (LightRagInsertFailedError("insert"), "LIGHTRAG_INSERT_FAILED"),
    (LightRagDeleteTimeoutError("delete"), "LIGHTRAG_DELETE_TIMEOUT"),
    (LightRagDeleteFailedError("delete"), "LIGHTRAG_DELETE_FAILED"),
    (CopyBrokenError("broken"), "LIGHTRAG_COMPENSATION_FAILED"),
    (LostWriteLockError("lost"), "LIGHTRAG_INSERT_FAILED"),
    (TimeoutError("drain"), "LIGHTRAG_INSERT_TIMEOUT"),
    (RuntimeError("unexpected"), "LIGHTRAG_INSERT_FAILED"),
])
def test_index_failures_are_per_item_and_later_worklogs_continue(error, expected):
    writer = SimpleNamespace(sync_worklog=AsyncMock(side_effect=[error, None]))
    response = asyncio.run(module.LightWorklogIndexService(writer=writer).index_worklogs([7, 8]))
    assert [(item.worklog_id, item.indexed, item.error) for item in response.items] == [
        (7, False, expected), (8, True, None),
    ]


@pytest.mark.parametrize("error,expected", [
    (LightRagDeleteTimeoutError("delete"), "LIGHTRAG_DELETE_TIMEOUT"),
    (TimeoutError("drain"), "LIGHTRAG_DELETE_TIMEOUT"),
    (LightRagDeleteFailedError("delete"), "LIGHTRAG_DELETE_FAILED"),
    (RuntimeError("unexpected"), "LIGHTRAG_DELETE_FAILED"),
])
def test_delete_failures_are_per_item(error, expected):
    writer = SimpleNamespace(sync_worklog=AsyncMock(side_effect=[error, None]))
    response = asyncio.run(module.LightWorklogIndexService(writer=writer).delete_worklogs([7, 8]))
    assert [(item.worklog_id, item.deleted, item.error) for item in response.items] == [
        (7, False, expected), (8, True, None),
    ]


@pytest.mark.parametrize("error", [LightRagConfigurationError("config"), asyncio.CancelledError()])
def test_configuration_and_cancellation_propagate(error):
    writer = SimpleNamespace(sync_worklog=AsyncMock(side_effect=error))
    with pytest.raises(type(error)):
        asyncio.run(module.LightWorklogIndexService(writer=writer).index_worklogs([7]))


def test_index_service_has_no_global_adapter():
    with pytest.raises(TypeError):
        module.LightWorklogIndexService(adapter_factory=lambda: object())


@pytest.mark.parametrize("error", [None, LightRagConfigurationError("config"), asyncio.CancelledError()])
def test_default_writer_owns_redis_client_per_call_and_closes(monkeypatch, error):
    clients = []
    def client_factory(url):
        client = SimpleNamespace(aclose=AsyncMock())
        clients.append(client)
        return client
    monkeypatch.setattr(module, "Redis", SimpleNamespace(from_url=client_factory))
    monkeypatch.setattr(module, "TeamCopyLocks", lambda **kwargs: object())
    monkeypatch.setattr(module, "get_lightrag_team_scope_adapter", lambda: object())
    monkeypatch.setattr(module, "get_worklog_team_copy_store", lambda: object())
    monkeypatch.setattr(module, "TeamCopyWriter", lambda **kwargs: SimpleNamespace(
        sync_worklog=AsyncMock(side_effect=error),
    ))
    service = module.LightWorklogIndexService()
    for _ in range(2):
        if error is None:
            asyncio.run(service.index_worklogs([7]))
        else:
            with pytest.raises(type(error)):
                asyncio.run(service.index_worklogs([7]))
    assert len(clients) == 2 and clients[0] is not clients[1]
    for client in clients:
        client.aclose.assert_awaited_once()


def test_empty_batch_does_not_open_redis(monkeypatch):
    monkeypatch.setattr(module, "Redis", SimpleNamespace(from_url=lambda url: pytest.fail("unexpected Redis")))
    assert asyncio.run(module.LightWorklogIndexService().index_worklogs([])).items == []


def test_mutation_lock_cancelled_waiter_releases_partial_batch_and_reversed_ids_finish():
    from app.light.v4.service.worklog_mutation_lock import worklog_mutation_locks

    async def run():
        entered, release = asyncio.Event(), asyncio.Event()

        async def hold():
            async with worklog_mutation_locks(["worklog:2"]):
                entered.set()
                await release.wait()

        async def mutate(ids):
            async with worklog_mutation_locks(ids):
                await asyncio.sleep(0)

        holder = asyncio.create_task(hold())
        await entered.wait()
        waiter = asyncio.create_task(mutate(["worklog:2", "worklog:1"]))
        for _ in range(3):
            await asyncio.sleep(0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        await asyncio.wait_for(mutate(["worklog:1"]), timeout=1)
        release.set()
        await holder
        await asyncio.wait_for(asyncio.gather(
            mutate(["worklog:1", "worklog:2"]), mutate(["worklog:2", "worklog:1"]),
        ), timeout=1)

    asyncio.run(run())
