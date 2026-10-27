"""Spring API LightRAG 색인 상태 통보 내부 클라이언트."""

import logging
from typing import Any

from app.client.base_api_client import BaseApiClient
from app.config.settings import settings

logger = logging.getLogger(__name__)


class WorklogIndexClient(BaseApiClient):
    """업무일지 색인 시작 승인 및 결과 통보를 Spring 백엔드로 전달한다."""

    def _get_headers(self) -> dict[str, str]:
        token = settings.ai_internal_token
        if not token:
            raise RuntimeError("AI_INTERNAL_TOKEN is not configured")
        return {"X-AI-Internal-Token": token}

    async def report_start(self, worklog_id: int, request_id: str, execution_id: str) -> bool:
        """색인 시작 승인을 요청하며 execute 여부를 반환한다."""
        path = f"/api/internal/worklogs/{worklog_id}/light-index/start"
        payload = {"requestId": request_id, "executionId": execution_id}
        response = await self.client.post(
            self.api_path(path),
            headers=self._get_headers(),
            json=payload,
        )
        response.raise_for_status()
        data = response.json()
        return bool(data.get("execute", False))

    async def report_result(
        self,
        worklog_id: int,
        request_id: str,
        execution_id: str,
        status: str,
        error: str | None = None,
    ) -> None:
        """색인 완료 또는 실패 결과를 Spring에 통보한다."""
        path = f"/api/internal/worklogs/{worklog_id}/light-index/result"
        payload: dict[str, Any] = {
            "requestId": request_id,
            "executionId": execution_id,
            "status": status,
            "error": error[:1000] if error else None,
        }
        response = await self.client.post(
            self.api_path(path),
            headers=self._get_headers(),
            json=payload,
        )
        response.raise_for_status()
