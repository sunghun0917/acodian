"""색인 요청 종류와 무관하게 팀 사본을 최신 업무 DB 상태로 맞춘다."""

import logging
from contextlib import asynccontextmanager

from redis.asyncio import Redis

from app.config.settings import Settings, settings
from app.light.v4.model.worklog_index import (
    WorklogLightDeleteExecutionItem, WorklogLightDeleteExecutionResponse,
    WorklogLightIndexExecutionItem, WorklogLightIndexExecutionResponse,
)
from app.light.v4.service.lightrag_adapter import (
    LightRagConfigurationError, LightRagDeleteFailedError, LightRagDeleteTimeoutError,
    LightRagInsertTimeoutError,
)
from app.light.v4.service.worklog_query_cache import WorklogQueryCache
from app.light.v4.service.worklog_source_row_reader import WorklogLightSourceRowReader
from app.light.v4.service.worklog_team_copy_apply import CopyBrokenError
from app.light.v4.service.worklog_team_copy_lock import TeamCopyLocks
from app.light.v4.service.worklog_team_copy_writer import TeamCopyWriter
from app.light.v4.service.worklog_team_scope import get_lightrag_team_scope_adapter
from app.light.v4.store.worklog_team_copy_store import get_worklog_team_copy_store


ERROR_WORKLOG_NOT_FOUND = "WORKLOG_NOT_FOUND"
ERROR_LIGHTRAG_INSERT_TIMEOUT = "LIGHTRAG_INSERT_TIMEOUT"
ERROR_LIGHTRAG_INSERT_FAILED = "LIGHTRAG_INSERT_FAILED"
ERROR_LIGHTRAG_DELETE_TIMEOUT = "LIGHTRAG_DELETE_TIMEOUT"
ERROR_LIGHTRAG_DELETE_FAILED = "LIGHTRAG_DELETE_FAILED"
ERROR_LIGHTRAG_DELETE_FAILED_COMPENSATED = "LIGHTRAG_DELETE_FAILED_COMPENSATED"
ERROR_LIGHTRAG_COMPENSATION_FAILED = "LIGHTRAG_COMPENSATION_FAILED"

logger = logging.getLogger(__name__)


class LightWorklogIndexService:
    """요청별 Redis 연결 수명과 업무일지별 실행 응답을 관리한다."""

    def __init__(
        self, *, cache: WorklogQueryCache | None = None, settings_obj: Settings = settings,
        writer: TeamCopyWriter | None = None,
    ) -> None:
        self._cache = cache
        self._settings = settings_obj
        self._writer = writer

    @asynccontextmanager
    async def _writer_scope(self):
        """Celery 작업의 이벤트 루프 밖으로 Redis 연결을 재사용하지 않는다."""
        if self._writer is not None:
            yield self._writer
            return
        client = Redis.from_url(self._settings.redis_url)
        try:
            yield TeamCopyWriter(
                pool=get_lightrag_team_scope_adapter(), store=get_worklog_team_copy_store(),
                locks=TeamCopyLocks(settings_obj=self._settings, client=client),
                source_reader=WorklogLightSourceRowReader(), cache=self._cache,
                settings_obj=self._settings,
            )
        finally:
            await client.aclose()

    async def index_worklogs(
        self, worklog_ids: list[int], *, already_mutated: bool = False,
    ) -> WorklogLightIndexExecutionResponse:
        """삭제된 원본도 삭제 수렴을 마치면 indexed=True로 성공을 반환한다."""
        errors = await self._sync_worklogs(worklog_ids)
        # 소스가 없어 삭제로 수렴해도 요청 실행은 성공이다.
        return WorklogLightIndexExecutionResponse(items=[
            WorklogLightIndexExecutionItem(worklogId=worklog_id, indexed=error is None, error=error)
            for worklog_id, error in zip(worklog_ids, errors, strict=True)
        ])

    async def reindex_worklogs(self, worklog_ids: list[int]) -> WorklogLightIndexExecutionResponse:
        """재색인도 요청 당시 내용 대신 최신 업무 DB 상태에 수렴한다."""
        return await self.index_worklogs(worklog_ids)

    async def delete_worklogs(self, worklog_ids: list[int]) -> WorklogLightDeleteExecutionResponse:
        """늦게 도착한 삭제 요청도 현재 업무 DB 상태로 맞춘다."""
        errors = await self._sync_worklogs(worklog_ids, deleting=True)
        return WorklogLightDeleteExecutionResponse(items=[
            WorklogLightDeleteExecutionItem(worklogId=worklog_id, deleted=error is None, error=error)
            for worklog_id, error in zip(worklog_ids, errors, strict=True)
        ])

    async def _sync_worklogs(self, worklog_ids: list[int], *, deleting: bool = False) -> list[str | None]:
        """설정 오류와 취소는 전파하고 나머지 실패는 해당 항목에만 담는다."""
        errors: list[str | None] = []
        if not worklog_ids:
            return errors
        async with self._writer_scope() as writer:
            for worklog_id in worklog_ids:
                try:
                    await writer.sync_worklog(worklog_id)
                except LightRagConfigurationError:
                    raise
                except Exception as exc:
                    logger.exception("LightRAG worklog sync failed: worklog=%s", worklog_id)
                    if isinstance(exc, LightRagDeleteTimeoutError):
                        error = ERROR_LIGHTRAG_DELETE_TIMEOUT
                    elif isinstance(exc, (TimeoutError, LightRagInsertTimeoutError)):
                        error = ERROR_LIGHTRAG_DELETE_TIMEOUT if deleting else ERROR_LIGHTRAG_INSERT_TIMEOUT
                    elif isinstance(exc, CopyBrokenError):
                        error = ERROR_LIGHTRAG_COMPENSATION_FAILED
                    elif deleting or isinstance(exc, LightRagDeleteFailedError):
                        error = ERROR_LIGHTRAG_DELETE_FAILED
                    else:
                        error = ERROR_LIGHTRAG_INSERT_FAILED
                    errors.append(error)
                else:
                    errors.append(None)
        return errors
