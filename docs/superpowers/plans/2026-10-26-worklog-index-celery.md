# 업무일지 인덱싱 Celery 전환 구현 계획

> 실행 지침: `superpowers:subagent-driven-development`로 작업별 구현과 독립 검토를 진행한다. 사용자의 구현 시작 지시에 따라 현재 기능 브랜치에서 계속 진행하며 별도 커밋·푸시는 하지 않는다.

**목표:** 색인 요청을 HTTP로 접수해 Celery에서 실행하고, 최신 원본 조회와 실제 실행 완료 계약을 유지한다.

**구조:** 기존 Outbox는 전달 실패를 복구한다. AI HTTP는 Redis 발행 확인 후 202를 반환하며, 단일 Celery 작업자가 실제 색인과 실행 재시도를 담당한다. 기존 업무 DB의 요청·실행 식별자로 늦은 결과 통보를 차단한다.

**기술:** Spring Boot, PostgreSQL/Flyway, FastAPI/Pydantic, Celery/Redis, Docker Compose.

**설계:** `docs/superpowers/specs/2026-10-25-worklog-index-celery-design.md`

## 공통 제약

- 인덱싱만 변경한다. 요약·태깅·첨부파일 요약의 실행 계약을 개편하지 않는다.
- 현재 브랜치 `feat/api/outbox-celery`에서 작업한다. 사용자가 이 브랜치에서 구현을 지시했으므로 별도 작업 트리를 만들거나 실행 승인을 반복 요청하지 않는다.
- 새 원본 버전 컬럼, CDC, 분산 잠금, 새 작업 관리 DB, 외부 의존성을 추가하지 않는다.
- 기존 `ai_worker` 하나, `concurrency=1`; 색인·재색인·삭제 직접 실행을 병행하지 않는다.
- 기존 내부 인증을 유지하고 새 시작·결과 API에는 `X-AI-Internal-Token` 검증을 추가한다. 토큰은 기존 `ai.worklog-light-index.internal-token`과 AI의 `ai_internal_token`을 사용한다.
- 모든 신규·변경 API 보조 메서드의 한국어 Javadoc을 확인한다. 프로젝트·영역별 문서 규칙을 적용한다.
- 테스트를 먼저 추가해 실패를 확인하고 최소 구현 후 통과시킨다. 구현자별 커밋 단계는 프로젝트의 커밋 금지 규칙에 따라 생략한다.

## 변경 파일 구성

- API: 기존 Light 요청 DTO·HTTP client·Outbox repository/worker·생성 후처리 service/repository를 변경한다. 색인 실행 상태 저장소와 내부 시작·결과 controller/DTO/service/Docs를 추가하고 Flyway V22로 상태 및 요청 식별자를 보존한다.
- AI: 기존 v4 색인 router/model을 비동기 접수 계약으로 변경한다. 접수 service, 색인 task, worker 수명 주기 실행기, 내부 callback client를 추가한다. 기존 index service는 실제 저장소 변경 책임을 유지한다.
- Infra: 기존 Compose의 작업자 명령·환경 변수를 동기화한다. 운영·측정 문서와 필요 시 작은 측정 도구를 추가한다.
- 각 영역의 기존 테스트와 fixture를 계약 변경에 맞춰 갱신한다.

## 공통 접수·결과 계약

### Spring → AI

기존 `/ai/light/worklogs-v4/index`, `/reindex`, `/delete`를 유지한다.

요청: `{"worklogIds":[123],"requestId":"UUID"}`. 최대 batch 크기는 기존 검증을 유지한다. 일괄 호출의 requestId는 해당 묶음의 각 업무에 함께 적용할 수 있으나 API Outbox 발행은 단건이다.

응답: HTTP 202, `{"items":[{"worklogId":123,"requestId":"UUID","taskId":"문자열"}]}`. 발행 실패는 503이며 202를 반환하지 않는다. Spring은 상태 코드·요청ID·업무ID·누락·중복·작업ID를 검증한다.

작업: `worklog.light_index`, 인자 `(worklog_id:int, request_id:str, operation:str)`, 큐 `worklog_index`. operation은 `INDEX`, `REINDEX`, `DELETE`만 허용한다.

### AI → Spring

기본 경로는 API context-path가 붙는 `/api/internal/worklogs/{worklogId}/light-index`이다. 기존 내부 API 관례에 따라 응답 봉투 없이 원시 JSON을 반환하며 Python client는 최상위 `execute`를 읽는다.

- `POST /start`: `{"requestId":"UUID","executionId":"UUID"}` → `{"execute":true|false}`.
- `POST /result`: `{"requestId":"UUID","executionId":"UUID","status":"COMPLETED|FAILED","error":null|"오류 코드"}` → 기존 빈 응답.
- 두 경로는 `X-AI-Internal-Token`이 없거나 잘못되면 거부하고, 설정 토큰이 비어 있어도 허용하지 않는다.
- 시작 등록은 현재 요청이며 완료되지 않은 경우에만 실행 식별자를 갱신한다. 이전 요청·이미 완료한 요청은 실행 생략을 반환한다. 시작 확인 전에 색인에 진입하지 않는다.
- 결과는 현재 요청·실행에만 반영한다. 이전 실행 결과는 무시하고 정상 응답한다. 동일 실행의 최종 상태는 한 번만 확정한다.

Celery의 재시도마다 새 executionId를 생성한다. 색인 실패 재시도는 최대 5회, 지수 대기 1·2·4·8·16초(상한 60초)로 제한한다. 설정·인증 오류는 반복 실행하지 않는다. 결과 통보 실패는 별도 `worklog.light_index_result` 작업으로 최대 5회 재시도하며 색인을 재실행하지 않는다. 큐 발행 자체가 실패하면 원래 작업을 성공 종료하지 않는다.

## 검토 중점

1. W2 완료 후 W1 재전달: 최신 결과 상태를 되돌리지 않고 과거 원본을 색인하지 않는다. 작업 1·2에서 검증한다.
2. 동일 요청의 실행 A/B 결과 역전: 최신 시작 실행만 유효하다. 작업 1에서 검증한다.
3. 시작/완료가 HTTP 202 응답보다 먼저 도착: 전달 완료가 실행 상태를 되돌리지 않는다. 작업 1에서 검증한다.
4. 부분 발행·부분 색인 실패·결과 통보 장애: 접수 성공과 실행 성공을 혼동하거나 비싼 색인을 불필요하게 반복하지 않는다. 작업 1·2에서 검증한다.
5. worker 재시작·이벤트 루프 교체·실행 시간 초과: 이전 저장 작업이 살아 있는 채로 다음 색인을 시작하지 않는다. 작업 2·3에서 검증한다.

## 작업 1: API의 전달과 실행 완료 분리

**파일:** `api/src/main/java/com/ibank/axwms/domain/worklog/{dto,external,repository,service,controller,event}/`의 관련 클래스, `api/src/main/resources/db/migration/V22__worklog_light_index_async.sql`, `V23__worklog_registration_index_recovery.sql`, 해당 테스트.

**제공 계약:** 위 HTTP 계약과 requestId/executionId 상태 기계. 최신 요청 상태는 기존 업무 DB에 저장한다. Outbox 발행 재시도는 같은 requestId를 유지하고 새 변경만 새 requestId를 만든다.

- [x] 기존 client 테스트에 202 접수 응답 검증 및 200 실행 응답 거부 테스트를 먼저 추가하고 실패 확인.
- [x] 시작·결과 API의 정상/인증실패/형식오류/과거 요청/중복결과/실행결과 역전 테스트를 먼저 추가하고 실패 확인.
- [x] 원본·Outbox·PENDING을 같은 트랜잭션에 저장한다. 수정·삭제 병합과 기존 revision/claim 보호를 유지한다. 생성 후처리 LIGHT_INDEX 요청도 안정적인 requestId와 연결한다.
- [x] 202는 전달만 완료시킨다. 생성 후처리는 실행 완료 전 성공 checkpoint·알림을 만들지 않으며 실행 대기 중 claim을 해제한다. callback과 worker의 행 잠금 순서는 업무일지→생성 후처리 Outbox로 통일한다.
- [x] 생성 후처리의 다른 단계 재시도는 보존하되 색인 FAILED를 Spring이 자동 재발행해 Celery 재시도 한도를 무력화하지 않게 한다. 명시적 수동 재요청은 새 requestId로 허용한다.
- [x] callback이 먼저 도착하거나 요청이 수정·삭제로 대체되어도 현재 요청만 완료시킨다. 기존 알림은 중복 생성하지 않는다.
- [x] 기존 미완료 행을 새 요청 식별자에 연결하는 마이그레이션을 작성한다. 기존 완료 데이터는 다시 색인하지 않는다.
- [x] API 관련 단위·통합 테스트를 실행하고 실제 결과를 보고한다.

검증 명령: `cd api; .\gradlew.bat test --tests "*WorklogLight*" --tests "*WorklogAiPostProcess*" --tests "*InternalWorklogLight*"`.

기대 결과: 신규 상태 기계·접수 계약과 기존 후처리 회귀 테스트 통과. Docker/PostgreSQL 미가용 시 단위 테스트와 컴파일을 우선 증명하고 통합 검증 공백을 기록한다.

## 작업 2: AI 접수·색인 task·자원 수명 주기

**파일:** `ai/app/light/v4/model/worklog_index.py`, `ai/app/light/v4/router/worklog_index.py`, `ai/app/light/v4/service/worklog_index_dispatch_service.py`, `ai/app/task/light_worklog_tasks.py`, `ai/app/client/worklog_index_client.py`, `ai/app/store/worklog_index_result_store.py`, `ai/app/service/worklog_index_result_delivery.py`, `celery_app.py`, 관련 테스트·설정.

**소비 계약:** 작업 1의 `/start`, `/result`와 공통 요청/응답. 기존 `LightWorklogIndexService`가 실제 색인·재색인·삭제를 수행한다.

- [x] 기존 router 테스트에 202·발행실패503·인증거부·UUID 검증·batch 제한을 추가해 실패 확인.
- [x] task 테스트에 시작 생략/시작 실패 시 저장소 미변경, 최신 원본 재조회, 부분 실패 재시도, 소진 시 FAILED 통보, 결과 통보 전용 재시도를 추가해 실패 확인.
- [x] 접수 service가 건별 작업을 지정 큐에 발행하고 계약에 맞는 응답을 만든다. 동기 endpoint에서 직접 색인하지 않는다.
- [x] task는 시작 API 승인 후 기존 index service를 호출한다. 원본이 삭제됐다면 INDEX/REINDEX도 문서 잔존을 정리한다.
- [x] 단일 worker 프로세스의 유지되는 비동기 실행기에서 LightRAG·DB·캐시 자원을 사용하고 종료 시 정리한다. 실행마다 새 이벤트 루프로 같은 adapter를 공유하지 않는다. 기존 다른 AI task까지 불필요하게 재구성하지 않는다.
- [x] 실행 중 시간 초과·취소가 기존 색인 보상 정리를 마친 후 반환하는지 테스트한다. 두 연속 작업과 재초기화를 검증한다.
- [x] 내부 callback client에 기존 토큰과 원시 JSON 응답 처리를 적용한다. 재시도 로그에는 본문·토큰·민감한 예외 문자열을 남기지 않는다.
- [x] task를 Celery에 등록하고 고정된 큐 라우팅·가시성 제한 설정을 필요한 범위에 적용한다.
- [x] 관련 pytest와 전체 테스트를 실행하고 결과를 보고한다.

검증 명령: `cd ai; .\.venv\Scripts\python.exe -m pytest tests/test_light_worklogs_v4_router.py tests/test_light_worklog_v4_index_service.py tests/test_light_worklog_tasks.py -q`, 이후 `python -m pytest tests -q`.

기대 결과: 네트워크 mock으로 계약과 재시도를 검증하고 기존 저장소·색인 회귀 테스트를 보존한다. 실제 모델/저장소 호출 검증 여부를 별도 기록한다.

## 작업 3: 배포·운영·기준선 검증

**파일:** `infra/compose.local.yml`, `infra/compose.deploy.yml`, 필요한 `.env.example` 및 관련 운영 문서, `docs/api/report/`의 기준선 절차/보고서.

**소비 계약:** 작업 2의 `worklog_index` 큐와 기존 기본 큐를 기존 `ai_worker` 하나가 처리한다.

- [x] 배포 명령을 두 환경 모두 `--concurrency=1 --queues=celery,worklog_index`로 고정한다. 기본 큐 이름은 실제 Celery 설정을 확인한다.
- [x] 필요한 토큰·색인 설정과 저장소 접근 설정이 HTTP 서비스와 작업자에 동일하게 전달되는지 확인한다.
- [x] Compose 설정 검증, API 테스트, AI 테스트를 수행한다. Docker가 준비되면 로컬 broker/PostgreSQL 기반 접수·실행·callback 경로를 검증한다.
- [x] 폴링→HTTP→큐→색인→결과 반영 시각을 requestId/실행ID로 연결할 로그를 확인한다. 기존 타이밍 보고서와 구분해 모의 실행 기준선 수집 절차를 제공하고, 실제로 실행한 측정만 수치로 기록한다.
- [x] 재구축 도구의 직접 실행은 작업자 중지 후에만 허용하도록 문서화한다. 전환·롤백·장기 PENDING 탐지·새 요청 재접수 절차와 Redis 유실 한계를 기록한다.
- [x] 전체 변경의 독립 리뷰에서 계약·동시성·보안·누락 caller를 확인하고 필요한 수정 후 검증한다.

기대 결과: 단일 색인 실행 설정과 재현 가능한 기준선 절차가 준비된다. CDC 수치나 실제 AI 성능을 모의 수치로 표현하지 않는다.
