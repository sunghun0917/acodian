import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.config.settings import Settings, settings
from app.light.v4.model.worklog_index import (
    WorklogLightDeleteItem,
    WorklogLightDeleteResponse,
    WorklogLightIndexItem,
    WorklogLightIndexResponse,
)
from app.light.v4.service.lightrag_adapter import (
    LightRagConfigurationError,
    LightRagDeleteFailedError,
    LightRagDeleteTimeoutError,
    LightRagInsertFailedError,
    LightRagInsertTimeoutError,
    get_lightrag_worklog_index_adapter,
)
from app.light.v4.service.worklog_custom_kg_builder import (
    LightRagWorklogCustomKgDocument,
    build_worklog_custom_kg_document,
)
from app.light.v4.service.worklog_document_builder import (
    LightRagWorklogDocument,
    build_worklog_light_document,
)
from app.light.v4.service.worklog_custom_kg_source_reader import (
    to_light_worklog_custom_kg_source,
)
from app.light.v4.service.worklog_source_reader import to_light_worklog_source
from app.light.v4.service.worklog_source_row_reader import WorklogLightSourceRowReader
from app.light.v4.service.worklog_query_cache import WorklogQueryCache
from app.light.v4.service.worklog_team_scope import get_lightrag_team_scope_adapter
from app.light.v4.service.worklog_mutation_lock import worklog_mutation_locks
from app.light.v4.store.worklog_source_store import LightWorklogSourceRow

ERROR_WORKLOG_NOT_FOUND = "WORKLOG_NOT_FOUND"
ERROR_LIGHTRAG_INSERT_TIMEOUT = "LIGHTRAG_INSERT_TIMEOUT"
ERROR_LIGHTRAG_INSERT_FAILED = "LIGHTRAG_INSERT_FAILED"
ERROR_LIGHTRAG_DELETE_TIMEOUT = "LIGHTRAG_DELETE_TIMEOUT"
ERROR_LIGHTRAG_DELETE_FAILED = "LIGHTRAG_DELETE_FAILED"
ERROR_LIGHTRAG_COMPENSATION_FAILED = "LIGHTRAG_COMPENSATION_FAILED"

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PreparedLightWorklogDocuments:
    """LightRAG insert 직전의 업무일지 text/custom KG 문서 준비 결과."""

    documents: list[LightRagWorklogDocument]
    custom_kg_documents: list[LightRagWorklogCustomKgDocument]
    found_worklog_ids: list[int]
    missing_worklog_ids: list[int]
    source_rows: list[LightWorklogSourceRow]


class LightWorklogIndexService:
    """업무일지 LightRAG 통합 index 유스케이스 진입점.

    한 번의 source reader 호출 결과에서 text document와 confirmed relation custom KG
    document를 함께 만들고, 둘 다 성공한 업무일지만 index 성공으로 응답한다.
    """

    def __init__(
        self, *, cache: WorklogQueryCache | None = None, settings_obj: Settings = settings,
        adapter_factory: Callable[[], Any] | None = None,
        team_adapter_factory: Callable[[], Any] | None = None,
        source_reader: WorklogLightSourceRowReader | None = None,
    ) -> None:
        """인덱싱/재색인/삭제 후 캐시 무효화에 사용할 Redis 캐시를 구성한다."""
        self._cache = cache
        self._settings = settings_obj
        self._adapter_factory = adapter_factory
        self._team_adapter_factory = team_adapter_factory
        self._source_reader = source_reader

    async def prepare_documents(self, worklog_ids: list[int]) -> PreparedLightWorklogDocuments:
        """업무일지 ID 목록을 LightRAG text/custom KG 문서와 missing ID 목록으로 나눈다."""
        reader = self._source_reader or WorklogLightSourceRowReader()
        source_rows_by_id = await reader.fetch_source_rows(worklog_ids)
        documents: list[LightRagWorklogDocument] = []
        custom_kg_documents: list[LightRagWorklogCustomKgDocument] = []
        found_worklog_ids: list[int] = []
        missing_worklog_ids: list[int] = []

        for worklog_id in worklog_ids:
            source_row = source_rows_by_id.get(worklog_id)
            if source_row is None:
                missing_worklog_ids.append(worklog_id)
                continue
            found_worklog_ids.append(worklog_id)
            documents.append(build_worklog_light_document(to_light_worklog_source(source_row)))
            custom_kg_documents.append(
                build_worklog_custom_kg_document(to_light_worklog_custom_kg_source(source_row))
            )

        return PreparedLightWorklogDocuments(
            documents=documents,
            custom_kg_documents=custom_kg_documents,
            found_worklog_ids=found_worklog_ids,
            missing_worklog_ids=missing_worklog_ids,
            source_rows=[source_rows_by_id[worklog_id] for worklog_id in found_worklog_ids],
        )

    async def index_worklogs(
        self, worklog_ids: list[int], *, already_mutated: bool = False,
    ) -> WorklogLightIndexResponse:
        """원본 조회부터 팀 사본 게시까지 동일 업무일지 mutation을 직렬화한다."""
        async with worklog_mutation_locks([f"worklog:{item}" for item in worklog_ids]):
            return await self._index_worklogs(worklog_ids, already_mutated=already_mutated)

    async def _index_worklogs(
        self,
        worklog_ids: list[int],
        *,
        already_mutated: bool = False,
        prepared: PreparedLightWorklogDocuments | None = None,
    ) -> WorklogLightIndexResponse:
        """요청된 업무일지 ID 순서대로 text/custom KG 통합 index 결과를 반환한다."""
        if not worklog_ids:
            if already_mutated and self._cache is not None:
                await self._cache.bump_version()
            return WorklogLightIndexResponse(items=[])

        prepared = prepared or await self.prepare_documents(worklog_ids)
        try:
            failed_found_ids = await self._index_found_documents(prepared)
        finally:
            # 요청 취소/설정 오류도 부분 쓰기를 남길 수 있어 반환 경로와 무관하게 무효화한다.
            if self._cache is not None and (prepared.documents or already_mutated):
                await self._cache.bump_version()

        missing_worklog_ids = set(prepared.missing_worklog_ids)
        indexed_worklog_ids = set(prepared.found_worklog_ids) - set(failed_found_ids)
        return WorklogLightIndexResponse(
            items=[
                self._build_response_item(
                    worklog_id=worklog_id,
                    missing_worklog_ids=missing_worklog_ids,
                    indexed_worklog_ids=indexed_worklog_ids,
                    failed_found_ids=failed_found_ids,
                )
                for worklog_id in worklog_ids
            ]
        )

    async def reindex_worklogs(self, worklog_ids: list[int]) -> WorklogLightIndexResponse:
        """삭제와 최신 원본 조회/색인을 하나의 업무일지 mutation으로 묶는다."""
        async with worklog_mutation_locks([f"worklog:{item}" for item in worklog_ids]):
            try:
                return await self._reindex_worklogs(worklog_ids)
            except (Exception, asyncio.CancelledError):
                if worklog_ids and self._cache is not None:
                    await self._cache.bump_version()
                raise

    async def _reindex_worklogs(self, worklog_ids: list[int]) -> WorklogLightIndexResponse:
        """생존 원본의 팀으로 부분 색인을 복구하고 삭제된 원본은 기록된 소유권으로 정리한다."""
        prepared = await self.prepare_documents(worklog_ids)
        adapter = (self._adapter_factory or get_lightrag_worklog_index_adapter)()
        delete_failed_items: dict[int, str] = {}

        for worklog_id in prepared.missing_worklog_ids:
            try:
                await self._delete_document(adapter, f"worklog-{worklog_id}")
            except LightRagConfigurationError:
                raise
            except LightRagDeleteTimeoutError:
                delete_failed_items[worklog_id] = ERROR_LIGHTRAG_DELETE_TIMEOUT
            except LightRagDeleteFailedError:
                delete_failed_items[worklog_id] = ERROR_LIGHTRAG_DELETE_FAILED

        indexed_response = await self._index_worklogs(
            worklog_ids, prepared=prepared,
            already_mutated=bool(prepared.missing_worklog_ids),
        )
        return WorklogLightIndexResponse(items=[
            WorklogLightIndexItem(worklogId=item.worklog_id, indexed=False,
                                 error=delete_failed_items[item.worklog_id])
            if item.worklog_id in delete_failed_items else item
            for item in indexed_response.items
        ])

    async def delete_worklogs(self, worklog_ids: list[int]) -> WorklogLightDeleteResponse:
        """동일 업무일지의 원본 조회/색인과 삭제를 직렬화한다."""
        async with worklog_mutation_locks([f"worklog:{item}" for item in worklog_ids]):
            try:
                return await self._delete_worklogs(worklog_ids)
            finally:
                if worklog_ids and self._cache is not None:
                    await self._cache.bump_version()

    async def _delete_worklogs(self, worklog_ids: list[int]) -> WorklogLightDeleteResponse:
        """요청된 업무일지 문서를 LightRAG에서 삭제하고 캐시 버전을 증가시킨다."""
        adapter = (self._adapter_factory or get_lightrag_worklog_index_adapter)()
        items: list[WorklogLightDeleteItem] = []

        for worklog_id in worklog_ids:
            try:
                await self._delete_document(adapter, f"worklog-{worklog_id}")
                items.append(WorklogLightDeleteItem(worklogId=worklog_id, deleted=True))
            except LightRagConfigurationError:
                raise
            except LightRagDeleteTimeoutError:
                items.append(
                    WorklogLightDeleteItem(
                        worklogId=worklog_id, deleted=False, error=ERROR_LIGHTRAG_DELETE_TIMEOUT
                    )
                )
            except LightRagDeleteFailedError:
                items.append(
                    WorklogLightDeleteItem(
                        worklogId=worklog_id, deleted=False, error=ERROR_LIGHTRAG_DELETE_FAILED
                    )
                )

        return WorklogLightDeleteResponse(items=items)

    async def _delete_document(self, adapter: Any, document_id: str) -> None:
        """팀 사본을 먼저 삭제하고 마지막에 전역 원본/소유권 레코드를 지운다."""
        if hasattr(adapter, "set_document_indexing_status"):
            try:
                await adapter.set_document_indexing_status(document_id, "STAGING")
            except LightRagInsertTimeoutError as exc:
                raise LightRagDeleteTimeoutError("LightRAG deletion gate close timed out") from exc
            except LightRagInsertFailedError as exc:
                raise LightRagDeleteFailedError("LightRAG deletion gate close failed") from exc
        team_id = await adapter.get_document_team_id(document_id)
        if team_id is not None:
            pool = (self._team_adapter_factory or get_lightrag_team_scope_adapter)()
            scoped = pool.adapter_for(team_id)
            await scoped.delete_document(document_id)
        await adapter.delete_document(document_id)

    async def _index_found_documents(
        self,
        prepared: PreparedLightWorklogDocuments,
    ) -> dict[int, str]:
        """문서별 잔존물을 지우고 모든 workspace 성공 후 전역 완료를 게시한다."""
        if not prepared.documents:
            return {}

        adapter = (self._adapter_factory or get_lightrag_worklog_index_adapter)()
        pool = (self._team_adapter_factory or get_lightrag_team_scope_adapter)()
        failures: dict[int, str] = {}
        for document, kg_document, row in zip(
            prepared.documents, prepared.custom_kg_documents, prepared.source_rows, strict=True,
        ):
            document_id = document.document_id
            compensations: list[tuple[str, Any]] = []
            completion_attempted = False
            try:
                previous_team = await adapter.get_document_team_id(document_id, allow_missing=True)
                try:
                    await adapter.set_document_indexing_status(document_id, "STAGING")
                except LightRagInsertTimeoutError as exc:
                    raise LightRagDeleteTimeoutError("LightRAG deletion gate close timed out") from exc
                except LightRagInsertFailedError as exc:
                    raise LightRagDeleteFailedError("LightRAG deletion gate close failed") from exc
                # native filter_keys가 부분 문서를 건너뛰지 않도록 재시도도 처음부터 시작한다.
                for team_id in dict.fromkeys([previous_team, row.team_id]):
                    if team_id is not None:
                        await pool.adapter_for(team_id).delete_document(document_id)
                await adapter.delete_document(document_id)
                # 본문/KG 공유 문서 삭제를 workspace당 첫 쓰기 전에 한 번 등록한다.
                compensations.append(("global", adapter))
                await adapter.index_documents([document])
                await adapter.index_custom_kg_documents([kg_document])
                await adapter.save_indexed_team_ids({row.worklog_id: row.team_id})
                compensations.append((f"team:{row.team_id}", pool.adapter_for(row.team_id)))
                await pool.index_source_rows([row])
                completion_attempted = True
                await adapter.set_document_indexing_status(document_id, "COMPLETED")
            except (Exception, asyncio.CancelledError) as exc:
                recovered = await self._await_compensation(
                    adapter, document_id, compensations, completion_attempted=completion_attempted,
                )
                if isinstance(exc, (LightRagConfigurationError, asyncio.CancelledError)):
                    raise
                if not recovered:
                    failures[row.worklog_id] = ERROR_LIGHTRAG_COMPENSATION_FAILED
                elif isinstance(exc, LightRagInsertTimeoutError):
                    failures[row.worklog_id] = ERROR_LIGHTRAG_INSERT_TIMEOUT
                elif isinstance(exc, LightRagDeleteTimeoutError):
                    failures[row.worklog_id] = ERROR_LIGHTRAG_DELETE_TIMEOUT
                elif isinstance(exc, LightRagDeleteFailedError):
                    failures[row.worklog_id] = ERROR_LIGHTRAG_DELETE_FAILED
                else:
                    logger.exception("LightRAG document indexing failed: %s", document_id)
                    failures[row.worklog_id] = ERROR_LIGHTRAG_INSERT_FAILED
        return failures

    async def _await_compensation(
        self, adapter: Any, document_id: str, compensations: list[tuple[str, Any]],
        *, completion_attempted: bool,
    ) -> bool:
        """취소와 분리한 보상 작업의 종료까지 잠금을 유지하되 시간 제한을 둔다."""
        task = asyncio.create_task(asyncio.wait_for(
            self._compensate_document(
                adapter, document_id, compensations, completion_attempted=completion_attempted,
            ),
            timeout=self._settings.lightrag_insert_timeout_seconds,
        ))
        cancelled = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
            except TimeoutError:
                break
        try:
            recovered = task.result()
        except TimeoutError:
            logger.error("LightRAG document compensation timed out: document=%s workspaces=%s",
                         document_id, [name for name, _ in compensations])
            recovered = False
        if cancelled:
            raise asyncio.CancelledError
        return recovered

    async def _compensate_document(
        self, adapter: Any, document_id: str, compensations: list[tuple[str, Any]],
        *, completion_attempted: bool,
    ) -> bool:
        """확보한 workspace·문서 ID로 팀→전역 정리하며 실패 시 소유권을 보존한다."""
        if completion_attempted:
            try:
                await adapter.set_document_indexing_status(document_id, "STAGING")
            except Exception:
                # 완료 응답만 유실됐을 수 있어 게이트를 닫기 전에는 삭제하지 않는다.
                logger.exception("LightRAG compensation gate close failed: document=%s workspace=global",
                                 document_id)
                return False
        for workspace_name, workspace in reversed(compensations):
            try:
                await workspace.delete_document(document_id)
            except Exception:
                # 팀 삭제 실패 시 전역 소유권을 지우면 다음 재시도에서 과거 팀을 잃는다.
                logger.exception("LightRAG document compensation failed: document=%s workspace=%s",
                                 document_id, workspace_name)
                return False
        return True

    def _build_response_item(
        self,
        *,
        worklog_id: int,
        missing_worklog_ids: set[int],
        indexed_worklog_ids: set[int],
        failed_found_ids: dict[int, str],
    ) -> WorklogLightIndexItem:
        """업무일지 ID 하나의 통합 index 응답 item을 만든다."""
        if worklog_id in missing_worklog_ids:
            return WorklogLightIndexItem(
                worklogId=worklog_id,
                indexed=False,
                error=ERROR_WORKLOG_NOT_FOUND,
            )
        if worklog_id in indexed_worklog_ids:
            return WorklogLightIndexItem(worklogId=worklog_id, indexed=True)
        return WorklogLightIndexItem(
            worklogId=worklog_id,
            indexed=False,
            error=failed_found_ids[worklog_id],
        )
