from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.config.settings import Settings
from app.light.v4.model.worklog_query import WorklogLightQueryRequest
from app.light.v4.service.lightrag_adapter import LightRagQueryFailedError
from app.light.v4.service.worklog_query_service import LightWorklogQueryService


class ScopedAdapter:
    def __init__(self) -> None:
        self.retrieval_calls = 0
        self.prompts: list[str] = []
        self.indexed_reader = fake_indexed_rows

    async def read_source_rows(self, ids, allowed_team_ids):
        return await self.indexed_reader(ids)

    async def query_data(self, options, allowed_team_ids=None):
        self.retrieval_calls += 1
        return {
            "status": "success",
            "data": {
                "chunks": [
                    {"file_path": "worklog://1", "content": "permitted chunk"},
                    {"file_path": "worklog://2", "content": "SECRET DENIED CHUNK"},
                ],
                "entities": [
                    {
                        "entity_name": "mixed concept",
                        "file_path": "worklog://1<SEP>worklog://2",
                        "description": "SECRET MIXED KG DESCRIPTION",
                    }
                ],
                "relationships": [
                    {
                        "src_id": "Worklog:1",
                        "tgt_id": "Worklog:2",
                        "description": "SECRET DENIED PREDECESSOR",
                    }
                ],
            },
        }

    async def complete_scoped(self, query: str, *, system_prompt: str, stream: bool = False):
        self.prompts.append(system_prompt)
        if stream:
            async def tokens():
                yield "safe answer"
            return tokens()
        return "safe answer"


def make_request(team_ids):
    return WorklogLightQueryRequest.model_validate(
        {"query": "吏꾪뻾?곹솴??", "allowedTeamIds": team_ids}
    )


async def fake_indexed_rows(ids):
    rows = {
        1: SimpleNamespace(
            worklog_id=1, team_id=101, title="Allowed title",
            request_content="Allowed request", work_content="Allowed work",
            author_id=11, author_name="Allowed author", team_name="Allowed team",
            tags=[], direct_predecessors=[],
        ),
        2: SimpleNamespace(
            worklog_id=2, team_id=102, title="SECRET DENIED TITLE",
            request_content="SECRET DENIED REQUEST", work_content="SECRET DENIED WORK",
            author_id=12, author_name="SECRET DENIED AUTHOR", team_name="SECRET DENIED TEAM",
            tags=[], direct_predecessors=[],
        ),
    }
    return {worklog_id: rows[worklog_id] for worklog_id in ids if worklog_id in rows}


def test_restricted_query_sends_only_authorized_original_rows_to_llm() -> None:
    adapter = ScopedAdapter()
    service = LightWorklogQueryService(
        settings_obj=Settings(_env_file=None), adapter_factory=lambda: adapter,
        team_adapter_factory=lambda: adapter,
    )

    result = asyncio.run(service.query_worklogs(make_request([101])))

    assert result.answer == "safe answer"
    assert [item.file_path for item in result.references] == ["worklog://1"]
    assert adapter.retrieval_calls == 1
    assert len(adapter.prompts) == 1
    assert "Allowed work" in adapter.prompts[0]
    assert "SECRET" not in adapter.prompts[0]
    assert "mixed concept" not in adapter.prompts[0]


def test_restricted_query_adds_only_authorized_one_hop_relations() -> None:
    adapter = ScopedAdapter()
    calls: list[list[int]] = []
    rows = {
        1: SimpleNamespace(
            worklog_id=1, team_id=101, title="Current task",
            request_content="Current request", work_content="Current work",
            author_id=11, author_name="Current author", team_name="Current team",
            tags=[SimpleNamespace(tag_id=7, tag_name="Allowed tag")],
            direct_predecessors=[
                SimpleNamespace(worklog_id=3, title="Allowed predecessor"),
                SimpleNamespace(worklog_id=4, title="SECRET predecessor title"),
            ],
        ),
        2: SimpleNamespace(
            worklog_id=2, team_id=102, title="SECRET denied candidate",
            request_content="SECRET", work_content="SECRET",
            author_id=12, author_name="SECRET author", team_name="SECRET team",
            tags=[], direct_predecessors=[],
        ),
        3: SimpleNamespace(
            worklog_id=3, team_id=103, title="Allowed predecessor",
            request_content="Predecessor request", work_content="Predecessor work",
            author_id=15, author_name="Predecessor author", team_name="Other allowed team",
            tags=[], direct_predecessors=[],
        ),
        4: SimpleNamespace(
            worklog_id=4, team_id=102, title="SECRET predecessor title",
            request_content="SECRET", work_content="SECRET",
            author_id=16, author_name="SECRET predecessor author", team_name="SECRET team",
            tags=[], direct_predecessors=[],
        ),
    }

    async def read_rows(ids: list[int]):
        calls.append(ids)
        return {worklog_id: rows[worklog_id] for worklog_id in ids if worklog_id in rows}

    adapter.indexed_reader = read_rows
    service = LightWorklogQueryService(
        settings_obj=Settings(_env_file=None), adapter_factory=lambda: adapter,
        team_adapter_factory=lambda: adapter,
    )
    result = asyncio.run(service.query_worklogs(make_request([101, 103])))

    prompt = adapter.prompts[0]
    assert "Worklog:1 DEPENDS_ON Worklog:3" in prompt
    assert "Worklog:3 AUTHORED_BY Predecessor author (User:15)" in prompt
    assert "Worklog:1 AUTHORED_BY Current author (User:11)" in prompt
    assert "Worklog:1 HAS_TAG Tag:7 (Allowed tag)" in prompt
    assert "SECRET" not in prompt
    assert "Worklog:4" not in prompt
    assert [item.file_path for item in result.references] == ["worklog://1", "worklog://3"]
    assert calls == [[1, 2], [3, 4]]


def test_restricted_query_rejects_excessive_predecessor_expansion() -> None:
    adapter = ScopedAdapter()
    calls: list[list[int]] = []

    async def read_rows(ids: list[int]):
        calls.append(ids)
        row = SimpleNamespace(
            worklog_id=1, team_id=101, title="Current task",
            request_content=None, work_content="Current work",
            author_id=11, author_name="Author", team_name="Team",
            tags=[], direct_predecessors=[
                SimpleNamespace(worklog_id=worklog_id)
                for worklog_id in range(3, 104)
            ],
        )
        return {1: row} if 1 in ids else {}

    adapter.indexed_reader = read_rows
    service = LightWorklogQueryService(
        settings_obj=Settings(_env_file=None), adapter_factory=lambda: adapter,
        team_adapter_factory=lambda: adapter,
    )
    with pytest.raises(LightRagQueryFailedError, match="relation limit exceeded"):
        asyncio.run(service.query_worklogs(make_request([101])))

    assert calls == [[1, 2]]
    assert adapter.prompts == []


def test_restricted_query_rejects_excessive_relation_context() -> None:
    adapter = ScopedAdapter()

    async def read_rows(ids: list[int]):
        row = SimpleNamespace(
            worklog_id=1, team_id=101, title="Current task",
            request_content=None, work_content="Current work",
            author_id=11, author_name="Author", team_name="Team",
            tags=[SimpleNamespace(tag_id=7, tag_name="A" * 12000)],
            direct_predecessors=[],
        )
        return {1: row} if 1 in ids else {}

    adapter.indexed_reader = read_rows
    service = LightWorklogQueryService(
        settings_obj=Settings(_env_file=None), adapter_factory=lambda: adapter,
        team_adapter_factory=lambda: adapter,
    )
    with pytest.raises(LightRagQueryFailedError, match="relation context limit exceeded"):
        asyncio.run(service.query_worklogs(make_request([101])))

    assert adapter.prompts == []


def test_empty_team_scope_does_not_retrieve_or_generate() -> None:
    adapter = ScopedAdapter()
    service = LightWorklogQueryService(
        settings_obj=Settings(_env_file=None), adapter_factory=lambda: adapter,
        team_adapter_factory=lambda: adapter,
    )

    result = asyncio.run(service.query_worklogs(make_request([])))

    assert result.answer == ""
    assert result.references == []
    assert adapter.retrieval_calls == 0
    assert adapter.prompts == []


def test_streaming_restricted_query_uses_same_authorized_context() -> None:
    adapter = ScopedAdapter()
    service = LightWorklogQueryService(
        settings_obj=Settings(_env_file=None), adapter_factory=lambda: adapter,
        team_adapter_factory=lambda: adapter,
    )

    async def collect():
        return [event async for event in service.query_worklogs_stream(make_request([101]))]

    events = asyncio.run(collect())

    assert events[0] == {"event": "token", "data": "safe answer"}
    assert events[-1]["event"] == "done"
    assert "worklog://1" in events[-1]["data"]
    assert "worklog://2" not in events[-1]["data"]
    assert "SECRET" not in adapter.prompts[0]
    assert "Worklog:1 AUTHORED_BY Allowed author (User:11)" in adapter.prompts[0]


@pytest.mark.parametrize("stream", [False, True])
def test_prefilter_uses_immutable_team_scope_and_no_postgres_before_llm(stream, monkeypatch):
    adapter = ScopedAdapter()
    retrieved_teams = []
    db_calls = []
    cache_calls = []

    class TeamPool:
        async def query_data(self, options, teams):
            assert isinstance(teams, tuple)
            retrieved_teams.append(teams)
            return {"status": "success", "data": {
                "chunks": [{"file_path": "worklog://1"}, {"file_path": "worklog://2"}],
            }}

        async def read_source_rows(self, ids, teams):
            assert teams == (101,)
            db_calls.append(ids)  # indexed-source lookup, not PostgreSQL
            return await fake_indexed_rows(ids)

    class NoCache:
        async def get_version(self):
            cache_calls.append("version")
            raise AssertionError("prefilter must not reuse legacy response cache")

    from app.light.v4.service import worklog_source_row_reader as db_module
    def unexpected_db(*args, **kwargs):
        raise AssertionError("Query must not enter PostgreSQL")
    monkeypatch.setattr(db_module, "get_session_factory", unexpected_db)
    monkeypatch.setattr(db_module.WorklogLightSourceRowReader, "fetch_source_rows", unexpected_db)

    settings = Settings(_env_file=None)
    service = LightWorklogQueryService(
        settings_obj=settings, adapter_factory=lambda: adapter,
        team_adapter_factory=TeamPool, cache=NoCache(),
    )
    request = make_request([101])

    async def run():
        if stream:
            return [event async for event in service.query_worklogs_stream(request)]
        return await service.query_worklogs(request)

    asyncio.run(run())
    assert retrieved_teams == [(101,)]
    assert db_calls == [[1, 2]]
    assert cache_calls == []
    assert adapter.retrieval_calls == 0
    assert "SECRET" not in adapter.prompts[0]
    assert "Allowed work" in adapter.prompts[0]
    assert "name (User:ID)" in adapter.prompts[0]
    assert "name (Team:ID)" in adapter.prompts[0]


def test_prefilter_failure_never_falls_back_to_global_retrieval():
    adapter = ScopedAdapter()

    class TeamPool:
        async def query_data(self, options, teams):
            raise LightRagQueryFailedError("scope failure")

    service = LightWorklogQueryService(
        settings_obj=Settings(_env_file=None),
        adapter_factory=lambda: adapter, team_adapter_factory=TeamPool,
    )
    with pytest.raises(LightRagQueryFailedError, match="scope failure"):
        asyncio.run(service.query_worklogs(make_request([101])))
    assert adapter.retrieval_calls == 0
    assert adapter.prompts == []


def test_prefilter_empty_scope_does_not_even_create_an_adapter():
    def unexpected():
        raise AssertionError("empty scope must not create an adapter")
    service = LightWorklogQueryService(
        settings_obj=Settings(_env_file=None),
        adapter_factory=unexpected, team_adapter_factory=unexpected,
    )
    result = asyncio.run(service.query_worklogs(make_request([])))
    assert result.answer == ""
