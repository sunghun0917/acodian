"""팀 격리 인덱스 계약: 공용 엔티티/선행 관계/동시 검색을 검증한다."""

import asyncio
from dataclasses import replace

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

    def __init__(self, *, settings_obj, expected_workspace):
        self.settings = settings_obj
        self.workspace = expected_workspace
        self.documents = []
        self.kg_documents = []
        self.rows = []
        self.calls = []
        self.closed = False

    async def index_documents(self, documents):
        self.documents.extend(documents)

    async def index_custom_kg_documents(self, documents):
        self.kg_documents.extend(documents)

    async def get_processed_worklog_ids(self, ids):
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


class FixtureReader:
    async def fetch_source_rows(self, ids):
        current = {1: source_row(10, 1), 2: source_row(20, 2)}
        return {key: row for key, row in current.items() if key in ids}


def pool(settings_obj=None):
    """외부 저장소를 초기화하지 않는 팀 adapter pool을 만든다."""
    return LightRagTeamScopeAdapter(
        settings_obj=settings_obj or Settings(_env_file=None),
        adapter_factory=FakeTeamAdapter, source_reader=FixtureReader(),
    )


def options():
    return LightRagQueryOptions(query="status?", top_k=2, chunk_top_k=2,
                                response_type="Multiple Paragraphs")


def test_source_lookup_only_reads_allowed_team_workspaces():
    scoped = pool()

    async def run():
        await scoped.index_source_rows([source_row(10, 1), source_row(20, 2)])
        assert set(await scoped.read_source_rows([1, 2], (10,))) == {1}
        assert ("read", (1, 2)) not in scoped.adapter_for(20).calls
        assert set(await scoped.read_source_rows([1, 2], (10, 20))) == {1, 2}
        assert await scoped.read_source_rows([1, 2], ()) == {}

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
            scoped.query_data(options(), (10,)),
            scoped.query_data(options(), (20,)),
            scoped.query_data(options(), (20, 10, 10)),
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
    assert asyncio.run(scoped.query_data(options(), ())) == {
        "status": "success", "data": {"chunks": [], "entities": [],
                                       "relationships": [], "references": []},
    }


def test_failed_team_query_is_not_retried_as_global_query():
    scoped = pool()
    async def reject(options):
        raise LightRagQueryFailedError("storage unavailable")
    scoped.adapter_for(10).query_data = reject
    with pytest.raises(LightRagQueryFailedError, match="unavailable"):
        asyncio.run(scoped.query_data(options(), (10,)))



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
        return await scoped.query_data(options(), (10, 20))
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


def test_source_lookup_uses_fresh_rows_and_matching_team_status():
    class Reader:
        async def fetch_source_rows(self, ids):
            return {1: replace(source_row(10, 1), work_content="LATEST_DB_BODY"),
                    2: source_row(20, 2)}
    scoped = LightRagTeamScopeAdapter(settings_obj=Settings(_env_file=None),
                                     adapter_factory=FakeTeamAdapter, source_reader=Reader())
    async def processed(ids):
        return {1, 2}
    scoped.adapter_for(10).get_processed_worklog_ids = processed
    async def run():
        result = await scoped.read_source_rows([1, 2], (10,))
        assert result[1].work_content == "LATEST_DB_BODY"
        assert 2 not in result
    asyncio.run(run())


def test_source_lookup_excludes_missing_and_unprocessed():
    class Reader:
        async def fetch_source_rows(self, ids):
            assert ids == [1]
            return {1: source_row(10, 1)}
    scoped = LightRagTeamScopeAdapter(settings_obj=Settings(_env_file=None),
                                     adapter_factory=FakeTeamAdapter, source_reader=Reader())
    async def processed(ids):
        return {1}
    scoped.adapter_for(10).get_processed_worklog_ids = processed
    assert set(asyncio.run(scoped.read_source_rows([1, 2, 3], (10,)))) == {1}


def test_source_failure_does_not_fallback_to_graph():
    class Reader:
        async def fetch_source_rows(self, ids):
            raise RuntimeError("source unavailable")
    scoped = LightRagTeamScopeAdapter(settings_obj=Settings(_env_file=None),
                                     adapter_factory=FakeTeamAdapter, source_reader=Reader())
    async def processed(ids):
        return {1}
    scoped.adapter_for(10).get_processed_worklog_ids = processed
    with pytest.raises(LightRagQueryFailedError):
        asyncio.run(scoped.read_source_rows([1], (10,)))


def test_moved_row_requires_processed_status_in_current_team():
    class Reader:
        async def fetch_source_rows(self, ids):
            return {1: source_row(20, 1)}
    scoped = LightRagTeamScopeAdapter(settings_obj=Settings(_env_file=None),
                                     adapter_factory=FakeTeamAdapter, source_reader=Reader())
    async def old(ids):
        return {1}
    async def new(ids):
        return set()
    scoped.adapter_for(10).get_processed_worklog_ids = old
    scoped.adapter_for(20).get_processed_worklog_ids = new
    assert asyncio.run(scoped.read_source_rows([1], (10, 20))) == {}


def test_latest_source_and_moved_predecessor_are_filtered_in_normal_and_sse():
    from app.light.v4.model.worklog_query import WorklogLightQueryRequest
    from app.light.v4.service.worklog_query_service import LightWorklogQueryService

    current = {
        1: replace(source_row(10, 1), direct_predecessors=[LightWorklogPredecessor(2, "OLD_PREDECESSOR", 10)]),
        2: source_row(10, 2),
    }
    class Reader:
        async def fetch_source_rows(self, ids):
            return {key: row for key, row in current.items() if key in ids}
    class Generator:
        def __init__(self):
            self.prompts = []

        async def complete_scoped(self, query, *, system_prompt, stream=False):
            self.prompts.append(system_prompt)
            if stream:
                async def tokens():
                    yield "safe answer"
                return tokens()
            return "safe answer"

    scoped = LightRagTeamScopeAdapter(settings_obj=Settings(_env_file=None),
                                     adapter_factory=FakeTeamAdapter, source_reader=Reader())
    generator = Generator()
    service = LightWorklogQueryService(settings_obj=Settings(_env_file=None),
                                      adapter_factory=lambda: generator, team_adapter_factory=lambda: scoped)
    request = WorklogLightQueryRequest.model_validate({"query": "status?", "allowedTeamIds": [10]})
    async def run():
        await scoped.index_source_rows(list(current.values()))
        current[1] = replace(current[1], work_content="LATEST_SOURCE_BODY")
        current[2] = source_row(20, 2)
        response = await service.query_worklogs(request)
        assert [ref.file_path for ref in response.references] == ["worklog://1"]
        events = [event async for event in service.query_worklogs_stream(request)]
        assert events[-1]["event"] == "done"
        assert len(generator.prompts) == 2
        for prompt in generator.prompts:
            assert "LATEST_SOURCE_BODY" in prompt
            assert "TEAM_20" not in prompt and "OLD_PREDECESSOR" not in prompt
            assert "DEPENDS_ON" not in prompt
    asyncio.run(run())
