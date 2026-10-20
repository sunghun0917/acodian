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
                "POSTGRES_PASSWORD", "POSTGRES_DATABASE", "POSTGRES_ENABLE_VECTOR"):
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
