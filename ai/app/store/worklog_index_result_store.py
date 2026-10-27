"""Spring에 전달되지 않은 최종 결과를 AI의 공유 데이터 볼륨에 보관한다."""

import asyncio
from dataclasses import asdict, dataclass
import json
import logging
import os
from pathlib import Path
import tempfile
from uuid import UUID

from app.config.settings import settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PendingWorklogIndexResult:
    worklog_id: int
    request_id: str
    execution_id: str
    status: str
    error: str | None = None

    def __post_init__(self) -> None:
        if type(self.worklog_id) is not int or self.worklog_id <= 0:
            raise ValueError("Invalid worklog ID")
        object.__setattr__(self, "request_id", str(UUID(self.request_id)))
        object.__setattr__(self, "execution_id", str(UUID(self.execution_id)))
        if self.status not in {"COMPLETED", "FAILED"}:
            raise ValueError("Only terminal index results can be retained")
        if self.error is not None and (not isinstance(self.error, str) or len(self.error) > 1000):
            raise ValueError("Invalid index result error")

    @property
    def filename(self) -> str:
        return f"{self.worklog_id}-{self.request_id}-{self.execution_id}.json"

    def task_result(self) -> dict[str, int | str]:
        return {"worklog_id": self.worklog_id, "request_id": self.request_id,
                "execution_id": self.execution_id, "status": self.status}


def _read_result(path: Path) -> PendingWorklogIndexResult:
    if path.is_symlink() or path.stat().st_size > 16_384:
        raise ValueError("Unsafe result record")
    record = PendingWorklogIndexResult(**json.loads(path.read_text(encoding="utf-8")))
    if record.filename != path.name:
        raise ValueError("Result filename does not match its identifiers")
    return record


def _sync_directory(folder: Path) -> None:
    if os.name == "posix":
        descriptor = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _persist(record: PendingWorklogIndexResult) -> PendingWorklogIndexResult:
    folder = Path(settings.worklog_light_index_result_dir)
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = folder / record.filename
    descriptor, name = tempfile.mkstemp(prefix=".pending-", dir=folder)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(asdict(record), stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            # 원자적으로 게시하되 같은 실행의 최초 최종 결과를 덮지 않는다.
            os.link(temporary, target)
        except FileExistsError:
            try:
                return _read_result(target)
            except FileNotFoundError:
                # 다른 전달자가 ACK 뒤 제거한 경우 다시 파일을 만들지 않는다.
                return record
        _sync_directory(folder)
        return record
    finally:
        temporary.unlink(missing_ok=True)


async def persist_light_index_result(
    worklog_id: int, request_id: str, execution_id: str, status: str, error: str | None = None,
) -> PendingWorklogIndexResult:
    """DB에 접근하지 않고 파일을 확정한 뒤 실제 보관된 최초 결과를 반환한다."""
    record = PendingWorklogIndexResult(worklog_id, request_id, execution_id, status, error[:1000] if error else None)
    return await asyncio.to_thread(_persist, record)


def _find_pending(worklog_id: int, request_id: str) -> PendingWorklogIndexResult | None:
    prefix = f"{int(worklog_id)}-{UUID(request_id)}-"
    for path in sorted(Path(settings.worklog_light_index_result_dir).glob(f"{prefix}*.json")):
        try:
            return _read_result(path)
        except FileNotFoundError:
            continue
    return None


async def find_pending_light_index_result(worklog_id: int, request_id: str) -> PendingWorklogIndexResult | None:
    """같은 요청의 미전달 결과가 있으면 재색인 대신 기존 실행 결과를 재통보한다."""
    return await asyncio.to_thread(_find_pending, worklog_id, request_id)


def _list_pending(after: str, limit: int) -> tuple[list[PendingWorklogIndexResult], str]:
    files = sorted(Path(settings.worklog_light_index_result_dir).glob("*.json"))
    following = [path for path in files if path.name > after]
    batch = (following or files)[:limit]
    records = []
    for path in batch:
        try:
            records.append(_read_result(path))
        except FileNotFoundError:
            continue
        except (OSError, ValueError, TypeError) as exc:
            logger.error("Invalid retained index result: file=%s errorType=%s", path.name, type(exc).__name__)
    return records, batch[-1].name if batch else ""


async def list_pending_light_index_results(after: str = "", limit: int = 20) -> tuple[list[PendingWorklogIndexResult], str]:
    """회전 커서로 실패·손상 파일이 뒤의 정상 결과 전달을 막지 않게 한다."""
    return await asyncio.to_thread(_list_pending, after, limit)


def _acknowledge(record: PendingWorklogIndexResult) -> None:
    folder = Path(settings.worklog_light_index_result_dir)
    (folder / record.filename).unlink(missing_ok=True)
    if folder.exists():
        _sync_directory(folder)


async def acknowledge_light_index_result(record: PendingWorklogIndexResult) -> None:
    """Spring의 HTTP 성공 응답을 확인한 결과 파일만 제거한다."""
    await asyncio.to_thread(_acknowledge, record)
