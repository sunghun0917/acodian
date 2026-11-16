# 업무일지 선행 관계의 낙관적 동시성 제어 구현 보고서

- **대상:** AX-WMS API, `portfolio.pdf` 5번 「READ COMMITTED 환경에서 발생한 동시성 정합성 문제 해결」
- **작성일:** 2026-10-07
- **상태:** 로컬 구현 및 테스트 검증 완료, 미커밋

## 1. 문제와 적용 범위

업무일지 A에 선행 업무 B를 연결할 때, B를 미완료로 읽은 직후 다른 요청이 B를 완료하면 기존 검증 결과가 낡아진다. PostgreSQL `READ COMMITTED`에서 일반 조회만으로는 조회 이후의 변경을 막지 못한다([PostgreSQL 공식 문서](https://www.postgresql.org/docs/current/transaction-iso.html)). 기존 API는 선행 **후보 검색**에서 `COMPLETED`를 제외했지만, 실제 등록 경로는 동일 팀·미삭제·자기참조·순환만 확인했다. 따라서 클라이언트가 후보 검색을 거치지 않거나 두 요청이 겹치면 완료된 B가 새 선행 업무로 저장될 수 있었다.

이번 변경은 `POST /worklogs`의 신규 연결과 `PATCH /worklogs/{worklogId}`에서 **새로 추가되는 연결(`toAdd`)**에 적용한다. 이미 저장된 연결의 유지·제거, 완료된 선행 업무를 포함한 과거 데이터의 일괄 정리, 선행 업무가 연결된 *후* 정상적으로 완료되는 흐름은 변경하지 않는다. 이는 인증 기능이 아니라 트랜잭션 사이의 **데이터 정합성 보호**다.

근거: `api/src/main/java/com/ibank/axwms/domain/worklog/repository/jooq/WorklogJooqRepositoryImpl.java`의 `searchPredecessorCandidatePage`, `api/src/main/java/com/ibank/axwms/domain/worklog/service/WorklogDependencyService.java`의 `registerPredecessor`·`replacePredecessors`.

## 2. 설계와 구현

### 2.1 버전 기반 충돌 감지

`V20__add_worklog_version.sql`로 `tb_worklog.version BIGINT NOT NULL DEFAULT 0`을 추가하고, `Worklog.version`에 JPA `@Version`을 적용했다. JPA가 상태 변경 등 업무일지의 갱신 시 버전을 증가시키므로, 선행 관계 등록 요청은 자신이 읽은 버전이 여전히 유효한지 확인할 수 있다.

새 선행 후보는 접근 가능 여부와 완료 상태를 검사한 뒤, 후보 ID 오름차순으로 다음 조건부 갱신을 수행한다. 영향 행 수가 `1`이 아니면 연결을 저장하지 않고 예외를 던진다.

```sql
UPDATE tb_worklog
SET version = version + 1
WHERE worklog_id = :worklogId
  AND version = :version
  AND team_id = :teamId
  AND is_deleted = false
  AND status_code <> 'COMPLETED';
```

이 `UPDATE`는 해당 행에 실제 잠금을 걸지만, 모든 후보를 조회 단계부터 `SELECT FOR UPDATE`로 직렬화하는 방식은 아니다. **읽은 버전의 유효성을 저장 시점에 검증하고 충돌한 요청을 실패시키는 낙관적 동시성 제어**다. 여러 후보 중 하나라도 실패하면 상위 생성·수정 트랜잭션이 롤백된다.

구현 위치: `api/src/main/java/com/ibank/axwms/domain/worklog/repository/WorklogRepository.java`의 `claimPredecessorVersion`, `api/src/main/java/com/ibank/axwms/domain/worklog/service/WorklogDependencyService.java`의 `validateAndClaimPredecessors`.

### 2.2 경쟁 순서별 결과

| 순서 | 결과 |
|---|---|
| B 완료 트랜잭션이 먼저 커밋 | 등록 요청의 조건부 `UPDATE`가 0행을 갱신한다. 새 관계는 저장되지 않는다. |
| 관계 등록의 조건부 `UPDATE`가 먼저 커밋 | 이전 버전의 B를 들고 있던 상태 변경은 JPA `@Version` 검사에서 실패한다. 클라이언트는 최신 상태로 재시도해야 한다. |
| 관계 등록 완료 후 별도 요청이 B를 완료 | 정상 상태 전이로 허용한다. 기존 연결을 소급해서 삭제하지 않는다. |

### 2.3 API와 부수효과

- 처음부터 완료된 후보는 `WORKLOG_PREDECESSOR_COMPLETED` (`409`), 조회 후 바뀐 후보는 `WORKLOG_PREDECESSOR_CONFLICT` (`409`)로 반환한다. JPA의 낙관적 락 예외는 `WORKLOG_CONCURRENT_MODIFICATION` (`409`)로 변환한다. 응답 형식은 기존 `ApiResponse` 오류 봉투를 유지한다.
- 다른 팀·없는 업무·소프트 삭제 업무는 기존 `WORKLOG_PREDECESSOR_NOT_ACCESSIBLE`을 유지한다. 완료 여부를 반환하기 전에 접근 범위를 먼저 검증해 타 팀 상태를 노출하지 않는다.
- 생성 흐름의 선행 업무 검증을 외부 파일 업로드보다 앞으로 옮겼다. 이 충돌 경로에서는 업로드가 시작되지 않는다. 일반적인 파일 업로드 실패의 보상 처리는 이번 범위에 포함하지 않는다.
- `WorklogControllerDocs`에 생성·수정·상태 변경 등 관련 `409` 계약을 반영했다.

## 3. 검증 증거

실제 PostgreSQL Testcontainers 기반 테스트에서 두 독립 트랜잭션의 실행 순서를 latch로 고정했다. `IntegrationTestSupport`는 `pgvector/pgvector:pg17` 컨테이너를 사용한다.

| 검증 항목 | 테스트 |
|---|---|
| B 조회 후 다른 트랜잭션이 완료·커밋하면 조건부 갱신 0행 | `WorklogDependencyRepositoryIntegrationTest.completed_between_read_and_claim_is_rejected` |
| 조건부 갱신 후 낡은 JPA 상태 변경의 낙관적 락 실패 | `WorklogDependencyRepositoryIntegrationTest.stale_status_update_after_claim_is_rejected` |
| 미완료 후보 연결 성공, 완료 후보의 새 연결 거부 | `WorklogDependencyRepositoryIntegrationTest.dependency_service_enforces_completion_state` |
| 다중 후보 중 한 건 충돌 시 연결 저장 없음, 기존 연결 재검증 없음 | `WorklogDependencyServiceTest` |
| 선행 충돌 시 외부 파일 업로드 미시작 | `WorklogServiceTest.predecessor_conflict_prevents_file_upload` |
| 상태 변경 버전 충돌의 HTTP `409` 및 오류 코드 | `WorklogControllerTest.status_version_conflict_returns_409` |

검증 명령과 결과:

```text
api> .\gradlew.bat test --console=plain   BUILD SUCCESSFUL
JUnit XML 집계                         571 tests, 0 failures, 0 errors, 0 skipped
api> .\gradlew.bat build --console=plain  BUILD SUCCESSFUL
git diff --check                       exit 0
```

## 4. 한계와 운영 시 주의점

1. `@Version`은 `Worklog` 전체에 적용되므로 상태 변경뿐 아니라 AI 콜백·요약 상태 갱신 등도 동시 수정 시 `409` 충돌이 날 수 있다. 해당 경로의 자동 재시도 정책은 추가하지 않았다. 충돌 빈도와 콜백 실패 로그를 관측한 뒤 별도 정책을 정해야 한다.
2. 조건부 갱신은 후보 행을 커밋까지 잠근다. ID 정렬로 다중 후보의 잠금 순서를 일정하게 했지만, 실제 운영 부하에서의 경합·교착 빈도와 처리량은 측정하지 않았다.
3. 조회 기반 순환 의존 검사의 **동시 그래프 수정 경쟁**은 별도 문제다. 이번 테스트는 완료 상태와 새 선행 관계 등록 사이의 경쟁을 검증하며, 그래프 전체의 직렬 가능성까지 입증하지 않는다.
4. PDF의 성능 수치나 운영 규모 효과는 이번 작업에서 재측정하지 않았다. 이 보고서의 결과 수치는 테스트 통과 건수에 한정한다.

## 5. 결론

완료 상태 조회와 관계 저장 사이에 다른 트랜잭션이 B를 변경하는 경우, 버전·상태 조건부 갱신 또는 JPA `@Version` 검사 중 하나가 충돌을 감지한다. 검증된 두 경쟁 순서에서 낡은 상태를 근거로 한 새 관계 저장은 거부된다. 소스 변경과 이 보고서는 아직 커밋하지 않았다.
