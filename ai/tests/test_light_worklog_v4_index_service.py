import asyncio
import pytest

from app.light.v4.service import worklog_index_service as service_module
from app.light.v4.service.lightrag_adapter import (
    LightRagDeleteFailedError,
    LightRagDeleteTimeoutError,
    LightRagInsertFailedError,
    LightRagInsertTimeoutError,
)
from app.light.v4.service.worklog_index_service import LightWorklogIndexService
from app.light.v4.store.worklog_source_store import (
    LightWorklogPredecessor,
    LightWorklogSourceRow,
    LightWorklogTag,
)
from app.config.settings import Settings


@pytest.fixture(autouse=True)
def isolated_cache(monkeypatch):
    """단위 테스트가 실제 Redis에 연결하지 않게 한다."""
    class Cache:
        async def bump_version(self):
            return 1
    monkeypatch.setattr(service_module, "WorklogQueryCache", lambda **kwargs: Cache())


@pytest.fixture(autouse=True)
def isolated_team_pool(monkeypatch):
    """순서/오류 매핑 단위 테스트는 실제 팀 저장소에 연결하지 않는다."""
    class Pool:
        def adapter_for(self, team_id):
            return OwnershipAdapter()

        async def index_source_rows(self, rows):
            assert rows and all(row.team_id > 0 for row in rows)

    monkeypatch.setattr(service_module, "get_lightrag_team_scope_adapter", Pool)


class OwnershipAdapter:
    """기존 문서는 소유권 없는 fixture로, 새 문서의 snapshot 쓰기는 기록한다."""

    async def save_indexed_team_ids(self, teams):
        self.saved_teams = {**getattr(self, "saved_teams", {}), **teams}

    async def get_document_team_id(self, doc_id, *, allow_missing=False):
        return None

    async def set_document_indexing_status(self, document_id, state):
        pass

    async def delete_document(self, document_id):
        pass


@pytest.mark.parametrize("blocked_phase", ["source", "team"])
@pytest.mark.parametrize("operation", ["index", "reindex"])
def test_delete_waits_for_source_read_and_team_index_publication(monkeypatch, blocked_phase, operation):
    async def run():
        phase_started, release_phase = asyncio.Event(), asyncio.Event()
        calls = []
        team_documents, global_documents = set(), set()

        async def block(phase):
            if phase == blocked_phase:
                phase_started.set()
                await release_phase.wait()

        class Reader:
            async def fetch_source_rows(self, ids):
                calls.append("source")
                await block("source")
                return {101: make_source_row(101)}

        class Adapter(OwnershipAdapter):
            async def save_indexed_team_ids(self, teams):
                global_documents.add(101)

            async def get_document_team_id(self, doc_id, *, allow_missing=False):
                return 7 if global_documents else None

            async def index_documents(self, docs):
                calls.append("text")

            async def index_custom_kg_documents(self, docs):
                calls.append("kg")

            async def delete_document(self, doc_id):
                calls.append("delete")
                global_documents.discard(101)

        class TeamAdapter:
            async def delete_document(self, doc_id):
                calls.append("team_delete")
                team_documents.discard(101)

        class Pool:
            async def index_source_rows(self, rows):
                await block("team")
                team_documents.add(101)
                calls.append("team")

            def adapter_for(self, team_id):
                return TeamAdapter()

        monkeypatch.setattr(service_module, "WorklogLightSourceRowReader", Reader)
        monkeypatch.setattr(service_module, "get_lightrag_worklog_index_adapter", Adapter)
        monkeypatch.setattr(service_module, "get_lightrag_team_scope_adapter", Pool)
        service = LightWorklogIndexService()
        mutate = service.index_worklogs if operation == "index" else service.reindex_worklogs
        indexing = asyncio.create_task(mutate([101]))
        await phase_started.wait()
        initial_deletes = calls.count("delete")
        deleting = asyncio.create_task(LightWorklogIndexService().delete_worklogs([101]))
        for _ in range(3):
            await asyncio.sleep(0)
        try:
            assert calls.count("delete") == initial_deletes
        finally:
            release_phase.set()
            await asyncio.gather(indexing, deleting)
        assert calls == ["source", "team_delete", "delete", "text", "kg",
                         "team", "team_delete", "delete"]
        assert not team_documents and not global_documents

    asyncio.run(run())


@pytest.mark.parametrize("status", ["failed", "pending", "processing"])
def test_native_unprocessed_body_skips_kg_snapshot_and_team_publication(monkeypatch, status):
    from types import SimpleNamespace
    from app.light.v4.service.lightrag_adapter import LightRagWorklogIndexAdapter

    calls = []

    class Reader:
        async def fetch_source_rows(self, ids):
            return {101: make_source_row(101)}

    class Adapter(LightRagWorklogIndexAdapter):
        async def get_document_team_id(self, doc_id, *, allow_missing=False):
            return None

        async def set_document_indexing_status(self, document_id, state):
            pass

        async def delete_document(self, document_id):
            pass

        async def index_custom_kg_documents(self, docs):
            calls.append("kg")

        async def save_indexed_team_ids(self, teams):
            calls.append("snapshot")

    async def insert(*args, **kwargs):
        calls.append("body")
        return "tracking-id"

    async def statuses(ids):
        return [{"status": status} for _ in ids]

    async def initialized():
        return SimpleNamespace(ainsert=insert, doc_status=SimpleNamespace(get_by_ids=statuses))

    class Pool:
        def adapter_for(self, team_id):
            return OwnershipAdapter()

        async def index_source_rows(self, rows):
            calls.append("team")

    adapter = Adapter(settings_obj=Settings(_env_file=None))
    adapter._get_initialized_rag = initialized
    monkeypatch.setattr(service_module, "WorklogLightSourceRowReader", Reader)
    monkeypatch.setattr(service_module, "get_lightrag_worklog_index_adapter", lambda: adapter)
    monkeypatch.setattr(service_module, "get_lightrag_team_scope_adapter", Pool)
    response = asyncio.run(LightWorklogIndexService().index_worklogs([101]))
    assert not response.items[0].indexed
    assert response.items[0].error == "LIGHTRAG_INSERT_FAILED"
    assert calls == ["body"]


@pytest.mark.parametrize("outcome", ["mixed_status", "timeout", "failure"])
def test_failed_text_insert_invalidates_cache_when_native_may_have_written(monkeypatch, outcome):
    from types import SimpleNamespace
    from app.light.v4.service.lightrag_adapter import LightRagWorklogIndexAdapter

    calls = []

    class Reader:
        async def fetch_source_rows(self, ids):
            return {item: make_source_row(item) for item in ids}

    class Cache:
        async def bump_version(self):
            calls.append("invalidate")
            return 2

    class Adapter(LightRagWorklogIndexAdapter):
        async def get_document_team_id(self, doc_id, *, allow_missing=False):
            return None

        async def set_document_indexing_status(self, document_id, state):
            pass

        async def delete_document(self, document_id):
            pass

        async def index_custom_kg_documents(self, docs):
            calls.append("kg")

        async def save_indexed_team_ids(self, teams):
            calls.append("snapshot")

    async def insert(*args, **kwargs):
        calls.append("body_written")
        if outcome == "timeout":
            raise TimeoutError("after partial write")
        if outcome == "failure":
            raise RuntimeError("after partial write")
        return "tracking-id"

    async def statuses(ids):
        return [{"status": "processed" if item == "worklog-101" else "failed"} for item in ids]

    async def initialized():
        return SimpleNamespace(ainsert=insert, doc_status=SimpleNamespace(get_by_ids=statuses))

    adapter = Adapter(settings_obj=Settings(_env_file=None))
    adapter._get_initialized_rag = initialized
    monkeypatch.setattr(service_module, "WorklogLightSourceRowReader", Reader)
    monkeypatch.setattr(service_module, "get_lightrag_worklog_index_adapter", lambda: adapter)
    response = asyncio.run(LightWorklogIndexService(cache=Cache()).index_worklogs([101, 102]))
    if outcome == "mixed_status":
        assert [(item.indexed, item.error) for item in response.items] == [
            (True, None), (False, "LIGHTRAG_INSERT_FAILED"),
        ]
        assert calls == ["body_written", "kg", "snapshot", "body_written", "invalidate"]
    else:
        assert all(not item.indexed for item in response.items)
        error = "LIGHTRAG_INSERT_TIMEOUT" if outcome == "timeout" else "LIGHTRAG_INSERT_FAILED"
        assert all(item.error == error for item in response.items)
        assert calls == ["body_written", "body_written", "invalidate"]


def test_mutation_lock_cancelled_waiter_releases_partial_batch_and_reversed_ids_finish():
    from app.light.v4.service.worklog_mutation_lock import worklog_mutation_locks

    async def run():
        entered, release = asyncio.Event(), asyncio.Event()

        async def hold():
            async with worklog_mutation_locks(["worklog:2"]):
                entered.set()
                await release.wait()

        async def mutate(ids):
            async with worklog_mutation_locks(ids):
                await asyncio.sleep(0)

        holder = asyncio.create_task(hold())
        await entered.wait()
        waiter = asyncio.create_task(mutate(["worklog:2", "worklog:1"]))
        for _ in range(3):
            await asyncio.sleep(0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        # 취소된 다중 잠금 대기가 먼저 확보한 worklog:1을 남기지 않는다.
        await asyncio.wait_for(mutate(["worklog:1"]), timeout=1)
        release.set()
        await holder
        await asyncio.wait_for(asyncio.gather(
            mutate(["worklog:1", "worklog:2"]), mutate(["worklog:2", "worklog:1"]),
        ), timeout=1)

    asyncio.run(run())


def test_prefilter_index_saves_ownership_before_team_writes_and_delete_cleans_both(monkeypatch):
    calls = []

    class Reader:
        async def fetch_source_rows(self, ids):
            return {101: make_source_row(101)}

    class Adapter(OwnershipAdapter):
        async def index_documents(self, docs):
            calls.append("legacy_text")

        async def index_custom_kg_documents(self, docs):
            calls.append("legacy_kg")

        async def save_indexed_team_ids(self, teams):
            assert teams == {101: 7}
            calls.append("ownership")

        async def get_document_team_id(self, doc_id, *, allow_missing=False):
            assert doc_id == "worklog-101"
            return 7

        async def delete_document(self, doc_id):
            calls.append("legacy_delete")

    class TeamAdapter:
        async def delete_document(self, doc_id):
            calls.append("team_delete")

    class TeamPool:
        async def index_source_rows(self, rows):
            assert [row.worklog_id for row in rows] == [101]
            calls.append("team_index")

        def adapter_for(self, team_id):
            assert team_id == 7
            return TeamAdapter()

    monkeypatch.setattr(service_module, "WorklogLightSourceRowReader", Reader)
    monkeypatch.setattr(service_module, "get_lightrag_worklog_index_adapter", Adapter)
    monkeypatch.setattr(service_module, "get_lightrag_team_scope_adapter", TeamPool)
    service = LightWorklogIndexService(
        settings_obj=Settings(_env_file=None),
    )

    async def run():
        indexed = await service.index_worklogs([101])
        deleted = await service.delete_worklogs([101])
        return indexed, deleted

    indexed, deleted = asyncio.run(run())
    assert indexed.items[0].indexed and deleted.items[0].deleted
    assert calls == ["team_delete", "legacy_delete", "legacy_text", "legacy_kg", "ownership", "team_index",
                     "team_delete", "legacy_delete"]


@pytest.mark.parametrize("status", ["fail", "not_allowed"])
def test_team_native_delete_failure_preserves_global_ownership(monkeypatch, status):
    from types import SimpleNamespace
    from lightrag.base import DeletionResult
    from app.light.v4.service.lightrag_adapter import LightRagWorklogIndexAdapter

    calls = []

    class GlobalAdapter(OwnershipAdapter):
        async def get_document_team_id(self, doc_id):
            return 7

        async def delete_document(self, doc_id):
            calls.append("global_delete")

    async def native_delete(doc_id):
        calls.append("team_delete")
        return DeletionResult(status=status, doc_id=doc_id, message="failed")

    async def initialized():
        return SimpleNamespace(adelete_by_doc_id=native_delete, doc_status=SimpleNamespace())

    team_adapter = LightRagWorklogIndexAdapter(settings_obj=Settings(_env_file=None))
    team_adapter._get_initialized_rag = initialized
    monkeypatch.setattr(service_module, "get_lightrag_team_scope_adapter",
                        lambda: SimpleNamespace(adapter_for=lambda team: team_adapter))
    service = LightWorklogIndexService(settings_obj=Settings(
        _env_file=None,
    ))
    with pytest.raises(LightRagDeleteFailedError):
        asyncio.run(service._delete_document(GlobalAdapter(), "worklog-101"))
    assert calls == ["team_delete"]


def make_source_row(worklog_id: int) -> LightWorklogSourceRow:
    return LightWorklogSourceRow(
        worklog_id=worklog_id,
        title=f"업무 {worklog_id}",
        request_content="요청",
        work_content="수행",
        author_id=5,
        author_name="김도윤",
        team_id=7,
        team_name="정산 고도화 TF",
        tags=[LightWorklogTag(tag_id=11, tag_name="정산", description=None)],
        direct_predecessors=[LightWorklogPredecessor(worklog_id=88, title="직접 선행")],
    )


def test_index_service_calls_text_and_custom_kg_from_one_source_fetch(monkeypatch) -> None:
    class _Reader:
        async def fetch_source_rows(self, worklog_ids: list[int]):
            assert worklog_ids == [101, 102]
            return {101: make_source_row(101), 102: make_source_row(102)}

    class _Adapter(OwnershipAdapter):
        def __init__(self) -> None:
            self.text_document_ids: list[str] = []
            self.custom_document_ids: list[str] = []

        async def index_documents(self, documents) -> None:
            self.text_document_ids.extend(document.document_id for document in documents)

        async def index_custom_kg_documents(self, documents) -> None:
            self.custom_document_ids.extend(document.document_id for document in documents)

    async def run_case() -> None:
        adapter = _Adapter()
        monkeypatch.setattr(service_module, "WorklogLightSourceRowReader", _Reader)
        monkeypatch.setattr(service_module, "get_lightrag_worklog_index_adapter", lambda: adapter)

        response = await LightWorklogIndexService().index_worklogs([101, 102])

        assert adapter.text_document_ids == ["worklog-101", "worklog-102"]
        assert adapter.custom_document_ids == ["worklog-101", "worklog-102"]
        assert adapter.saved_teams == {101: 7, 102: 7}
        assert [item.model_dump(by_alias=True) for item in response.items] == [
            {"worklogId": 101, "indexed": True, "error": None},
            {"worklogId": 102, "indexed": True, "error": None},
        ]

    asyncio.run(run_case())


def test_index_service_maps_custom_kg_batch_failure_to_all_found_ids(monkeypatch) -> None:
    class _Reader:
        async def fetch_source_rows(self, worklog_ids: list[int]):
            return {101: make_source_row(101), 103: make_source_row(103)}

    class _Adapter(OwnershipAdapter):
        async def index_documents(self, documents) -> None:
            assert len(documents) == 1
            assert documents[0].document_id in {"worklog-101", "worklog-103"}

        async def index_custom_kg_documents(self, documents) -> None:
            assert len(documents) == 1
            assert documents[0].document_id in {"worklog-101", "worklog-103"}
            raise LightRagInsertTimeoutError("timeout")

    async def run_case() -> None:
        monkeypatch.setattr(service_module, "WorklogLightSourceRowReader", _Reader)
        monkeypatch.setattr(service_module, "get_lightrag_worklog_index_adapter", lambda: _Adapter())

        response = await LightWorklogIndexService().index_worklogs([101, 102, 103])

        assert [item.model_dump(by_alias=True) for item in response.items] == [
            {"worklogId": 101, "indexed": False, "error": "LIGHTRAG_INSERT_TIMEOUT"},
            {"worklogId": 102, "indexed": False, "error": "WORKLOG_NOT_FOUND"},
            {"worklogId": 103, "indexed": False, "error": "LIGHTRAG_INSERT_TIMEOUT"},
        ]

    asyncio.run(run_case())


def test_index_service_maps_text_batch_failure_to_all_found_ids(monkeypatch) -> None:
    class _Reader:
        async def fetch_source_rows(self, worklog_ids: list[int]):
            return {101: make_source_row(101), 103: make_source_row(103)}

    class _Adapter(OwnershipAdapter):
        def __init__(self) -> None:
            self.custom_calls = 0

        async def index_documents(self, documents) -> None:
            raise LightRagInsertFailedError("failed")

        async def index_custom_kg_documents(self, documents) -> None:
            self.custom_calls += 1

    async def run_case() -> None:
        adapter = _Adapter()
        monkeypatch.setattr(service_module, "WorklogLightSourceRowReader", _Reader)
        monkeypatch.setattr(service_module, "get_lightrag_worklog_index_adapter", lambda: adapter)

        response = await LightWorklogIndexService().index_worklogs([101, 102, 103])

        assert adapter.custom_calls == 0
        assert [item.model_dump(by_alias=True) for item in response.items] == [
            {"worklogId": 101, "indexed": False, "error": "LIGHTRAG_INSERT_FAILED"},
            {"worklogId": 102, "indexed": False, "error": "WORKLOG_NOT_FOUND"},
            {"worklogId": 103, "indexed": False, "error": "LIGHTRAG_INSERT_FAILED"},
        ]

    asyncio.run(run_case())


def test_reindex_service_deletes_first_and_bumps_cache_version(monkeypatch) -> None:
    calls: list[str] = []

    class _Reader:
        async def fetch_source_rows(self, worklog_ids: list[int]):
            return {101: make_source_row(101)}

    class _Adapter(OwnershipAdapter):
        async def delete_document(self, document_id: str) -> None:
            calls.append(f"delete:{document_id}")

        async def index_documents(self, documents) -> None:
            calls.append(f"index_docs:{[d.document_id for d in documents]}")

        async def index_custom_kg_documents(self, documents) -> None:
            calls.append(f"index_kg:{[d.document_id for d in documents]}")

    class _FakeCache:
        def __init__(self) -> None:
            self.bump_count = 0

        async def bump_version(self) -> int:
            self.bump_count += 1
            calls.append("bump_version")
            return 2

    async def run_case() -> None:
        adapter = _Adapter()
        cache = _FakeCache()
        monkeypatch.setattr(service_module, "WorklogLightSourceRowReader", _Reader)
        monkeypatch.setattr(service_module, "get_lightrag_worklog_index_adapter", lambda: adapter)

        service = LightWorklogIndexService(cache=cache)  # type: ignore
        response = await service.reindex_worklogs([101])

        assert calls == [
            "delete:worklog-101",
            "index_docs:['worklog-101']",
            "index_kg:['worklog-101']",
            "bump_version",
        ]
        assert [item.model_dump(by_alias=True) for item in response.items] == [
            {"worklogId": 101, "indexed": True, "error": None}
        ]
        assert cache.bump_count == 1

    asyncio.run(run_case())


def test_delete_service_deletes_documents_and_bumps_cache_version(monkeypatch) -> None:
    deleted_ids: list[str] = []

    class _Adapter(OwnershipAdapter):
        async def delete_document(self, document_id: str) -> None:
            deleted_ids.append(document_id)

    class _FakeCache:
        def __init__(self) -> None:
            self.bump_count = 0

        async def bump_version(self) -> int:
            self.bump_count += 1
            return 3

    class _Reader:
        async def fetch_source_rows(self, worklog_ids: list[int]):
            return {}

    async def run_case() -> None:
        adapter = _Adapter()
        cache = _FakeCache()
        monkeypatch.setattr(service_module, "WorklogLightSourceRowReader", _Reader)
        monkeypatch.setattr(service_module, "get_lightrag_worklog_index_adapter", lambda: adapter)

        service = LightWorklogIndexService(cache=cache)  # type: ignore
        response = await service.delete_worklogs([101, 102])

        assert deleted_ids == ["worklog-101", "worklog-102"]
        assert [item.model_dump(by_alias=True) for item in response.items] == [
            {"worklogId": 101, "deleted": True, "error": None},
            {"worklogId": 102, "deleted": True, "error": None},
        ]
        assert cache.bump_count == 1

    asyncio.run(run_case())


def test_reindex_service_records_delete_failure_and_skips_insert(monkeypatch) -> None:
    calls: list[str] = []

    class _Reader:
        async def fetch_source_rows(self, worklog_ids: list[int]):
            return {101: make_source_row(101)}

    class _Adapter(OwnershipAdapter):
        async def delete_document(self, document_id: str) -> None:
            calls.append(f"delete:{document_id}")
            if document_id == "worklog-102":
                raise LightRagDeleteTimeoutError("timeout")

        async def index_documents(self, documents) -> None:
            calls.append(f"index_docs:{[d.document_id for d in documents]}")

        async def index_custom_kg_documents(self, documents) -> None:
            calls.append(f"index_kg:{[d.document_id for d in documents]}")

    class _FakeCache:
        def __init__(self) -> None:
            self.bump_count = 0

        async def bump_version(self) -> int:
            self.bump_count += 1
            calls.append("bump_version")
            return 2

    async def run_case() -> None:
        adapter = _Adapter()
        cache = _FakeCache()
        monkeypatch.setattr(service_module, "WorklogLightSourceRowReader", _Reader)
        monkeypatch.setattr(service_module, "get_lightrag_worklog_index_adapter", lambda: adapter)

        service = LightWorklogIndexService(cache=cache)  # type: ignore
        response = await service.reindex_worklogs([101, 102])

        assert calls == [
            "delete:worklog-102",
            "delete:worklog-101",
            "index_docs:['worklog-101']",
            "index_kg:['worklog-101']",
            "bump_version",
        ]
        assert [item.model_dump(by_alias=True) for item in response.items] == [
            {"worklogId": 101, "indexed": True, "error": None},
            {"worklogId": 102, "indexed": False, "error": "LIGHTRAG_DELETE_TIMEOUT"},
        ]
        assert cache.bump_count == 1

    asyncio.run(run_case())


def test_reindex_service_all_delete_failures_invalidates_cache_and_returns_errors(monkeypatch) -> None:
    calls: list[str] = []

    class _Adapter(OwnershipAdapter):
        async def delete_document(self, document_id: str) -> None:
            calls.append(f"delete:{document_id}")
            raise LightRagDeleteFailedError("failed")

        async def index_documents(self, documents) -> None:
            calls.append("index_docs")

        async def index_custom_kg_documents(self, documents) -> None:
            calls.append("index_kg")

    class _FakeCache:
        def __init__(self) -> None:
            self.bump_count = 0

        async def bump_version(self) -> int:
            self.bump_count += 1
            return 2

    class _Reader:
        async def fetch_source_rows(self, worklog_ids: list[int]):
            return {}

    async def run_case() -> None:
        adapter = _Adapter()
        cache = _FakeCache()
        monkeypatch.setattr(service_module, "WorklogLightSourceRowReader", _Reader)
        monkeypatch.setattr(service_module, "get_lightrag_worklog_index_adapter", lambda: adapter)

        service = LightWorklogIndexService(cache=cache)  # type: ignore
        response = await service.reindex_worklogs([101])

        assert calls == ["delete:worklog-101"]
        assert [item.model_dump(by_alias=True) for item in response.items] == [
            {"worklogId": 101, "indexed": False, "error": "LIGHTRAG_DELETE_FAILED"}
        ]
        assert cache.bump_count == 1

    asyncio.run(run_case())


def test_index_service_custom_kg_failure_still_bumps_cache_version(monkeypatch) -> None:
    class _Reader:
        async def fetch_source_rows(self, worklog_ids: list[int]):
            return {101: make_source_row(101)}

    class _Adapter(OwnershipAdapter):
        async def index_documents(self, documents) -> None:
            assert [d.document_id for d in documents] == ["worklog-101"]

        async def index_custom_kg_documents(self, documents) -> None:
            raise LightRagInsertTimeoutError("timeout")

    class _FakeCache:
        def __init__(self) -> None:
            self.bump_count = 0

        async def bump_version(self) -> int:
            self.bump_count += 1
            return 2

    async def run_case() -> None:
        adapter = _Adapter()
        cache = _FakeCache()
        monkeypatch.setattr(service_module, "WorklogLightSourceRowReader", _Reader)
        monkeypatch.setattr(service_module, "get_lightrag_worklog_index_adapter", lambda: adapter)

        service = LightWorklogIndexService(cache=cache)  # type: ignore
        response = await service.index_worklogs([101])

        assert [item.model_dump(by_alias=True) for item in response.items] == [
            {"worklogId": 101, "indexed": False, "error": "LIGHTRAG_INSERT_TIMEOUT"}
        ]
        assert cache.bump_count == 1

    asyncio.run(run_case())
