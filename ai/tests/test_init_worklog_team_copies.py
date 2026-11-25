"""기존 팀 인덱스의 보수적인 사본 초기화 계약."""

import asyncio
from functools import wraps
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.test_light_worklog_team_copy_writer import Locks, MemoryStore, Pool, row


def async_test(test):
    @wraps(test)
    def run(*args, **kwargs):
        return asyncio.run(test(*args, **kwargs))
    return run


class Scenario:
    def __init__(self):
        self.events = []
        self.current = {7: row(7)}
        self.store = MemoryStore(self.events)
        self.locks = Locks(self.events)
        self.pool = Pool(self.events, self.locks)
        self.completed = {7}
        self.teams = {7: 3}
        self.processed = {(3, "a"): {7}}
        self.outbox_rows = set()
        self.after_seed_rows = None
        self.outbox_reads = 0
        self.pending = 0
        self.on_locked_fetch = None
        self.global_adapter = SimpleNamespace(
            get_processed_worklog_ids=self.global_processed, get_document_team_id=self.owner,
        )
        self.reader = SimpleNamespace(fetch_source_ids=self.ids, fetch_source_rows=self.fetch)
        self.outbox = SimpleNamespace(pending_count=self.pending_count, worklog_ids_with_rows=self.outbox_ids)
        original_adapter_for = self.pool.adapter_for

        def adapter_for(team, copy="a"):
            adapter = original_adapter_for(team, copy)
            adapter.get_processed_worklog_ids = AsyncMock(
                side_effect=lambda ids, *, require_completed: self.processed.get((team, copy), set()) & set(ids),
            )
            return adapter

        self.pool.adapter_for = adapter_for

    async def ids(self, *, after_id, limit):
        return sorted(key for key in self.current if key > after_id)[:limit]

    async def fetch(self, ids):
        if self.locks.active and self.on_locked_fetch:
            self.on_locked_fetch()
        return {key: self.current[key] for key in ids if key in self.current}

    async def pending_count(self):
        return self.pending

    async def outbox_ids(self):
        self.outbox_reads += 1
        if self.outbox_reads > 1 and self.after_seed_rows is not None:
            return self.after_seed_rows
        return self.outbox_rows

    async def global_processed(self, ids):
        return self.completed & set(ids)

    async def owner(self, document_id):
        return self.teams.get(int(document_id.removeprefix("worklog-")))

    async def run(self, **kwargs):
        from app.util.init_worklog_team_copies import init_copies
        return await init_copies(
            reader=self.reader, outbox_checker=self.outbox, global_adapter=self.global_adapter,
            pool=self.pool, store=self.store, locks=self.locks, batch_size=kwargs.pop("batch_size", 2),
            **kwargs,
        )


@async_test
async def test_refuses_when_index_requests_are_pending():
    s = Scenario()
    s.pending = 2
    with pytest.raises(SystemExit, match="2"):
        await s.run()
    assert s.store.data == {} and s.events == []


@async_test
async def test_seed_copy_a_only_for_completed_matching_team_documents():
    s = Scenario()
    s.current = {7: row(7), 8: row(8), 9: row(9, team=4), 10: row(10)}
    s.completed = {7, 9, 10}
    s.teams = {7: 3, 9: 4, 10: 4}
    result = await s.run()
    assert result == {"seeded": 1, "skipped": 3, "removed_after_recheck": 0, "rebuilt_teams": 2}
    entry = (await s.store.entries(3, "a"))[7]
    assert entry.state == "READY" and entry.snapshot == s.current[7]
    assert await s.store.entries(4, "a") == {}


@async_test
async def test_worklogs_with_outbox_rows_are_not_seeded():
    s = Scenario()
    s.current[8] = row(8)
    s.completed.add(8)
    s.teams[8] = 3
    s.processed[3, "a"].add(8)
    s.outbox_rows = {8}
    result = await s.run()
    assert result["seeded"] == 1 and result["skipped"] == 1
    assert set(await s.store.entries(3, "a")) == {7}


@async_test
async def test_worklog_changed_during_seed_is_removed_on_recheck():
    s = Scenario()
    s.after_seed_rows = {7}
    result = await s.run()
    assert result["removed_after_recheck"] == 1
    assert await s.store.entries(3, "a") == {} and await s.store.entries(3, "b") == {}


@async_test
async def test_builds_copy_b_from_copy_a_snapshots():
    s = Scenario()
    await s.run()
    assert s.events == ["drain:3:b", "rebuild:3:b", "apply:3:b:worklog-7:v3"]
    assert (await s.store.entries(3, "b"))[7].snapshot == s.current[7]
    assert await s.store.broken_copy(3) is None
    assert await s.store.read_copy(3) == "a"
    assert s.locks.max_concurrent_team_locks == 1


@async_test
async def test_refuses_rebuild_when_pointer_changes_to_b_inside_team_lock(monkeypatch):
    s = Scenario()
    original_lock = s.locks.team_write_lock
    lock_count = 0

    @asynccontextmanager
    async def team_write_lock(team_id):
        nonlocal lock_count
        async with original_lock(team_id) as lease:
            lock_count += 1
            if lock_count == 3:
                s.store.pointers[team_id] = "b"
            yield lease

    monkeypatch.setattr(s.locks, "team_write_lock", team_write_lock)
    rebuild = AsyncMock()
    monkeypatch.setattr("app.util.init_worklog_team_copies.rebuild_copy", rebuild)
    with pytest.raises(SystemExit, match="team 3 already reads copy b"):
        await s.run()
    rebuild.assert_not_called()
    assert s.events == []


@async_test
async def test_copy_b_is_not_modified_until_readers_are_drained():
    s = Scenario()
    s.locks.readers[3, "b"] = 1
    task = asyncio.create_task(s.run())
    await asyncio.wait_for(s.locks.drain_started.wait(), 1)
    assert s.events == ["drain:3:b"] and await s.store.entries(3, "b") == {}
    s.locks.readers[3, "b"] = 0
    assert (await task)["rebuilt_teams"] == 1


@async_test
async def test_rerun_is_safe_and_does_not_overwrite_ready_snapshot():
    s = Scenario()
    await s.run()
    previous = (await s.store.entries(3, "a"))[7]
    s.current[7] = replace(s.current[7], title="changed")
    result = await s.run()
    assert result["seeded"] == 0 and result["skipped"] == 1
    assert (await s.store.entries(3, "a")) == {7: previous}
    assert (await s.store.entries(3, "b"))[7].snapshot == previous.snapshot


@async_test
async def test_seed_only_rechecks_outbox_without_rebuilding_b():
    s = Scenario()
    s.after_seed_rows = {7}
    result = await s.run(seed_only=True)
    assert result["rebuilt_teams"] == 0 and result["removed_after_recheck"] == 1
    assert s.events == [] and await s.store.entries(3, "a") == {}


@async_test
async def test_refuses_all_seeding_when_any_team_pointer_is_b():
    s = Scenario()
    s.current[9] = row(9, team=4)
    s.store.pointers[4] = "b"
    with pytest.raises(SystemExit, match="4"):
        await s.run(batch_size=1)
    assert s.store.data == {} and s.events == []


@async_test
async def test_seeding_reads_current_source_inside_team_lock():
    s = Scenario()
    s.on_locked_fetch = lambda: s.current.update({7: replace(s.current[7], title="latest")})
    await s.run(seed_only=True)
    assert (await s.store.entries(3, "a"))[7].snapshot.title == "latest"


@async_test
async def test_team_move_between_discovery_and_seed_is_skipped():
    s = Scenario()
    s.on_locked_fetch = lambda: s.current.update({7: row(7, team=4)})
    result = await s.run(seed_only=True)
    assert result["skipped"] == 1 and s.store.data == {}


@async_test
async def test_failed_rebuild_keeps_copy_b_broken_and_pointer_a():
    s = Scenario()
    s.pool.fail_on = ("b", "worklog-7", None)
    with pytest.raises(Exception, match="poison"):
        await s.run()
    assert await s.store.broken_copy(3) == "b" and await s.store.read_copy(3) == "a"


@pytest.mark.parametrize("batch_size", [0, -1, True])
@async_test
async def test_rejects_invalid_batch_size(batch_size):
    with pytest.raises(ValueError, match="batch"):
        await Scenario().run(batch_size=batch_size)


@async_test
async def test_outbox_checker_counts_every_nonterminal_row_and_lists_all_rows():
    from app.util.init_worklog_team_copies import OutboxChecker

    statements = []

    class Session:
        async def execute(self, statement):
            sql = " ".join(str(statement).split())
            statements.append(sql)
            return SimpleNamespace(scalar_one=lambda: 2, scalars=lambda: [7, 8, 8])

    @asynccontextmanager
    async def session_factory():
        yield Session()

    checker = OutboxChecker(session_factory=session_factory)
    assert await checker.pending_count() == 2
    assert await checker.worklog_ids_with_rows() == {7, 8}
    assert statements == [
        "SELECT COUNT(*) FROM tb_worklog_light_outbox WHERE NOT terminal_failed",
        "SELECT worklog_id FROM tb_worklog_light_outbox",
    ]


def test_cli_parses_seed_only_and_batch_size():
    from app.util.init_worklog_team_copies import parse_args
    args = parse_args(["--batch-size", "5", "--seed-only"])
    assert args.batch_size == 5 and args.seed_only is True
