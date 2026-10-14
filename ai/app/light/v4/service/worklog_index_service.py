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
    ) -> None:
        """인덱싱/재색인/삭제 후 캐시 무효화에 사용할 Redis 캐시를 구성한다."""
        self._cache = cache or WorklogQueryCache(settings_obj=settings_obj)

    async def prepare_documents(self, worklog_ids: list[int]) -> PreparedLightWorklogDocuments:
        """업무일지 ID 목록을 LightRAG text/custom KG 문서와 missing ID 목록으로 나눈다."""
        source_rows_by_id = await WorklogLightSourceRowReader().fetch_source_rows(worklog_ids)
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
    ) -> WorklogLightIndexResponse:
        """요청된 업무일지 ID 순서대로 text/custom KG 통합 index 결과를 반환한다."""
        if not worklog_ids:
            if already_mutated:
                await self._cache.bump_version()
            return WorklogLightIndexResponse(items=[])

        prepared = await self.prepare_documents(worklog_ids)
        failed_found_ids, mutation_attempted = await self._index_found_documents(prepared)

        missing_worklog_ids = set(prepared.missing_worklog_ids)
        indexed_worklog_ids = set(prepared.found_worklog_ids) - set(failed_found_ids)
        if indexed_worklog_ids or mutation_attempted or already_mutated:
            await self._cache.bump_version()

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
            return await self._reindex_worklogs(worklog_ids)

    async def _reindex_worklogs(self, worklog_ids: list[int]) -> WorklogLightIndexResponse:
        """기존 인덱스를 단일 문서 단위로 삭제 후 최신 DB 내용으로 재색인한다.

        삭제 실패한 문서는 재색인을 진행하지 않고 즉시 실패 처리하며,
        일부라도 삭제되거나 텍스트가 삽입된 경우 캐시 버전을 증가시킨다.
        """
        adapter = get_lightrag_worklog_index_adapter()
        delete_failed_items: dict[int, str] = {}
        deleted_worklog_ids: list[int] = []

        for worklog_id in worklog_ids:
            try:
                await self._delete_document(adapter, f"worklog-{worklog_id}")
                deleted_worklog_ids.append(worklog_id)
            except LightRagConfigurationError:
                raise
            except LightRagDeleteTimeoutError:
                delete_failed_items[worklog_id] = ERROR_LIGHTRAG_DELETE_TIMEOUT
            except LightRagDeleteFailedError:
                delete_failed_items[worklog_id] = ERROR_LIGHTRAG_DELETE_FAILED

        has_deleted = bool(deleted_worklog_ids)

        indexed_response = await self._index_worklogs(
            deleted_worklog_ids,
            already_mutated=has_deleted,
        )
        indexed_item_by_id = {item.worklog_id: item for item in indexed_response.items}

        items: list[WorklogLightIndexItem] = []
        for worklog_id in worklog_ids:
            if worklog_id in delete_failed_items:
                items.append(
                    WorklogLightIndexItem(
                        worklogId=worklog_id,
                        indexed=False,
                        error=delete_failed_items[worklog_id],
                    )
                )
            elif worklog_id in indexed_item_by_id:
                items.append(indexed_item_by_id[worklog_id])

        return WorklogLightIndexResponse(items=items)

    async def delete_worklogs(self, worklog_ids: list[int]) -> WorklogLightDeleteResponse:
        """동일 업무일지의 원본 조회/색인과 삭제를 직렬화한다."""
        async with worklog_mutation_locks([f"worklog:{item}" for item in worklog_ids]):
            return await self._delete_worklogs(worklog_ids)

    async def _delete_worklogs(self, worklog_ids: list[int]) -> WorklogLightDeleteResponse:
        """요청된 업무일지 문서를 LightRAG에서 삭제하고 캐시 버전을 증가시킨다."""
        adapter = get_lightrag_worklog_index_adapter()
        items: list[WorklogLightDeleteItem] = []
        any_success = False

        for worklog_id in worklog_ids:
            try:
                await self._delete_document(adapter, f"worklog-{worklog_id}")
                items.append(WorklogLightDeleteItem(worklogId=worklog_id, deleted=True))
                any_success = True
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

        if any_success:
            await self._cache.bump_version()

        return WorklogLightDeleteResponse(items=items)

    async def _delete_document(self, adapter: Any, document_id: str) -> None:
        """팀 사본을 먼저 삭제하고 마지막에 전역 원본/소유권 레코드를 지운다."""
        team_id = await adapter.get_document_team_id(document_id)
        if team_id is not None:
            scoped = get_lightrag_team_scope_adapter().adapter_for(team_id)
            await scoped.delete_document(document_id)
        await adapter.delete_document(document_id)

    async def _index_found_documents(
        self,
        prepared: PreparedLightWorklogDocuments,
    ) -> tuple[dict[int, str], bool]:
        """조회된 업무일지만 adapter에 전달하고 text/custom KG 실패를 ID별 오류로 변환한다.

        반환값: (failed_found_ids, mutation_attempted)
        native insert는 실패/timeout 응답 전 부분 쓰기가 가능하므로 시도만으로 캐시를 무효화한다.
        """
        if not prepared.documents:
            return {}, False

        adapter = get_lightrag_worklog_index_adapter()
        try:
            await adapter.index_documents(prepared.documents)
        except LightRagConfigurationError:
            raise
        except LightRagInsertTimeoutError:
            return {
                worklog_id: ERROR_LIGHTRAG_INSERT_TIMEOUT
                for worklog_id in prepared.found_worklog_ids
            }, True
        except LightRagInsertFailedError:
            return {
                worklog_id: ERROR_LIGHTRAG_INSERT_FAILED
                for worklog_id in prepared.found_worklog_ids
            }, True

        try:
            await adapter.index_custom_kg_documents(prepared.custom_kg_documents)
            # 부분 실패 후에도 색인 당시 팀을 찾아 재시도/삭제할 수 있게 먼저 저장한다.
            await adapter.save_source_rows(prepared.source_rows)
            await get_lightrag_team_scope_adapter().index_source_rows(prepared.source_rows)
        except LightRagConfigurationError:
            raise
        except LightRagInsertTimeoutError:
            return {
                worklog_id: ERROR_LIGHTRAG_INSERT_TIMEOUT
                for worklog_id in prepared.found_worklog_ids
            }, True
        except LightRagInsertFailedError:
            return {
                worklog_id: ERROR_LIGHTRAG_INSERT_FAILED
                for worklog_id in prepared.found_worklog_ids
            }, True
        return {}, True

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
