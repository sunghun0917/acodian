"""PG configuration and native workspace isolation contracts; no network calls."""

import asyncio
import os
from types import SimpleNamespace

import pytest

from app.config.settings import Settings
from app.light.v4.service import lightrag_adapter as module
from tests.test_lightrag_v4_adapter import FakeLightRAG, make_dependencies, make_settings

DSN = "postgresql://rag:local-test-password@127.0.0.1:5544/axwms_lightrag"


@pytest.fixture(autouse=True)
def isolated_pg_environment(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    for key in ("POSTGRES_WORKSPACE", "POSTGRES_HOST", "POSTGRES_PORT", "POSTGRES_USER",
                "POSTGRES_PASSWORD", "POSTGRES_DATABASE", "POSTGRES_ENABLE_VECTOR", "PGVECTOR_DSN"):
        monkeypatch.delenv(key, raising=False)


def configured(**updates):
    return make_settings().model_copy(update={"lightrag_pg_dsn": DSN,
                                            "lightrag_workspace": "axwms_pg_v1", **updates})


class StorageRag(FakeLightRAG):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.workspace = kwargs["workspace"]
        self.closed_stores = []
        for name in module._SCOPE_STORAGE_NAMES:
            async def finalize(name=name):
                self.closed_stores.append(name)
            setattr(self, name, SimpleNamespace(workspace=self.workspace,
                                                effective_workspace=self.workspace,
                                                _client=None, _initialized=False,
                                                get_by_id=self.get_status, upsert=self.upsert_status,
                                                finalize=finalize))


def test_pg_settings_keep_source_database_separate():
    settings = Settings(_env_file=None, lightrag_pg_dsn=DSN)
    assert settings.lightrag_pg_dsn == DSN
    assert settings.pgvector_dsn.endswith("/axwms")
    assert "local-test-password" not in repr(settings)


def test_pg_adapter_uses_default_storage_classes():
    dependencies, *_ = make_dependencies(StorageRag)
    rag = module.LightRagWorklogIndexAdapter(settings_obj=configured(), dependencies=dependencies)._build_lightrag()
    assert rag.kwargs["kv_storage"] == "PGKVStorage"
    assert rag.kwargs["doc_status_storage"] == "PGDocStatusStorage"
    assert rag.kwargs["vector_storage"] == "QdrantVectorDBStorage"
    assert os.environ["POSTGRES_DATABASE"] == "axwms_lightrag"
    assert os.environ["POSTGRES_PORT"] == "5544"
    assert os.environ["POSTGRES_ENABLE_VECTOR"] == "false"


@pytest.mark.parametrize("origin", ["env", "ini"])
def test_pg_override_rejected_before_initialization(origin, monkeypatch, tmp_path):
    if origin == "env":
        monkeypatch.setenv("POSTGRES_WORKSPACE", "shared")
    else:
        (tmp_path / "config.ini").write_text("[postgres]\nworkspace=shared\n", encoding="utf-8")
    dependencies, *_ = make_dependencies(StorageRag)
    with pytest.raises(module.LightRagConfigurationError, match="workspace|WORKSPACE"):
        module.LightRagWorklogIndexAdapter(settings_obj=configured(), dependencies=dependencies)._build_lightrag()


def test_pg_workspace_mismatch_after_initialization_cleans_up():
    class OverriddenRag(StorageRag):
        async def initialize_storages(self):
            self.doc_status.workspace = "other-team"

    dependencies, *_ = make_dependencies(OverriddenRag)
    adapter = module.LightRagWorklogIndexAdapter(settings_obj=configured(), dependencies=dependencies)
    with pytest.raises(module.LightRagConfigurationError, match="doc_status"):
        asyncio.run(adapter._get_initialized_rag())
    assert adapter._rag is None and not adapter._initialized
    rag = OverriddenRag.instances[-1]
    assert set(rag.closed_stores) == {name for name in module._SCOPE_STORAGE_NAMES if not name.endswith("_vdb")}
    assert rag.chunks_vdb._client is None and not rag.chunks_vdb._initialized


@pytest.mark.parametrize("scenario", ["pages", "empty", "rag_mismatch", "doc_status_mismatch"])
def test_list_document_ids_reads_all_pages_only_from_validated_workspace(scenario):
    calls = []

    class PaginatedRag(StorageRag):
        async def initialize_storages(self):
            if scenario == "rag_mismatch":
                self.workspace = "other-team"
            elif scenario == "doc_status_mismatch":
                self.doc_status.workspace = "other-team"

            async def get_docs_paginated(**kwargs):
                calls.append(kwargs)
                if scenario == "empty":
                    return [], 0
                return {
                    1: ([("file-9", {}), ("worklog-7", {"status": "processed"})], 3),
                    2: ([("worklog-8", {"status": "failed"})], 3),
                }[kwargs["page"]]

            self.doc_status.get_docs_paginated = get_docs_paginated

    dependencies, *_ = make_dependencies(PaginatedRag)
    adapter = module.LightRagWorklogIndexAdapter(settings_obj=configured(), dependencies=dependencies)
    if scenario.endswith("mismatch"):
        with pytest.raises(module.LightRagConfigurationError, match="workspace"):
            asyncio.run(adapter.list_document_ids())
        assert calls == []
    else:
        assert asyncio.run(adapter.list_document_ids()) == (
            [] if scenario == "empty" else ["file-9", "worklog-7", "worklog-8"]
        )
        assert calls == [
            {"page": page, "page_size": 200, "sort_field": "id", "sort_direction": "asc"}
            for page in ([1] if scenario == "empty" else [1, 2])
        ]


@pytest.mark.parametrize("dsn", ["", "invalid-secret", "mysql://rag:secret@localhost/rag",
                                 "postgresql://rag:secret@localhost/axwms"])
def test_pg_invalid_dsn_does_not_expose_credentials(dsn):
    with pytest.raises(module.LightRagConfigurationError) as error:
        module._configure_postgres_environment(configured(lightrag_pg_dsn=dsn))
    assert "secret" not in str(error.value)


def test_pg_password_is_url_decoded():
    module._configure_postgres_environment(configured(
        lightrag_pg_dsn="postgresql://rag:p%40ss%3Aword@127.0.0.1/rag_index"))
    assert os.environ["POSTGRES_PASSWORD"] == "p@ss:word"


def test_new_global_workspace_never_runs_native_legacy_vector_migration():
    from app.light.v4.store.worklog_team_vector_store import initialize_worklog_team_vector_storage
    rag = module.LightRagWorklogIndexAdapter(settings_obj=configured())._build_lightrag()
    assert rag.chunks_vdb.initialize.func is initialize_worklog_team_vector_storage


def test_empty_base_workspace_is_rejected_before_storage_construction():
    dependencies, *_ = make_dependencies(StorageRag)
    with pytest.raises(module.LightRagConfigurationError, match="workspace"):
        module.LightRagWorklogIndexAdapter(settings_obj=configured(lightrag_workspace=""),
                                         dependencies=dependencies)._build_lightrag()


def test_workspace_check_allows_only_llm_cache_to_be_shared():
    owner = module.LightRagWorklogIndexAdapter(settings_obj=configured(lightrag_workspace="team_3"))
    adapter = module.LightRagWorklogIndexAdapter(
        settings_obj=configured(lightrag_workspace="team_3_b"), shared_llm_cache_from=owner,
    )
    rag = StorageRag(workspace="team_3_b")
    rag.llm_response_cache.workspace = "team_3"
    adapter._verify_storage_workspaces(rag)
    for name in module._SCOPE_STORAGE_NAMES:
        if name == "llm_response_cache":
            continue
        storage = getattr(rag, name)
        storage.workspace = "team_3"
        with pytest.raises(module.LightRagConfigurationError, match=name):
            adapter._verify_storage_workspaces(rag)
        storage.workspace = "team_3_b"
    rag.llm_response_cache.workspace = "team_4"
    with pytest.raises(module.LightRagConfigurationError, match="llm_response_cache"):
        adapter._verify_storage_workspaces(rag)


def test_shared_cache_is_wrapped_and_not_finalized_by_b():
    class LifecycleRag(StorageRag):
        async def initialize_storages(self):
            self.calls.append("initialize_storages")

        async def finalize_storages(self):
            for name in module._SCOPE_STORAGE_NAMES:
                storage = getattr(self, name, None)
                if storage is not None:
                    await storage.finalize()

    dependencies, *_ = make_dependencies(LifecycleRag)
    owner = module.LightRagWorklogIndexAdapter(
        settings_obj=configured(lightrag_workspace="team_3"), dependencies=dependencies,
    )
    adapter = module.LightRagWorklogIndexAdapter(
        settings_obj=configured(lightrag_workspace="team_3_b"), dependencies=dependencies,
        shared_llm_cache_from=owner,
    )

    async def run():
        a, b = await asyncio.gather(owner._get_initialized_rag(), adapter._get_initialized_rag())
        assert b.llm_response_cache is a.llm_response_cache
        assert isinstance(b.llm_response_cache, module.ValidatedWorklogExtractionCache)
        await adapter.close()
        assert "llm_response_cache" not in a.closed_stores
        await owner.close()
        assert a.closed_stores.count("llm_response_cache") == 1

    asyncio.run(run())


def test_failed_copy_b_initialization_does_not_close_copy_a_cache():
    class OverriddenB(StorageRag):
        async def initialize_storages(self):
            if self.workspace == "team_3_b":
                self.text_chunks.workspace = "other-team"

    dependencies, *_ = make_dependencies(OverriddenB)
    owner = module.LightRagWorklogIndexAdapter(
        settings_obj=configured(lightrag_workspace="team_3"), dependencies=dependencies,
    )
    adapter = module.LightRagWorklogIndexAdapter(
        settings_obj=configured(lightrag_workspace="team_3_b"), dependencies=dependencies,
        shared_llm_cache_from=owner,
    )

    async def run():
        try:
            a = await owner._get_initialized_rag()
            with pytest.raises(module.LightRagConfigurationError, match="text_chunks"):
                await adapter._get_initialized_rag()
            assert "llm_response_cache" not in a.closed_stores
            assert owner._initialized and not adapter._initialized
        finally:
            await adapter.close()
            await owner.close()

    asyncio.run(run())
