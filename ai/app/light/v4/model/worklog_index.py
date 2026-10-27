"""LightRAG v4 업무일지 index 요청/응답 모델."""

from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field, PositiveInt

DEFAULT_LIGHTRAG_INDEX_MAX_BATCH_SIZE = 10


class WorklogLightIndexRequest(BaseModel):
    """POST /ai/light/worklogs-v4/index 및 /reindex 요청."""

    model_config = ConfigDict(extra="forbid", validate_by_alias=True, validate_by_name=False)

    worklog_ids: list[PositiveInt] = Field(
        alias="worklogIds",
        min_length=1,
        max_length=DEFAULT_LIGHTRAG_INDEX_MAX_BATCH_SIZE,
    )
    request_id: UUID = Field(alias="requestId")


class WorklogLightDeleteRequest(BaseModel):
    """POST /ai/light/worklogs-v4/delete 요청."""

    model_config = ConfigDict(extra="forbid", validate_by_alias=True, validate_by_name=False)

    worklog_ids: list[PositiveInt] = Field(
        alias="worklogIds",
        min_length=1,
        max_length=DEFAULT_LIGHTRAG_INDEX_MAX_BATCH_SIZE,
    )
    request_id: UUID = Field(alias="requestId")


class WorklogLightIndexItem(BaseModel):
    """업무일지 1건의 접수 결과 (HTTP 202)."""

    model_config = ConfigDict(populate_by_name=True)

    worklog_id: int = Field(alias="worklogId")
    request_id: UUID = Field(alias="requestId")
    task_id: str = Field(alias="taskId")


class WorklogLightIndexResponse(BaseModel):
    """업무일지 LightRAG index 접수 응답 (HTTP 202)."""

    model_config = ConfigDict(populate_by_name=True)

    items: list[WorklogLightIndexItem]


class WorklogLightDeleteItem(BaseModel):
    """업무일지 1건의 삭제 접수 결과 (HTTP 202)."""

    model_config = ConfigDict(populate_by_name=True)

    worklog_id: int = Field(alias="worklogId")
    request_id: UUID = Field(alias="requestId")
    task_id: str = Field(alias="taskId")


class WorklogLightDeleteResponse(BaseModel):
    """업무일지 저장소 삭제 접수 응답 (HTTP 202)."""

    model_config = ConfigDict(populate_by_name=True)

    items: list[WorklogLightDeleteItem]


class WorklogLightIndexExecutionItem(BaseModel):
    """업무일지 1건의 LightRAG index 실제 실행 결과."""

    model_config = ConfigDict(populate_by_name=True)

    worklog_id: int = Field(alias="worklogId")
    indexed: bool
    error: str | None = None


class WorklogLightIndexExecutionResponse(BaseModel):
    """업무일지 LightRAG index 실제 실행 응답."""

    model_config = ConfigDict(populate_by_name=True)

    items: list[WorklogLightIndexExecutionItem]


class WorklogLightDeleteExecutionItem(BaseModel):
    """업무일지 1건의 저장소 삭제 실제 실행 결과."""

    model_config = ConfigDict(populate_by_name=True)

    worklog_id: int = Field(alias="worklogId")
    deleted: bool
    error: str | None = None


class WorklogLightDeleteExecutionResponse(BaseModel):
    """업무일지 저장소 삭제 실제 실행 응답."""

    model_config = ConfigDict(populate_by_name=True)

    items: list[WorklogLightDeleteExecutionItem]
