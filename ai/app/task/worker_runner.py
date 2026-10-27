"""Celery worker 프로세스 수명 주기 비동기 실행기."""

import asyncio
from collections.abc import Coroutine
from contextvars import copy_context
import logging
from typing import Any, TypeVar

from celery.signals import worker_process_shutdown

logger = logging.getLogger(__name__)

T = TypeVar("T")


class WorkerAsyncRunner:
    """단일 Celery worker 프로세스 내에서 지속되는 asyncio.Runner를 관리한다.

    각 태스크 실행마다 contextvars를 copy_context()로 전달해 요청 간 관측 상태 및
    trace 컨텍스트 누수를 차단하며, 잔존 미완료 태스크를 격리 정리한다.
    """

    def __init__(self) -> None:
        self._runner: asyncio.Runner | None = None

    def get_runner(self) -> asyncio.Runner:
        if self._runner is None:
            self._runner = asyncio.Runner()
        return self._runner

    def run(self, coro: Coroutine[Any, Any, T]) -> T:
        runner = self.get_runner()
        ctx = copy_context()

        async def _isolated() -> T:
            try:
                return await coro
            finally:
                loop = asyncio.get_running_loop()
                current = asyncio.current_task()
                pending = [t for t in asyncio.all_tasks(loop) if t is not current and not t.done()]
                if pending:
                    for t in pending:
                        t.cancel()
                    await asyncio.gather(*pending, return_exceptions=True)

        return runner.run(_isolated(), context=ctx)

    def close(self) -> None:
        """이벤트 루프와 관련 리소스를 정리한다."""
        if self._runner is not None:
            try:
                self._runner.close()
            except Exception as exc:
                logger.warning("Error closing WorkerAsyncRunner: %s", exc)
            finally:
                self._runner = None


worker_runner = WorkerAsyncRunner()


@worker_process_shutdown.connect
def shutdown_worker_runner(**kwargs: Any) -> None:
    """Celery worker 프로세스 종료 시 이벤트 루프를 닫는다."""
    worker_runner.close()
