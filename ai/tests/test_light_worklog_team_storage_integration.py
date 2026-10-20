"""별도 로컬 Qdrant/Neo4j에서 실제 LightRAG 저장→검색→삭제를 검증한다.

RAG_SCOPE_TEST_ID, RAG_SCOPE_TEST_QDRANT_URL, RAG_SCOPE_TEST_NEO4J_URI로 명시 실행.
기존 6333/7687 포트는 거부하며, 유료 모델은 고정 로컬 함수로 대체한다.
"""

import asyncio
import json
import os
from dataclasses import replace
from types import SimpleNamespace
from urllib.parse import urlparse
from uuid import uuid4

import numpy as np
import pytest

from app.config.settings import Settings
from app.light.v4.model.worklog_query import WorklogLightQueryRequest
from app.light.v4.service.lightrag_adapter import LightRagDependencies, LightRagWorklogIndexAdapter
from app.light.v4.service.worklog_query_service import LightWorklogQueryService
from app.light.v4.service.worklog_team_scope import LightRagTeamScopeAdapter
from app.light.v4.store.worklog_source_store import (
    LightWorklogPredecessor, LightWorklogSourceRow, LightWorklogTag,
)

pytestmark = pytest.mark.skipif(not (os.environ.get("RAG_SCOPE_TEST_ID") and os.environ.get("RAG_SCOPE_TEST_PG_DSN")),
                                reason="requires isolated Qdrant/Neo4j test containers")


def integration_pool(tmp_path, monkeypatch):
    """운영 인덱스와 공유하지 않는 loopback 테스트 서버에만 adapter를 연결한다."""
    from lightrag import LightRAG, QueryParam
    from lightrag.utils import wrap_embedding_func_with_attrs
    from app.light.v4.service import taskgroup_search
    import lightrag.operate as operate

    from tests.test_light_worklog_pg_storage_integration import validate_test_pg_dsn
    pg_dsn = validate_test_pg_dsn(os.environ["RAG_SCOPE_TEST_PG_DSN"])
    qdrant = os.environ["RAG_SCOPE_TEST_QDRANT_URL"]
    neo4j = os.environ["RAG_SCOPE_TEST_NEO4J_URI"]
    for endpoint, forbidden in ((qdrant, 6333), (neo4j, 7687)):
        parsed = urlparse(endpoint)
        assert parsed.hostname == "127.0.0.1" and parsed.port not in (None, forbidden)
    for key in ("NEO4J_WORKSPACE", "QDRANT_WORKSPACE", "QDRANT_API_KEY", "POSTGRES_WORKSPACE"):
        monkeypatch.delenv(key, raising=False)
    for key in ("NEO4J_URI", "NEO4J_USERNAME", "NEO4J_PASSWORD", "NEO4J_DATABASE", "QDRANT_URL"):
        monkeypatch.setenv(key, "")  # fixture 종료 시 실제 환경 복원
    monkeypatch.setattr(operate, "_perform_kg_search", taskgroup_search.parallel_perform_kg_search)

    async def complete(prompt, **kwargs):
        if kwargs.get("keyword_extraction"):
            return json.dumps({"high_level_keywords": ["shared issue"],
                               "low_level_keywords": ["SharedConcept", "User:24"]})
        marker = "B_ONLY_TEXT" if "B_ONLY_TEXT" in prompt else "A_ONLY_TEXT"
        return (f"entity<|#|>SharedConcept<|#|>concept<|#|>{marker}\n"
                "entity<|#|>User:24<|#|>user<|#|>Shared author\n"
                f"relation<|#|>SharedConcept<|#|>User:24<|#|>shared issue<|#|>{marker}\n"
                "<|COMPLETE|>")

    async def embed(texts, **kwargs):
        return np.array([[1.0, 0.0] if "B_ONLY_TEXT" in text or text == "shared issue"
                         else [0.8, 0.6] for text in texts])

    dependencies = LightRagDependencies(
        lightrag_class=LightRAG, query_param_class=QueryParam,
        gemini_model_complete=complete, gemini_embed=SimpleNamespace(func=embed),
        embedding_wrapper=wrap_embedding_func_with_attrs,
    )
    identity = f"{os.environ['RAG_SCOPE_TEST_ID']}_{uuid4().hex[:8]}"
    settings = Settings(
        _env_file=None, lightrag_pg_dsn=pg_dsn, gemini_api_key="local-test-only", gemini_api_keys="",
        lightrag_workspace=f"scope_test_{identity}",
        lightrag_working_dir=str(tmp_path),
        lightrag_embedding_model=f"scope-test-{identity}", embedding_dim=2,
        lightrag_vector_storage="QdrantVectorDBStorage", lightrag_qdrant_url=qdrant,
        lightrag_qdrant_api_key="", lightrag_graph_storage="Neo4JStorage",
        lightrag_neo4j_database="neo4j", neo4j_uri=neo4j,
        neo4j_user="neo4j", neo4j_password="scope_contract_test",
        lightrag_rerank_binding_host="", lightrag_insert_timeout_seconds=90,
        lightrag_query_timeout_seconds=60,
    )

    def adapter_factory(**kwargs):
        return LightRagWorklogIndexAdapter(**kwargs, dependencies=dependencies)

    return LightRagTeamScopeAdapter(settings_obj=settings, adapter_factory=adapter_factory), settings


def rows():
    """유사한 본문, 공유 User/Tag, 팀 간 선행 업무를 가진 작은 고정 fixture."""
    return [LightWorklogSourceRow(
        worklog_id=worklog, title=f"Task {worklog}", request_content="shared issue",
        work_content=marker, author_id=24, author_name="Shared author",
        team_id=team, team_name=f"Team {team}",
        tags=[LightWorklogTag(tag_id=5, tag_name="Shared tag", description=marker)],
        direct_predecessors=[LightWorklogPredecessor(2, "B_ONLY_TITLE", team_id=20)] if worklog == 1 else [],
    ) for worklog, team, marker in ((2, 20, "B_ONLY_TEXT"), (1, 10, "A_ONLY_TEXT"), (3, 10, "A_ONLY_TEXT"))]


def test_real_team_index_retrieval_graph_pg_and_delete(tmp_path, monkeypatch):
    from app.light.v4.service.lightrag_adapter import LightRagQueryOptions
    pool, _ = integration_pool(tmp_path, monkeypatch)

    async def run():
        try:
            await pool.index_source_rows(rows())
            a = await pool.adapter_for(10)._get_initialized_rag()
            b = await pool.adapter_for(20)._get_initialized_rag()
            for storage_name in ("chunks_vdb", "entities_vdb", "relationships_vdb"):
                av, bv = getattr(a, storage_name), getattr(b, storage_name)
                results = await av.query("shared issue", top_k=1, query_embedding=[1.0, 0.0])
                assert results and all(item["workspace_id"] == a.workspace for item in results)
                assert "B_ONLY" not in json.dumps(results)
                points, _ = av._client.scroll(av.final_namespace, limit=100)
                assert {point.payload["workspace_id"] for point in points} == {a.workspace, b.workspace}
            graph = a.chunk_entity_relation_graph
            assert await graph.get_node("Worklog:2") is None
            assert await b.chunk_entity_relation_graph.get_node("Worklog:1") is None
            assert "A_ONLY" in (await graph.get_node("Tag:5"))["description"]
            assert "B_ONLY" in (await b.chunk_entity_relation_graph.get_node("Tag:5"))["description"]
            assert "B_ONLY" not in json.dumps(await graph.get_nodes_batch(["User:24", "Tag:5", "SharedConcept"]))
            assert "B_ONLY" not in repr(await graph.get_edges_batch([
                {"src": "SharedConcept", "tgt": "User:24"}, {"src": "Worklog:1", "tgt": "Tag:5"},
            ]))
            assert all("Worklog:2" not in pair for pair in (await graph.get_nodes_edges_batch(["User:24"]))["User:24"])
            record = await a.full_docs.get_by_id("worklog-1")
            assert "worklog_source" not in record and record["content"]
            assert (await a.doc_status.get_by_id("worklog-1"))["status"] == "processed"
            options = LightRagQueryOptions(query="shared issue", top_k=10, chunk_top_k=10, response_type="summary")
            results = await asyncio.gather(*(pool.query_data(options, teams) for teams in ((10,), (20,), (10, 20))))
            assert "A_ONLY" in json.dumps(results[0]) and "B_ONLY" not in json.dumps(results[0])
            assert "B_ONLY" in json.dumps(results[1]) and "A_ONLY" not in json.dumps(results[1])
            assert "A_ONLY" in json.dumps(results[2]) and "B_ONLY" in json.dumps(results[2])
            await pool.adapter_for(10).delete_document("worklog-1")
            assert await graph.get_node("Worklog:1") is None
            assert await graph.get_node("User:24") is not None
            assert await graph.get_node("Tag:5") is not None
            assert await b.chunk_entity_relation_graph.get_node("Worklog:2") is not None
            await pool.adapter_for(10).delete_document("worklog-3")
            assert await graph.get_node("User:24") is None
            assert await graph.get_node("Tag:5") is None
            assert "B_ONLY" in json.dumps(await pool.query_data(options, (20,)))
        finally:
            await pool.close()

    asyncio.run(run())


def test_real_retrieval_normal_and_sse_use_latest_source(tmp_path, monkeypatch):
    pool, settings = integration_pool(tmp_path, monkeypatch)
    prompts, source_calls = [], []
    source = {row.worklog_id: row for row in rows()}
    class Reader:
        async def fetch_source_rows(self, ids):
            source_calls.append(list(ids))
            return {key: row for key, row in source.items() if key in ids}
    pool._source_reader = Reader()

    class Generator:
        async def complete_scoped(self, query, *, system_prompt, stream=False):
            prompts.append(system_prompt)
            if stream:
                async def tokens():
                    yield "safe answer"
                return tokens()
            return "safe answer"

    service = LightWorklogQueryService(
        settings_obj=settings, adapter_factory=Generator, team_adapter_factory=lambda: pool,
    )
    request = WorklogLightQueryRequest.model_validate({"query": "shared issue", "allowedTeamIds": [10]})

    async def run():
        try:
            await pool.index_source_rows(list(source.values()))
            source[1] = replace(source[1], work_content="LATEST_A_BODY")
            response = await service.query_worklogs(request)
            assert response.answer == "safe answer"
            assert {ref.file_path for ref in response.references} == {"worklog://1", "worklog://3"}
            events = [event async for event in service.query_worklogs_stream(request)]
            assert events[-1]["event"] == "done" and "worklog://2" not in events[-1]["data"]
            assert source_calls and len(prompts) == 2
            assert all("LATEST_A_BODY" in prompt and "B_ONLY" not in prompt for prompt in prompts)
        finally:
            await pool.close()

    asyncio.run(run())
