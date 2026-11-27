"""기존 LightRAG workspace 격리를 재사용하는 팀별 검색/색인 경계."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import replace
from typing import Any

from app.config.settings import Settings, settings
from app.light.v4.service.lightrag_adapter import (
    LightRagConfigurationError,
    LightRagQueryFailedError,
    LightRagQueryOptions,
    LightRagQueryTimeoutError,
    LightRagWorklogIndexAdapter,
)
from app.light.v4.service.worklog_team_copy_lock import TeamCopyLocks
from app.light.v4.service.worklog_custom_kg_builder import build_worklog_custom_kg_document
from app.light.v4.service.worklog_custom_kg_source_reader import to_light_worklog_custom_kg_source
from app.light.v4.service.worklog_document_builder import build_worklog_light_document
from app.light.v4.service.worklog_source_reader import to_light_worklog_source
from app.light.v4.service.worklog_source_row_reader import WorklogLightSourceRowReader
from app.light.v4.store.worklog_source_store import LightWorklogSourceRow
from app.light.v4.store.worklog_team_copy_store import (
    TeamCopy, WorklogTeamCopyStore, get_worklog_team_copy_store,
)


class LightRagTeamScopeAdapter:
    """팀별 불변 adapter를 재사용하며 전역 인스턴스의 검색 범위를 바꾸지 않는다."""

    def __init__(
        self, *, settings_obj: Settings = settings,
        adapter_factory: Callable[..., Any] = LightRagWorklogIndexAdapter,
        source_reader: WorklogLightSourceRowReader | None = None,
        store: WorklogTeamCopyStore | None = None,
        locks: TeamCopyLocks | None = None,
    ) -> None:
        """같은 서버/working directory 안에 버전별 팀 workspace를 구성한다."""
        self._settings = settings_obj.model_copy(deep=True)
        self._adapter_factory = adapter_factory
        self._source_reader = source_reader or WorklogLightSourceRowReader()
        self._store = store if store is not None else get_worklog_team_copy_store()
        self._locks = locks if locks is not None else TeamCopyLocks(settings_obj=self._settings)
        self._adapters: dict[tuple[int, TeamCopy], Any] = {}
        base = json.dumps([
            self._settings.lightrag_workspace.strip(), self._settings.lightrag_embedding_model,
            self._settings.embedding_dim,
        ], separators=(",", ":"))
        self._base_hash = hashlib.sha256(base.encode()).hexdigest()[:16]

    def adapter_for(self, team_id: int, copy: TeamCopy = "a") -> Any:
        """외부 label/path를 받지 않고 검증된 팀 ID로만 adapter를 생성한다."""
        if type(team_id) is not int or team_id <= 0:
            raise LightRagConfigurationError("Team scope requires a positive integer ID")
        if copy not in ("a", "b"):
            raise LightRagConfigurationError("Team copy must be a or b")
        key = (team_id, copy)
        if key not in self._adapters:
            workspace = f"worklog_scope_v1_{self._base_hash}_team_{team_id}"
            shared = {}
            if copy == "b":
                workspace += "_b"
                shared["shared_llm_cache_from"] = self.adapter_for(team_id, "a")
            scoped_settings = self._settings.model_copy(update={"lightrag_workspace": workspace})
            self._adapters[key] = self._adapter_factory(
                settings_obj=scoped_settings, expected_workspace=workspace, **shared,
            )
        return self._adapters[key]

    async def index_source_rows(self, rows: list[LightWorklogSourceRow]) -> None:
        """본문과 팀 내 확정 KG를 같은 workspace에 색인한다."""
        by_team: dict[int, list[LightWorklogSourceRow]] = {}
        for row in rows:
            by_team.setdefault(row.team_id, []).append(row)
        for team_id, team_rows in by_team.items():
            adapter = self.adapter_for(team_id)
            await adapter.index_documents([
                build_worklog_light_document(to_light_worklog_source(row)) for row in team_rows
            ])
            # 다른 팀의 제목/관계를 내 팀 graph/vector description에 복사하지 않는다.
            kg_rows = [replace(row, direct_predecessors=[
                item for item in row.direct_predecessors if item.team_id == team_id
            ]) for row in team_rows]
            await adapter.index_custom_kg_documents([
                build_worklog_custom_kg_document(to_light_worklog_custom_kg_source(row))
                for row in kg_rows
            ])

    @asynccontextmanager
    async def read_copies(self, team_ids: tuple[int, ...]) -> AsyncIterator[dict[int, TeamCopy]]:
        """모든 허용 팀 사본을 빌리고 문맥 생성 종료와 예외 시 함께 반환한다."""
        try:
            async with AsyncExitStack() as stack:
                copies = {}
                for team in sorted(set(team_ids)):
                    copies[team] = await stack.enter_async_context(self._locks.read_copy(team, self._store))
                yield copies
        except (LightRagConfigurationError, LightRagQueryFailedError, LightRagQueryTimeoutError):
            raise
        except Exception as exc:
            raise LightRagQueryFailedError("Team read copy lookup failed") from exc

    async def query_data(
        self, options: LightRagQueryOptions, copies: dict[int, TeamCopy],
    ) -> dict[str, Any]:
        """허용 팀 안에서만 검색한 후보를 합치며 실패 시 전역 검색으로 우회하지 않는다."""
        data: dict[str, list[Any]] = {
            "chunks": [], "entities": [], "relationships": [], "references": [],
        }

        async def retrieve(team_id: int) -> dict[str, Any]:
            adapter = self.adapter_for(team_id, copies[team_id])
            return await adapter.query_data(options)

        tasks = [asyncio.create_task(retrieve(team)) for team in sorted(copies)]
        try:
            results = await asyncio.gather(*tasks)
        except BaseException:
            # 실패한 팀의 형제 검색도 끝내야 사본 읽기 토큰을 반환할 수 있다.
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        for result in results:
            if (isinstance(result, dict) and result.get("status") == "failure"
                    and isinstance(result.get("metadata"), dict)
                    and result["metadata"].get("failure_reason") == "no_results"):
                continue
            if not isinstance(result, dict) or result.get("status") != "success":
                raise LightRagQueryFailedError("Team-scoped retrieval failed")
            retrieved = result.get("data")
            if not isinstance(retrieved, dict):
                raise LightRagQueryFailedError("Team-scoped retrieval returned invalid data")
            for section in data:
                items = retrieved.get(section, [])
                if not isinstance(items, list):
                    raise LightRagQueryFailedError("Team-scoped retrieval returned invalid items")
                data[section].extend(items)
        return {"status": "success", "data": data}

    async def close(self) -> None:
        """생성한 팀별 storage 연결을 모두 종료한다."""
        results = await asyncio.gather(
            *(adapter.close() for adapter in self._adapters.values()), return_exceptions=True,
        )
        self._adapters.clear()
        for result in results:
            if isinstance(result, BaseException):
                raise result

    async def read_source_rows(
        self, worklog_ids: list[int], copies: dict[int, TeamCopy],
    ) -> dict[int, LightWorklogSourceRow]:
        """사본의 READY 내용만 쓰며 생존 여부와 현재 팀은 업무 DB로 확인한다."""
        if not worklog_ids or not copies:
            return {}

        try:
            candidates = list(dict.fromkeys(worklog_ids))
            snapshots = {}
            for team, copy in copies.items():
                entries = await self._store.entries(team, copy, candidates)
                for worklog_id, entry in entries.items():
                    row = entry.snapshot
                    if entry.state != "READY" or row is None:
                        continue
                    if (worklog_id not in candidates or entry.worklog_id != worklog_id
                            or entry.team_id != team or entry.copy != copy
                            or row.worklog_id != worklog_id or row.team_id != team):
                        raise ValueError("Snapshot identity mismatch")
                    snapshots[team, worklog_id] = row
            if not snapshots:
                return {}
            current = await self._source_reader.fetch_source_rows(candidates)
            selected = {}
            for worklog_id, row in current.items():
                if row.worklog_id != worklog_id or worklog_id not in candidates:
                    raise ValueError("Source identity mismatch")
                snapshot = snapshots.get((row.team_id, worklog_id))
                if row.team_id in copies and snapshot is not None:
                    selected[worklog_id] = snapshot
            return selected
        except (LightRagConfigurationError, LightRagQueryFailedError):
            raise
        except Exception as exc:
            raise LightRagQueryFailedError("Team current source lookup failed") from exc



_team_scope_adapter: LightRagTeamScopeAdapter | None = None


def get_lightrag_team_scope_adapter() -> LightRagTeamScopeAdapter:
    """수명주기가 고정된 팀 adapter pool을 반환한다."""
    global _team_scope_adapter
    if _team_scope_adapter is None:
        _team_scope_adapter = LightRagTeamScopeAdapter()
    return _team_scope_adapter


async def close_lightrag_team_scope_adapter() -> None:
    """FastAPI 종료 시 모든 팀별 연결을 닫는다."""
    global _team_scope_adapter
    adapter = _team_scope_adapter
    _team_scope_adapter = None
    if adapter is not None:
        await adapter.close()
