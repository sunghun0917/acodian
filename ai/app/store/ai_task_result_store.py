"""요약·태그·파일 작업의 최종 결과를 콜백 전에 보관한다."""

import asyncio
from dataclasses import dataclass
import json
import logging
import os
from pathlib import Path
import tempfile
from typing import Any
from uuid import UUID

from app.config.settings import settings
from app.store.worklog_index_result_store import _sync_directory

MAX_RESULT_BYTES = 8 * 1024 * 1024
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PendingAiTaskResult:
    worklog_id: int
    payload: dict[str, Any]

    def __post_init__(self) -> None:
        if type(self.worklog_id) is not int or self.worklog_id <= 0:
            raise ValueError("Invalid worklog ID")
        try:
            UUID(self.payload["requestId"])
            UUID(self.payload["executionId"])
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise ValueError("Invalid task identity") from exc
        if self.payload.get("status") not in {"COMPLETED", "FAILED"}:
            raise ValueError("Only terminal task results can be retained")
        if self.payload.get("taskKind") not in {"SUMMARY", "TAG", "FILE_SUMMARY"}:
            raise ValueError("Invalid task kind")

    @property
    def filename(self) -> str:
        return f"{self.worklog_id}-{UUID(self.payload['requestId'])}-{UUID(self.payload['executionId'])}.json"


def _folder() -> Path:
    return Path(settings.worklog_light_index_result_dir) / "general-v2"


def _read(path: Path) -> PendingAiTaskResult:
    if path.is_symlink() or path.stat().st_size > MAX_RESULT_BYTES:
        raise ValueError("Unsafe result record")
    record = PendingAiTaskResult(**json.loads(path.read_text(encoding="utf-8")))
    if record.filename != path.name:
        raise ValueError("Result filename does not match its identifiers")
    return record


def _persist(record: PendingAiTaskResult) -> PendingAiTaskResult:
    folder = _folder()
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, name = tempfile.mkstemp(prefix=".pending-", dir=folder)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump({"worklog_id": record.worklog_id, "payload": record.payload}, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        if temporary.stat().st_size > MAX_RESULT_BYTES:
            raise ValueError("Task result exceeds storage limit")
        target = folder / record.filename
        try:
            os.link(temporary, target)
        except FileExistsError:
            return _read(target)
        _sync_directory(folder)
        return record
    finally:
        temporary.unlink(missing_ok=True)


async def persist_ai_task_result(worklog_id: int, payload: dict[str, Any]) -> PendingAiTaskResult:
    """동일 실행의 최초 최종 결과를 파일로 확정한다."""
    return await asyncio.to_thread(_persist, PendingAiTaskResult(worklog_id, payload))


def _find(worklog_id: int, request_id: str) -> PendingAiTaskResult | None:
    if type(worklog_id) is not int or worklog_id <= 0:
        raise ValueError("Invalid worklog ID")
    for path in sorted(_folder().glob(f"{worklog_id}-{UUID(request_id)}-*.json")):
        try:
            return _read(path)
        except FileNotFoundError:
            continue
    return None


async def find_ai_task_result(worklog_id: int, request_id: str) -> PendingAiTaskResult | None:
    """같은 요청의 저장된 결과가 있으면 새 실행 대신 전달에 재사용한다."""
    return await asyncio.to_thread(_find, worklog_id, request_id)


def _list(after: str, limit: int) -> tuple[list[PendingAiTaskResult], str]:
    paths = sorted(_folder().glob("*.json"))
    batch = ([path for path in paths if path.name > after] or paths)[:limit]
    results = []
    for path in batch:
        try:
            results.append(_read(path))
        except FileNotFoundError:
            continue
        except (OSError, ValueError, TypeError, KeyError) as exc:
            logger.error("Invalid retained task result: file=%s errorType=%s", path.name, type(exc).__name__)
            continue
    return results, batch[-1].name if batch else ""


async def list_ai_task_results(after: str = "", limit: int = 20) -> tuple[list[PendingAiTaskResult], str]:
    """손상된 파일이 뒤쪽 결과 재전송을 가로막지 않도록 커서를 전진한다."""
    return await asyncio.to_thread(_list, after, limit)
