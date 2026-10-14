"""실제 Neo4j driver 호출 직전 Cypher를 검사한다. 실서버 roundtrip을 대체하지 않는다."""

import asyncio
from types import SimpleNamespace

from lightrag.kg.neo4j_impl import Neo4JStorage


def test_native_graph_batch_lookup_and_expansion_constrain_both_endpoints(monkeypatch):
    monkeypatch.delenv("NEO4J_WORKSPACE", raising=False)
    calls = []

    class Result:
        async def __aiter__(self):
            if False:
                yield None

        async def consume(self):
            return None

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def run(self, query, **params):
            calls.append((query, params))
            return Result()

    graph = Neo4JStorage(namespace="graph", workspace="team_a", global_config={}, embedding_func=None)
    graph._driver = SimpleNamespace(session=lambda **kwargs: Session())
    graph._DATABASE = "neo4j"

    async def run():
        await graph.get_nodes_batch(["User:24", "Worklog:1"])
        await graph.get_edges_batch([{"src": "Worklog:1", "tgt": "User:24"}])
        await graph.get_nodes_edges_batch(["User:24"])

    asyncio.run(run())
    assert "MATCH (n:`team_a`" in calls[0][0]
    assert "start:`team_a`" in calls[1][0] and "end:`team_a`" in calls[1][0]
    assert "n:`team_a`" in calls[2][0] and "connected:`team_a`" in calls[2][0]
    assert "User:24" not in calls[0][0]  # ID는 Cypher에 문자열 삽입하지 않는다.
    assert calls[0][1] == {"node_ids": ["User:24", "Worklog:1"]}
