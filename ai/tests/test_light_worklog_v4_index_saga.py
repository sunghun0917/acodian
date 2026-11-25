"""사본 실패와 다음 요청의 복구를 서비스 응답까지 검증한다."""

import asyncio

import pytest

from app.light.v4.service.worklog_index_service import LightWorklogIndexService
from tests.test_light_worklog_team_copy_writer import Scenario, row


def test_failed_apply_preserves_read_copy_and_next_request_publishes_other_worklog():
    async def run():
        s = Scenario(); s.seed_both(row()); s.current[7] = row(title="v4")
        s.pool.fail_on = ("*", "worklog-7", "v4")
        service = LightWorklogIndexService(writer=s.writer)
        failed = await service.index_worklogs([7])
        assert failed.items[0].error == "LIGHTRAG_INSERT_FAILED"
        assert s.store.pointers == {} and s.snapshot(3, "a").title == "v3"
        s.current[8] = row(8)
        success = await service.reindex_worklogs([8])
        assert success.items[0].indexed
        assert s.snapshot(3, "b").title == "v3" and (3, "b", 8) in s.store.data
    asyncio.run(run())


def test_failed_compensation_leaves_working_row_and_next_job_restores_it():
    async def run():
        s = Scenario(); s.seed_both(row()); s.current[7] = row(title="v4")
        s.pool.fail_on = ("b", "worklog-7", None)
        service = LightWorklogIndexService(writer=s.writer)
        response = await service.index_worklogs([7])
        assert not response.items[0].indexed and s.store.pointers == {}
        assert s.store.data[3, "b", 7].state == "WORKING"
        s.pool.fail_on = None; s.current[8] = row(8)
        assert (await service.index_worklogs([8])).items[0].indexed
        assert s.snapshot(3, "b").title == "v3"
    asyncio.run(run())


def test_broken_copy_does_not_compensate_or_flip_then_rebuilds():
    async def run():
        s = Scenario(); s.seed_both(row()); s.current[7] = row(title="v4")
        s.pool.adapter_for(3, "b").stale = ["stale"]
        service = LightWorklogIndexService(writer=s.writer)
        response = await service.index_worklogs([7])
        assert response.items[0].error == "LIGHTRAG_COMPENSATION_FAILED"
        assert s.store.broken[3] == "b" and s.store.pointers == {}
        assert not any(e.startswith("apply:") for e in s.events)
        s.pool.adapter_for(3, "b").stale.clear(); s.current[8] = row(8)
        assert (await service.index_worklogs([8])).items[0].indexed
        assert s.snapshot(3, "b").title == "v3" and s.store.broken[3] is None
    asyncio.run(run())


def test_default_index_service_does_not_construct_response_cache(monkeypatch):
    from app.light.v4.service import worklog_index_service as module
    monkeypatch.setattr(module, "WorklogQueryCache", lambda **kwargs: pytest.fail("unexpected cache"))
    assert module.LightWorklogIndexService()._cache is None
