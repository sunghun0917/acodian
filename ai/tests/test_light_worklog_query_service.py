from __future__ import annotations

from typing import Any

import asyncio

import pytest

from app.config.settings import Settings
from app.light.v3.model.worklog_query import WorklogLightQueryRequest
from app.light.v3.service.lightrag_adapter import (
    LightRagQueryFailedError,
    LightRagQueryOptions,
    LightRagQueryResult,
    LightRagQueryTimeoutError,
)
from app.light.v3.service.worklog_query_service import LightWorklogQueryService


_DEFAULT_ITERATOR = object()


class FakeAdapter:
    def __init__(self, raw: dict[str, Any], iterator: Any = _DEFAULT_ITERATOR) -> None:
        self.raw = raw
        self.iterator = iterator
        self.calls: list[LightRagQueryOptions] = []

    async def query_worklogs(self, options: LightRagQueryOptions) -> LightRagQueryResult:
        self.calls.append(options)
        return LightRagQueryResult(raw=self.raw)

    async def query_worklogs_stream(self, options: LightRagQueryOptions) -> tuple[Any, Any]:
        self.calls.append(options)
        iterator = self.iterator
        if iterator is _DEFAULT_ITERATOR:
            async def default_gen():
                yield "tok1"
                yield "tok2"
            iterator = default_gen()
        return iterator, self.raw


def make_request(**overrides: Any) -> WorklogLightQueryRequest:
    payload = {"query": "업무일지 요약", "allowedTeamIds": None, **overrides}
    return WorklogLightQueryRequest.model_validate(payload)


def test_query_service_applies_settings_defaults_and_normalizes_reference() -> None:
    adapter = FakeAdapter(
        {
            "llm_response": {"content": " native answer "},
            "data": {
                "references": [
                    {"reference_id": "1", "file_path": "worklog://1"},
                    {"reference_id": "2", "file_path": "worklog://2"},
                ]
            },
        }
    )
    settings = Settings(
        _env_file=None,
        lightrag_query_top_k=11,
        lightrag_query_chunk_top_k=5,
        lightrag_query_response_type="Multiple Paragraphs",
    )
    service = LightWorklogQueryService(settings_obj=settings, adapter_factory=lambda: adapter)

    response = asyncio.run(service.query_worklogs(make_request()))

    assert len(adapter.calls) == 1
    call = adapter.calls[0]
    assert call.query == "업무일지 요약"
    assert call.top_k == 11
    assert call.chunk_top_k == 5
    assert call.response_type == "Multiple Paragraphs"
    assert call.enable_rerank is False
    assert call.system_prompt is not None
    assert "allowedTeamIds" not in call.system_prompt
    assert "Access Scope" not in call.system_prompt
    assert response.answer == "native answer"
    assert response.references[0].reference_id == "1"
    assert response.references[0].file_path == "worklog://1"
    assert response.references[1].reference_id == "2"
    assert response.references[1].file_path == "worklog://2"
    assert response.internal_only is True


def test_query_service_uses_one_version_snapshot_for_read_and_write() -> None:
    class Cache:
        version = 0

        def __init__(self):
            self.values = {}
            self.read_versions = []
            self.write_versions = []

        async def get_version(self):
            return self.version

        async def get_exact(self, scope, signature, version):
            self.read_versions.append(version)
            return self.values.get((version, scope, signature))

        async def get_semantic(self, scope, embedding, version):
            self.read_versions.append(version)
            return None

        async def put(self, scope, signature, embedding, version, response):
            self.write_versions.append(version)
            self.values[(version, scope, signature)] = response

    adapter = FakeAdapter({"llm_response": {"content": "answer"}, "data": {"references": []}})
    cache = Cache()
    service = LightWorklogQueryService(
        settings_obj=Settings(_env_file=None), adapter_factory=lambda: adapter,
        cache=cache, embedder=lambda _: asyncio.sleep(0, result=[0.1] * 768),
    )

    async def run_case():
        first = await service.query_worklogs(make_request())
        assert await service.query_worklogs(make_request()) == first
        assert len(adapter.calls) == 1
        cache.version = 1
        assert await service.query_worklogs(make_request()) == first
        assert len(adapter.calls) == 2
        assert cache.read_versions == [0, 0, 0, 1, 1]
        assert cache.write_versions == [0, 1]

    asyncio.run(run_case())


def test_query_service_skips_cache_when_version_read_fails() -> None:
    class BrokenCache:
        async def get_version(self):
            raise ConnectionError("Redis down")

        async def get_exact(self, *args):
            raise AssertionError("cache must be bypassed")

        async def get_semantic(self, *args):
            raise AssertionError("cache must be bypassed")

        async def put(self, *args):
            raise AssertionError("cache must be bypassed")

    adapter = FakeAdapter({"llm_response": {"content": "answer"}, "data": {"references": []}})
    service = LightWorklogQueryService(
        settings_obj=Settings(_env_file=None), adapter_factory=lambda: adapter,
        cache=BrokenCache(),
    )
    assert asyncio.run(service.query_worklogs(make_request())).answer == "answer"
    assert len(adapter.calls) == 1


def test_query_service_enables_rerank_per_query() -> None:
    adapter = FakeAdapter(
        {"llm_response": {"content": "answer"}, "data": {"references": []}}
    )
    settings = Settings(_env_file=None, lightrag_rerank_enabled=False)
    service = LightWorklogQueryService(settings_obj=settings, adapter_factory=lambda: adapter)

    asyncio.run(service.query_worklogs(make_request(enableRerank=True)))

    assert adapter.calls[0].enable_rerank is True


def test_query_service_passes_rerank_model_override() -> None:
    adapter = FakeAdapter(
        {"llm_response": {"content": "answer"}, "data": {"references": []}}
    )
    service = LightWorklogQueryService(
        settings_obj=Settings(_env_file=None), adapter_factory=lambda: adapter
    )

    asyncio.run(
        service.query_worklogs(
            make_request(enableRerank=True, rerankModel="BAAI/custom-reranker")
        )
    )

    assert adapter.calls[0].rerank_model == "BAAI/custom-reranker"


def test_query_service_empty_allowed_team_ids_skips_adapter() -> None:
    adapter = FakeAdapter(
        {
            "llm_response": {"content": "answer"},
            "data": {"references": []},
        }
    )
    service = LightWorklogQueryService(
        settings_obj=Settings(_env_file=None),
        adapter_factory=lambda: adapter,
    )

    response = asyncio.run(service.query_worklogs(make_request(allowedTeamIds=[])))

    assert response.answer == ""
    assert response.references == []
    assert adapter.calls == []


def test_query_service_does_not_add_all_scope_for_null_allowed_team_ids() -> None:
    adapter = FakeAdapter(
        {
            "llm_response": {"content": "answer"},
            "data": {"references": []},
        }
    )
    service = LightWorklogQueryService(
        settings_obj=Settings(_env_file=None),
        adapter_factory=lambda: adapter,
    )

    asyncio.run(service.query_worklogs(make_request(allowedTeamIds=None)))

    assert len(adapter.calls) == 1
    assert adapter.calls[0].system_prompt is not None
    assert "allowedTeamIds" not in adapter.calls[0].system_prompt
    assert "ALL" not in adapter.calls[0].system_prompt


def test_query_service_rejects_empty_llm_content() -> None:
    adapter = FakeAdapter({"llm_response": {"content": None}, "data": {"references": []}})
    service = LightWorklogQueryService(
        settings_obj=Settings(_env_file=None),
        adapter_factory=lambda: adapter,
    )

    with pytest.raises(LightRagQueryFailedError, match="empty LLM response"):
        asyncio.run(service.query_worklogs(make_request()))


def test_query_service_rejects_missing_llm_response() -> None:
    adapter = FakeAdapter({"data": {"references": []}})
    service = LightWorklogQueryService(
        settings_obj=Settings(_env_file=None),
        adapter_factory=lambda: adapter,
    )

    with pytest.raises(LightRagQueryFailedError, match="invalid LLM response"):
        asyncio.run(service.query_worklogs(make_request()))


def test_query_service_allows_missing_references() -> None:
    adapter = FakeAdapter({"llm_response": {"content": "answer"}})
    service = LightWorklogQueryService(
        settings_obj=Settings(_env_file=None),
        adapter_factory=lambda: adapter,
    )

    response = asyncio.run(service.query_worklogs(make_request()))

    assert response.answer == "answer"
    assert response.references == []


def test_query_service_stream_yields_tokens_and_done() -> None:
    adapter = FakeAdapter({"data": {"references": [{"reference_id": "r1", "file_path": "fp1"}]}})
    service = LightWorklogQueryService(
        settings_obj=Settings(_env_file=None),
        adapter_factory=lambda: adapter,
    )

    async def collect():
        events = []
        async for event in service.query_worklogs_stream(make_request()):
            events.append(event)
        return events

    events = asyncio.run(collect())
    assert len(events) == 3
    assert events[0] == {"event": "token", "data": "tok1"}
    assert events[1] == {"event": "token", "data": "tok2"}
    assert events[2]["event"] == "done"
    assert "r1" in events[2]["data"]


def test_query_service_stream_enforces_timeout() -> None:
    async def hanging_gen():
        yield "fast-token"
        await asyncio.sleep(1.0)
        yield "late-token"

    adapter = FakeAdapter({"data": {"references": []}}, iterator=hanging_gen())
    settings = Settings(_env_file=None, lightrag_query_timeout_seconds=1)
    service = LightWorklogQueryService(
        settings_obj=settings,
        adapter_factory=lambda: adapter,
    )

    async def collect():
        events = []
        async for event in service.query_worklogs_stream(make_request()):
            events.append(event)
        return events

    with pytest.raises(LightRagQueryTimeoutError):
        asyncio.run(collect())


def test_query_service_stream_rejects_missing_iterator() -> None:
    adapter = FakeAdapter({"data": {"references": []}}, iterator=None)
    service = LightWorklogQueryService(
        settings_obj=Settings(_env_file=None),
        adapter_factory=lambda: adapter,
    )

    async def collect():
        events = []
        async for event in service.query_worklogs_stream(make_request()):
            events.append(event)
        return events

    with pytest.raises(LightRagQueryFailedError, match="invalid response iterator"):
        asyncio.run(collect())


def test_query_service_stream_rejects_non_async_iterator() -> None:
    adapter = FakeAdapter({"data": {"references": []}}, iterator=["not", "async"])
    service = LightWorklogQueryService(
        settings_obj=Settings(_env_file=None),
        adapter_factory=lambda: adapter,
    )

    async def collect():
        events = []
        async for event in service.query_worklogs_stream(make_request()):
            events.append(event)
        return events

    with pytest.raises(LightRagQueryFailedError, match="invalid response iterator"):
        asyncio.run(collect())
