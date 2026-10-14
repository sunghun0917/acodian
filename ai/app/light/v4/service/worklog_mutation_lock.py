"""업무일지 mutation의 애플리케이션 잠금 경계."""

import asyncio
from collections.abc import AsyncIterator, Iterable
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any
from weakref import WeakKeyDictionary, WeakValueDictionary

_locks: WeakKeyDictionary[asyncio.AbstractEventLoop, WeakValueDictionary[str, asyncio.Lock]] = WeakKeyDictionary()


@asynccontextmanager
async def worklog_mutation_locks(keys: Iterable[str]) -> AsyncIterator[None]:
    """정렬된 키의 잠금을 공유하고 대기/실행이 끝난 키는 자동 회수한다."""
    # shortcut: 단일 프로세스/이벤트 루프 전용, worker를 늘릴 때 분산 잠금으로 교체한다.
    locks = _locks.setdefault(asyncio.get_running_loop(), WeakValueDictionary())
    async with AsyncExitStack() as stack:
        for key in sorted(set(keys)):
            lock = locks.setdefault(key, asyncio.Lock())
            await stack.enter_async_context(lock)
        yield


def workspace_mutation_key(rag: Any) -> str:
    """native의 내부 GraphDB/pipeline 잠금을 재획득하지 않는 별도 키를 만든다."""
    return f"workspace:{getattr(rag, 'working_dir', '')}:{getattr(rag, 'workspace', '')}"
