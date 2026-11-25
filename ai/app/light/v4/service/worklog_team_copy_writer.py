"""팀 쓰기 사본을 복구하고 최신 업무 DB 상태를 조건부 게시한다."""

import logging

from app.config.settings import Settings, settings
from app.light.v4.service.worklog_query_cache import WorklogQueryCache
from app.light.v4.service.worklog_team_copy_apply import CopyBrokenError, apply_to_copy, rebuild_copy
from app.light.v4.service.worklog_team_copy_lock import LostWriteLockError
from app.light.v4.store.worklog_team_copy_store import other_copy, snapshot_fingerprint


logger = logging.getLogger(__name__)


class TeamCopyWriter:
    """한 번에 팀 하나만 잠그고 읽기 사본을 보존하는 쓰기 흐름."""

    def __init__(
        self, *, pool, store, locks, source_reader,
        cache: WorklogQueryCache | None, settings_obj: Settings = settings,
    ) -> None:
        self._pool = pool
        self._store = store
        self._locks = locks
        self._source_reader = source_reader
        self._cache = cache
        self._settings = settings_obj

    async def sync_worklog(self, worklog_id: int) -> None:
        """현재 팀을 먼저 맞추고 과거 사본에 남은 다른 팀을 순서대로 정리한다."""
        current = (await self._source_reader.fetch_source_rows([worklog_id])).get(worklog_id)
        teams = await self._store.teams_containing(worklog_id)
        ordered = sorted(teams - {current.team_id}) if current else sorted(teams)
        if current:
            ordered.insert(0, current.team_id)
        for team_id in ordered:
            await self._sync_team(team_id, worklog_id)

    async def _sync_team(self, team_id: int, worklog_id: int) -> None:
        """독자가 빠진 쓰기 사본만 복구하고 락 안에서 다시 읽은 원본을 반영한다."""
        async with self._locks.team_write_lock(team_id) as lease:
            read = await self._store.read_copy(team_id)
            write = other_copy(read)
            await self._locks.wait_readers_drained(
                team_id, write, timeout=self._settings.worklog_light_index_timeout_seconds / 2,
            )
            if await self._store.broken_copy(team_id) == write:
                entries = await self._store.entries(team_id, read)
                await rebuild_copy(
                    self._pool, self._store, lease=lease, team_id=team_id, copy=write,
                    rows=[entry.snapshot for entry in entries.values() if entry.state == "READY"],
                )
            else:
                for differing_id in await self._store.differing_worklog_ids(team_id, read=read):
                    entry = (await self._store.entries(team_id, read, [differing_id])).get(differing_id)
                    await apply_to_copy(
                        self._pool, self._store, lease=lease, team_id=team_id, copy=write,
                        worklog_id=differing_id,
                        desired=entry.snapshot if entry and entry.state == "READY" else None,
                    )

            await lease.ensure_owned()
            current = (await self._source_reader.fetch_source_rows([worklog_id])).get(worklog_id)
            desired = current if current and current.team_id == team_id else None
            have = (await self._store.entries(team_id, read, [worklog_id])).get(worklog_id)
            have_fp = have.fingerprint if have and have.state == "READY" else ("<working>" if have else None)
            if snapshot_fingerprint(desired) == have_fp:
                return
            try:
                await apply_to_copy(
                    self._pool, self._store, lease=lease, team_id=team_id, copy=write,
                    worklog_id=worklog_id, desired=desired,
                )
            except (CopyBrokenError, LostWriteLockError):
                raise
            except Exception:
                try:
                    await apply_to_copy(
                        self._pool, self._store, lease=lease, team_id=team_id, copy=write,
                        worklog_id=worklog_id,
                        desired=have.snapshot if have and have.state == "READY" else None,
                    )
                except Exception:
                    logger.exception("Team copy compensation failed: team=%s copy=%s worklog=%s",
                                     team_id, write, worklog_id)
                raise
            await lease.ensure_owned()
            if not await self._store.flip_read_copy(team_id, expected=read, new=write):
                raise LostWriteLockError("Team read copy changed before publication")
            if self._cache is not None:
                await self._cache.bump_version()
