"""실제 SDK와 메모리 exporter로 LightRAG 관측 경계를 검증한다."""

from functools import partial
import json
from uuid import uuid4

from google import genai
from google.genai import types
import httpx
from langfuse import Langfuse
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
import pytest

from app.config.settings import Settings


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def tracing(monkeypatch):
    from app.client import langfuse_client

    exporter = InMemorySpanExporter()
    monkeypatch.setattr(langfuse_client, "_client", None)
    monkeypatch.setattr(langfuse_client, "Langfuse", partial(Langfuse, span_exporter=exporter))
    config = Settings(
        _env_file=None,
        langfuse_tracing_enabled=True,
        langfuse_public_key=f"pk-lf-{uuid4()}",
        langfuse_secret_key="sk-lf-test-secret",
        langfuse_base_url="https://cloud.langfuse.com",
        gemini_api_key="test-gemini-secret",
        gemini_api_keys="test-second-gemini-secret",
    )
    yield langfuse_client, config, exporter
    if langfuse_client._client is not None:
        langfuse_client._client.shutdown()
        langfuse_client._client._resources.tracer_provider.shutdown()
    from openinference.instrumentation.google_genai import GoogleGenAIInstrumentor

    if GoogleGenAIInstrumentor().is_instrumented_by_opentelemetry:
        GoogleGenAIInstrumentor().uninstrument()


def gemini_client(handler=None):
    def respond(request):
        payload = {
            "candidates": [{"content": {"role": "model", "parts": [{"text": "dummy answer"}]},
                            "finishReason": "STOP"}],
            "modelVersion": "gemini-2.5-flash",
            "usageMetadata": {"promptTokenCount": 12, "candidatesTokenCount": 3,
                              "totalTokenCount": 15},
        }
        if "streamGenerateContent" in request.url.path:
            return httpx.Response(200, headers={"Content-Type": "text/event-stream"},
                                  content=f"data: {json.dumps(payload)}\n\n")
        return httpx.Response(200, json=payload)

    return genai.Client(
        api_key="test-gemini-secret",
        http_options=types.HttpOptions(
            async_client_args={"transport": httpx.MockTransport(handler or respond)},
            retry_options=types.HttpRetryOptions(attempts=1),
        ),
    )


@pytest.mark.parametrize("enabled,public_key,secret_key", [
    (False, "pk-lf-test", "sk-lf-test"),
    (True, "", "sk-lf-test"),
    (True, "pk-lf-test", ""),
])
def test_unconfigured_tracing_preserves_operation_without_exporting(tracing, enabled, public_key, secret_key):
    module, config, exporter = tracing
    config.langfuse_tracing_enabled = enabled
    config.langfuse_public_key = public_key
    config.langfuse_secret_key = secret_key
    module.initialize_langfuse(config)
    with module.trace_worklog_operation("query-worklogs", input="dummy question") as trace:
        trace["output"] = "dummy answer"
    assert module._client is None
    assert exporter.get_finished_spans() == ()


@pytest.mark.anyio
async def test_gemini_generation_is_nested_with_model_tokens_and_no_credentials(tracing):
    module, config, exporter = tracing
    module.initialize_langfuse(config)
    original = module._client
    module.initialize_langfuse(config)
    assert module._client is original
    client = gemini_client()
    try:
        with module.trace_worklog_operation("query-worklogs", input="dummy question") as trace:
            response = await client.aio.models.generate_content(
                model="gemini-2.5-flash", contents="dummy question",
            )
            trace["output"] = response.text
    finally:
        await client.aio.aclose()
        client.close()
    module.flush_langfuse()
    spans = exporter.get_finished_spans()
    assert len(spans) == 2
    root = next(span for span in spans if span.name == "query-worklogs")
    generation = next(span for span in spans if span.name == "AsyncGenerateContent")
    assert generation.parent.span_id == root.context.span_id
    assert generation.context.trace_id == root.context.trace_id
    assert generation.attributes["llm.model_name"] == "gemini-2.5-flash"
    assert generation.attributes["llm.token_count.prompt"] == 12
    assert generation.attributes["llm.token_count.completion"] == 3
    assert root.attributes["langfuse.observation.input"] == "dummy question"
    assert root.attributes["langfuse.observation.output"] == "dummy answer"
    serialized = "".join(span.to_json() for span in spans)
    assert "test-gemini-secret" not in serialized
    assert "sk-lf-test-secret" not in serialized


@pytest.mark.anyio
async def test_other_ai_calls_are_not_exported(tracing):
    module, config, exporter = tracing
    module.initialize_langfuse(config)
    client = gemini_client()
    try:
        await client.aio.models.generate_content(model="gemini-2.5-flash", contents="outside LightRAG")
    finally:
        await client.aio.aclose()
        client.close()
    module.flush_langfuse()
    assert exporter.get_finished_spans() == ()


def test_error_trace_records_type_without_raw_exception_or_secret(tracing):
    module, config, exporter = tracing
    module.initialize_langfuse(config)
    with pytest.raises(ValueError, match="test-gemini-secret"):
        with module.trace_worklog_operation("query-worklogs", input="dummy question"):
            raise ValueError("upstream leaked test-gemini-secret")
    module.flush_langfuse()
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].attributes["langfuse.observation.level"] == "ERROR"
    assert spans[0].attributes["langfuse.observation.status_message"] == "ValueError"
    assert "test-gemini-secret" not in spans[0].to_json()
    assert not spans[0].events


@pytest.mark.anyio
async def test_provider_exception_with_secret_is_not_exported(tracing):
    module, config, exporter = tracing
    module.initialize_langfuse(config)

    def fail(request):
        return httpx.Response(400, json={"error": {"code": 400, "status": "INVALID_ARGUMENT",
                              "message": "invalid test-second-gemini-secret"}})

    client = gemini_client(fail)
    try:
        with pytest.raises(Exception):
            with module.trace_worklog_operation("query-worklogs", input="dummy question"):
                await client.aio.models.generate_content(model="gemini-2.5-flash", contents="question")
    finally:
        await client.aio.aclose()
        client.close()
    module.flush_langfuse()
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].name == "query-worklogs"
    assert spans[0].attributes["langfuse.observation.level"] == "ERROR"
    assert "test-second-gemini-secret" not in spans[0].to_json()


@pytest.mark.anyio
async def test_embedding_is_nested_without_exporting_vectors(tracing):
    module, config, exporter = tracing
    module.initialize_langfuse(config)

    def embed(request):
        return httpx.Response(200, json={"embeddings": [{"values": [0.25, 0.5, 0.75]}]})

    client = gemini_client(embed)
    try:
        with module.trace_worklog_operation("index-worklogs", input=[123]):
            response = await client.aio.models.embed_content(model="gemini-embedding-001", contents="dummy text")
        assert response.embeddings[0].values == [0.25, 0.5, 0.75]
    finally:
        await client.aio.aclose()
        client.close()
    module.flush_langfuse()
    spans = exporter.get_finished_spans()
    assert len(spans) == 2
    root = next(span for span in spans if span.name == "index-worklogs")
    embedding = next(span for span in spans if span.name != "index-worklogs")
    assert embedding.parent.span_id == root.context.span_id
    assert embedding.attributes["embedding.model_name"] == "gemini-embedding-001"
    assert all(value == "__REDACTED__" for key, value in embedding.attributes.items()
               if key.endswith(".vector"))
    assert embedding.attributes["gen_ai.request.model"] == "gemini-embedding-001"


@pytest.mark.anyio
async def test_gemini_stream_keeps_generation_under_root_until_exhausted(tracing):
    module, config, exporter = tracing
    module.initialize_langfuse(config)

    def respond(request):
        chunks = [
            {"candidates": [{"content": {"role": "model", "parts": [{"text": "first "}]}}]},
            {"candidates": [{"content": {"role": "model", "parts": [{"text": "second"}]},
                             "finishReason": "STOP"}], "modelVersion": "gemini-2.5-flash",
             "usageMetadata": {"promptTokenCount": 12, "candidatesTokenCount": 3, "totalTokenCount": 15}},
        ]
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"},
                              content="".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks))

    client = gemini_client(respond)
    try:
        with module.trace_worklog_operation("stream-worklogs-query", input="dummy question") as trace:
            stream = await client.aio.models.generate_content_stream(model="gemini-2.5-flash", contents="question")
            tokens = [chunk.text async for chunk in stream]
            trace["output"] = "".join(tokens)
    finally:
        await client.aio.aclose()
        client.close()
    module.flush_langfuse()
    spans = exporter.get_finished_spans()
    assert len(spans) == 2
    root = next(span for span in spans if span.name == "stream-worklogs-query")
    generation = next(span for span in spans if span.name != "stream-worklogs-query")
    assert generation.parent.span_id == root.context.span_id
    assert generation.end_time <= root.end_time
    assert generation.attributes["llm.token_count.prompt"] == 12
    assert generation.attributes["llm.token_count.completion"] == 3


def test_telemetry_update_failure_does_not_hide_application_result(tracing, monkeypatch):
    module, config, _ = tracing
    module.initialize_langfuse(config)
    from langfuse._client.span import LangfuseSpan

    def fail(*args, **kwargs):
        raise RuntimeError("telemetry unavailable")

    monkeypatch.setattr(LangfuseSpan, "update", fail)
    with module.trace_worklog_operation("index-worklogs", input=[123]) as trace:
        trace["output"] = {"indexed": [123]}
    with pytest.raises(LookupError):
        with module.trace_worklog_operation("query-worklogs", input="question"):
            raise LookupError("application failure")


def test_secrets_with_quotes_backslashes_and_unicode_are_blocked(tracing):
    module, config, exporter = tracing
    config.langfuse_secret_key = 'sk-lf-quote"backslash\\한글'
    module.initialize_langfuse(config)
    with module.trace_worklog_operation("query-worklogs", input=config.langfuse_secret_key):
        pass
    module.flush_langfuse()
    assert exporter.get_finished_spans() == ()


@pytest.mark.anyio
@pytest.mark.parametrize("stream", [False, True])
async def test_real_lightrag_workers_preserve_sequential_and_concurrent_request_context(tracing, tmp_path, stream):
    import asyncio
    import numpy as np
    from dataclasses import replace
    from types import SimpleNamespace
    from app.light.v4.service.lightrag_adapter import (
        LightRagWorklogIndexAdapter, _load_lightrag_dependencies,
    )

    module, config, exporter = tracing
    module.initialize_langfuse(config)
    client = gemini_client()

    async def complete(prompt, **kwargs):
        if kwargs.get("stream"):
            # LightRAG gemini.py처럼 SDK stream 생성/소비는 문자열 generator 안에서 시작한다.
            async def tokens():
                response = await client.aio.models.generate_content_stream(
                    model="gemini-2.5-flash", contents=prompt,
                )
                async for chunk in response:
                    yield chunk.text
            return tokens()
        response = await client.aio.models.generate_content(model="gemini-2.5-flash", contents=prompt)
        return response.text

    async def embed(texts, **kwargs):
        # 실제 embedding 큐를 통과하되 같은 mock Google 호출로 부모 context만 검증한다.
        await complete(texts[0])
        return np.zeros((len(texts), 768))

    deps = replace(_load_lightrag_dependencies(), gemini_model_complete=complete,
                   gemini_embed=SimpleNamespace(func=embed))
    config.lightrag_working_dir = str(tmp_path)
    rag = LightRagWorklogIndexAdapter(settings_obj=config, dependencies=deps)._build_lightrag()

    async def request(name):
        with module.trace_worklog_operation(name, input=name):
            result = await rag.llm_model_func(name, stream=stream)
            if stream:
                tokens = []
                while True:
                    try:
                        tokens.append(await asyncio.wait_for(anext(result), timeout=5))
                    except StopAsyncIteration:
                        break
                assert "".join(tokens) == "dummy answer"
            await rag.embedding_func([name])

    try:
        await request("request-one")
        await request("request-two")
        await asyncio.gather(request("request-three"), request("request-four"))
        await rag.llm_model_func("outside-LightRAG-request")
    finally:
        await rag.llm_model_func.shutdown()
        await rag.embedding_func.func.shutdown()
        await client.aio.aclose()
        client.close()
    module.flush_langfuse()
    spans = exporter.get_finished_spans()
    roots = {span.name: span for span in spans if span.name.startswith("request-")}
    assert set(roots) == {"request-one", "request-two", "request-three", "request-four"}
    for name, root in roots.items():
        children = [span for span in spans if span.parent and span.parent.span_id == root.context.span_id]
        assert len(children) == 2, name
        assert all(span.context.trace_id == root.context.trace_id for span in children)


def test_initialization_failure_is_nonfatal_and_never_logs_credentials(tracing, monkeypatch, caplog):
    from openinference.instrumentation.google_genai import GoogleGenAIInstrumentor

    module, config, _ = tracing

    def fail(*args, **kwargs):
        raise RuntimeError("could not connect sk-lf-test-secret")

    monkeypatch.setattr(GoogleGenAIInstrumentor, "instrument", fail)
    module.initialize_langfuse(config)
    assert module._client is None
    with module.trace_worklog_operation("query-worklogs", input="question") as trace:
        trace["output"] = "answer"
    assert "sk-lf-test-secret" not in caplog.text


def test_langfuse_settings_load_env_file_without_exposing_secret(tmp_path, monkeypatch):
    for key in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_BASE_URL"):
        monkeypatch.delenv(key, raising=False)
    env = tmp_path / ".env"
    env.write_text("LANGFUSE_PUBLIC_KEY=pk-lf-dummy\nLANGFUSE_SECRET_KEY=sk-lf-hidden\n"
                   "LANGFUSE_BASE_URL=https://us.cloud.langfuse.com\n", encoding="utf-8")
    config = Settings(_env_file=env)
    assert config.langfuse_base_url == "https://us.cloud.langfuse.com"
    assert config.langfuse_secret_key == "sk-lf-hidden"
    assert "sk-lf-hidden" not in repr(config)


@pytest.mark.parametrize("operation", ["index", "reindex"])
def test_index_routes_capture_result_but_never_auth_headers(tracing, monkeypatch, operation):
    from fastapi.testclient import TestClient
    from app.light.v4.model.worklog_index import WorklogLightIndexResponse, WorklogLightIndexItem
    from app.light.v4.router import worklog_index
    from app.main import app

    module, config, exporter = tracing
    module.initialize_langfuse(config)
    monkeypatch.setattr(worklog_index.settings, "ai_internal_token", "private-auth-token")

    async def index(worklog_ids):
        return WorklogLightIndexResponse(items=[WorklogLightIndexItem(worklogId=123, indexed=True)])

    monkeypatch.setattr(worklog_index.worklog_index_service, f"{operation}_worklogs", index)
    response = TestClient(app).post(
        f"/ai/light/worklogs-v4/{operation}", json={"worklogIds": [123]},
        headers={"X-AI-Internal-Token": "private-auth-token"},
    )
    assert response.status_code == 200
    module.flush_langfuse()
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].name == f"{operation}-worklogs"
    assert json.loads(spans[0].attributes["langfuse.observation.output"]) == response.json()
    assert "private-auth-token" not in spans[0].to_json()


@pytest.mark.parametrize("stream", [False, True])
def test_query_routes_capture_complete_answer_and_keep_auth_outside_trace(tracing, monkeypatch, stream):
    from fastapi.testclient import TestClient
    from app.light.v4.model.worklog_query import WorklogLightQueryResponse
    from app.light.v4.router import worklog_query
    from app.main import app

    module, config, exporter = tracing
    module.initialize_langfuse(config)
    monkeypatch.setattr(worklog_query.settings, "ai_internal_token", "private-auth-token")

    async def query(request):
        return WorklogLightQueryResponse(answer="first second", references=[])

    async def query_stream(request):
        yield {"event": "token", "data": "first "}
        yield {"event": "token", "data": "second"}
        yield {"event": "done", "data": '{"references": [], "done": true}'}

    monkeypatch.setattr(worklog_query.worklog_query_service, "query_worklogs", query)
    monkeypatch.setattr(worklog_query.worklog_query_service, "query_worklogs_stream", query_stream)
    url = "/ai/light/worklogs-v4/query" + ("/stream" if stream else "")
    client = TestClient(app)
    denied = client.post(url, json={"query": "dummy question", "allowedTeamIds": [1]})
    assert denied.status_code == 403
    module.flush_langfuse()
    assert exporter.get_finished_spans() == ()
    response = client.post(url, json={"query": "dummy question", "allowedTeamIds": [1]},
                           headers={"X-AI-Internal-Token": "private-auth-token"})
    assert response.status_code == 200
    module.flush_langfuse()
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].name == ("stream-worklogs-query" if stream else "query-worklogs")
    assert spans[0].attributes["langfuse.observation.input"] == "dummy question"
    assert spans[0].attributes["langfuse.observation.output"] == "first second"
    assert "private-auth-token" not in spans[0].to_json()
