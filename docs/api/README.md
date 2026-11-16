# API 문서 안내

Spring Boot API의 설계 규칙, 명세, 검증 기록을 관리한다. 코드 작업 규칙은 [api/AGENTS.md](../../api/AGENTS.md)를 먼저 읽는다.

## 폴더 구조

| 위치 | 내용 |
|---|---|
| `adr.yaml`, `code-convention.yaml` | 설계 결정과 코드 컨벤션 (1차 기준) |
| `guide/` | 개발 가이드 (패키지 구조, 엔드포인트, jOOQ) |
| `spec/` | 도메인별 API 명세 |
| `report/<주제>/` | 작업별 종합 정리(`README.md`)와 근거가 되는 원본 보고서, 측정 원본 |

## 규칙

- [설계 결정 기록](adr.yaml)
- [코드 컨벤션](code-convention.yaml)

## 개발 가이드 (`guide/`)

| 문서 | 내용 |
|---|---|
| [package-structure.md](guide/package-structure.md) | Spring Boot 패키지 구조 |
| [endpoint-checklist.md](guide/endpoint-checklist.md) | 엔드포인트 구현, 검증 체크리스트 |
| [endpoint-class-mapping.md](guide/endpoint-class-mapping.md) | 엔드포인트와 클래스 매핑 |
| [jooq-codegen-policy.md](guide/jooq-codegen-policy.md) | jOOQ 코드 생성 운영 정책 |

## API 명세 (`spec/`)

[명세 인덱스](spec/api-spec-index.md)에서 도메인별 명세로 이동한다. 공통 응답, 규약은 [공통 명세](spec/api-spec-common.md)를 참고한다.

## 작업별 보고서 (`report/`)

각 폴더의 `README.md`가 그 주제의 종합 정리다. 전체 작업 목록은 [전체 문서 안내](../README.md#작업별-종합-정리)에 있다.

| 폴더 | 주제 | 폴더 안의 원본 |
|---|---|---|
| [worklog-outbox](report/worklog-outbox/README.md) | 업무일지 AI 후처리 Outbox (도입, 역할 분리) | [outbox-introduction-2026-10-18.md](report/worklog-outbox/outbox-introduction-2026-10-18.md), [role-separation-2026-10-29.md](report/worklog-outbox/role-separation-2026-10-29.md), 측정 원본 [CSV](report/worklog-outbox/timing-5x-2026-10-18.csv) / [JSON](report/worklog-outbox/timing-5x-2026-10-18.json) |
| [worklog-dependency-lock](report/worklog-dependency-lock/README.md) | 선행 업무 등록 동시성 제어 (낙관적 락) | [optimistic-lock-report-2026-10-07.md](report/worklog-dependency-lock/optimistic-lock-report-2026-10-07.md) |
| [worklog-index-celery](report/worklog-index-celery/README.md) | 업무일지 색인 비동기화 (Celery 전환, Worker 분리) | [celery-migration-report-2026-10-27.md](report/worklog-index-celery/celery-migration-report-2026-10-27.md), 설계 [design-celery-2026-10-25.md](report/worklog-index-celery/design-celery-2026-10-25.md), [design-worker-isolation-2026-10-29.md](report/worklog-index-celery/design-worker-isolation-2026-10-29.md), 계획 [plan-celery-2026-10-26.md](report/worklog-index-celery/plan-celery-2026-10-26.md), [plan-worker-isolation-2026-10-29.md](report/worklog-index-celery/plan-worker-isolation-2026-10-29.md) |
| [ai-task-reliability](report/ai-task-reliability/README.md) | AI 작업 신뢰성 (작업별 알림, 수동 재시도, 단순화). **현재 구조의 기준** | [simplification-2026-11-12.md](report/ai-task-reliability/simplification-2026-11-12.md), README 부록에 현재 구현 흐름 원문 |

그 밖의 보고서:
- [api-refactoring-review-2026-10-30.md](report/api-refactoring-review-2026-10-30.md): API 기능별 검수와 리팩토링 목록 (2026-10-30)

보고서는 검증 당시의 기록이며 최신 명세, 규칙을 대체하지 않는다. 각 README의 "현재 상태"를 먼저 본다.

[전체 문서 안내](../README.md)
