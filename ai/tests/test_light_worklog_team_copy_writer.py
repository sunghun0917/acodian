"""쓰기 사본 배출, 복구, 최신 원본 반영과 조건부 게시 계약."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.light.v4.service.lightrag_adapter import LightRagInsertFailedError
from app.light.v4.service.worklog_team_copy_lock import LostWriteLockError
from app.light.v4.store.worklog_team_copy_store import (
    CopyEntry, DocumentFootprint, other_copy, snapshot_fingerprint,
)
from app.light.v4.store.worklog_source_store import LightWorklogTag
from tests.test_light_worklog_team_scope import source_row


EMPTY = DocumentFootprint(frozenset(), frozenset(), frozenset())


def row(worklog_id=7, *, team=3, title="v3", tags=None):
    return replace(source_row(team, worklog_id), title=title, tags=tags or [])


class MemoryStore:
    def __init__(self, events):
        self.events, self.data, self.pointers, self.broken = events, {}, {}, {}
        self.reject_flip = False

    def seed(self, team, copy, value):
        self.data[team, copy, value.worklog_id] = CopyEntry(
            team, copy, value.worklog_id, "READY", snapshot_fingerprint(value), value, None,
        )

    async def read_copy(self, team):
        return self.pointers.get(team, "a")

    async def flip_read_copy(self, team, *, expected, new):
        if self.reject_flip or await self.read_copy(team) != expected:
            return False
        self.pointers[team] = new
        self.events.append(f"flip:{team}:{expected}->{new}")
        return True

    async def broken_copy(self, team):
        return self.broken.get(team)

    async def set_broken_copy(self, team, copy):
        self.broken[team] = copy

    async def entries(self, team, copy, worklog_ids=None):
        return {key[2]: value for key, value in self.data.items()
                if key[:2] == (team, copy) and (worklog_ids is None or key[2] in worklog_ids)}

    async def mark_working(self, team, copy, worklog_id, footprint=EMPTY):
        self.data[team, copy, worklog_id] = CopyEntry(
            team, copy, worklog_id, "WORKING", None, None, footprint,
        )

    async def put_ready(self, team, copy, value):
        self.seed(team, copy, value)

    async def remove(self, team, copy, worklog_id):
        self.data.pop((team, copy, worklog_id), None)

    async def clear_copy(self, team, copy):
        self.data = {key: value for key, value in self.data.items() if key[:2] != (team, copy)}

    async def teams_containing(self, worklog_id):
        return {key[0] for key in self.data if key[2] == worklog_id}

    async def differing_worklog_ids(self, team, *, read):
        r, w = await self.entries(team, read), await self.entries(team, other_copy(read))
        return sorted(key for key in r.keys() | w.keys()
                      if key not in r or key not in w or w[key].state == "WORKING"
                      or r[key].fingerprint != w[key].fingerprint)


class Locks:
    def __init__(self, events):
        self.events, self.readers, self.active = events, {}, set()
        self.lost = False
        self.max_concurrent_team_locks = 0
        self.drain_started = asyncio.Event()

    @asynccontextmanager
    async def team_write_lock(self, team):
        assert team not in self.active
        self.active.add(team)
        self.max_concurrent_team_locks = max(self.max_concurrent_team_locks, len(self.active))
        try:
            yield self
        finally:
            self.active.remove(team)

    async def ensure_owned(self):
        if self.lost:
            raise LostWriteLockError("lost")

    async def wait_readers_drained(self, team, copy, *, timeout):
        assert team in self.active
        self.events.append(f"drain:{team}:{copy}")
        self.drain_started.set()
        async with asyncio.timeout(timeout):
            while self.readers.get((team, copy), 0):
                await asyncio.sleep(0.001)


class Pool:
    def __init__(self, events, locks):
        self.events, self.locks, self.adapters = events, locks, {}
        self.fail_on = None
        self.after_apply = None
        self.fail_delete = False

    def adapter_for(self, team, copy):
        if (team, copy) not in self.adapters:
            self.adapters[team, copy] = Adapter(self, team, copy)
        return self.adapters[team, copy]


class Adapter:
    def __init__(self, pool, team, copy):
        self.pool, self.team, self.copy, self.documents = pool, team, copy, {}
        self.stale = []

    async def document_footprint(self, document_id):
        return EMPTY

    async def delete_document(self, document_id):
        assert self.team in self.pool.locks.active
        assert self.pool.locks.readers.get((self.team, self.copy), 0) == 0
        if self.pool.fail_delete:
            from app.light.v4.service.lightrag_adapter import LightRagDeleteFailedError
            raise LightRagDeleteFailedError("delete failed")
        self.documents.pop(document_id, None)

    async def find_stale_references(self, footprint):
        return self.stale

    async def index_documents(self, documents):
        for document in documents:
            title = next(line[7:] for line in document.text.splitlines() if line.startswith("title: "))
            self.pool.events.append(f"apply:{self.team}:{self.copy}:{document.document_id}:{title}")
            self.documents[document.document_id] = document
            fail = self.pool.fail_on
            if fail and fail[0] in ("*", self.copy) and fail[1] == document.document_id and fail[2] in (None, title):
                if self.pool.after_apply:
                    self.pool.after_apply()
                raise LightRagInsertFailedError("poison")

    async def index_custom_kg_documents(self, documents):
        if self.pool.after_apply:
            self.pool.after_apply()

    async def drop_all(self):
        assert self.pool.locks.readers.get((self.team, self.copy), 0) == 0
        self.pool.events.append(f"rebuild:{self.team}:{self.copy}")
        self.documents.clear()


class Scenario:
    def __init__(self):
        from app.light.v4.service.worklog_team_copy_writer import TeamCopyWriter
        self.events, self.current = [], {}
        self.store = MemoryStore(self.events)
        self.locks = Locks(self.events)
        self.pool = Pool(self.events, self.locks)
        self.cache = SimpleNamespace(bump_version=AsyncMock())
        self.reader = SimpleNamespace(fetch_source_rows=self.fetch)
        self.on_fetch = None
        self.writer = TeamCopyWriter(pool=self.pool, store=self.store, locks=self.locks,
                                     source_reader=self.reader, cache=self.cache)

    async def fetch(self, ids):
        if self.on_fetch:
            self.on_fetch(bool(self.locks.active))
        return {key: value for key, value in self.current.items() if key in ids}

    def seed_both(self, value):
        for copy in ("a", "b"):
            self.store.seed(value.team_id, copy, value)
            self.pool.adapter_for(value.team_id, copy).documents[f"worklog-{value.worklog_id}"] = value
        self.current[value.worklog_id] = value

    def snapshot(self, team, copy, worklog_id=7):
        return self.store.data[team, copy, worklog_id].snapshot


@pytest.fixture
def s():
    return Scenario()


def test_update_applies_write_copy_then_flips_and_leaves_old_copy(s):
    s.seed_both(row()); s.current[7] = row(title="v4")
    asyncio.run(s.writer.sync_worklog(7))
    assert s.events == ["drain:3:b", "apply:3:b:worklog-7:v4", "flip:3:a->b"]
    assert s.snapshot(3, "b").title == "v4" and s.snapshot(3, "a").title == "v3"
    s.cache.bump_version.assert_awaited_once()


def test_next_job_cleans_old_copy_to_read_state_first(s):
    async def run():
        s.seed_both(row()); s.current[7] = row(title="v4")
        await s.writer.sync_worklog(7); s.events.clear()
        s.current[8] = row(8)
        await s.writer.sync_worklog(8)
        assert s.events[:2] == ["drain:3:a", "apply:3:a:worklog-7:v4"]
        assert s.snapshot(3, "a").title == "v4"
    asyncio.run(run())


def test_next_job_waits_for_readers_before_cleanup(s):
    async def run():
        s.seed_both(row()); s.current[7] = row(title="v4")
        await s.writer.sync_worklog(7); s.events.clear(); s.locks.drain_started.clear()
        s.locks.readers[3, "a"] = 1; s.current[8] = row(8)
        task = asyncio.create_task(s.writer.sync_worklog(8))
        await asyncio.wait_for(s.locks.drain_started.wait(), 1)
        assert not task.done() and s.events == ["drain:3:a"]
        assert s.snapshot(3, "a").title == "v3"
        s.locks.readers[3, "a"] = 0
        await asyncio.wait_for(task, 1)
        assert s.snapshot(3, "a").title == "v4"
    asyncio.run(run())


def test_tag_only_change_is_applied(s):
    s.seed_both(row()); s.current[7] = row(tags=[LightWorklogTag(5, "tag", None)])
    asyncio.run(s.writer.sync_worklog(7))
    assert s.snapshot(3, "b").tags == s.current[7].tags
    assert "flip:3:a->b" in s.events


def test_same_content_is_noop(s):
    s.seed_both(row())
    asyncio.run(s.writer.sync_worklog(7))
    assert s.events == ["drain:3:b"]
    s.cache.bump_version.assert_not_awaited()


@pytest.mark.parametrize("current_exists", [False, True])
def test_read_copy_working_row_is_not_skipped(s, current_exists):
    async def run():
        s.seed_both(row()); await s.store.mark_working(3, "a", 7)
        if not current_exists:
            s.current.clear()
        await s.writer.sync_worklog(7)
        assert "flip:3:a->b" in s.events
        assert ((3, "b", 7) in s.store.data) == current_exists
    asyncio.run(run())


def test_compensation_checks_lease_first(s):
    s.seed_both(row()); s.current[7] = row(title="v4")
    s.pool.fail_on = ("b", "worklog-7", "v4")
    s.pool.after_apply = lambda: setattr(s.locks, "lost", True)
    with pytest.raises(LightRagInsertFailedError):
        asyncio.run(s.writer.sync_worklog(7))
    assert [e for e in s.events if e.startswith("apply:")] == ["apply:3:b:worklog-7:v4"]
    assert s.store.pointers == {}


def test_late_old_request_uses_latest_db_row(s):
    s.seed_both(row(title="v5"))
    asyncio.run(s.writer.sync_worklog(7))
    assert s.events == ["drain:3:b"] and s.snapshot(3, "a").title == "v5"


def test_latest_row_is_read_again_inside_team_lock(s):
    s.seed_both(row())
    def change(inside):
        if inside:
            s.current[7] = row(title="changed-under-lock")
    s.on_fetch = change
    asyncio.run(s.writer.sync_worklog(7))
    assert s.snapshot(3, "b").title == "changed-under-lock"


def test_failed_apply_compensates_and_keeps_pointer(s):
    s.seed_both(row()); s.current[7] = row(title="v4")
    s.pool.fail_on = ("b", "worklog-7", "v4")
    with pytest.raises(LightRagInsertFailedError):
        asyncio.run(s.writer.sync_worklog(7))
    assert s.store.pointers == {} and s.snapshot(3, "b").title == "v3"
    s.cache.bump_version.assert_not_awaited()


def test_working_row_is_restored_from_read_copy_by_next_job(s):
    async def run():
        s.seed_both(row()); await s.store.mark_working(3, "b", 7)
        s.pool.adapter_for(3, "b").documents.clear()
        s.current.update({7: row(title="v4"), 8: row(8)})
        await s.writer.sync_worklog(8)
        assert s.snapshot(3, "b").title == "v3"
        assert "worklog-7" in s.pool.adapter_for(3, "b").documents
        assert s.store.pointers[3] == "b"
    asyncio.run(run())


def test_poison_worklog_does_not_block_team(s):
    async def run():
        s.seed_both(row()); s.current[7] = row(title="v4")
        s.pool.fail_on = ("*", "worklog-7", "v4")
        for _ in range(4):
            with pytest.raises(LightRagInsertFailedError):
                await s.writer.sync_worklog(7)
        s.current[8] = row(8)
        await s.writer.sync_worklog(8)
        assert s.snapshot(3, s.store.pointers[3]).title == "v3"
        assert (3, s.store.pointers[3], 8) in s.store.data
    asyncio.run(run())


def test_new_worklog_failure_is_cleaned_to_absent(s):
    s.current[9] = row(9); s.pool.fail_on = ("b", "worklog-9", None)
    with pytest.raises(LightRagInsertFailedError):
        asyncio.run(s.writer.sync_worklog(9))
    assert (3, "b", 9) not in s.store.data
    assert not s.pool.adapter_for(3, "b").documents


def test_broken_copy_is_rebuilt_before_next_change(s):
    s.seed_both(row()); s.store.broken[3] = "b"; s.current[8] = row(8)
    asyncio.run(s.writer.sync_worklog(8))
    assert s.events[:2] == ["drain:3:b", "rebuild:3:b"]
    assert s.store.broken[3] is None and s.snapshot(3, "b").title == "v3"


@pytest.mark.parametrize("lost_lease", [False, True])
def test_lost_lock_before_flip_does_not_flip(s, lost_lease):
    s.seed_both(row()); s.current[7] = row(title="v4")
    if lost_lease:
        s.pool.after_apply = lambda: setattr(s.locks, "lost", True)
    else:
        s.store.reject_flip = True
    with pytest.raises(LostWriteLockError):
        asyncio.run(s.writer.sync_worklog(7))
    assert s.store.pointers == {} and s.snapshot(3, "a").title == "v3"
    s.cache.bump_version.assert_not_awaited()


def test_team_move_indexes_new_team_then_removes_old(s):
    s.seed_both(row()); s.current[7] = row(team=4)
    asyncio.run(s.writer.sync_worklog(7))
    assert s.events.index("flip:4:a->b") < s.events.index("flip:3:a->b")
    assert (4, "b", 7) in s.store.data and (3, "b", 7) not in s.store.data
    assert s.locks.max_concurrent_team_locks == 1


def test_delete_removes_from_read_copy_after_flip(s):
    s.seed_both(row()); s.current.clear()
    asyncio.run(s.writer.sync_worklog(7))
    assert (3, s.store.pointers[3], 7) not in s.store.data
    assert s.snapshot(3, "a").title == "v3"


def test_drain_timeout_prevents_all_mutation(s):
    s.seed_both(row()); s.locks.readers[3, "b"] = 1; s.current[7] = row(title="v4")
    s.writer._settings = SimpleNamespace(worklog_light_index_timeout_seconds=0.02)
    with pytest.raises(TimeoutError):
        asyncio.run(s.writer.sync_worklog(7))
    assert s.events == ["drain:3:b"] and s.snapshot(3, "b").title == "v3"


def test_cancelled_apply_leaves_working_row_for_next_job(s):
    async def run():
        s.seed_both(row()); s.current[7] = row(title="v4")
        started = asyncio.Event()
        async def blocked(documents):
            started.set()
            await asyncio.Future()
        adapter = s.pool.adapter_for(3, "b")
        original = adapter.index_documents
        adapter.index_documents = blocked
        task = asyncio.create_task(s.writer.sync_worklog(7))
        await asyncio.wait_for(started.wait(), 1); task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert s.store.data[3, "b", 7].state == "WORKING"
        assert not s.locks.active and s.store.pointers == {}
        adapter.index_documents = original; s.current[8] = row(8)
        await s.writer.sync_worklog(8)
        assert s.snapshot(3, "b").title == "v3"
    asyncio.run(run())
