"""LightRAG 1.4.10 Custom KG의 누락된 문서 삭제·공유 노드 복원 추적을 보완한다."""

from typing import Any

from app.light.v4.service.worklog_custom_kg_builder import LightRagWorklogCustomKgDocument
from app.light.v4.service.worklog_mutation_lock import worklog_mutation_locks, workspace_mutation_key


async def _extend_record(store: Any, key: str, field: str, values: list[Any]) -> None:
    """기존 본문 및 다른 문서의 참조를 보존하면서 native 추적 목록을 확장한다."""
    record = await store.get_by_id(key) or {}
    merged = list(record.get(field, []))
    for value in values:
        if value not in merged:
            merged.append(value)
    await store.upsert({key: {**record, field: merged, "count": len(merged)}})


async def insert_tracked_worklog_custom_kg(
    rag: Any, document: LightRagWorklogCustomKgDocument,
) -> None:
    """공유 참조 확장과 native KG 쓰기를 같은 workspace mutation 안에서 실행한다."""
    async with worklog_mutation_locks([workspace_mutation_key(rag)]):
        await _insert_tracked_worklog_custom_kg(rag, document)


async def _insert_tracked_worklog_custom_kg(
    rag: Any, document: LightRagWorklogCustomKgDocument,
) -> None:
    """확정 KG를 색인하고 native 삭제에 필요한 참조와 재구성 데이터를 함께 남긴다.

    native Custom KG는 삭제 추적을 생략한다. 확정 데이터는 LLM 재추출 없이
    native extraction-cache 형식으로 보존해 공유 노드의 남은 문서를 복원한다.
    """
    from lightrag.prompt import PROMPTS
    from lightrag.utils import compute_mdhash_id, make_relation_chunk_key, sanitize_text_for_encoding

    kg = document.custom_kg
    chunks = kg.get("chunks", [])
    if not chunks and not kg.get("entities") and not kg.get("relationships"):
        await rag.ainsert_custom_kg(kg, full_doc_id=document.document_id)
        return
    if len(chunks) != 1:
        raise ValueError("Worklog Custom KG requires one provenance chunk")
    chunk = chunks[0]
    source_id = chunk["source_id"]
    chunk_id = compute_mdhash_id(sanitize_text_for_encoding(chunk["content"]), prefix="chunk-")
    status = await rag.doc_status.get_by_id(document.document_id)
    if not isinstance(status, dict):
        raise ValueError("Custom KG requires an indexed body document")
    entities = kg.get("entities", [])
    relationships = kg.get("relationships", [])
    if any(item.get("source_id") != source_id for item in [*entities, *relationships]):
        raise ValueError("Custom KG source does not match its document chunk")

    delimiter = PROMPTS["DEFAULT_TUPLE_DELIMITER"]
    completion = PROMPTS["DEFAULT_COMPLETION_DELIMITER"]

    def field(value: str) -> str:
        """본문의 줄바꿈/예약 구분자가 native 재구성 레코드를 주입하지 못하게 한다."""
        return " ".join(value.replace(delimiter, " ").replace(completion, " ")
                        .replace(completion.lower(), " ").splitlines())

    records = [delimiter.join(map(field, (
        "entity", item["entity_name"], item["entity_type"], item["description"],
    ))) for item in entities]
    records.extend(delimiter.join(map(field, (
        "relation", item["src_id"], item["tgt_id"], item["keywords"], item["description"],
    ))) for item in relationships)
    cache_key = f"confirmed:extract:{chunk_id}"
    await rag.llm_response_cache.upsert({cache_key: {
        "cache_type": "extract", "chunk_id": chunk_id,
        "return": "\n".join([*records, completion]), "original_prompt": "DB-confirmed Custom KG",
    }})
    chunk_ids = list(dict.fromkeys([*status.get("chunks_list", []), chunk_id]))
    await rag.doc_status.upsert({document.document_id: {
        **status, "chunks_list": chunk_ids, "chunks_count": len(chunk_ids),
    }})
    await _extend_record(rag.full_entities, document.document_id, "entity_names",
                         [item["entity_name"] for item in entities])
    pairs = [list(sorted((item["src_id"], item["tgt_id"]))) for item in relationships]
    await _extend_record(rag.full_relations, document.document_id, "relation_pairs", pairs)
    for item in entities:
        await _extend_record(rag.entity_chunks, item["entity_name"], "chunk_ids", [chunk_id])
    for pair in pairs:
        await _extend_record(rag.relation_chunks, make_relation_chunk_key(*pair), "chunk_ids", [chunk_id])
    stores = (rag.doc_status, rag.full_entities, rag.full_relations, rag.entity_chunks,
              rag.relation_chunks, rag.llm_response_cache)
    # 부분 KG 쓰기도 나중에 삭제할 수 있도록 먼저 추적을 영속화한다.
    for store in stores:
        await store.index_done_callback()
    try:
        await rag.ainsert_custom_kg(kg, full_doc_id=document.document_id)
    finally:
        stored_chunk = await rag.text_chunks.get_by_id(chunk_id)
        if stored_chunk is not None:
            await rag.text_chunks.upsert({chunk_id: {
                **stored_chunk, "llm_cache_list": list(dict.fromkeys([
                    *stored_chunk.get("llm_cache_list", []), cache_key,
                ])),
            }})
            await rag.text_chunks.index_done_callback()
