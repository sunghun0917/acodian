# AX-WMS 문서 안내

문서는 **업무 영역과 용도**로 분류한다. 처음 참여한다면 [온보딩](shared/onboarding.md) → [아키텍처 가이드](architecture/AX-WMS_기획서_아키텍처가이드.md) → 담당 영역의 안내 순서로 읽는다.

## 주제별 찾아보기

| 분류 | 진입점 | 보관 내용 |
|---|---|---|
| 아키텍처 | [통합 아키텍처 가이드](architecture/AX-WMS_기획서_아키텍처가이드.md) | 서비스 경계·데이터 소유권·처리 흐름, [기존 Word 원본](architecture/AX-WMS_기획서_아키텍처가이드.docx) |
| 데이터베이스 | [ERD 초안](database/ax-wms-erd-draft.sql) | 설계용 SQL 초안. 실제 스키마 변경은 API의 Flyway migration 기준 |
| API | [API 문서 안내](api/README.md) | Spring Boot 규칙, API 명세, 구현·검증 보고서 |
| Web | [Web 문서 안내](web/README.md) | Next.js 규칙, 디자인 토큰, 협업·담당 범위, 작업 기록 |
| AI | [AI 문서 안내](ai/README.md) | FastAPI·LightRAG 가이드, 검색 전략, 평가 데이터·결과·보고서 |
| 인프라 | [인프라 문서 안내](infra/README.md) | Nginx, 배포·CI/CD, 라우팅, 설계 결정 |
| 공통 | [온보딩](shared/onboarding.md) · [로컬 개발 서버](shared/local-dev-server-setup.md) | [보안 정책](shared/security-policy.md), [용어집](shared/glossary.md), [데이터 사전](shared/data-dictionary.md) |
| 설계·구현 계획 | [설계서](superpowers/specs/) · [구현 계획](superpowers/plans/) | 날짜가 포함된 작업별 산출물. 현재 운영 규칙과 구분 |
| 이미지 | [시스템 개요](images/acodian-overview.png) · [AWS 구성도](images/acodian-aws-architecture.png) | 문서에서 사용하는 공용 그림 |
| 과거 기술 참고 | [NestJS 표준 가이드](archive/AX-WMS_NestJS_표준가이드.docx) | 현재 Spring Boot API의 구현 기준이 아닌 과거 기술 문서 |

## 문서 관리·도구

- [문서 구조 및 유지 규칙](docs-strategy.md)
- [스킬 빠른 참조](skills-reference.md)
- [OMX guidance 스키마 계약](guidance-schema.md)

## 읽고 추가할 때

- 영역별 결정과 컨벤션은 각 영역의 `adr.yaml`, `code-convention.yaml`을 읽는다. Word 가이드·과거 작업 기록을 현재 구현 규칙으로 간주하지 않는다.
- 새 문서는 해당 영역에 추가하고, 안내 문서에 상대 링크를 연결한다. 이동할 때는 저장소의 참조 경로도 함께 수정한다.
- AI 평가의 데이터·실행 스크립트·결과는 `ai/evaluation/`에 함께 보존한다. 결과 파일이나 SQL을 정리 목적으로 재생성·실행하지 않는다.
