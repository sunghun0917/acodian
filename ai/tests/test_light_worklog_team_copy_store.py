"""팀 사본의 지문, 스냅샷 및 PostgreSQL 조건부 전환 계약."""

import asyncio
from dataclasses import replace
import json
import os
from uuid import uuid4

import pytest
from sqlalchemy import event, text

from app.config.settings import Settings
from app.light.v4.store.worklog_source_store import (
    LightWorklogPredecessor, LightWorklogSourceRow, LightWorklogTag,
)
from app.light.v4.store.worklog_team_copy_store import (
    DocumentFootprint, WorklogTeamCopyStore, footprint_from_json,
    footprint_to_json, other_copy, snapshot_fingerprint,
    snapshot_from_json, snapshot_to_json,
)
from tests.test_light_worklog_pg_storage_integration import validate_test_pg_dsn


pg_required = pytest.mark.skipif(
    not (os.environ.get("RAG_SCOPE_TEST_ID") and os.environ.get("RAG_SCOPE_TEST_PG_DSN")),
    reason="requires explicit isolated PostgreSQL test database",
)
EMPTY_FOOTPRINT = DocumentFootprint(frozenset(), frozenset(), frozenset())


def make_row(worklog_id=7, *, team=3, **changes):
    return replace(LightWorklogSourceRow(
        worklog_id=worklog_id, title="입고 검증", request_content=None,
        work_content="입고 수량을 확인했다.", author_id=24, author_name="홍길동",
        team_id=team, team_name="물류팀", tags=[], direct_predecessors=[],
    ), **changes)


def test_fingerprint_ignores_tag_order_and_detects_tag_change():
    tags = [LightWorklogTag(1, "태그1", None), LightWorklogTag(2, "태그2", "설명")]
    base = make_row(tags=tags)
    assert snapshot_fingerprint(base) == snapshot_fingerprint(make_row(tags=tags[::-1]))
    assert snapshot_fingerprint(base) != snapshot_fingerprint(make_row(tags=tags[:1]))
    assert snapshot_fingerprint(None) is None


def test_fingerprint_normalizes_predecessor_order_without_mutating_snapshot():
    predecessors = [LightWorklogPredecessor(6, "둘째", 3), LightWorklogPredecessor(5, "첫째", None)]
    row = make_row(direct_predecessors=predecessors)
    assert snapshot_fingerprint(row) == snapshot_fingerprint(
        replace(row, direct_predecessors=predecessors[::-1]),
    )
    assert row.direct_predecessors == predecessors


@pytest.mark.parametrize("change", [
    {"worklog_id": 8}, {"title": "수정"}, {"request_content": "요청"},
    {"work_content": "수정"}, {"author_id": 25}, {"author_name": "다른 사람"},
    {"team_id": 4}, {"team_name": "다른 팀"},
    {"tags": [LightWorklogTag(1, "태그", "수정")]},
    {"direct_predecessors": [LightWorklogPredecessor(5, "선행", 4)]},
])
def test_fingerprint_covers_snapshot_fields(change):
    row = make_row(tags=[LightWorklogTag(1, "태그", None)],
                   direct_predecessors=[LightWorklogPredecessor(5, "선행", 3)])
    assert snapshot_fingerprint(row) != snapshot_fingerprint(replace(row, **change))


def test_snapshot_json_roundtrip():
    row = make_row(tags=[LightWorklogTag(1, "태그", None)],
                   direct_predecessors=[LightWorklogPredecessor(5, "선행", None)])
    assert snapshot_from_json(json.loads(json.dumps(snapshot_to_json(row)))) == row


def test_footprint_json_roundtrip():
    footprint = DocumentFootprint(
        frozenset({"chunk-2", "chunk-1"}), frozenset({"Tag:5", "User:24"}),
        frozenset({("User:24", "Tag:5"), ("Tag:5", "Worklog:7")}),
    )
    assert footprint_from_json(json.loads(json.dumps(footprint_to_json(footprint)))) == footprint
    assert footprint_from_json(footprint_to_json(EMPTY_FOOTPRINT)) == EMPTY_FOOTPRINT
    assert footprint_to_json(footprint)["chunk_ids"] == ["chunk-1", "chunk-2"]


def test_other_copy():
    assert other_copy("a") == "b"
    assert other_copy("b") == "a"


@pytest.mark.parametrize("dsn", ["", "sqlite:///business.db"])
def test_store_requires_light_database_dsn(dsn):
    store = WorklogTeamCopyStore(settings_obj=Settings(_env_file=None, lightrag_pg_dsn=dsn))
    with pytest.raises(ValueError):
        asyncio.run(store.read_copy(3))


@pytest.fixture
def team():
    return uuid4().int % (2**63 - 1)


@pytest.fixture
def store(team):
    dsn = validate_test_pg_dsn(os.environ["RAG_SCOPE_TEST_PG_DSN"])
    store = WorklogTeamCopyStore(settings_obj=Settings(_env_file=None, lightrag_pg_dsn=dsn))
    yield store

    async def cleanup():
        try:
            await store.clear_copy(team, "a")
            await store.clear_copy(team, "b")
            async with store._engine.begin() as connection:
                await connection.execute(text("DELETE FROM worklog_team_copy_pointer WHERE team_id = :team"),
                                         {"team": team})
        finally:
            await store.close()

    asyncio.run(cleanup())


@pg_required
def test_flip_is_conditional(store, team):
    async def run():
        assert await store.read_copy(team) == "a"
        assert await store.flip_read_copy(team, expected="b", new="a") is False
        assert await store.flip_read_copy(team, expected="a", new="b") is True
        assert await store.flip_read_copy(team, expected="a", new="b") is False
        assert await store.read_copy(team) == "b"
        assert await store.flip_read_copy(team, expected="b", new="a") is True
    asyncio.run(run())


@pg_required
def test_flip_and_mark_other_broken_is_one_update(store, team):
    async def run():
        assert await store.flip_read_copy(team, expected="a", new="b", mark_other_broken=True)
        assert await store.read_copy(team) == "b"
        assert await store.broken_copy(team) == "a"
        assert not await store.flip_read_copy(team, expected="a", new="b", mark_other_broken=True)
        assert await store.broken_copy(team) == "a"
        statements = []
        def capture(connection, cursor, statement, parameters, context, executemany):
            statements.append(statement.strip().lower())
        event.listen(store._engine.sync_engine, "before_cursor_execute", capture)
        try:
            assert await store.flip_read_copy(team, expected="b", new="a", mark_other_broken=True)
        finally:
            event.remove(store._engine.sync_engine, "before_cursor_execute", capture)
        assert len(statements) == 1
        assert statements[0].startswith("update ") and "broken_copy =" in statements[0]
        assert await store.broken_copy(team) == "b"
    asyncio.run(run())


@pg_required
@pytest.mark.parametrize("initialized", [False, True])
def test_concurrent_flips_have_one_winner(store, team, initialized):
    async def run():
        if initialized:
            await store.set_broken_copy(team, None)
        results = await asyncio.gather(*(
            store.flip_read_copy(team, expected="a", new="b") for _ in range(4)
        ))
        assert results.count(True) == 1
        assert await store.read_copy(team) == "b"
    asyncio.run(run())


@pg_required
def test_broken_copy_defaults_and_preserves_pointer(store, team):
    async def run():
        assert await store.broken_copy(team) is None
        await store.set_broken_copy(team, "b")
        assert await store.read_copy(team) == "a"
        assert await store.broken_copy(team) == "b"
        await store.flip_read_copy(team, expected="a", new="b")
        assert await store.broken_copy(team) == "b"
        await store.set_broken_copy(team, None)
        assert await store.read_copy(team) == "b"
        assert await store.broken_copy(team) is None
    asyncio.run(run())


@pg_required
def test_working_row_keeps_footprint_and_clears_fingerprint(store, team):
    async def run():
        footprint = DocumentFootprint(frozenset({"chunk-1"}), frozenset({"Tag:5"}),
                                     frozenset({("Worklog:7", "Tag:5")}))
        row = make_row(team=team)
        await store.put_ready(team, "b", row)
        await store.mark_working(team, "b", 7, footprint)
        entry = (await store.entries(team, "b", [7]))[7]
        assert (entry.team_id, entry.copy, entry.worklog_id) == (team, "b", 7)
        assert entry.state == "WORKING"
        assert entry.fingerprint is None and entry.snapshot is None
        assert entry.footprint == footprint
        await store.put_ready(team, "b", row)
        ready = (await store.entries(team, "b"))[7]
        assert ready.state == "READY" and ready.snapshot == row
        assert ready.fingerprint == snapshot_fingerprint(row) and ready.footprint is None
    asyncio.run(run())


@pg_required
def test_differing_ids_include_working_presence_and_fingerprint(store, team):
    async def run():
        for copy in ("a", "b"):
            await store.put_ready(team, copy, make_row(7, team=team))
        await store.put_ready(team, "a", make_row(8, team=team))
        await store.mark_working(team, "b", 8, EMPTY_FOOTPRINT)
        await store.put_ready(team, "a", make_row(9, team=team))
        await store.put_ready(team, "a", make_row(10, team=team, title="x"))
        await store.put_ready(team, "b", make_row(10, team=team, title="y"))
        await store.put_ready(team, "b", make_row(11, team=team))
        assert await store.differing_worklog_ids(team, read="a") == [8, 9, 10, 11]
        assert await store.differing_worklog_ids(team, read="b") == [8, 9, 10, 11]
    asyncio.run(run())


@pg_required
def test_entries_filter_remove_clear_and_team_membership(store, team):
    async def run():
        assert await store.entries(team, "a") == {}
        await store.put_ready(team, "a", make_row(7, team=team))
        await store.put_ready(team, "a", make_row(8, team=team))
        await store.mark_working(team, "b", 7, EMPTY_FOOTPRINT)
        assert set(await store.entries(team, "a", [7, 999])) == {7}
        assert await store.entries(team, "a", []) == {}
        assert await store.entries(team, "b", [8]) == {}
        assert team in await store.teams_containing(7)
        await store.remove(team, "a", 7)
        assert team in await store.teams_containing(7)
        await store.clear_copy(team, "b")
        assert team not in await store.teams_containing(7)
        assert set(await store.entries(team, "a")) == {8}
        await store.remove(team, "a", 999)
    asyncio.run(run())
