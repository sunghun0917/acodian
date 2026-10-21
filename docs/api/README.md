# API 문서 안내

Spring Boot API의 설계 규칙, 명세, 검증 기록을 관리한다. 코드 작업 규칙은 [api/AGENTS.md](../../api/AGENTS.md)를 먼저 읽는다.

## 규칙·개발 가이드

- [설계 결정 기록](adr.yaml)
- [코드 컨벤션](code-convention.yaml)
- [패키지 구조](api-springboot-package-structure-guide.md)
- [엔드포인트 체크리스트](api-springboot-endpoint-checklist.md)
- [엔드포인트·클래스 매핑](api-springboot-endpoint-class-mapping.md)
- [jOOQ 코드 생성 정책](jooq-codegen-policy.md)

## API 명세

[명세 인덱스](spec/api-spec-index.md)에서 도메인별 명세로 이동한다. 공통 응답·규약은 [공통 명세](spec/api-spec-common.md)를 참고한다.

## 구현·검증 기록

- [업무일지 생성 Outbox 검증](report/worklog-create-outbox-validation.md)
- [업무일지 의존성 낙관적 락 보고서](report/worklog-dependency-optimistic-lock-report.md)
- Outbox 반복 측정 원본: [CSV](report/worklog-outbox-5x-timing-2026-10-18.csv) · [JSON](report/worklog-outbox-5x-timing-2026-10-18.json)

보고서는 검증 당시의 기록이며 최신 명세·규칙을 대체하지 않는다. [전체 문서 안내](../README.md)
