"""파일 AI 요약 라우터.

api 의 ``WorklogFileAiSummaryTrigger`` 가 AFTER_COMMIT 단계에서 호출한다.
처리가 길어 fire-and-forget 시맨틱이며, 본문은 Celery 큐에 위임 후 즉시 202 를 돌려준다.
실제 결과는 작업별 결과 API(``/internal/worklogs/{worklogId}/ai-tasks``)로 api 에 전달된다.
"""

from fastapi import APIRouter, Depends, status

from app.model.file_model import FileSummaryAcceptedResponse, FileSummaryRequest
from app.service.file_summary_service import FileSummaryService
from app.security.internal_token import require_internal_token

router = APIRouter(prefix="/summarize", tags=["summarize"])
file_summary_service = FileSummaryService()


@router.post(
    "/files",
    response_model=FileSummaryAcceptedResponse,
    response_model_exclude_unset=True,
    dependencies=[Depends(require_internal_token)],
    status_code=status.HTTP_202_ACCEPTED,
)
async def trigger_file_summary(
    request: FileSummaryRequest,
) -> FileSummaryAcceptedResponse:
    return await file_summary_service.trigger_file_summary(request)
