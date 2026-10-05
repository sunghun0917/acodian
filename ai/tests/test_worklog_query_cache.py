import asyncio
import fnmatch

import pytest

from app.light.v3.model.worklog_query import WorklogLightQueryResponse
from app.light.v3.service.worklog_query_cache import WorklogQueryCache


class FakeSearch:
    def __init__(self):
        self.created = False
        self.docs = {}
        self.last_search = None
        self.search_result = [0]

    async def execute_command(self, *args):
        if args[0] == "FT.CREATE":
            self.created = True
            return "OK"
        if args[0] == "FT.SEARCH":
            self.last_search = args
            return self.search_result
        raise AssertionError(args)


class FakePipeline:
    def __init__(self, redis):
        self.redis = redis
        self.actions = []

    def hset(self, key, mapping):
        self.actions.append(("hset", key, mapping))
        return self

    def expire(self, key, seconds):
        self.actions.append(("expire", key, seconds))
        return self

    async def execute(self):
        self.redis.pipeline_actions = self.actions


class FakeRedis(FakeSearch):
    def __init__(self):
        super().__init__()
        self.kv = {}
        self.pipeline_actions = []
        self.closed = False
        self.deleted_batches = []
        self.incr_calls = 0

    async def incr(self, key):
        self.incr_calls += 1
        self.kv[key] = int(self.kv.get(key, 0)) + 1
        return self.kv[key]

    async def scan_iter(self, *, match, count):
        assert count == 100
        for key in list(self.kv):
            if fnmatch.fnmatch(key, match):
                yield key

    async def delete(self, *keys):
        self.deleted_batches.append(keys)
        for key in keys:
            self.kv.pop(key, None)
        return len(keys)

    async def get(self, key):
        return self.kv.get(key)

    async def set(self, key, value, *, ex):
        self.kv[key] = value
        self.set_expiry = ex

    def pipeline(self, *, transaction):
        assert transaction is True
        return FakePipeline(self)

    async def aclose(self):
        self.closed = True


def make_cache():
    class Settings:
        redis_url = "redis://localhost:6379/0"

    client = FakeRedis()
    return WorklogQueryCache(settings_obj=Settings(), client=client), client


def test_scope_and_signature_are_stable_and_distinct():
    cache, _ = make_cache()
    assert cache.scope_key([3, 1, 3]) == cache.scope_key([1, 3])
    assert cache.scope_key(None) != cache.scope_key([])
    assert len(cache.scope_key([1])) == 64
    opts = dict(top_k=10, chunk_top_k=5, response_type="Multiple Paragraphs",
                enable_rerank=False, rerank_model=None, llm_model="gemini",
                embedding_model="gemini-embedding-001")
    signature = cache.signature("질문", **opts)
    assert len(signature) == 64
    assert signature != cache.signature("다른 질문", **opts)
    assert signature != cache.signature("질문", **{**opts, "top_k": 20})


def test_exact_roundtrip_and_ttl():
    asyncio.run(_exact_roundtrip_and_ttl())


async def _exact_roundtrip_and_ttl():
    cache, redis = make_cache()
    response = WorklogLightQueryResponse(answer="답변", references=[])
    await cache.put("a" * 64, "b" * 64, [0.1] * 768, 0, response)
    assert redis.set_expiry == 86400
    assert any(action[0] == "expire" and action[2] == 86400 for action in redis.pipeline_actions)
    assert await cache.get_exact("a" * 64, "b" * 64, 0) == response
    assert await cache.get_exact("c" * 64, "b" * 64, 0) is None
    await cache.close()
    assert redis.closed


def test_semantic_search_is_scope_filtered_and_uses_cosine_threshold():
    asyncio.run(_semantic_search_is_scope_filtered_and_uses_cosine_threshold())


async def _semantic_search_is_scope_filtered_and_uses_cosine_threshold():
    cache, redis = make_cache()
    assert await cache.get_semantic("a" * 64, [0.1] * 768, 0) is None
    assert redis.created
    command = redis.last_search
    assert "a" * 64 in command[2]
    assert "@version:{0}" in command[2]
    assert "distance" in command
    assert "DIALECT" in command
    response = WorklogLightQueryResponse(answer="답변", references=[])
    redis.search_result = [1, b"key", [b"response", response.model_dump_json().encode(), b"distance", b"0.09"]]
    assert await cache.get_semantic("a" * 64, [0.1] * 768, 0) == response
    redis.search_result = [1, b"key", [b"response", response.model_dump_json().encode(), b"distance", b"0.11"]]
    assert await cache.get_semantic("a" * 64, [0.1] * 768, 0) is None


@pytest.mark.parametrize("vector", [[0.1], [float("nan")] * 768, [float("inf")] * 768, [1e40] * 768])
def test_invalid_embedding_rejected_before_redis(vector):
    asyncio.run(_invalid_embedding_rejected_before_redis(vector))


async def _invalid_embedding_rejected_before_redis(vector):
    cache, redis = make_cache()
    with pytest.raises(ValueError):
        await cache.get_semantic("a" * 64, vector, 0)
    assert not redis.created


def test_invalidate_all_deletes_only_exact_and_semantic_cache_keys():
    async def run_case():
        cache, redis = make_cache()
        redis.kv.update({
            "worklog:query:v3:exact:a": "answer",
            "worklog:query:v3:semantic:b": "answer",
            "worklog:query:v3:other:c": "keep",
            "unrelated:exact:d": "keep",
        })

        deleted = await cache.invalidate_all()

        assert deleted == 2
        assert redis.kv == {
            "worklog:query:v3:other:c": "keep",
            "unrelated:exact:d": "keep",
        }
        assert all(len(batch) <= 100 for batch in redis.deleted_batches)

    asyncio.run(run_case())


def test_version_bump_is_single_incr_and_keeps_old_keys_until_ttl():
    async def run_case():
        cache, redis = make_cache()
        response = WorklogLightQueryResponse(answer="답변", references=[])
        scope, signature, vector = "a" * 64, "b" * 64, [0.1] * 768
        assert await cache.get_version() == 0
        await cache.put(scope, signature, vector, 0, response)
        assert await cache.bump_version() == 1
        assert redis.incr_calls == 1
        assert await cache.get_version() == 1
        assert await cache.get_exact(scope, signature, 1) is None
        assert await cache.get_exact(scope, signature, 0) == response
        assert redis.kv[f"{cache._prefix}:version"] == 1
        assert redis.kv[f"{cache._prefix}:exact:0:{scope}:{signature}"]
        assert any(action[0] == "hset" and action[2]["version"] == "0"
                   for action in redis.pipeline_actions)
        await cache.get_semantic(scope, vector, 1)
        assert "@version:{1}" in redis.last_search[2]

    asyncio.run(run_case())
