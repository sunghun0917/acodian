# 업무일지 AI 후처리 Outbox

> **현재 상태:** 완료. 이후 2026-11-12 단순화(V25)로 일반 Outbox가 "작업별 Outbox"로 바뀌었다. 최신 구조는 [AI 작업 신뢰성](../ai-task-reliability/README.md) 문서를 본다.
> **기간:** 2026-10-18 (Outbox 도입) → 2026-10-29 (역할 분리)
> **한 줄 요약:** 업무 등록 후 AI 요청을 메모리 비동기(`@Async`)에서 DB Outbox로 바꿔 서버가 죽어도 요청이 사라지지 않게 했고, 이어서 색인 요청을 Light Outbox 한 곳으로 모았다.

---

## 1. 배경: 무엇이 문제였나

### 1단계 (10-18 이전)
- 업무 등록 후 AI 요청(요약, 태그, 색인)을 `@Async`로 커밋 후에 보냈다.
- API 서버가 요청 전에 죽거나 AI 서버가 꺼져 있으면 **요청이 그냥 사라졌다.** 재시도할 기록이 DB에 없었다.

### 2단계 (10-29 이전)
- Outbox를 도입한 뒤에도 색인 요청이 두 군데로 나뉘어 있었다.
  - 최초 색인(INDEX): 일반 AI Outbox가 직접 HTTP 전송
  - 수정 재색인(REINDEX), 삭제(DELETE): Light Outbox가 전송
- 색인의 전송, 대기, 재시도, 최신 요청 관리를 이해하려면 두 경로를 다 봐야 했다.
- AI 쪽 Worker는 이미 일반 AI(`ai_general`)와 색인(`worklog_index`)으로 나뉘어 있었는데, API 쪽 요청 경로는 그 역할과 맞지 않았다.

## 2. 목표와 완료 기준

| 단계 | 목표 |
|---|---|
| 1단계 | 업무 원본과 AI 요청 의도를 같은 트랜잭션에 저장한다. 커밋 후 요청이 실패하면 재시도한다. |
| 2단계 | 색인 요청(INDEX, REINDEX, DELETE)을 Light Outbox 하나로 모은다. 일반 AI Outbox는 파일 요약과 업무 파이프라인만 맡는다. |

2단계의 추가 정책 (사용자 결정):
- 등록 알림은 **최신 색인 결과까지 기다린다.** 등록 직후 수정되면 최신 재색인을 기다린다.
- 알림 전에 업무가 삭제되면 등록 알림을 보내지 않는다.
- 색인 기능 스위치 3개(`enabled`, `update-enabled`, `delete-enabled`)는 모두 제거한다.
- 새 Outbox 테이블, 새 Worker, 새 큐는 만들지 않는다.

## 3. 선택지와 결정

### 1단계: 왜 Outbox인가
원본 문서에는 대안 비교가 없다. 아래 표는 공부용으로 일반적인 비교를 정리한 것이다.

| 방법 | 장점 | 단점 | 선택 |
|---|---|---|---|
| `@Async` 유지 | 단순 | 서버 종료 시 요청 유실, 재시도 기록 없음 | X |
| 외부 MQ (Kafka 등) 도입 | 내구성 높음 | 인프라 추가, DB 저장과 MQ 발행 사이 불일치 문제가 그대로 남음 | X |
| **DB Outbox** | 원본과 요청 의도를 한 트랜잭션에 저장, 추가 인프라 없음 | 폴링 지연(기본 5초), 스케줄러 관리 필요 | **O** |

### 2단계: 색인 요청을 어디로 모을까
| 대안 | 장점 | 비용/위험 | 판단 |
|---|---|---|---|
| A. 두 테이블 유지, 최초 INDEX를 Light Outbox로 이동 | 기존 Worker와 HTTP 계약 재사용, 업무별 최신 의도 보존 | 등록 알림 집계가 색인 상태를 계속 관찰해야 함 | **선택** |
| B. 알림 집계까지 별도 테이블/Worker로 분리 | 일반 Outbox가 색인과 완전히 독립 | 상태 머신 하나 더, 마이그레이션과 경합 증가 | 현재 규모에 과함 |
| C. 현 구조 유지 | 위험 최소 | 핵심 목표 미달성 | X |

**포기한 것 (트레이드오프)**
- 운영자가 환경변수만으로 색인 기능을 개별로 끌 수 없게 됐다.
- 등록 알림은 여전히 최신 색인 상태에 의존한다.
- 기존 Outbox에 행이 남아 있으면 마이그레이션(V24)이 멈춘다. 데이터를 몰래 지우지 않기 위한 선택이다.

참고: Worker를 나눴다고 Outbox도 반드시 나눠야 하는 건 아니다. "전달 책임을 한 곳에 모아 이해하기 쉽게 하자"는 선택이다.

## 4. 구현: 어떻게 동작하나

### 흐름 (2단계 완료 시점)
```text
POST /api/worklogs
  → 업무일지 저장
  → BEFORE_COMMIT 이벤트: 일반 AI Outbox + Light Outbox(INDEX) 기록
  → 같은 트랜잭션 커밋 → HTTP 201

Spring 내부 스케줄러 (5초 poll, 한 번에 최대 20건)
  ├─ 일반 AI Outbox worker → 파일 요약, 본문 요약/태그 요청 → ai_general 큐
  └─ Light Outbox worker → operation 별로 /index, /reindex, /delete 요청 → worklog_index 큐

AI 결과 콜백
  → request ID, execution ID, 현재 Light Outbox 행 확인 (오래된 콜백 무시)
  → 최신 색인 상태가 PENDING/RUNNING이면 등록 알림 대기
  → 끝나면 상태, 알림, Outbox 완료를 한 트랜잭션으로 처리
```

### 핵심 규칙
- **원격 호출 동안 DB 트랜잭션을 잡지 않는다.** 선점(claim token + 10분 lease) 후 트랜잭션 밖에서 HTTP를 보낸다.
- **성공한 단계는 checkpoint로 저장**하고 실패한 단계만 재시도한다. 실패 시 백오프는 최대 256초.
- **업무당 Light Outbox는 한 행.** 새 의도가 오면 덮어쓰되 DELETE가 우선이고 revision이 올라간다.
- **실패 정책이 다르다.** 최초 INDEX 최종 실패는 자동 재시도를 멈추고 수동 재시도를 기다린다. REINDEX/DELETE 실패는 백오프로 계속 재전달한다.

### 핵심 코드 위치 (`api/src/main/java/com/ibank/axwms/domain/worklog/` 기준)
| 역할 | 파일 |
|---|---|
| 등록 트랜잭션에서 Outbox 기록 | `event/WorklogAiPostProcessTrigger.java` |
| Light 전달 (operation 분기) | `service/WorklogLightOutboxWorker.java` |
| Light 의도 병합 (DELETE 우선, revision) | `repository/WorklogLightOutboxRepository.java` |
| 색인 콜백 처리 | `service/InternalWorklogLightIndexService.java` |
| 마이그레이션 | `V21__add_worklog_ai_post_process_outbox.sql`, `V24__separate_worklog_index_outbox.sql` |

현재(11-12 이후) 일반 AI 쪽은 `WorklogAiTaskOutboxWorker`, `WorklogAiTaskRepository`, `V25__worklog_ai_tasks.sql`로 바뀌었다.

## 5. 결과와 검증

### 1단계 (10-18)
- API 자동 테스트: 77개 suite, **611건 통과**, 실패 0
- 실제 서버(Spring + FastAPI + Celery + PG + Redis + Qdrant + Neo4j, mock 없음)에서 장애 복구 확인
  - AI 중단 후 등록 → AI 재기동 시 자동 재시도 성공
  - Celery 중단 → 재기동 후 처리 성공
  - 재시도 대기 중 API 강제 종료 → 재기동 후 처리 성공
  - 수정/삭제도 AI 중단, API kill 후 자동 복구
- 순차 15회 시간 측정 (로컬 Docker, 운영 성능 아님)

| 작업 | API 응답 중앙값 | 후처리 끝까지 중앙값 |
|---|---:|---:|
| 등록 | 128ms | 26.3초 |
| 수정 | 157ms | 27.9초 |
| 삭제 | 56ms | 9.6초 |

### 2단계 (10-29)
- API 전체 **653건 통과**, 실패 0, 건너뜀 7
- 오래된 콜백 차단, 재색인 경합 등 PostgreSQL 통합 테스트 32건 통과
- AI 회귀 47건 통과

## 6. 진행 중 발견한 문제와 바뀐 결정 (날짜순)

| 날짜 | 내용 |
|---|---|
| 10-18 | 실제 기동해 보니 `application.yml` 들여쓰기 오류 4건 발견. 테스트용 동명 YAML이 이걸 가리고 있었다. 실제 YAML을 읽는 회귀 테스트 추가. |
| 10-18 | Light Outbox `poll()`이 `processOne(null)`을 호출할 때 PostgreSQL이 null 파라미터 타입을 몰라 실패 (`could not determine data type of parameter $2`). `CAST(? AS BIGINT)`로 수정. mock 테스트만 있어서 놓쳤던 문제. |
| 10-18 | **등록 시 AI 태그가 검색 색인에 빠지는 문제 발견 (5/5 재현).** 색인이 원본을 읽을 때 태그가 아직 저장 전이었다. 태그 콜백 이후 재색인 예약이 없었다. |
| 10-18 | `COMPLETED`가 "요청 전달 완료"일 뿐 "AI 실제 완료"가 아님을 확인. |
| 10-29 | 역할 분리 설계 검토 1회차에서 반려: INDEX 실패로 멈춘 등록 집계가 새 REINDEX가 와도 다시 깨어나지 않는 문제. 새 요청 활성화 트랜잭션에서 정지를 해제하도록 보완 후 2회차 승인. |
| 11-12 | 단순화로 일반 Outbox를 작업별(SUMMARY, TAG, FILE_SUMMARY)로 나누고 작업별 알림으로 변경. "최신 색인까지 기다리는 등록 알림 집계"는 이때 사라졌다. |

## 7. 한계와 위험

- **exactly-once가 아니다.** 원격 호출 성공 후 checkpoint 기록 전에 서버가 죽으면 같은 요청이 다시 갈 수 있다. AI 쪽은 request/execution ID로 중복 반영을 막는다.
- **프로세스 내부 잠금.** AI 쪽 mutation 락은 `asyncio.Lock`이라 서버를 여러 대로 늘리면 다시 검토해야 한다.
- **무중단 배포 불가.** V24 전환은 업무 쓰기 중지, 큐 드레인, Outbox 비우기가 필요하다. 구버전과 신버전을 섞어 돌릴 수 없다.
- **Outbox를 비워도 업무 상태는 자동으로 정상화되지 않는다.** 버려진 요청은 PENDING/RUNNING으로 남을 수 있다.
- **미검증:** 동시 부하, 첨부파일 S3 실연동, 원격 처리 중 API 종료, 다중 인스턴스. Linux 큐 독립성 테스트 1건은 시간 초과 원인을 못 찾았다.
- **보안:** 삭제 요청의 내부 토큰 검증과 작성자 검증은 기능 스위치를 없앤 뒤에도 유지한다.

## 8. 앞으로 개선할 점

1. 등록 후 태그가 색인에 반영되지 않는 순서 문제 해결 (태그 콜백 후 재색인 예약, 또는 색인 선후행 계약)
2. "요청 전달 완료"와 "AI 실제 완료"를 상태와 알림에서 구분
3. 동시 요청, 큐 적체 측정
4. 다중 인스턴스 대비 잠금 (DB 또는 Redis 기반)

## 9. 스스로 점검 질문

- **Q. 왜 `AFTER_COMMIT`이 아니라 `BEFORE_COMMIT`에서 Outbox를 쓰나?**
  A. 원본과 Outbox가 같은 트랜잭션이어야 둘 중 하나만 저장되는 일이 없다. `AFTER_COMMIT`이면 커밋 후 Outbox 저장이 실패할 때 요청이 사라진다.
- **Q. 원격 HTTP 호출 중에 DB 트랜잭션을 잡지 않는 이유는?**
  A. AI 응답이 느리면 DB 커넥션과 행 잠금을 오래 잡아서 다른 요청을 막는다. 그래서 선점(claim token, lease)만 해두고 트랜잭션 밖에서 호출한다.
- **Q. Outbox가 있으면 중복 요청이 없나?**
  A. 아니다. at-least-once다. 받는 쪽이 request ID, execution ID로 중복을 걸러야 한다.
- **Q. 최초 INDEX 실패는 왜 자동 재시도를 멈추나?**
  A. 계속 실패하는 입력이면 무한 재시도가 된다. 사용자에게 실패를 알리고 수동 재시도로 넘긴다. 반면 REINDEX/DELETE는 최신 상태를 맞춰야 해서 계속 재전달한다.
- **Q. mock 테스트가 통과했는데 왜 실제 서버에서 SQL 오류가 났나?**
  A. Repository를 mock으로 대체해서 실제 SQL의 null 파라미터 경로를 실행하지 않았기 때문이다. 그래서 Testcontainers 기반 PostgreSQL 테스트를 추가했다.

## 10. 원본 문서

| 문서 | 내용 |
|---|---|
| [outbox-introduction-2026-10-18.md](outbox-introduction-2026-10-18.md) | 1단계 구현, 오류 수정, 실서버 검증, 15회 측정 |
| [timing-5x-2026-10-18.csv](timing-5x-2026-10-18.csv) / [json](timing-5x-2026-10-18.json) | 15회 측정 원본 |
| [role-separation-2026-10-29.md](role-separation-2026-10-29.md) | 2단계 역할 분리 결과 |
| `.omx/plans/prd-worklog-outbox-role.md` | 2단계 설계 (대안 비교, 전환/롤백 조건) |
| `.omx/plans/test-spec-worklog-outbox-role.md` | 2단계 테스트 계획 |
| `.omx/plans/architect-worklog-outbox-role-cycle1~2.md`, `critic-...-cycle1~2.md` | 설계 검토 기록 |
| `.omx/context/worklog-outbox-verification-handoff-20261029.md` | 검증 결과와 운영 전환 인계 |
| `.omx/callback-ownership-cleanup-plan.md` | AI의 업무 DB 직접 쓰기 제거, 결과 보관함 방식으로 대체 |
