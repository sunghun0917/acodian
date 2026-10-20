"""Pinned native PG behavior must not hide failed deletion or normal absence."""

import asyncio
from types import SimpleNamespace

import pytest

from app.light.v4.service.lightrag_adapter import LightRagWorklogIndexAdapter, LightRagDeleteFailedError
from tests.test_lightrag_v4_adapter import make_settings


def native_rag(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("POSTGRES_WORKSPACE", raising=False)
    return LightRagWorklogIndexAdapter(settings_obj=make_settings())._build_lightrag()


def test_native_pg_all_missing_batch_is_a_normal_empty_result(monkeypatch, tmp_path):
    rag = native_rag(monkeypatch, tmp_path)
    class DB:
        async def query(self, *args, **kwargs):
            return []
    rag.doc_status.db = DB()
    adapter = LightRagWorklogIndexAdapter(settings_obj=make_settings())
    async def initialized():
        return rag
    adapter._get_initialized_rag = initialized
    assert asyncio.run(adapter.get_processed_worklog_ids([1, 2])) == set()


@pytest.mark.parametrize("name", ["full_docs", "text_chunks", "full_entities", "full_relations",
                                  "entity_chunks", "relation_chunks", "llm_response_cache", "doc_status"])
def test_native_pg_delete_sql_errors_are_not_swallowed(monkeypatch, tmp_path, name):
    rag = native_rag(monkeypatch, tmp_path)
    class DB:
        async def execute(self, *args, **kwargs):
            raise RuntimeError("injected DELETE failure")
    storage = getattr(rag, name)
    storage.db = DB()
    # Cache validation is a proxy; the DB belongs to the underlying PG storage.
    if name == "llm_response_cache":
        storage._storage.db = DB()
    with pytest.raises(RuntimeError, match="injected DELETE failure"):
        asyncio.run(storage.delete(["worklog-1"]))


def test_native_pg_delete_verifies_records_are_absent(monkeypatch, tmp_path):
    rag = native_rag(monkeypatch, tmp_path)
    class DB:
        async def execute(self, *args, **kwargs):
            pass
        async def query(self, *args, **kwargs):
            return [{"id": "worklog-1", "content": "still present"}]
    rag.full_docs.db = DB()
    with pytest.raises(LightRagDeleteFailedError):
        asyncio.run(rag.full_docs.delete(["worklog-1"]))


def test_failed_team_pg_delete_does_not_remove_global_ownership(monkeypatch, tmp_path):
    from app.light.v4.service import worklog_index_service as service_module
    rag = native_rag(monkeypatch, tmp_path)
    class DB:
        async def execute(self, *args, **kwargs):
            raise RuntimeError("injected DELETE failure")
    rag.full_docs.db = DB()
    async def native_delete(doc_id):
        await rag.full_docs.delete([doc_id])
        return {"status": "success"}
    rag.adelete_by_doc_id = native_delete
    adapter = LightRagWorklogIndexAdapter(settings_obj=make_settings())
    async def initialized():
        return rag
    adapter._get_initialized_rag = initialized
    removed = []
    class Global:
        async def get_document_team_id(self, doc_id):
            return 10
        async def delete_document(self, doc_id):
            removed.append(doc_id)
    monkeypatch.setattr(service_module, "get_lightrag_team_scope_adapter",
                        lambda: SimpleNamespace(adapter_for=lambda team: adapter))
    service = service_module.LightWorklogIndexService(cache=SimpleNamespace())
    with pytest.raises(LightRagDeleteFailedError):
        asyncio.run(service._delete_document(Global(), "worklog-1"))
    assert removed == []


@pytest.mark.parametrize("name,table", [
    ("full_docs", "LIGHTRAG_DOC_FULL"), ("text_chunks", "LIGHTRAG_DOC_CHUNKS"),
    ("full_entities", "LIGHTRAG_FULL_ENTITIES"), ("full_relations", "LIGHTRAG_FULL_RELATIONS"),
    ("entity_chunks", "LIGHTRAG_ENTITY_CHUNKS"), ("relation_chunks", "LIGHTRAG_RELATION_CHUNKS"),
    ("llm_response_cache", "LIGHTRAG_LLM_CACHE"), ("doc_status", "LIGHTRAG_DOC_STATUS"),
])
def test_pg_delete_succeeds_with_workspace_parameters_and_empty_postcondition(monkeypatch, tmp_path, name, table):
    rag = native_rag(monkeypatch, tmp_path)
    calls = []
    class DB:
        async def execute(self, sql, data):
            calls.append((sql, data))
        async def query(self, *args, **kwargs):
            return []
    storage = getattr(rag, name)
    (storage._storage if name == "llm_response_cache" else storage).db = DB()
    async def run():
        await storage.delete([])
        assert calls == []
        await storage.delete(["worklog-1"])
    asyncio.run(run())
    assert calls == [(f"DELETE FROM {table} WHERE workspace=$1 AND id = ANY($2)",
                      {"workspace": "axwms_pg_v1", "ids": ["worklog-1"]})]
