import asyncio
import json
from types import SimpleNamespace

import httpx

from benchmarks import semantic_cache_real_embedding as benchmark


def test_benchmark_uses_unscoped_cache_for_seed_and_http_requests(tmp_path, monkeypatch):
    """팀 scope 캐시 우회 때문에 벤치마크가 RAG 경로에 도달하는 회귀를 잡는다."""
    entries = {}

    class Cache(benchmark.WorklogQueryCache):
        def __init__(self, **kwargs):
            self.exact_ms = []
            self.semantic_ms = []
            self.semantic_hits = 0

        async def get_version(self):
            return 0

        async def put(self, scope, signature, embedding, version, response):
            entries[(scope, signature, version)] = response

        async def get_exact(self, scope, signature, version):
            self.exact_ms.append(0.1)
            return entries.get((scope, signature, version))

        async def get_semantic(self, scope, embedding, version):
            self.semantic_ms.append(0.1)
            hit = next((value for (key, _, ver), value in entries.items()
                        if key == scope and ver == version), None)
            self.semantic_hits += hit is not None
            return hit

    class Redis:
        @staticmethod
        def from_url(url):
            return Redis()

        async def ping(self):
            return True

        async def dbsize(self):
            return 0

        async def aclose(self):
            pass

    class Gemini:
        def __init__(self, **kwargs):
            pass

        async def embed(self, texts):
            return [[1.0, 0.0] for _ in texts]

    monkeypatch.setattr(benchmark, "WorklogQueryCache", Cache)
    monkeypatch.setattr(benchmark, "TimedCache", Cache)
    monkeypatch.setattr(benchmark, "Redis", Redis)
    monkeypatch.setattr(benchmark, "GeminiClient", Gemini)
    monkeypatch.setattr(benchmark.settings, "gemini_api_key", "local-fake")
    monkeypatch.setattr(benchmark.settings, "embedding_dim", 768)
    monkeypatch.setattr(benchmark.settings, "ai_internal_token", "local-token")
    monkeypatch.setattr(benchmark.secrets, "token_urlsafe", lambda _: "local-token")
    output = tmp_path / "result.json"
    monkeypatch.setattr(benchmark, "OUTPUT", output)
    monkeypatch.setattr(benchmark.subprocess, "Popen", lambda *args, **kwargs: SimpleNamespace(
        poll=lambda: None, terminate=lambda: None, wait=lambda **kwargs: None))
    server = benchmark.make_server()
    async_client = httpx.AsyncClient
    monkeypatch.setattr(benchmark.httpx, "AsyncClient", lambda **kwargs: async_client(
        transport=httpx.ASGITransport(app=server), **kwargs))

    asyncio.run(benchmark.run())

    result = json.loads(output.read_text(encoding="utf-8"))
    assert result["status"] == "complete"
    assert result["audit"]["adapter_calls"] == 0
    assert result["audit"]["embedding_calls"] == 12
    assert result["audit"]["semantic_hits"] == 12
    assert result["summary"]["exact"]["count"] == 10
    assert result["summary"]["semantic"]["count"] == 10
