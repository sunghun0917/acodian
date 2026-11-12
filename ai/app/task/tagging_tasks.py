import asyncio
from app.client.tagging_client import TaggingClient
from app.model.tagging_model import MetaTag, NewTag
from app.service.ai_task_executor import execute_ai_task
from app.service.tagging_service import TaggingService
from app.task.celery_app import celery_app
from app.task.task_identity import validate_task_identity


@celery_app.task(name="worklog.tagging", bind=True)
def generate_worklog_tags(
    self,
    worklog_id: int,
    work_content: str,
    metadata: dict | None = None,
) -> dict[str, int | list[str] | list[dict[str, str]]]:
    validate_task_identity(metadata, "TAG", worklog_id)
    return asyncio.run(execute_ai_task(worklog_id, metadata, lambda: _generate_identified_tags(work_content)))


async def _generate_identified_tags(work_content: str) -> dict:
    """태그를 생성해 결과 payload를 반환한다."""
    async with TaggingClient() as client:
        existing_tags = await client.list_tags()
        result = await TaggingService().generate_tags(work_content=work_content, existing_tags=existing_tags)
    return {
        "existingTagIds": _find_existing_tag_ids(result.existing_tags, existing_tags),
        "newTags": _dump_new_tags(result.new_tags),
    }


def _find_existing_tag_ids(
    tag_names: list[str],
    existing_tags: list[MetaTag],
) -> list[int]:
    tag_id_by_name = {tag.tag_name: tag.tag_id for tag in existing_tags}
    return [
        tag_id_by_name[tag_name]
        for tag_name in tag_names
        if tag_name in tag_id_by_name
    ]


def _dump_new_tags(new_tags: list[NewTag]) -> list[dict[str, str]]:
    return [
        tag.model_dump(by_alias=True)
        for tag in new_tags
    ]
