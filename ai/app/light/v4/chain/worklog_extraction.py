"""Native LightRAG 추출 문자열의 검증과 제한 재시도."""

from collections.abc import Awaitable, Callable
import logging
from typing import Any

from app.light.v4.prompt.worklog_extraction_prompt import (
    EXTRACTION_FORMAT_RETRY, NATIVE_EXTRACTION_ROLE,
)
from app.light.v4.store.worklog_extraction_cache import is_valid_native_extraction

logger = logging.getLogger(__name__)


def is_body_extraction(system_prompt: str | None, keyword_extraction: bool) -> bool:
    """고정 native 시스템 역할로만 본문 추출을 식별한다."""
    return not keyword_extraction and bool(
        system_prompt and system_prompt.startswith(NATIVE_EXTRACTION_ROLE)
    )


async def complete_body_extraction(
    complete: Callable[..., Awaitable[str]],
    prompt: str,
    *,
    system_prompt: str,
    history_messages: list[dict[str, Any]] | None,
    **kwargs: Any,
) -> str:
    """Native 프롬프트·이력을 유지하며 정상 형식만 반환한다. 최초 포함 최대 3회."""
    for attempt in range(3):
        result = await complete(
            prompt if attempt == 0 else prompt + EXTRACTION_FORMAT_RETRY,
            system_prompt=system_prompt,
            history_messages=history_messages or [],
            **kwargs,
        )
        if is_valid_native_extraction(result):
            return result.strip()
        logger.warning("Body extraction validation rejected response attempt=%s/3", attempt + 1)
    raise ValueError("Body extraction validation failed after 3 attempts")
