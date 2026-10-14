"""확정 KG도 native 문서 삭제와 공유 노드 재구성의 대상이어야 한다."""

import asyncio
from dataclasses import MISSING, fields
from types import SimpleNamespace
import pytest

from app.light.v4.store.worklog_custom_kg_tracking import insert_tracked_worklog_custom_kg
from app.light.v4.service.worklog_custom_kg_builder import LightRagWorklogCustomKgDocument


class MemoryKv:
    def __init__(self, data=None):
        self.data = data or {}

    async def get_by_id(self, key):
        return self.data.get(key)

    async def get_by_ids(self, keys):
        return [self.data.get(key) for key in keys]

    async def upsert(self, data):
        self.data.update(data)

    async def delete(self, keys):
        for key in keys:
            self.data.pop(key, None)

    async def index_done_callback(self):
        pass


def document(worklog_id, description="Confirmed"):
    return LightRagWorklogCustomKgDocument(
        document_id=f"worklog-{worklog_id}", custom_kg={
            "chunks": [{"source_id": f"source-{worklog_id}", "content": f"anchor-{worklog_id}"}],
            "entities": [{"entity_name": "User:24", "entity_type": "USER",
                          "description": description, "source_id": f"source-{worklog_id}"}],
            "relationships": [],
        })


def test_custom_kg_registers_native_provenance_and_rebuild_cache():
    from lightrag.utils import compute_mdhash_id
    from lightrag.operate import _get_cached_extraction_results, _rebuild_from_extraction_result

    rag = SimpleNamespace(**{name: MemoryKv() for name in (
        "doc_status", "full_entities", "full_relations", "entity_chunks",
        "relation_chunks", "text_chunks", "llm_response_cache",
    )})
    rag.doc_status.data = {f"worklog-{i}": {"chunks_list": [f"body-{i}"], "chunks_count": 1}
                           for i in (1, 2)}
    rag.full_entities.data = {"worklog-1": {"entity_names": ["BodyEntity"]}}
    rag.entity_chunks.data = {"User:24": {"chunk_ids": ["body-1"], "count": 1}}

    async def insert(kg, full_doc_id):
        # Native JsonKV upsert of custom chunks resets llm_cache_list to [].
        assert full_doc_id in rag.full_entities.data
        for chunk in kg["chunks"]:
            key = compute_mdhash_id(chunk["content"], prefix="chunk-")
            await rag.text_chunks.upsert({key: {"content": chunk["content"], "llm_cache_list": [],
                                              "file_path": "worklog://1"}})

    rag.ainsert_custom_kg = insert

    async def run():
        for i in (1, 2):
            await insert_tracked_worklog_custom_kg(rag, document(i, "Confirmed\n<|#|> text"))
        keys = [compute_mdhash_id(f"anchor-{i}", prefix="chunk-") for i in (1, 2)]
        assert rag.doc_status.data["worklog-1"]["chunks_list"] == ["body-1", keys[0]]
        assert rag.full_entities.data["worklog-1"]["entity_names"] == ["BodyEntity", "User:24"]
        assert rag.entity_chunks.data["User:24"]["chunk_ids"] == ["body-1", *keys]
        cached = await _get_cached_extraction_results(rag.llm_response_cache, set(keys), rag.text_chunks)
        assert set(cached) == set(keys)
        entities, _ = await _rebuild_from_extraction_result(rag.text_chunks, cached[keys[1]][0][0],
                                                         keys[1], 0)
        assert set(entities) == {"User:24"}
        assert "Confirmed" in entities["User:24"][0]["description"]

    asyncio.run(run())


class MemoryGraph:
    def __init__(self):
        self.nodes = {}
        self.edges = {}

    async def upsert_node(self, key, node_data):
        self.nodes[key] = dict(node_data)

    async def get_node(self, key):
        return self.nodes.get(key)

    async def has_node(self, key):
        return key in self.nodes

    async def upsert_edge(self, a, b, edge_data):
        self.edges[tuple(sorted((a, b)))] = dict(edge_data)

    async def get_edge(self, a, b):
        return self.edges.get(tuple(sorted((a, b))))

    async def get_nodes_batch(self, keys):
        return {key: dict(self.nodes[key]) for key in keys if key in self.nodes}

    async def get_edges_batch(self, pairs):
        return {(pair["src"], pair["tgt"]): dict(edge) for pair in pairs
                if (edge := await self.get_edge(pair["src"], pair["tgt"])) is not None}

    async def get_nodes_edges_batch(self, keys):
        return {key: [pair for pair in self.edges if key in pair] for key in keys}

    async def remove_edges(self, pairs):
        for pair in pairs:
            self.edges.pop(tuple(sorted(pair)), None)

    async def remove_nodes(self, keys):
        for key in keys:
            self.nodes.pop(key, None)
        self.edges = {pair: edge for pair, edge in self.edges.items() if not set(keys).intersection(pair)}


def native_rag(monkeypatch):
    """실제 dataclass/native 삽입·삭제·재구성을 쓰며 IO와 모델만 메모리로 대체한다."""
    import lightrag.lightrag as mod
    import lightrag.operate as operate
    from lightrag import LightRAG

    pipeline = {"history_messages": []}

    async def namespace(*args, **kwargs):
        return pipeline

    async def noop():
        pass

    async def no_llm(*args, **kwargs):
        raise AssertionError("Confirmed KG reconstruction must not call LLM in this fixture")

    monkeypatch.setattr(mod, "get_namespace_data", namespace)
    monkeypatch.setattr(mod, "get_namespace_lock", lambda *args, **kwargs: asyncio.Lock())
    monkeypatch.setattr(operate, "get_storage_keyed_lock", lambda *args, **kwargs: asyncio.Lock())
    rag = LightRAG.__new__(LightRAG)
    for field in fields(LightRAG):
        if field.default is not MISSING:
            setattr(rag, field.name, field.default)
        elif field.default_factory is not MISSING:
            setattr(rag, field.name, field.default_factory())
        else:
            setattr(rag, field.name, None)
    rag.workspace = "contract"
    rag.tokenizer = SimpleNamespace(encode=lambda text: list(text))
    rag.llm_model_func = no_llm
    rag._insert_done = noop
    for name in ("chunks_vdb", "text_chunks", "entities_vdb", "relationships_vdb",
                 "full_docs", "doc_status", "full_entities", "full_relations",
                 "entity_chunks", "relation_chunks", "llm_response_cache"):
        setattr(rag, name, MemoryKv())
    rag.chunk_entity_relation_graph = MemoryGraph()
    return rag


def worklog_document(worklog_id, *, tag_id=5):
    from app.light.v4.service.worklog_custom_kg_builder import build_worklog_custom_kg_document
    from app.light.v4.service.worklog_custom_kg_source_reader import to_light_worklog_custom_kg_source
    from app.light.v4.store.worklog_source_store import LightWorklogSourceRow, LightWorklogTag

    row = LightWorklogSourceRow(
        worklog_id=worklog_id, title=f"Task {worklog_id}", request_content=None, work_content="Body",
        author_id=24, author_name="User", team_id=10, team_name="Team",
        tags=[LightWorklogTag(tag_id=tag_id, tag_name=f"Tag {tag_id}", description=None)],
        direct_predecessors=[],
    )
    return build_worklog_custom_kg_document(to_light_worklog_custom_kg_source(row))


def test_concurrent_custom_kg_retains_both_shared_references(monkeypatch):
    rag = native_rag(monkeypatch)

    class YieldingKv(MemoryKv):
        async def get_by_id(self, key):
            record = await super().get_by_id(key)
            await asyncio.sleep(0)
            return record

    rag.entity_chunks = YieldingKv()
    rag.relation_chunks = YieldingKv()

    async def run():
        await asyncio.gather(index_body_and_kg(rag, 1), index_body_and_kg(rag, 2))
        assert len(rag.entity_chunks.data["User:24"]["chunk_ids"]) == 2
        assert (await rag.adelete_by_doc_id("worklog-1")).status == "success"
        assert "User:24" in rag.chunk_entity_relation_graph.nodes
        assert ("Tag:5", "Worklog:2") in rag.chunk_entity_relation_graph.edges

    asyncio.run(run())


def test_native_delete_waits_for_another_documents_shared_tracking(monkeypatch):
    from app.config.settings import Settings
    from app.light.v4.service.lightrag_adapter import LightRagWorklogIndexAdapter

    rag = native_rag(monkeypatch)

    async def run():
        await index_body_and_kg(rag, 1)
        entered, release = asyncio.Event(), asyncio.Event()
        original_read = rag.entity_chunks.get_by_id

        async def blocked_read(key):
            record = await original_read(key)
            if key == "User:24" and not entered.is_set():
                entered.set()
                await release.wait()
            return record

        rag.entity_chunks.get_by_id = blocked_read
        adapter = LightRagWorklogIndexAdapter(settings_obj=Settings(_env_file=None))

        async def initialized():
            return rag

        adapter._get_initialized_rag = initialized
        inserting = asyncio.create_task(index_body_and_kg(rag, 2))
        await entered.wait()
        deleting = asyncio.create_task(adapter.delete_document("worklog-1"))
        for _ in range(3):
            await asyncio.sleep(0)
        try:
            assert not deleting.done()
        finally:
            release.set()
            await asyncio.gather(inserting, deleting)
        assert len(rag.entity_chunks.data["User:24"]["chunk_ids"]) == 1
        assert "User:24" in rag.chunk_entity_relation_graph.nodes
        assert ("Tag:5", "Worklog:2") in rag.chunk_entity_relation_graph.edges

    asyncio.run(run())


async def index_body_and_kg(rag, worklog_id, *, tag_id=5):
    doc_id, chunk_id = f"worklog-{worklog_id}", f"body-{worklog_id}"
    await rag.full_docs.upsert({doc_id: {"content": "Body"}})
    await rag.doc_status.upsert({doc_id: {"status": "processed", "chunks_list": [chunk_id],
                                         "file_path": f"worklog://{worklog_id}"}})
    await rag.text_chunks.upsert({chunk_id: {"content": "Body"}})
    await rag.chunks_vdb.upsert({chunk_id: {"content": "Body"}})
    await insert_tracked_worklog_custom_kg(rag, worklog_document(worklog_id, tag_id=tag_id))


def test_native_delete_removes_custom_chunks_nodes_and_edges(monkeypatch):
    rag = native_rag(monkeypatch)

    async def run():
        await index_body_and_kg(rag, 1)
        result = await rag.adelete_by_doc_id("worklog-1")
        assert result.status == "success", result.message
        for store in (rag.text_chunks, rag.chunks_vdb, rag.entities_vdb, rag.relationships_vdb,
                      rag.full_docs, rag.doc_status, rag.full_entities, rag.full_relations,
                      rag.entity_chunks, rag.relation_chunks):
            assert not store.data
        assert not rag.chunk_entity_relation_graph.nodes
        assert not rag.chunk_entity_relation_graph.edges

    asyncio.run(run())


def test_native_delete_and_reindex_preserve_shared_user_and_tag(monkeypatch):
    from lightrag.utils import compute_mdhash_id
    rag = native_rag(monkeypatch)

    async def run():
        await index_body_and_kg(rag, 1)
        await index_body_and_kg(rag, 2)
        remaining = compute_mdhash_id(worklog_document(2).custom_kg["chunks"][0]["content"], prefix="chunk-")
        result = await rag.adelete_by_doc_id("worklog-1")
        assert result.status == "success", result.message
        for key in ("User:24", "Tag:5", "Team:10"):
            assert rag.chunk_entity_relation_graph.nodes[key]["source_id"] == remaining
            assert rag.entity_chunks.data[key]["chunk_ids"] == [remaining]
            assert rag.entities_vdb.data[compute_mdhash_id(key, prefix="ent-")]["source_id"] == remaining
        assert "Worklog:1" not in rag.chunk_entity_relation_graph.nodes
        assert ("Tag:5", "Worklog:2") in rag.chunk_entity_relation_graph.edges
        await index_body_and_kg(rag, 1, tag_id=6)
        assert ("Tag:5", "Worklog:1") not in rag.chunk_entity_relation_graph.edges
        assert ("Tag:6", "Worklog:1") in rag.chunk_entity_relation_graph.edges
        assert ("Tag:5", "Worklog:2") in rag.chunk_entity_relation_graph.edges
        assert (await rag.adelete_by_doc_id("worklog-1")).status == "success"
        assert (await rag.adelete_by_doc_id("worklog-2")).status == "success"
        assert not rag.chunk_entity_relation_graph.nodes
        assert not rag.chunk_entity_relation_graph.edges

    asyncio.run(run())


def test_partial_custom_kg_write_remains_deletable(monkeypatch):
    rag = native_rag(monkeypatch)
    native_insert = rag.ainsert_custom_kg

    async def fail_after_write(*args, **kwargs):
        await native_insert(*args, **kwargs)
        raise RuntimeError("flush failed")

    rag.ainsert_custom_kg = fail_after_write

    async def run():
        with pytest.raises(RuntimeError, match="flush failed"):
            await index_body_and_kg(rag, 1)
        assert (await rag.adelete_by_doc_id("worklog-1")).status == "success"
        assert not rag.text_chunks.data
        assert not rag.chunk_entity_relation_graph.nodes
        assert not rag.chunk_entity_relation_graph.edges

    asyncio.run(run())
