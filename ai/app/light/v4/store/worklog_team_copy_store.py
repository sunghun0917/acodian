"""LightRAG DB에 팀 읽기 포인터와 사본별 확정 스냅샷을 보관한다."""

import asyncio
from dataclasses import asdict, dataclass
import hashlib
import json
from typing import Any, Literal

from sqlalchemy import CursorResult, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from app.config.settings import Settings, settings
from app.light.v4.store.worklog_source_store import (
    LightWorklogPredecessor, LightWorklogSourceRow, LightWorklogTag,
)


TeamCopy = Literal["a", "b"]


def other_copy(copy: TeamCopy) -> TeamCopy:
    """현재 사본의 반대쪽을 반환한다."""
    return "b" if copy == "a" else "a"


def snapshot_fingerprint(row: LightWorklogSourceRow | None) -> str | None:
    """태그와 선행 업무 순서를 정규화한 원본 전체 지문을 반환한다."""
    if row is None:
        return None
    source = snapshot_to_json(row)
    source["tags"].sort(key=lambda item: (item["tag_id"], item["tag_name"], item["description"] or ""))
    source["direct_predecessors"].sort(key=lambda item: (
        item["worklog_id"], item["title"], item["team_id"] or 0,
    ))
    return hashlib.sha256(json.dumps(source, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def snapshot_to_json(row: LightWorklogSourceRow) -> dict[str, Any]:
    """중첩 태그와 선행 업무를 포함해 원본을 JSON 값으로 변환한다."""
    return asdict(row)


def snapshot_from_json(data: dict[str, Any]) -> LightWorklogSourceRow:
    """JSON 스냅샷을 중첩 source dataclass까지 복원한다."""
    return LightWorklogSourceRow(**{
        **data,
        "tags": [LightWorklogTag(**tag) for tag in data["tags"]],
        "direct_predecessors": [LightWorklogPredecessor(**row) for row in data["direct_predecessors"]],
    })


@dataclass(frozen=True)
class DocumentFootprint:
    """문서 삭제가 끊겨도 검증할 수 있는 삭제 전 참조 기록."""

    chunk_ids: frozenset[str]
    entity_names: frozenset[str]
    relation_keys: frozenset[tuple[str, str]]


def footprint_to_json(footprint: DocumentFootprint) -> dict[str, Any]:
    """삭제 전 참조 집합을 정렬된 JSON 목록으로 변환한다."""
    return {
        "chunk_ids": sorted(footprint.chunk_ids),
        "entity_names": sorted(footprint.entity_names),
        "relation_keys": [list(pair) for pair in sorted(footprint.relation_keys)],
    }


def footprint_from_json(data: dict[str, Any]) -> DocumentFootprint:
    """JSON 목록에서 불변 삭제 전 참조 집합을 복원한다."""
    return DocumentFootprint(
        chunk_ids=frozenset(data["chunk_ids"]),
        entity_names=frozenset(data["entity_names"]),
        relation_keys=frozenset((source, target) for source, target in data["relation_keys"]),
    )


@dataclass(frozen=True)
class CopyEntry:
    """사본 한 벌에 보관된 업무일지의 반영 상태."""

    team_id: int
    copy: TeamCopy
    worklog_id: int
    state: Literal["READY", "WORKING"]
    fingerprint: str | None
    snapshot: LightWorklogSourceRow | None
    footprint: DocumentFootprint | None


class WorklogTeamCopyStore:
    """업무 DB와 분리된 LightRAG DB에서 사본 상태를 원자적으로 갱신한다."""

    def __init__(self, *, settings_obj: Settings = settings) -> None:
        self._settings = settings_obj
        self._engine: AsyncEngine | None = None
        self._initialized = False
        self._initialize_lock = asyncio.Lock()

    async def _initialize(self) -> None:
        async with self._initialize_lock:
            if self._initialized:
                return
            if self._engine is None:
                dsn = self._settings.lightrag_pg_dsn.strip()
                if not dsn:
                    raise ValueError("lightrag_pg_dsn is required for team copies")
                url = make_url(dsn)
                if url.get_backend_name() != "postgresql":
                    raise ValueError("Team copies require a PostgreSQL LightRAG database")
                # Celery 작업마다 이벤트 루프가 바뀌어도 이전 루프의 연결을 재사용하지 않는다.
                self._engine = create_async_engine(url.set(drivername="postgresql+asyncpg"), poolclass=NullPool)
            async with self._engine.begin() as connection:
                await connection.execute(text("""
                    CREATE TABLE IF NOT EXISTS worklog_team_copy_pointer (
                        team_id BIGINT PRIMARY KEY,
                        read_copy TEXT NOT NULL CHECK (read_copy IN ('a', 'b')),
                        broken_copy TEXT NULL CHECK (broken_copy IN ('a', 'b'))
                    )
                """))
                await connection.execute(text("""
                    CREATE TABLE IF NOT EXISTS worklog_team_copy_entry (
                        team_id BIGINT NOT NULL,
                        copy TEXT NOT NULL CHECK (copy IN ('a', 'b')),
                        worklog_id BIGINT NOT NULL,
                        state TEXT NOT NULL CHECK (state IN ('READY', 'WORKING')),
                        fingerprint TEXT NULL,
                        snapshot JSONB NULL,
                        footprint JSONB NULL,
                        PRIMARY KEY (team_id, copy, worklog_id)
                    )
                """))
            self._initialized = True

    async def _execute(self, sql: str, parameters: dict[str, Any]) -> CursorResult[Any]:
        await self._initialize()
        async with self._engine.begin() as connection:
            return await connection.execute(text(sql), parameters)

    async def read_copy(self, team_id: int) -> TeamCopy:
        """현재 포인터를 캐시 없이 읽으며 미등록 팀은 a로 본다."""
        result = await self._execute(
            "SELECT read_copy FROM worklog_team_copy_pointer WHERE team_id = :team_id", {"team_id": team_id},
        )
        return result.scalar_one_or_none() or "a"

    async def flip_read_copy(
        self, team_id: int, *, expected: TeamCopy, new: TeamCopy, mark_other_broken: bool = False,
    ) -> bool:
        """기대 포인터가 일치할 때만 전환하고 필요하면 이전 사본을 함께 망가짐 표시한다."""
        await self._initialize()
        parameters = {"team_id": team_id, "expected": expected, "new": new}
        broken_update = ", broken_copy = :expected" if mark_other_broken else ""
        async with self._engine.begin() as connection:
            result = await connection.execute(text(f"""
                UPDATE worklog_team_copy_pointer SET read_copy = :new{broken_update}
                WHERE team_id = :team_id AND read_copy = :expected
                RETURNING team_id
            """), parameters)
            if result.scalar_one_or_none() is not None:
                return True
            if expected != "a":
                return False
            result = await connection.execute(text("""
                INSERT INTO worklog_team_copy_pointer (team_id, read_copy, broken_copy)
                VALUES (:team_id, :new, :broken)
                ON CONFLICT (team_id) DO NOTHING
                RETURNING team_id
            """), {**parameters, "broken": expected if mark_other_broken else None})
            return result.scalar_one_or_none() is not None

    async def broken_copy(self, team_id: int) -> TeamCopy | None:
        """팀의 망가진 사본 표시를 읽는다."""
        result = await self._execute(
            "SELECT broken_copy FROM worklog_team_copy_pointer WHERE team_id = :team_id", {"team_id": team_id},
        )
        return result.scalar_one_or_none()

    async def set_broken_copy(self, team_id: int, copy: TeamCopy | None) -> None:
        """읽기 포인터를 유지하며 망가진 사본 표시만 저장한다."""
        await self._execute("""
            INSERT INTO worklog_team_copy_pointer (team_id, read_copy, broken_copy)
            VALUES (:team_id, 'a', :copy)
            ON CONFLICT (team_id) DO UPDATE SET broken_copy = EXCLUDED.broken_copy
        """, {"team_id": team_id, "copy": copy})

    async def entries(
        self, team_id: int, copy: TeamCopy, worklog_ids: list[int] | None = None,
    ) -> dict[int, CopyEntry]:
        """팀과 사본으로 격리한 상태 행을 업무일지 ID별로 반환한다."""
        if worklog_ids == []:
            return {}
        id_filter = " AND worklog_id = ANY(:worklog_ids)" if worklog_ids is not None else ""
        parameters: dict[str, Any] = {"team_id": team_id, "copy": copy}
        if worklog_ids is not None:
            parameters["worklog_ids"] = worklog_ids
        result = await self._execute(f"""
            SELECT team_id, copy, worklog_id, state, fingerprint, snapshot, footprint
            FROM worklog_team_copy_entry WHERE team_id = :team_id AND copy = :copy{id_filter}
        """, parameters)
        return {
            row["worklog_id"]: CopyEntry(
                team_id=row["team_id"], copy=row["copy"], worklog_id=row["worklog_id"],
                state=row["state"], fingerprint=row["fingerprint"],
                snapshot=snapshot_from_json(row["snapshot"]) if row["snapshot"] is not None else None,
                footprint=footprint_from_json(row["footprint"]) if row["footprint"] is not None else None,
            )
            for row in result.mappings()
        }

    async def mark_working(
        self, team_id: int, copy: TeamCopy, worklog_id: int, footprint: DocumentFootprint,
    ) -> None:
        """변경 전에 확정 내용을 지우고 재시작에 필요한 삭제 전 참조를 남긴다."""
        await self._execute("""
            INSERT INTO worklog_team_copy_entry
                (team_id, copy, worklog_id, state, fingerprint, snapshot, footprint)
            VALUES (:team_id, :copy, :worklog_id, 'WORKING', NULL, NULL, CAST(:footprint AS JSONB))
            ON CONFLICT (team_id, copy, worklog_id) DO UPDATE
                SET state = 'WORKING', fingerprint = NULL, snapshot = NULL, footprint = EXCLUDED.footprint
        """, {"team_id": team_id, "copy": copy, "worklog_id": worklog_id,
              "footprint": json.dumps(footprint_to_json(footprint), ensure_ascii=False)})

    async def put_ready(self, team_id: int, copy: TeamCopy, row: LightWorklogSourceRow) -> None:
        """성공한 반영 내용과 지문을 확정하고 삭제 전 참조를 지운다."""
        await self._execute("""
            INSERT INTO worklog_team_copy_entry
                (team_id, copy, worklog_id, state, fingerprint, snapshot, footprint)
            VALUES (:team_id, :copy, :worklog_id, 'READY', :fingerprint, CAST(:snapshot AS JSONB), NULL)
            ON CONFLICT (team_id, copy, worklog_id) DO UPDATE
                SET state = 'READY', fingerprint = EXCLUDED.fingerprint,
                    snapshot = EXCLUDED.snapshot, footprint = NULL
        """, {"team_id": team_id, "copy": copy, "worklog_id": row.worklog_id,
              "fingerprint": snapshot_fingerprint(row),
              "snapshot": json.dumps(snapshot_to_json(row), ensure_ascii=False)})

    async def remove(self, team_id: int, copy: TeamCopy, worklog_id: int) -> None:
        """한 사본의 한 업무일지 상태만 제거한다."""
        await self._execute("""
            DELETE FROM worklog_team_copy_entry
            WHERE team_id = :team_id AND copy = :copy AND worklog_id = :worklog_id
        """, {"team_id": team_id, "copy": copy, "worklog_id": worklog_id})

    async def clear_copy(self, team_id: int, copy: TeamCopy) -> None:
        """한 팀의 지정 사본 내용 표만 비운다."""
        await self._execute("""
            DELETE FROM worklog_team_copy_entry WHERE team_id = :team_id AND copy = :copy
        """, {"team_id": team_id, "copy": copy})

    async def teams_containing(self, worklog_id: int) -> set[int]:
        """반영 중 행까지 포함해 업무일지가 남아 있는 팀을 찾는다."""
        result = await self._execute("""
            SELECT DISTINCT team_id FROM worklog_team_copy_entry WHERE worklog_id = :worklog_id
        """, {"worklog_id": worklog_id})
        return set(result.scalars())

    async def differing_worklog_ids(self, team_id: int, *, read: TeamCopy) -> list[int]:
        """쓰기 사본의 반영 중 행과 존재 여부 또는 지문이 다른 업무일지를 찾는다."""
        result = await self._execute("""
            SELECT COALESCE(r.worklog_id, w.worklog_id) AS worklog_id
            FROM (SELECT worklog_id, fingerprint FROM worklog_team_copy_entry
                  WHERE team_id = :team_id AND copy = :read) r
            FULL OUTER JOIN (SELECT worklog_id, fingerprint, state FROM worklog_team_copy_entry
                             WHERE team_id = :team_id AND copy = :write) w
                ON r.worklog_id = w.worklog_id
            WHERE w.state = 'WORKING' OR r.worklog_id IS NULL OR w.worklog_id IS NULL
                OR r.fingerprint IS DISTINCT FROM w.fingerprint
            ORDER BY worklog_id
        """, {"team_id": team_id, "read": read, "write": other_copy(read)})
        return list(result.scalars())

    async def close(self) -> None:
        """DB 연결 자원을 해제하고 다음 사용 때 다시 초기화한다."""
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None
        self._initialized = False


_store: WorklogTeamCopyStore | None = None


def get_worklog_team_copy_store() -> WorklogTeamCopyStore:
    """모듈 단위 공유 사본 저장소를 지연 생성한다."""
    global _store
    if _store is None:
        _store = WorklogTeamCopyStore()
    return _store
