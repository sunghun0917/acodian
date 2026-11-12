"""큐에서 재전달된 일반 AI 작업도 실행 전에 식별자를 검증한다."""

import re
from uuid import UUID


def validate_task_identity(metadata: dict | None, task_kind: str, target_id: int) -> None:
    """식별자가 없거나 작업·대상과 다르면 AI 호출 전에 거절한다."""
    if not isinstance(metadata, dict) or set(metadata) != {"requestId", "inputHash", "taskKind", "targetId"}:
        raise ValueError("Invalid task identity")
    try:
        UUID(metadata["requestId"])
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError("Invalid task identity") from exc
    if (metadata["taskKind"] != task_kind or type(metadata["targetId"]) is not int or metadata["targetId"] != target_id
            or not isinstance(metadata["inputHash"], str) or re.fullmatch(r"[0-9a-f]{64}", metadata["inputHash"]) is None):
        raise ValueError("Invalid task identity")
