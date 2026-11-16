# 업무일지 색인 비동기화 (Celery 전환, Worker 분리)

> **현재 상태:** 완료 (10-27 Celery 전환, 10-29 Worker 분리). 이후 11-12 단순화에서 시작 승인과 결과 통보 방식이 일부 정리됐다. 최신 구조는 [AI 작업 신뢰성](../ai-task-reliability/README.md)을 본다.
> **기간:** 2026-10-17 (측정 계획) → 10-25 (설계) → 10-27 (Celery 전환) → 10-29 (Worker 분리)
> **한 줄 요약:** Spring이 AI 색인 완료까지 HTTP로 기다리던 구조를 "202로 접수만 받고, Celery가 실행한 뒤 콜백으로 결과를 알려주는" 구조로 바꿨다. 이어서 일반 AI 작업과 색인 작업이 서로 기다리지 않도록 Worker를 나눴다.

---

## 1. 배경: 무엇이 문제였나

### 동기 블로킹
- Spring Outbox worker가 AI의 `/index`, `/reindex`, `/delete`를 **동기 HTTP로 호출하고 끝날 때까지 기다렸다.**
- LightRAG 색인(청킹, 임베딩, 엔티티/관계 추출)은 본문 길이에 따라 수십 초에서 수 분이 걸린다.
- 그동안 HTTP 읽기 타임아웃, 커넥션 풀, Outbox 폴러 스레드가 묶였다.

### 전달 실패와 실행 완료가 섞임
- 네트워크가 잠깐 끊기거나 타임아웃이 나면, AI는 실제로 색인 중인데 Spring은 "전달 실패"로 보고 다시 보냈다.
- 같은 문서에 대해 비싼 Gemini 호출과 임베딩이 **중복 실행**됐다.

### 결과 역전
- W1 수정 직후 W2 수정을 하면, 오래 걸린 W1 결과가 W2보다 늦게 도착해서 최신 상태를 과거 값으로 덮을 수 있었다.

### 일반 AI와 색인이 서로 대기 (10-29)
- Celery Worker 하나가 `celery`(일반 AI)와 `worklog_index`(색인) 큐를 모두 처리했다(concurrency=1). 긴 색인이 돌면 요약/태그가 기다렸고, 반대도 마찬가지였다.

## 2. 목표와 완료 기준

- Outbox는 **HTTP 접수까지만** 책임진다. 실행과 재시도는 Celery가 맡는다.
- 접수 성공(202)과 실제 실행 성공을 엄격히 구분한다.
- 최신 요청의 결과만 반영한다. 오래된 실행 결과는 무시한다.
- AI는 Spring DB에 직접 쓰지 않는다.
- 범위 밖 (사용자 결정): 요약/태그/파일 요약 실행 방식 변경, 태그 완료 후 색인 보장, 다중 Worker, 분산 락, CDC, Kafka.

## 3. 선택지와 결정

### 전달 경로
| 대안 | 장단점 | 선택 |
|---|---|---|
| **Spring → AI HTTP 접수 → Redis → Celery** | 기존 내부 HTTP와 인증 재사용. Spring이 Celery 메시지 형식을 몰라도 됨 | **선택** |
| Spring이 Redis에 Celery 메시지 직접 발행 | HTTP 구간이 줄지만 Java와 Celery 프로토콜이 결합됨 | 제외 |
| CDC로 전달 | 폴러를 없앨 수 있지만 실행 전환과 동시에 바꾸면 효과를 분리 측정하기 어려움 | 후속 단계 |

### 결과를 Spring에 어떻게 돌려주나
| 대안 | 문제 | 선택 |
|---|---|---|
| AI가 Spring PostgreSQL에 결과 테이블을 만들어 직접 INSERT | 서비스 간 DB 결합, 마이그레이션 소유권 침해 | **처음 시도했다가 폐기** |
| **AI 로컬 파일 보관함 + HTTP 콜백 + 실패 시 재전송** | DB 경계 유지 | **선택** |

### Worker 분리
| 대안 | 장단점 | 선택 |
|---|---|---|
| 기존 `celery`, `worklog_index` 큐 유지하고 Worker만 분리 | 간단하지만 큐 이름이 목적을 드러내지 않음 | X |
| **새 `ai_general` 큐 + 기존 `worklog_index`, 큐마다 전용 Worker** | 목적이 명확. 기존 `celery` 큐 전환과 롤백 절차가 필요 | **선택** |

**포기한 것 (트레이드오프)**
- Worker 프로세스가 하나 늘어 메모리와 운영 대상이 증가한다.
- Redis, DB, 외부 모델 API 호출 한도는 여전히 공유한다.
- **태그를 기다리지 않고 색인한다** (사용자 결정). 색인이 태그 저장보다 먼저 원본을 읽으면 태그가 다음 재색인까지 빠진다. Worker를 나누면 이 경합이 더 자주 생길 수 있다는 걸 알고 수용했다.

## 4. 구현: 어떻게 동작하나

### 흐름
```text
업무 변경 + Outbox 의도 저장 (같은 트랜잭션, requestId 발급)
  → Spring 폴러가 선점
  → AI에 HTTP 접수 (/index 등, requestId 포함)
  → AI가 Redis worklog_index 큐에 발행 성공 확인 → HTTP 202 + taskId
  → Spring: 전달 완료, 도메인 상태는 PENDING 유지

Celery Worker (concurrency=1)
  → 새 executionId 생성
  → POST /api/internal/worklogs/{id}/light-index/start
      ├─ 최신 requestId + 미완료 → {"execute": true}, RUNNING
      └─ 과거 요청 또는 이미 완료 → {"execute": false}, 실행 생략 (Gemini 호출 절약)
  → 업무 잠금 → 최신 DB 원본 조회 → 색인
  → 결과를 파일로 원자적 보관 ({worklogId}-{requestId}-{executionId}.json)
  → POST .../light-index/result
      ├─ 성공 → 보관 파일 삭제
      └─ 실패 → 파일 유지 → FastAPI lifespan 재전송기가 백오프로 재통보
```

### 핵심 아이디어
- **requestId**: "어떤 변경 요청인가". Spring이 발급. 새 수정이 오면 바뀐다.
- **executionId**: "그 요청의 몇 번째 실행인가". Worker가 실행마다 새로 만든다.
- Spring은 **현재 기록된 requestId와 executionId가 둘 다 맞을 때만** 결과를 반영한다. 같은 실행의 최종 결과는 최초 한 번만 확정한다.
- 색인 요청은 본문 복사본이 아니라 **업무 ID만** 보낸다. Worker가 실행 시점에 최신 원본을 읽으므로 중간 버전을 모두 처리할 필요가 없다.
- 결과 파일은 임시 파일에 쓰고 `fsync`한 뒤 `os.link`로 게시한다. 같은 이름이 있으면 덮지 않는다(최초 결과 보존).
- 내부 콜백은 `X-AI-Internal-Token`을 `MessageDigest.isEqual`로 비교한다(타이밍 공격 방지). 설정값이 비어 있어도 거부한다.
- 색인 상태 컬럼(requestId, executionId, status)은 **JDBC만 소유**하고 JPA 엔티티에 매핑하지 않는다. JPA flush가 색인 상태를 덮어쓰지 않게 하려는 것이다.

### Worker 구성 (10-29)
```text
file.summary, worklog.summary, worklog.tagging, worklog.embedding
  → ai_general 큐 → 일반 AI Worker (concurrency=1)
worklog.light_index, worklog.light_index_result
  → worklog_index 큐 → 색인 Worker (1개, concurrency=1)
```

### 핵심 코드 위치
| 역할 | 파일 |
|---|---|
| 시작 승인, 결과 반영 | `api/.../domain/worklog/service/InternalWorklogLightIndexService.java` |
| 내부 콜백 컨트롤러 | `api/.../domain/worklog/controller/InternalWorklogLightIndexController.java` |
| Celery 작업 | `ai/app/task/light_worklog_tasks.py` |
| 큐 라우팅 | `ai/app/task/celery_app.py` |
| 결과 보관함 | `ai/app/store/worklog_index_result_store.py` |
| 결과 재전송 | `ai/app/service/worklog_index_result_delivery.py` |
| Worker 정의 | `infra/compose.local.yml`, `infra/compose.deploy.yml` |
| 마이그레이션 | `V22`, `V23` (requestId, executionId 컬럼) |

## 5. 결과와 검증

### Celery 전환 (10-27)
- API 관련 테스트 **111건 통과**, `assemble` 성공. 작업 1 기준 API 전체 620건 통과
- Python 드라이버와 Spring 테스트 서버 사이 실제 HTTP `/start`, `/result` E2E 통과
- AI 전용 테스트 84건 통과. Linux 컨테이너 회귀 629 passed, 3 skipped
- Compose 설정 검증 통과

### Worker 분리 (10-29)
- 라우팅 테스트 RED 4건 → GREEN, 관련 33건 통과
- Compose/배포 계약 10건 통과
- 임시 Redis에서 "일반 작업이 느릴 때 색인이 독립적으로 시작되는지" 양방향 검증 통과

### 정성 평가
| 관점 | 효과 |
|---|---|
| 응답성 | 폴러가 수 분 동안 묶이지 않고 수십 ms 만에 202로 풀림 |
| 정합성 | requestId + executionId로 결과 역전 방지 |
| 비용 | `/start`에서 대체된 요청을 미리 걸러 불필요한 Gemini 호출 차단 |
| 경계 | AI가 Spring DB에 직접 접근하지 않음 |

성능 향상 수치(전후 처리량, 지연)는 측정하지 않았다. 10-17에 기준선 측정 계획을 세웠지만 실행 결과 문서는 없다.

## 6. 진행 중 발견한 문제와 바뀐 결정

| 시점 | 내용 |
|---|---|
| 설계 초기 | AI가 Spring DB에 결과 테이블을 직접 쓰는 방식 검토 → 서비스 간 DB 결합이라 폐기. 파일 보관함 + 콜백으로 대체 |
| 작업 1 | 기존 클라이언트가 202 접수 응답을 "색인 완료"로 해석하던 문제를 테스트로 재현 후 수정 |
| 작업 1 | 실제 COMPLETED 콜백이 왔는데 HTTP 접수 응답 유실 예외로 FAILED가 되던 문제 재현 후 수정 (콜백이 202보다 먼저 와도 상태를 되돌리지 않음) |
| 작업 2 검토 | 중간 검토에서 P1 3건 발견: `/start` 실패 시 재시도 없음, `Task.retry` 발행 실패 시 메시지 유실, 서로 다른 이벤트 루프가 DB 풀 공유. 이후 수정과 통합 검증 진행 |
| 응답 형식 | 내부 컨트롤러는 공통 응답 래핑 대상이 아니라서 `/start` 응답을 원시 JSON `{"execute": true}`로 확정 |
| 10-29 | 기존 `celery` 큐 호환 소비를 배포 전에 제거 (사용자 결정) |
| 11-12 | 단순화: 시작 승인은 `execute: true/false`만 반환. 결과 전달 claim, 다중 기한, 실행 token 등을 제거하고 단일 lease로 통일 |

## 7. 한계와 위험

- **단일 Worker 전제:** 색인 잠금은 프로세스 내부 `asyncio.Lock`이다. Worker를 복제하거나 concurrency를 올리면 분산 락이 없어 안전하지 않다.
- **exactly-once 아님:** HTTP 응답이 유실되면 재발행될 수 있다. `/start`의 최신 요청 확인과 결과 멱등 처리로 반영만 한 번 되게 한다.
- **태그 누락 경합:** 위 트레이드오프 참고. 검토에서 HIGH로 지적됐지만 범위 밖으로 남겼다.
- **배포 중 중단:** 컨테이너 교체 중 작업이 끊길 수 있다. 무중단이나 정확히 한 번 실행을 약속하지 않는다.
- **미검증:** 실제 Compose의 Linux prefork Worker가 Redis를 소비하는 동작은 Windows에서 재현하지 못했다.
- **수동 전체 재구축 도구:** Worker를 멈춘 점검 시간에만 실행해야 한다. 같이 돌면 저장소 쓰기가 경합한다.
- **보안:** 내부 토큰을 상수 시간 비교로 검증한다. 로그에는 ID와 상태만 남기고 본문, 토큰, payload는 남기지 않는다.

## 8. 앞으로 개선할 점

1. 태그 콜백 후 재색인 보장 (또는 색인 선후행 계약)
2. 다중 색인 Worker를 위한 분산 락
3. CDC로 폴러 교체 후 같은 조건으로 성능 비교
4. 운영 모니터링: `worklog_index` 큐 적체(`LLEN`), 30분 넘게 PENDING인 업무 조회

## 9. 스스로 점검 질문

- **Q. 202 Accepted를 쓰는 이유는?**
  A. "요청은 받았고 나중에 처리한다"는 뜻이다. 오래 걸리는 작업을 HTTP 연결에 묶지 않고, 접수와 완료를 분리할 수 있다.
- **Q. requestId와 executionId를 둘 다 쓰는 이유는?**
  A. requestId만 있으면 같은 요청이 재실행될 때 이전 실행의 늦은 결과와 구분할 수 없다. executionId로 "이번 실행"을 구분한다.
- **Q. `/start` 단계가 왜 필요한가?**
  A. 큐에 있는 동안 새 수정이 와서 요청이 대체됐을 수 있다. 실행 직전에 확인하면 쓸모없는 LLM 호출을 아낀다.
- **Q. AI가 Spring DB에 직접 쓰면 왜 안 되나?**
  A. 두 서비스가 같은 스키마에 묶여서 한쪽의 마이그레이션이 다른 쪽을 깨뜨릴 수 있다. 데이터 소유권이 흐려진다.
- **Q. 콜백 실패 시 결과를 왜 파일로 보관하나?**
  A. 메모리에만 있으면 Worker가 죽을 때 결과가 사라진다. 파일로 보관하고 ACK 받은 뒤 지우면 유실 없이 재전송할 수 있다.
- **Q. Worker를 나눠도 동시성 문제가 해결되지 않는 이유는?**
  A. Worker 분리는 "서로 기다리지 않게" 할 뿐이다. 색인 Worker를 여러 개로 늘리는 것과는 다르고, 분산 락 문제는 그대로 남는다.

## 10. 원본 문서

| 문서 | 내용 |
|---|---|
| [celery-migration-report-2026-10-27.md](celery-migration-report-2026-10-27.md) | Celery 전환 종합 보고서 (전후 시퀀스 다이어그램, 핵심 코드) |
| [design-celery-2026-10-25.md](design-celery-2026-10-25.md) | Celery 전환 설계 |
| [plan-celery-2026-10-26.md](plan-celery-2026-10-26.md) | Celery 전환 구현 계획 |
| [design-worker-isolation-2026-10-29.md](design-worker-isolation-2026-10-29.md) | Worker 분리 설계 |
| [plan-worker-isolation-2026-10-29.md](plan-worker-isolation-2026-10-29.md) | Worker 분리 구현 계획 |
| `.omx/plans/indexing-baseline-measurement-2026-10-17.md` | 전환 전 기준선 측정 계획 (실행 결과 없음) |
| `.superpowers/sdd/2026-10-26-worklog-index-celery/` | 작업별 진행 기록, 작업 1 보고서, 중간 검토 |
| `.superpowers/sdd/2026-10-29-celery-worker-isolation/progress.md` | Worker 분리 진행 기록 |
