"""문서별 색인 보상과 재시도·완료 게이트 계약."""

import asyncio

import pytest

from app.light.v4.service import worklog_index_service as module
from app.light.v4.service.lightrag_adapter import (
    LightRagDeleteFailedError,
    LightRagInsertFailedError,
    LightRagInsertTimeoutError,
)
from tests.test_light_worklog_v4_index_service import make_source_row


class Workspace:
    def __init__(self, name, calls, failure=None):
        self.name, self.calls, self.failure = name, calls, failure
        self.documents = set()
        self.states = {}
        self.owners = {}

    def fail(self, phase, document_id):
        if self.failure == (phase, document_id):
            raise LightRagInsertFailedError(phase)

    async def get_document_team_id(self, document_id, *, allow_missing=False):
        return self.owners.get(document_id)

    async def set_document_indexing_status(self, document_id, state):
        self.calls.append((self.name, state, document_id))
        self.fail(state, document_id)
        self.states[document_id] = state

    async def delete_document(self, document_id):
        self.calls.append((self.name, "delete", document_id))
        if self.failure == ("delete", document_id) and document_id in self.documents:
            raise LightRagDeleteFailedError("delete")
        self.documents.discard(document_id)
        self.states.pop(document_id, None)
        self.owners.pop(document_id, None)

    async def index_documents(self, documents):
        for doc in documents:
            self.calls.append((self.name, "body", doc.document_id))
            assert doc.document_id not in self.documents, "native skips existing IDs"
            self.documents.add(doc.document_id)
            self.fail("body", doc.document_id)

    async def index_custom_kg_documents(self, documents):
        for doc in documents:
            self.calls.append((self.name, "kg", doc.document_id))
            self.fail("kg", doc.document_id)

    async def save_indexed_team_ids(self, teams):
        for worklog_id, team_id in teams.items():
            document_id = f"worklog-{worklog_id}"
            self.calls.append((self.name, "ownership", document_id))
            self.fail("ownership", document_id)
            self.owners[document_id] = team_id


def setup(monkeypatch, failure=None, team_failure=None):
    calls = []
    global_adapter = Workspace("global", calls, failure)
    team = Workspace("team", calls, team_failure)

    class Reader:
        async def fetch_source_rows(self, ids):
            return {item: make_source_row(item) for item in ids}

    class Pool:
        def adapter_for(self, team_id):
            assert team_id == 7
            return team

        async def index_source_rows(self, rows):
            from app.light.v4.service.worklog_document_builder import build_worklog_light_document
            from app.light.v4.service.worklog_source_reader import to_light_worklog_source
            from app.light.v4.service.worklog_custom_kg_builder import build_worklog_custom_kg_document
            from app.light.v4.service.worklog_custom_kg_source_reader import to_light_worklog_custom_kg_source
            await team.index_documents([build_worklog_light_document(to_light_worklog_source(r)) for r in rows])
            await team.index_custom_kg_documents([
                build_worklog_custom_kg_document(to_light_worklog_custom_kg_source(r)) for r in rows
            ])

    class Cache:
        async def bump_version(self):
            calls.append(("cache", "bump", None))

    monkeypatch.setattr(module, "WorklogLightSourceRowReader", Reader)
    monkeypatch.setattr(module, "get_lightrag_worklog_index_adapter", lambda: global_adapter)
    monkeypatch.setattr(module, "get_lightrag_team_scope_adapter", Pool)
    return module.LightWorklogIndexService(cache=Cache()), global_adapter, team, calls


def test_default_index_service_does_not_construct_response_cache(monkeypatch):
    def unexpected(**kwargs):
        raise AssertionError("disabled response cache must not be constructed")

    monkeypatch.setattr(module, "WorklogQueryCache", unexpected)
    service = module.LightWorklogIndexService()
    assert service._cache is None


def test_index_and_delete_work_without_response_cache(monkeypatch):
    service, adapter, team, calls = setup(monkeypatch)
    service._cache = None
    assert asyncio.run(service.index_worklogs([101])).items[0].indexed
    assert asyncio.run(service.delete_worklogs([101])).items[0].deleted


@pytest.mark.parametrize("workspace,phase", [
    ("global", "body"), ("global", "kg"), ("global", "ownership"),
    ("team", "body"), ("team", "kg"), ("global", "COMPLETED"),
])
def test_failed_phase_is_compensated_without_failing_other_documents(monkeypatch, workspace, phase):
    failure = (phase, "worklog-101")
    service, adapter, team, calls = setup(
        monkeypatch, failure if workspace == "global" else None,
        failure if workspace == "team" else None,
    )
    response = asyncio.run(service.index_worklogs([101, 102]))
    assert [(item.indexed, item.error) for item in response.items] == [
        (False, "LIGHTRAG_INSERT_FAILED"), (True, None),
    ]
    assert adapter.documents == team.documents == {"worklog-102"}
    assert adapter.states["worklog-102"] == "COMPLETED"
    failure_position = calls.index((workspace, phase, "worklog-101"))
    cleanup = [item[:2] for item in calls[failure_position + 1:]
               if item[1] == "delete" and item[2] == "worklog-101"]
    assert cleanup == ([("team", "delete")] if workspace == "team" or phase == "COMPLETED" else []) + [
        ("global", "delete"),
    ]


def test_failed_team_compensation_preserves_global_ownership_and_retry_starts_clean(monkeypatch):
    service, adapter, team, calls = setup(monkeypatch, team_failure=("kg", "worklog-101"))
    original_delete = team.delete_document

    async def fail_cleanup(document_id):
        if document_id in team.documents:
            raise LightRagDeleteFailedError("cleanup unavailable")
        await original_delete(document_id)

    team.delete_document = fail_cleanup
    failed = asyncio.run(service.index_worklogs([101]))
    assert failed.items[0].error == "LIGHTRAG_COMPENSATION_FAILED"
    assert adapter.documents == team.documents == {"worklog-101"}
    assert adapter.owners["worklog-101"] == 7
    assert adapter.states.get("worklog-101") != "COMPLETED"
    team.delete_document = original_delete
    team.failure = None
    retried = asyncio.run(service.index_worklogs([101]))
    assert retried.items[0].indexed
    assert adapter.documents == team.documents == {"worklog-101"}
    assert adapter.states["worklog-101"] == "COMPLETED"


def test_completion_is_published_only_after_all_workspaces_succeed(monkeypatch):
    service, adapter, team, calls = setup(monkeypatch)
    response = asyncio.run(service.index_worklogs([101]))
    assert response.items[0].indexed
    completed = calls.index(("global", "COMPLETED", "worklog-101"))
    assert completed > calls.index(("team", "kg", "worklog-101"))
    assert completed > calls.index(("global", "ownership", "worklog-101"))


def test_timeout_preserves_insert_error_after_successful_compensation(monkeypatch):
    service, adapter, team, calls = setup(monkeypatch)

    async def timeout(documents):
        adapter.documents.add(documents[0].document_id)
        raise LightRagInsertTimeoutError("partial write timed out")

    adapter.index_documents = timeout
    response = asyncio.run(service.index_worklogs([101]))
    assert response.items[0].error == "LIGHTRAG_INSERT_TIMEOUT"
    assert not adapter.documents and not team.documents


def test_completion_response_loss_requires_gate_close_before_deletion(monkeypatch):
    service, adapter, team, calls = setup(monkeypatch)

    async def lost_completion(document_id, state):
        if state == "COMPLETED":
            adapter.states[document_id] = state
            raise LightRagInsertFailedError("response lost after commit")
        if adapter.states.get(document_id) == "COMPLETED":
            raise LightRagInsertFailedError("cannot close gate")

    adapter.set_document_indexing_status = lost_completion
    response = asyncio.run(service.index_worklogs([101]))
    assert response.items[0].error == "LIGHTRAG_COMPENSATION_FAILED"
    assert adapter.documents == team.documents == {"worklog-101"}
    assert adapter.states["worklog-101"] == "COMPLETED"


def test_cancelled_index_waits_for_compensation_before_releasing_mutation_lock(monkeypatch):
    service, adapter, team, calls = setup(monkeypatch)

    async def run():
        started, cleanup_started, release_cleanup = asyncio.Event(), asyncio.Event(), asyncio.Event()
        original_delete = team.delete_document

        async def cancelled_team_index(rows):
            team.documents.add("worklog-101")
            started.set()
            await asyncio.Future()

        async def blocked_cleanup(document_id):
            if document_id in team.documents:
                cleanup_started.set()
                await release_cleanup.wait()
            await original_delete(document_id)

        pool = module.get_lightrag_team_scope_adapter()
        pool.index_source_rows = cancelled_team_index
        monkeypatch.setattr(module, "get_lightrag_team_scope_adapter", lambda: pool)
        team.delete_document = blocked_cleanup
        task = asyncio.create_task(service.index_worklogs([101]))
        await started.wait()
        task.cancel()
        await cleanup_started.wait()
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release_cleanup.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not adapter.documents and not team.documents

    async def bounded():
        await asyncio.wait_for(run(), timeout=2)

    asyncio.run(bounded())


def test_service_dependency_injection_uses_supplied_factories_and_reader():
    calls = []
    adapter, team = Workspace("global", calls), Workspace("team", calls)

    class Reader:
        async def fetch_source_rows(self, ids):
            calls.append(("reader", "fetch", tuple(ids)))
            return {101: make_source_row(101)}

    class Pool:
        def adapter_for(self, team_id):
            return team

        async def index_source_rows(self, rows):
            calls.append(("team", "index", rows[0].worklog_id))

    class Cache:
        async def bump_version(self):
            pass

    service = module.LightWorklogIndexService(
        cache=Cache(), adapter_factory=lambda: adapter,
        team_adapter_factory=Pool, source_reader=Reader(),
    )
    assert asyncio.run(service.index_worklogs([101])).items[0].indexed
    assert ("reader", "fetch", (101,)) in calls


@pytest.mark.parametrize("operation", ["delete", "reindex"])
def test_delete_gate_failure_returns_per_document_delete_error(monkeypatch, operation):
    service, adapter, team, calls = setup(monkeypatch, failure=("STAGING", "worklog-101"))
    mutate = service.delete_worklogs if operation == "delete" else service.reindex_worklogs
    response = asyncio.run(mutate([101]))
    assert response.items[0].error == "LIGHTRAG_DELETE_FAILED"
    assert not any(item[1] == "delete" for item in calls)


def test_failed_delete_attempt_invalidates_cache(monkeypatch):
    service, adapter, team, calls = setup(monkeypatch)
    adapter.documents.add("worklog-101")
    adapter.owners["worklog-101"] = 7
    team.documents.add("worklog-101")
    team.failure = ("delete", "worklog-101")
    response = asyncio.run(service.delete_worklogs([101]))
    assert response.items[0].error == "LIGHTRAG_DELETE_FAILED"
    assert ("cache", "bump", None) in calls
    assert adapter.owners["worklog-101"] == 7


def test_previous_team_is_cleaned_before_current_team_is_written(monkeypatch):
    service, adapter, team, calls = setup(monkeypatch)
    old_team = Workspace("previous-team", calls)
    old_team.documents.add("worklog-101")
    adapter.documents.add("worklog-101")
    adapter.states["worklog-101"] = "COMPLETED"
    adapter.owners["worklog-101"] = 6
    pool = module.get_lightrag_team_scope_adapter()
    original_adapter_for = pool.adapter_for
    pool.adapter_for = lambda team_id: old_team if team_id == 6 else original_adapter_for(team_id)
    monkeypatch.setattr(module, "get_lightrag_team_scope_adapter", lambda: pool)
    response = asyncio.run(service.index_worklogs([101]))
    assert response.items[0].indexed
    assert not old_team.documents
    assert adapter.owners["worklog-101"] == 7
    assert calls.index(("previous-team", "delete", "worklog-101")) < calls.index(
        ("team", "body", "worklog-101"),
    )


def test_compensation_timeout_preserves_team_ownership(monkeypatch):
    service, adapter, team, calls = setup(monkeypatch, team_failure=("kg", "worklog-101"))
    service._settings = service._settings.model_copy(update={"lightrag_insert_timeout_seconds": 0.01})
    original_delete = team.delete_document

    async def blocked_cleanup(document_id):
        if document_id in team.documents:
            await asyncio.Future()
        await original_delete(document_id)

    team.delete_document = blocked_cleanup
    response = asyncio.run(service.index_worklogs([101]))
    assert response.items[0].error == "LIGHTRAG_COMPENSATION_FAILED"
    assert adapter.owners["worklog-101"] == 7


def test_global_compensation_failure_is_reported(monkeypatch):
    service, adapter, team, calls = setup(monkeypatch, failure=("kg", "worklog-101"))
    original_delete = adapter.delete_document

    async def fail_cleanup(document_id):
        if document_id in adapter.documents:
            raise LightRagDeleteFailedError("global cleanup unavailable")
        await original_delete(document_id)

    adapter.delete_document = fail_cleanup
    response = asyncio.run(service.index_worklogs([101]))
    assert response.items[0].error == "LIGHTRAG_COMPENSATION_FAILED"
    assert adapter.documents == {"worklog-101"}
    assert adapter.states.get("worklog-101") != "COMPLETED"


def test_reindex_cancellation_after_partial_delete_invalidates_cache(monkeypatch):
    service, adapter, team, calls = setup(monkeypatch)
    adapter.owners["worklog-101"] = 7

    async def cancelled_delete(document_id):
        calls.append(("team", "delete", document_id))
        raise asyncio.CancelledError

    team.delete_document = cancelled_delete
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(service.reindex_worklogs([101]))
    assert ("cache", "bump", None) in calls


def test_reindex_recovers_partial_document_without_indexed_ownership(monkeypatch):
    service, adapter, team, calls = setup(monkeypatch)
    adapter.documents.add("worklog-101")
    adapter.states["worklog-101"] = "FAILED"

    async def ownership(document_id, *, allow_missing=False):
        if document_id in adapter.documents and document_id not in adapter.owners and not allow_missing:
            raise LightRagDeleteFailedError("indexed ownership missing")
        return adapter.owners.get(document_id)

    adapter.get_document_team_id = ownership
    response = asyncio.run(service.reindex_worklogs([101]))
    assert response.items[0].indexed
    assert adapter.states["worklog-101"] == "COMPLETED"
    assert adapter.owners["worklog-101"] == 7
    assert adapter.documents == team.documents == {"worklog-101"}


def test_reindex_deleted_source_requires_recoverable_indexed_ownership(monkeypatch):
    service, adapter, team, calls = setup(monkeypatch)
    adapter.documents.add("worklog-101")

    class DeletedReader:
        async def fetch_source_rows(self, ids):
            return {}

    async def ownership(document_id, *, allow_missing=False):
        assert not allow_missing, "deleted source cannot supply a fallback team"
        raise LightRagDeleteFailedError("indexed ownership missing")

    service._source_reader = DeletedReader()
    adapter.get_document_team_id = ownership
    response = asyncio.run(service.reindex_worklogs([101]))
    assert response.items[0].error == "LIGHTRAG_DELETE_FAILED"
    assert adapter.documents == {"worklog-101"}
    assert not any(item[1] == "body" for item in calls)
