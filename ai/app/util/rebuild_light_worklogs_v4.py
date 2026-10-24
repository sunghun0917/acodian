"""활성 workspace를 유지한 채 별도 LightRAG v4 인덱스를 재구축·대조한다.

이 도구는 서비스 전환을 실행하지 않는다. ready는 원본 쓰기 동결을 실행자가
확인한 현재 대조 결과이며, 동결 해제 후 재사용할 수 있는 승인 토큰이 아니다.
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import AsyncIterator, Callable
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from typing import Any

from app.config.settings import Settings, settings
from app.light.v4.store.worklog_source_store import LightWorklogSourceRow


def target_settings(
    active: Settings, *, active_workspace: str, target_workspace: str, working_dir: Path,
) -> Settings:
    """설정된 활성 workspace와 경로에 대한 쓰기를 거부하고 독립 설정을 만든다."""
    if active_workspace != active.lightrag_workspace.strip():
        raise ValueError("Active workspace must match the current settings")
    if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}", target_workspace)
            or target_workspace.startswith("worklog_scope_")
            or target_workspace == active_workspace):
        raise ValueError("Target must be a separate, explicit workspace")
    if working_dir.resolve() == Path(active.lightrag_working_dir).resolve():
        raise ValueError("Target working directory must differ from the active directory")
    return active.model_copy(update={
        "lightrag_workspace": target_workspace,
        "lightrag_working_dir": str(working_dir.resolve()),
    })


def source_fingerprint(row: LightWorklogSourceRow) -> str:
    """순서가 의미 없는 태그/선행 관계를 정규화하고 원본 전체의 지문을 만든다."""
    source = asdict(row)
    source["tags"] = sorted(source["tags"], key=lambda item: (item["tag_id"], item["tag_name"], item["description"] or ""))
    source["direct_predecessors"] = sorted(source["direct_predecessors"], key=lambda item: (
        item["worklog_id"], item["title"], item["team_id"] or 0,
    ))
    return hashlib.sha256(json.dumps(source, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class SnapshotReader:
    """manifest의 지문과 실제 색인에 같은 batch source를 제공한다."""

    def __init__(self, rows: dict[int, LightWorklogSourceRow]) -> None:
        self._rows = rows

    async def fetch_source_rows(self, worklog_ids: list[int]) -> dict[int, LightWorklogSourceRow]:
        """색인 요청에 포함된 고정 원본만 반환한다."""
        return {key: self._rows[key] for key in worklog_ids if key in self._rows}


def write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    """같은 디렉터리의 임시 파일을 교체하여 불완전 JSON이 readiness가 되지 않게 한다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as handle:
            temporary = handle.name
            json.dump(manifest, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)


def read_manifest(path: Path, *, active_workspace: str, target_workspace: str) -> dict[str, Any]:
    """다른 세대의 manifest나 임의 문서 ID를 삭제 대상으로 받아들이지 않는다."""
    if not path.exists():
        return {}
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if (not isinstance(manifest, dict) or manifest.get("schema_version") != 1
            or manifest.get("target_workspace") != target_workspace
            or manifest.get("active_workspace") != active_workspace):
        raise ValueError("Manifest identity mismatch")
    documents = manifest.get("documents")
    if not isinstance(documents, dict):
        raise ValueError("Invalid manifest documents")
    for worklog_id, record in documents.items():
        if (not isinstance(worklog_id, str) or not re.fullmatch(r"[1-9][0-9]*", worklog_id)
                or not isinstance(record, dict) or type(record.get("team_id")) is not int
                or record["team_id"] <= 0 or not isinstance(record.get("fingerprint"), str)
                or not re.fullmatch(r"[a-f0-9]{64}", record["fingerprint"])):
            raise ValueError("Invalid manifest document")
    return documents


async def source_batches(
    reader: Any, batch_size: int,
) -> AsyncIterator[tuple[list[int], dict[int, LightWorklogSourceRow]]]:
    """생존 ID를 keyset paging하고 잘못된 reader 결과는 즉시 거부한다."""
    after_id = 0
    while True:
        ids = await reader.fetch_source_ids(after_id=after_id, limit=batch_size)
        if not ids:
            return
        if (not isinstance(ids, list) or len(ids) > batch_size
                or any(type(key) is not int or key <= after_id for key in ids)
                or ids != sorted(set(ids))):
            raise ValueError("Invalid source ID page")
        rows = await reader.fetch_source_rows(ids)
        if (not isinstance(rows, dict) or any(key not in ids or row.worklog_id != key for key, row in rows.items())):
            raise ValueError("Source identity mismatch")
        yield ids, rows
        after_id = ids[-1]


async def delete_target_document(adapter: Any, team_pool: Any, worklog_id: int, team_id: int) -> None:
    """manifest에 확보한 팀/문서 ID로만 target을 정리하고 실패 시 소유권을 보존한다."""
    document_id = f"worklog-{worklog_id}"
    await adapter.set_document_indexing_status(document_id, "STAGING")
    await team_pool.adapter_for(team_id).delete_document(document_id)
    await adapter.delete_document(document_id)


async def rebuild_worklogs(
    *, reader: Any, service_factory: Callable[[SnapshotReader], Any], adapter: Any,
    team_pool: Any, manifest_path: Path, active_workspace: str, target_workspace: str,
    batch_size: int, source_writes_frozen: bool,
) -> dict[str, Any]:
    """동일 snapshot을 색인한 후 전체 원본·완료 상태를 대조하여 준비 여부를 기록한다."""
    if (not active_workspace or not target_workspace or active_workspace == target_workspace
            or type(batch_size) is not int or not 1 <= batch_size <= settings.lightrag_index_max_batch_size):
        raise ValueError("Invalid rebuild target or batch size")
    documents = read_manifest(manifest_path, active_workspace=active_workspace, target_workspace=target_workspace)
    manifest: dict[str, Any] = {
        "schema_version": 1, "active_workspace": active_workspace, "target_workspace": target_workspace,
        "ready": False, "source_writes_frozen_attested": source_writes_frozen,
        "documents": documents, "failures": {}, "blockers": ["verification_pending"],
    }
    # 이전 ready 결과는 첫 DB 호출 전에 무효화한다.
    write_manifest(manifest_path, manifest)
    exit_code = 1
    try:
        seen: set[int] = set()
        async for ids, rows in source_batches(reader, batch_size):
            seen.update(ids)
            for worklog_id, row in rows.items():
                previous = documents.get(str(worklog_id))
                if previous is not None and previous["team_id"] != row.team_id:
                    await delete_target_document(adapter, team_pool, worklog_id, previous["team_id"])
                documents[str(worklog_id)] = {"fingerprint": source_fingerprint(row), "team_id": row.team_id}
            # 실패/종료 뒤 원본이 삭제되어도 시도한 target 문서를 정리할 수 있게 먼저 기록한다.
            write_manifest(manifest_path, manifest)
            response = await service_factory(SnapshotReader(rows)).index_worklogs(ids)
            if (len(response.items) != len(ids) or {item.worklog_id for item in response.items} != set(ids)):
                raise ValueError("Invalid index response identities")
            for item in response.items:
                if not item.indexed or item.worklog_id not in rows:
                    manifest["failures"][str(item.worklog_id)] = item.error or "SOURCE_CHANGED"
            write_manifest(manifest_path, manifest)

        for key in list(documents):
            if int(key) not in seen:
                try:
                    await delete_target_document(adapter, team_pool, int(key), documents[key]["team_id"])
                    del documents[key]
                except Exception as exc:
                    manifest["failures"][key] = type(exc).__name__

        current: dict[str, dict[str, Any]] = {}
        statuses_valid = True
        async for ids, rows in source_batches(reader, batch_size):
            if len(rows) != len(ids):
                statuses_valid = False
            completed = await adapter.get_processed_worklog_ids(ids)
            for team_id in {row.team_id for row in rows.values()}:
                team_ids = [key for key, row in rows.items() if row.team_id == team_id]
                processed = await team_pool.adapter_for(team_id).get_processed_worklog_ids(team_ids, require_completed=False)
                if not set(team_ids) <= processed:
                    statuses_valid = False
            for key, row in rows.items():
                current[str(key)] = {"fingerprint": source_fingerprint(row), "team_id": row.team_id}
                if key not in completed or await adapter.get_document_team_id(f"worklog-{key}") != row.team_id:
                    statuses_valid = False
        blockers = []
        if not source_writes_frozen:
            blockers.append("source_writes_not_frozen")
        if documents != current:
            blockers.append("source_changed")
        if not statuses_valid:
            blockers.append("incomplete_index")
        if manifest["failures"]:
            blockers.append("mutation_failed")
        manifest.update(ready=not blockers, blockers=blockers,
                        checked_at=datetime.now(timezone.utc).isoformat())
        write_manifest(manifest_path, manifest)
        return manifest
    except Exception as exc:
        manifest.update(ready=False, blockers=["verification_failed"], error_type=type(exc).__name__)
        write_manifest(manifest_path, manifest)
        raise


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """운영 대상과 실제 데이터 반출 승인을 명시적으로 받는다."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--active-workspace", required=True)
    parser.add_argument("--target-workspace", required=True)
    parser.add_argument("--working-dir", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--batch-size", type=int, default=settings.lightrag_index_max_batch_size)
    parser.add_argument("--confirm-real-data-export", action="store_true", required=True,
                        help="업무 원본을 Gemini에 전송하는 것을 승인한다.")
    parser.add_argument("--source-writes-frozen", action="store_true",
                        help="원본 쓰기 동결과 진행 중 작업 배출을 실행자가 확인했음을 선언한다(자동 동결 아님).")
    return parser.parse_args(argv)


async def main(argv: list[str] | None = None) -> int:
    """별도 프로세스에서만 target 인덱스를 만들며 서비스 전환은 실행하지 않는다."""
    from app.light.v4.service.lightrag_adapter import LightRagWorklogIndexAdapter
    from app.light.v4.service.worklog_index_service import LightWorklogIndexService
    from app.light.v4.service.worklog_source_row_reader import WorklogLightSourceRowReader
    from app.light.v4.service.worklog_team_scope import LightRagTeamScopeAdapter

    args = parse_args(argv)
    target = target_settings(settings, active_workspace=args.active_workspace,
                             target_workspace=args.target_workspace, working_dir=args.working_dir)
    reader = WorklogLightSourceRowReader()
    adapter = LightRagWorklogIndexAdapter(settings_obj=target)
    team_pool = LightRagTeamScopeAdapter(settings_obj=target, source_reader=reader,
                                        global_adapter_factory=lambda: adapter)

    class UnusedResponseCache:
        async def bump_version(self) -> int:
            """비활성 target 구축은 기존 서비스의 응답 캐시 버전을 바꾸지 않는다."""
            return 0

    def service_factory(snapshot: SnapshotReader) -> LightWorklogIndexService:
        return LightWorklogIndexService(settings_obj=target, cache=UnusedResponseCache(),
                                       adapter_factory=lambda: adapter, team_adapter_factory=lambda: team_pool,
                                       source_reader=snapshot)

    try:
        result = await rebuild_worklogs(
            reader=reader, service_factory=service_factory, adapter=adapter, team_pool=team_pool,
            manifest_path=args.manifest, active_workspace=args.active_workspace,
            target_workspace=args.target_workspace, batch_size=args.batch_size,
            source_writes_frozen=args.source_writes_frozen,
        )
        print(json.dumps({"ready": result["ready"], "blockers": result["blockers"], "manifest": str(args.manifest)}))
        exit_code = 0 if result["ready"] else 2
    except Exception as exc:
        print(json.dumps({"ready": False, "error_type": type(exc).__name__}), file=sys.stderr)
    finally:
        closed = await asyncio.gather(team_pool.close(), adapter.close(), return_exceptions=True)
        if any(isinstance(item, BaseException) for item in closed):
            if args.manifest.exists():
                artifact = json.loads(args.manifest.read_text(encoding="utf-8"))
                artifact.update(ready=False, blockers=["storage_close_failed"])
                write_manifest(args.manifest, artifact)
            print(json.dumps({"ready": False, "error_type": "StorageCloseFailed"}), file=sys.stderr)
            exit_code = 1
    return exit_code


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
