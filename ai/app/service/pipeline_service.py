from app.model.pipeline_model import PipelineAcceptedResponse, WorklogPipelineRequest
from app.task.tagging_tasks import generate_worklog_tags
from app.task.worklog_tasks import generate_worklog_summary


class PipelineService:
    async def trigger_worklog_pipeline(
        self,
        request: WorklogPipelineRequest,
    ) -> PipelineAcceptedResponse:
        summary_task = None
        tagging_task = None
        identity = None
        if request.request_id is not None:
            identity = {
                "requestId": str(request.request_id),
                "inputHash": request.input_hash, "taskKind": request.task_kind, "targetId": request.worklog_id,
            }
        if request.task_kind in (None, "SUMMARY"):
            args = (request.worklog_id, request.request_content, request.work_content)
            summary_task = generate_worklog_summary.delay(*args, identity) if identity else generate_worklog_summary.delay(*args)
        if request.task_kind in (None, "TAG"):
            args = (request.worklog_id, request.work_content)
            tagging_task = generate_worklog_tags.delay(*args, identity) if identity else generate_worklog_tags.delay(*args)

        return PipelineAcceptedResponse(
            worklog_id=request.worklog_id,
            summary_task_id=summary_task.id if summary_task else None,
            tagging_task_id=tagging_task.id if tagging_task else None,
            status="ACCEPTED",
            **({"request_id": request.request_id} if request.request_id is not None else {}),
        )
