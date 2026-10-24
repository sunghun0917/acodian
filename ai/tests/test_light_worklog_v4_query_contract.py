from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from app.config.settings import Settings
from app.light.v4.model.worklog_query import WorklogLightQueryRequest
from app.light.v4.service.lightrag_adapter import (
    LightRagQueryFailedError, LightRagQueryOptions, LightRagQueryTimeoutError,
)
from app.light.v4.service.worklog_query_cache import WorklogQueryCache
from app.light.v4.service.worklog_query_service import LightWorklogQueryService
from tests.test_light_worklog_v4_query_service import fake_indexed_rows


_DEFAULT_ITERATOR = object()


class FakeAdapter:
    def __init__(self, *, answer: Any = " answer ", iterator: Any = _DEFAULT_ITERATOR,
                 data: dict[str, Any] | None = None) -> None:
        self.answer = answer
        self.iterator = iterator
        self.data = data if data is not None else {
            "status": "success", "data": {"chunks": [
                {"file_path": "worklog://1"}, {"file_path": "worklog://2"},
            ]},
        }
        self.calls: list[LightRagQueryOptions] = []
        self.prompts: list[str] = []

    async def query_data(self, options: LightRagQueryOptions) -> dict[str, Any]:
        self.calls.append(options)
        return self.data

    async def read_source_rows(self, ids, scope):
        return await fake_indexed_rows(ids)

    async def complete_scoped(self, query, *, system_prompt, stream=False):
        self.prompts.append(system_prompt)
        if not stream:
            return self.answer
        if self.iterator is not _DEFAULT_ITERATOR:
            return self.iterator
        async def tokens():
            yield "tok1"
            yield "tok2"
        return tokens()


def make_request(**overrides: Any) -> WorklogLightQueryRequest:
    return WorklogLightQueryRequest.model_validate(
        {"query": "업무일지 요약", "allowedTeamIds": None, **overrides},
    )


def service(adapter, settings=None):
    return LightWorklogQueryService(
        settings_obj=settings or Settings(_env_file=None), adapter_factory=lambda: adapter,
        team_adapter_factory=lambda: adapter,
    )


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("reason", ["storage_error", None])
def test_retrieval_failure_is_not_an_empty_success(stream, reason):
    adapter = FakeAdapter(data={"status": "failure", "data": {},
                                "metadata": {"failure_reason": reason}})

    async def run():
        with pytest.raises(LightRagQueryFailedError):
            if stream:
                _ = [event async for event in service(adapter).query_worklogs_stream(make_request())]
            else:
                await service(adapter).query_worklogs(make_request())

    asyncio.run(run())
    assert adapter.prompts == []


@pytest.mark.parametrize("stream", [False, True])
def test_no_results_remains_an_empty_success(stream):
    adapter = FakeAdapter(data={"status": "failure", "data": {},
                                "metadata": {"failure_reason": "no_results"}})

    async def run():
        if stream:
            events = [event async for event in service(adapter).query_worklogs_stream(make_request())]
            assert events == [{"event": "done", "data": json.dumps({"references": [], "done": True})}]
        else:
            response = await service(adapter).query_worklogs(make_request())
            assert response.answer == ""
            assert response.references == []

    asyncio.run(run())
    assert adapter.prompts == []


def test_query_service_applies_settings_defaults_and_uses_verified_references():
    adapter = FakeAdapter()
    settings = Settings(_env_file=None, lightrag_query_top_k=11,
                        lightrag_query_chunk_top_k=5,
                        lightrag_query_response_type="Multiple Paragraphs")
    response = asyncio.run(service(adapter, settings).query_worklogs(make_request()))
    assert len(adapter.calls) == 1
    call = adapter.calls[0]
    assert call.query == "업무일지 요약"
    assert call.top_k == 11
    assert call.chunk_top_k == 5
    assert call.response_type == "Multiple Paragraphs"
    assert call.enable_rerank is False
    assert "allowedTeamIds" not in adapter.prompts[0]
    assert "Access Scope" not in adapter.prompts[0]
    assert response.answer == "answer"
    assert [(item.reference_id, item.file_path) for item in response.references] == [
        ("worklog-1", "worklog://1"), ("worklog-2", "worklog://2"),
    ]
    assert response.internal_only is True


@pytest.mark.parametrize("stream", [False, True])
def test_queries_never_read_or_write_response_cache(stream, monkeypatch):
    cache_calls = []
    async def unexpected(*args, **kwargs):
        cache_calls.append(True)
        raise AssertionError("Response cache must stay disabled")
    for name in ("get_version", "get_exact", "get_semantic", "put"):
        monkeypatch.setattr(WorklogQueryCache, name, unexpected)
    adapter = FakeAdapter()
    query_service = service(adapter)
    async def run():
        for _ in range(2):
            if stream:
                events = [event async for event in query_service.query_worklogs_stream(make_request())]
                assert events[-1]["event"] == "done"
            else:
                assert (await query_service.query_worklogs(make_request())).answer == "answer"
    asyncio.run(run())
    assert len(adapter.calls) == 2
    assert len(adapter.prompts) == 2
    assert cache_calls == []


def test_query_service_enables_rerank_per_query():
    adapter = FakeAdapter()
    asyncio.run(service(adapter).query_worklogs(make_request(enableRerank=True)))
    assert adapter.calls[0].enable_rerank is True


def test_query_service_passes_rerank_model_override():
    adapter = FakeAdapter()
    asyncio.run(service(adapter).query_worklogs(
        make_request(enableRerank=True, rerankModel="BAAI/custom-reranker"),
    ))
    assert adapter.calls[0].rerank_model == "BAAI/custom-reranker"


def test_query_service_empty_allowed_team_ids_skips_adapter():
    def unexpected():
        raise AssertionError("Empty scope must not initialize adapters")
    query_service = LightWorklogQueryService(adapter_factory=unexpected,
                                            team_adapter_factory=unexpected)
    response = asyncio.run(query_service.query_worklogs(make_request(allowedTeamIds=[])))
    assert response.answer == ""
    assert response.references == []


@pytest.mark.parametrize("content", [None, "", "   ", 42])
def test_query_service_rejects_empty_or_invalid_llm_content(content):
    with pytest.raises(LightRagQueryFailedError, match="empty LLM response"):
        asyncio.run(service(FakeAdapter(answer=content)).query_worklogs(make_request()))


@pytest.mark.parametrize("stream", [False, True])
def test_query_service_returns_empty_without_candidates(stream):
    adapter = FakeAdapter(data={"status": "success", "data": {}})
    query_service = service(adapter)
    async def run():
        if stream:
            events = [event async for event in query_service.query_worklogs_stream(make_request())]
            assert events == [{"event": "done", "data": json.dumps({"references": [], "done": True})}]
        else:
            response = await query_service.query_worklogs(make_request())
            assert response.answer == ""
            assert response.references == []
    asyncio.run(run())
    assert adapter.prompts == []


def test_query_service_stream_yields_tokens_and_verified_done_references():
    adapter = FakeAdapter()
    async def collect():
        return [event async for event in service(adapter).query_worklogs_stream(make_request())]
    events = asyncio.run(collect())
    assert events[:2] == [{"event": "token", "data": "tok1"}, {"event": "token", "data": "tok2"}]
    assert json.loads(events[-1]["data"]) == {"references": [
        {"referenceId": "worklog-1", "filePath": "worklog://1"},
        {"referenceId": "worklog-2", "filePath": "worklog://2"},
    ], "done": True}


def test_query_service_stream_enforces_token_timeout():
    async def hanging_gen():
        yield "fast-token"
        await asyncio.sleep(1.0)
        yield "late-token"
    adapter = FakeAdapter(iterator=hanging_gen())
    settings = Settings(_env_file=None, lightrag_query_timeout_seconds=1)
    async def collect():
        return [event async for event in service(adapter, settings).query_worklogs_stream(make_request())]
    with pytest.raises(LightRagQueryTimeoutError):
        asyncio.run(collect())


@pytest.mark.parametrize("stage", ["retrieve", "database", "generate"])
def test_stream_deadline_includes_context_lookup_and_generation(stage):
    class SlowAdapter(FakeAdapter):
        async def query_data(self, options):
            if stage == "retrieve":
                await asyncio.sleep(0.05)
            return await super().query_data(options)

        async def read_source_rows(self, ids, scope):
            if stage == "database":
                await asyncio.sleep(0.05)
            return await super().read_source_rows(ids, scope)

        async def complete_scoped(self, query, *, system_prompt, stream=False):
            if stage == "generate":
                await asyncio.sleep(0.05)
            return await super().complete_scoped(query, system_prompt=system_prompt, stream=stream)

    settings = Settings(_env_file=None).model_copy(update={"lightrag_query_timeout_seconds": 0.01})
    async def collect():
        return [event async for event in service(SlowAdapter(), settings).query_worklogs_stream(make_request())]
    with pytest.raises(LightRagQueryTimeoutError, match="context or generation timed out"):
        asyncio.run(collect())


def test_empty_stream_does_not_leave_timeout_active_in_consumer():
    adapter = FakeAdapter(data={"status": "success", "data": {}})
    settings = Settings(_env_file=None).model_copy(update={"lightrag_query_timeout_seconds": 0.01})
    async def consume():
        iterator = service(adapter, settings).query_worklogs_stream(make_request())
        assert (await anext(iterator))["event"] == "done"
        await asyncio.sleep(0.03)
        with pytest.raises(StopAsyncIteration):
            await anext(iterator)
    asyncio.run(consume())


@pytest.mark.parametrize("iterator", [None, ["not", "async"]])
def test_query_service_stream_rejects_invalid_iterator(iterator):
    adapter = FakeAdapter(iterator=iterator)
    async def collect():
        return [event async for event in service(adapter).query_worklogs_stream(make_request())]
    with pytest.raises(LightRagQueryFailedError, match="invalid response iterator"):
        asyncio.run(collect())
