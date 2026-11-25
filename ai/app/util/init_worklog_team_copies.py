"""색인 워커를 멈춘 상태에서 기존 팀 인덱스를 사본 a로 등록하고 b를 채운다."""

import argparse
import asyncio
from collections.abc import Callable
from contextlib import AsyncExitStack
import json
import sys
from typing import Any

from sqlalchemy import text

from app.config.settings import settings
from app.light.v4.service.worklog_team_copy_apply import rebuild_copy
from app.store.session import get_session_factory


class OutboxChecker:
    """업무 DB의 대기 요청과 최종 실패를 포함한 Outbox 행을 조회한다."""

    def __init__(self, *, session_factory: Callable | None = None) -> None:
        self._session_factory = session_factory if session_factory is not None else get_session_factory()

    async def pending_count(self) -> int:
        """완료 요청은 삭제되므로 최종 실패가 아닌 모든 행을 대기로 센다."""
        async with self._session_factory() as session:
            result = await session.execute(text(
                "SELECT COUNT(*) FROM tb_worklog_light_outbox WHERE NOT terminal_failed",
            ))
            return result.scalar_one()

    async def worklog_ids_with_rows(self) -> set[int]:
        """처리 상태와 무관하게 Outbox에 남은 업무일지를 모두 반환한다."""
        async with self._session_factory() as session:
            result = await session.execute(text("SELECT worklog_id FROM tb_worklog_light_outbox"))
            return set(result.scalars())


async def init_copies(
    *, reader: Any, outbox_checker: Any, global_adapter: Any, pool: Any, store: Any,
    locks: Any, batch_size: int, seed_only: bool = False,
) -> dict[str, int]:
    """완료 상태가 일치하는 원본만 등록하고 Outbox 재확인 후 b를 복구한다."""
    if type(batch_size) is not int or not 1 <= batch_size <= settings.lightrag_index_max_batch_size:
        raise ValueError("Invalid initialization batch size")
    pending = await outbox_checker.pending_count()
    if pending:
        raise SystemExit(f"Refusing initialization: {pending} pending index requests")

    # 락 밖 원본은 팀 순서를 결정하는 데만 쓰고 실제 스냅샷은 락 안에서 다시 읽는다.
    by_team: dict[int, list[int]] = {}
    after_id = 0
    while ids := await reader.fetch_source_ids(after_id=after_id, limit=batch_size):
        if (len(ids) > batch_size or ids != sorted(set(ids))
                or any(type(key) is not int or key <= after_id for key in ids)):
            raise ValueError("Invalid source ID page")
        rows = await reader.fetch_source_rows(ids)
        for key, row in rows.items():
            if key not in ids or row.worklog_id != key:
                raise ValueError("Source identity mismatch")
            by_team.setdefault(row.team_id, []).append(key)
        after_id = ids[-1]

    for team_id in sorted(by_team):
        if await store.read_copy(team_id) != "a":
            raise SystemExit(f"Refusing initialization: team {team_id} already reads copy b")

    result = {"seeded": 0, "skipped": 0, "removed_after_recheck": 0, "rebuilt_teams": 0}
    excluded = await outbox_checker.worklog_ids_with_rows()
    for team_id, team_ids in sorted(by_team.items()):
        async with locks.team_write_lock(team_id) as lease:
            await lease.ensure_owned()
            if await store.read_copy(team_id) != "a":
                raise SystemExit(f"Refusing initialization: team {team_id} already reads copy b")
            adapter = pool.adapter_for(team_id, "a")
            for offset in range(0, len(team_ids), batch_size):
                ids = team_ids[offset:offset + batch_size]
                rows = await reader.fetch_source_rows(ids)
                completed = await global_adapter.get_processed_worklog_ids(ids)
                processed = await adapter.get_processed_worklog_ids(ids, require_completed=False)
                entries = await store.entries(team_id, "a", ids)
                for worklog_id in ids:
                    row = rows.get(worklog_id)
                    entry = entries.get(worklog_id)
                    if (row is None or row.worklog_id != worklog_id or row.team_id != team_id
                            or worklog_id in excluded or worklog_id not in completed
                            or worklog_id not in processed or entry is not None and entry.state == "READY"
                            or await global_adapter.get_document_team_id(f"worklog-{worklog_id}") != team_id):
                        result["skipped"] += 1
                        continue
                    await lease.ensure_owned()
                    await store.put_ready(team_id, "a", row)
                    result["seeded"] += 1

    excluded = await outbox_checker.worklog_ids_with_rows()
    for team_id in sorted(by_team):
        async with locks.team_write_lock(team_id) as lease:
            entries = await store.entries(team_id, "a")
            for worklog_id in entries.keys() & excluded:
                await lease.ensure_owned()
                await store.remove(team_id, "a", worklog_id)
                result["removed_after_recheck"] += 1

    if not seed_only:
        for team_id in sorted(by_team):
            async with locks.team_write_lock(team_id) as lease:
                if await store.read_copy(team_id) != "a":
                    raise SystemExit(f"Refusing initialization: team {team_id} already reads copy b")
                await locks.wait_readers_drained(
                    team_id, "b", timeout=settings.worklog_light_index_timeout_seconds / 2,
                )
                entries = await store.entries(team_id, "a")
                await rebuild_copy(
                    pool, store, lease=lease, team_id=team_id, copy="b",
                    rows=[entry.snapshot for entry in entries.values() if entry.state == "READY"],
                )
                result["rebuilt_teams"] += 1
    return result


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """초기 등록 배치 크기와 사본 a만 등록하는 모드를 받는다."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-size", type=int, default=settings.lightrag_index_max_batch_size)
    parser.add_argument("--seed-only", action="store_true", help="사본 a 등록과 Outbox 재확인까지만 실행한다.")
    return parser.parse_args(argv)


async def main(argv: list[str] | None = None) -> int:
    """기존 전사 어댑터를 검증용으로만 열고 모든 연결을 종료한다."""
    from redis.asyncio import Redis

    from app.light.v4.service.lightrag_adapter import LightRagWorklogIndexAdapter
    from app.light.v4.service.worklog_source_row_reader import WorklogLightSourceRowReader
    from app.light.v4.service.worklog_team_copy_lock import TeamCopyLocks
    from app.light.v4.service.worklog_team_scope import LightRagTeamScopeAdapter
    from app.light.v4.store.worklog_team_copy_store import WorklogTeamCopyStore
    from app.store.session import get_engine

    args = parse_args(argv)
    async with AsyncExitStack() as stack:
        client = Redis.from_url(settings.redis_url)
        stack.push_async_callback(client.aclose)
        store = WorklogTeamCopyStore()
        stack.push_async_callback(store.close)
        reader = WorklogLightSourceRowReader()
        locks = TeamCopyLocks(client=client)
        adapter = LightRagWorklogIndexAdapter()
        stack.push_async_callback(adapter.close)
        pool = LightRagTeamScopeAdapter(source_reader=reader, store=store, locks=locks)
        stack.push_async_callback(pool.close)
        stack.push_async_callback(get_engine().dispose)
        result = await init_copies(
            reader=reader, outbox_checker=OutboxChecker(), global_adapter=adapter,
            pool=pool, store=store, locks=locks, batch_size=args.batch_size, seed_only=args.seed_only,
        )
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except Exception as exc:
        print(json.dumps({"error_type": type(exc).__name__}), file=sys.stderr)
        raise SystemExit(1) from exc
