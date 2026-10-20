"""Opt-in native PostgreSQL contracts. Never use the application database."""

import asyncio
import os
from uuid import uuid4

import pytest
from sqlalchemy.engine import make_url

from app.config.settings import Settings
from app.light.v4.service.lightrag_adapter import _configure_postgres_environment


def validate_test_pg_dsn(dsn):
    """Require an explicit isolated loopback database before any connection."""
    url = make_url(dsn)
    if (url.drivername != "postgresql" or url.host != "127.0.0.1"
            or not url.port or url.port in {5432, 5434}
            or not url.database or not url.database.startswith("test_") or url.query):
        raise ValueError("requires an isolated test_ database on a non-application loopback port")
    return dsn


@pytest.mark.parametrize("dsn", ["postgresql://u:p@remote:5544/test_rag",
                                 "postgresql://u:p@127.0.0.1:5434/test_rag",
                                 "postgresql://u:p@127.0.0.1:5544/axwms_lightrag"])
def test_pg_integration_rejects_application_targets(dsn):
    with pytest.raises(ValueError):
        validate_test_pg_dsn(dsn)


@pytest.mark.skipif(not (os.environ.get("RAG_SCOPE_TEST_ID") and os.environ.get("RAG_SCOPE_TEST_PG_DSN")),
                    reason="requires explicit isolated PostgreSQL test database")
def test_pg_kv_and_status_roundtrip_across_reinitialize(monkeypatch, tmp_path):
    from lightrag.kg.postgres_impl import PGKVStorage, PGDocStatusStorage
    from lightrag.kg.shared_storage import initialize_share_data

    dsn = validate_test_pg_dsn(os.environ["RAG_SCOPE_TEST_PG_DSN"])
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("POSTGRES_WORKSPACE", raising=False)
    for key in ("POSTGRES_HOST", "POSTGRES_PORT", "POSTGRES_USER", "POSTGRES_PASSWORD",
                "POSTGRES_DATABASE", "POSTGRES_ENABLE_VECTOR"):
        monkeypatch.setenv(key, "")
    _configure_postgres_environment(Settings(_env_file=None, lightrag_pg_dsn=dsn))
    workspace = f"test_pg_{uuid4().hex}"
    initialize_share_data(workers=1)

    async def run():
        stores = []
        namespaces = ("full_docs", "text_chunks", "llm_response_cache", "entity_chunks", "doc_status")

        async def open_stores(scope):
            result = {}
            for namespace in namespaces:
                cls = PGDocStatusStorage if namespace == "doc_status" else PGKVStorage
                store = cls(namespace=namespace, workspace=scope,
                            global_config={"embedding_batch_num": 8}, embedding_func=None)
                await store.initialize()
                stores.append(store)
                result[namespace] = store
            return result

        key = "confirmed:extract:chunk-test"
        ids = {"full_docs": ["worklog-1"], "text_chunks": ["chunk-test"],
               "llm_response_cache": [key], "entity_chunks": ["User:24"], "doc_status": ["worklog-1"]}
        try:
            current = await open_stores(workspace)
            await current["full_docs"].upsert({"worklog-1": {"content": "body", "file_path": "worklog://1"}})
            await current["text_chunks"].upsert({"chunk-test": {
                "tokens": 1, "chunk_order_index": 0, "full_doc_id": "worklog-1", "content": "anchor",
                "file_path": "worklog://1", "llm_cache_list": [key],
            }})
            await current["llm_response_cache"].upsert({key: {
                "return": "confirmed-result", "original_prompt": "DB-confirmed Custom KG",
                "cache_type": "extract", "chunk_id": "chunk-test",
            }})
            await current["entity_chunks"].upsert({"User:24": {"chunk_ids": ["chunk-test"], "count": 1}})
            await current["doc_status"].upsert({"worklog-1": {
                "status": "processed", "file_path": "worklog://1", "content_summary": "body",
                "content_length": 4, "chunks_count": 1, "chunks_list": ["chunk-test"],
                "metadata": {"indexed_team_id": 10}, "created_at": "2026-10-21T00:00:00+00:00",
                "updated_at": "2026-10-21T00:00:00+00:00",
            }})
            for store in stores:
                await store.finalize()
            stores.clear()
            current = await open_stores(workspace)
            assert (await current["doc_status"].get_by_id("worklog-1"))["metadata"]["indexed_team_id"] == 10
            assert (await current["text_chunks"].get_by_id("chunk-test"))["llm_cache_list"] == [key]
            cached = await current["llm_response_cache"].get_by_id(key)
            assert (cached["return"], cached["chunk_id"], cached["cache_type"]) == ("confirmed-result", "chunk-test", "extract")
            other = await open_stores(workspace + "_other")
            assert await other["full_docs"].get_by_id("worklog-1") is None
            assert (await current["entity_chunks"].get_by_id("User:24"))["chunk_ids"] == ["chunk-test"]
        finally:
            for store in stores:
                try:
                    await store.delete(ids[store.namespace])
                finally:
                    await store.finalize()

    asyncio.run(run())
