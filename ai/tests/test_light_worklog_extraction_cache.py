import asyncio
import copy

import pytest

from app.light.v4.store.worklog_extraction_cache import ValidatedWorklogExtractionCache


def test_live_cache_proxy_is_not_deepcopied_by_native_dataclass_serialization():
    cache = ValidatedWorklogExtractionCache(MemoryCache())
    assert copy.deepcopy(cache) is cache


@pytest.mark.parametrize("value", [
    'entity<|#|>""<|#|>Data<|#|>desc\n<|COMPLETE|>',
    'entity<|#|>A<|#|><|DataType|><|#|>desc\nentity<|#|>B<|#|>Data<|#|>desc\n<|COMPLETE|>',
    'entity<|#|>A<|#|>,,,<|#|>desc\nentity<|#|>B<|#|>Data<|#|>desc\n<|COMPLETE|>',
])
def test_parser_rejected_field_values_invalidate_entire_cached_extraction(value):
    native = MemoryCache({"bad": cache_entry(value)})
    cache = ValidatedWorklogExtractionCache(native)
    assert asyncio.run(cache.get_by_id("bad")) is None
    assert native.deleted == ["bad"]


class MemoryCache:
    def __init__(self, data=None):
        self.data = dict(data or {})
        self.deleted = []
        self.flushes = 0
        self.global_config = {"enable_llm_cache_for_entity_extract": True}
        self.workspace = "test"
        self.namespace = "llm_response_cache"

    async def get_by_id(self, key):
        return self.data.get(key)

    async def get_by_ids(self, keys):
        return [self.data.get(key) for key in keys]

    async def upsert(self, data):
        self.data.update(data)

    async def delete(self, keys):
        self.deleted.extend(keys)
        for key in keys:
            self.data.pop(key, None)

    async def index_done_callback(self):
        self.flushes += 1


def cache_entry(value, *, cache_type="extract", chunk_id="chunk-a", original_prompt="prompt"):
    return {"return": value, "cache_type": cache_type, "chunk_id": chunk_id,
            "original_prompt": original_prompt}


def test_malformed_extract_cache_is_invalidated_and_flushed():
    native = MemoryCache({"default:extract:bad": cache_entry("entity<|#|>only-two<|COMPLETE|>")})
    cache = ValidatedWorklogExtractionCache(native)

    assert asyncio.run(cache.get_by_id("default:extract:bad")) is None
    assert native.deleted == ["default:extract:bad"]
    assert native.flushes == 1


def test_get_by_ids_keeps_none_slots_and_invalidates_only_malformed_extracts():
    native = MemoryCache({"bad": cache_entry("entity<|#|>a<|#|>TYPE<|COMPLETE|>"),
                          "query": cache_entry("some answer", cache_type="query")})
    cache = ValidatedWorklogExtractionCache(native)

    values = asyncio.run(cache.get_by_ids(["missing", "bad", "query"]))

    assert values == [None, None, native.data["query"]]
    assert native.deleted == ["bad"]
    assert native.flushes == 1


@pytest.mark.parametrize("value", [
    "entity<|#|>123<|#|>WORKLOG<|#|>desc<|COMPLETE|>",
    "relation<|#|>Worklog:1<|#|>User:2<|#|>AUTHORED_BY<|#|>desc<|COMPLETE|>",
    "<|COMPLETE|>",
])
def test_valid_native_extract_results_are_preserved(value):
    native = MemoryCache({"extract": cache_entry(value)})
    cache = ValidatedWorklogExtractionCache(native)
    original = native.data["extract"]

    assert asyncio.run(cache.get_by_id("extract")) == original
    assert native.deleted == []
    assert native.flushes == 0


def test_query_and_confirmed_custom_kg_cache_are_preserved():
    confirmed = cache_entry("not native syntax", original_prompt="DB-confirmed Custom KG")
    native = MemoryCache({"query": cache_entry("answer", cache_type="query"),
                          "confirmed:extract:chunk": confirmed})
    cache = ValidatedWorklogExtractionCache(native)

    assert asyncio.run(cache.get_by_ids(["query", "confirmed:extract:chunk"])) == [
        native.data["query"], confirmed,
    ]
    assert native.deleted == []


def test_confirmed_cache_exception_requires_all_markers():
    trusted_key = "confirmed:extract:chunk"
    native = MemoryCache({trusted_key: cache_entry("bad", original_prompt="wrong")})
    cache = ValidatedWorklogExtractionCache(native)

    assert asyncio.run(cache.get_by_id(trusted_key)) is None
    assert native.deleted == [trusted_key]


@pytest.mark.parametrize("value", [
    "<|COMPLETE|>\n<|COMPLETE|>",
    "entity<|#|>Name<|#|>TYPE<|#|>desc<|COMPLETE|> <|#|>junk",
])
def test_native_extract_rejects_reserved_marker_corruption(value):
    native = MemoryCache({"bad": cache_entry(value)})
    cache = ValidatedWorklogExtractionCache(native)

    assert asyncio.run(cache.get_by_id("bad")) is None
    assert native.deleted == ["bad"]


def test_upsert_rejects_malformed_extract_and_accepts_confirmed_payload():
    native = MemoryCache()
    cache = ValidatedWorklogExtractionCache(native)

    with pytest.raises(ValueError):
        asyncio.run(cache.upsert({"bad": cache_entry("entity<|#|>short<|COMPLETE|>")}))

    asyncio.run(cache.upsert({"confirmed:extract:chunk": cache_entry(
        "bad", original_prompt="DB-confirmed Custom KG")
    }))
    assert "bad" not in native.data
    assert "confirmed:extract:chunk" in native.data


def test_other_cache_types_are_not_parsed():
    native = MemoryCache()
    cache = ValidatedWorklogExtractionCache(native)

    asyncio.run(cache.upsert({"query": cache_entry("anything", cache_type="query")}))
    assert native.data["query"]["return"] == "anything"

