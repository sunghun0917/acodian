# LightRAG PostgreSQL Storage Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** JSON 기반 LightRAG KV·문서 상태를 전용 PostgreSQL DB로 바꾸고 snapshot 없이 최신 업무 원본을 조회한다.

**Architecture:** 기존 LightRAG adapter에 기본 `PGKVStorage`·`PGDocStatusStorage`를 지정한다. 팀 pool의 source 조회 인터페이스는 유지하되 기존 업무 DB reader로 구현하고, 전역 문서 상태의 metadata에 색인 당시 팀 ID를 보존한다. 전역·팀 색인, Neo4j·Qdrant, mutation 락은 유지한다.

**Tech Stack:** FastAPI, Python 3.12, lightrag-hku==1.4.10, PostgreSQL, 기존 asyncpg==0.31.0·SQLAlchemy, Neo4j, Qdrant, pytest.

**Spec:** [승인된 설계 명세](../specs/2026-10-21-lightrag-postgres-storage-design.md)

## Global Constraints

- 같은 PostgreSQL 서버 안의 별도 LightRAG DB 사용.
- 업무 DB 조회는 기존 `app/store/session.py`의 session factory와 업무 DB 연결 설정을 유지한다.
- KV와 문서 상태는 기본 PG 구현을 사용하고 새 커스텀 저장소·업무 테이블을 추가하지 않는다.
- 기존 색인은 이관하지 않고 새 base workspace `axwms_pg_v1`에 전체 재색인한다.
- JSON 데이터 변환·이관 도구, 구버전 snapshot 호환 코드와 무중단 전환은 제외한다.
- Neo4j·Qdrant, 전역·팀 이중 색인과 기존 업무일지/workspace 락을 유지한다.
- API·웹 계약과 보존된 `v3` 코드는 변경하지 않는다.
- 실제 비밀값은 문서·테스트·로그에 넣지 않는다. 실제 `.env`는 수정하지 않는다.
- 공유 DB·volume·graph·collection 전체 초기화와 실제 기존 색인 삭제는 실행하지 않는다.
- 사용자의 명시적 커밋 요청이 없으므로 이 계획에는 커밋 단계를 넣지 않는다.

## Review Focus

1. 초기화 중 native PG가 workspace를 덮어써도 adapter가 공개되지 않아야 한다 → Task 1의 초기화 후 mismatch·cleanup 테스트.
2. 팀 색인 실패·팀 이동·원본 영구 삭제 뒤에도 옛 팀 소유권이 남아야 한다 → Task 2의 기록 순서·실패·재시도 테스트.
3. 검색 후보와 predecessor의 팀·삭제 상태가 바뀌어도 현재 접근 불가한 내용은 prompt에 없어야 한다 → Task 3의 일반/SSE 권한 회귀 테스트.
4. PG의 필드 매핑·재연결 때문에 confirmed 캐시와 공유 노드 기여분이 사라지면 안 된다 → Task 4의 실제 PG 왕복·삭제 재구성 테스트.
5. DB 이름 설정만 추가하고 전용 DB 준비·worker 설정을 빠뜨리면 안 된다 → Task 5의 provisioning runbook·compose 설정 검증.

## 파일 책임과 인터페이스

| 파일 | 책임 |
|---|---|
| `ai/app/config/settings.py`, `ai/.env.example` | 업무 DB와 분리된 `LIGHTRAG_PG_DSN` 및 새 workspace 기본값 |
| `ai/app/light/v4/service/lightrag_adapter.py` | PG 설정 전달·lifecycle, 팀 소유권 저장/회수, processed 문서 ID 조회 |
| `ai/app/light/v4/service/worklog_index_service.py` | 팀 쓰기 전 소유권 기록, 기존 삭제·재색인 순서 유지 |
| `ai/app/light/v4/service/worklog_team_scope.py` | marker·snapshot 제거, 기존 source reader + 팀별 상태 필터 |
| `ai/app/light/v4/service/worklog_query_service.py` | 기존 normal/SSE 문맥 계약 보존 및 snapshot 설명 정리 |
| 관련 `ai/tests/test_light_worklog_*.py`, `test_lightrag_v4_adapter.py` | 기존 fake·snapshot 계약을 새 source·status 계약으로 교체 |
| `ai/tests/test_light_worklog_pg_storage_integration.py` (신규) | 명시적으로 격리된 실제 PG 기본 storage 계약 |
| `infra/compose.deploy.yml`, `infra/.env.example` | AI·worker에 같은 전용 DB DSN·workspace 전달 |
| `docs/ai/lightrag-postgres-setup.md` (신규), `docs/ai/rag-team-prefilter.md`, `ai/AGENTS.md` | DB 준비·전환 절차, 변경된 조회 계약, 파일 소유권 기록 |

기존 `worklog_source_store.py`·`worklog_source_row_reader.py`·Custom KG 생성/검증 로직은 재사용한다. 동작 변경이 필요 없는 파일은 수정하지 않는다.

### 고정 인터페이스

```python
# Settings에 기존 키 다음으로 추가. 비밀값 repr 노출 금지.
lightrag_pg_dsn: str = Field(default="", repr=False)
# 기존 lightrag_workspace의 기본값은 "axwms_pg_v1"로 변경.

# LightRagWorklogIndexAdapter
async def save_indexed_team_ids(self, teams_by_worklog_id: dict[int, int]) -> None: ...
async def get_document_team_id(self, document_id: str) -> int | None: ...
async def get_processed_worklog_ids(self, worklog_ids: list[int]) -> set[int]: ...

# 기존 팀 pool source API는 변경하지 않음.
async def read_source_rows(
    self, worklog_ids: list[int], allowed_team_ids: tuple[int, ...],
) -> dict[int, LightWorklogSourceRow]: ...

# LightRagTeamScopeAdapter 생성자에 테스트/실제 reader 주입 경계 추가.
source_reader: WorklogLightSourceRowReader | None = None
```

`WorklogLightIndexAdapter` Protocol에는 실제 index service가 호출하는 소유권 메서드를 함께 선언한다. 새 base class나 저장소 wrapper를 만들지 않는다.

## Task 1: 기본 PG 설정과 안전한 lifecycle

**Files:** Modify `ai/app/config/settings.py`, `ai/.env.example`, `ai/app/light/v4/service/lightrag_adapter.py`, `ai/tests/test_lightrag_v4_adapter.py`.

**Interfaces:** Consumes 기존 `Settings`, `LightRagDependencies`, `_SCOPE_STORAGE_NAMES`. Produces `_configure_postgres_environment(settings_obj: Settings) -> None`, 기본 PG 생성 인자와 초기화 후 workspace 검증.

- [x] **Step 1: 실패 테스트 추가.** `test_pg_settings_keep_source_database_separate`, `test_pg_adapter_uses_default_storage_classes`, `test_pg_override_rejected_before_initialization`, `test_pg_workspace_mismatch_after_initialization_cleans_up`, `test_pg_invalid_dsn_does_not_expose_credentials`를 작성한다. 핵심 assertion:

```python
assert settings.pgvector_dsn == source_dsn
assert rag.kwargs["kv_storage"] == "PGKVStorage"
assert rag.kwargs["doc_status_storage"] == "PGDocStatusStorage"
assert os.environ["POSTGRES_DATABASE"] == "axwms_lightrag"
assert os.environ["POSTGRES_ENABLE_VECTOR"] == "false"
assert password not in str(error.value)
assert adapter._rag is None and not adapter._initialized
```

- [x] **Step 2: RED 확인.** `cd ai; ./.venv/Scripts/python.exe -B -m pytest -p no:cacheprovider tests/test_lightrag_v4_adapter.py -q -k 'pg_'`. 신규 설정/생성 인자·검증 부재에 의한 실패를 확인한다.
- [x] **Step 3: 최소 구현.** `lightrag_pg_dsn`을 추가하고 기존 SQLAlchemy `make_url`로 분해해 native `POSTGRES_HOST/PORT/USER/PASSWORD/DATABASE`에 전달한다. 빈 DSN·비 PostgreSQL URL·필수 접속 요소 누락·업무 DB와 같은 DB 이름은 설정 오류로 거절한다. 실패 메시지에 URL·비밀값을 넣지 않는다. 기존 업무 DSN은 변경하지 않는다.
- [x] **Step 4: workspace 검증.** 환경 및 CWD `config.ini`의 PG workspace override를 거절한다. 팀뿐 아니라 새 전역 workspace도 생성 전/초기화 후 검증한다. 후검증 예외도 기존 초기화 실패 cleanup 안에서 처리한다. `_initialized`를 먼저 설정하지 않는다. 기존 Qdrant·Neo4j 검증과 캐시 validation wrapper를 유지한다.
- [x] **Step 5: GREEN 확인.** Step 2 명령을 재실행한다. 기존 테스트의 `make_settings()`에는 테스트 전용 DSN을 명시하고, env monkeypatch로 native 설정이 다른 테스트에 새지 않게 한다. 새 driver 의존성은 추가하지 않는다.

## Task 2: snapshot 대신 최소 팀 소유권과 색인 순서

**Files:** Modify `lightrag_adapter.py`, `worklog_index_service.py`, `ai/tests/test_lightrag_v4_adapter.py`, `ai/tests/test_light_worklog_v4_index_service.py`, 필요 시 `ai/tests/test_light_worklog_custom_kg_tracking.py`.

**Interfaces:** Produces 위 `save_indexed_team_ids`, 기존 시그니처의 `get_document_team_id`. Consumes native `doc_status.get_by_id/get_by_ids/upsert`와 기존 workspace 락.

- [x] **Step 1: 실패 테스트 작성.** `test_indexed_team_metadata_preserves_native_fields`, `test_indexed_team_metadata_rejects_invalid_ids`, `test_team_writes_wait_for_indexed_ownership`, `test_team_delete_failure_preserves_indexed_ownership`, `test_delete_uses_indexed_team_after_source_move_or_hard_delete`를 추가/수정한다.

```python
assert stored["metadata"] == {**native_metadata, "indexed_team_id": 10}
assert stored["chunks_list"] == old_chunks
assert calls.index("save_indexed_team_ids") < calls.index("team_index")
assert deleted_teams == [10]  # 업무 DB의 현재 팀은 20이거나 원본이 없음
assert await adapter.get_document_team_id("worklog-1") == 10
```

- [x] **Step 2: RED 확인.** `cd ai; ./.venv/Scripts/python.exe -B -m pytest -p no:cacheprovider tests/test_lightrag_v4_adapter.py tests/test_light_worklog_v4_index_service.py -q -k 'indexed_team or ownership or source_move'`.
- [x] **Step 3: 소유권 구현.** 기존 상태·metadata를 복사해 `indexed_team_id`만 병합한다. 양의 정수 ID와 processed 상태를 검증하고 기존 workspace mutation 경계에서 기록한다. 문서 상태가 없으면 team ID 회수는 `None`; 상태가 존재하는데 metadata가 누락/손상이면 `LightRagDeleteFailedError`다. 상태·청크·시간 정보를 팀 ID만 있는 레코드로 덮어쓰지 않는다.
- [x] **Step 4: 호출부 전환.** `_index_found_documents()`의 전역 Custom KG 성공 후 `save_indexed_team_ids({row.worklog_id: row.team_id ...})`를 호출한 뒤 팀 색인을 시작한다. `save_source_rows()`와 JSON snapshot TypeAdapter 등 불용 코드를 제거한다. 팀 삭제→전역 삭제 순서와 기존 mutation/cache 처리·실패 응답은 유지한다.
- [x] **Step 5: GREEN 확인.** 위 두 파일과 `tests/test_light_worklog_custom_kg_tracking.py`, `tests/test_light_worklog_extraction_cache.py`를 실행한다. JSON snapshot roundtrip 기대값을 metadata 보존 테스트로 교체하고, native Custom KG 상태 갱신 후에도 metadata가 남는지 검증한다.

## Task 3: 최신 source 조회·팀별 상태 검증·marker 제거

**Files:** Modify `lightrag_adapter.py`, `worklog_team_scope.py`, `worklog_query_service.py`, `ai/tests/test_lightrag_v4_adapter.py`, `ai/tests/test_light_worklog_team_scope.py`, `ai/tests/test_light_worklog_v4_query_service.py`.

**Interfaces:** Consumes Task 1 lifecycle, 기존 `WorklogLightSourceRowReader.fetch_source_rows(ids)`. Produces `get_processed_worklog_ids()` 및 기존 팀 pool의 `read_source_rows(ids, allowed_team_ids)` 계약. 정상/SSE query service 호출부의 인자 계약은 유지한다.

- [x] **Step 1: 실패 테스트 작성.** `test_source_lookup_uses_fresh_rows_and_matching_team_status`, `test_source_lookup_excludes_missing_and_unprocessed`, `test_source_lookup_rejects_malformed_status_batch`, `test_source_failure_does_not_fallback_to_graph`, `test_moved_or_deleted_predecessor_is_absent_from_normal_and_sse_prompt`를 추가한다.

```python
assert result[1].work_content == "LATEST_DB_BODY"
assert 2 not in result  # 현재 팀이 허용 범위 밖이거나 현재 팀 workspace에 processed 상태 없음
assert "OLD_SNAPSHOT_BODY" not in prompt
assert "FORBIDDEN_PREDECESSOR_TITLE" not in prompt
assert not global_query_calls
```

- [x] **Step 2: RED 확인.** `cd ai; ./.venv/Scripts/python.exe -B -m pytest -p no:cacheprovider tests/test_light_worklog_team_scope.py tests/test_light_worklog_v4_query_service.py -q`.
- [x] **Step 3: adapter 상태 조회 구현.** ID·batch 길이·레코드 형식을 검증해 processed 문서 집합을 반환한다. 없거나 정상적인 미완료 상태는 제외하고, 잘못된 구조는 `LightRagQueryFailedError`로 처리한다. 전체 snapshot 조회, marker 저장/검사 메서드를 제거한다.
- [x] **Step 4: pool 구현 전환.** 허용 팀별 processed ID 집합을 구한 뒤 그 합집합만 기존 source reader로 한 번 조회한다. 현재 `row.team_id`에 해당하는 팀의 processed 집합에도 ID가 있는 경우만 반환한다. 빈 ID/빈 scope에는 DB·전역 검색을 호출하지 않는다. reader 예외는 query 실패로 변환한다.
- [x] **Step 5: marker·snapshot 호출 제거.** 팀 색인에서는 본문→팀 내 Custom KG까지만 수행한다. cross-team KG 필터는 유지한다. 기존 `read_source_rows` 시그니처·predecessor 재조회·후보 순서·참조 형식·문맥 제한·일반/SSE 경로를 유지하며 관련 설명만 갱신한다.
- [x] **Step 6: GREEN 확인.** Step 2 및 `tests/test_light_worklog_v4_query_contract.py`를 실행한다. 각 테스트에 source reader fake를 주입해 실제 업무 DB로 연결되지 않게 하고, 기존 scope cache 동작을 확대하지 않는다. 최신성 검증은 scope가 있는 일반/SSE 경로에서 수행한다.

## Task 4: 기본 PG 왕복 및 기존 실제 저장소 통합 테스트

**Files:** Create `ai/tests/test_light_worklog_pg_storage_integration.py`; Modify `ai/tests/test_light_worklog_team_storage_integration.py`.

**Interfaces:** Consumes Tasks 1–3, native PG KV/status 클래스와 기존 test-only LLM/embedding injection. Opt-in 환경변수는 `RAG_SCOPE_TEST_ID`, `RAG_SCOPE_TEST_PG_DSN`, 기존 격리 Qdrant/Neo4j endpoint다.

- [x] **Step 1: 테스트 가드 작성.** 기본 실행은 skip이다. PG DSN은 명시한 loopback host·비기본 포트·`test_`로 시작하는 전용 DB 이름만 허용한다. `axwms_lightrag`, 업무 DB, 외부 host는 거절한다. workspace는 테스트 ID+UUID로 생성하고 cleanup은 이 workspace의 생성 데이터에만 한정한다. database/table 전체 drop은 금지한다.
- [x] **Step 2: 실패 계약 작성.** `test_pg_kv_and_status_roundtrip_across_reinitialize`, `test_pg_workspaces_keep_same_document_id_separate`, `test_pg_confirmed_cache_and_provenance_survive_shared_node_delete`를 작성한다. cache `return/chunk_id/cache_type`, chunk `full_doc_id/llm_cache_list`, status `metadata.indexed_team_id`를 왕복·재초기화 후 assert한다.
- [x] **Step 3: 기존 통합 테스트 변경.** JSON snapshot assert와 `Engine.connect` 금지 monkeypatch를 없앤다. 실제 PG KV + Qdrant/Neo4j로 색인·검색·삭제하고, 주입된 source reader의 원본을 바꿔 일반/SSE prompt가 새 본문을 쓰며 현재 권한 필터를 유지하는지 확인한다. source reader fake를 사용한 테스트를 실제 업무 SQL 검증으로 보고하지 않는다.
- [x] **Step 4: 기본 테스트 수집 검증.** `cd ai; ./.venv/Scripts/python.exe -B -m pytest -p no:cacheprovider tests/test_light_worklog_pg_storage_integration.py tests/test_light_worklog_team_storage_integration.py -q`. 명시적 opt-in이 없으면 네트워크 호출 없이 SKIPPED여야 한다.
- [ ] **Step 5: 격리 서비스가 준비된 경우에만 실제 실행.** 위 환경변수를 테스트 대상으로 설정하고 Step 4를 재실행한다. 기본 storage 재연결·팀 이동/삭제·공유 노드 보존을 확인한다. 서비스가 없으면 skip 사유와 실제 PG 검증 공백을 결과에 남기고 가짜 성공으로 대체하지 않는다.

## Task 5: 전용 DB 준비·설정 공급·전체 회귀

**Files:** Modify `infra/compose.deploy.yml`, `infra/.env.example`, `docs/ai/rag-team-prefilter.md`, `ai/AGENTS.md`; Create `docs/ai/lightrag-postgres-setup.md`. `infra/compose.local.yml`의 PostgreSQL 서버·volume 구성은 변경하지 않는다.

**Interfaces:** Consumes `LIGHTRAG_PG_DSN`, `LIGHTRAG_WORKSPACE=axwms_pg_v1`. 업무 DB `PGVECTOR_DSN`·기존 서비스 포트·healthcheck·배포 정책은 유지한다.

- [x] **Step 1: 인프라 규칙과 현재 설정 확인.** `infra/AGENTS.md`, `docs/infra/adr.yaml`, `docs/infra/code-convention.yaml`을 읽는다. ai/worker에 같은 전용 DSN·workspace를 전달할 위치와 기존 `PGVECTOR_DSN`을 확인한다.
- [x] **Step 2: 최소 설정 추가.** deploy ai/worker에 같은 `LIGHTRAG_PG_DSN`·`LIGHTRAG_WORKSPACE`를 전달하고 infra 예시에 비밀이 아닌 smoke 값/설명을 추가한다. local은 기존 PostgreSQL을 그대로 쓰고 AI의 `.env.example`에서 전용 DB DSN을 설정하도록 한다. 새 서버·상시 init 서비스·volume은 추가하지 않는다.
- [x] **Step 3: provisioning·재색인 runbook 작성.** 기존 서버에 전용 LOGIN role과 `axwms_lightrag` DB를 준비하는 관리자 절차를 명시한다. password는 `psql`의 비표시 입력으로 설정하고 문서/명령 인자에 실값을 남기지 않는다. DB/role 존재 여부·owner를 확인하고 기존 객체를 임의 변경하지 않는다. Postgres 기존 volume에 환경변수만 추가해도 DB가 생성되는 것으로 설명하지 않는다.
- [x] **Step 4: 문서와 책임 갱신.** 새 workspace에서 PG·Neo4j·Qdrant를 함께 재구축하고, 확인된 전체 업무 ID를 기존 index API로 전달하는 절차와 오류·재시도를 작성한다. cache는 기존 버전 증가 기능을 사용하고 전역 삭제하지 않는다. `rag-team-prefilter.md`의 snapshot/marker 설명을 현재 source·status 계약으로 갱신하고 `ai/AGENTS.md`에 PG 전환 책임을 기록한다.
- [x] **Step 5: 전체 회귀 실행.** `cd ai; ./.venv/Scripts/python.exe -B -m pytest -p no:cacheprovider tests -q`. 변경 Python 파일은 bytecode를 쓰지 않는 `ast.parse`로 문법 검증하고, `git diff --check`를 실행한다. 설정된 별도 lint/typecheck가 없으면 그 공백을 명시하고 도구를 새로 설치하지 않는다.
- [x] **Step 6: compose 정적 검증.** Docker가 있으면 `docker compose --env-file infra/.env.example -f infra/compose.deploy.yml config --quiet`를 smoke용 image/APP_ENV/port 변수와 함께 실행한다. 서비스는 기동하지 않는다. 도구가 없으면 YAML 구조·양쪽 환경값을 검사하고 미실행 사유를 보고한다.
- [x] **Step 7: 독립 리뷰 및 결과 보고.** 실행 방식이 Native이면 구현 후 새 reviewer로 전체 diff를 검토한다. Spec 불일치·권한·데이터 유실·실패 복구 문제를 먼저 수정하고 해당 테스트를 재실행한다. 변경 파일, 통과/skip 테스트, 실제 PG 검증 여부, 미실행 DB 준비·재색인·기존 동시성 한계를 구분해 보고한다. 커밋하지 않는다.

## 자체 검토와 실행 대기

- Spec 1–4: Task 1·2·5에서 저장소/DB/설정과 데이터별 처리를 다룬다.
- Spec 5: Task 2에서 metadata와 삭제 순서·실패 보존을 다룬다.
- Spec 6: Task 3에서 현재 source·권한·선행 문서·normal/SSE를 다룬다.
- Spec 7: Task 5의 runbook, Task 4의 테스트 격리로 재구축 경계를 다룬다.
- Spec 8–10: 전체 파일 책임·회귀/통합 테스트·명시적 미검증 보고를 포함한다.
- 인터페이스 이름은 위 고정 인터페이스와 통일하고 snapshot 대신 새 저장소 abstraction을 추가하지 않는다.
- 실제 DB 생성·전체 재색인은 코드 구현과 별개이며 대상·자격증명·환경을 확인하지 않고 실행하지 않는다.

**상태:** 사용자 승인에 따라 직접 구현하고 최종 독립 리뷰 지적을 회귀 테스트로 보완했다. 실제 외부 저장소 통합 검증은 Docker 엔진 미실행으로 대기한다. DB 생성·기존 데이터 삭제·전체 재색인·커밋은 수행하지 않았다.

**구현 시 보완:** native PG의 전부 미존재 `[]` 응답과 정상 `preprocessed` 상태를 제외 후보로 처리했다. 1.4.10의 PG 삭제 예외 은폐는 같은 native SQL/client를 쓰는 인스턴스별 삭제 훅과 삭제 후 조회로 보완했다. 새 전역 Qdrant 초기화도 기존 비이관 함수를 재사용한다. 새 PG 전환 설정으로 인해 보존 v3 테스트의 legacy namespace와 Langfuse worker 테스트의 전용 DSN은 fixture에서 명시했다. 제품 v3 코드는 변경하지 않았다.
