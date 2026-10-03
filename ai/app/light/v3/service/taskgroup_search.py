"""asyncio.TaskGroup 기반 LightRAG 비동기 병렬 검색 패치.

포트폴리오:
- Qdrant 벡터 검색과 Neo4j 그래프 탐색을 asyncio.TaskGroup으로 병렬화해 검색 단계의 순차 대기시간 제거
- 두 조회 중 하나라도 실패하면 관련 작업을 함께 취소할 수 있도록 TaskGroup을 사용해 동시 작업 생명주기 관리
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

_ORIGINAL_PERFORM_KG_SEARCH = None
_PATCH_APPLIED = False


async def _compute_vector_chunks_and_embedding(
    query: str,
    chunks_vdb: Any,
    query_param: Any,
    text_chunks_db: Any,
    kg_chunk_pick_method: str,
) -> tuple[list[Any], Any]:
    """임베딩 계산과 Qdrant 벡터 검색을 단일 태스크로 묶어 비동기 병렬 실행한다."""
    from lightrag.operate import _get_vector_context

    query_embedding = None
    if query and (kg_chunk_pick_method == "VECTOR" or chunks_vdb):
        actual_embedding_func = getattr(text_chunks_db, "embedding_func", None)
        if actual_embedding_func:
            try:
                embeddings = await actual_embedding_func([query])
                query_embedding = embeddings[0]
                logger.debug("[TaskGroup] Pre-computed query embedding inside vector task")
            except Exception as e:
                logger.warning(f"[TaskGroup] Failed to pre-compute query embedding: {e}")
                query_embedding = None

    vector_chunks = []
    if chunks_vdb:
        vector_chunks = await _get_vector_context(
            query,
            chunks_vdb,
            query_param,
            query_embedding,
        )
    return vector_chunks, query_embedding


async def parallel_perform_kg_search(
    query: str,
    ll_keywords: str,
    hl_keywords: str,
    knowledge_graph_inst: Any,
    entities_vdb: Any,
    relationships_vdb: Any,
    text_chunks_db: Any,
    query_param: Any,
    chunks_vdb: Any = None,
) -> dict[str, Any]:
    """asyncio.TaskGroup을 활용하여 Qdrant 벡터 검색과 Neo4j 그래프 탐색을 병렬 실행한다."""
    from lightrag.operate import (
        DEFAULT_KG_CHUNK_PICK_METHOD,
        _get_edge_data,
        _get_node_data,
    )

    local_entities = []
    local_relations = []
    global_entities = []
    global_relations = []
    vector_chunks = []
    chunk_tracking = {}
    query_embedding = None

    kg_chunk_pick_method = text_chunks_db.global_config.get(
        "kg_chunk_pick_method", DEFAULT_KG_CHUNK_PICK_METHOD
    )

    if query_param.mode == "local" and len(ll_keywords) > 0:
        local_entities, local_relations = await _get_node_data(
            ll_keywords,
            knowledge_graph_inst,
            entities_vdb,
            query_param,
        )
        if query and kg_chunk_pick_method == "VECTOR":
            actual_embedding_func = getattr(text_chunks_db, "embedding_func", None)
            if actual_embedding_func:
                try:
                    embeddings = await actual_embedding_func([query])
                    query_embedding = embeddings[0]
                except Exception as e:
                    logger.warning(f"Failed to pre-compute query embedding: {e}")
    elif query_param.mode == "global" and len(hl_keywords) > 0:
        global_relations, global_entities = await _get_edge_data(
            hl_keywords,
            knowledge_graph_inst,
            relationships_vdb,
            query_param,
        )
        if query and kg_chunk_pick_method == "VECTOR":
            actual_embedding_func = getattr(text_chunks_db, "embedding_func", None)
            if actual_embedding_func:
                try:
                    embeddings = await actual_embedding_func([query])
                    query_embedding = embeddings[0]
                except Exception as e:
                    logger.warning(f"Failed to pre-compute query embedding: {e}")
    else:
        # hybrid or mix mode: asyncio.TaskGroup을 통한 완전 병렬화 실행
        task_local = None
        task_global = None
        task_vector = None

        logger.info("[TaskGroup] Launching concurrent Qdrant & Neo4j searches via asyncio.TaskGroup")
        async with asyncio.TaskGroup() as tg:
            if len(ll_keywords) > 0:
                task_local = tg.create_task(
                    _get_node_data(
                        ll_keywords,
                        knowledge_graph_inst,
                        entities_vdb,
                        query_param,
                    )
                )
            if len(hl_keywords) > 0:
                task_global = tg.create_task(
                    _get_edge_data(
                        hl_keywords,
                        knowledge_graph_inst,
                        relationships_vdb,
                        query_param,
                    )
                )
            if (query_param.mode == "mix" and chunks_vdb) or kg_chunk_pick_method == "VECTOR":
                task_vector = tg.create_task(
                    _compute_vector_chunks_and_embedding(
                        query,
                        chunks_vdb if query_param.mode == "mix" else None,
                        query_param,
                        text_chunks_db,
                        kg_chunk_pick_method,
                    )
                )

        if task_local is not None:
            local_entities, local_relations = task_local.result()
        if task_global is not None:
            global_relations, global_entities = task_global.result()
        if task_vector is not None:
            vector_chunks, query_embedding = task_vector.result()
            for i, chunk in enumerate(vector_chunks):
                chunk_id = chunk.get("chunk_id") or chunk.get("id")
                if chunk_id:
                    chunk_tracking[chunk_id] = {
                        "source": "C",
                        "frequency": 1,
                        "order": i + 1,
                    }
                else:
                    logger.warning(f"Vector chunk missing chunk_id: {chunk}")

    # Round-robin merge entities
    final_entities = []
    seen_entities = set()
    max_len = max(len(local_entities), len(global_entities))
    for i in range(max_len):
        if i < len(local_entities):
            entity = local_entities[i]
            entity_name = entity.get("entity_name")
            if entity_name and entity_name not in seen_entities:
                final_entities.append(entity)
                seen_entities.add(entity_name)

        if i < len(global_entities):
            entity = global_entities[i]
            entity_name = entity.get("entity_name")
            if entity_name and entity_name not in seen_entities:
                final_entities.append(entity)
                seen_entities.add(entity_name)

    # Round-robin merge relations
    final_relations = []
    seen_relations = set()
    max_len = max(len(local_relations), len(global_relations))
    for i in range(max_len):
        if i < len(local_relations):
            relation = local_relations[i]
            if "src_tgt" in relation:
                rel_key = tuple(sorted(relation["src_tgt"]))
            else:
                rel_key = tuple(
                    sorted([relation.get("src_id"), relation.get("tgt_id")])
                )

            if rel_key not in seen_relations:
                final_relations.append(relation)
                seen_relations.add(rel_key)

        if i < len(global_relations):
            relation = global_relations[i]
            if "src_tgt" in relation:
                rel_key = tuple(sorted(relation["src_tgt"]))
            else:
                rel_key = tuple(
                    sorted([relation.get("src_id"), relation.get("tgt_id")])
                )

            if rel_key not in seen_relations:
                final_relations.append(relation)
                seen_relations.add(rel_key)

    logger.info(
        f"[TaskGroup] Search complete: {len(final_entities)} entities, {len(final_relations)} relations, {len(vector_chunks)} vector chunks"
    )

    return {
        "final_entities": final_entities,
        "final_relations": final_relations,
        "vector_chunks": vector_chunks,
        "chunk_tracking": chunk_tracking,
        "query_embedding": query_embedding,
    }


def apply_taskgroup_search_patch() -> None:
    """lightrag.operate._perform_kg_search를 asyncio.TaskGroup 병렬 버전으로 교체한다."""
    global _ORIGINAL_PERFORM_KG_SEARCH, _PATCH_APPLIED
    if _PATCH_APPLIED:
        return

    try:
        import lightrag.operate
        _ORIGINAL_PERFORM_KG_SEARCH = lightrag.operate._perform_kg_search
        lightrag.operate._perform_kg_search = parallel_perform_kg_search
        _PATCH_APPLIED = True
        logger.info("[TaskGroup] Successfully patched lightrag.operate._perform_kg_search with asyncio.TaskGroup")
    except Exception as e:
        logger.warning(f"[TaskGroup] Failed to patch lightrag.operate._perform_kg_search: {e}")


def remove_taskgroup_search_patch() -> None:
    """lightrag.operate._perform_kg_search를 원래 순차 실행 함수로 복원한다."""
    global _ORIGINAL_PERFORM_KG_SEARCH, _PATCH_APPLIED
    if not _PATCH_APPLIED or _ORIGINAL_PERFORM_KG_SEARCH is None:
        return

    try:
        import lightrag.operate
        lightrag.operate._perform_kg_search = _ORIGINAL_PERFORM_KG_SEARCH
        _PATCH_APPLIED = False
        logger.info("[TaskGroup] Successfully restored original sequential _perform_kg_search")
    except Exception as e:
        logger.warning(f"[TaskGroup] Failed to restore original _perform_kg_search: {e}")
