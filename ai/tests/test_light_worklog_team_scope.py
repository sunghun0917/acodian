"""팀 격리 인덱스 계약: 공용 엔티티/선행 관계/동시 검색을 검증한다."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.config.settings import Settings
from app.light.v4.service.lightrag_adapter import (
    LightRagConfigurationError,
    LightRagQueryFailedError,
    LightRagQueryOptions,
)
from app.light.v4.service.worklog_team_scope import LightRagTeamScopeAdapter
from app.light.v4.store.worklog_source_store import (
    LightWorklogPredecessor,
    LightWorklogSourceRow,
    LightWorklogTag,
)


class MemoryCopyStore:
    def __init__(self):
        self.data, self.pointers = {}, {}
        self.flip_after_first_read = False
        self.reads = 0

    def seed_ready(self, team, copy, row):
        from app.light.v4.store.worklog_team_copy_store import CopyEntry, snapshot_fingerprint
        self.data[team, copy, row.worklog_id] = CopyEntry(
            team, copy, row.worklog_id, "READY", snapshot_fingerprint(row), row, None,
        )

    async def read_copy(self, team):
        self.reads += 1
        if self.flip_after_first_read and self.reads > 1:
            self.pointers[team] = "b"
        return self.pointers.get(team, "a")

    async def entries(self, team, copy, worklog_ids=None):
        return {key[2]: entry for key, entry in self.data.items()
                if key[:2] == (team, copy) and (worklog_ids is None or key[2] in worklog_ids)}


def memory_locks():
    from app.light.v4.service.worklog_team_copy_lock import TeamCopyLocks
    from tests.test_light_worklog_team_copy_lock import FakeRedis, lock_settings
    return TeamCopyLocks(settings_obj=lock_settings(), client=FakeRedis())


def live_readers(scoped):
    return sum(len(tokens) for tokens in scoped._locks._redis.readers.values())


def source_row(team_id: int, worklog_id: int) -> LightWorklogSourceRow:
    """두 팀이 같은 사용자/태그를 공유하되 비밀 표식은 다른 fixture다."""
    return LightWorklogSourceRow(
        worklog_id=worklog_id, title=f"TEAM_{team_id}_TITLE", request_content=None,
        work_content=f"TEAM_{team_id}_BODY", author_id=24, author_name="Shared user",
        team_id=team_id, team_name=f"Team {team_id}",
        tags=[LightWorklogTag(tag_id=1, tag_name="Shared tag", description=None)],
        direct_predecessors=[],
    )


class FakeTeamAdapter:
    """워크스페이스별 인덱스와 검색 호출을 기록한다."""

    def __init__(self, *, settings_obj, expected_workspace, shared_llm_cache_from=None):
        self.settings = settings_obj
        self.workspace = expected_workspace
        self.shared_llm_cache_from = shared_llm_cache_from
        self.documents = []
        self.kg_documents = []
        self.rows = []
        self.calls = []
        self.closed = False
        self.prompts = []

    async def index_documents(self, documents):
        self.documents.extend(documents)

    async def index_custom_kg_documents(self, documents):
        self.kg_documents.extend(documents)

    async def get_processed_worklog_ids(self, ids, *, require_completed=True):
        self.calls.append(("read", tuple(ids)))
        indexed = {int(doc.document_id.removeprefix("worklog-")) for doc in self.documents}
        return set(ids) & indexed

    async def query_data(self, options):
        await asyncio.sleep(0)
        self.calls.append(("query", options.query))
        return {"status": "success", "data": {
            "chunks": [{"file_path": doc.file_path} for doc in self.documents],
            "entities": [{"entity_name": "User:24", "file_path": doc.file_path}
                         for doc in self.documents],
        }}

    async def delete_document(self, document_id):
        self.calls.append(("delete", document_id))

    async def close(self):
        self.closed = True

    async def complete_scoped(self, query, *, system_prompt, stream=False):
        self.prompts.append(system_prompt)
        if stream:
            async def tokens():
                yield "safe answer"
            return tokens()
        return "safe answer"


class FixtureReader:
    def __init__(self, current=None):
        self.current = current if current is not None else {1: source_row(10, 1), 2: source_row(20, 2)}

    async def fetch_source_rows(self, ids):
        return {key: row for key, row in self.current.items() if key in ids}


def pool(settings_obj=None, *, current=None):
    """외부 저장소를 초기화하지 않는 팀 adapter pool을 만든다."""
    reader, store = FixtureReader(current), MemoryCopyStore()
    for row in reader.current.values():
        store.seed_ready(row.team_id, "a", row)
    return LightRagTeamScopeAdapter(
        settings_obj=settings_obj or Settings(_env_file=None),
        adapter_factory=FakeTeamAdapter, source_reader=reader, store=store, locks=memory_locks(),
    )


def options():
    return LightRagQueryOptions(query="status?", top_k=2, chunk_top_k=2,
                                response_type="Multiple Paragraphs")


def test_source_lookup_only_reads_allowed_team_workspaces():
    scoped = pool()

    async def run():
        await scoped.index_source_rows([source_row(10, 1), source_row(20, 2)])
        assert set(await scoped.read_source_rows([1, 2], {10: "a"})) == {1}
        assert ("read", (1, 2)) not in scoped.adapter_for(20).calls
        assert set(await scoped.read_source_rows([1, 2], {10: "a", 20: "a"})) == {1, 2}
        assert await scoped.read_source_rows([1, 2], {}) == {}

    asyncio.run(run())


def test_team_workspaces_are_stable_disjoint_and_preserve_base_settings(tmp_path):
    settings = Settings(_env_file=None, lightrag_workspace="company",
                        lightrag_working_dir=str(tmp_path))
    scoped = pool(settings)
    a, b = scoped.adapter_for(10), scoped.adapter_for(20)
    assert scoped.adapter_for(10) is a
    assert a.workspace != b.workspace
    assert a.workspace != "company"
    assert a.settings.lightrag_working_dir == b.settings.lightrag_working_dir
    assert a.settings.lightrag_workspace == a.workspace
    assert settings.lightrag_workspace == "company"
    assert pool(settings).adapter_for(10).workspace == a.workspace
    assert pool(settings.model_copy(update={"lightrag_workspace": "other"})).adapter_for(10).workspace != a.workspace
    assert pool(settings.model_copy(update={"lightrag_embedding_model": "other-model"})).adapter_for(10).workspace != a.workspace
    assert pool(settings.model_copy(update={"embedding_dim": 512})).adapter_for(10).workspace != a.workspace


def test_pool_settings_do_not_change_after_construction(tmp_path):
    settings = Settings(_env_file=None, lightrag_workspace="company", lightrag_working_dir=str(tmp_path))
    scoped = pool(settings)
    settings.lightrag_working_dir = "changed-directory"
    assert scoped.adapter_for(10).settings.lightrag_working_dir == str(tmp_path)


def test_copy_b_workspace_has_suffix_and_a_keeps_current_name():
    scoped = pool()
    current = scoped.adapter_for(3).workspace
    b = scoped.adapter_for(3, "b")
    assert scoped.adapter_for(3, "a").workspace == current
    assert b.workspace == current + "_b"
    assert scoped.adapter_for(3, "b") is b
    assert b.shared_llm_cache_from is scoped.adapter_for(3, "a")
    assert scoped.adapter_for(4, "b").shared_llm_cache_from is scoped.adapter_for(4, "a")
    assert scoped.adapter_for(3, "a").shared_llm_cache_from is None


@pytest.mark.parametrize("copy", ["c", "", None, True])
def test_team_scope_rejects_invalid_copy(copy):
    with pytest.raises(LightRagConfigurationError):
        pool().adapter_for(3, copy)


@pytest.mark.parametrize("team_id", [0, -1, True, "10", None])
def test_team_scope_rejects_invalid_ids(team_id):
    with pytest.raises(LightRagConfigurationError):
        pool().adapter_for(team_id)


def test_index_separates_shared_entities_and_never_copies_cross_team_predecessors():
    scoped = pool()
    a = replace(source_row(10, 1), direct_predecessors=[
        LightWorklogPredecessor(worklog_id=2, title="TEAM_20_SECRET", team_id=20),
        LightWorklogPredecessor(worklog_id=3, title="TEAM_10_PREDECESSOR", team_id=10),
        LightWorklogPredecessor(worklog_id=4, title="UNKNOWN_TEAM_SECRET"),
    ])
    b = source_row(20, 2)
    asyncio.run(scoped.index_source_rows([a, b]))
    aa, bb = scoped.adapter_for(10), scoped.adapter_for(20)
    assert [doc.document_id for doc in aa.documents] == ["worklog-1"]
    assert [doc.document_id for doc in bb.documents] == ["worklog-2"]
    kg = aa.kg_documents[0].custom_kg
    assert "TEAM_20_SECRET" not in str(kg)
    assert "UNKNOWN_TEAM_SECRET" not in str(kg)
    assert "TEAM_10_PREDECESSOR" in str(kg)


def test_queries_for_different_team_scopes_do_not_mix_under_concurrency():
    scoped = pool()

    async def run():
        await scoped.index_source_rows([source_row(10, 1), source_row(20, 2)])
        return await asyncio.gather(
            scoped.query_data(options(), {10: "a"}),
            scoped.query_data(options(), {20: "a"}),
            scoped.query_data(options(), {20: "a", 10: "a"}),
        )

    a, b, both = asyncio.run(run())
    assert a["data"]["chunks"] == [{"file_path": "worklog://1"}]
    assert b["data"]["chunks"] == [{"file_path": "worklog://2"}]
    assert {item["file_path"] for item in both["data"]["chunks"]} == {"worklog://1", "worklog://2"}
    assert scoped.adapter_for(10).calls.count(("query", "status?")) == 2
    assert scoped.adapter_for(20).calls.count(("query", "status?")) == 2


def test_empty_scope_does_not_create_or_call_an_adapter():
    def unexpected(**kwargs):
        raise AssertionError("empty scope must not initialize storage")
    scoped = LightRagTeamScopeAdapter(settings_obj=Settings(_env_file=None),
                                     adapter_factory=unexpected)
    assert asyncio.run(scoped.query_data(options(), {})) == {
        "status": "success", "data": {"chunks": [], "entities": [],
                                       "relationships": [], "references": []},
    }


def test_failed_team_query_is_not_retried_as_global_query():
    scoped = pool()
    async def reject(options):
        raise LightRagQueryFailedError("storage unavailable")
    scoped.adapter_for(10).query_data = reject
    with pytest.raises(LightRagQueryFailedError, match="unavailable"):
        asyncio.run(scoped.query_data(options(), {10: "a"}))



def test_close_finalizes_all_initialized_team_adapters():
    scoped = pool()
    a, b = scoped.adapter_for(10), scoped.adapter_for(20)
    asyncio.run(scoped.close())
    assert a.closed and b.closed


def test_one_team_with_no_results_does_not_fail_other_allowed_teams():
    scoped = pool()

    async def no_results(options):
        return {"status": "failure", "data": {}, "metadata": {"failure_reason": "no_results"}}
    scoped.adapter_for(10).query_data = no_results

    async def run():
        await scoped.index_source_rows([source_row(20, 2)])
        return await scoped.query_data(options(), {10: "a", 20: "a"})
    result = asyncio.run(run())
    assert result["data"]["chunks"] == [{"file_path": "worklog://2"}]


def test_kg_failure_does_not_publish_team_schema():
    scoped = pool()
    adapter = scoped.adapter_for(10)

    async def fail(documents):
        raise RuntimeError("KG insert failed")
    adapter.index_custom_kg_documents = fail
    with pytest.raises(RuntimeError, match="KG insert failed"):
        asyncio.run(scoped.index_source_rows([source_row(10, 1)]))
    assert not any(call[0] == "publish" for call in adapter.calls)


def test_context_uses_read_copy_snapshot_not_current_source():
    scoped = pool(current={7: source_row(3, 7)})
    scoped._source_reader.current[7] = replace(source_row(3, 7), title="CURRENT_BODY", author_name="CURRENT_AUTHOR")
    async def run():
        async with scoped.read_copies((3,)) as copies:
            result = await scoped.read_source_rows([7], copies)
            assert result[7].title == "TEAM_3_TITLE"
            assert result[7].author_name == "Shared user"
    asyncio.run(run())


def test_working_or_absent_is_hidden():
    scoped = pool(current={7: source_row(3, 7), 8: source_row(3, 8)})
    entry = scoped._store.data[3, "a", 7]
    scoped._store.data[3, "a", 7] = replace(entry, state="WORKING", snapshot=None, fingerprint=None)
    del scoped._store.data[3, "a", 8]
    async def run():
        async with scoped.read_copies((3,)) as copies:
            assert await scoped.read_source_rows([7, 8], copies) == {}
    asyncio.run(run())


def test_deleted_in_source_is_hidden_before_index_delete():
    scoped = pool(current={7: source_row(3, 7)})
    scoped._source_reader.current.clear()
    async def run():
        async with scoped.read_copies((3,)) as copies:
            assert await scoped.read_source_rows([7], copies) == {}
    asyncio.run(run())


def test_team_moved_worklog_is_hidden_from_old_team():
    scoped = pool(current={7: source_row(3, 7)})
    scoped._source_reader.current[7] = source_row(4, 7)
    async def run():
        async with scoped.read_copies((3,)) as copies:
            assert await scoped.read_source_rows([7], copies) == {}
    asyncio.run(run())


@pytest.mark.parametrize("new_team,old_team", [(3, 4), (4, 3)])
def test_team_move_keeps_current_team_snapshot_for_director(new_team, old_team):
    scoped = pool(current={7: source_row(new_team, 7)})
    scoped._store.seed_ready(new_team, "a", replace(source_row(new_team, 7), title="new"))
    scoped._store.seed_ready(old_team, "a", replace(source_row(old_team, 7), title="old"))
    async def run():
        async with scoped.read_copies((3, 4)) as copies:
            assert (await scoped.read_source_rows([7], copies))[7].title == "new"
    asyncio.run(run())


def test_source_failure_does_not_fallback_to_graph():
    scoped = pool()
    async def fail(ids):
        raise RuntimeError("source unavailable")
    scoped._source_reader.fetch_source_rows = fail
    with pytest.raises(LightRagQueryFailedError, match="source lookup failed"):
        asyncio.run(scoped.read_source_rows([1], {10: "a"}))


def test_source_identity_mismatch_fails_closed():
    scoped = pool()
    scoped._source_reader.current[1] = source_row(10, 7)
    with pytest.raises(LightRagQueryFailedError):
        asyncio.run(scoped.read_source_rows([1], {10: "a"}))


def make_query_service(scoped, settings=None):
    from app.light.v4.service.worklog_query_service import LightWorklogQueryService
    return LightWorklogQueryService(settings_obj=settings or Settings(_env_file=None),
                                   team_adapter_factory=lambda: scoped)


def query_request(teams):
    from app.light.v4.model.worklog_query import WorklogLightQueryRequest
    return WorklogLightQueryRequest(query="status?", allowedTeamIds=teams)


@pytest.mark.parametrize("flip", [False, True])
def test_query_searches_only_read_copy(flip):
    scoped = pool(current={7: source_row(3, 7)})
    scoped._store.flip_after_first_read = flip
    scoped._store.seed_ready(3, "b", replace(source_row(3, 7), title="B_SNAPSHOT"))
    async def run():
        for copy in ("a", "b"):
            await scoped.adapter_for(3, copy).index_documents([
                SimpleNamespace(file_path="worklog://7"),
            ])
        await make_query_service(scoped).query_worklogs(query_request([3]))
        chosen = "b" if flip else "a"
        assert scoped.adapter_for(3, chosen).calls == [("query", "status?")]
        assert scoped.adapter_for(3, "a" if flip else "b").calls == []
        assert live_readers(scoped) == 0
    asyncio.run(run())


def test_all_team_query_never_touches_global_and_uses_smallest_team_copy_for_llm(monkeypatch):
    from app.light.v4.service import lightrag_adapter
    def unexpected():
        raise AssertionError("global adapter must never be used")
    monkeypatch.setattr(lightrag_adapter, "get_lightrag_worklog_index_adapter", unexpected)
    scoped = pool()
    scoped._store.pointers[10] = "b"
    scoped._store.seed_ready(10, "b", source_row(10, 1))
    async def run():
        await scoped.adapter_for(10, "b").index_documents([
            SimpleNamespace(file_path="worklog://1"),
        ])
        await make_query_service(scoped).query_worklogs(query_request([20, 10, 30]))
        for team, copy in ((10, "b"), (20, "a"), (30, "a")):
            assert scoped.adapter_for(team, copy).calls == [("query", "status?")]
        assert len(scoped.adapter_for(10, "b").prompts) == 1
        assert scoped.adapter_for(20, "a").prompts == []
    asyncio.run(run())


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("predecessor", ["ready", "absent", "working", "moved", "deleted", "outside"])
def test_predecessor_uses_its_own_copy_snapshot_and_permission(stream, predecessor):
    main = replace(source_row(3, 7), direct_predecessors=[LightWorklogPredecessor(5, "STALE_PREDECESSOR", 3)])
    scoped = pool(current={7: main, 5: source_row(3, 5)})
    scoped._store.pointers[3] = "b"
    scoped._store.seed_ready(3, "b", main)
    pred = replace(source_row(3, 5), title="SNAPSHOT_PREDECESSOR")
    if predecessor not in ("absent", "working"):
        scoped._store.seed_ready(3, "b", pred)
    if predecessor == "working":
        scoped._store.seed_ready(3, "b", pred)
        scoped._store.data[3, "b", 5] = replace(scoped._store.data[3, "b", 5], state="WORKING")
    if predecessor in ("moved", "outside"):
        scoped._source_reader.current[5] = source_row(4, 5)
    if predecessor == "deleted":
        del scoped._source_reader.current[5]
    adapter = scoped.adapter_for(3, "b")
    async def run():
        await adapter.index_documents([SimpleNamespace(file_path="worklog://7")])
        original = adapter.complete_scoped
        async def complete(*args, **kwargs):
            assert live_readers(scoped) == 0
            return await original(*args, **kwargs)
        adapter.complete_scoped = complete
        service = make_query_service(scoped)
        if stream:
            import inspect
            iterator = service.query_worklogs_stream(query_request([3]))
            assert inspect.isasyncgen(iterator)
            assert (await anext(iterator))["event"] == "token"
            assert live_readers(scoped) == 0
            await iterator.aclose()
        else:
            await service.query_worklogs(query_request([3]))
        prompt = adapter.prompts[0]
        assert "TEAM_3_BODY" in prompt and "STALE_PREDECESSOR" not in prompt
        assert ("DEPENDS_ON Worklog:5 (SNAPSHOT_PREDECESSOR)" in prompt) == (predecessor == "ready")
    asyncio.run(run())


@pytest.mark.parametrize("stage", ["register", "search", "snapshot", "permission"])
@pytest.mark.parametrize("cancel", [False, True])
def test_reader_tokens_released_on_exception_or_cancellation(stage, cancel):
    scoped = pool()
    entered = asyncio.Event()
    async def interrupt(*args, **kwargs):
        entered.set()
        if cancel:
            await asyncio.Event().wait()
        raise RuntimeError("injected failure")
    if stage == "register":
        original_read = scoped._store.read_copy
        async def read(team):
            if team == 20:
                return await interrupt()
            return await original_read(team)
        scoped._store.read_copy = read
    elif stage == "search":
        scoped.adapter_for(10).query_data = interrupt
    elif stage == "snapshot":
        scoped._store.entries = interrupt
    else:
        scoped._source_reader.fetch_source_rows = interrupt
    async def run():
        await scoped.index_source_rows([source_row(10, 1), source_row(20, 2)])
        task = asyncio.create_task(make_query_service(scoped).query_worklogs(query_request([10, 20])))
        if cancel:
            await entered.wait()
            task.cancel()
        with pytest.raises((asyncio.CancelledError, RuntimeError, LightRagQueryFailedError)):
            await task
        assert live_readers(scoped) == 0
    asyncio.run(run())


def test_failed_search_cancels_siblings_before_releasing_tokens():
    scoped = pool()
    entered = asyncio.Event()
    async def pending(options):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            assert live_readers(scoped) == 2
    async def fail(options):
        await entered.wait()
        raise LightRagQueryFailedError("failed retrieval")
    scoped.adapter_for(10).query_data = pending
    scoped.adapter_for(20).query_data = fail
    with pytest.raises(LightRagQueryFailedError):
        asyncio.run(make_query_service(scoped).query_worklogs(query_request([10, 20])))
    assert live_readers(scoped) == 0


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("stage", ["register", "search", "snapshot", "permission"])
def test_context_deadline_covers_all_read_stages_and_releases_tokens(stream, stage):
    from app.light.v4.service.lightrag_adapter import LightRagQueryTimeoutError
    scoped = pool()
    settings = Settings(_env_file=None).model_copy(update={"lightrag_query_timeout_seconds": 0.02})
    async def hang(*args, **kwargs):
        assert live_readers(scoped) == (1 if stage == "register" else 2)
        await asyncio.Event().wait()
    if stage == "register":
        original = scoped._store.read_copy
        async def read(team):
            return await hang() if team == 20 else await original(team)
        scoped._store.read_copy = read
    elif stage == "search":
        scoped.adapter_for(10).query_data = hang
    elif stage == "snapshot":
        scoped._store.entries = hang
    else:
        scoped._source_reader.fetch_source_rows = hang
    async def run():
        await scoped.index_source_rows([source_row(10, 1), source_row(20, 2)])
        service = make_query_service(scoped, settings)
        with pytest.raises(LightRagQueryTimeoutError):
            if stream:
                _ = [event async for event in service.query_worklogs_stream(query_request([10, 20]))]
            else:
                await service.query_worklogs(query_request([10, 20]))
        assert live_readers(scoped) == 0
    asyncio.run(run())


def test_context_uses_one_deadline_across_search_and_permission():
    from app.light.v4.service.lightrag_adapter import LightRagQueryTimeoutError
    scoped = pool()
    settings = Settings(_env_file=None).model_copy(update={"lightrag_query_timeout_seconds": 0.06})
    adapter = scoped.adapter_for(10)
    query, read = adapter.query_data, scoped._source_reader.fetch_source_rows
    async def slow_query(options):
        await asyncio.sleep(0.04)
        return await query(options)
    async def slow_read(ids):
        await asyncio.sleep(0.04)
        return await read(ids)
    adapter.query_data = slow_query
    scoped._source_reader.fetch_source_rows = slow_read
    async def run():
        await scoped.index_source_rows([source_row(10, 1)])
        with pytest.raises(LightRagQueryTimeoutError):
            await make_query_service(scoped, settings).query_worklogs(query_request([10]))
        assert live_readers(scoped) == 0
    asyncio.run(run())


def test_read_copy_registration_failure_maps_to_query_failed():
    scoped = pool()
    async def fail(team):
        raise RuntimeError("pointer database unavailable")
    scoped._store.read_copy = fail
    with pytest.raises(LightRagQueryFailedError):
        asyncio.run(make_query_service(scoped).query_worklogs(query_request([10])))
    assert live_readers(scoped) == 0


@pytest.mark.parametrize("stream", [False, True])
def test_pointer_flip_during_search_keeps_borrowed_snapshot_until_context_is_built(stream):
    scoped = pool(current={7: source_row(3, 7)})
    scoped._store.seed_ready(3, "b", replace(source_row(3, 7), work_content="NEW_COPY_BODY"))
    adapter = scoped.adapter_for(3, "a")
    original_query = adapter.query_data
    async def flip(options):
        assert live_readers(scoped) == 1
        scoped._store.pointers[3] = "b"
        scoped._source_reader.current[7] = replace(source_row(3, 7), work_content="CURRENT_DB_BODY")
        return await original_query(options)
    adapter.query_data = flip
    async def run():
        await adapter.index_documents([SimpleNamespace(file_path="worklog://7")])
        service = make_query_service(scoped)
        if stream:
            _ = [event async for event in service.query_worklogs_stream(query_request([3]))]
        else:
            await service.query_worklogs(query_request([3]))
        assert live_readers(scoped) == 0
        assert "TEAM_3_BODY" in adapter.prompts[0]
        assert "NEW_COPY_BODY" not in adapter.prompts[0]
        assert "CURRENT_DB_BODY" not in adapter.prompts[0]
        assert scoped.adapter_for(3, "b").prompts == []
    asyncio.run(run())
