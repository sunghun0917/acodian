"""Spring API가 소유하는 업무 AI 작업의 실행 권위와 결과를 통보한다."""

from typing import Any

from app.client.base_api_client import BaseApiClient
from app.config.settings import settings


class AiTaskClient(BaseApiClient):
    async def _post(self, worklog_id: int, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        token = settings.ai_internal_token
        if not token:
            raise RuntimeError("AI_INTERNAL_TOKEN is not configured")
        response = await self.client.post(
            self.api_path(f"/api/internal/worklogs/{worklog_id}/ai-tasks/{operation}"),
            headers={"X-AI-Internal-Token": token},
            json=payload,
        )
        response.raise_for_status()
        return response.json()

    async def start(self, worklog_id: int, identity: dict[str, Any]) -> dict[str, Any]:
        """같은 요청의 중복 실행을 막기 위해 서버의 시작 승인을 얻는다."""
        return await self._post(worklog_id, "start", identity)

    async def attempt_result(self, worklog_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        """일시적인 실행 실패와 재시도 가능성을 통보한다."""
        return await self._post(worklog_id, "attempt-result", payload)

    async def result(self, worklog_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        """확정된 작업 결과를 해당 실행 식별자와 함께 전달한다."""
        return await self._post(worklog_id, "result", payload)
