import asyncio
import copy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from tests.test_lightrag_v4_adapter import FakeLightRAG, make_dependencies, make_settings
from app.light.v4.service.lightrag_adapter import (
    LightRagWorklogIndexAdapter, LightRagQueryOptions,
    LightRagProviderUnavailableError, _build_gemini_llm_model_func,
)


def settings_with_answer():
    return make_settings().model_copy(update={
        "lightrag_llm_model": "gemini-3.5-flash-lite",
        "lightrag_answer_llm_model": "gemini-3.6-flash",
        "lightrag_query_llm_max_retries_per_model": 0,
        "lightrag_query_llm_retry_initial_delay_seconds": 0,
    })


def test_answer_and_keyword_routing_preserves_extraction_model():
    settings = settings_with_answer()
    deps, _, _, calls = make_dependencies()
    fn = _build_gemini_llm_model_func(settings_obj=settings, dependencies=deps)
    global_cache = SimpleNamespace(global_config={"llm_model_name": settings.lightrag_llm_model})

    async def run():
        await fn("answer", answer_generation=True, hashing_kv=global_cache)
        await fn("keywords", answer_generation=True, keyword_extraction=True, hashing_kv=global_cache)
        await fn("summary", hashing_kv=global_cache)

    asyncio.run(run())
    assert [c["model_name"] for c in calls] == [
        "gemini-3.6-flash", "gemini-3.5-flash-lite", "gemini-3.5-flash-lite",
    ]
    assert "hashing_kv" not in calls[0]
    assert calls[1]["hashing_kv"] is global_cache


@pytest.mark.parametrize("stream", [False, True])
def test_scoped_answer_routes_both_normal_and_stream_to_answer_model(stream):
    deps, _, _, calls = make_dependencies()
    fn = _build_gemini_llm_model_func(settings_obj=settings_with_answer(), dependencies=deps)
    adapter = LightRagWorklogIndexAdapter(settings_obj=settings_with_answer(), dependencies=deps)

    async def initialized():
        return SimpleNamespace(llm_model_func=fn)

    adapter._get_initialized_rag = initialized
    asyncio.run(adapter.complete_scoped("question", system_prompt="authorized context", stream=stream))
    assert calls[0]["model_name"] == "gemini-3.6-flash"
    assert calls[0]["stream"] is stream
    assert calls[0]["system_prompt"] == "authorized context"


def test_native_query_override_uses_answer_model_but_keywords_stay_lightweight():
    deps, _, _, calls = make_dependencies()
    fn = _build_gemini_llm_model_func(settings_obj=settings_with_answer(), dependencies=deps)
    adapter = LightRagWorklogIndexAdapter(settings_obj=settings_with_answer(), dependencies=deps)

    async def initialized():
        return SimpleNamespace(llm_model_func=fn)

    adapter._get_initialized_rag = initialized
    param = adapter._build_query_param(LightRagQueryOptions(
        query="question", top_k=10, chunk_top_k=5, response_type="Multiple Paragraphs",
    ))
    assert getattr(param, "model_func", None) is not None
    assert copy.deepcopy(param).model_func is param.model_func

    async def run():
        await param.model_func("answer")
        await param.model_func("keywords", keyword_extraction=True)

    asyncio.run(run())
    assert [c["model_name"] for c in calls] == ["gemini-3.6-flash", "gemini-3.5-flash-lite"]


def test_answer_provider_failure_does_not_silently_use_extraction_fallback():
    settings = settings_with_answer().model_copy(update={"lightrag_llm_fallback_models": "extraction-fallback"})
    deps, _, _, _ = make_dependencies()
    attempted = []

    async def unavailable(prompt, **kwargs):
        attempted.append(kwargs["model_name"])
        error = RuntimeError("503 Service Unavailable")
        error.status_code = 503
        raise error

    fn = _build_gemini_llm_model_func(settings_obj=settings,
        dependencies=replace(deps, gemini_model_complete=unavailable))
    with pytest.raises(LightRagProviderUnavailableError):
        asyncio.run(fn("answer", answer_generation=True))
    assert attempted == ["gemini-3.6-flash"]



