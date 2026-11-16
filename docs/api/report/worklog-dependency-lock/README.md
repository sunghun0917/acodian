# 선행 업무 등록 동시성 제어 (낙관적 락)

> **현재 상태:** 완료 (2026-10-07). 이후 바뀐 결정 없음.
> **한 줄 요약:** 선행 업무 B가 미완료인 걸 확인한 직후 다른 요청이 B를 완료해도, 완료된 B가 새 선행 업무로 저장되지 않도록 버전 조건부 UPDATE와 JPA `@Version`으로 막았다.

---

## 1. 배경: 무엇이 문제였나

업무 A에 선행 업무 B를 연결하는 상황이다.

```text
T1 (A에 B 연결)                 T2 (B 완료 처리)
B 조회: 미완료 ✓
                                B를 COMPLETED로 변경, 커밋
A → B 연결 저장, 커밋   ← 완료된 B가 선행 업무로 저장됨
```

- PostgreSQL 기본 격리 수준 `READ COMMITTED`에서는 **일반 SELECT 결과가 읽은 뒤에 바뀌는 것을 막지 못한다.**
- 게다가 후보 **검색**에서만 `COMPLETED`를 빼고 있었다. 실제 **등록** 경로는 같은 팀, 미삭제, 자기 참조, 순환만 확인했다. 그래서 검색을 거치지 않고 API를 직접 호출해도 완료된 업무를 연결할 수 있었다.

## 2. 목표와 완료 기준

1. 같은 팀의 미삭제, 미완료 업무만 **새** 선행 업무로 등록된다. 완료된 업무는 `409 Conflict`.
2. T1이 B를 읽은 뒤 T2가 B를 완료하면 T1 전체가 롤백된다. 연결, 상태 이력, 태그 등 부분 저장이 남지 않는다.
3. 반대로 T1이 먼저 성공하면 T2는 낡은 버전 때문에 실패한다.
4. 여러 후보 중 하나라도 충돌이면 전체 실패.
5. 적용 범위: `POST /worklogs`의 신규 연결, `PATCH /worklogs/{id}`에서 **새로 추가되는 연결(`toAdd`)만**. 이미 있는 연결, 과거 데이터, 연결 후 B가 정상 완료되는 흐름은 건드리지 않는다.

## 3. 선택지와 결정

| 방법 | 장점 | 단점 | 선택 |
|---|---|---|---|
| 격리 수준을 `SERIALIZABLE`로 | 코드 변경 적음 | 전체 트랜잭션 비용 증가, 직렬화 실패 재시도 처리 필요 | X |
| 비관적 락 (`SELECT ... FOR UPDATE`) | 확실하게 직렬화 | 조회부터 잠금을 오래 잡음, 경합 시 대기 | X |
| **낙관적 락 (버전 조건부 UPDATE + `@Version`)** | 충돌이 드문 환경(약 35명 규모)에 적합, 잠금 시간 짧음 | 충돌 시 클라이언트가 재시도해야 함, `@Version`이 엔티티 전체에 걸림 | **O** |

원본 문서에는 격리 수준과 비관적 락 비교가 따로 없다. 위 두 줄은 공부용으로 일반적인 비교를 정리한 것이다.

**포기한 것 (트레이드오프):** `@Version`이 `Worklog` 전체에 적용된다. 그래서 상태 변경뿐 아니라 AI 콜백의 요약/상태 갱신도 동시에 일어나면 `409`가 날 수 있다.

## 4. 구현: 어떻게 동작하나

### 스키마
`V20__add_worklog_version.sql`: `tb_worklog.version BIGINT NOT NULL DEFAULT 0`, 엔티티에 `@Version`.

### 등록 시 조건부 UPDATE
후보의 접근 가능 여부와 완료 상태를 먼저 검사한 뒤, **후보 ID 오름차순으로** 아래 UPDATE를 실행한다. 영향 행 수가 1이 아니면 예외를 던지고 연결을 저장하지 않는다.

```sql
UPDATE tb_worklog
SET version = version + 1
WHERE worklog_id = :worklogId
  AND version = :version          -- 내가 읽은 버전 그대로인가
  AND team_id = :teamId
  AND is_deleted = false
  AND status_code <> 'COMPLETED';
```

- PostgreSQL `READ COMMITTED`에서 UPDATE는 커밋된 최신 행으로 WHERE 조건을 **다시 평가**한다. 이 동작을 이용한다.
- ID 오름차순으로 갱신하는 이유: 여러 후보를 잠글 때 순서를 고정해야 교착(deadlock)을 줄일 수 있다.

### 경쟁 순서별 결과
| 순서 | 결과 |
|---|---|
| B 완료가 먼저 커밋 | 조건부 UPDATE가 0행 → 연결 저장 안 됨 (`WORKLOG_PREDECESSOR_CONFLICT`) |
| 조건부 UPDATE가 먼저 커밋 | 낡은 버전의 B 상태 변경이 `@Version` 검사에서 실패 (`WORKLOG_CONCURRENT_MODIFICATION`) |
| 연결 완료 후 별도 요청으로 B 완료 | 정상 상태 전이로 허용 |

### 오류 코드 (모두 `409`, 기존 `ApiResponse` 형식)
- `WORKLOG_PREDECESSOR_COMPLETED`: 처음부터 완료된 후보
- `WORKLOG_PREDECESSOR_CONFLICT`: 읽은 뒤 바뀐 후보
- `WORKLOG_CONCURRENT_MODIFICATION`: JPA 낙관적 락 예외 변환

### 핵심 코드 위치
| 역할 | 파일 |
|---|---|
| 조건부 UPDATE | `domain/worklog/repository/WorklogRepository.java`의 `claimPredecessorVersion` |
| 검증 순서 | `domain/worklog/service/WorklogDependencyService.java`의 `validateAndClaimPredecessors` |
| 오류 변환 | `global/error/GlobalExceptionHandler.java`, `ErrorCode.java` |

## 5. 결과와 검증

실제 PostgreSQL(Testcontainers `pgvector/pgvector:pg17`)에서 두 독립 트랜잭션의 순서를 **latch로 고정**해 재현했다.

| 검증 | 테스트 |
|---|---|
| 읽은 뒤 다른 트랜잭션이 완료하면 0행 | `WorklogDependencyRepositoryIntegrationTest.completed_between_read_and_claim_is_rejected` |
| 조건부 UPDATE 후 낡은 상태 변경 실패 | `...stale_status_update_after_claim_is_rejected` |
| 미완료는 성공, 완료는 거부 | `...dependency_service_enforces_completion_state` |
| 여러 후보 중 하나 충돌 시 전체 미저장 | `WorklogDependencyServiceTest` |
| 충돌 시 파일 업로드 시작 안 함 | `WorklogServiceTest.predecessor_conflict_prevents_file_upload` |
| HTTP 409 응답 | `WorklogControllerTest.status_version_conflict_returns_409` |

API 전체 **571건 통과**, 실패 0.

## 6. 진행 중 바뀐 결정

| 내용 |
|---|
| 생성 흐름에서 선행 업무 검증을 **외부 파일 업로드보다 앞으로** 옮겼다. 충돌로 롤백될 요청이 S3에 파일부터 올리는 일을 막기 위해서다. |
| 다른 팀/없는/삭제 업무는 완료 여부를 알려주기 **전에** 접근 불가로 먼저 거절한다. 다른 팀 업무의 상태가 노출되지 않게 하려는 것이다. |

## 7. 한계와 위험

- **`@Version` 충돌 확대:** AI 콜백이 사용자 수정과 겹치면 `409`가 날 수 있다. 자동 재시도 정책은 아직 없다. 사용자 변경을 덮어쓰지 않도록 일부러 넣지 않았다.
- **잠금과 교착:** 조건부 UPDATE도 커밋까지 행을 잠근다. 실제 부하에서 경합/교착 빈도는 측정하지 않았다.
- **순환 검사 경쟁은 별개 문제:** 순환 의존 검사는 여전히 조회 기반이다. 두 요청이 동시에 그래프를 바꾸면 순환이 생길 수 있다. 이번 작업은 "완료 상태 vs 새 연결" 경쟁만 해결했다.
- **JPA와 jOOQ 혼용:** 조건부 UPDATE(jOOQ/네이티브)는 JPA 영속성 컨텍스트를 갱신하지 않는다. 그래서 후보를 엔티티가 아니라 projection으로 읽는다.
- **보안:** 접근 범위를 먼저 검증해 다른 팀 상태 노출을 막았다. 오류 응답에 SQL이나 스택트레이스를 노출하지 않는다.

## 8. 앞으로 개선할 점

1. AI 콜백 경로의 `409` 빈도 관측 후 재시도 정책 결정
2. 순환 의존 검사의 동시성 보호 (그래프 수정 직렬화)
3. 일반적인 파일 업로드 실패 보상 처리

## 9. 스스로 점검 질문

- **Q. `READ COMMITTED`에서 왜 이 문제가 생기나?**
  A. 각 SELECT는 그 순간 커밋된 데이터만 본다. 읽은 뒤 다른 트랜잭션이 바꾸고 커밋해도 내 트랜잭션은 그걸 막지 못한다. 확인과 저장 사이에 틈이 있다.
- **Q. 조건부 UPDATE가 왜 그 틈을 막나?**
  A. UPDATE는 행을 잠그고, 최신 커밋된 값으로 WHERE를 다시 평가한다. 그 사이 B가 완료됐거나 버전이 바뀌었으면 0행이 되고, 그걸 충돌로 처리한다.
- **Q. 낙관적 락과 비관적 락은 언제 쓰나?**
  A. 충돌이 드물면 낙관적(잠금 짧음, 실패 시 재시도), 충돌이 잦거나 재시도 비용이 크면 비관적(미리 잠금)이다. 이 서비스는 사용자가 적고 충돌이 드물다.
- **Q. 여러 후보를 왜 ID 순으로 갱신하나?**
  A. 두 트랜잭션이 서로 다른 순서로 행을 잠그면 서로를 기다리는 교착이 생긴다. 순서를 고정하면 이를 피할 수 있다.
- **Q. 후보 검색에서 이미 완료를 빼는데 왜 등록에서 또 검사하나?**
  A. 화면 필터는 보안/정합성 경계가 아니다. API를 직접 호출하거나 검색과 등록 사이에 상태가 바뀌면 소용없다. 규칙은 서버의 저장 시점에 강제해야 한다.

## 10. 원본 문서

| 문서 | 내용 |
|---|---|
| [optimistic-lock-report-2026-10-07.md](optimistic-lock-report-2026-10-07.md) | 구현 결과 보고서 |
| `.omx/plans/portfolio-5-worklog-dependency-concurrency.md` | 구현 계획 (수용 기준, 위험) |
