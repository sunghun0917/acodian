from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.config.settings import Settings
from app.light.v3.model.worklog_query import WorklogLightQueryRequest
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
        ),
        2: SimpleNamespace(
            worklog_id=2, team_id=102, title="SECRET DENIED TITLE",
            request_content="SECRET DENIED REQUEST", work_content="SECRET DENIED WORK",
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
