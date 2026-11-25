"""팀 사본 전환을 확인한 운영자가 기존 전사 업무일지 문서를 정리한다.

실행: python -m app.util.delete_global_worklog_index --confirm-delete-global-index
"""

from __future__ import annotations

import argparse
import asyncio
import json

from app.config.settings import settings
from app.light.v4.service.lightrag_adapter import (
    LightRagWorklogIndexAdapter,
    get_lightrag_worklog_index_adapter,
)


def assert_global_workspace(name: str) -> None:
    """이름이 없는 workspace와 팀 사본에 대한 삭제를 거부한다."""
    if not name.strip() or name.strip().startswith("worklog_scope_"):
        raise ValueError("An explicit global workspace is required; team workspaces must not be deleted")


async def delete_global_index(
    *, global_adapter: LightRagWorklogIndexAdapter, document_ids: list[str],
) -> dict[str, int | list[str]]:
    """전사 업무일지 문서를 순차 삭제하고 실패 ID를 모아 반환한다."""
    assert_global_workspace(settings.lightrag_workspace)
    if any(not document_id.startswith("worklog-") for document_id in document_ids):
        raise ValueError("Only worklog- document IDs can be deleted")
    deleted = 0
    failed: list[str] = []
    for document_id in document_ids:
        try:
            await global_adapter.delete_document(document_id)
        except Exception:
            failed.append(document_id)
        else:
            deleted += 1
    return {"deleted": deleted, "failed": failed}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """삭제를 승인한 경우에만 CLI 실행을 허용한다."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm-delete-global-index", action="store_true", required=True,
                        help="팀 사본 전환 확인 후 기존 전사 업무일지 인덱스 삭제를 승인한다.")
    return parser.parse_args(argv)


async def main(argv: list[str] | None = None) -> int:
    """전사 doc_status의 모든 업무일지 ID를 모은 뒤 삭제한다."""
    parse_args(argv)
    assert_global_workspace(settings.lightrag_workspace)
    adapter = get_lightrag_worklog_index_adapter()
    try:
        # 삭제 전에 목록을 확정해야 페이지가 당겨지며 문서가 누락되지 않는다.
        document_ids = [document_id for document_id in await adapter.list_document_ids()
                        if document_id.startswith("worklog-")]
        result = await delete_global_index(global_adapter=adapter, document_ids=document_ids)
        print(json.dumps(result))
        return 2 if result["failed"] else 0
    finally:
        await adapter.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
