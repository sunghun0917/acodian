from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.config.settings import Settings
from app.light.v3.model.worklog_query import WorklogLightQueryRequest
from app.light.v3.service.lightrag_adapter import LightRagQueryFailedError
from app.light.v3.service.worklog_query_service import LightWorklogQueryService


class ScopedAdapter:
    def __init__(self) -> None:
        self.retrieval_calls = 0
        self.prompts: list[str] = []

    async def query_data(self, options):
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
        {"query": "진행상황은?", "allowedTeamIds": team_ids}
    )


async def fake_source_reader(ids):
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
        source_reader=fake_source_reader,
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

    service = LightWorklogQueryService(
        settings_obj=Settings(_env_file=None), adapter_factory=lambda: adapter,
        source_reader=read_rows,
    )
    result = asyncio.run(service.query_worklogs(make_request([101, 103])))

    prompt = adapter.prompts[0]
    assert "Worklog:1 DEPENDS_ON Worklog:3" in prompt
    assert "Worklog:3 AUTHORED_BY User:15 (Predecessor author)" in prompt
    assert "Worklog:1 AUTHORED_BY User:11 (Current author)" in prompt
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

    service = LightWorklogQueryService(
        settings_obj=Settings(_env_file=None), adapter_factory=lambda: adapter,
        source_reader=read_rows,
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

    service = LightWorklogQueryService(
        settings_obj=Settings(_env_file=None), adapter_factory=lambda: adapter,
        source_reader=read_rows,
    )
    with pytest.raises(LightRagQueryFailedError, match="relation context limit exceeded"):
        asyncio.run(service.query_worklogs(make_request([101])))

    assert adapter.prompts == []


def test_empty_team_scope_does_not_retrieve_or_generate() -> None:
    adapter = ScopedAdapter()
    service = LightWorklogQueryService(
        settings_obj=Settings(_env_file=None), adapter_factory=lambda: adapter,
        source_reader=fake_source_reader,
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
        source_reader=fake_source_reader,
    )

    async def collect():
        return [event async for event in service.query_worklogs_stream(make_request([101]))]

    events = asyncio.run(collect())

    assert events[0] == {"event": "token", "data": "safe answer"}
    assert events[-1]["event"] == "done"
    assert "worklog://1" in events[-1]["data"]
    assert "worklog://2" not in events[-1]["data"]
    assert "SECRET" not in adapter.prompts[0]
    assert "Worklog:1 AUTHORED_BY User:11" in adapter.prompts[0]
