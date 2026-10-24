"""공통 완료 게시와 status 없는 부분 문서 복구 경계."""

import asyncio
from types import SimpleNamespace

import pytest

from app.light.v4.service.lightrag_adapter import (
    LightRagWorklogIndexAdapter, LightRagInsertFailedError, LightRagDeleteFailedError,
)
from tests.test_light_worklog_pg_ownership import adapter_with_status
from tests.test_lightrag_v4_adapter import make_settings


@pytest.mark.parametrize("state, expected", [(None, set()), ("STAGING", set()), ("COMPLETED", {1})])
def test_native_processed_requires_completed_publication(state, expected):
    adapter, status = adapter_with_status({})

    async def records(ids):
        return [{"status": "processed", "metadata": {"indexing_status": state}}]

    status.get_by_ids = records
    assert asyncio.run(adapter.get_processed_worklog_ids([1])) == expected


def test_publication_preserves_native_metadata_and_can_close_gate():
    record = {"status": "processed", "chunks_list": ["body", "kg"],
              "metadata": {"indexed_team_id": 10, "processing_start_time": 12}}
    adapter, status = adapter_with_status({"worklog-1": record})

    async def run():
        await adapter.set_document_indexing_status("worklog-1", "COMPLETED")
        assert status.records["worklog-1"]["metadata"]["indexing_status"] == "COMPLETED"
        await adapter.set_document_indexing_status("worklog-1", "STAGING")
        assert status.records["worklog-1"] == {
            **record, "metadata": {**record["metadata"], "indexing_status": "STAGING"},
        }
        await adapter.set_document_indexing_status("worklog-99", "STAGING")
        assert "worklog-99" not in status.records
        with pytest.raises(LightRagInsertFailedError):
            await adapter.set_document_indexing_status("worklog-99", "COMPLETED")

    asyncio.run(run())


@pytest.mark.parametrize("record", [{"status": "processing"}, {"status": "processed"}])
def test_publication_requires_processed_status_and_ownership(record):
    adapter, _ = adapter_with_status({"worklog-1": record})
    with pytest.raises(LightRagInsertFailedError):
        asyncio.run(adapter.set_document_indexing_status("worklog-1", "COMPLETED"))


def test_publication_flush_failure_is_propagated():
    adapter, status = adapter_with_status({"worklog-1": {
        "status": "processed", "metadata": {"indexed_team_id": 10},
    }})

    async def fail():
        raise RuntimeError("flush failed")

    status.index_done_callback = fail
    with pytest.raises(LightRagInsertFailedError):
        asyncio.run(adapter.set_document_indexing_status("worklog-1", "COMPLETED"))


def test_recovery_allows_absent_ownership_but_not_corrupt_ownership():
    adapter, status = adapter_with_status({"worklog-1": {"status": "failed"}})

    async def run():
        assert await adapter.get_document_team_id("worklog-1", allow_missing=True) is None
        status.records["worklog-1"]["metadata"] = {"indexed_team_id": "10"}
        with pytest.raises(LightRagDeleteFailedError):
            await adapter.get_document_team_id("worklog-1", allow_missing=True)

    asyncio.run(run())


class MemoryStore:
    def __init__(self, records=None):
        self.records = records or {}

    async def get_by_id(self, key):
        return self.records.get(key)

    async def upsert(self, records):
        self.records.update(records)

    async def delete(self, keys):
        for key in keys:
            self.records.pop(key, None)

    async def index_done_callback(self):
        pass


@pytest.mark.parametrize("status_exists", [False, True])
def test_failed_status_write_can_be_recovered_before_native_delete(status_exists):
    from app.light.v4.service.lightrag_adapter import _configure_worklog_status_tracking

    class Status(MemoryStore):
        unavailable = True

        async def upsert(self, records):
            if self.unavailable:
                raise RuntimeError("status unavailable")
            await super().upsert(records)

    record = {"status": "processed", "chunks_list": ["body"],
              "metadata": {"indexed_team_id": 10}}
    status = Status({"worklog-1": record} if status_exists else {})
    _configure_worklog_status_tracking(status)

    async def run():
        with pytest.raises(RuntimeError):
            await status.upsert({"worklog-1": {**record,
                                               "chunks_list": ["body", "kg"]}})
        async def native_delete(document_id):
            assert status.records[document_id]["chunks_list"] == ["body", "kg"]
            assert status.records[document_id]["metadata"]["indexing_status"] == "STAGING"
            await status.delete([document_id])
            return {"status": "success"}

        rag = SimpleNamespace(doc_status=status, adelete_by_doc_id=native_delete,
                              workspace="test", working_dir="test")
        adapter = LightRagWorklogIndexAdapter(settings_obj=make_settings())

        async def initialized():
            return rag

        adapter._get_initialized_rag = initialized
        with pytest.raises(LightRagDeleteFailedError):
            await adapter.delete_document("worklog-1")
        assert status._worklog_pending_chunks["worklog-1"] == ["body", "kg"]
        status.unavailable = False
        await adapter.delete_document("worklog-1")
        assert status.records == {}
        assert status._worklog_pending_chunks == {}

    asyncio.run(run())


def test_delete_statusless_full_document_cleans_native_not_found_orphan():
    docs = MemoryStore({"worklog-1": {"content": "partial body"}, "worklog-2": {"content": "keep"}})
    rag = SimpleNamespace(doc_status=MemoryStore(), full_docs=docs,
                          full_entities=MemoryStore(), full_relations=MemoryStore(),
                          workspace="test", working_dir="test")

    async def native_delete(doc_id):
        return {"status": "not_found"}

    async def initialized():
        return rag

    rag.adelete_by_doc_id = native_delete
    adapter = LightRagWorklogIndexAdapter(settings_obj=make_settings())
    adapter._get_initialized_rag = initialized
    asyncio.run(adapter.delete_document("worklog-1"))
    assert docs.records == {"worklog-2": {"content": "keep"}}


def test_statusless_graph_tracking_is_not_silently_marked_recovered():
    rag = SimpleNamespace(doc_status=MemoryStore(), full_docs=MemoryStore(),
                          full_entities=MemoryStore({"worklog-1": {"entity_names": ["User:1"]}}),
                          full_relations=MemoryStore(), workspace="test", working_dir="test")

    async def native_delete(doc_id):
        return {"status": "not_found"}

    async def initialized():
        return rag

    rag.adelete_by_doc_id = native_delete
    adapter = LightRagWorklogIndexAdapter(settings_obj=make_settings())
    adapter._get_initialized_rag = initialized
    with pytest.raises(LightRagDeleteFailedError):
        asyncio.run(adapter.delete_document("worklog-1"))


@pytest.mark.parametrize("first_write_fails", [False, True])
def test_native_failure_preserves_chunk_tracking_for_compensation(first_write_fails):
    from app.light.v4.service import lightrag_adapter as module

    class NativeStatus(MemoryStore):
        async def upsert(self, records):
            if first_write_fails and any(row["status"] == "processing" for row in records.values()):
                raise RuntimeError("status unavailable while chunks write succeeds")
            # PGDocStatusStorage treats omitted chunks_list as [], not a patch.
            self.records.update({key: {**value, "chunks_list": value.get("chunks_list", [])}
                                 for key, value in records.items()})

    status = NativeStatus()
    module._configure_worklog_status_tracking(status)

    async def run():
        try:
            await status.upsert({"worklog-1": {"status": "processing", "chunks_list": ["chunk-1"]}})
        except RuntimeError:
            pass
        await status.upsert({"worklog-1": {"status": "failed", "metadata": {"error": "extraction"}}})
        assert status.records["worklog-1"]["chunks_list"] == ["chunk-1"]
        assert status.records["worklog-1"]["chunks_count"] == 1

    asyncio.run(run())


def test_global_and_team_search_require_the_same_gate_and_live_owner():
    from app.light.v4.service.worklog_team_scope import LightRagTeamScopeAdapter
    from tests.test_light_worklog_team_scope import FakeTeamAdapter, source_row

    class Global:
        completed = {1}
        async def get_processed_worklog_ids(self, ids):
            return set(ids) & self.completed
        async def get_document_team_id(self, doc_id):
            return 10

    class Reader:
        rows = {1: source_row(10, 1)}
        async def fetch_source_rows(self, ids):
            return {key: row for key, row in self.rows.items() if key in ids}

    gate, reader = Global(), Reader()
    pool = LightRagTeamScopeAdapter(settings_obj=make_settings(), adapter_factory=FakeTeamAdapter,
                                     source_reader=reader, global_adapter_factory=lambda: gate)

    async def run():
        await pool.index_source_rows([source_row(10, 1)])
        for scope in (None, (10,)):
            assert set(await pool.read_source_rows([1], scope)) == {1}
            gate.completed.clear()
            assert await pool.read_source_rows([1], scope) == {}
            gate.completed.add(1)
            reader.rows.clear()
            assert await pool.read_source_rows([1], scope) == {}
            reader.rows[1] = source_row(20, 1)
            assert await pool.read_source_rows([1], scope) == {}
            reader.rows[1] = source_row(10, 1)

    asyncio.run(run())


def test_retrieval_enforces_reranker_model_contract():
    from tests.test_lightrag_v4_adapter import make_dependencies, make_query_options
    from app.light.v4.service.lightrag_adapter import LightRagConfigurationError

    settings = make_settings()
    settings.lightrag_rerank_binding_host = "http://localhost:8080/rerank"
    settings.lightrag_rerank_model = "configured/model"
    dependencies, *_ = make_dependencies()
    adapter = LightRagWorklogIndexAdapter(settings_obj=settings, dependencies=dependencies)
    with pytest.raises(LightRagConfigurationError, match="LIGHTRAG_RERANK_MODEL"):
        adapter._build_query_param(make_query_options(enable_rerank=True, rerank_model="other/model"))
