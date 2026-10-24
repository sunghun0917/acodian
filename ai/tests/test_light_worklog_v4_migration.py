"""활성 인덱스에 쓰지 않고 재구축 결과와 최종 원본을 대조한다."""

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.config.settings import Settings
from app.util.rebuild_light_worklogs_v4 import (
    rebuild_worklogs, source_fingerprint, target_settings,
)
from tests.test_light_worklog_team_scope import source_row
from app.light.v4.store.worklog_source_store import LightWorklogTag


def test_target_guard_rejects_active_workspace_or_directory(tmp_path):
    active = Settings(_env_file=None, lightrag_workspace="active", lightrag_working_dir=str(tmp_path / "active"))
    with pytest.raises(ValueError):
        target_settings(active, active_workspace="active", target_workspace="active", working_dir=tmp_path / "new")
    with pytest.raises(ValueError):
        target_settings(active, active_workspace="active", target_workspace="new", working_dir=tmp_path / "active")
    with pytest.raises(ValueError):
        target_settings(active, active_workspace="different", target_workspace="new", working_dir=tmp_path / "new")
    new = target_settings(active, active_workspace="active", target_workspace="new", working_dir=tmp_path / "new")
    assert new.lightrag_workspace == "new"
    assert active.lightrag_workspace == "active"


def test_fingerprint_covers_all_source_fields_but_normalizes_collection_order():
    row = replace(source_row(10, 1), tags=[LightWorklogTag(2, "B", None), LightWorklogTag(1, "A", "desc")])
    assert source_fingerprint(row) == source_fingerprint(replace(row, tags=list(reversed(row.tags))))
    for changed in (replace(row, title="new"), replace(row, work_content="new"),
                    replace(row, request_content="new"), replace(row, team_id=20),
                    replace(row, author_name="new"), replace(row, team_name="new"),
                    replace(row, tags=[LightWorklogTag(2, "B", "changed")])):
        assert source_fingerprint(row) != source_fingerprint(changed)


class Reader:
    def __init__(self, rows):
        self.rows = rows

    async def fetch_source_ids(self, *, after_id, limit):
        return sorted(key for key in self.rows if key > after_id)[:limit]

    async def fetch_source_rows(self, ids):
        return {key: self.rows[key] for key in ids if key in self.rows}


class Runtime:
    def __init__(self, rows):
        self.reader = Reader(rows)
        self.indexed = {}
        self.deleted = []
        self.fail_ids = set()
        self.incomplete_team = False
        self.after_index = None

    def service(self, snapshot_reader):
        async def index(ids):
            snapshots = await snapshot_reader.fetch_source_rows(ids)
            for key, row in snapshots.items():
                if key not in self.fail_ids:
                    self.indexed[key] = row
            if self.after_index:
                self.after_index()
            return SimpleNamespace(items=[SimpleNamespace(worklog_id=key, indexed=key not in self.fail_ids,
                                                         error="FAILED" if key in self.fail_ids else None) for key in ids])

        async def delete(ids):
            self.deleted.extend(ids)
            for key in ids:
                self.indexed.pop(key, None)
            return SimpleNamespace(items=[SimpleNamespace(worklog_id=key, deleted=True, error=None) for key in ids])

        return SimpleNamespace(index_worklogs=index, delete_worklogs=delete)

    async def get_processed_worklog_ids(self, ids, *, require_completed=True):
        if not require_completed and self.incomplete_team:
            return set()
        return set(ids) & set(self.indexed)

    async def get_document_team_id(self, doc_id):
        return self.indexed[int(doc_id.removeprefix("worklog-"))].team_id

    async def set_document_indexing_status(self, doc_id, state):
        pass

    async def delete_document(self, doc_id):
        key = int(doc_id.removeprefix("worklog-"))
        self.deleted.append(key)
        self.indexed.pop(key, None)

    def adapter_for(self, team_id):
        async def delete(doc_id):
            pass

        return SimpleNamespace(delete_document=delete, get_processed_worklog_ids=self.get_processed_worklog_ids)


def run(runtime, path, frozen=False):
    return asyncio.run(rebuild_worklogs(
        reader=runtime.reader, service_factory=runtime.service,
        adapter=runtime, team_pool=runtime, manifest_path=path,
        active_workspace="active", target_workspace="new", batch_size=1,
        source_writes_frozen=frozen,
    ))


def test_build_requires_operator_freeze_attestation_before_ready(tmp_path):
    runtime = Runtime({1: source_row(10, 1), 2: source_row(20, 2)})
    result = run(runtime, tmp_path / "manifest.json")
    assert result["ready"] is False
    assert set(runtime.indexed) == {1, 2}
    assert "source_writes_not_frozen" in result["blockers"]
    result = run(runtime, tmp_path / "manifest.json", frozen=True)
    assert result["ready"] is True
    assert len(result["documents"]) == 2


def test_failed_index_or_team_status_blocks_readiness(tmp_path):
    runtime = Runtime({1: source_row(10, 1)})
    runtime.fail_ids.add(1)
    result = run(runtime, tmp_path / "manifest.json", frozen=True)
    assert result["ready"] is False
    assert result["failures"] == {"1": "FAILED"}
    runtime.fail_ids.clear()
    runtime.incomplete_team = True
    assert run(runtime, tmp_path / "manifest.json", frozen=True)["ready"] is False


def test_source_change_after_indexing_invalidates_readiness(tmp_path):
    runtime = Runtime({1: source_row(10, 1)})
    runtime.after_index = lambda: runtime.reader.rows.update({1: replace(source_row(10, 1), work_content="CHANGED")})
    result = run(runtime, tmp_path / "manifest.json", frozen=True)
    assert result["ready"] is False
    assert "source_changed" in result["blockers"]
    assert runtime.indexed[1].work_content != "CHANGED"


def test_rerun_cleans_deleted_documents_only_in_target_manifest(tmp_path):
    runtime = Runtime({1: source_row(10, 1), 2: source_row(20, 2)})
    path = tmp_path / "manifest.json"
    assert run(runtime, path, frozen=True)["ready"] is True
    del runtime.reader.rows[1]
    assert run(runtime, path, frozen=True)["ready"] is True
    assert runtime.deleted == [1]
    assert set(runtime.indexed) == {2}


def test_storage_error_invalidates_previously_ready_artifact(tmp_path):
    runtime = Runtime({1: source_row(10, 1)})
    path = tmp_path / "manifest.json"
    assert run(runtime, path, frozen=True)["ready"] is True

    async def fail(**kwargs):
        raise RuntimeError("DB unavailable")

    runtime.reader.fetch_source_ids = fail
    with pytest.raises(RuntimeError):
        run(runtime, path, frozen=True)
    assert json.loads(path.read_text(encoding="utf-8"))["ready"] is False


def test_manifest_from_other_workspace_is_rejected_without_writes(tmp_path):
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"target_workspace": "active", "documents": {}}), encoding="utf-8")
    runtime = Runtime({1: source_row(10, 1)})
    with pytest.raises(ValueError):
        run(runtime, path, frozen=True)
    assert runtime.indexed == {} and runtime.deleted == []
