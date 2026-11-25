"""재구축은 독자가 빠진 사본만 바꾸고 두 사본을 끝까지 맞춘다."""

import asyncio
import json
import sys
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.light.v4.service.worklog_team_copy_lock import LostWriteLockError
from app.util import rebuild_light_worklogs_v4 as migration
from tests.test_light_worklog_team_copy_apply import CopyAdapter, Lease, MemoryStore
from tests.test_light_worklog_team_scope import source_row


class Store(MemoryStore):
    def __init__(self, events):
        super().__init__(events)
        self.pointers = {}
        self.flip_allowed = True

    @property
    def pointer(self):
        return self.pointers.get(3, "a")

    @pointer.setter
    def pointer(self, copy):
        self.pointers[3] = copy

    async def read_copy(self, team_id):
        return self.pointers.get(team_id, "a")

    async def flip_read_copy(self, team_id, *, expected, new, mark_other_broken=False):
        if not self.flip_allowed or await self.read_copy(team_id) != expected:
            return False
        self.pointers[team_id] = new
        if mark_other_broken:
            self.broken = expected
        self.events.append(("flip", expected, new, mark_other_broken))
        return True


class Locks:
    def __init__(self):
        self.held = False
        self.lease = Lease()
        self.blocked = set()
        self.waiting = asyncio.Event()
        self.release = asyncio.Event()

    @asynccontextmanager
    async def team_write_lock(self, team_id):
        assert not self.held
        self.held = True
        try:
            yield self.lease
        finally:
            self.held = False

    async def wait_readers_drained(self, team_id, copy, *, timeout):
        assert self.held and timeout > 0
        if copy in self.blocked:
            self.waiting.set()
            await self.release.wait()
            self.blocked.remove(copy)


class Reader:
    def __init__(self, locks):
        self.locks = locks
        self.rows = {7: source_row(3, 7), 8: source_row(3, 8), 9: source_row(4, 9)}

    async def fetch_source_ids(self, *, after_id, limit):
        assert self.locks.held
        return sorted(key for key in self.rows if key > after_id)[:1]

    async def fetch_source_rows(self, ids):
        assert self.locks.held
        return {key: self.rows[key] for key in ids if key in self.rows}


@pytest.fixture
def runtime():
    events = []
    locks = Locks()
    store = Store(events)
    adapters = {}
    drops = []
    reader = Reader(locks)

    def adapter_for(team_id, copy):
        if (team_id, copy) not in adapters:
            adapter = CopyAdapter(events)
            original_drop = adapter.drop_all

            async def drop():
                assert locks.held and copy not in locks.blocked
                assert copy != await store.read_copy(team_id)
                drops.append((team_id, copy))
                await original_drop()

            adapter.drop_all = drop
            adapters[team_id, copy] = adapter
        return adapters[team_id, copy]

    return SimpleNamespace(reader=reader, store=store, locks=locks, events=events,
                           pool=SimpleNamespace(adapter_for=adapter_for), adapters=adapters,
                           drops=drops)


def rebuild(runtime, team_id=3):
    return migration.rebuild_team(team_id=team_id, reader=runtime.reader, pool=runtime.pool,
                                  store=runtime.store, locks=runtime.locks)


def test_rebuild_refuses_when_index_requests_are_pending(runtime):
    async def pending_count():
        return 1

    with pytest.raises(SystemExit):
        asyncio.run(migration.rebuild_all(
            reader=runtime.reader, outbox_checker=SimpleNamespace(pending_count=pending_count),
            pool=runtime.pool, store=runtime.store, locks=runtime.locks, team_ids=[3],
        ))
    assert runtime.events == []


@pytest.mark.parametrize("args", [[], ["--confirm-real-data-export"],
                                  ["--confirm-index-worker-stopped"]])
def test_rebuild_requires_both_confirmations(args):
    with pytest.raises(SystemExit):
        migration.parse_args(args)


def test_rebuild_accepts_repeated_team_selection():
    args = migration.parse_args(["--confirm-real-data-export", "--confirm-index-worker-stopped",
                                 "--team", "3", "--team", "4"])
    assert args.team == [3, 4]


@pytest.mark.parametrize("team", ["0", "-1", "abc"])
def test_rebuild_rejects_invalid_team_selection(team):
    with pytest.raises(SystemExit):
        migration.parse_args(["--confirm-real-data-export", "--confirm-index-worker-stopped",
                              "--team", team])


@pytest.mark.parametrize("read,write", [("a", "b"), ("b", "a")])
def test_rebuild_team_rebuilds_both_copies(runtime, read, write):
    runtime.store.pointer = read
    asyncio.run(rebuild(runtime))
    assert runtime.drops == [(3, write), (3, read)]
    assert runtime.store.pointer == write and runtime.store.broken is None
    assert [event for event in runtime.events if isinstance(event, tuple)] == [
        ("flip", read, write, True),
    ]
    for copy in ("a", "b"):
        assert {key[2]: entry.snapshot for key, entry in runtime.store.data.items()
                if key[:2] == (3, copy)} == {7: source_row(3, 7), 8: source_row(3, 8)}


@pytest.mark.parametrize("blocked", ["a", "b"])
def test_rebuild_waits_for_readers_before_each_copy(runtime, blocked):
    async def scenario():
        runtime.locks.blocked.add(blocked)
        task = asyncio.create_task(rebuild(runtime))
        await asyncio.wait_for(runtime.locks.waiting.wait(), 1)
        assert runtime.drops == ([] if blocked == "b" else [(3, "b")])
        runtime.locks.release.set()
        await task

    asyncio.run(scenario())


def test_second_copy_uses_new_read_snapshot_not_changed_source(runtime):
    original_flip = runtime.store.flip_read_copy

    async def flip(*args, **kwargs):
        result = await original_flip(*args, **kwargs)
        runtime.reader.rows[7] = replace(source_row(3, 7), title="changed after publication")
        return result

    runtime.store.flip_read_copy = flip
    asyncio.run(rebuild(runtime))
    assert runtime.store.data[3, "a", 7].snapshot.title == source_row(3, 7).title


@pytest.mark.parametrize("failed_copy", ["a", "b"])
def test_failed_rebuild_preserves_pointer_and_broken_marker(runtime, failed_copy):
    runtime.pool.adapter_for(3, failed_copy).fail_on = "drop"
    with pytest.raises(RuntimeError):
        asyncio.run(rebuild(runtime))
    assert runtime.store.pointer == ("a" if failed_copy == "b" else "b")
    assert runtime.store.broken == failed_copy


def test_failed_conditional_flip_does_not_touch_old_read_copy(runtime):
    runtime.store.flip_allowed = False
    with pytest.raises(LostWriteLockError):
        asyncio.run(rebuild(runtime))
    assert runtime.drops == [(3, "b")] and runtime.store.pointer == "a"


def test_lost_lease_after_first_rebuild_prevents_publication(runtime):
    original_ready = runtime.store.put_ready

    async def ready(team_id, copy, row):
        await original_ready(team_id, copy, row)
        if row.worklog_id == 8:
            runtime.locks.lease.lost = True

    runtime.store.put_ready = ready
    with pytest.raises(LostWriteLockError):
        asyncio.run(rebuild(runtime))
    assert runtime.store.pointer == "a" and runtime.drops == [(3, "b")]


def test_empty_team_clears_both_copies(runtime):
    runtime.reader.rows = {}
    asyncio.run(runtime.store.put_ready(3, "a", source_row(3, 7)))
    asyncio.run(rebuild(runtime))
    assert runtime.store.data == {} and runtime.drops == [(3, "b"), (3, "a")]


def test_rebuild_all_reports_failures_and_continues(runtime):
    async def pending_count():
        return 0

    runtime.pool.adapter_for(3, "b").fail_on = "drop"
    result = asyncio.run(migration.rebuild_all(
        reader=runtime.reader, outbox_checker=SimpleNamespace(pending_count=pending_count),
        pool=runtime.pool, store=runtime.store, locks=runtime.locks, team_ids=[3, 4],
    ))
    assert list(result["failed"]) == [3] and result["rebuilt"] == [4]
    assert "drop failed" in result["failed"][3]


@pytest.fixture
def cli(runtime, monkeypatch):
    closed = []
    runtime.pending = 0

    class CLIStore(Store):
        def __init__(self):
            self.__dict__ = runtime.store.__dict__

        async def close(self):
            closed.append("store")

    async def bump_version():
        runtime.events.append("cache_bumped")

    async def close_cache():
        closed.append("cache")

    async def close_pool():
        closed.append("pool")

    async def dispose_engine():
        closed.append("engine")

    async def pending_count():
        return runtime.pending

    @asynccontextmanager
    async def session():
        async def scalars(statement):
            assert statement.whereclause is None
            return [3, 4]

        yield SimpleNamespace(scalars=scalars)

    runtime.pool.close = close_pool
    monkeypatch.setattr(migration, "Redis", SimpleNamespace(from_url=lambda url: object()))
    monkeypatch.setattr(migration, "WorklogQueryCache", lambda **kwargs: SimpleNamespace(
        bump_version=bump_version, close=close_cache,
    ))
    monkeypatch.setattr(migration, "WorklogTeamCopyStore", CLIStore)
    monkeypatch.setattr(migration, "WorklogLightSourceRowReader", lambda: runtime.reader)
    monkeypatch.setattr(migration, "TeamCopyLocks", lambda **kwargs: runtime.locks)
    monkeypatch.setattr(migration, "LightRagTeamScopeAdapter", lambda **kwargs: runtime.pool)
    monkeypatch.setattr(migration, "get_session_factory", lambda: session)
    monkeypatch.setattr(migration, "get_engine", lambda: SimpleNamespace(dispose=dispose_engine))
    monkeypatch.setitem(sys.modules, "app.util.init_worklog_team_copies", SimpleNamespace(
        OutboxChecker=lambda: SimpleNamespace(pending_count=pending_count),
    ))
    return closed


@pytest.mark.parametrize("fails", [False, True])
def test_cli_bumps_cache_at_publication_and_reports_exit_code(runtime, cli, capsys, fails):
    if fails:
        runtime.pool.adapter_for(3, "a").fail_on = "drop"
    code = asyncio.run(migration.main([
        "--confirm-real-data-export", "--confirm-index-worker-stopped", "--team", "3",
    ]))
    assert code == (2 if fails else 0)
    assert runtime.events.index(("flip", "a", "b", True)) < runtime.events.index("cache_bumped")
    assert runtime.events.index("cache_bumped") < runtime.events.index("broken:a")
    assert set(cli) == {"pool", "store", "cache", "engine"}
    result = json.loads(capsys.readouterr().out)
    assert result["rebuilt"] == ([] if fails else [3])
    assert list(result["failed"]) == (["3"] if fails else [])


def test_cli_selects_all_teams_including_deleted_and_empty(runtime, cli, capsys):
    runtime.reader.rows = {}
    code = asyncio.run(migration.main([
        "--confirm-real-data-export", "--confirm-index-worker-stopped",
    ]))
    assert code == 0
    assert json.loads(capsys.readouterr().out) == {"rebuilt": [3, 4], "failed": {}}
    assert runtime.drops == [(3, "b"), (3, "a"), (4, "b"), (4, "a")]


def test_cli_pending_requests_return_failure_code_and_close_resources(runtime, cli, capsys):
    runtime.pending = 1
    code = asyncio.run(migration.main([
        "--confirm-real-data-export", "--confirm-index-worker-stopped", "--team", "3",
    ]))
    assert code == 2
    assert "Pending index requests" in capsys.readouterr().err
    assert runtime.drops == [] and set(cli) == {"pool", "store", "cache", "engine"}


@pytest.mark.parametrize("invalid", ["page", "row"])
def test_invalid_source_identity_is_rejected_before_modifying_copies(runtime, invalid):
    async def invalid_page(**kwargs):
        return [7, 7]

    async def invalid_rows(ids):
        return {7: source_row(3, 8)}

    if invalid == "page":
        runtime.reader.fetch_source_ids = invalid_page
    else:
        runtime.reader.fetch_source_rows = invalid_rows
    with pytest.raises(ValueError):
        asyncio.run(rebuild(runtime))
    assert runtime.drops == [] and runtime.store.pointer == "a"
