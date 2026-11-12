# AI 작업·Light 색인 단순화 결과

## S1b 잔여 정리(R-S1 minor)

1. 알림의 미사용 `DELIVERY_UNCONFIRMED`/`RESULT_UNCONFIRMED` 분기와 재시도 중단 문구, 10-인자 오버로드를 제거하고 재시도 조건을 `FAILED`로 정리했다. 실제 실패 상태를 조회하는 메서드는 `isFailed`로 바꿨다.
2. 사용하지 않는 `recoveryCandidates` 플래그와 `claimDelivery` 오버로드, `startDecision` 래퍼를 제거하고 호출부와 테스트를 맞췄다.
3. 운영 호출이 없는 `InternalWorklogAiCallbackService`, `InternalFileAiCallbackService`, `hasPostProcess` 및 두 서비스 전용 테스트를 삭제했다. 식별자 없는 LEGACY 콜백의 컨트롤러 차단과 그 테스트는 유지한다.
4. 일반·Light 결과의 영구 4xx(408/429 제외)는 재전송을 중단하고 파일 삭제와 상태 코드 로그를 남긴다. 408/429·5xx·통신 실패는 기존대로 보관하며, 태그명과 설명의 공백만 있는 값은 AI 모델에서 거부한다. 영구 거절된 작업의 도메인 상태는 기존 lease 만료 회수로 FAILED 처리된다.
5. Light 만료 회수는 poll당 한 번만 조회하며 회수와 개별 처리 오류를 각각 기록하고 다음 건을 처리한다.
6. 지적된 DTO record와 저장소 후보·결과 권한 타입에 Javadoc을 추가했다. 삭제된 G0 테스트가 포함된 아래 산출물만 제거하고 normal 하네스 산출물은 보존했다.

삭제: 호출 없는 파일·Worklog AI 콜백 서비스 2개와 전용 테스트, 실행 산출물(`ai/tests/e2e/artifacts-*`).

검증: API `gradlew.bat test --no-daemon` 성공, 686건 중 실패 0·오류 0·건너뜀 7 (`api/build/test-results/test/TEST-*.xml`). AI `.venv/Scripts/python.exe -m pytest tests -q --junitxml=%TEMP%/s1b-ai-junit.xml` 성공, 572 passed·17 skipped·경고 2. API 통합 테스트는 Testcontainers를 사용했으며 실제 LLM과 axwms-* 리소스는 사용하지 않았다. AI 테스트 환경은 Python 3.11.6이며 Google 라이브러리의 Python 지원 종료 예고와 로컬 Qdrant 인덱스 경고가 발생했다.

2026-11-12, Task S1. 사용자 결정에 따라 G0의 원격 쓰기 종료 증명과 전역 실행 gate를 제거했다. 작업별 Outbox, 최신 요청 검증, 결과별 알림, 보상 삭제, 수동 재시도와 단일 lease 만료 회수는 유지했다. 실제 LLM, 개발 DB 초기화, 기존 개발 컨테이너·볼륨 변경, site-packages 수정, 커밋·푸시는 수행하지 않았다.

## 실행 계약

- 자동 시도 한도는 API의 `AI_TASK_MAX_ATTEMPTS` 하나이며 기본값은 **총 4회**다. 최초 전달도 포함하고, HTTP 전달 실패와 AI 실행 실패가 같은 `attempts`를 사용한다. provider별 DB 예산과 별도 실행 횟수는 없다.
- Outbox 선점 시 `lease_until = 현재 시각 + CELERY_TASK_TIME_LIMIT_SECONDS + 60초`를 설정한다. 기본값은 600초다. 큐 대기·실행·결과 대기에 같은 lease를 사용하며 heartbeat, 실행 lease, 전체 결과 deadline을 별도로 두지 않는다.
- 선점은 `FOR UPDATE SKIP LOCKED`로 수행한다. 외부 HTTP는 DB 트랜잭션 밖에서 실행한다. 콜백·수정·삭제·수동 재시도는 업무 행을 먼저 잠근다.
- 일반 작업은 `(worklog_id, task_kind, target_id)`당 현재 요청 하나다. `SUMMARY`, `TAG`, 파일별 `FILE_SUMMARY`가 독립적으로 실행·실패·재시도된다. 쓰지 않던 LEGACY 집계 워커·checkpoint 경로를 제거했다.
- Light Outbox는 업무당 한 행이며 INDEX/REINDEX/DELETE, DELETE 우선, 증가하는 revision을 유지한다. 시작 API는 최신 요청·유효 선점에 대해 `execute: true/false`만 반환한다. 실행 시작 전 또는 자동 재시도 사이에 선점 없는 메시지가 시도 한도를 우회할 수 없다.
- `request_id`, 입력 해시와 `execution_id`를 이용해 최신 요청의 해당 실행만 반영한다. 일반 작업의 결과 digest와 Light의 종결 상태로 동일 결과를 멱등 처리하고 모순된 재통보를 무시한다. 별도 execution token과 dispatch sequence는 제거했다.

## 단순화된 흐름

```text
업무 등록·파일 추가
  → 같은 트랜잭션에서 SUMMARY / TAG / FILE_SUMMARY Outbox와 INDEX 의도 저장
  → SKIP LOCKED 선점, attempts + 1, 단일 lease 설정
  → AI HTTP 접수 → ai_general 또는 worklog_index 큐
  → 최신 요청 시작 확인 → AI 실행 → 결과 파일 보관 → 결과 콜백
  → 최신 요청·실행·입력 확인 → 도메인 상태와 작업별 알림 함께 커밋
  → 콜백 전송 실패: 보관 결과만 재전송, ACK 뒤 파일 삭제

색인 실행 실패
  → 쓰다 만 팀·전역 문서 보상 삭제(shield + timeout)
  → 최신 실행이며 시도 여유가 있으면 Outbox backoff → 재선점·재실행
  → 한도 소진 또는 치명적 설정 오류: FAILED + 실패 알림
  → 작성자 수동 재시도: 새 request_id, attempts 0, retried_from_request_id 기록

접수한 메시지나 실행 결과가 끝내 오지 않음
  → 단일 lease 만료 → FAILED + 실패 알림 → 작성자 수동 재시도
```

전달 실패와 일시 실행 실패의 backoff는 시도 횟수에 따라 1/2/4/…초로 증가한다. 빠른 시작·실패·결과가 먼저 도착한 경우 뒤늦은 HTTP 접수 응답이 해당 전이를 되돌리지 않는다. 과거 색인 결과는 최신 요청 상태·Outbox·알림을 변경하지 않는다.

## 유지 1~15 검증표

아래 테스트는 최종 전체 실행에 포함되었다. opt-in E2E는 별도 검증 범위에서 구분한다.

| 번호 | 유지 기능 | 이를 확인하는 테스트 |
|---|---|---|
| 1 | 업무당 Light Outbox 한 행, INDEX/REINDEX/DELETE, DELETE 우선, revision | `WorklogLightOutboxIntegrationTest.latestIntentKeepsOneRowIncreasingRevisionAndDeletePriority`; `WorklogLightTaskResultIntegrationTest.finalFailureStopsAutomaticDeliveryAndNotifiesAuthorOnce`, `oldResultsInAnyOrderAreStaleAcksForReplacement` |
| 2 | SUMMARY/TAG/파일별 FILE_SUMMARY 독립 처리 | `WorklogAiTaskSchemaIntegrationTest.initialEnqueueCreatesIndependentTasksWithoutReplacingPendingRows`; `InternalWorklogAiTaskResultIntegrationTest.registrationEventCreatesTaskOutboxAndScheduledWorkerUsesIt`, `addedFileEventIsPersistedAsSingleTaskBeforeCommit`, `failedFileCanBeRetriedIndependentlyByAuthor` |
| 3 | SKIP LOCKED + lease, 실패 backoff·자동 재시도 한도 | `WorklogAiTaskSchemaIntegrationTest.lockedTaskIsSkippedWithoutBlockingOtherTask`, `deliveryClaimsCountAttemptsAndRejectsStaleWorkers`, `retryTransitionIsIdempotentAndFourthFailureRemainsAvailableForTerminalResult`; `WorklogLightOutboxIntegrationTest.lockedWorklogIsSkippedWithoutBlockingOtherWorklog`; `WorklogLightTaskResultIntegrationTest.fourAutomaticFailuresNotifyOnceThenAllowManualRetry` |
| 4 | 요약·태그·파일·색인별 결과 알림 | `InternalWorklogAiTaskResultIntegrationTest.summaryAndNotificationCommitOnceAndConflictingResultIsRejected`, `tagResultCreatesAssociationAndNotificationOnlyOnce`, `fileSummaryResultIsAppliedOnlyToTheCurrentActiveFile`; `WorklogLightTaskResultIntegrationTest.finalFailureStopsAutomaticDeliveryAndNotifiesAuthorOnce`, `teamWithoutDepartmentCanReceiveIndexResultNotification` |
| 5 | 알림 unique와 중복 방지 | `InternalWorklogAiTaskResultIntegrationTest.summaryAndNotificationCommitOnceAndConflictingResultIsRejected`; `WorklogLightTaskResultIntegrationTest.concurrentLatestSuccessReplaysNotifyOnceAndIgnoreContradiction`, `repeatedAndContradictoryResultCannotChangeTerminalState`; migration의 `uq_notification_ai_task_result` 유지 |
| 6 | 부분 색인 보상 삭제와 취소 시 보상 대기 | `test_light_worklog_v4_index_saga.py::test_failed_phase_is_compensated_without_failing_other_documents`, `test_cancelled_index_waits_for_compensation_before_releasing_mutation_lock`, `test_compensation_timeout_preserves_team_ownership`, `test_global_compensation_failure_is_reported` |
| 7 | STAGING → COMPLETED 검색 노출 | `test_light_worklog_v4_index_saga.py::test_completion_is_published_only_after_all_workspaces_succeed`; `test_light_worklog_v4_completion.py::test_native_processed_requires_completed_publication`, `test_global_and_team_search_require_the_same_gate_and_live_owner`의 gate는 문서 노출 상태 검사이며 제거한 전역 실행 gate와 다름 |
| 8 | 최신 요청·입력만 반영, 요약·태그 supersede | `WorklogAiTaskSupersedeIntegrationTest.bodyChangeSupersedesInFlightSummaryAndTag`, `requestContentChangeSupersedesOnlySummary`, `removingFileSupersedesOnlyThatFileTask`, `deletingWorklogSupersedesInFlightTasks`; `WorklogLightTaskResultIntegrationTest.oldResultsInAnyOrderAreStaleAcksForReplacement` |
| 9 | 같은 실행의 첫 결과, 중복·모순 콜백 차단 | `WorklogAiTaskSchemaIntegrationTest.terminalResultIsIdempotentAndRejectsStaleOrContradictoryCallbacks`; `WorklogLightTaskResultIntegrationTest.repeatedLatestResultOverHttpIsIdempotentAck`, `repeatedAndContradictoryResultCannotChangeTerminalState`; `test_worklog_index_result_store.py::test_first_terminal_result_is_retained_across_reads` |
| 10 | 내부 API 토큰과 콜백 인증 순서 | `test_ai_ingress_security.py::test_ingress_rejects_missing_or_wrong_token_before_dispatch`; `InternalWorklogAiTaskControllerTest.invalidBodyDoesNotRevealValidationBeforeInternalTokenCheck`; `InternalWorklogLightIndexControllerTest.both_callbacks_require_matching_nonempty_configured_token` |
| 11 | 팀 사본·prefilter 권한, 삭제 원본 검색 제외 | `test_light_worklog_team_scope.py::test_index_separates_shared_entities_and_never_copies_cross_team_predecessors`, `test_queries_for_different_team_scopes_do_not_mix_under_concurrency`, `test_source_lookup_excludes_missing_and_unprocessed`, `test_latest_source_and_moved_predecessor_are_filtered_in_normal_and_sse`; `test_light_worklog_v4_query_safety.py::test_global_query_validates_before_generation_and_ignores_index_content` |
| 12 | ai_general/worklog_index 분리, 단일 Celery hard-limit 설정 | `test_celery_routing.py::test_task_routes_to_expected_queue`, `test_explicit_index_queue_is_preserved`; `test_celery_worker_compose.py::test_workers_are_isolated_and_share_required_configuration`; `test_light_worklog_tasks.py::test_all_celery_tasks_share_hard_limit` |
| 13 | 업무·workspace 프로세스 잠금 | `test_light_worklog_v4_index_service.py::test_mutation_lock_cancelled_waiter_releases_partial_batch_and_reversed_ids_finish`; `test_light_worklog_v4_index_saga.py::test_cancelled_index_waits_for_compensation_before_releasing_mutation_lock`; 기존 `worklog_mutation_lock.py` 유지 |
| 14 | 실패 작업 수동 재시도, retryable, 중복 클릭 수렴 | `InternalWorklogAiTaskResultIntegrationTest.concurrentRetryClicksConvergeOnOneNewRequest`, `manualRetryIsAuthorOnlyAndRepeatedClickReturnsSameNewRequest`, `notificationListExposesRetryOnlyForCurrentFailedTask`; `WorklogLightTaskResultIntegrationTest.deletedIndexRetryIsAuthorOnlyAndConcurrentClicksReturnSameRequest`, `retryablePredicateMatchesEndpointWhileReplacementProgresses`; `WorklogAiTaskRetryControllerTest` |
| 15 | 멈춘 작업의 단일 timeout → FAILED·알림·수동 retry | `WorklogAiTaskRecoveryIntegrationTest.timeoutFailsOnceAndOldExecutionCannotCommit`, `acceptedButNeverStartedTimesOut`, `liveLeaseIsNotRevoked`, `notificationAndTimeoutRollbackTogether`; `WorklogLightTaskResultIntegrationTest.singleLeaseTimeoutFailsAndNotifiesOnce`; `WorklogAiTaskSchemaIntegrationTest.schemaHasOnlyCurrentTaskIdentityAndSingleAttemptLease` |

## 제거 1~8 변경표

경로는 저장소 루트 기준이다. 삭제된 파일은 현 작업 시작 시 존재하던 미커밋 파일도 포함한다.

| 번호 | 제거 내용 | 삭제 파일·주요 변경 파일 |
|---|---|---|
| 1 | 전역 gate·epoch·BUSY·종료 증거·시작 승인 재시도 | 삭제: `api/.../repository/WorklogLightGateRepository.java`, 구 V29~V31. 변경: `InternalWorklogLightIndexService.java`, Light 내부 Controller/Docs/DTO, `WorklogLightOutboxRepository.java`, `WorklogAiTaskRetryService.java`, `NotificationSearchProjection.java`, `ai/app/client/worklog_index_client.py`, `ai/app/task/light_worklog_tasks.py`, `ai/app/store/worklog_index_result_store.py`. `/execution-end`, 종료 증거 보관·replay, `deferBusy`/`wakeGateWaiters` 제거 |
| 2 | mutation guard 전체 및 호출부 | 삭제: `ai/app/light/v4/service/worklog_mutation_guard.py`. 변경: `worklog_index_service.py`, `lightrag_adapter.py`, `light_worklog_tasks.py`, `worklog_team_vector_store.py` |
| 3 | provider 오류 분류·실패 latch·native rebuild history 검사 | 위 guard 및 adapter/task/service의 G0 추가분 제거. 자식 task 추적, `failure_confirmed`, `compensation_scope`, `observe_native_delete_rebuild`, native 삭제 status latch, 보상 등록 전 latch 제거. 기존 검색용 provider 처리와 부분 문서 보상은 유지 |
| 4 | G0 저장소 단일 시도 wrapper | 삭제: `ai/app/light/v4/store/worklog_graph_store.py`, `worklog_pg_store.py`. 변경: `lightrag_adapter.py`, `worklog_team_vector_store.py`. 강제 Tenacity 1회, 명시 transaction wrapper, wait/completed 검증·scroll latch 제거. `requirements.txt`의 G0용 `neo4j==6.3.1` 고정은 기존 `neo4j>=5.0.0,<7.0.0`으로 복원. G0 이전의 PG 삭제 후조건 검사·문서 상태 추적은 정상 기능에 필요해 유지 |
| 5 | provider 작업별 DB 예산·checkpoint | 삭제: `ai/app/client/ai_task_provider_budget.py`, 관련 Python/Java canonical-vector 테스트·fixture. 변경: `gemini_client.py`, `ai_task_executor.py`, `file_summary/dispatch.py`, `AiTaskApiDto.java`, `InternalWorklogAiTaskController.java`, `WorklogAiTaskRepository.java`. provider-attempt/provider-result API 제거 |
| 6 | 이중 전달·실행·provider 예산 및 다중 기한 | 변경: 두 Outbox repository, `AiTaskExecutionBudgetProperties.java`, 일반 worker/recovery/delivery outcome/retry, API·AI DTO/identity와 테스트, `application.yml`, 신규 V25. 별도 delivery_count/execution_count/provider_attempts/execution_token/dispatch_seq/result_deadline_at/execution_lease_until/retry_execution_id 제거. 단일 attempts·lease 사용. 미사용 LEGACY 집계 서비스·워커와 전용 4개 테스트 제거, 잠금 repository만 축소 유지 |
| 7 | 결과 전달 claim·총 4회 전달 예산·결과→종료 증거 순서 | 삭제: `ai/app/store/worklog_index_delivery_claim.py`. 변경: `ai_task_result_delivery.py`, `worklog_index_result_delivery.py`, `worklog_index_result_store.py`. 보관 후 재전송·성공/stale ACK 뒤 삭제만 유지하며 전송 오류로 AI를 재실행하지 않음 |
| 8 | G0 전용 E2E·단위 테스트 | 삭제: `ai/tests/e2e/{g0_worker.py,g0_fault_proxy.py,test_g0_inflight_e2e.py,test_g0_fault_proxy.py,compose.g0.yml,run-g0.ps1,run-g0-matrix.ps1,artifacts-g0/}`. 삭제한 단위 테스트는 아래 목록. 정상 E2E `test_light_normal_flow_e2e.py`와 normal 하네스는 bool 시작 응답·attempts 계약에 맞춰 유지·수정 |

삭제한 G0 단위 테스트:

```text
test_light_execution_gate.py
test_light_mutation_confirmation.py
test_light_compensation_provider_failure.py
test_light_compensation_rebuild_evidence.py
test_light_confirmed_failure_recovery.py
test_light_delete_rebuild_evidence.py
test_light_native_delete_failure.py
test_light_single_attempt_storage.py
test_light_qdrant_inflight_probe.py
test_light_generation_fencing_runtime.py
test_ai_task_provider_budget.py
test_task_execution_budget_alignment.py
test_provider_canonical_vectors.py
```

API의 `AiTaskExecutionBudgetPropertiesTest`, `WorklogAiTaskBudgetIntegrationTest`, `ProviderCanonicalVectorsTest`는 제거된 다중 예산·provider 계약 전용이라 삭제했다. 일반 작업·Light의 새 단일 lease/한도는 위 유지 3·15의 실제 PostgreSQL 테스트가 확인한다.

## Migration 최종 목록

| 범위 | 최종 상태 |
|---|---|
| V1~V24 | 수정 없음. `git diff --exit-code` 확인 |
| V25 | **`api/src/main/resources/db/migration/V25__worklog_ai_tasks.sql`** 하나로 통합 |
| 구 V26~V31 | 미커밋 파일 삭제. V25 다음에 불연속 번호를 남기지 않음 |

V25는 일반 Outbox의 작업별 복합 PK, request/execution/input 식별, 결과 멱등 정보, 수동 재시도 관계와 알림의 4개 AI 열·unique를 추가한다. Light에는 수동 재시도 관계만 추가하며 기존 V19~V24의 operation/revision/attempts/lease/최종 실패 기능을 재사용한다. LEGACY용 retry_payload/completed_stages/pending_failed_stages/terminal_failed는 일반 Outbox에서 제거한다. V25에는 빈 Outbox 배포 guard, gate 테이블, 별도 실행 예산·기한 CHECK를 만들지 않는다.

표준 Gradle compile 진입점이 `jooqGenerate`를 선행해 이 스키마로 재생성하는 것을 확인했다. 생성물은 `api/src/generated/jooq/main/java`에 있으며 Git 관리·수기 수정 대상이 아니다. 코드 생성의 자체 임시 DB와 테스트의 Testcontainers만 사용했고, 개발 DB 초기화는 실행하지 않았다.

## 검증 결과와 증거

| 검증 | 최종 명령·결과 | 증거 경로 |
|---|---|---|
| API 전체 | `cd api; .\gradlew.bat test --console=plain` → **693 tests / 0 failures / 0 errors / 7 skipped**, BUILD SUCCESSFUL, 2분 55초 | `api/build/test-results/test/TEST-*.xml` 93개, `api/build/reports/tests/test/index.html`; 보존 사본 `C:/Users/a7710/AppData/Local/Temp/axwms-s1-evidence/api-junit/`; 실행 로그 같은 폴더 `api-full.log` |
| AI 전체 | `cd ai; .venv/Scripts/python.exe -m pytest tests -q -rs --tb=short --junitxml=C:/Users/a7710/AppData/Local/Temp/axwms-s1-evidence/ai-full.xml` → **561 passed / 17 skipped / 2 warnings**, 21.05초 | `C:/Users/a7710/AppData/Local/Temp/axwms-s1-evidence/ai-full.xml`, `ai-full.log` |
| 선행 집중 회귀 | AI 핵심 경로 **94 passed**; API 작업·Light·클라이언트 집중 실행 통과 후 시작 선점 계약·SKIP LOCKED 회귀를 추가하여 최종 전체에서 검증 | 같은 증거 폴더의 `s1-ai-focused.log`, `s1-api-focused.log` |
| 코드 생성 | V25 통합 후 PostgreSQL bootstrap·jOOQ 생성 및 전체 컴파일 성공 | `s1-api-focused.log`, disposable generated source |
| opt-in 하네스 컴파일 | callback E2E의 불필요한 등록 집계 분기를 제거하고 `compileTestJava` 성공. normal E2E의 직접 태그 콜백에서도 제거된 dispatchSeq·실행 token을 사용하지 않도록 수정 | `api-compile-final.log`; 실제 opt-in E2E 실행은 아래와 같이 제외 |
| 제거 참조 | `rg` 설치·사용 확인. 제품 코드와 신규 V25에서 아래 제거 식별자 **0건**, exit 1 | 아래 명령 |
| 변경·인코딩 | `git diff --check` exit 0; V1~V24 diff exit 0; 코드·보고서 689개 파일 UTF-8 decode·U+FFFD 검사 및 보고서 재읽기 | `C:/Users/a7710/AppData/Local/Temp/axwms-s1-evidence/summary.json` |

```powershell
rg -n 'worklog_mutation_guard|WorklogLightGateRepository|tb_worklog_light_gate|worklog_graph_store|worklog_pg_store|ai_task_provider_budget|worklog_index_delivery_claim|execution-end|DRAINED|NOT_STARTED|busy_attempts|drained_execution_ids|not_started_execution_ids' ai/app api/src/main/java api/src/main/resources/db/migration/V25__worklog_ai_tasks.sql
```

API와 pytest를 동시에 실행하지 않았다. 최초 AI 전체 실행에서는 dispatchSeq가 빠진 뒤 유효해진 예전 invalid fixture 1건이 실패했고, 이를 새 계약의 잘못된 inputHash fixture로 수정한 뒤 위 최종 전체 실행이 통과했다.

API skip 7건은 명시적 Python callback E2E 환경 미설정이다. AI skip 17건은 normal Compose E2E 10건, Linux/격리 Redis worker 4건, 격리 PG 1건, Qdrant/Neo4j 서버 통합 2건이다. normal 하네스는 갱신했으나 이를 실제 실행 통과로 집계하지 않는다. 로컬 Python은 3.11.6이며 3.12 재검증은 수행하지 않았다. 경고는 Google의 Python 지원 예정 변경과 local Qdrant의 payload index 미지원이다.

## 제거 전후 규모

비교 기준은 **이번 S1 시작 시의 미커밋 작업 상태**다. HEAD와의 diff에는 이전 작업이 섞이므로 사용하지 않았다. 시작 시 파일별 줄 수 snapshot을 저장하고 같은 범위의 `.py/.java/.sql` 실제 줄 수를 비교했다. 설정·셸·JSON·생성 jOOQ·캐시·G0 증거 산출물은 이 코드 수치에서 제외했다.

| 범위 | S1 시작 | S1 완료 | 감소 |
|---|---:|---:|---:|
| `ai/app` | 11,010 | 9,860 | 1,150 |
| `ai/tests` 코드·하네스 | 14,938 | 11,616 | 3,322 |
| API main Java | 24,858 | 23,590 | 1,268 |
| API Flyway SQL | 921 | 855 | 66 |
| API test Java·SQL | 22,827 | 19,696 | 3,131 |
| 기타 기존 SQL | 7,691 | 7,691 | 0 |
| **합계** | **82,245** | **73,308** | **8,937 (10.9%)** |

제품 코드와 migration만 합하면 36,789 → 34,305줄로 **2,484줄 감소**했다. 나머지 감소는 제거된 G0/LEGACY 테스트다. 근거 snapshot: `C:/Users/a7710/AppData/Local/Temp/axwms-s1-before.json`, `C:/Users/a7710/AppData/Local/Temp/axwms-s1-evidence/lines.json`.

## 알려진 한계

- **timeout 뒤 원격 쓰기:** DB에서 FAILED로 정리해도 이미 전송된 Qdrant/Neo4j/PG 요청이 계속 반영될 수 있다. executionId 검사는 API 결과 반영을 차단하지만 원격 저장소를 fence하거나 종료를 증명하지 않는다.
- **native 공유 엔티티 rebuild:** LightRAG가 공유 엔티티 rebuild 실패를 내부에서 삼키면 최상위 삭제가 성공으로 보일 수 있다. history 검사와 latch를 제거했으므로 이 경우의 정합성 증명을 제공하지 않는다.
- **프로세스 잠금 범위:** 업무·workspace 잠금은 프로세스 안에서만 유효하다. 기존 단일 Light worker/concurrency=1 전제를 유지하며 다중 worker·서버에서의 원격 mutation 직렬화를 보장하지 않는다.
- **큐 대기도 같은 lease 사용:** 많은 대기 작업 때문에 정상 메시지가 시작 전에 600초를 넘겨도 실패·수동 재시도로 정리된다. lease 만료 자체로 자동 재실행하지 않는다.
- **보상 제한:** 취소 중에도 shield로 보상 종료를 기다리지만 보상 timeout·hard kill·저장소 실패 때 부분 데이터가 남을 수 있다. 실패 알림 뒤의 재시도에서 다시 정리하며 성공을 보장하지 않는다.
- **provider·결과 전송 횟수:** provider별 누적 예산과 결과 전달 claim을 제거했다. provider 호출의 전체 중복 비용과 동시 콜백 횟수는 별도로 제한하지 않으며, 저장된 결과는 API ACK까지 재전송한다. API는 동일 실행의 첫 결과만 반영한다.

## 최종 정리(S3)

- 삭제 코드(API): `InternalFileAiCallbackController(+Docs)`, `InternalWorklogAiCallbackController(+Docs)`(항상 거절만 하던 `PATCH /internal/files/{id}/ai-result`, `PATCH /internal/worklogs/{id}/ai-result`, `POST /internal/worklogs/{id}/ai-tags`), `UpdateFileAiApiDto`, `UpdateWorklogAiApiDto`, `InternalFileLegacyCallbackBlockedTest`, `InternalWorklogAiCallbackControllerTest`. `ApplyWorklogAiTagsApiDto`는 현재 결과 반영 서비스가 사용하므로 유지했다.
- 삭제 코드(AI): 옛 경로를 호출하던 `FileClient`, `WorklogClient`, `TaggingClient.apply_worklog_ai_tags`, 옛 큐 메시지 거부 테스트 `test_legacy_ai_tasks_blocked.py`(식별자 일치 검증 테스트는 `test_identified_worklog_tasks.py`로 이동), 작업 함수의 `metadata=None` 기본값(식별자 필수).
- 삭제 문서: `docs/archive/g0-light-gate/` 전체(13개), `r-s1-review-2026-11-12.md`, `r-s2-review-2026-11-12.md`. `docs/README.md`, `docs/docs-strategy.md`, `docs/ai/README.md`, `docs/api/README.md`의 링크와 목차를 함께 정리했다.
- 검증: API `gradlew.bat test --no-daemon` 성공, 680건 중 실패 0·오류 0·건너뜀 7(삭제 테스트 6건 감소). AI `pytest tests` 567 passed, 17 skipped, 2 failed(`tests/pr_review/test_tools.py` 2건은 이 환경에 `rg`가 없어 생기는 `FileNotFoundError`로 이번 변경과 무관). 문서 상대 링크 331개 중 깨진 링크 0, 삭제한 엔드포인트(`ai-result`, `ai-tags`)를 호출하는 코드 0건.
- 남긴 항목: 식별자 없는 요청을 큐에 넣는 `request_id` 선택 필드와 작업 함수의 `metadata=None` 기본값은 모델 계약 변경이 필요해 유지했다(실행 시 식별자 검증에서 거절).
