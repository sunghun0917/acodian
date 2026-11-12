# API 기능별 검수 및 리팩토링 목록 — 2026-10-30

현재 API는 업무 저장과 AI 요청 보존, 역할별 조직 관리, 알림 전달을 구현한다. 이번 검수는 기능을 유지하면서 고칠 오류와 데이터 일관성 문제를 찾는 읽기 전용 리뷰다. 소스 수정은 하지 않았다.

## 범위와 검증 근거

- `api/src/main/java`의 컨트롤러 16개, HTTP 핸들러 64개를 목록화하고 연결된 서비스·저장소·관련 테스트를 추적했다. 내부 콜백과 스케줄러도 포함했다.
- 여러 조직 사용자의 동시 요청, 장시간 실행되는 API/작업자를 전제로 판단했다. 운영 사용자 수·실제 부하·다중 인스턴스 수는 확인하지 않았다.
- 프로젝트의 `AGENTS.md`, `docs/api/adr.yaml`, `docs/api/code-convention.yaml` 및 관련 보고서의 의도적 계약을 대조했다.
- Graphify manifest와 현재 Java 소스를 비교했을 때 9개 파일이 신규 또는 변경 상태였다. 최신 근거는 실제 소스를 사용했고 그래프는 갱신하지 않았다.
- 실행: `gradlew.bat test --tests '*ServiceTest' --tests '*TokenServiceTest' --tests '*NotificationRedisStreamSseSubscriberTest' --tests '*NotificationSseEmitterRegistryTest' --console=plain`
- 결과: **23개 테스트 클래스, 275개 테스트 통과, 실패·오류·스킵 0**. 컴파일 작업은 UP-TO-DATE, 테스트 작업은 이번에 실행됐다.
- 아래 발견은 코드상 조건과 결과를 추적한 판단이다. 동시성·외부 스토리지 장애 사례를 실환경에서 재현한 결과와 구분한다. 기존 테스트 통과는 아래 결함이 없다는 증거가 아니다.

## 우선순위

| 분류 | 번호 | 기준 |
|---|---|---|
| Must fix / P1 | 1, 3, 4, 7, 8, 9, 10, 15 | 세션 폐기·데이터 보존·상태 전이·검색 동기화가 깨지는 경로 |
| Must fix / P2 | 2, 5, 6, 11, 12, 13, 14 | 입력 계약 불일치, 데이터 손실 및 조회 정합성 오류 |
| Should fix / P2 | 16, 17 | 무제한 보관과 배치 조회 비용 |
| 정책·계약 확인 | A, B, C | 현행 명세 변경 또는 요구사항 결정이 먼저 필요한 항목 |

기능별로 번호를 고정했다. 같은 원인의 여러 증상을 별개 항목으로 중복 계산하지 않았다.

## 1. 인증·세션

### 1. [P1] 토큰 회전과 로그아웃 사이에서 폐기한 세션이 다시 저장됨

- **하는 일:** refresh 토큰을 Redis 값과 비교한 뒤 필요하면 같은 세션 ID로 새 토큰을 저장한다.
- **문제:** 회전 대상 refresh가 검증을 마침 → logout이 세션 삭제 → refresh가 회전 값을 저장하는 순서에서 삭제한 세션이 살아난다. 검증과 저장이 별도 Redis 연산이어서 DB의 `@Transactional`로 보호되지 않는다.
- **최소 수정:** 기존 토큰 비교와 회전 저장을 Redis에서 원자적으로 처리한다. 로그아웃과 회전의 경쟁 및 동시 회전 두 요청을 통합 테스트하고, 경쟁에서 패한 요청의 응답 계약도 정한다.
- **방치 영향:** 사용자가 로그아웃한 뒤에도 회전된 refresh 토큰으로 세션이 유지될 수 있다.
- **근거:** [TokenService.java](../../../api/src/main/java/com/ibank/axwms/domain/auth/service/TokenService.java#L49), 같은 파일 `issueRefreshTokens()`의 73–74행. 회전은 남은 수명이 절반 이하인 요청에 적용된다. 기존 테스트는 이 경쟁 순서를 고정하지 않는다.

## 2. 사용자·프로필 이미지

### 2. [P2] 프로필 이미지 복사 실패 후 복구 수단이 없음

- **하는 일:** 임시 이미지를 업로드하고 최종 URL을 DB에 커밋한 뒤, 커밋 이벤트에서 최종 저장 위치로 복사한다.
- **문제:** DB 커밋 후 S3 복사가 실패하면 실패를 기록하고 끝난다. 이후 임시 파일 정리가 원본을 지우면 최종 URL에는 파일이 없고 원본도 없어질 수 있다.
- **최소 수정:** 실패한 복사를 복구할 수 있도록 요청을 보존하고 재시도한다. 미완료 복사의 원본은 임시 파일 정리 대상에서 보호한다. 먼저 일시적 복사 실패 → 재실행 → 복구 시나리오를 고정한다.
- **방치 영향:** 회원가입·관리자 사용자 수정이 성공했는데 프로필 이미지가 계속 깨진다.
- **근거:** [ProfileImageCommitHandler.java](../../../api/src/main/java/com/ibank/axwms/domain/organization/user/event/ProfileImageCommitHandler.java#L18), [ProfileImageStorageService.java](../../../api/src/main/java/com/ibank/axwms/domain/organization/user/service/ProfileImageStorageService.java#L90). 기존 커밋 핸들러 테스트만으로 장애 후 복구를 보장하지 못한다.

## 3. 팀 관리

### 3. [P1] 삭제된 팀에 기존 구성원이 새 업무를 등록할 수 있음

- **하는 일:** 업무 등록은 팀 존재와 멤버십을 확인한다. 팀 삭제는 팀의 `deletedAt`을 설정한다.
- **문제:** `getTeamOrThrow()`는 삭제 팀도 반환하고 `isMember()`는 남아 있는 ACTIVE 멤버십을 인정한다. 팀 삭제 뒤 기존 구성원이 업무를 등록할 수 있다. 조회에서는 삭제 팀을 제외하는 경로가 있어 생성한 업무가 보이지 않는 상태도 생긴다.
- **최소 수정:** 업무 등록 경계에서 미삭제 팀을 검증한다. 조회 helper의 모든 소비자가 같은 정책인지 확인한 뒤 공통화하고, 팀 삭제 후 등록 거부를 통합 테스트한다.
- **방치 영향:** 삭제한 조직에 새 업무·파일·AI 처리 요청이 계속 쌓일 수 있다.
- **근거:** [TeamService.java](../../../api/src/main/java/com/ibank/axwms/domain/organization/team/service/TeamService.java#L183)의 삭제, 197행 존재 조회, 211행 멤버십 검사. [WorklogService.java](../../../api/src/main/java/com/ibank/axwms/domain/worklog/service/WorklogService.java#L131)의 등록 경로.

### 4. [P1] 관리 권한을 가진 부서장이 둘이면 팀 상세 조회가 복수 행으로 실패

- **하는 일:** 팀 상세 쿼리가 팀 관리자 중 DEPT_HEAD 사용자를 조인하고 단일 결과를 반환한다.
- **문제:** 팀 수정은 다른 부서장의 관리 권한 추가를 허용한다. 두 부서장이 같은 팀의 관리 권한을 가지면 조인 결과가 두 행인데 `fetchOptional()`은 한 행만 기대한다.
- **최소 수정:** 상세에 표시할 단일 관리자 선택 규칙을 정해 조회를 한 행으로 만든다. 다중 관리자 자체를 허용하지 않는 정책이라면 쓰기·DB 제약에서 막아야 한다. 단순 `distinct`나 임의 `limit(1)`로 의미를 숨기지 않는다.
- **방치 영향:** 정상적인 관리 권한 추가 뒤 팀 상세 API가 500 오류를 낼 수 있다.
- **근거:** [TeamJooqRepositoryImpl.java](../../../api/src/main/java/com/ibank/axwms/domain/organization/team/repository/jooq/TeamJooqRepositoryImpl.java#L188)의 관리자 조인과 `fetchOptional()`, [TeamService.java](../../../api/src/main/java/com/ibank/axwms/domain/organization/team/service/TeamService.java#L408)의 관리자 추가. 부서장 두 명 fixture 테스트가 필요하다.

### 5. [P2] 기존 구성원의 리더 지정이 대표 팀·배정 정보를 초기화

- **하는 일:** `addUsers`에 이미 존재하는 팀 구성원이 들어오면 `reactivate()`를 호출한다.
- **문제:** ACTIVE 구성원에게도 `reactivate(..., null, false)`를 호출하여 기존 배정과 `isPrimary`를 덮어쓴다. 기존 대표 팀 구성원을 리더로 지정하는 요청이 대표 소속까지 지운다.
- **최소 수정:** ACTIVE 구성원의 리더·팀 역할 갱신과 LEFT 구성원의 재가입을 구분하고, 요청하지 않은 기존 배정·대표 소속 값을 보존한다.
- **방치 영향:** 팀 역할만 바꾼 사용자의 대표 소속이나 배정 정보가 예고 없이 달라진다.
- **근거:** [TeamService.java](../../../api/src/main/java/com/ibank/axwms/domain/organization/team/service/TeamService.java#L430). 대표 팀 구성원의 리더 변경 보존 테스트가 필요하다.

## 4. 부서 관리

### 6. [P2] 부서 목록의 전체 활성 인원수가 탈퇴·삭제 팀 구성원을 포함

- **하는 일:** 부서 목록 응답의 활성 사용자 수를 계산한다.
- **문제:** 전체 인원 집계는 사용자와 팀 ACTIVE 조건만 검사하고 멤버십 상태와 팀 삭제 여부를 빠뜨린다. LEFT 멤버십이나 삭제된 ACTIVE 팀에만 속한 사용자도 집계한다. 같은 저장소의 부서별 집계는 두 조건을 검사한다.
- **최소 수정:** 전체 집계에 ACTIVE 멤버십과 미삭제 팀 조건을 맞춘다. LEFT 멤버십·삭제 팀·복수 팀 소속의 중복 제외를 테스트한다.
- **방치 영향:** 부서별 인원과 목록 전체 인원수가 서로 다른 기준으로 표시된다.
- **근거:** [DepartmentJooqRepositoryImpl.java](../../../api/src/main/java/com/ibank/axwms/domain/organization/department/repository/jooq/DepartmentJooqRepositoryImpl.java#L172), 비교 대상 `fetchActiveUserCount(Long)` 139행.

## 5. 업무 등록·상태·선행 관계

### 7. [P1] 수동 태그를 선택한 신규 업무의 AI 상태가 DB에서 PENDING으로 남음

- **하는 일:** 태그 연결·사용 횟수를 저장한 뒤 새 업무를 AI 처리 중 상태로 바꾼다.
- **문제:** 수동 태그가 하나 이상이면 사용 횟수 bulk 갱신의 `clearAutomatically=true`가 Worklog를 분리한다. 그 뒤 `startAiProcessing()`은 분리된 객체만 변경한다. Outbox는 저장되지만 DB의 업무 상태에는 그 변경이 반영되지 않는다.
- **최소 수정:** `startAiProcessing()`을 영속성 컨텍스트 초기화 전에 수행한다. 단순히 전체 clear 옵션을 제거하려면 다른 호출부의 오래된 엔티티 조회 여부도 확인해야 한다.
- **방치 영향:** 실제 작업이 대기·진행 중이어도 화면과 후속 처리가 PENDING 상태를 읽는다.
- **근거:** [WorklogService.java](../../../api/src/main/java/com/ibank/axwms/domain/worklog/service/WorklogService.java#L167), [TagRepository.java](../../../api/src/main/java/com/ibank/axwms/domain/tag/repository/TagRepository.java#L22). 등록 통합 fixture의 빈 태그 목록과 달리 유효한 수동 태그를 포함한 DB 재조회 테스트가 필요하다.

### 8. [P1] 선행 관계 동시 추가가 순환 검사를 통과

- **하는 일:** 새 선행 업무를 연결하기 전에 그래프의 순환 여부를 확인한다.
- **문제:** 관계가 없는 A와 B에 A→B, B→A를 동시에 추가하면 두 트랜잭션 모두 기존 그래프에서 순환이 없다고 본다. 서로 다른 선행 행을 점유하면 둘 다 커밋할 수 있다.
- **최소 수정:** 같은 팀의 관계 변경을 공통 잠금으로 직렬화한 뒤 순환 검사를 한다. 임의 길이의 순환도 보호해야 하므로 단순히 신규 양 끝 행만 잠그는 수정은 충분한지 별도 증명이 필요하다.
- **방치 영향:** 선행 업무 완료 조건이 서로 기다리는 잘못된 그래프가 저장된다.
- **근거:** [WorklogDependencyService.java](../../../api/src/main/java/com/ibank/axwms/domain/worklog/service/WorklogDependencyService.java#L86). 양방향 및 3개 이상 업무의 동시 변경 통합 테스트가 필요하다. 실제 경쟁 실행은 재현하지 않았다.

### 11. [P2] 제목 길이의 API 검증과 DB 제약이 다름

- **하는 일:** 생성·수정 DTO는 제목을 100자까지 허용한다.
- **문제:** V14 이후 DB의 제목은 VARCHAR(50)이다. 51–100자 제목은 요청 검증을 통과한 뒤 저장 단계에서 실패한다.
- **최소 수정:** 생성·수정 DTO와 오류 메시지를 DB의 50자 계약에 맞춘다. 다른 저장 경로와 엔티티 길이 선언도 함께 대조한다.
- **방치 영향:** 클라이언트에 허용된 것으로 보인 입력이 DB 예외로 실패한다.
- **근거:** [CreateWorklogApiDto.java](../../../api/src/main/java/com/ibank/axwms/domain/worklog/dto/CreateWorklogApiDto.java#L30), [UpdateWorklogApiDto.java](../../../api/src/main/java/com/ibank/axwms/domain/worklog/dto/UpdateWorklogApiDto.java#L31), [V14 마이그레이션](../../../api/src/main/resources/db/migration/V14__shorten_worklog_title_to_50.sql#L6). 50자 성공·51자 400 경계를 테스트한다.

### 12. [P2] 완료 상태로 최초 등록한 업무에 완료일이 없음

- **하는 일:** 최초 등록에서도 COMPLETED 상태를 받을 수 있다.
- **문제:** 생성 factory는 상태만 설정하고 완료일을 채우지 않는다. 이후 완료 상태를 유지하면 기간별 완료 집계의 날짜 조건에서 계속 빠질 수 있다.
- **최소 수정:** 생성 시 COMPLETED라면 완료일을 함께 정한다. 최초 완료 상태를 금지하려면 요청 계약 변경이므로 정책 결정 후 제한한다.
- **방치 영향:** 완료 업무가 기간별 완료 수와 대시보드 집계에서 누락된다.
- **근거:** [Worklog.java](../../../api/src/main/java/com/ibank/axwms/domain/worklog/entity/Worklog.java#L127), [WorklogJooqRepositoryImpl.java](../../../api/src/main/java/com/ibank/axwms/domain/worklog/repository/jooq/WorklogJooqRepositoryImpl.java#L419). 완료 상태 최초 등록 후 DB 완료일과 기간 집계를 검증해야 한다.

### 13. [P2] 삭제된 선행 업무가 목록 개수에는 남음

- **하는 일:** 목록은 선행 업무 수를, 상세는 선행 업무 목록을 제공한다.
- **문제:** B가 A의 선행 업무일 때 B를 소프트 삭제하면 상세는 B를 제외하지만 개수 쿼리는 연결 행만 센다. 목록에는 1개, 상세에는 0개가 표시된다.
- **최소 수정:** 개수 조회도 선행 업무를 조인하여 미삭제 조건을 적용한다.
- **방치 영향:** 동일 업무의 목록·상세·후속 조건이 서로 다른 선행 관계를 보여 준다.
- **근거:** [WorklogDependencyJooqRepositoryImpl.java](../../../api/src/main/java/com/ibank/axwms/domain/worklog/repository/jooq/WorklogDependencyJooqRepositoryImpl.java#L47). 비교 대상 상세 조회는 33행. 선행 업무 삭제 후 두 조회의 일치 테스트가 필요하다.

## 6. AI 콜백·검색 색인

### 9. [P1] 늦은 AI 콜백이 사용자의 요약 편집본을 덮어씀

- **하는 일:** AI 완료 콜백이 요약을 업무에 저장한다. 사용자가 요약을 편집하면 `aiSummaryEdited`가 설정된다.
- **문제:** 사용자가 편집한 뒤 과거 AI 요청의 COMPLETED 콜백이 오면 편집 여부 검사 없이 `changeAiSummary()`가 덮어쓴다.
- **최소 수정:** 사용자 편집본을 보호하는 조건을 공통 반영 경계에 두고, 요청의 최신성 확인도 필요한지 콜백 계약과 함께 정한다.
- **방치 영향:** 사용자가 저장한 텍스트가 이전 AI 작업 결과로 사라진다.
- **근거:** `InternalWorklogAiCallbackService.java:32` (당시 코드, S1에서 삭제), [Worklog.java](../../../api/src/main/java/com/ibank/axwms/domain/worklog/entity/Worklog.java#L167). 편집 저장 → 지연 콜백 순서의 회귀 테스트가 필요하다.

### 10. [P1] AI 태그 저장이 LightRAG 재색인을 예약하지 않음

- **하는 일:** 최초 색인과 AI 태그 생성은 각각 진행되고 태그 콜백이 업무의 태그 관계를 저장한다.
- **문제:** 최초 색인이 원본을 읽은 뒤 AI 태그가 저장되면 추가 색인 요청이 없어 태그가 DB에만 반영된다. 태그 병합으로 업무 태그가 치환되는 경로도 같은 동기화 정책을 확인해야 한다.
- **최소 수정:** 실제 신규 태그 관계가 생긴 경우 같은 트랜잭션에서 재색인을 예약한다. 중복 콜백에서 불필요한 재색인이 반복되지 않게 한다.
- **방치 영향:** 태그를 검색 문맥으로 쓰는 AI 검색이 최신 업무 정보를 계속 놓칠 수 있다.
- **근거:** [InternalTagAiCallbackService.java](../../../api/src/main/java/com/ibank/axwms/domain/tag/service/InternalTagAiCallbackService.java#L44). 기존 [업무 등록 검증 보고서](worklog-create-outbox-validation.md)의 254–275행에도 해당 실행 순서의 관측이 기록되어 있다. 이번 검수에서 외부 AI 실연동은 반복하지 않았다.

## 7. 태그 선택·AI 태그·병합

### 14. [P2] 병합으로 삭제한 태그를 다시 연결할 수 있음

- **하는 일:** 선택된 태그 ID가 존재하는지 조회하여 업무와 연결한다.
- **문제:** `normalizeExistingTagIds()`는 존재 개수만 검사한다. 삭제된 태그도 조회되므로 병합 후 과거 source ID를 보낸 생성·수정·지연 콜백이 해당 태그를 다시 연결한다. AI 신규 태그명 처리도 동일 이름의 삭제 태그를 되찾을 수 있다.
- **최소 수정:** 신규 연결은 활성 태그만 허용하고 AI 신규 태그명 조회에도 동일 정책을 적용한다. 기존 삭제 태그 관계를 제거하는 경로까지 활성 검증으로 막지 않도록 연결과 해제의 검증 의미를 구분한다.
- **방치 영향:** 검색 picker에서 숨긴 태그가 업무에 다시 붙고 병합 결과와 사용 횟수가 어긋난다.
- **근거:** [TagService.java](../../../api/src/main/java/com/ibank/axwms/domain/tag/service/TagService.java#L36), [InternalTagAiCallbackService.java](../../../api/src/main/java/com/ibank/axwms/domain/tag/service/InternalTagAiCallbackService.java#L73), 자동 삭제 필터가 없는 `MetaTag`. 병합 후 이전 ID·동일 이름을 재전송하는 테스트가 필요하다.

### 15. [P1] 병합 후보 수정과 적용이 경합하면 APPLIED가 PENDING으로 돌아갈 수 있음

- **하는 일:** 후보 수정은 PENDING을 읽어 검증한 후 snapshot과 후보 항목을 갱신한다. 병합은 상태 조건부 UPDATE로 APPLIED를 선점한다.
- **문제:** 수정이 PENDING을 읽은 뒤 병합이 커밋하고, 수정이 다른 활성 태그로 snapshot을 바꾸어 flush하면 오래된 엔티티의 상태까지 덮어쓸 수 있다. `@Version`과 공통 잠금이 없고 항목 삭제는 자동 flush한다. 적용된 병합을 다른 내용의 대기 후보로 되돌릴 수 있다.
- **최소 수정:** 수정과 적용 양쪽이 상태 검증 전에 같은 후보 행을 잠그거나, 모든 갱신에 버전·상태 조건을 적용한다.
- **방치 영향:** 이미 수행한 태그 치환과 후보의 상태·내용이 불일치하고 같은 후보가 다시 적용될 수 있다.
- **근거:** [TagMergeCandidateService.java](../../../api/src/main/java/com/ibank/axwms/domain/tag/service/TagMergeCandidateService.java#L91), [TagMergeCandidateRepository.java](../../../api/src/main/java/com/ibank/axwms/domain/tag/repository/TagMergeCandidateRepository.java#L49), `TagMergeCandidateItemRepository.deleteByMergeCandidateId()` 및 버전 필드가 없는 엔티티. 실제 SQL 경쟁 재현은 수행하지 않았다.

## 8. 알림·Redis·배치

### 16. [P2 / Should fix] 실시간 알림 Redis Stream에 보관 상한이 없음

- **하는 일:** 알림 생성마다 Redis Stream에 내용을 추가한다. 구독자는 시작 시 기존 끝 지점부터 읽고 과거 알림을 replay하지 않는다.
- **문제:** API 코드에는 Stream trim·TTL·삭제 경로가 없다. DB 읽은 알림을 정리해도 Redis의 과거 내용은 별개로 계속 누적된다. 일정한 알림량만 발생해도 장기간에는 저장량이 계속 증가한다.
- **최소 수정:** live 알림 전달에 필요한 여유를 정해 길이 또는 시간 기준 trim을 적용한다. 현재 poll 중인 메시지를 과도하게 제거하지 않도록 지연 허용 범위와 보관량을 함께 테스트한다.
- **방치 영향:** 사용하지 않는 알림 과거 데이터가 Redis 메모리·지속 저장 공간을 차지한다.
- **근거:** [NotificationRedisStreamPublisher.java](../../../api/src/main/java/com/ibank/axwms/domain/notification/redis/NotificationRedisStreamPublisher.java#L37), `NotificationRedisStreamSseSubscriber.liveStreamOffset()`. 외부 운영 trim 작업 존재 여부와 실제 메모리 사용량은 미확인이다.

### 17. [P2 / Should fix] 마감 초과 알림 배치가 업무 수만큼 개별 조회

- **하는 일:** 당일·기한 초과 업무 전체를 읽고 업무마다 기존 알림을 찾아 갱신 또는 생성한다.
- **문제:** 후보 N개마다 `findExistingWorklogReminder()`가 실행되어 후보 조회 외 N번의 SELECT와 각 쓰기가 발생한다. 기한 초과 상태가 오래 쌓이면 매일 같은 방식으로 비용이 커진다.
- **최소 수정:** 후보에 해당하는 기존 알림을 한 번에 또는 묶음으로 읽어 맵으로 대조한다. 데이터량이 크면 후보도 분할 처리한다. 조회 수와 결과의 동일성을 함께 검증한다.
- **방치 영향:** 미완료 업무 누적에 비례하여 배치 실행시간과 DB 호출량이 증가한다.
- **근거:** [NotificationService.java](../../../api/src/main/java/com/ibank/axwms/domain/notification/service/NotificationService.java#L131)의 반복과 323행 조회. 실제 부하 측정은 하지 않았으므로 운영 성능 장애가 이미 발생했다고 단정하지 않는다.

## 9. 정책을 먼저 결정할 사항

### A. 가입·본인 직책 수정으로 최고 권한 획득

공개 가입에서 본부장 직책을 고르거나 일반 사용자가 본인 직책을 본부장으로 수정하면 DIRECTOR 역할로 이어진다. 본인 수정 후 DIRECTOR 발급은 기존 E2E 테스트가 기대하는 동작이므로 구현 회귀와 구분한다. 운영 보안 요구가 있는 서비스라면 역할 부여를 신뢰 가능한 관리자·승인 경계로 제한하는 정책 변경이 우선 필요하다.

근거: `AuthService.signup()`, `UserService` 본인 프로필 수정, `UserRole.findByTitleName()`, `UserMeProfileE2eTest`의 직책 변경 후 토큰 발급 테스트. 승인 계약을 바꾸지 않고 코드만 차단하면 현재 API 계약을 깨뜨린다.

### B. 비활성 부서의 활성 팀 허용 여부

부서 비활성화는 ACTIVE 팀이 없어야 하지만 이후 팀 생성·재활성화는 부서 활성 상태를 검사하지 않는다. 비활성 부서 소유 ACTIVE 팀을 금지한다면 생성·수정·비활성화 경계에서 공통 검증과 동시 실행 제어가 필요하다.

근거: `DepartmentService` 112행, `TeamService.createTeam()` 및 `updateTeam()`. ADR-034의 삭제 차단 규칙만으로 모든 생성 정책까지 확정하지 않았다.

### C. 대시보드 여러 집계의 동일 시점 보장 여부

`DashboardService` 118행은 readOnly 트랜잭션이 6개 쿼리의 read-view 일관성을 보장한다고 설명한다. PostgreSQL 기본 READ COMMITTED에서는 SELECT마다 다른 시점이므로 그 설명은 성립하지 않는다. 동시에 업무 상태가 바뀌면 전체와 팀별 집계가 잠깐 달라질 수 있다. 동일 시점 응답이 계약이면 해당 조회 격리 수준과 테스트를 보강하고, 허용되는 차이라면 잘못된 보장 주석을 수정한다. 운영 격리 수준 override는 확인하지 않았다.

## 10. 기능별 검수 인벤토리

| 기능 | 컨트롤러 | HTTP 핸들러 | 결과 |
|---|---|---:|---|
| 인증 | AuthController | 5 | #1, 정책 A |
| 사용자·프로필 | UserController | 7 | #2, 정책 A |
| 부서 | DepartmentController | 6 | #6, 정책 B |
| 팀 | TeamController | 7 | #3–5, 정책 B |
| 스킬 | UserSkillController | 4 | 이번 검수에서 별도 확인된 발견 없음 |
| 평가 | UserEvaluationController | 3 | 이번 검수에서 별도 확인된 발견 없음 |
| 대시보드 | DashboardController | 1 | 정책 C, #12의 영향 |
| 업무·검색 | WorklogController | 13 | #3, #7–13 |
| 업무 AI 콜백 | InternalWorklogAiCallbackController | 2 | #9, #10, #14 |
| Light 색인 콜백 | InternalWorklogLightIndexController | 2 | 개별 콜백·Outbox 흐름 검토, 신규 확정 발견 없음 |
| 파일 | FileController | 2 | 조회·종류·권한 경로 검토, 신규 확정 발견 없음 |
| 파일 AI 콜백 | InternalFileAiCallbackController | 1 | 신규 확정 발견 없음 |
| 태그 조회 | TagController | 1 | 신규 연결 경계 관련 #14 |
| 태그 내부 조회 | InternalTagAiCallbackController | 1 | AI 태그 적용은 업무 콜백 컨트롤러 경유 |
| 태그 병합 | TagMergeCandidateController | 5 | #14, #15 |
| 알림 | NotificationController | 4 | 연계 스케줄러·Stream 포함 #16, #17 |
| 합계 | 16개 | 64 | 코드·테스트 정적 검수 범위 |

별도 발견 없음은 결함이 전혀 없다는 보증이 아니다. 공통 보안·응답·설정과 각 기능의 연결 코드도 읽었지만, 임의의 모든 입력·운영 상태를 전수 실행하지 않았다.

## 11. 유지할 설계와 권장 작업 단위

- `publishWorklogAiPostProcessRequest()` 및 BEFORE_COMMIT 리스너는 사용자 결정대로 유지한다. 단지 이벤트 계층이 있다는 이유로 제거 대상으로 분류하지 않는다.
- AI 등록 상태가 요청 접수와 Light 결과를 집계하는 현재 의미는 기존 2026-10-29 Outbox 보고서의 의도다. 실제 모든 AI 작업 완료를 기다리도록 바꾸는 작업은 이번 결함 목록에 넣지 않았다.
- `tb_worklog_status_history`는 실제 저장·조회 기능이 있으므로 제거하지 않는다. embedding 테이블은 API의 직접 소비 여부만으로 삭제하지 않는다.
- 서비스가 길거나 인터페이스 구현이 하나라는 이유만으로 분리·삭제를 제안하지 않았다. 새로운 공통 계층·의존성을 추가하는 일괄 리팩토링도 제안하지 않는다.
- 하나의 큰 리팩토링 브랜치보다 기능별로 나눈다. 예: 인증 #1, 팀 #3–5, 업무 입력·생성 #7/#11/#12, 선행 관계 #8/#13, AI 콜백 #9/#10, 태그 #14/#15, 알림 #16/#17.
- 각 작업은 위에 적은 실패 사례를 재현하는 테스트부터 고정하고 가장 작은 수정 후 관련 테스트를 실행한다. 스키마 변경이 필요하면 기존 마이그레이션을 고치지 않고 새 파일을 추가한다.

**판정: 1, 3, 4, 7, 8, 9, 10, 15번을 우선 수정한다.** 나머지 기능별 정합성 수정과 운영 정책 결정은 별도 작업으로 이어간다.

**미검증:** 전체 통합·보안 E2E 테스트, Redis/DB 경쟁의 실재현, S3·AI 장애 주입, 운영 부하 및 외부 정리 정책. 이번에 실행한 275개 테스트는 통과했으며 소스 변경·커밋·푸시는 하지 않았다. 줄 수 절감은 실제 수정 diff 없이 산정하지 않았다.
