"""Indexed team ownership survives source moves and native status updates."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest

from app.light.v4.service.lightrag_adapter import (
    LightRagWorklogIndexAdapter, LightRagDeleteFailedError, LightRagInsertFailedError,
    LightRagQueryFailedError,
)
from tests.test_lightrag_v4_adapter import make_settings


class MemoryStatus:
    def __init__(self, records):
        self.records = deepcopy(records)

    async def get_by_id(self, key):
        return deepcopy(self.records.get(key))

    async def upsert(self, records):
        self.records.update(deepcopy(records))

    async def index_done_callback(self):
        pass


def adapter_with_status(records):
    status = MemoryStatus(records)
    adapter = LightRagWorklogIndexAdapter(settings_obj=make_settings())
    async def initialized():
        return SimpleNamespace(doc_status=status, full_docs=MemoryStatus({}), workspace="global", working_dir="test")
    adapter._get_initialized_rag = initialized
    return adapter, status


def test_indexed_team_metadata_preserves_native_fields():
    record = {"status": "processed", "chunks_list": ["body", "custom"], "chunks_count": 2,
              "metadata": {"processing_start_time": 123}, "created_at": "original"}
    adapter, status = adapter_with_status({"worklog-1": record})
    async def run():
        await adapter.save_indexed_team_ids({1: 10})
        assert await adapter.get_document_team_id("worklog-1") == 10
        assert await adapter.get_document_team_id("worklog-999") is None
    asyncio.run(run())
    assert status.records["worklog-1"] == {**record, "metadata": {"processing_start_time": 123, "indexed_team_id": 10}}


@pytest.mark.parametrize("teams", [{True: 10}, {1: True}, {0: 10}, {1: -1}])
def test_indexed_team_metadata_rejects_invalid_ids(teams):
    adapter, _ = adapter_with_status({})
    with pytest.raises(LightRagInsertFailedError):
        asyncio.run(adapter.save_indexed_team_ids(teams))


@pytest.mark.parametrize("metadata", [{}, {"indexed_team_id": True}, {"indexed_team_id": "10"}, None])
def test_existing_status_without_valid_ownership_fails_closed(metadata):
    adapter, _ = adapter_with_status({"worklog-1": {"status": "processed", "metadata": metadata}})
    with pytest.raises(LightRagDeleteFailedError):
        asyncio.run(adapter.get_document_team_id("worklog-1"))


def test_ownership_requires_processed_document():
    adapter, _ = adapter_with_status({"worklog-1": {"status": "processing"}})
    with pytest.raises(LightRagInsertFailedError):
        asyncio.run(adapter.save_indexed_team_ids({1: 10}))


def test_processed_lookup_excludes_missing_and_incomplete():
    adapter, status = adapter_with_status({})
    async def statuses(ids):
        return [{"status": "processed"}, None, {"status": "processing"}, {"status": "failed"}]
    status.get_by_ids = statuses
    assert asyncio.run(adapter.get_processed_worklog_ids([1, 2, 3, 4])) == {1}


@pytest.mark.parametrize("records", [[None, None], ["bad"], [{"status": "unknown"}]])
def test_processed_lookup_rejects_malformed_status_batch(records):
    adapter, status = adapter_with_status({})
    async def statuses(ids):
        return records
    status.get_by_ids = statuses
    with pytest.raises(LightRagQueryFailedError):
        asyncio.run(adapter.get_processed_worklog_ids([1]))


@pytest.mark.parametrize("state", ["pending", "processing", "preprocessed", "failed"])
def test_all_native_incomplete_states_are_excluded_without_query_failure(state):
    adapter, status = adapter_with_status({})
    async def statuses(ids):
        return [{"status": state}]
    status.get_by_ids = statuses
    assert asyncio.run(adapter.get_processed_worklog_ids([1])) == set()
