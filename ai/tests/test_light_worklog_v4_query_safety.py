"""전사 검색은 최신 DB 검증 전 답변이나 오래된 응답 캐시를 노출하지 않는다."""

import asyncio
import json

import pytest

from app.config.settings import Settings
from app.light.v4.model.worklog_query import WorklogLightQueryRequest
from app.light.v4.service.lightrag_adapter import LightRagQueryFailedError
from app.light.v4.service.worklog_query_service import LightWorklogQueryService
from tests.test_light_worklog_v4_query_service import fake_indexed_rows


class SafetyAdapter:
    def __init__(self):
        self.events = []
        self.prompts = []

    async def query_data(self, options):
        self.events.append("retrieve")
        return {"status": "success", "data": {"chunks": [
            {"file_path": "worklog://1", "content": "STALE_INDEX_BODY"},
            {"file_path": "worklog://2", "content": "DELETED_OR_INCOMPLETE_BODY"},
        ]}}

    async def complete_scoped(self, query, *, system_prompt, stream=False):
        self.events.append("generate")
        self.prompts.append(system_prompt)
        if stream:
            async def tokens():
                yield "validated answer"
            return tokens()
        return "validated answer"

    async def query_worklogs(self, options):
        raise AssertionError("Native generation bypasses DB validation")

    async def query_worklogs_stream(self, options):
        raise AssertionError("Native streaming bypasses DB validation")


@pytest.mark.parametrize("stream", [False, True])
def test_global_query_validates_before_generation_and_ignores_index_content(stream):
    adapter = SafetyAdapter()

    class SourcePool:
        async def read_source_rows(self, ids, scope):
            assert scope is None
            assert ids == [1, 2]
            adapter.events.append("validate")
            return await fake_indexed_rows([1])

    service = LightWorklogQueryService(
        settings_obj=Settings(_env_file=None), adapter_factory=lambda: adapter,
        team_adapter_factory=SourcePool,
    )
    request = WorklogLightQueryRequest(query="진행상황", allowedTeamIds=None)

    async def run():
        if stream:
            events = [event async for event in service.query_worklogs_stream(request)]
            assert events[0] == {"event": "token", "data": "validated answer"}
            references = json.loads(events[-1]["data"])["references"]
            assert [item["filePath"] for item in references] == ["worklog://1"]
        else:
            response = await service.query_worklogs(request)
            assert response.answer == "validated answer"
            assert [item.file_path for item in response.references] == ["worklog://1"]

    asyncio.run(run())
    assert adapter.events == ["retrieve", "validate", "generate"]
    assert "Allowed work" in adapter.prompts[0]
    assert "STALE_INDEX_BODY" not in adapter.prompts[0]
    assert "DELETED_OR_INCOMPLETE_BODY" not in adapter.prompts[0]


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("source_failure", [False, True])
def test_global_query_does_not_generate_without_verified_rows(stream, source_failure):
    adapter = SafetyAdapter()

    class SourcePool:
        async def read_source_rows(self, ids, scope):
            adapter.events.append("validate")
            if source_failure:
                raise LightRagQueryFailedError("DB validation failed")
            return {}

    service = LightWorklogQueryService(
        settings_obj=Settings(_env_file=None), adapter_factory=lambda: adapter,
        team_adapter_factory=SourcePool,
    )
    request = WorklogLightQueryRequest(query="진행상황", allowedTeamIds=None)

    async def run():
        if stream:
            events = [event async for event in service.query_worklogs_stream(request)]
            assert events == [{"event": "done", "data": json.dumps({"references": [], "done": True})}]
        else:
            response = await service.query_worklogs(request)
            assert response.answer == ""
            assert response.references == []

    if source_failure:
        with pytest.raises(LightRagQueryFailedError, match="DB validation failed"):
            asyncio.run(run())
    else:
        asyncio.run(run())
    assert adapter.events == ["retrieve", "validate"]
    assert adapter.prompts == []
