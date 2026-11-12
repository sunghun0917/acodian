import asyncio
from app.service.ai_task_executor import execute_ai_task
from app.service.summary_service import SummaryService
from app.task.celery_app import celery_app
from app.task.task_identity import validate_task_identity


@celery_app.task(name="worklog.summary", bind=True)
def generate_worklog_summary(
    self,
    worklog_id: int,
    request_content: str | None,
    work_content: str,
    metadata: dict | None = None,
) -> dict[str, int | str]:
    validate_task_identity(metadata, "SUMMARY", worklog_id)
    return asyncio.run(execute_ai_task(
        worklog_id, metadata, lambda: _generate_identified_summary(request_content, work_content),
    ))


async def _generate_identified_summary(request_content: str | None, work_content: str) -> dict:
    """식별된 작업은 결과 반영을 권한 검증을 수행하는 콜백에만 맡긴다."""
    summary = await SummaryService().generate_summary(request_content=request_content, work_content=work_content)
    return {"aiSummary": summary}
