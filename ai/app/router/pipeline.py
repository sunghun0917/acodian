from fastapi import APIRouter, Depends, status

from app.model.pipeline_model import PipelineAcceptedResponse, WorklogPipelineRequest
from app.service.pipeline_service import PipelineService
from app.security.internal_token import require_internal_token

router = APIRouter(prefix="/pipeline", tags=["pipeline"])
pipeline_service = PipelineService()


@router.post(
    "/worklog",
    response_model=PipelineAcceptedResponse,
    response_model_exclude_unset=True,
    dependencies=[Depends(require_internal_token)],
    status_code=status.HTTP_202_ACCEPTED,
)
async def trigger_worklog_pipeline(
    request: WorklogPipelineRequest,
) -> PipelineAcceptedResponse:
    return await pipeline_service.trigger_worklog_pipeline(request)
