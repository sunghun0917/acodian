# AI 작업 신뢰성 (작업별 알림, 수동 재시도, 단순화)

> **현재 상태:** 완료, 커밋됨 (2026-11-13). **지금 코드의 기준은 6절의 "11-12 단순화" 이후 구조다.** 그 전의 원격 쓰기 안전 장치, provider 호출 예산, 이중 실행 예산 등은 모두 제거됐다.
> **기간:** 2026-10-30 (요구사항) → 11-07 (계획) → 11-10 (정상 흐름 검증, 보강) → 11-11~12 (원격 쓰기 안전 장치) → 11-12 (방향 전환, 단순화) → 11-13 (정리, 커밋)
> **한 줄 요약:** 요약, 태그, 파일 요약, 색인을 작업 단위로 쪼개서 각각 결과를 알리고, 실패한 작업만 작성자가 다시 실행할 수 있게 했다. 중간에 안전 장치를 과하게 쌓았다가, 포트폴리오 목적에 맞게 핵심만 남기고 약 8,900줄을 걷어냈다.

---

## 1. 배경: 무엇이 문제였나

- 등록 후처리는 요약, 태그, 파일 요약, 색인을 **한 묶음으로 집계**해서 알림을 하나만 보냈다. 어느 작업이 실패했는지 알기 어려웠다.
- 실패하면 시스템이 자동 재시도만 했다. 사용자가 "이 작업만 다시"를 할 수 없었다.
- "전달 실패(HTTP 실패, 응답 유실)"와 "AI 작업 자체의 실패"가 섞여 있었다.
- 업무를 수정해도 진행 중인 요약/태그는 **옛 입력으로 계속 실행**됐다. 결과는 버려지는데 행이 RUNNING으로 남아서 새 요청이 영원히 안 생기는 결함도 있었다.
- 색인 Worker가 결과 통보 없이 죽으면 그 요청이 RUNNING에 묶여서 새 실행이 계속 거절될 수 있었다.

## 2. 목표와 완료 기준

- 요약, 태그, 파일별 요약은 **실제 결과마다** 알린다. 색인(INDEX/REINDEX/DELETE)은 **최신 요청의 결과만** 알린다.
- 실패한 작업만 작성자가 수동 재시도한다. 중복 클릭은 같은 새 요청으로 모인다.
- 결과와 알림은 같은 DB 트랜잭션으로 저장한다.
- 자동 시도는 최초 1회 + 재시도 3회 = **총 4회**.
- 오래된 결과는 최신 상태와 알림을 바꾸지 못한다.
- 범위 밖: 기존 Outbox/Redis 데이터 이관(초기화 허용), 구버전과 신버전 동시 운영, 과거 색인 요청별 알림 이력.

## 3. 선택지와 결정

### 일반 작업을 어떻게 쪼갤까
| 대안 | 장점 | 비용/위험 | 판단 |
|---|---|---|---|
| **기존 일반 Outbox를 작업별 행으로 확장** (PK `(worklog_id, task_kind, target_id)`) | 기존 선점, 스케줄러, 트랜잭션 재사용 | 모든 조회 변경, 구버전과 동시 실행 불가 | **선택** |
| 새 통합 작업 장부(ledger) 테이블 | 모델 통일, 기존 데이터 보존 가능 | 초기화를 허용하면 이점이 거의 없음 | X |
| 업무 1행에 JSON으로 작업 중첩 | PK 유지 | 파일별 경합, 알림 unique 처리 복잡 | X |

### 기존 데이터를 살릴까
- **살리지 않는다 (사용자 결정, 10-31).** 기존 Outbox와 Redis 대기 데이터 보존, 이관, 구버전 호환을 모두 뺐다. 덕분에 설계가 크게 줄었다.
- 단, 업무 DB, 기존 알림, Qdrant/Neo4j, AI 결과 파일은 초기화 대상이 아니다.

### 안전 장치를 어디까지 쌓을까 (가장 큰 결정)
| 시점 | 결정 |
|---|---|
| 11-11~12 | timeout 뒤에도 Qdrant/Neo4j/PG 쓰기가 살아 있을 수 있으니, **이전 실행이 끝났다는 증거가 올 때까지 다음 색인을 막는 전역 관문**을 만들었다. 불명확하면 무기한 보류. 검토에서 6번 반려되고 고치기를 반복했다. |
| 11-12 | Gemini 429 한 번에 전역 관문이 닫혀 **전체 색인이 멈추는** 운영 문제가 남았다. |
| **11-12 18:20** | **방향 전환 (사용자 결정): "운영 서비스가 아니라 포트폴리오다. 예외 처리가 과하다."** 전역 관문과 관련 장치를 전부 제거하고 핵심 기능만 남겼다. |

**포기한 것 (트레이드오프):** timeout 뒤 원격 저장소 쓰기를 막거나 종료를 증명하지 못한다. 대신 코드를 이해하고 설명할 수 있는 크기로 줄였다.

## 4. 구현: 어떻게 동작하나 (현재 구조)

### 흐름
```text
업무 등록, 파일 추가
  → 같은 트랜잭션에서 SUMMARY / TAG / FILE_SUMMARY Outbox + Light INDEX 의도 저장
  → FOR UPDATE SKIP LOCKED로 선점, attempts + 1, lease 설정
  → AI HTTP 접수 → ai_general 또는 worklog_index 큐
  → Worker가 시작 API로 "최신 요청이고 유효한 선점인가" 확인 (execute: true/false)
  → AI 실행 → 결과 파일 보관 → 결과 콜백
  → requestId, executionId, 입력 해시 확인 → 도메인 상태와 작업별 알림을 함께 커밋
  → 콜백 전송 실패 시 보관 결과만 재전송, ACK 받으면 파일 삭제

색인 실패
  → 쓰다 만 팀/전역 문서 보상 삭제
  → 시도 여유가 있으면 backoff(1, 2, 4...초) 후 재선점
  → 한도 소진 또는 설정 오류 → FAILED + 실패 알림
  → 작성자 수동 재시도: 새 requestId, attempts 0, retried_from_request_id 기록

메시지나 결과가 끝내 안 옴
  → 단일 lease 만료 → FAILED + 실패 알림 → 수동 재시도
```

### 핵심 규칙
- **시도 한도는 하나:** `AI_TASK_MAX_ATTEMPTS`(기본 4). 전달 실패와 실행 실패가 같은 `attempts`를 쓴다.
- **lease도 하나:** `CELERY_TASK_TIME_LIMIT_SECONDS + 60초`. 큐 대기, 실행, 결과 대기에 같은 lease를 쓴다.
- **외부 HTTP는 트랜잭션 밖.** 콜백, 수정, 삭제, 수동 재시도는 업무 행을 먼저 잠근다(잠금 순서 고정).
- **입력이 바뀌면 진행 중 작업을 교체(supersede)한다.** 요청 내용만 바뀌면 SUMMARY만, 업무 내용이 바뀌면 SUMMARY + TAG. 제목, 상태, 태그, 파일 변경은 교체하지 않는다. 파일을 제거하면 그 파일의 요약만 종결, 업무를 삭제하면 진행 중 작업 모두 종결.
- **결과 멱등:** 일반 작업은 결과 digest, 색인은 종결 상태로 같은 결과를 걸러내고 모순된 재통보는 무시한다.
- **영구 4xx는 재전송 안 함:** API가 408/429를 제외한 4xx로 거절하면 결과 파일을 지우고, 작업은 lease 만료로 FAILED 처리된다.
- **알림 중복 방지:** `uq_notification_ai_task_result` unique 제약.
- **내부 API 인증이 본문 검증보다 먼저:** `@ModelAttribute`로 토큰을 먼저 확인해서, 토큰 없는 요청에 본문 검증 오류(400)를 보여주지 않는다(403).

### 핵심 코드 위치
| 역할 | 파일 |
|---|---|
| 일반 작업 Outbox worker | `api/.../domain/worklog/service/WorklogAiTaskOutboxWorker.java` |
| 일반 작업 저장소 | `api/.../domain/worklog/repository/WorklogAiTaskRepository.java` |
| 시작 승인, 결과 반영 | `InternalWorklogAiTaskStartService.java`, `InternalWorklogAiTaskResultService.java` |
| lease 만료 회수 | `WorklogAiTaskRecoveryService.java` |
| 수동 재시도 | `WorklogAiTaskRetryService.java` |
| 색인 | `WorklogLightOutboxWorker.java`, `InternalWorklogLightIndexService.java` |
| AI 결과 전달 | `ai/app/service/ai_task_result_delivery.py`, `worklog_index_result_delivery.py` |
| 마이그레이션 | `V25__worklog_ai_tasks.sql` (하나로 통합) |
| 웹 알림 재시도 | `web/src/app/(protected)/notification/` |

## 5. 결과와 검증

### 정상 흐름 검증 (11-10)
격리된 Docker(API, AI, 색인 Worker 1개, PG, Redis, Qdrant, Neo4j)에서 **10건 통과**. LLM은 결정적 stub.
- 등록 → 색인 → 완료 알림 1건
- A 실행 중 B 수정 → A 결과는 B 상태를 안 바꾸고 알림 0, B만 알림 1
- A 조회 후 B, C 연속 수정 → B는 건너뛰고(SKIPPED) C만 처리
- 삭제가 겹치면 과거 INDEX/REINDEX 시작 거절, 저장소에서 삭제 대상 제거, 공유 데이터는 유지

### 단순화 결과 (11-12)
| 범위 | 전 | 후 | 감소 |
|---|---:|---:|---:|
| 제품 코드 + migration | 36,789줄 | 34,305줄 | 2,484줄 |
| 테스트 포함 전체 | 82,245줄 | 73,308줄 | **8,937줄 (10.9%)** |

- API 693건 통과(실패 0, 건너뜀 7), AI 561 passed(17 skipped)
- 유지 기능 15개 각각을 확인하는 테스트를 표로 남겼다 (원본 단순화 보고서 참고)
- 마이그레이션은 미커밋 V25~V31을 V25 하나로 통합

### 최종 정리 (11-13)
- 항상 거절만 하던 옛 콜백 엔드포인트 3개와 관련 클라이언트, 테스트 삭제
- API 680건 통과, AI 567 passed

## 6. 진행 중 바뀐 결정 (날짜순)

| 날짜 | 내용 |
|---|---|
| 10-30 | 요구사항: 작업별 실패 알림 + 수동 재시도 |
| 10-31 | 기존 Outbox/Redis 데이터 보존 안 함 → 이관, 호환 설계 제거 |
| 11-07 | 계획 확정. 일반 Outbox를 작업별 행으로 |
| 11-09 | "실행별 전체 workspace 복제 후 게시" 같은 격리 제안 **불채택**. 단일 Worker, 기존 증분 색인 유지 |
| 11-10 | **"장애 설계보다 정상 흐름부터"** 로 재계획. 정상 흐름 10건 통과 |
| 11-10 | 보강: 색인 Worker가 사라졌을 때 회수, 콜백 재전송/역순/모순 처리, 수정 시 진행 중 요약/태그 교체 |
| 11-11 | 보강: Celery hard limit 설정을 환경변수 하나로 통일, provider 호출 예산(요청 단위 4회 제한, Python과 Java 간 canonical JSON 일치까지) |
| 11-11~12 | 원격 쓰기 안전 장치(전역 관문) 구현. 검토 반려와 수정이 6회 반복 |
| **11-12** | **방향 전환:** 포트폴리오 목적에 맞게 단순화. 제거: 전역 관문, mutation guard, 저장소 단일 시도 wrapper, provider 예산, 이중 실행 예산, 결과 전달 순서 보장, 관련 E2E. 유지: 위 4절의 핵심 규칙 + **수동 재시도**(사용자 유지 결정) |
| 11-13 | 옛 콜백 경로 삭제, 커밋, PR 준비 |

**교훈:** 11-11의 provider 예산 작업(교차 언어 JSON 정규화까지)과 원격 쓰기 안전 장치는 기술적으로는 맞았지만 프로젝트 규모에 비해 비용이 컸다. 결국 하루 뒤 전부 제거됐다.

## 7. 한계와 위험

- **timeout 뒤 원격 쓰기:** DB에서 FAILED로 정리해도 이미 보낸 Qdrant/Neo4j/PG 요청이 계속 반영될 수 있다. executionId 검사는 API 쪽 결과 반영만 막는다.
- **LightRAG 공유 엔티티 rebuild 실패:** LightRAG 내부에서 실패를 삼키면 삭제가 성공으로 보일 수 있다. 이를 감지하던 장치는 제거했다.
- **프로세스 잠금:** 단일 색인 Worker, concurrency=1 전제. 다중 Worker에서는 직렬화를 보장하지 않는다.
- **큐 대기도 같은 lease:** 대기 작업이 많으면 정상 메시지도 시작 전에 lease를 넘겨 FAILED가 될 수 있다. 자동 재실행하지 않고 수동 재시도로 넘긴다.
- **보상 실패:** 보상 timeout, 강제 종료, 저장소 실패 때 부분 데이터가 남을 수 있다.
- **provider 비용 무제한:** provider별 누적 예산을 제거해서 전체 중복 호출 비용은 따로 제한하지 않는다.
- **보안:** 내부 토큰 상수 시간 비교, 인증을 본문 검증보다 먼저 수행, 수동 재시도는 작성자만 가능.

## 8. 앞으로 개선할 점

1. 다중 색인 Worker가 필요해지면 분산 락 (DB advisory lock 또는 Redis)
2. 큐 적체 시 lease를 큐 대기와 실행으로 나눌지 검토 (지금은 단순함을 택함)
3. provider 호출 비용 모니터링
4. 태그 저장 후 색인 반영 순서 문제 ([Outbox 문서](../worklog-outbox/README.md) 8절과 같은 문제)

## 9. 스스로 점검 질문

- **Q. 왜 요약, 태그, 파일 요약을 작업별 행으로 나눴나?**
  A. 묶어서 집계하면 어느 작업이 실패했는지 알릴 수 없고, 실패한 것만 다시 돌릴 수 없다. 작업별로 상태, 시도 횟수, 알림을 따로 가져야 한다.
- **Q. `FOR UPDATE SKIP LOCKED`를 쓰는 이유는?**
  A. 여러 스케줄러가 동시에 선점할 때 이미 잠긴 행은 건너뛰고 다른 행을 가져간다. 서로 기다리지 않는다.
- **Q. 업무를 수정하면 진행 중 요약은 어떻게 되나?**
  A. 입력 해시가 바뀐 작업만 같은 행을 새 requestId로 교체한다. 옛 실행의 결과는 requestId가 안 맞아 stale로 무시된다.
- **Q. 수동 재시도 버튼을 두 번 누르면?**
  A. 업무 행 잠금 안에서 "현재 retried_from이 이 실패 요청과 같은가"를 보고, 같으면 이미 만든 새 요청을 돌려준다. 새 실행이 두 번 생기지 않는다.
- **Q. 왜 전역 관문을 만들었다가 지웠나?**
  A. timeout 뒤 원격 쓰기 문제는 실제로 존재한다. 하지만 이걸 완벽히 막으려면 저장소별 종료 증거가 필요했고, 외부 API 오류 하나에 전체 색인이 멈추는 부작용이 생겼다. 운영 서비스가 아닌 포트폴리오에서는 한계로 문서화하는 편이 낫다고 판단했다.
- **Q. 전달 실패와 실행 실패를 왜 구분하나?**
  A. 전달 실패는 AI가 아예 못 받은 것이고, 실행 실패는 받아서 돌렸는데 실패한 것이다. 처음에는 따로 셌지만, 단순화 후에는 같은 attempts 한도를 쓰고 상태로만 구분한다.

## 10. 원본 문서

| 문서 | 내용 |
|---|---|
| [이 문서의 부록](#부록-현재-구현-흐름-원문) | **현재 구현 흐름 원문** (단순화 기준, 원래 별도 파일이던 것) |
| [simplification-2026-11-12.md](simplification-2026-11-12.md) | **단순화 결과**: 실행 계약, 유지 15개와 테스트, 제거 8개, 규모 감소, 한계 |
| `.omx/plans/prd-ai-task-manual-retry.md` | 최초 계획 (기존 데이터 이관 포함, 이후 대체됨) |
| `.omx/plans/ai-task-manual-retry-reset-addendum.md` | 기존 데이터 초기화 결정에 따른 계획 변경 |
| `.omx/plans/prd-ai-task-manual-retry-reset.md` | 초기화 전제의 계획 (대안 비교, 스키마, 상태 전이) |
| `.omx/plans/test-spec-ai-task-manual-retry*.md` | 테스트 계획 |
| `.omx/plans/ai-task-normal-indexing-*-2026-11-10.md` | 정상 흐름 재계획, 검증 명세, 검증 결과 |
| `.omx/plans/ai-task-light-recovery-budget-2026-11-10.md` | 색인 Worker 소실 시 회수 (C1) |
| `.omx/plans/ai-task-callback-retryable-2026-11-10.md` | 콜백 재전송, 역순, 모순 처리 (C2) |
| `.omx/plans/ai-task-review-minors-2026-11-10.md` | 검토 지적 정리 (C3) |
| `.omx/plans/ai-task-summary-tag-supersede-2026-11-10.md` | 수정 시 진행 중 요약/태그 교체 (C4) |
| `.omx/plans/ai-task-provider-budget*.md` | provider 호출 예산 (C5, 이후 제거) |
| `.omx/plans/ai-task-hard-limit-config-2026-11-11.md` | hard limit 설정 통일 |
| `.omx/plans/ai-task-internal-auth-order-2026-11-11.md` | 내부 API 인증 순서 점검 |
| `.omx/plans/ai-task-g0-revalidation-2026-11-09.md` | 원격 쓰기 안전성 재검증 (G0, 이후 제거) |
| `.omx/plans/claude-coordinator-handoff-2026-11-12.md` | 전체 진행 인계, 6절에 방향 전환 기록 |
| `.omx/plans/architect-*`, `critic-*` (manual-retry, normal-indexing) | 설계 검토 기록 |

---

## 부록. 현재 구현 흐름 원문

> 원래 별도 문서였던 `ai-task-manual-retry-current-flow-2026-11-07.md`(제목: AI 비동기 작업·알림·수동 재시도 현재 구현 흐름)를 원문 그대로 옮겼다. 파일 이름에는 11-07이 붙어 있었지만 내용은 11-12 단순화 기준이다.

2026-11-12 S1 단순화 기준. 변경 근거와 테스트·한계는 [S1 보고서](simplification-2026-11-12.md)를 참고한다.

### 등록부터 결과까지

업무 등록과 파일 추가 시 일반 작업 `SUMMARY`, `TAG`, 파일별 `FILE_SUMMARY`와 Light `INDEX` 의도를 같은 트랜잭션에 저장한다. 각 일반 작업과 Light는 독립적으로 실행되고 작업별 결과 알림을 만든다. Light는 업무당 한 Outbox 행에 최신 `INDEX`/`REINDEX`/`DELETE` 의도와 증가하는 revision을 유지하며 DELETE가 우선한다.

스케줄러는 `FOR UPDATE SKIP LOCKED`로 선점하고 `attempts`와 하나의 `lease_until`을 사용한다. 외부 HTTP는 트랜잭션 밖에서 호출하며 AI는 `ai_general` 또는 `worklog_index` 큐에 발행한다. Worker는 최신 요청의 유효한 선점인지 시작 API로 확인하고 실행한다. 현재 요청의 `requestId`, `executionId`, 입력 해시를 검증한 결과만 반영하며 중복 결과는 멱등 처리한다.

결과는 AI 측 파일에 보관한 후 콜백한다. 전송이 실패하면 보관된 결과를 재전송하고 성공 또는 stale ACK 후 파일을 삭제한다. 콜백 실패가 AI 재실행을 유발하지는 않는다. API가 영구 4xx(408/429 제외)로 거절한 결과는 재전송하지 않고 파일을 지우며, 해당 작업은 lease 만료 회수로 FAILED 처리된다.

### 실패·재시도

전달 실패나 재시도 가능한 실행 실패는 attempts 한도 내에서 1/2/4/…초의 backoff로 재선점한다. 기본 최대 attempts는 4이며 설정으로 변경할 수 있다. 색인 실행 실패 시 부분 산출물을 보상 삭제한다. 설정 오류 또는 한도 소진은 `FAILED`와 작업별 알림으로 끝난다. 접수 후 시작이나 결과가 오지 않아 단일 lease가 만료된 경우에도 `FAILED`로 전환한다.

작성자는 실패 알림에서 해당 작업을 수동 재시도할 수 있다. 새 `requestId`와 초기화된 attempts를 사용하고 이전 요청과 연결한다. 중복 클릭은 같은 새 요청으로 수렴한다. 색인 삭제 실패도 작성자가 재시도할 수 있다.

### 확인할 한계

timeout 이후에도 이미 전송된 원격 저장소 쓰기가 계속될 수 있으며 프로세스 내부 잠금은 다중 worker를 직렬화하지 않는다. 보상 실패·강제 종료에서는 부분 데이터가 남을 수 있다. provider 호출 횟수와 결과 콜백 동시 횟수는 별도 제한하지 않는다. 실행 결과와 opt-in E2E 범위는 S1 보고서를 따른다.

주요 코드: `WorklogAiTaskOutboxWorker`, `WorklogLightOutboxWorker`, `WorklogAiTaskRetryService`, `InternalWorklogLightIndexService`, `ai/app/task/light_worklog_tasks.py`, `ai/app/service/ai_task_result_delivery.py`.
