"""사본 반영 순서, 중단 복구, 그래프 삭제 확인과 workspace 삭제 격리 계약."""

import asyncio
import os
from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.light.v4.service import lightrag_adapter as adapter_module
from app.light.v4.service.worklog_team_copy_apply import CopyBrokenError, apply_to_copy, rebuild_copy
from app.light.v4.service.worklog_team_copy_lock import LostWriteLockError
from app.light.v4.store.worklog_team_copy_store import CopyEntry, DocumentFootprint
from app.light.v4.store.worklog_source_store import LightWorklogPredecessor
from tests.test_light_worklog_team_scope import source_row


FOOTPRINT = DocumentFootprint(frozenset({"old-chunk"}), frozenset({"Tag:5"}),
                              frozenset({("Tag:5", "Worklog:7")}))


class MemoryStore:
    def __init__(self, events):
        self.events, self.data, self.broken = events, {}, None

    async def entries(self, team_id, copy, worklog_ids=None):
        return {key[2]: value for key, value in self.data.items()
                if key[:2] == (team_id, copy) and (worklog_ids is None or key[2] in worklog_ids)}

    async def mark_working(self, team_id, copy, worklog_id, footprint):
        self.events.append("working")
        self.data[team_id, copy, worklog_id] = CopyEntry(
            team_id, copy, worklog_id, "WORKING", None, None, footprint,
        )

    async def put_ready(self, team_id, copy, row):
        self.events.append("ready")
        self.data[team_id, copy, row.worklog_id] = CopyEntry(
            team_id, copy, row.worklog_id, "READY", "fingerprint", row, None,
        )

    async def remove(self, team_id, copy, worklog_id):
        self.events.append("remove")
        self.data.pop((team_id, copy, worklog_id), None)

    async def set_broken_copy(self, team_id, copy):
        self.events.append(f"broken:{copy}")
        self.broken = copy

    async def clear_copy(self, team_id, copy):
        self.events.append("clear")
        self.data = {key: value for key, value in self.data.items() if key[:2] != (team_id, copy)}


class Lease:
    def __init__(self):
        self.checks, self.lost = 0, False

    async def ensure_owned(self):
        self.checks += 1
        if self.lost:
            raise LostWriteLockError("lost")


class CopyAdapter:
    def __init__(self, events):
        self.events, self.calls, self.stale = events, [], []
        self.fail_on, self.kg = None, []
        self.footprint = FOOTPRINT

    async def document_footprint(self, document_id):
        self.calls.append(f"footprint:{document_id}")
        return self.footprint

    async def delete_document(self, document_id):
        self.calls.append(f"delete:{document_id}")

    async def find_stale_references(self, footprint):
        assert footprint == FOOTPRINT
        self.calls.append("check_stale")
        return self.stale

    async def index_documents(self, documents):
        self.calls.append(f"index:{documents[0].document_id}")
        if self.fail_on == "index":
            raise RuntimeError("insert failed")

    async def index_custom_kg_documents(self, documents):
        self.calls.append(f"custom_kg:{documents[0].document_id}")
        self.kg.extend(documents)
        if self.fail_on == "custom_kg":
            raise RuntimeError("KG insert failed")

    async def drop_all(self):
        self.events.append("drop_all")
        if self.fail_on == "drop":
            raise adapter_module.LightRagDeleteFailedError("drop failed")


@pytest.fixture
def context():
    events = []
    store, adapter, lease = MemoryStore(events), CopyAdapter(events), Lease()
    pool = SimpleNamespace(adapter_for=lambda team_id, copy: adapter)
    return pool, store, adapter, lease, events


def apply(context, desired=source_row(3, 7)):
    pool, store, _, lease, _ = context
    return apply_to_copy(pool, store, lease=lease, team_id=3, copy="b", worklog_id=7, desired=desired)


def test_apply_marks_working_first_and_ready_last(context):
    asyncio.run(apply(context))
    _, store, adapter, lease, events = context
    assert events == ["working", "ready"]
    assert adapter.calls == ["footprint:worklog-7", "delete:worklog-7", "check_stale",
                             "index:worklog-7", "custom_kg:worklog-7"]
    assert lease.checks == 1
    assert store.data[3, "b", 7].snapshot == source_row(3, 7)


@pytest.mark.parametrize("stage", ["index", "custom_kg"])
def test_apply_failure_leaves_working_with_footprint(context, stage):
    _, store, adapter, _, _ = context
    adapter.fail_on = stage
    with pytest.raises(RuntimeError):
        asyncio.run(apply(context))
    entry = store.data[3, "b", 7]
    assert entry.state == "WORKING" and entry.footprint == FOOTPRINT


def test_apply_reuses_saved_footprint_after_crash(context):
    _, store, adapter, _, _ = context
    asyncio.run(store.mark_working(3, "b", 7, FOOTPRINT))
    adapter.footprint = DocumentFootprint(frozenset(), frozenset(), frozenset())
    adapter.stale = ["Tag:5"]
    with pytest.raises(CopyBrokenError):
        asyncio.run(apply(context))
    assert "footprint:worklog-7" in adapter.calls and store.broken == "b"


@pytest.mark.parametrize("stage", ["index", "custom_kg"])
@pytest.mark.parametrize("cleanup", [False, True])
def test_apply_retry_merges_current_footprint_and_blocks_stale_new_graph(context, stage, cleanup):
    _, store, adapter, _, events = context
    adapter.fail_on = stage
    with pytest.raises(RuntimeError):
        asyncio.run(apply(context))
    adapter.fail_on = None
    adapter.calls.clear()
    adapter.footprint = DocumentFootprint(
        frozenset({"new-chunk"}), frozenset({"Tag:9"}),
        frozenset({("Tag:9", "Worklog:7")}),
    )
    checked = []

    async def find_stale(footprint):
        checked.append(footprint)
        return ["Tag:9"] if "new-chunk" in footprint.chunk_ids and "Tag:9" in footprint.entity_names else []

    adapter.find_stale_references = find_stale
    with pytest.raises(CopyBrokenError):
        asyncio.run(apply(context, desired=None if cleanup else source_row(3, 7)))
    merged = DocumentFootprint(
        frozenset({"old-chunk", "new-chunk"}), frozenset({"Tag:5", "Tag:9"}),
        frozenset({("Tag:5", "Worklog:7"), ("Tag:9", "Worklog:7")}),
    )
    assert checked == [merged]
    assert store.data[3, "b", 7].footprint == merged
    assert store.data[3, "b", 7].state == "WORKING" and store.broken == "b"
    assert adapter.calls == ["footprint:worklog-7", "delete:worklog-7"]
    assert "ready" not in events and "remove" not in events


def test_apply_none_removes_row(context):
    asyncio.run(apply(context, desired=None))
    _, store, adapter, _, events = context
    assert adapter.calls[-1] == "check_stale" and not store.data
    assert events == ["working", "remove"]


def test_stale_reference_after_delete_marks_copy_broken(context):
    _, store, adapter, _, _ = context
    adapter.stale = ["Tag:5"]
    with pytest.raises(CopyBrokenError):
        asyncio.run(apply(context))
    assert store.broken == "b" and not any(call.startswith("index:") for call in adapter.calls)


def test_apply_stops_when_lease_lost(context):
    _, store, adapter, lease, events = context
    lease.lost = True
    with pytest.raises(LostWriteLockError):
        asyncio.run(apply(context))
    assert not adapter.calls and not events and not store.data


def test_apply_filters_cross_team_predecessors_but_keeps_full_snapshot(context):
    desired = replace(source_row(3, 7), direct_predecessors=[
        LightWorklogPredecessor(1, "allowed", team_id=3),
        LightWorklogPredecessor(2, "secret", team_id=4),
        LightWorklogPredecessor(3, "unknown"),
    ])
    asyncio.run(apply(context, desired))
    _, store, adapter, _, _ = context
    assert "allowed" in str(adapter.kg) and "secret" not in str(adapter.kg) and "unknown" not in str(adapter.kg)
    assert store.data[3, "b", 7].snapshot == desired


def test_rebuild_copy_marks_broken_before_drop_and_clears_after(context):
    pool, store, _, lease, events = context
    asyncio.run(rebuild_copy(pool, store, lease=lease, team_id=3, copy="b",
                             rows=[source_row(3, 7), source_row(3, 8)]))
    assert events[:3] == ["broken:b", "drop_all", "clear"]
    assert events[-1] == "broken:None" and store.broken is None
    assert sorted(key[2] for key in store.data) == [7, 8]


@pytest.mark.parametrize("stage", ["drop", "index"])
def test_failed_rebuild_keeps_broken_and_does_not_publish(context, stage):
    pool, store, adapter, lease, events = context
    adapter.fail_on = stage
    with pytest.raises((adapter_module.LightRagDeleteFailedError, RuntimeError)):
        asyncio.run(rebuild_copy(pool, store, lease=lease, team_id=3, copy="b", rows=[source_row(3, 7)]))
    assert store.broken == "b" and "ready" not in events
    if stage == "drop":
        assert "clear" not in events and not adapter.calls


def initialized_adapter(rag):
    adapter = adapter_module.LightRagWorklogIndexAdapter()

    async def initialized():
        return rag

    adapter._get_initialized_rag = initialized
    return adapter


def test_document_footprint_reads_all_three_native_records():
    from tests.test_light_worklog_custom_kg_tracking import MemoryKv
    rag = SimpleNamespace(
        doc_status=MemoryKv({"worklog-7": {"chunks_list": ["old-chunk"]}}),
        full_entities=MemoryKv({"worklog-7": {"entity_names": ["Tag:5"]}}),
        full_relations=MemoryKv({"worklog-7": {"relation_pairs": [["Tag:5", "Worklog:7"]]}}),
    )
    adapter = initialized_adapter(rag)
    assert asyncio.run(adapter.document_footprint("worklog-7")) == FOOTPRINT
    assert asyncio.run(adapter.document_footprint("missing")) == DocumentFootprint(frozenset(), frozenset(), frozenset())


@pytest.mark.parametrize("shared", [False, True, None])
@pytest.mark.parametrize("status_type", [dict, SimpleNamespace])
def test_shared_chunk_used_by_other_document_is_not_stale(shared, status_type):
    from lightrag.constants import GRAPH_FIELD_SEP
    from tests.test_light_worklog_custom_kg_tracking import MemoryGraph
    graph = MemoryGraph()
    graph.nodes["Tag:5"] = {"source_id": f"unrelated{GRAPH_FIELD_SEP}old-chunk"}
    graph.edges["Tag:5", "Worklog:7"] = {"source_id": "old-chunk"}
    calls = []

    async def paginated(**kwargs):
        calls.append(kwargs["page"])
        # Two pages ensure failed/pending documents beyond page one also protect shared chunks.
        chunks = None if shared is None else ["old-chunk"] if shared else []
        return ([("other", status_type(chunks_list=chunks))], 2)

    rag = SimpleNamespace(doc_status=SimpleNamespace(get_docs_paginated=paginated),
                          chunk_entity_relation_graph=graph)
    stale = asyncio.run(initialized_adapter(rag).find_stale_references(FOOTPRINT))
    assert (not stale) == bool(shared)
    if not shared:
        assert "Tag:5" in stale and len(stale) == 2
    assert calls == [1, 2]


@pytest.mark.parametrize("result", [{"status": "error"}, {}, None])
def test_drop_all_keeps_shared_llm_cache_and_raises_on_error_status(result):
    dropped = []

    def storage(name):
        async def drop():
            dropped.append(name)
            return result if name == "text_chunks" else {"status": "success"}
        return SimpleNamespace(drop=drop)

    rag = SimpleNamespace(**{name: storage(name) for name in adapter_module._SCOPE_STORAGE_NAMES})
    with pytest.raises(adapter_module.LightRagDeleteFailedError):
        asyncio.run(initialized_adapter(rag).drop_all())
    assert "llm_response_cache" not in dropped


def test_drop_all_unit_only_clears_own_workspace():
    data = {(scope, name): {"value"} for scope in ("team3", "team3_b", "team4")
            for name in adapter_module._SCOPE_STORAGE_NAMES}

    def storage(scope, name):
        async def drop():
            data[scope, name].clear()
            return {"status": "success"}
        return SimpleNamespace(drop=drop)

    rag = SimpleNamespace(**{name: storage("team3_b", name) for name in adapter_module._SCOPE_STORAGE_NAMES})
    rag.llm_response_cache = storage("team3", "llm_response_cache")
    asyncio.run(initialized_adapter(rag).drop_all())
    for (scope, name), values in data.items():
        assert bool(values) == (scope != "team3_b" or name == "llm_response_cache")


def test_native_drop_commands_are_scoped_and_never_target_shared_cache(tmp_path, monkeypatch):
    from tests.test_lightrag_v4_adapter import make_settings
    for key in ("POSTGRES_WORKSPACE", "QDRANT_WORKSPACE", "NEO4J_WORKSPACE"):
        monkeypatch.delenv(key, raising=False)
    # Restore the environment bridge after constructing real, disconnected native stores.
    for key in ("POSTGRES_HOST", "POSTGRES_PORT", "POSTGRES_USER", "POSTGRES_PASSWORD",
                "POSTGRES_DATABASE", "POSTGRES_ENABLE_VECTOR", "QDRANT_URL", "QDRANT_API_KEY",
                "NEO4J_URI", "NEO4J_USERNAME", "NEO4J_PASSWORD", "NEO4J_DATABASE"):
        monkeypatch.setenv(key, "")
    settings = make_settings().model_copy(update={
        "lightrag_workspace": "team3_b", "lightrag_working_dir": str(tmp_path),
    })
    rag = adapter_module.LightRagWorklogIndexAdapter(settings_obj=settings)._build_lightrag()
    sql_calls, vector_calls, graph_calls = [], [], []

    async def execute(sql, params):
        sql_calls.append((sql, params))

    for name in adapter_module._PG_STORAGE_NAMES:
        getattr(rag, name).db = SimpleNamespace(execute=execute)

    def delete(**kwargs):
        vector_calls.append(kwargs)

    for name in ("chunks_vdb", "entities_vdb", "relationships_vdb"):
        getattr(rag, name)._client = SimpleNamespace(delete=delete)

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def run(self, query):
            graph_calls.append(query)

            async def consume():
                pass

            return SimpleNamespace(consume=consume)

    rag.chunk_entity_relation_graph._driver = SimpleNamespace(session=lambda **kwargs: Session())
    rag.chunk_entity_relation_graph._DATABASE = "neo4j"
    rag.llm_response_cache._storage.workspace = "team3"
    rag.doc_status._worklog_pending_chunks = {"worklog-7": ["chunk-test"]}
    asyncio.run(initialized_adapter(rag).drop_all())
    assert len(sql_calls) == len(adapter_module._PG_STORAGE_NAMES) - 1
    assert all("WHERE workspace=$1" in sql and params == {"workspace": "team3_b"}
               for sql, params in sql_calls)
    assert all("LIGHTRAG_LLM_CACHE" not in sql for sql, _ in sql_calls)
    assert len(vector_calls) == 3
    for call in vector_calls:
        condition, = call["points_selector"].filter.must
        assert condition.key == "workspace_id" and condition.match.value == "team3_b"
        assert call["wait"] is True
    assert graph_calls == ["MATCH (n:`team3_b`) DETACH DELETE n"]
    assert not rag.doc_status._worklog_pending_chunks


def test_rebuild_stops_before_drop_when_lease_lost(context):
    pool, store, adapter, lease, events = context
    lease.lost = True
    with pytest.raises(LostWriteLockError):
        asyncio.run(rebuild_copy(pool, store, lease=lease, team_id=3, copy="b", rows=[]))
    assert not events and not adapter.calls


def test_hidden_rebuild_failure_is_detected(monkeypatch, context):
    from tests.test_light_worklog_custom_kg_tracking import native_rag, index_body_and_kg
    rag = native_rag(monkeypatch)
    original_upsert = rag.chunk_entity_relation_graph.upsert_node

    async def fail_tag(key, node_data):
        if key == "Tag:5":
            raise RuntimeError("injected Tag rebuild failure")
        await original_upsert(key, node_data)

    async def paginated(**kwargs):
        return list(rag.doc_status.data.items()), len(rag.doc_status.data)

    rag.doc_status.get_docs_paginated = paginated
    pool, store, _, lease, _ = context
    pool.adapter_for = lambda team_id, copy: initialized_adapter(rag)

    async def run():
        await index_body_and_kg(rag, 1)
        await index_body_and_kg(rag, 2)
        rag.chunk_entity_relation_graph.upsert_node = fail_tag
        with pytest.raises(CopyBrokenError):
            await apply_to_copy(pool, store, lease=lease, team_id=10, copy="b", worklog_id=2, desired=None)
        assert store.broken == "b"

    asyncio.run(run())


native_required = pytest.mark.skipif(
    not all(os.environ.get(key) for key in ("RAG_SCOPE_TEST_ID", "RAG_SCOPE_TEST_PG_DSN",
                                           "RAG_SCOPE_TEST_QDRANT_URL", "RAG_SCOPE_TEST_NEO4J_URI")),
    reason="requires explicit isolated PostgreSQL/Qdrant/Neo4j test services",
)


@native_required
def test_copy_b_insert_hits_shared_llm_cache(tmp_path, monkeypatch):
    from tests.test_light_worklog_team_storage_integration import integration_pool
    from app.light.v4.service.worklog_document_builder import build_worklog_light_document
    from app.light.v4.service.worklog_source_reader import to_light_worklog_source
    pool, _ = integration_pool(tmp_path, monkeypatch)
    a, b = pool.adapter_for(3, "a"), pool.adapter_for(3, "b")
    calls = []
    complete = a._dependencies.gemini_model_complete

    async def counted(*args, **kwargs):
        calls.append(1)
        return await complete(*args, **kwargs)

    # A frozen dependency bundle is replaced on both instances before either initializes.
    dependencies = replace(a._dependencies, gemini_model_complete=counted)
    a._dependencies = b._dependencies = dependencies
    doc = build_worklog_light_document(to_light_worklog_source(source_row(3, 7)))

    async def run():
        try:
            await a.index_documents([doc])
            assert calls
            calls.clear()
            await b.index_documents([doc])
            assert calls == []
            ar, br = await a._get_initialized_rag(), await b._get_initialized_rag()
            assert ar.llm_response_cache is br.llm_response_cache
        finally:
            await pool.close()

    asyncio.run(run())


@native_required
def test_drop_all_only_clears_own_workspace(tmp_path, monkeypatch):
    from tests.test_light_worklog_team_storage_integration import integration_pool
    pool, _ = integration_pool(tmp_path, monkeypatch)
    adapters = [pool.adapter_for(3, "a"), pool.adapter_for(3, "b"), pool.adapter_for(4, "a")]

    async def run():
        try:
            rags = [await adapter._get_initialized_rag() for adapter in adapters]
            for rag in rags:
                await rag.full_docs.upsert({"worklog-7": {"content": "keep", "file_path": "worklog://7"}})
                await rag.text_chunks.upsert({"chunk-test": {"tokens": 1, "chunk_order_index": 0,
                    "full_doc_id": "worklog-7", "content": "keep", "file_path": "worklog://7", "llm_cache_list": []}})
                await rag.chunk_entity_relation_graph.upsert_node("Keep", {"entity_id": "Keep", "entity_type": "CONCEPT",
                    "description": "keep", "source_id": "chunk-test", "file_path": "worklog://7"})
                await rag.chunks_vdb.upsert({"chunk-test": {"content": "keep", "full_doc_id": "worklog-7"}})
            key = "copy-test:cache"
            await rags[0].llm_response_cache.upsert({key: {"return": "keep", "original_prompt": "test", "cache_type": "query"}})
            await adapters[1].drop_all()
            assert await rags[1].full_docs.get_by_id("worklog-7") is None
            assert await rags[1].chunk_entity_relation_graph.get_node("Keep") is None
            for rag in (rags[0], rags[2]):
                assert await rag.full_docs.get_by_id("worklog-7")
                assert await rag.text_chunks.get_by_id("chunk-test")
                assert await rag.chunk_entity_relation_graph.get_node("Keep")
                assert await rag.chunks_vdb.query("keep", top_k=1)
            assert (await rags[0].llm_response_cache.get_by_id(key))["return"] == "keep"
        finally:
            await pool.close()

    asyncio.run(run())
