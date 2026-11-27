"""팀 쓰기 락 안에서 한 사본을 갱신하고 숨겨진 그래프 삭제 실패를 차단한다."""

from dataclasses import replace

from app.light.v4.service.worklog_custom_kg_builder import build_worklog_custom_kg_document
from app.light.v4.service.worklog_custom_kg_source_reader import to_light_worklog_custom_kg_source
from app.light.v4.service.worklog_document_builder import build_worklog_light_document
from app.light.v4.service.worklog_source_reader import to_light_worklog_source
from app.light.v4.service.worklog_team_copy_lock import TeamWriteLease
from app.light.v4.service.worklog_team_scope import LightRagTeamScopeAdapter
from app.light.v4.store.worklog_source_store import LightWorklogSourceRow
from app.light.v4.store.worklog_team_copy_store import DocumentFootprint, TeamCopy, WorklogTeamCopyStore


class CopyBrokenError(RuntimeError):
    """삭제 뒤 이전 문서의 그래프 참조가 남아 사본을 다시 만들어야 한다."""


async def apply_to_copy(
    pool: LightRagTeamScopeAdapter, store: WorklogTeamCopyStore, *, lease: TeamWriteLease,
    team_id: int, copy: TeamCopy, worklog_id: int, desired: LightWorklogSourceRow | None,
) -> None:
    """배출 확인된 쓰기 사본에 반영하고 성공했을 때만 스냅샷을 확정한다."""
    await lease.ensure_owned()
    adapter = pool.adapter_for(team_id, copy)
    document_id = f"worklog-{worklog_id}"
    entry = (await store.entries(team_id, copy, [worklog_id])).get(worklog_id)
    footprint = await adapter.document_footprint(document_id)
    if entry is not None and entry.state == "WORKING" and entry.footprint is not None:
        footprint = DocumentFootprint(
            entry.footprint.chunk_ids | footprint.chunk_ids,
            entry.footprint.entity_names | footprint.entity_names,
            entry.footprint.relation_keys | footprint.relation_keys,
        )
    await store.mark_working(team_id, copy, worklog_id, footprint)
    await adapter.delete_document(document_id)
    if await adapter.find_stale_references(footprint):
        await store.set_broken_copy(team_id, copy)
        raise CopyBrokenError("Deleted document still has graph references")
    if desired is None:
        await store.remove(team_id, copy, worklog_id)
        return
    await adapter.index_documents([build_worklog_light_document(to_light_worklog_source(desired))])
    kg_row = replace(desired, direct_predecessors=[
        item for item in desired.direct_predecessors if item.team_id == team_id
    ])
    await adapter.index_custom_kg_documents([
        build_worklog_custom_kg_document(to_light_worklog_custom_kg_source(kg_row)),
    ])
    await store.put_ready(team_id, copy, desired)


async def rebuild_copy(
    pool: LightRagTeamScopeAdapter, store: WorklogTeamCopyStore, *, lease: TeamWriteLease,
    team_id: int, copy: TeamCopy, rows: list[LightWorklogSourceRow],
) -> None:
    """망가짐 표시를 먼저 남기고 배출 확인된 사본을 스냅샷 목록으로 복구한다."""
    await lease.ensure_owned()
    await store.set_broken_copy(team_id, copy)
    await pool.adapter_for(team_id, copy).drop_all()
    await store.clear_copy(team_id, copy)
    for row in rows:
        await apply_to_copy(pool, store, lease=lease, team_id=team_id, copy=copy,
                            worklog_id=row.worklog_id, desired=row)
    await store.set_broken_copy(team_id, None)
