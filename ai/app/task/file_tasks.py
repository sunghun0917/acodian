"""실행 권위를 승인받은 파일 단위 AI 요약 Celery 태스크."""

import asyncio
import shutil

from app.client.gemini_client import get_gemini_client
from app.client.s3_downloader import S3Downloader
from app.service.ai_task_executor import execute_ai_task
from app.service.file_summary.dispatch import (
    resolve_source_type,
    summarize_by_source,
)
from app.task.celery_app import celery_app
from app.task.task_identity import validate_task_identity


@celery_app.task(name="file.summary")
def generate_file_summary(
    file_id: int,
    worklog_id: int,
    storage_key: str,
    original_name: str,
    file_extension: str,
    metadata: dict | None = None,
) -> dict[str, int | str]:
    validate_task_identity(metadata, "FILE_SUMMARY", file_id)
    return asyncio.run(execute_ai_task(worklog_id, metadata, lambda: _generate_identified_file_summary(
        storage_key, original_name, file_extension,
    )))


async def _generate_identified_file_summary(storage_key: str, original_name: str, file_extension: str) -> dict:
    """파일 요약만 생성하고 결과 적용은 현재 실행 권한 검증에 맡긴다."""
    source_type = resolve_source_type(file_extension)
    download = asyncio.create_task(asyncio.to_thread(S3Downloader().download_to_tempfile, storage_key, original_name))
    local_path = None
    try:
        local_path = await asyncio.shield(download)
        summary = await summarize_by_source(get_gemini_client(), source_type, local_path)
        return {"aiSummary": summary}
    finally:
        if local_path is not None:
            shutil.rmtree(local_path.parent, ignore_errors=True)
        else:
            download.add_done_callback(_cleanup_cancelled_download)


def _cleanup_cancelled_download(download: asyncio.Task) -> None:
    """취소 후에도 완료된 다운로드의 임시파일은 백그라운드에서 제거한다."""
    try:
        path = download.result()
    except (Exception, asyncio.CancelledError):
        return
    shutil.rmtree(path.parent, ignore_errors=True)
