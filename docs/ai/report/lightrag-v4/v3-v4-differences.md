# LightRAG 업무일지 v3 → v4 변경 기록

- 작성: 2026-11-10
- 목적: `ai/app/light/v3/`를 삭제하기 전에 v3와 v4의 차이를 남긴다. v3 원본 코드는 git 이력에서 확인할 수 있다(마지막 보존 시점 `f914fd18`, v4 분기 커밋 `3999ebb5 feat(ai): LightRAG v3 보존 및 v4 팀 검색 적용`).
- 상태: v3는 v4 도입 이후 서버 라우터에 등록되지 않은 보존 코드였다(`tests/test_light_worklog_versions.py`가 `/light/worklogs-v3/*` 미등록을 검증). 앱 코드 중 v3를 import하는 곳은 없었고, v3 전용 테스트만 v3를 참조했다.

## 한눈에 보기

| 구분 | v3 | v4 |
|---|---|---|
| API 경로 | `/ai/light/worklogs-v3/{index,delete,query,...}` | `/ai/light/worklogs-v4/{index,reindex,delete,query,...}` |
| 색인 실행 방식 | 요청을 받은 HTTP 처리 안에서 **동기 색인** 후 결과(`indexed`, `error`) 반환 | 요청은 **HTTP 202로 접수**만 하고 Celery 색인 큐(`worklog_index`)에 발행. 실제 결과는 worker가 API 콜백으로 통보 |
| 요청 식별 | 업무일지 ID 목록만 받음 | `requestId`(UUID) 필수. 응답에 `requestId`·`taskId` 포함. API start 승인·최신 요청 검증의 기준 |
| 인증 | 없음 | 색인·재색인·삭제 변경 경로는 `x-ai-internal-token` 내부 토큰 필수(미설정 503, 불일치 403) |
| 검색 범위 | 단일 전역 workspace | 전역 workspace + **팀별 workspace 격리**(`worklog_team_scope.py`). 질의는 허용 팀 범위에서만 수행 |
| 질의 응답 캐시 | Redis 질의 캐시 + 임베딩 기반 재사용 | 응답 캐시 **비활성화**(DB 최신성 검증을 지원할 때까지). 검색 범위·최신 DB 문맥 검증 뒤에만 답변 생성 |
| 저장소 | LightRAG 기본 저장소 구성 | PostgreSQL(LightRAG 전용 DB, 업무 DB와 분리) KV/상태 + Qdrant vector + Neo4j graph. 초기화 전에 세 저장소 workspace 일치 검증 |
| 동시 변경 보호 | 없음 | 업무일지 단위 mutation 잠금(`worklog_mutation_lock.py`) |
| 실패 보상 | 재색인 삭제 실패 방어 수준 | 색인 중 실패 시 팀→전역 순서 보상 삭제, 완료 게이트(STAGING/COMPLETED), 보상 실패를 별도 오류 코드로 분리 |
| 관측 | 없음 | Langfuse 요청 단위 추적(`query-worklogs`, `stream-worklogs-query`) |
| 답변 모델 | 색인·답변 같은 LLM 설정 | 답변 전용 모델 분리(`LIGHTRAG_ANSWER_LLM_MODEL`, 비어 있으면 기존 설정) |

## 질의 시 후보 메타데이터 DB 재조회

LightRAG(KG·vector)는 후보 업무일지 ID만 제공하고, LLM 문맥은 업무 DB를 다시 조회해 만든다. 이 원칙은 v3(`467d68d0 feat(ai): 권한 검증된 직접 의존관계 문맥 추가`)부터 있었고 v4가 이어받아 검증을 강화했다.

공통 흐름(v3·v4):
1. LightRAG 검색 결과에서 후보 worklog ID 추출(`_candidate_worklog_ids`).
2. 업무 DB에서 후보 원본 재조회 — 제목·요청/업무 내용·작성자·팀·태그·직접 선행 업무. `is_deleted=false` 업무와 `deleted_at IS NULL` 팀만(`worklog_source_store.py`).
3. 허용 팀(`allowed_team_ids`)에 속한 원본만 본문 문맥으로 사용.
4. 직접 선행 업무를 한 번 더 DB 조회해 접근 가능한 것만 `DEPENDS_ON` 관계로 포함(개수·문자 수 상한 초과 시 실패).
5. 프롬프트에 "confirmed_relations는 권한 필터됨, 없는 관계를 추측하지 말 것" 지시 추가.

| 항목 | v3 | v4 |
|---|---|---|
| 후보 검색 | 전역 workspace 1개 | 허용 팀별 workspace 병렬 검색(`LightRagTeamScopeAdapter.query_data`) |
| DB 재조회 진입점 | `WorklogLightSourceRowReader.fetch_source_rows` 직접 | `LightRagTeamScopeAdapter.read_source_rows`로 감싼 뒤 호출 |
| 색인 소유권 검증 | 없음 | 전역 색인 메타데이터의 `indexed_team_id`와 현재 DB 팀이 같은 후보만 유지(팀 이동 후 이전 팀 색인 잔여 차단) |
| 색인 완료 상태 검증 | 없음 | 팀 workspace 처리 상태와 전역 완료 게이트(`COMPLETED`) 확인, DB 조회 직후 재확인(`still_completed`)해 색인·삭제 진행 중 문서 제외 |
| `allowed_team_ids=None` | 허용 팀 0개로 취급(결과 없음) | 범위 제한 없음으로 취급 |
| 응답 캐시 | Redis 정확 일치 + 임베딩 유사도 캐시 | 비활성화 — 캐시 히트가 DB 재조회·검증을 우회할 수 있어 DB 최신성 검증 지원 전까지 끔 |

## 모듈 구조 차이

공통 모듈(model, prompt/worklog_query_prompt, router, service의 adapter·taskgroup_search·context_repair·custom_kg_builder·custom_kg_source_reader·document_builder·index_service·query_cache·query_service·source_reader·source_row_reader, store의 relation_scope_store·source_store)은 양쪽에 있으며 v4에서 import 경로가 `app.light.v4.*`로 바뀌었다. 그 외 차이는 다음과 같다.

### v4에만 있는 모듈

| 모듈 | 역할 |
|---|---|
| `service/worklog_index_dispatch_service.py` | 색인·재색인·삭제 요청을 Celery 작업으로 발행(Redis에는 worklogId/requestId/operation만 전달) |
| `service/worklog_team_scope.py` | 기존 LightRAG workspace 격리를 재사용하는 팀별 검색·색인 경계, 최신 업무 DB 문맥 조회 |
| `service/worklog_mutation_lock.py` | 업무일지 mutation의 애플리케이션 잠금 |
| `store/worklog_team_vector_store.py` | LightRAG 1.4.10 팀 vector storage 초기화. 구 전역 데이터 자동 이관 금지 |
| `store/worklog_custom_kg_tracking.py` | Custom KG 문서 삭제 추적 누락 보완, 다른 문서가 참조하는 공유 노드 복원 |
| `store/worklog_extraction_cache.py` | native extraction cache 결과 검증 proxy(잘못된 추출 결과 제거) |
| `chain/worklog_extraction.py`, `prompt/worklog_extraction_prompt.py` | native LightRAG 추출 문자열 검증과 제한 재시도 |

### v3에만 있던 것

- `router/__init__.py`(빈 패키지 파일)만 v3에 있었다. 기능 모듈 중 v3에만 있는 것은 없었다.

### 주요 파일별 변화

| 파일 | v3 → v4 변화 |
|---|---|
| `model/worklog_index.py` | `requestId` 추가, `/reindex` 요청 지원, 응답을 실행 결과(`indexed`/`error`)에서 접수 결과(`requestId`/`taskId`, HTTP 202)로 변경. worker 내부 실행 결과 모델(`*ExecutionItem/Response`)은 별도 정의 |
| `router/worklog_index.py` | `LightWorklogIndexService` 직접 호출 → `WorklogIndexDispatchService` 큐 발행. 내부 토큰 검사 추가 |
| `router/worklog_query.py` | 질의 캐시 구성 제거, Langfuse 추적 추가 |
| `service/lightrag_adapter.py` (733 → 1118줄) | PG 저장소 환경 분리(`LIGHTRAG_PG_DSN`이 업무 DB와 다른 DB여야 함), vector/graph/KV workspace 일치 검증, 색인 팀 소유권 기록(`indexed_team_id`), 전역 완료 게이트 영속화, PG delete 예외 은폐 보완·삭제 후조건 확인, 답변 전용 모델 함수 |
| `service/worklog_index_service.py` (258 → 371줄) | 팀 workspace 색인, mutation 잠금, 보상 정리(`_await_compensation`/`_compensate_document`), 오류 코드 세분화(`LIGHTRAG_DELETE_FAILED_COMPENSATED`, `LIGHTRAG_COMPENSATION_FAILED`) |
| `service/worklog_query_service.py` (395 → 276줄) | 응답 캐시·질의 임베딩 경로 제거, 팀 scope adapter 사용, 최신 DB 문맥 검증 후 답변 |
| `store/worklog_source_store.py`, `service/worklog_source_row_reader.py` | source row에 `team_id` 추가, 재구축용 생존 업무일지 ID keyset 조회(`fetch_source_ids`) 추가 |

## 삭제 후에도 이름에 "v3"가 남는 항목 (의도적으로 유지)

이름에 v3가 들어 있지만 v4가 실제로 쓰는 값이라 바꾸지 않는다. 바꾸면 기존 데이터·캐시를 찾지 못한다.

- `LIGHTRAG_WORKING_DIR=./data/lightrag-v3` (`ai/app/config/settings.py`, `ai/.env.example`, `ai/Dockerfile`, `infra/compose.local.yml`, `ai/tests/e2e/compose.yml`): v4 LightRAG의 실제 작업 디렉터리.
- v4 질의 캐시 Redis 키 prefix `worklog:query:v3:relations-v1` (`ai/app/light/v4/service/worklog_query_cache.py`).
- `docs/ai/evaluation/v1/*.py` 등 과거 평가 스크립트의 `app.light.v3` import: 측정 당시 기록이므로 수정하지 않는다. 재실행하려면 v3가 있던 커밋에서 실행한다.

## 함께 삭제한 테스트

v3만 검증하던 테스트 15개를 삭제했다: `test_lightrag_v3_adapter.py`, `test_light_worklogs_v3_query_router.py`, `test_light_worklogs_v3_router.py`, `test_light_worklog_context_repair.py`, `test_light_worklog_custom_kg_builder.py`, `test_light_worklog_custom_kg_source_reader.py`, `test_light_worklog_document_builder.py`, `test_light_worklog_index_service.py`, `test_light_worklog_query_scope.py`, `test_light_worklog_query_service.py`, `test_light_worklog_scoped_adapter.py`, `test_light_worklog_source_reader.py`, `test_light_worklog_source_row_reader.py`, `test_light_worklog_source_store.py`, `test_worklog_query_cache.py`. `test_light_worklog_versions.py`는 v3 보존 검증을 제거하고 v4 라우트 등록과 v3 라우트 미등록 검증만 남겼다.

삭제로 v4 커버리지가 줄지 않는다. 15개 모두 v4 대응 테스트가 이미 있다: `test_lightrag_v4_adapter.py`, `test_light_worklogs_v4_query_router.py`, `test_light_worklogs_v4_router.py`, `test_light_worklog_v4_{context_repair,custom_kg_builder,custom_kg_source_reader,document_builder,index_service,query_service,scoped_adapter,source_reader,source_row_reader,source_store,query_cache}.py`, 그리고 v3 `query_scope`에 해당하는 `test_light_worklog_team_scope.py`·`test_light_worklog_v4_query_safety.py`. v3와 v4의 공통 모듈 대부분(taskgroup_search, context_repair, custom_kg_source_reader, document_builder, source_reader, relation_scope_store, model/worklog_query, prompt/worklog_query_prompt)은 import 경로 외 코드가 동일했다.
