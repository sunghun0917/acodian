# 업무일지 Outbox 구현·오류 수정·실연동 및 시간 측정 통합 보고서

- 작성일: 2026-10-18 (KST)
- 대상: 현재 작업 트리의 Spring Boot API·FastAPI AI 업무일지 등록 후처리 및 수정·삭제 색인 경로
- 문서 상태: 구현·검증 기록 통합, 미해결 태그 반영 문제 포함
- 통합 원본: 기존 `worklog-create-outbox-validation.md`, `worklog-outbox-5x-timing-2026-10-18.md`

## 1. 결론

**등록 Outbox 전환과 수정·삭제 Outbox의 SQL 오류 수정은 반영됐고, 실제 서버의 전달·복구·본문 재색인·삭제 경로는 확인했다. 그러나 후속 15회 측정에서 등록 5건 모두 AI 태그가 검색 문서·그래프에 반영되지 않아, 전체 후처리 정합성은 완료되지 않았다.**

| 항목 | 상태 | 근거·제약 |
|---|---|---|
| 등록 후처리 Outbox 도입 | 구현 | 업무일지와 후처리 의도를 같은 트랜잭션에 저장, API 재시작 후 재시도 |
| 기존 수정·삭제 Outbox null ID SQL 오류 | 수정 | 실제 PostgreSQL에서 재현 후 `CAST(? AS BIGINT)` 적용 |
| 실제 기동 설정 YAML 오류 | 수정 | 들여쓰기 4개 항목 수정, 원본 YAML 파싱 회귀 테스트 |
| API 자동 회귀 검증 | 통과 | 최종 77개 suite, 611개 테스트, failure/error/skip 각각 0개 |
| 실제 서버의 장애 복구 | 검증 항목 통과 | AI·Celery 중단, 재시도 대기 중 API 강제 종료 및 복구 |
| 등록·수정·삭제 각 5회 측정 | 수행 | 순차 15회, 기본 5초 poll, 실제 API·AI·저장소 사용 |
| 등록 AI 태그의 색인 반영 | **미해결** | 등록 5/5에서 DB 태그는 있으나 전역·팀 snapshot·KG 태그는 비어 있음 |
| `COMPLETED`의 의미 | **주의·미변경** | 현재 후처리 요청 전달 완료이며 모든 Celery 결과 완료를 보장하지 않음 |
| 동시 부하·첨부파일/S3·실행 중 장애 | 미검증 | 아래 한계 참조 |

초기 보고서의 “통과”는 당시 확인한 전달·본문·상태·저장소 존재 조건을 뜻한다. 이후 검증 범위를 DB 태그와 색인 태그의 일치까지 넓혀 누락을 발견했다. 초기 결과를 모든 후처리 결과의 정합성 보장으로 해석하면 안 된다.

## 2. 작업 범위와 구현

### 2.1 등록 후처리를 메모리 비동기 실행에서 DB Outbox로 전환

기존 등록의 `@Async`·커밋 후 실행 경로 대신, 업무일지와 AI 후처리 요청 snapshot을 같은 DB 트랜잭션에 기록하도록 변경했다.

```text
POST /api/worklogs
  → 업무일지 저장
  → BEFORE_COMMIT 이벤트: 업무일지 flush + 등록 Outbox 저장
  → 같은 트랜잭션 커밋 → HTTP 201

Spring 내부 Outbox 스케줄러
  → 기한 도래 작업 선점
  → 단계별 외부 요청 및 성공 checkpoint 기록
      ├─ 첨부파일 요약 접수(설정·파일 조건 충족 시)
      ├─ 본문 요약·태그 파이프라인 접수 → Redis/Celery → AI 결과 콜백
      └─ LightRAG 색인 → JSON 문서/상태 + Qdrant + Neo4j
  → 상태·결과 알림·Outbox 완료/재시도 갱신
```

여기서 “파이프라인 접수”와 “Celery 실행 완료”는 다르다. 위 다이어그램의 분기들은 결과까지 순차 대기한다는 의미가 아니다.

- 원본 DB 저장이 롤백되면 같은 트랜잭션의 Outbox도 남지 않는다. Outbox 저장 실패도 원본 트랜잭션 실패로 전파된다.
- 커밋 이후 AI 요청 실패는 이미 저장한 업무일지를 롤백하지 않는다. 작업을 남기고 재시도한다.
- 작업별 10분 선점 유효기간과 claim token을 사용하며 외부 호출 직전 유효성을 확인한다. 원격 호출 동안 DB 트랜잭션을 유지하지 않는다.
- 성공 단계는 checkpoint로 보존하고 실패한 부분을 재시도한다. 명시적 수동 재요청은 진행 중 snapshot을 덮어쓰지 않고 다음 실행 입력으로 보존한다.
- 등록 worker는 기본 5초 poll, 한 poll 최대 20건, 실패 시 최대 256초 백오프다.
- 완료 시 상태·알림·Outbox 변경을 같은 트랜잭션으로 처리한다. 기존 업무당 최초 결과 알림 정책은 유지했다.
- 이미 삭제된 업무일지·제거된 첨부파일은 외부 처리 대상으로 계속 보내지 않도록 검사한다.

**외부 MQ나 별도 등록 worker 서버를 도입한 것은 아니다.** 등록·수정·삭제 Outbox 스케줄러는 여전히 Spring API 애플리케이션 내부에서 실행한다. Celery·Redis는 파일/본문 요약·태그 등의 AI 작업에 사용하며 LightRAG 색인 자체를 Celery로 옮긴 것이 아니다.

### 2.2 주요 변경 파일

아래 Java 경로의 공통 접두는 `api/src/main/java/com/ibank/axwms/`다.

| 파일·영역 | 변경 내용 |
|---|---|
| `api/src/main/resources/db/migration/V21__add_worklog_ai_post_process_outbox.sql` | 등록 Outbox 테이블과 snapshot·진행 단계·재시도·선점 정보 |
| `domain/worklog/repository/WorklogAiPostProcessOutboxRepository.java` | 저장·선점·checkpoint·수동 재요청·재시도·완료 |
| `domain/worklog/event/WorklogAiPostProcessTrigger.java` | BEFORE_COMMIT에서 flush 후 Outbox 기록 |
| `domain/worklog/service/WorklogAiPostProcessOutboxWorker.java` | DB의 대기 작업을 복구해 실행 |
| `domain/worklog/service/WorklogAiPostProcessService.java` | 외부 요청과 완료 트랜잭션 분리, 성공 단계 재사용 |
| `domain/file/service/FileService.java` | 현재 업무일지에 속한 활성 첨부파일인지 확인 |
| `domain/notification/service/NotificationService.java` | 후처리 결과 알림이 완료 트랜잭션에 참여하도록 변경 |
| `domain/worklog/service/WorklogService.java`, 등록 이벤트 | 새 전달 경로에 맞춘 연동·설명 갱신 |
| `global/config/AsyncConfig.java` | 사용하지 않는 기존 비동기 설정 제거 |
| `domain/worklog/repository/WorklogLightOutboxRepository.java` | 수정·삭제의 null ID 선점 SQL 오류 수정 |
| `api/src/main/resources/application.yml` | 실제 기동을 막던 Light index 설정 들여쓰기 수정 |

등록·수정·삭제 흐름을 설명하는 [HTML 흐름 문서](../../ai/report/rag-flow-guide.html)의 등록 경로도 Outbox 방식으로 갱신했다. 사용자 요청에 따라 해당 그림에서는 파일 요약·태그 상세와 선점/단계 복구 설명을 생략했다. **그림에서 생략한 기능을 실제 코드에서 제거한 것은 아니다.**

## 3. 발견한 오류와 수정

### 3.1 실제 기동 YAML 문법 오류

`application.yml`의 `update-enabled`, `delete-enabled`, `read-timeout`, `internal-token` 4개 항목에 잘못된 들여쓰기가 있었다. 테스트용 동명 YAML이 이 문제를 가리고 있었다.

- 해당 4개 항목의 들여쓰기를 수정했다.
- `AxWmsApplicationTests`에 `YamlPropertySourceLoader`로 실제 `src/main/resources/application.yml`을 읽는 회귀 테스트를 추가했다.
- 수정 전 YAML 파싱 실패와 수정 후 통과를 확인했다.

### 3.2 수정·삭제 Outbox 스케줄러의 null ID 선점 오류

실제 Spring 기동 중 `WorklogLightOutboxWorker.poll()`이 `processOne(null)`을 호출하면서 다음 SQL의 첫 null 파라미터 타입을 PostgreSQL이 판단하지 못했다.

```sql
-- 수정 전
WHERE (? IS NULL OR worklog_id = ?)

-- 수정 후
WHERE (CAST(? AS BIGINT) IS NULL OR worklog_id = ?)
```

실제 오류는 `could not determine data type of parameter $2`였다. 수정·삭제 작업을 스케줄러가 가져오지 못하는 결함이었고, 등록 Outbox는 별도 저장소에서 이미 CAST를 사용하고 있었다.

`WorklogLightOutboxIntegrationTest`에 실제 PostgreSQL 기반 4개 테스트를 추가했다. 수정 전 4개 중 3개에서 동일 오류를 재현했으며, 수정 후 새 테스트 4개와 기존 worker 테스트 2개가 통과했다. 기존 테스트는 Repository mock과 지정 ID만 사용해 null ID의 실제 SQL 경로를 놓쳤다.

## 4. 검증 방식: 자동 테스트와 실제 서버를 구분

| 단계 | 실제로 실행한 것 | 외부 AI 대체 여부 | 증명 범위 |
|---|---|---|---|
| 단위 테스트 | Java 객체·mock | 사용 | 단계 skip·예외·호출 경로 |
| DB 통합 테스트 | Spring 테스트 컨텍스트 + Testcontainers PostgreSQL/Redis | 사용한 테스트 있음 | 실제 SQL·트랜잭션·선점·재시도·상태 저장 |
| 서버 실연동 검증 | 독립 컨테이너의 Spring Boot JAR + Uvicorn FastAPI + Celery + 전체 저장소 | **사용 안 함** | 실제 HTTP·AI 결과·색인·장애 복구 |
| 각 5회 측정 | 동일한 실제 서버 구성, 실제 API에 15회 요청 | **사용 안 함** | 개별 처리 지연과 문서·태그·삭제 정합성 |

DB 통합 테스트 통과만으로 서버 간 연동까지 완료했다고 보지 않았다. 이후 실제 Spring·FastAPI를 다시 띄워 별도로 검증했다.

### 4.1 자동 회귀 검증 기록

- 등록 관련 테스트에서 트랜잭션 롤백, 동시 선점, 선점 만료·이전 소유자 갱신 차단, 성공 단계 보존, 수동 재요청 보존, 삭제된 원본 취소, 알림 실패 시 완료 트랜잭션 롤백 등을 확인했다.
- 수정·삭제 SQL 회귀 테스트는 빈 대기열 null 조회, 지정 ID 조회, 실제 worker의 수정·삭제 전달/완료, 실패·백오프·재시도 성공을 확인했다.
- 테스트용 자동 worker를 명시적으로 비활성화하고 수동 worker만 사용하는 방식으로 경쟁을 방지했으며, 단위/통합 테스트의 재시도 기한은 고정해 실행 속도 의존성을 줄였다. 이는 아래 실제 서버 검증에서 DB 기한을 조작했다는 뜻이 아니다.

| 시점 | 명령·결과 |
|---|---|
| YAML 회귀 추가 후 | `api/gradlew.bat -p api cleanTest test check bootJar --no-daemon --max-workers=2 --console=plain` → 76개 suite, 607개 통과, 5분 32초 |
| 수정·삭제 SQL 회귀 추가 후 최종 | `api/gradlew.bat -p api test check bootJar --no-daemon --max-workers=2 --console=plain` → 77개 suite, 611개 통과, 2분 21초 |

두 전체 실행 모두 failure/error/skip 각각 0개였다. 빌드·`git diff --check` 및 SQL 수정의 독립 코드 리뷰도 통과했다. 시간 측정 과정에서는 제품 코드를 변경하지 않았으며, 위 자동 테스트 수치는 해당 실행 시점의 기록이다.

## 5. 실제 서버 준비와 장애 복구 검증

### 5.1 공통 환경과 요청 경로

이 PC의 Docker Desktop `desktop-linux`에 테스트 전용 네트워크와 컨테이너 7개를 생성했다. 기존 개발 DB·컨테이너는 사용하거나 중단하지 않았다.

| 구성 | 실행 내용 |
|---|---|
| Spring Boot | 현재 소스의 `bootJar`, 컨테이너 내부 `8100` |
| FastAPI | 현재 `ai/app` 소스를 Uvicorn으로 실행, 내부 `8200` |
| Celery | 실제 Redis broker/backend, eager 모드 아님, solo/concurrency 1 |
| PostgreSQL | 테스트용 원본·Outbox DB, Flyway 적용 |
| Redis | 메시지·결과 저장 |
| Qdrant·Neo4j | 실제 벡터·그래프 저장 |

PC 접근은 localhost 임시 포트였고 실행·재기동마다 달라질 수 있었다. 서버들은 테스트 종료 후 정리했으므로 현재 임시 주소로 접속할 수 없다. Gemini는 `gemini-3.5-flash-lite`, 임베딩은 `gemini-embedding-001` 768차원을 실제 호출했다.

테스트 DB에 부서·사용자·팀·소속 fixture를 만들고 `POST /api/auth/login`에서 받은 토큰으로 요청했다. 인증 우회나 AI endpoint만 직접 호출한 검증이 아니었다.

### 5.2 1차: 등록 정상 처리 및 장애 복구

실행 ID: `outbox-live-20261018-044044-cfa56d`

이 실행은 등록 poll만 2초로 줄였고 선점은 기본 10분이었다. 파일 없는 실제 `POST /api/worklogs`의 201 응답부터 원본 DB, Celery 결과, Outbox, 전역·팀 문서 상태, Qdrant 청크, Neo4j 그래프까지 확인했다. DB 기한 조작·수동 완료 처리는 하지 않았다.

| 시나리오 | 장애 중 관측 | 복구 후 결과 |
|---|---|---|
| 정상 등록, ID 1 | 원본·Outbox 저장 | 약 56초, 요약 126자·태그 3개, 색인·Outbox 완료 |
| AI 중단 후 등록, ID 2 | 원본 유지, attempts=1, 파이프라인·색인 실패 기록 | AI 재기동 후 자동 재시도, 요약 146자·태그 3개 |
| Celery 중단 후 등록, ID 3 | Redis 작업 2개 대기, 요약·태그 없음 | 워커 재기동 후 요약 130자·태그 3개 |
| API 강제 종료, ID 4 | AI 장애로 재시도 대기 상태를 만든 뒤 API를 kill, 원본·Outbox 유지 | AI·API 재기동 후 요약 153자·태그 3개, 색인·Outbox 완료 |

등록 4건, Celery 성공 8건, 전역·팀별 각 문서 청크 2개, 최종 등록 Outbox 0행·Redis 대기열 0개였다. 이 단계에서는 생성된 DB 태그와 KG 태그의 일치까지 확인하지 않았으므로, 위 표의 “태그”는 **원본 DB에 저장된 태그**다.

### 5.3 2차: SQL 수정 후 실제 수정·삭제와 복구

실행 ID: `outbox-live-20261018-052450-de2fbe`

수정된 JAR로 실제 서버를 새로 띄웠다. 먼저 등록으로 색인 문서를 만든 뒤 아래 요청을 보냈다. 수정·삭제 worker는 기본 5초 poll이며 하네스에서 `processOne()`을 직접 호출하지 않았다.

| 실제 API 시나리오 | 결과 |
|---|---|
| 정상 `PATCH /api/worklogs/1` | 200, REINDEX Outbox 생성·제거, 전역·팀 문서·snapshot·벡터에서 새 marker 확인 |
| AI 중단 중 재수정 | DB 수정 성공·Outbox 실패 기록, AI 재기동 후 자동 재색인·Outbox 제거 |
| AI 중단 중 `DELETE /api/worklogs/1`, 이후 API kill | soft delete·DELETE 작업 보존, AI·API 재기동 후 자동 삭제·Outbox 제거 |

삭제 전 양쪽 workspace마다 청크 2개·업무 노드 1개였고, 삭제 복구 후 해당 문서·상태·청크·업무 노드는 모두 제거됐다. 실제 Spring PATCH 성공 2회·DELETE 성공 1회와 FastAPI reindex 성공 2회·delete 성공 1회를 로그로 대조했다. 기존 SQL 오류는 없었고 두 Outbox 모두 0행으로 종료했다.

API 강제 종료는 두 장애 검증 모두 **실패 후 재시도 대기 중** 수행했다. 원격 호출 처리 중이나 성공 직후 checkpoint 기록 전 종료까지 검증한 것은 아니다.

<a id="timing"></a>
## 6. 3차: 등록·수정·삭제 각 5회 시간 측정

실행 ID: `outbox-live-20261018-062120-67e5e0`

### 6.1 조건과 시간 정의

- 같은 팀의 파일 없는 업무일지 5개를 만들고 **등록 5회 → 수정 5회 → 삭제 5회** 순서로 실행했다.
- 각 요청의 비동기 후처리 종료·저장소 확인 후 다음 요청을 보냈다. **테스트 요청은 순차였고 서버의 Outbox·스케줄러·Celery를 동기 처리로 바꾼 것은 아니다.**
- 양쪽 Outbox 기본 5초 poll과 첫 요청 초기화 비용을 포함했다. 로그인은 측정 전 수행하고 HTTP 연결을 재사용했다.
- 짧은 유사 기술 업무 본문에 요청별 고유 marker를 넣었다. 외부 AI·저장소·worker mock은 사용하지 않았다.
- 자원 상한: API 1CPU/512MiB(heap256MiB), AI 1CPU/640MiB, Celery 1CPU/512MiB, Neo4j640MiB, Qdrant256MiB, PG256MiB, Redis96MiB.
- 물리 PC와 Docker 자원은 기존 개발 컨테이너와 공유했다. 운영 부하·동시 처리량·p95 측정이 아니다.

| 시간 항목 | 측정 경계 |
|---|---|
| API 응답 | HTTP 요청 시작부터 응답 수신까지, 로그인·사전 fixture 조회 제외 |
| 처리 종료 관측 | 요청 시작부터 DB·Outbox·Celery 조건을 처음 확인한 시점. 1초 poll 관측 지연 약 0~1초 및 조회 비용 포함 |
| 저장소 확인 포함 | 위 시점 이후 문서·상태·Qdrant·Neo4j 확인까지 포함한 전체 시간. API 응답 시간도 포함하므로 두 값을 더하지 않음 |

DB/Redis 조회·저장소 검증과 1.5초 간격 Neo4j marker 관측의 부하도 포함된다. 등록의 태그 불일치는 WARN으로 기록하고 측정을 계속했다. **등록 수치는 본문·핵심 경로 종료 시간이며, 태그까지 정합한 완료 시간은 이번 실행에서 측정할 수 없었다.**

### 6.2 요약

| 작업 | 횟수 | API 평균(ms) | API 중앙값(ms) | API 최소~최대(ms) | 확인 포함 평균(s) | 중앙값(s) | 최소~최대(s) |
|---|---:|---:|---:|---:|---:|---:|---:|
| 등록 | 5 | 239.487 | 127.857 | 101.612~727.438 | 30.992 | 26.337 | 24.340~51.525 |
| 수정 | 5 | 1334.714 | 157.019 | 93.429~4901.940 | 32.821 | 27.946 | 27.678~49.861 |
| 삭제 | 5 | 92.454 | 55.960 | 42.930~258.112 | 9.233 | 9.577 | 6.391~13.869 |

첫 등록은 API 727ms/확인 포함 51.525초, 첫 수정은 API 4.902초/확인 포함 49.861초였다. 느린 표본을 제외하지 않았다. 경로 초기화·외부 모델·로컬 자원 영향을 분리 측정하지 않았으므로 느린 원인은 단정하지 않는다.

### 6.3 개별 15회 결과

| 작업 | 회차 | 업무 ID | API(ms) | 처리 종료 관측(s) | 저장소 검증(s) | 확인 포함(s) | 태그 판정 |
|---|---:|---:|---:|---:|---:|---:|---|
| 등록 | 1 | 1 | 727.438 | 51.073 | 0.453 | 51.525 | 불일치 |
| 등록 | 2 | 2 | 138.202 | 25.027 | 0.144 | 25.171 | 불일치 |
| 등록 | 3 | 3 | 127.857 | 26.204 | 0.132 | 26.337 | 불일치 |
| 등록 | 4 | 4 | 101.612 | 24.207 | 0.133 | 24.340 | 불일치 |
| 등록 | 5 | 5 | 102.326 | 26.421 | 1.165 | 27.586 | 불일치 |
| 수정 | 1 | 1 | 4901.940 | 49.628 | 0.233 | 49.861 | 일치 |
| 수정 | 2 | 2 | 1391.059 | 27.816 | 0.131 | 27.946 | 일치 |
| 수정 | 3 | 3 | 157.019 | 27.698 | 0.129 | 27.827 | 일치 |
| 수정 | 4 | 4 | 130.125 | 27.546 | 0.132 | 27.678 | 일치 |
| 수정 | 5 | 5 | 93.429 | 30.615 | 0.179 | 30.794 | 일치 |
| 삭제 | 1 | 1 | 258.112 | 9.829 | 0.046 | 9.876 | 삭제 확인 |
| 삭제 | 2 | 2 | 62.334 | 13.836 | 0.033 | 13.869 | 삭제 확인 |
| 삭제 | 3 | 3 | 42.930 | 9.526 | 0.051 | 9.577 | 삭제 확인 |
| 삭제 | 4 | 4 | 42.932 | 6.364 | 0.027 | 6.391 | 삭제 확인 |
| 삭제 | 5 | 5 | 55.960 | 6.410 | 0.041 | 6.451 | 삭제 확인 |

### 6.4 측정 중 정합성 검사

- Spring의 POST 201/PATCH 200/DELETE 200 각각 5회와 FastAPI index/reindex/delete HTTP 200 각각 5회를 대조했다.
- 15건 모두 실제 Outbox 대기 상태와 최종 제거를 관측했다. 관측된 재시도 attempts는 모두 0, 기존 SQL 오류는 없었다.
- 등록 5건에서 Celery 요약·태그 각 1건씩 총 10건 SUCCESS와 원본 DB 저장을 확인했다. 수정·삭제에는 추가 Celery 작업이 생기지 않았다.
- 전역·팀별 문서 제목·본문·snapshot, Qdrant 문서 ID·file_path·marker를 확인했다. Neo4j 제목 marker 20개를 관측했고 수정 description에는 이전 marker가 남지 않았다.
- **수정 5건**은 DB 태그·전역/팀 snapshot 태그·그래프 태그가 정확히 일치했다. 기존 계약대로 요약·태그를 재생성하지 않고 재색인하며 원래 요약·태그는 유지됐다.
- **삭제 5건**은 soft delete 및 색인 제거를 확인했다. 최종 양 workspace의 Qdrant chunks/entities/relationships 모두 0개, Neo4j 전체 노드 0개였다.
- 최종 등록·수정삭제 Outbox 0행, 활성 업무일지 0건(원본 5건 soft delete), Redis 대기열 0개였다.

<a id="open-issues"></a>
## 7. 미해결 문제와 원인

### 7.1 등록 후 AI 태그 색인 누락 — 5/5 재현

| 등록 회차 | DB 태그 ID | 전역/팀 snapshot·KG 태그 |
|---|---|---|
| 1 | [1, 2, 3] | [] / [] |
| 2 | [4, 5] | [] / [] |
| 3 | [6, 7, 8] | [] / [] |
| 4 | [1, 3] | [] / [] |
| 5 | [6, 8, 11] | [] / [] |

다음 순서가 가능하고 이번 실행에서 해당 불일치가 관측됐다.

```text
1. Spring → FastAPI에 요약·태그 파이프라인 요청
2. FastAPI → Celery에 작업 접수 후 즉시 응답
3. Spring → LightRAG 색인 요청
4. 색인용 DB 원본 조회: 태그가 아직 없어 []를 읽음
5. Celery 태그 생성 완료 → 콜백으로 원본 DB에 태그 저장
6. 색인은 앞에서 읽은 []를 사용해 snapshot·그래프를 구성
7. 태그 콜백 이후 추가 재색인 예약 없음
```

색인 전체가 태그 생성보다 먼저 끝났다는 뜻은 아니다. **색인용 원본을 먼저 읽은 것**이 문제이며, 색인 완료 전에 DB 태그가 생겨도 이미 읽은 입력이 자동 갱신되지 않는다.

| 근거 | 확인 내용 |
|---|---|
| `api/src/main/java/com/ibank/axwms/domain/worklog/service/WorklogAiPostProcessService.java:64-65` | 파이프라인 요청 반환 후 색인 실행 |
| `ai/app/service/pipeline_service.py:11-21` | Celery 요약·태그 `.delay()` 접수 후 반환 |
| `ai/app/light/v4/service/worklog_index_service.py:70-84` | 색인 전에 원본 snapshot 준비 |
| `ai/app/task/tagging_tasks.py:47-63` | 태그 콜백 후 task 성공 |
| `api/src/main/java/com/ibank/axwms/domain/tag/service/InternalTagAiCallbackService.java:43-55` | DB 태그 연결 저장, 재색인 예약 없음 |

독립 원인 분석에서도 비동기 순서 경쟁과 재색인 예약 누락으로 판단했다. 이후 수행한 사용자 수정의 재색인에서는 DB 태그가 색인에 반영됐다. **태그 생성 실패가 아니라 생성 이후 검색용 데이터에 반영되지 않는 문제**이며 아직 수정하지 않았다.

### 7.2 `COMPLETED`는 모든 비동기 결과 완료를 의미하지 않음

Celery 중단 검증에서 요약·태그가 없고 Redis에 2개 작업이 대기하는 중에도 `ai_processing_status=COMPLETED`, 등록 Outbox 제거 상태를 관측했다. 현재 `WorklogAiPostProcessService.finish()`와 `WorklogAiProcessingStatusService.completeAiProcessing()`은 후처리 요청 전달 완료를 기준으로 동작한다.

따라서 Outbox 제거·`COMPLETED`·HTTP 200만으로 요약·태그 실행 및 최종 색인 정합성이 보장되지는 않는다. 이 의미와 후속 결과 추적 방식은 변경하지 않았다.

### 7.3 확장·내구성 한계

- 원격 성공과 checkpoint 기록 사이에 종료되면 요청이 재전송될 수 있다. 관측 범위에서 중복이 없었다는 사실과 exactly-once 보장은 다르다.
- 긴 원격 요청이 선점 기한을 넘는 경우, 오래된 AI 콜백과 최신 수정의 경쟁, 독립 프로세스·다중 API/AI 인스턴스의 동시 처리까지 실연동 검증하지 않았다.
- 현재 AI mutation 락은 프로세스 내부 `asyncio.Lock`이다. 서버를 늘릴 때 별도 동시성 경계 검토가 필요하다.

## 8. LightRAG JSON 외부 DB 전환 검토 — 미구현

현재 문서·청크·LLM 캐시·처리 상태는 기본 JSON 저장소, 벡터는 Qdrant, 그래프는 Neo4j다. 원본 업무일지 DB와 검색용 파생 데이터는 별개다.

운영·공유 저장을 위한 선택지로 기존 PostgreSQL 인프라의 전용 DB·계정과 `PGKVStorage`·`PGDocStatusStorage`를 검토했고, Qdrant·Neo4j를 유지하는 방향을 설명했다. LightRAG 1.4.10의 [공식 설정](https://github.com/HKUDS/LightRAG/blob/v1.4.10/env.example)을 확인했지만 **설정·코드·데이터 이전은 하지 않았다.**

JSON을 DB로 옮기는 것만으로 위 태그 순서 문제, 색인 락, 프로세스 간 동시성 문제가 해결되지는 않는다. 전환 시에는 문서 ID·상태·기존 Qdrant/Neo4j 참조의 이전 정합성을 별도로 검증해야 한다.

## 9. 후속 작업과 미검증 범위

후속 우선순위는 다음과 같다. 아래 항목은 완료된 구현이 아니다.

1. 태그 콜백 이후의 신뢰 가능한 재색인 예약 또는 색인 선후행 계약을 설계·수정하고, 등록만으로 태그 정합성이 맞는지 재검증.
2. 요청 전달 완료와 요약·태그·색인 결과 완료의 상태 의미·추적 방식 구분.
3. 그 후 동시 요청·큐 적체·락 경합 측정. 이번 순차 결과를 동시 처리 성능으로 사용하지 않음.
4. 첨부파일/S3, 원격 처리 중 API 종료, 성공 직후 checkpoint 전 종료, 선점 만료, 다중 인스턴스 경쟁 검증.

Redis 전원·디스크 손실의 메시지 내구성, 장문·대용량 파일, 검색 답변 품질, 운영 부하·처리량은 이번 작업에서 검증하지 않았다. 파일 관련 단위 테스트가 있다는 사실은 S3 실연동 완료를 뜻하지 않는다.

## 10. 증거·산출물과 현재 상태

| 증거 | 위치 |
|---|---|
| 등록 장애 복구 1차 | `graphify-out/outbox-live-20261018-044044-cfa56d/` |
| 수정·삭제 실연동 2차 | `graphify-out/outbox-live-20261018-052450-de2fbe/` |
| 15회 시간 측정 3차 | `graphify-out/outbox-live-20261018-062120-67e5e0/` |
| 비밀정보 없는 측정 JSON | [원시 결과 JSON](worklog-outbox-5x-timing-2026-10-18.json) |
| 비밀정보 없는 측정 CSV | [원시 결과 CSV](worklog-outbox-5x-timing-2026-10-18.csv) |
| 등록 하네스 | `graphify-out/.outbox_live_validation.py` |
| 수정·삭제 하네스 | `graphify-out/.light_outbox_live_validation.py` |
| 시간 측정·추가 그래프 관측 | `graphify-out/.outbox_5x_timing.py`, `graphify-out/.outbox_graph_monitor.py` |

2·3차 실행 jar SHA-256은 `5898e145a02c6e31060b66544163d68bfa7c06916fb6c76a02391e205de20639`다. Git HEAD만으로 미커밋 구현을 식별하지 않고 실제 실행 jar 지문을 보존했다.

원시 실행 폴더와 하네스는 Git 제외 경로다. 환경 파일에는 자격증명이 있으므로 공유하지 않는다. JSON의 `core_pass`는 본문·전달·삭제 핵심 경로의 판정이며 태그 정합성 성공을 의미하지 않는다.

각 실행의 테스트 전용 컨테이너 7개와 네트워크는 정리했고 기존 개발 컨테이너 5개는 건드리지 않았다. 제품 코드 추가 수정·기존 개발 서버 재기동·커밋은 이번 보고서 통합 작업에서 하지 않았다.

기존 [2026-10-06 RAG 캐시·증분 재색인 보고서](../../ai/report/worklog-rag-cache-and-reindex-report.md)는 다른 시점·범위의 기록이므로 본문을 합치지 않고 참고 문서로 유지한다. 해당 문서의 1,000건 캐시 벤치마크와 이번 순차 15회 처리 시간은 서로 다른 실험이다.
