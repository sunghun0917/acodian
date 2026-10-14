"""LightRAG 1.4.10 팀 vector storage 초기화: 구 전역 데이터 자동 이관을 금지한다."""

from typing import Any

from qdrant_client import QdrantClient, models

from app.config.settings import Settings


async def initialize_worklog_team_vector_storage(storage: Any, settings_obj: Settings) -> None:
    """workspace별 point는 유지하되 collection 생성 시 legacy migration은 실행하지 않는다."""
    if storage._initialized:
        return
    client = QdrantClient(
        url=settings_obj.lightrag_qdrant_url.strip(),
        api_key=settings_obj.lightrag_qdrant_api_key.strip() or None,
    )
    try:
        dimension = storage.embedding_func.embedding_dim
        if client.collection_exists(storage.final_namespace):
            vectors = client.get_collection(storage.final_namespace).config.params.vectors
            if not isinstance(vectors, models.VectorParams) or vectors.size != dimension:
                raise ValueError("Team vector collection dimension mismatch")
        else:
            client.create_collection(
                storage.final_namespace,
                vectors_config=models.VectorParams(size=dimension, distance=models.Distance.COSINE),
                hnsw_config=models.HnswConfigDiff(payload_m=16, m=0),
            )
        client.create_payload_index(
            collection_name=storage.final_namespace, field_name="workspace_id",
            field_schema=models.KeywordIndexParams(type=models.KeywordIndexType.KEYWORD, is_tenant=True),
        )
    except BaseException:
        client.close()
        raise
    storage._client = client
    storage._initialized = True


async def finalize_worklog_team_vector_storage(storage: Any) -> None:
    """기본 LightRAG Qdrant finalize가 닫지 않는 client를 종료한다."""
    if storage._client is not None:
        storage._client.close()
    storage._client = None
    storage._initialized = False
