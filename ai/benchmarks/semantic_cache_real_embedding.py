"""Isolated HTTP benchmark for real Gemini embedding + Redis semantic cache.

Run from ai/: python -m benchmarks.semantic_cache_real_embedding run
Requires a dedicated Redis 8 instance at redis://127.0.0.1:6388/0.
Never points this script at the configured/shared Redis URL.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
from pathlib import Path
import secrets
import statistics
import subprocess
import sys
import time

import httpx
from fastapi import FastAPI
from redis.asyncio import Redis

from app.client.gemini_client import GeminiClient
from app.config.settings import settings
from app.light.v3.model.worklog_query import WorklogLightQueryResponse
from app.light.v3.router.worklog_query import get_worklog_query_service, router
from app.light.v3.service.worklog_query_cache import WorklogQueryCache
from app.light.v3.service.worklog_query_service import LightWorklogQueryService


REDIS_URL = "redis://127.0.0.1:6388/0"
BASE_URL = "http://127.0.0.1:8099"
QUERY_A = "최근 업무일지에서 해결된 장애 사례를 요약해줘"
QUERY_B = "최근 작업 일지의 장애 해결 사례를 요약해 주세요"
MARKER = "AXWMS_SEMANTIC_BENCHMARK_SEEDED_ANSWER"
OUTPUT = Path(__file__).resolve().parents[2] / "docs/ai/report/semantic-cache-real-embedding-raw.json"


def summarize(values: list[float]) -> dict[str, float | int]:
    """Report descriptive values only; n=10 does not support a p95 claim."""
    if not values:
        raise ValueError("empty sample")
    return {"count": len(values), "median_ms": round(statistics.median(values), 3),
            "min_ms": round(min(values), 3), "max_ms": round(max(values), 3)}


def validate_hit(body: dict, marker: str) -> None:
    """Reject a fallback answer or malformed response as a cache hit."""
    if body.get("answer") != marker or body.get("mode") != "mix" or body.get("internalOnly") is not True:
        raise ValueError("response did not match the seeded cache answer")


class TimedCache(WorklogQueryCache):
    """Measure only the real Redis lookup calls without changing their behavior."""

    def __init__(self):
        super().__init__(settings_obj=settings, client=Redis.from_url(REDIS_URL))
        self.exact_ms: list[float] = []
        self.semantic_ms: list[float] = []
        self.semantic_hits = 0

    async def get_exact(self, scope, signature, version):
        start = time.perf_counter_ns()
        result = await super().get_exact(scope, signature, version)
        self.exact_ms.append((time.perf_counter_ns() - start) / 1e6)
        return result

    async def get_semantic(self, scope, embedding, version):
        start = time.perf_counter_ns()
        result = await super().get_semantic(scope, embedding, version)
        self.semantic_ms.append((time.perf_counter_ns() - start) / 1e6)
        self.semantic_hits += result is not None
        return result


def make_server() -> FastAPI:
    """Mount the production router/service with a fail-closed RAG adapter."""
    app = FastAPI()
    cache = TimedCache()
    gemini = GeminiClient(api_key=settings.gemini_api_key)
    audit = {"embedding_calls": 0, "embedding_ms": [], "adapter_calls": 0}

    async def embed(text: str) -> list[float]:
        if audit["embedding_calls"] >= 12:
            raise RuntimeError("Gemini call cap reached")
        audit["embedding_calls"] += 1
        start = time.perf_counter_ns()
        try:
            return (await gemini.embed([text]))[0]
        finally:
            audit["embedding_ms"].append((time.perf_counter_ns() - start) / 1e6)

    def blocked_adapter():
        audit["adapter_calls"] += 1
        raise RuntimeError("LightRAG adapter was reached: cache miss")

    service = LightWorklogQueryService(settings_obj=settings, cache=cache,
                                       embedder=embed, adapter_factory=blocked_adapter)
    app.dependency_overrides[get_worklog_query_service] = lambda: service
    app.include_router(router, prefix="/ai")

    @app.get("/benchmark-audit")
    async def benchmark_audit():
        return {**audit, "exact_ms": cache.exact_ms, "semantic_ms": cache.semantic_ms,
                "semantic_hits": cache.semantic_hits}

    return app


async def run() -> None:
    """Preflight similarity, seed a real Redis HASH, and time sequential HTTP requests."""
    if not settings.gemini_api_key:
        raise RuntimeError("GEMINI_API_KEY missing")
    if settings.embedding_dim != 768:
        raise RuntimeError("expected 768-dimensional embeddings")
    if OUTPUT.exists():
        raise RuntimeError(f"refusing to overwrite existing evidence: {OUTPUT}")

    redis = Redis.from_url(REDIS_URL)
    if not await redis.ping():
        raise RuntimeError("dedicated Redis unavailable")
    if await redis.dbsize() != 0:
        raise RuntimeError("dedicated Redis must be empty; not deleting keys")

    result = {"status": "started", "preflight_calls": 0, "samples": {"semantic": [], "exact": []},
              "environment": {"python": sys.version.split()[0], "embedding_model": settings.embedding_model,
                              "redis_url": REDIS_URL, "server_url": BASE_URL, "concurrency": 1}}
    proc = None
    try:
        gemini = GeminiClient(api_key=settings.gemini_api_key)
        vectors = []
        for query in (QUERY_A, QUERY_B):
            result["preflight_calls"] += 1
            vectors.append((await gemini.embed([query]))[0])
        a, b = vectors
        cosine = sum(x * y for x, y in zip(a, b)) / (math.sqrt(sum(x*x for x in a)) * math.sqrt(sum(y*y for y in b)))
        result["cosine"] = round(cosine, 6)
        if cosine < 0.90:
            raise RuntimeError("query pair does not meet the 0.90 similarity threshold")

        cache = WorklogQueryCache(settings_obj=settings, client=redis)
        scope = cache.scope_key([1])
        def signature(query: str) -> str:
            return cache.signature(query, top_k=settings.lightrag_query_top_k,
                chunk_top_k=settings.lightrag_query_chunk_top_k,
                response_type=settings.lightrag_query_response_type,
                enable_rerank=settings.lightrag_rerank_enabled, rerank_model=None,
                llm_model=settings.gemini_model, embedding_model=settings.embedding_model)
        assert signature(QUERY_A) != signature(QUERY_B)
        await cache.put(scope, signature(QUERY_A), a, 0,
                        WorklogLightQueryResponse(answer=MARKER, references=[], internalOnly=True))

        token = secrets.token_urlsafe(32)
        env = {**os.environ, "REDIS_URL": REDIS_URL, "AI_INTERNAL_TOKEN": token,
               "GEMINI_API_KEYS": ""}
        proc = subprocess.Popen([sys.executable, "-m", "benchmarks.semantic_cache_real_embedding", "server"],
                                cwd=Path(__file__).resolve().parents[1], env=env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        async with httpx.AsyncClient(timeout=45.0) as client:
            for _ in range(50):
                try:
                    if (await client.get(BASE_URL + "/benchmark-audit")).status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                if proc.poll() is not None:
                    raise RuntimeError("benchmark server exited early")
                await asyncio.sleep(0.2)
            else:
                raise RuntimeError("benchmark server did not start")

            headers = {"x-ai-internal-token": token}
            async def request(query: str) -> float:
                start = time.perf_counter_ns()
                response = await client.post(BASE_URL + "/ai/light/worklogs-v3/query",
                                             json={"query": query, "allowedTeamIds": [1]}, headers=headers)
                elapsed = (time.perf_counter_ns() - start) / 1e6
                response.raise_for_status()
                validate_hit(response.json(), MARKER)
                return round(elapsed, 3)

            for _ in range(2):
                await request(QUERY_B)
            audit_after_warmup = (await client.get(BASE_URL + "/benchmark-audit")).json()
            if audit_after_warmup["embedding_calls"] != 2 or audit_after_warmup["semantic_hits"] != 2 or audit_after_warmup["adapter_calls"]:
                raise RuntimeError("semantic warmup did not follow the expected path")
            for _ in range(10):
                result["samples"]["semantic"].append(await request(QUERY_B))
            semantic_audit = (await client.get(BASE_URL + "/benchmark-audit")).json()
            if semantic_audit["embedding_calls"] != 12 or semantic_audit["semantic_hits"] != 12 or semantic_audit["adapter_calls"]:
                raise RuntimeError("semantic measured requests did not follow the expected path")
            for _ in range(2):
                await request(QUERY_A)
            for _ in range(10):
                result["samples"]["exact"].append(await request(QUERY_A))
            final_audit = (await client.get(BASE_URL + "/benchmark-audit")).json()
            if final_audit["embedding_calls"] != 12 or final_audit["semantic_hits"] != 12 or final_audit["adapter_calls"]:
                raise RuntimeError("exact control unexpectedly used Gemini or LightRAG")
            result["audit"] = final_audit
            result["gemini_total_calls"] = result["preflight_calls"] + final_audit["embedding_calls"]
            result["summary"] = {name: summarize(values) for name, values in result["samples"].items()}
            result["status"] = "complete"
    except Exception as exc:
        result["status"] = "failed"
        result["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
        raise
    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        await redis.aclose()
        OUTPUT.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"status={result['status']} preflight_calls={result['preflight_calls']} evidence={OUTPUT}")


if __name__ == "__main__":
    if sys.argv[1:] == ["server"]:
        import uvicorn
        uvicorn.run(make_server(), host="127.0.0.1", port=8099, log_level="error")
    elif sys.argv[1:] == ["run"]:
        asyncio.run(run())
    else:
        raise SystemExit("use 'run' or 'server'")
