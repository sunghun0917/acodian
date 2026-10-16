"""LightRAG v4 요청에 한정한 Langfuse/Gemini 관측 진입점."""

import asyncio
from collections.abc import Awaitable, Callable, Coroutine, Iterator
from contextlib import ExitStack, contextmanager
from contextvars import copy_context
from functools import wraps
import json
import logging
from typing import Any

from langfuse import Langfuse, propagate_attributes
from openinference.instrumentation import TraceConfig
from openinference.instrumentation.google_genai import GoogleGenAIInstrumentor
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider

from app.config.settings import Settings, settings

logger = logging.getLogger(__name__)
_client: Langfuse | None = None


def restore_lightrag_context(
    func: Callable[..., Coroutine[Any, Any, Any]],
) -> Callable[..., Coroutine[Any, Any, Any]]:
    """LightRAG 영속 worker 안에서 큐에 들어온 요청의 context로 모델을 실행한다."""
    @wraps(func)
    async def execute(*args: Any, **kwargs: Any) -> Any:
        context = kwargs.pop("_ax_wms_trace_context", None)
        if context is None:
            return await func(*args, **kwargs)
        return await asyncio.create_task(func(*args, **kwargs), context=context)

    return execute


def _capture_lightrag_context(func: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
    """큐 진입 시 context를 캡처하고 원래 wrapper의 shutdown 속성도 보존한다."""
    @wraps(func)
    async def enqueue(*args: Any, **kwargs: Any) -> Any:
        return await func(*args, _ax_wms_trace_context=copy_context(), **kwargs)

    return enqueue


def configure_lightrag_tracing(rag: Any) -> None:
    """관측이 켜진 LightRAG 1.4.10 인스턴스의 LLM·임베딩 큐 경계만 보정한다."""
    if _client is not None:
        rag.llm_model_func = _capture_lightrag_context(rag.llm_model_func)
        rag.embedding_func.func = _capture_lightrag_context(rag.embedding_func.func)


def initialize_langfuse(settings_obj: Settings = settings) -> None:
    """키가 설정된 프로세스에서만 SDK와 Gemini 계측을 한 번 초기화한다."""
    global _client
    if _client is not None or not settings_obj.langfuse_tracing_enabled:
        return
    if not settings_obj.langfuse_public_key or not settings_obj.langfuse_secret_key:
        return

    secrets = tuple(value for value in (
        *settings_obj.gemini_api_key_candidates,
        settings_obj.langfuse_secret_key,
        settings_obj.ai_internal_token,
        settings_obj.aws_access_key_id,
        settings_obj.aws_secret_access_key,
        settings_obj.lightrag_rerank_api_key,
        settings_obj.lightrag_qdrant_api_key,
        settings_obj.neo4j_password,
        settings_obj.pgvector_dsn,
        settings_obj.redis_url,
    ) if value)
    provider = TracerProvider()
    client = None
    try:
        client = Langfuse(
            public_key=settings_obj.langfuse_public_key,
            secret_key=settings_obj.langfuse_secret_key,
            base_url=settings_obj.langfuse_base_url,
            environment=settings_obj.environment,
            tracer_provider=provider,
            should_export_span=lambda span: _should_export_span(span, secrets),
        )
        GoogleGenAIInstrumentor().instrument(
            tracer_provider=provider,
            config=TraceConfig(hide_embedding_vectors=True, enable_genai_semconv=True),
        )
        _client = client
    except Exception as exc:
        logger.warning("Langfuse initialization skipped (%s)", type(exc).__name__)
        if client is not None:
            client.shutdown()
        provider.shutdown()


def _should_export_span(span: ReadableSpan, secrets: tuple[str, ...]) -> bool:
    """다른 AI 기능과 비밀값을 포함한 span은 예외 event/status까지 검사해 차단한다."""
    tags = (span.attributes or {}).get("langfuse.trace.tags", ())
    if not isinstance(tags, (list, tuple)) or "lightrag-v4" not in tags:
        return False
    values = [json.loads(span.to_json())]
    while values:
        value = values.pop()
        if isinstance(value, dict):
            values.extend(value.keys())
            values.extend(value.values())
        elif isinstance(value, (list, tuple)):
            values.extend(value)
        elif isinstance(value, str):
            if any(secret in value for secret in secrets):
                return False
            try:
                decoded = json.loads(value)
            except ValueError:
                continue
            if isinstance(decoded, (dict, list, str)) and decoded != value:
                values.append(decoded)
    return True


@contextmanager
def trace_worklog_operation(
    name: str, *, input: Any, metadata: dict[str, Any] | None = None,
) -> Iterator[dict[str, Any]]:
    """명시적 입출력만 기록하고 관측 장애가 업무 처리 결과를 바꾸지 않게 한다."""
    result: dict[str, Any] = {}
    stack = ExitStack()
    observation = None
    if _client is not None:
        try:
            stack.enter_context(propagate_attributes(tags=["lightrag-v4"]))
            observation = stack.enter_context(_client.start_as_current_observation(
                name=name, input=input, metadata=metadata,
            ))
        except Exception as exc:
            logger.warning("Langfuse observation skipped (%s)", type(exc).__name__)
    try:
        yield result
    except BaseException as exc:
        result.update(level="ERROR", status_message=type(exc).__name__)
        raise
    finally:
        try:
            if observation is not None:
                observation.update(**result)
        except Exception as exc:
            logger.warning("Langfuse observation update skipped (%s)", type(exc).__name__)
        finally:
            try:
                # close()에 업무 예외를 넘기지 않아 원문 예외/자격증명이 자동 수집되지 않는다.
                stack.close()
            except Exception as exc:
                logger.warning("Langfuse observation close skipped (%s)", type(exc).__name__)


def flush_langfuse() -> None:
    """앱 종료 시 전송을 마치고 프로세스 자원 해제는 SDK의 atexit에 맡긴다."""
    if _client is not None:
        try:
            _client.flush()
        except Exception as exc:
            logger.warning("Langfuse flush skipped (%s)", type(exc).__name__)
