"""대기 요청이 없고 색인 워커가 정지된 상태에서 팀 사본 두 벌을 재구축한다."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import AsyncIterator
import json
import sys

from redis.asyncio import Redis
from sqlalchemy import select

from app.config.settings import settings
from app.light.v4.service.worklog_query_cache import WorklogQueryCache
from app.light.v4.service.worklog_source_row_reader import WorklogLightSourceRowReader
from app.light.v4.service.worklog_team_copy_apply import rebuild_copy
from app.light.v4.service.worklog_team_copy_lock import LostWriteLockError, TeamCopyLocks
from app.light.v4.service.worklog_team_scope import LightRagTeamScopeAdapter
from app.light.v4.store.worklog_source_store import LightWorklogSourceRow
from app.light.v4.store.worklog_team_copy_store import TeamCopy, WorklogTeamCopyStore, other_copy
from app.store.models import Team
from app.store.session import get_engine, get_session_factory


async def _source_batches(reader) -> AsyncIterator[dict[int, LightWorklogSourceRow]]:
    """생존 원본을 페이지로 읽고 ID가 다른 행의 반영을 거부한다."""
    after_id = 0
    batch_size = settings.lightrag_index_max_batch_size
    while ids := await reader.fetch_source_ids(after_id=after_id, limit=batch_size):
        if (not isinstance(ids, list) or len(ids) > batch_size
                or any(type(key) is not int or key <= after_id for key in ids)
                or ids != sorted(set(ids))):
            raise ValueError("Invalid source ID page")
        rows = await reader.fetch_source_rows(ids)
        if any(key not in ids or row.worklog_id != key for key, row in rows.items()):
            raise ValueError("Source identity mismatch")
        yield rows
        after_id = ids[-1]


async def rebuild_team(*, team_id: int, reader, pool, store, locks) -> None:
    """팀 락 안에서 최신 원본을 게시하고 반대 사본도 게시된 스냅샷으로 맞춘다."""
    if type(team_id) is not int or team_id <= 0:
        raise ValueError("Team ID must be a positive integer")
    async with locks.team_write_lock(team_id) as lease:
        read = await store.read_copy(team_id)
        write = other_copy(read)
        timeout = settings.worklog_light_index_timeout_seconds / 2
        await locks.wait_readers_drained(team_id, write, timeout=timeout)
        # shortcut: 팀마다 전체 ID 스캔, 업무일지가 많아지면 팀별 조회 메서드 추가
        rows = []
        async for batch in _source_batches(reader):
            rows.extend(row for row in batch.values() if row.team_id == team_id)
        await rebuild_copy(pool, store, lease=lease, team_id=team_id, copy=write, rows=rows)
        await lease.ensure_owned()
        if not await store.flip_read_copy(team_id, expected=read, new=write, mark_other_broken=True):
            raise LostWriteLockError("Team read copy changed before rebuild publication")
        await locks.wait_readers_drained(team_id, read, timeout=timeout)
        entries = await store.entries(team_id, write)
        await rebuild_copy(
            pool, store, lease=lease, team_id=team_id, copy=read,
            rows=[entry.snapshot for entry in entries.values() if entry.state == "READY"],
        )


async def rebuild_all(*, reader, outbox_checker, pool, store, locks, team_ids: list[int]) -> dict:
    """대기 요청이 있으면 거부하고 팀별 실패를 모으며 나머지 팀을 계속 처리한다."""
    if await outbox_checker.pending_count() > 0:
        raise SystemExit("Pending index requests must be drained before rebuilding")
    result = {"rebuilt": [], "failed": {}}
    for team_id in sorted(set(team_ids)):
        try:
            await rebuild_team(team_id=team_id, reader=reader, pool=pool, store=store, locks=locks)
            result["rebuilt"].append(team_id)
        except Exception as exc:
            result["failed"][team_id] = f"{type(exc).__name__}: {exc}"
    return result


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """실제 데이터 반출과 색인 워커 정지를 확인하고 선택할 팀 번호를 받는다."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm-real-data-export", action="store_true", required=True,
                        help="업무 원본을 Gemini에 전송하는 것을 승인한다.")
    parser.add_argument("--confirm-index-worker-stopped", action="store_true", required=True,
                        help="색인 워커를 정지했음을 확인한다.")
    parser.add_argument("--team", type=int, action="append", help="재구축할 팀 ID. 여러 번 지정할 수 있다.")
    args = parser.parse_args(argv)
    if args.team and any(team <= 0 for team in args.team):
        parser.error("--team must be a positive integer")
    return args


async def main(argv: list[str] | None = None) -> int:
    """팀 목록을 정하고 게시 직후 캐시를 갱신하며 실패가 있으면 코드 2로 끝낸다."""
    args = parse_args(argv)
    from app.util.init_worklog_team_copies import OutboxChecker

    redis = Redis.from_url(settings.redis_url)
    cache = WorklogQueryCache(settings_obj=settings, client=redis)

    class PublishingCopyStore(WorklogTeamCopyStore):
        async def flip_read_copy(
            self, team_id: int, *, expected: TeamCopy, new: TeamCopy, mark_other_broken: bool = False,
        ) -> bool:
            """게시 성공 직후 캐시를 갱신하여 후속 복구 실패에도 옛 답변을 막는다."""
            flipped = await super().flip_read_copy(
                team_id, expected=expected, new=new, mark_other_broken=mark_other_broken,
            )
            if flipped:
                await cache.bump_version()
            return flipped

    reader = WorklogLightSourceRowReader()
    store = PublishingCopyStore()
    locks = TeamCopyLocks(client=redis)
    pool = LightRagTeamScopeAdapter(source_reader=reader, store=store, locks=locks)
    exit_code = 2
    try:
        team_ids = args.team
        if team_ids is None:
            async with get_session_factory()() as session:
                team_ids = list(await session.scalars(select(Team.team_id).order_by(Team.team_id)))
        result = await rebuild_all(reader=reader, outbox_checker=OutboxChecker(), pool=pool,
                                   store=store, locks=locks, team_ids=team_ids)
        print(json.dumps(result, ensure_ascii=False))
        exit_code = 2 if result["failed"] else 0
    except (Exception, SystemExit) as exc:
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False), file=sys.stderr)
    finally:
        closed = await asyncio.gather(pool.close(), store.close(), cache.close(), get_engine().dispose(), return_exceptions=True)
        if any(isinstance(item, BaseException) for item in closed):
            print(json.dumps({"error": "StorageCloseFailed"}), file=sys.stderr)
            exit_code = 2
    return exit_code


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
