# 업무일지 AI 후처리 Outbox 역할 분리 보고서

- **대상:** 업무일지 등록·수정·삭제에 따른 일반 AI 후처리, LightRAG 색인 요청과 결과 집계.
- **분석 범위:** API의 업무일지 서비스·Outbox·콜백, Flyway V24, 관련 테스트, 배포 환경변수. UI 변경과 별도의 문서 편집은 대상에서 제외한다.
- **전후 기준:** 변경 전은 `HEAD` `3e58166d`, 변경 후는 보고서 작성 시점의 미커밋 작업 트리. 신규 `api/src/main/resources/db/migration/V24__separate_worklog_index_outbox.sql`과 `api/src/test/java/com/ibank/axwms/domain/worklog/event/WorklogAiPostProcessTriggerTest.java`는 추적 전 파일이므로 일반 `git diff` 통계에 포함되지 않는다.
- **대화 범위:** 현재 접근 가능한 대화에서 확인한 사용자 요구·정책 결정·실행 결과. 다른 세션의 대화나 운영 시스템은 확인하지 않았다.
- **결론의 한계:** 소스에는 변경이 반영돼 있지만 운영 DB 전환, 배포 및 커밋은 수행하지 않았다. Linux 큐 독립성 테스트의 시간 초과는 원인을 확인하지 못했다.

## 1. 환경

- API는 Java 21·Spring Boot, PostgreSQL, Flyway를 사용한다. Java 21은 `api/build.gradle`의 toolchain에 선언돼 있고 로컬 실행 환경에서도 확인했다. 선언된 버전과 실제 운영 버전은 구별해야 한다.
- AI는 FastAPI·Celery 구조다. `ai_general`은 일반 AI 작업, `worklog_index`는 업무일지 색인 작업 큐다. API의 Outbox worker가 AI에 HTTP 접수하고, AI가 Redis/Celery를 거쳐 실행 결과를 API에 통보한다. 근거: `ai/app/task/celery_app.py:27-30`, `infra/compose.deploy.yml`의 일반·색인 worker 정의.
- 변경 전 API 색인 설정은 `enabled`, `update-enabled`, `delete-enabled`를 제공했다. 변경 후 이 세 설정과 대응 배포 환경변수를 제거했다. 색인 장애 시 개별 기능 스위치로 끄는 방식은 더 이상 사용할 수 없다. 근거: `git diff -- api/src/main/resources/application.yml infra/compose.deploy.yml infra/.env.example`.
- 삭제 요청의 내부 토큰 준비 확인은 유지된다. 이 보고서는 토큰 값이나 운영 설정값을 수집하지 않았다. 근거: `api/src/main/java/com/ibank/axwms/domain/worklog/service/WorklogService.java:382-389`.

## 2. 문제사항

**확인 사실:** 변경 전에는 최초 색인을 등록 후처리 Outbox가 직접 전송했고, 수정·삭제 색인은 Light Outbox가 전송했다. 전송·접수 대기·재시도와 최신 요청 관리를 이해하려면 두 경로를 살펴야 했다. 근거: `3e58166d:api/src/main/java/com/ibank/axwms/domain/worklog/service/WorklogAiPostProcessService.java`의 `requestLightIndex`, `3e58166d:api/src/main/java/com/ibank/axwms/domain/worklog/service/WorklogLightOutboxWorker.java`.

**대화상 문제 제기:** 사용자는 Worker가 이미 일반 AI와 색인으로 나뉜 상태에서, 색인 요청 경로도 역할에 맞춰 정리하고 싶다고 설명했다. Outbox 테이블을 무조건 하나로 합치자는 뜻은 아니었다.

**영향과 한계:** 책임 분리와 코드 이해가 어려운 구조적 문제는 확인된다. 이 사실만으로 실제 운영 장애나 중복 요청이 발생했다고 단정할 근거는 없다. 변경 전 기본 설정은 수정 재색인 등을 끌 수 있었으나 과거 운영 환경의 실제 값은 확인하지 못했다.

## 3. 해결아이디어

대화에서 비교한 방향은 현행 구조를 유지하거나 최초 INDEX를 기존 Light Outbox로 옮기는 것이었다. 사용자는 후자를 선택해 다음 책임을 명확히 했다.

- 일반 AI Outbox: 파일 요약과 업무 파이프라인 요청 및 등록 결과 집계.
- Light Outbox: 등록 최초 `INDEX`, 수정 `REINDEX`, 삭제 `DELETE` 전달.
- 등록 알림: 색인 결과까지 대기. 등록 직후 수정되면 최신 재색인을 기다리고, 알림 전에 업무가 삭제되면 등록 알림을 보내지 않음.
- 후속 결정: 색인 관련 세 기능 스위치는 모두 제거. 일반 AI의 실제 Worker 완료까지 기다리도록 바꾸는 것은 이번 범위가 아니다.

Worker 분리 자체가 Outbox 분리를 기술적으로 강제하는 것은 아니다. 전달 책임을 한곳으로 모아 흐름을 이해·관리하려는 선택이다. 이는 대화에서 명확히 한 요구와 코드 구조에 근거한 해석이다.

## 4. 해결방식

1. 등록의 `BEFORE_COMMIT` 이벤트에서 일반 AI Outbox와 최초 `INDEX` Light Outbox를 기록한다. 업무 원본과 두 의도는 같은 DB 트랜잭션 경계를 사용한다. 근거: `api/src/main/java/com/ibank/axwms/domain/worklog/event/WorklogAiPostProcessTrigger.java:23-28`.
2. Light Outbox worker는 operation을 구분해 기존 `/index`, `/reindex`, `/delete` 요청을 호출한다. 일반 AI 서비스에서는 최초 색인 HTTP 직접 호출을 제거했다. 근거: `api/src/main/java/com/ibank/axwms/domain/worklog/service/WorklogLightOutboxWorker.java:38-48`, `git diff`의 `WorklogAiPostProcessService`.
3. 일반 AI Outbox는 일반 요청 접수 결과와 최신 `tb_worklog.light_index_status`를 집계한다. `PENDING`·`RUNNING` 동안에는 일반 AI 실패 이력도 저장하고 성공·실패 알림을 기다린다. 근거: `api/src/main/java/com/ibank/axwms/domain/worklog/service/WorklogAiPostProcessService.java:67-110`, `api/src/main/java/com/ibank/axwms/domain/worklog/repository/WorklogAiPostProcessOutboxRepository.java:165-201`.
4. 결과 콜백은 request ID, execution ID 및 현재 Light Outbox 행을 확인한다. 최초 INDEX의 최종 실패는 terminal 상태로 자동 선점을 중단하고, REINDEX·DELETE 실패는 백오프로 재전달한다. 새 INDEX·REINDEX 의도는 등록 집계를 재개할 수 있다. 근거: `api/src/main/java/com/ibank/axwms/domain/worklog/service/InternalWorklogLightIndexService.java:21-73`, `api/src/main/java/com/ibank/axwms/domain/worklog/repository/WorklogLightOutboxRepository.java:22-49,96-117`.
5. 신규 V24는 두 Outbox가 비어 있는지 확인한 뒤 Light Outbox에 INDEX와 terminal 상태를 추가하고 일반 Outbox에서 기존 색인 전달 열을 제거한다. 자동 삭제는 하지 않는다. 근거: `api/src/main/resources/db/migration/V24__separate_worklog_index_outbox.sql`.

## 5. 핵심코드

### 등록 트랜잭션: 최초 INDEX 의도 기록

현재 작업 트리의 `WorklogAiPostProcessTrigger.onCommit` 발췌:

```java
@TransactionalEventListener(phase = TransactionPhase.BEFORE_COMMIT)
public void onCommit(WorklogAiPostProcessRequestedEvent event) {
    worklogRepository.flush();
    if (repository.enqueue(event)) {
        lightOutboxRepository.enqueue(event.worklogId(), "INDEX");
    }
}
```

일반 후처리 행이 새로 활성화될 때 최초 색인 의도를 Light Outbox에 기록한다. 기존 행이 있는 수동 재요청은 별도 승격 경로를 거친다. 근거: `api/src/main/java/com/ibank/axwms/domain/worklog/event/WorklogAiPostProcessTrigger.java:23-28`, `api/src/main/java/com/ibank/axwms/domain/worklog/service/WorklogAiPostProcessService.java:76-79`.

### 최신 색인 상태 대기

현재 작업 트리의 `WorklogAiPostProcessService.finish` 발췌:

```java
String lightState = outboxRepository.lightState(claim);
if ("PENDING".equals(lightState) || "RUNNING".equals(lightState)) {
    outboxRepository.awaitLightResult(claim, failedStages);
    return;
}
```

일반 AI 접수 실패가 있더라도 최신 색인이 진행 중이면 실패 목록을 보존하고 알림 집계를 기다린다. 근거: `api/src/main/java/com/ibank/axwms/domain/worklog/service/WorklogAiPostProcessService.java:81-95`.

### 색인 전달 단일화

설명용 발췌이며 DTO 인자는 생략했다.

```java
switch (claim.operation()) {
    case "INDEX" -> client.requestIndex(...);
    case "REINDEX" -> client.requestReindex(...);
    case "DELETE" -> client.requestDelete(...);
    default -> throw new IllegalStateException(...);
}
```

최초 색인과 수정·삭제 색인이 같은 worker의 명시적 operation 분기를 거친다. 근거: `api/src/main/java/com/ibank/axwms/domain/worklog/service/WorklogLightOutboxWorker.java:43-47`.

## 6. 전후 해당 서비스 로직

같은 시나리오인 **업무 등록 직후 본문 수정**을 비교한다.

| 단계 | 변경 전 `3e58166d` | 현재 작업 트리 |
|---|---|---|
| 진입·검증 | 업무 등록 권한·팀 등 검증 후 원본과 등록 후처리 의도 기록 | 기존 검증 후 원본, 일반 AI 의도, Light INDEX 의도 기록 |
| 일반 AI | 등록 후처리 worker가 파일 요약·업무 파이프라인 요청 | 일반 AI Outbox worker가 동일 책임을 담당 |
| 최초 색인 | 등록 후처리 서비스가 AI 색인 API에 직접 요청 | Light Outbox worker가 `INDEX` 요청 |
| 수정 | 색인 대상이 실제 변경되고 설정이 활성화되면 `REINDEX` 의도 기록 | 설정 스위치 없이 색인 대상의 실제 변경 시 `REINDEX` 의도 기록 |
| 동시성 | 요청·실행 ID와 Outbox claim으로 과거 작업을 구분 | 여기에 현재 Light Outbox 행 일치 검증을 추가하고 최신 색인 상태로 등록 결과 집계 |
| 응답·실패 | 등록 응답은 비동기 후처리를 기다리지 않음. 일부 일반 AI 실패는 색인 진행 중에도 실패 알림 가능 | 등록 응답은 계속 비동기. 성공·실패 알림 모두 최신 색인 종료를 기다림 |
| 삭제 | 기능 스위치와 내부 토큰이 삭제 가능 여부에 영향 | 기능 스위치는 제거하되 내부 토큰·작성자 검증은 유지 |

변경 전 근거는 `3e58166d`의 `WorklogService`, `WorklogAiPostProcessService`, `WorklogLightOutboxWorker`이며, 변경 후 근거는 해당 현재 파일과 `InternalWorklogLightIndexService`다. 업무 원본 DB와 외부 파일 저장소가 하나의 원자적 트랜잭션이라는 뜻은 아니다.

## 7. 평가 및 결과

| 요구사항 | 판정 | 근거 |
|---|---|---|
| 최초 색인을 Light Outbox로 이동 | 충족 | 등록 이벤트의 INDEX enqueue 및 worker operation 분기 |
| 수정·삭제 색인 전달도 같은 Outbox 사용 | 충족 | `WorklogLightOutboxWorker.java:43-47` |
| 최신 색인 대기, 이전 콜백 차단 | 구현 및 관련 테스트 확인 | `InternalWorklogLightIndexService.java:21-46`; `WorklogAiPostProcessExecutionIntegrationTest`의 과거 요청·경합 테스트 |
| 삭제된 업무의 등록 알림 생략 | 구현 및 관련 테스트 확인 | 일반 등록 집계의 업무 생존 검사와 삭제 시나리오 |
| 기능 스위치 제거, 내부 토큰 유지 | 충족 | 설정 diff 및 `WorklogService.java:382-389` |
| 운영 환경의 기존 Outbox 전환 | 미실행 | V24 사전조건 확인만 구현; 운영 삭제·배포 미실행 |
| Linux 큐 독립성 | 미해결 | 격리 Redis 환경에서 해당 테스트 두 차례 시간 초과 |

### 대화 중 실행된 검증

다음 결과는 현재 대화에 기록된 이전 실행 결과다. 이 보고서 작성 과정에서는 테스트를 다시 실행하지 않았다.

- `api/`에서 `./gradlew.bat test`: API 전체 **653건, 실패 0건, 건너뜀 7건**. Python 콜백 E2E는 별도 활성화 실행에서 성공했다.
- PostgreSQL 기반 `WorklogAiPostProcessExecutionIntegrationTest`: 양방향 등록 실패 집계·재색인 경합을 포함한 **32건 통과**. 테스트 자체의 경합 시나리오는 `api/src/test/java/com/ibank/axwms/domain/worklog/service/WorklogAiPostProcessExecutionIntegrationTest.java:143-220`에서 확인된다.
- AI 회귀 테스트: Windows **47건 통과·4건 환경 조건으로 건너뜀**. 격리 Redis를 사용한 Windows 큐 독립성 테스트는 별도 통과했다.
- Linux 격리 환경: Worker 장애 복구 테스트 **3건 통과**, 큐 독립성 테스트 **1건 두 차례 시간 초과**. 실패 원인을 환경과 실제 격리 문제 중 어느 쪽으로도 확정하지 않았다.
- V24는 **임시 PostgreSQL 최소 스키마**에서 기존 Outbox 행 존재 시 중단·보존, 빈 경우 적용, operation 허용 조건을 확인했다. 실제 운영 데이터와 전체 운영 전환 리허설을 검증한 것은 아니다.

## 8. 트레이드오프

| 얻은 점 | 비용 또는 제약 |
|---|---|
| 색인 전달·재전달의 책임이 Light Outbox 한곳으로 모인다 | 등록 결과 집계는 최신 색인 상태에 계속 의존한다 |
| 색인 기능 스위치의 배포 설정 차이로 수정·삭제 요청이 비활성화되는 구조를 없앤다 | 운영자가 환경변수만으로 색인 기능을 개별 중단할 수 없다 |
| 기존 Outbox 행을 V24가 임의 삭제하지 않는다 | 기존 행이 있으면 사전 정리 없이는 마이그레이션이 중단된다 |
| 최신 요청·실행과 Outbox 행을 대조해 늦은 콜백의 권위를 제한한다 | 이미 시작된 기존 Worker와 Redis 메시지·AI 결과 파일은 전환 절차에서 별도로 고려해야 한다 |

위 효과는 구조에 근거한 평가다. 성능 향상률·처리량·비용 절감은 측정하지 않았다.

## 9. 추가 개선점

### 배포 전 확인 필요

1. **Linux 큐 독립성 시간 초과 조사:** 격리 Redis와 같은 Celery 테스트 조건에서 실패를 재현해 테스트 환경 문제인지 실제 Worker 격리 문제인지 구분한다.
2. **운영 전환 계획 확정:** 대상 DB·기존 Outbox 행과 `PENDING`/`RUNNING` 업무 상태, 실행 중 Worker·Redis 대기 작업의 영향을 확인한다. 두 Outbox를 실제로 비우는 작업과 배포는 환경을 특정한 별도 승인 이후에만 수행한다.
3. **전환·롤백 리허설:** 구·신 버전 혼재를 피하고 기존 Worker 종료, 일반 큐 드레인, 잔여 결과 콜백을 포함해 테스트한다. Outbox를 비워도 기존 업무 상태가 자동 정상화되지는 않는다.

### 선택적 개선

일반 AI 접수 성공과 실제 파일 요약·태그 Worker 완료의 의미를 사용자 상태와 알림에서 구분할지 검토할 수 있다. 이는 별도 정책·구현이 필요한 제안이며 이번 리팩토링에 포함되지 않았다.

## 10. 근거와 한계

- 전후 비교: `git diff`와 `git show 3e58166d:<path>`, 현재 작업 트리. 신규 V24와 신규 테스트는 추적 전 파일로 별도 확인했다.
- 주요 코드: `WorklogAiPostProcessTrigger`, `WorklogAiPostProcessService`, `WorklogLightOutboxRepository`, `WorklogLightOutboxWorker`, `InternalWorklogLightIndexService`, `WorklogService`.
- 테스트: `WorklogAiPostProcessExecutionIntegrationTest`, `WorklogLightOutboxIntegrationTest`, `WorklogLightCallbackE2ETest`, AI Celery·결과 전달 테스트. 테스트 파일을 읽은 사실과 위에 명시된 과거 실행 결과를 구분했다.
- 대화: 사용자는 Worker 역할 기준 Outbox 분리와 최신 색인까지 알림 대기, 수정 후 최신 재색인, 삭제 시 무알림, 세 설정 제거를 각각 명시했다. 기존 Outbox 데이터 보존이 필수는 아니라고 했으나 실제 환경 삭제·배포는 승인하지 않았다.
- 운영 환경, 과거 운영 재색인 여부, 실제 전환·롤백 결과는 확인하지 않았다. Linux 큐 독립성 테스트 실패와 OMX 목표의 `usageLimited`로 인한 최종 체크포인트 미기록이 남아 있다.