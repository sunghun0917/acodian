"""PR 리뷰 에이전트 입출력 모델을 정의한다."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


Severity = Literal["P1", "P2", "P3", "P4"]


class ReviewFinding(BaseModel):
    """PR 코멘트로 게시 가능한 근거 기반 발견 사항이다."""

    model_config = ConfigDict(extra="forbid")

    severity: Severity
    title: str = Field(min_length=1, max_length=120)
    body: str = Field(min_length=1, max_length=1200)
    evidence: list[str] = Field(max_length=4)


class ReviewResult(BaseModel):
    """최종 PR 리뷰 결과다."""

    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1, max_length=1500)
    findings: list[ReviewFinding] = Field(max_length=10)
