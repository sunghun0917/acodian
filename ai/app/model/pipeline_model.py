from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class WorklogPipelineRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    worklog_id: int = Field(alias="worklogId", ge=1)
    request_content: str | None = Field(default=None, alias="requestContent")
    work_content: str = Field(alias="workContent", min_length=1)
    author_id: int = Field(alias="authorId", ge=1)
    team_id: int = Field(alias="teamId", ge=1)
    department_id: int | None = Field(default=None, alias="departmentId", ge=1)
    task_kind: Literal["SUMMARY", "TAG"] = Field(alias="taskKind")
    request_id: UUID = Field(alias="requestId")
    input_hash: str = Field(alias="inputHash", pattern="^[0-9a-f]{64}$")


class PipelineAcceptedResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    worklog_id: int = Field(alias="worklogId")
    summary_task_id: str | None = Field(default=None, alias="summaryTaskId")
    tagging_task_id: str | None = Field(default=None, alias="taggingTaskId")
    request_id: UUID | None = Field(default=None, alias="requestId")
    status: Literal["ACCEPTED"] = "ACCEPTED"
