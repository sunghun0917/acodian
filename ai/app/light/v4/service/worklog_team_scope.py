"""기존 LightRAG workspace 격리를 재사용하는 팀별 검색/색인 경계."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from app.config.settings import Settings, settings
from app.light.v4.service.lightrag_adapter import (
    LightRagConfigurationError,
    LightRagQueryFailedError,
    LightRagQueryOptions,
    LightRagWorklogIndexAdapter,
    get_lightrag_worklog_index_adapter,
)
from app.light.v4.service.worklog_custom_kg_builder import build_worklog_custom_kg_document
from app.light.v4.service.worklog_custom_kg_source_reader import to_light_worklog_custom_kg_source
from app.light.v4.service.worklog_document_builder import build_worklog_light_document
from app.light.v4.service.worklog_source_reader import to_light_worklog_source
from app.light.v4.service.worklog_source_row_reader import WorklogLightSourceRowReader
from app.light.v4.store.worklog_source_store import LightWorklogSourceRow


class LightRagTeamScopeAdapter:
    """팀별 불변 adapter를 재사용하며 전역 인스턴스의 검색 범위를 바꾸지 않는다."""

    def __init__(
        self, *, settings_obj: Settings = settings,
        adapter_factory: Callable[..., Any] = LightRagWorklogIndexAdapter,
        source_reader: WorklogLightSourceRowReader | None = None,
        global_adapter_factory: Callable[..., Any] | None = None,
    ) -> None:
        """같은 서버/working directory 안에 버전별 팀 workspace를 구성한다."""
        self._settings = settings_obj.model_copy(deep=True)
        self._adapter_factory = adapter_factory
        self._source_reader = source_reader or WorklogLightSourceRowReader()
        self._global_adapter_factory = global_adapter_factory or get_lightrag_worklog_index_adapter
        self._adapters: dict[int, Any] = {}
        base = json.dumps([
            self._settings.lightrag_workspace.strip(), self._settings.lightrag_embedding_model,
            self._settings.embedding_dim,
        ], separators=(",", ":"))
        self._base_hash = hashlib.sha256(base.encode()).hexdigest()[:16]

    def adapter_for(self, team_id: int) -> Any:
        """외부 label/path를 받지 않고 검증된 팀 ID로만 adapter를 생성한다."""
        if type(team_id) is not int or team_id <= 0:
            raise LightRagConfigurationError("Team scope requires a positive integer ID")
        if team_id not in self._adapters:
            workspace = f"worklog_scope_v1_{self._base_hash}_team_{team_id}"
            scoped_settings = self._settings.model_copy(update={"lightrag_workspace": workspace})
            self._adapters[team_id] = self._adapter_factory(
                settings_obj=scoped_settings, expected_workspace=workspace,
            )
        return self._adapters[team_id]

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

    async def query_data(
        self, options: LightRagQueryOptions, allowed_team_ids: tuple[int, ...],
    ) -> dict[str, Any]:
        """허용 팀 안에서만 검색한 후보를 합치며 실패 시 전역 검색으로 우회하지 않는다."""
        data: dict[str, list[Any]] = {
            "chunks": [], "entities": [], "relationships": [], "references": [],
        }

        async def retrieve(team_id: int) -> dict[str, Any]:
            adapter = self.adapter_for(team_id)
            return await adapter.query_data(options)

        try:
            results = await asyncio.wait_for(
                asyncio.gather(*(retrieve(team_id) for team_id in sorted(set(allowed_team_ids)))),
                timeout=self._settings.lightrag_query_timeout_seconds,
            )
        except TimeoutError as exc:
            raise LightRagQueryFailedError("Team-scoped retrieval timed out") from exc
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
        self, worklog_ids: list[int], allowed_team_ids: tuple[int, ...] | None,
    ) -> dict[int, LightWorklogSourceRow]:
        """전역 완료·팀 native 처리 상태·최신 DB 생존 및 소속을 함께 검증한다."""
        if not worklog_ids or allowed_team_ids == ():
            return {}

        async def read() -> dict[int, LightWorklogSourceRow]:
            global_adapter = self._global_adapter_factory()
            completed = await global_adapter.get_processed_worklog_ids(worklog_ids)
            candidates = [item for item in dict.fromkeys(worklog_ids)
                          if item in completed]
            if not candidates:
                return {}
            current = await self._source_reader.fetch_source_rows(candidates)
            selected = {}
            for worklog_id, row in current.items():
                if row.worklog_id != worklog_id or worklog_id not in candidates:
                    raise ValueError("Source identity mismatch")
                if allowed_team_ids is None or row.team_id in allowed_team_ids:
                    selected[worklog_id] = row
            owners = await asyncio.gather(*(
                global_adapter.get_document_team_id(f"worklog-{key}") for key in selected
            ))
            selected = {key: row for (key, row), owner in zip(selected.items(), owners)
                        if owner == row.team_id}
            teams = sorted({row.team_id for row in selected.values()})
            processed = await asyncio.gather(*(
                self.adapter_for(team).get_processed_worklog_ids(list(selected), require_completed=False)
                for team in teams
            ))
            by_team = dict(zip(teams, processed))
            # DB 조회 중 시작된 재색인/삭제도 완료 게이트로 다시 차단한다.
            still_completed = await global_adapter.get_processed_worklog_ids(list(selected))
            return {key: row for key, row in selected.items()
                    if key in still_completed and key in by_team[row.team_id]}

        try:
            return await asyncio.wait_for(read(), timeout=self._settings.lightrag_query_timeout_seconds)
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
