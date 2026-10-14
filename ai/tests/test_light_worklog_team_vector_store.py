"""설치된 LightRAG/Qdrant local 엔진으로 payload와 실제 사전 필터를 검증한다."""

import asyncio
from dataclasses import dataclass

import numpy as np
import pytest
from qdrant_client import QdrantClient, models
from lightrag.kg.qdrant_impl import QdrantVectorDBStorage

from app.config.settings import Settings
from app.light.v4.store import worklog_team_vector_store as store_module


@dataclass
class LocalEmbedding:
    """유료 모델 없이 모든 문서에 같은 벡터를 반환한다."""

    embedding_dim: int = 2
    model_name: str = "scope-contract"

    async def __call__(self, texts, **kwargs):
        return np.array([[1.0, 0.0] for _ in texts])


def vector_store(namespace: str, workspace: str):
    return QdrantVectorDBStorage(
        namespace=namespace, workspace=workspace, embedding_func=LocalEmbedding(),
        global_config={"embedding_batch_num": 2,
                       "vector_db_storage_cls_kwargs": {"cosine_better_than_threshold": 0.1}},
        meta_fields={"entity_name", "src_id", "tgt_id", "source_id", "content", "file_path"},
    )


@pytest.mark.parametrize("namespace", ["chunks", "entities", "relationships"])
def test_real_native_qdrant_payload_and_top_k_are_isolated_before_ranking(monkeypatch, namespace):
    monkeypatch.delenv("QDRANT_WORKSPACE", raising=False)
    client = QdrantClient(":memory:")
    monkeypatch.setattr(store_module, "QdrantClient", lambda **kwargs: client)
    a, b = vector_store(namespace, "team_a"), vector_store(namespace, "team_b")

    async def run():
        for storage in (a, b):
            await store_module.initialize_worklog_team_vector_storage(storage, Settings(_env_file=None))
        await b.upsert({"shared-id": {"content": "TEAM_B_SECRET", "file_path": "worklog://2"}})
        await a.upsert({"shared-id": {"content": "TEAM_A_BODY", "file_path": "worklog://1"}})
        results = await a.query("question", top_k=1, query_embedding=[1.0, 0.0])
        assert [row["content"] for row in results] == ["TEAM_A_BODY"]
        assert [row["workspace_id"] for row in results] == ["team_a"]
        assert [row["content"] for row in await b.query("question", top_k=1)] == ["TEAM_B_SECRET"]

    try:
        asyncio.run(run())
    finally:
        client.close()


def test_team_initialization_never_migrates_or_deletes_unscoped_legacy_collection(monkeypatch):
    monkeypatch.delenv("QDRANT_WORKSPACE", raising=False)
    client = QdrantClient(":memory:")
    client.create_collection("lightrag_vdb_chunks", vectors_config=models.VectorParams(size=2, distance=models.Distance.COSINE))
    client.upsert("lightrag_vdb_chunks", points=[models.PointStruct(
        id=1, vector=[1.0, 0.0], payload={"id": "old", "content": "GLOBAL_SECRET"},
    )])
    monkeypatch.setattr(store_module, "QdrantClient", lambda **kwargs: client)
    storage = vector_store("chunks", "team_a")
    try:
        asyncio.run(store_module.initialize_worklog_team_vector_storage(storage, Settings(_env_file=None)))
        assert client.count(storage.final_namespace).count == 0
        assert client.count("lightrag_vdb_chunks").count == 1
    finally:
        client.close()
