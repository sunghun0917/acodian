# AX-WMS 문서 안내

문서는 **업무 영역과 용도**로 분류한다. 처음 참여한다면 [온보딩](shared/onboarding.md) → [아키텍처 가이드](architecture/AX-WMS_기획서_아키텍처가이드.md) → 담당 영역의 안내 순서로 읽는다.

## 주제별 찾아보기

| 분류 | 진입점 | 보관 내용 |
|---|---|---|
| 아키텍처 | [통합 아키텍처 가이드](architecture/AX-WMS_기획서_아키텍처가이드.md), [업무일지 등록/수정/삭제/조회 흐름](architecture/worklog-flow.md) | 서비스 경계·데이터 소유권·처리 흐름, [기존 Word 원본](architecture/AX-WMS_기획서_아키텍처가이드.docx) |
| 데이터베이스 | [ERD 초안](database/ax-wms-erd-draft.sql) | 설계용 SQL 초안. 실제 스키마 변경은 API의 Flyway migration 기준 |
| API | [API 문서 안내](api/README.md) | Spring Boot 규칙, API 명세, 구현·검증 보고서 |
| Web | [Web 문서 안내](web/README.md) | Next.js 규칙, 디자인 토큰, 협업·담당 범위, 작업 기록 |
| AI | [AI 문서 안내](ai/README.md) | FastAPI·LightRAG 가이드, 검색 전략, 평가 데이터·결과·보고서 |
| 인프라 | [인프라 문서 안내](infra/README.md) | Nginx, 배포·CI/CD, 라우팅, 설계 결정 |
| 공통 | [온보딩](shared/onboarding.md) · [로컬 개발 서버](shared/local-dev-server-setup.md) | [보안 정책](shared/security-policy.md), [용어집](shared/glossary.md), [데이터 사전](shared/data-dictionary.md) |
| 학습 정리 | [분산 트랜잭션과 보상 처리](shared/transaction-study/README.md), [MVCC와 락](shared/mvcc-study/README.md), [색인 워커 병렬화와 락](shared/celery-parallel-study/README.md) | 2PC·Saga·Outbox, S3 보상 흐름, 업무일지 색인의 정합성과 한계. 공유락/배타락, MVCC, 격리 수준. 색인 워커를 여러 개로 늘릴 때의 락 검토(학습용, 구현 아님) |
| 이미지 | [시스템 개요](images/acodian-overview.png) · [AWS 구성도](images/acodian-aws-architecture.png) | 문서에서 사용하는 공용 그림 |
| 과거 기술 참고 | [NestJS 표준 가이드](archive/AX-WMS_NestJS_표준가이드.docx) | 현재 Spring Boot API의 구현 기준이 아닌 과거 기술 문서 |

## 작업별 종합 정리

지금까지 한 개발 작업은 **주제마다 폴더 하나**로 모았다. 폴더의 `README.md`가 그 주제의 종합 정리이고, 같은 폴더에 근거가 되는 원본 보고서와 측정 원본이 있다. 각 README는 같은 순서로 되어 있다: 현재 상태, 배경(문제), 목표, 선택지와 결정(트레이드오프), 구현, 결과, 진행 중 바뀐 결정, 한계와 위험, 개선할 점, 점검 질문, 원본 문서.

**처음 읽을 때는 각 README 맨 위의 "현재 상태"부터 본다.** 나중에 바뀌거나 제거된 내용이 있으면 거기에 적혀 있다.

| 기간 | 주제 | 영역 | 현재 상태 | 폴더 |
|---|---|---|---|---|
| 09-26 ~ 10-15 | RAG 검색 품질과 평가 (RAGAS, 벤치마크 V1~V4, 응답 속도) | AI | 평가 기록 보존 | [ai/report/rag-evaluation](ai/report/rag-evaluation/README.md) |
| 10-04 ~ 10-11 | RAG 답변 캐시와 증분 재색인 (2단계 캐시, 버전 무효화) | AI | 구현 완료, v4에서 답변 캐시는 꺼짐 | [ai/report/rag-cache](ai/report/rag-cache/README.md) |
| 10-07 | 선행 업무 등록 동시성 제어 (낙관적 락) | API | 완료 | [api/report/worklog-dependency-lock](api/report/worklog-dependency-lock/README.md) |
| 10-13 ~ 10-24 | LightRAG v4 전환 (팀별 검색, PostgreSQL 저장소, 1단계 안정화) | AI | 완료, 현재 서비스 코드 | [ai/report/lightrag-v4](ai/report/lightrag-v4/README.md) |
| 10-18 ~ 10-29 | 업무일지 AI 후처리 Outbox (도입, 역할 분리) | API | 완료, 11-12에 작업별 Outbox로 변경 | [api/report/worklog-outbox](api/report/worklog-outbox/README.md) |
| 10-17 ~ 10-29 | 업무일지 색인 비동기화 (Celery 전환, Worker 분리) | API, AI | 완료, 11-12에 일부 정리 | [api/report/worklog-index-celery](api/report/worklog-index-celery/README.md) |
| 10-30 ~ 11-13 | AI 작업 신뢰성 (작업별 알림, 수동 재시도, 단순화) | API, AI | 완료, **현재 구조의 기준** | [api/report/ai-task-reliability](api/report/ai-task-reliability/README.md) |
| 11-17 ~ | 전사 인덱스 제거와 팀 인덱스 두 벌 번갈아 쓰기 | AI | **계획, 미구현** (종합 정리 README 없이 설계서와 계획서만 있음) | [ai/report/team-index-mvcc](ai/report/team-index-mvcc/design.md) |

주제 사이의 관계:
- 색인 요청 경로는 **캐시 문서(10-04) → Outbox(10-18) → Celery(10-27) → AI 작업 신뢰성(11-12)** 순서로 바뀌었다. 지금 구조는 마지막 문서가 기준이다.
- 검색과 권한은 **캐시 문서(사후 필터) → LightRAG v4(팀 workspace)** 순서로 바뀌었다.

## 문서 관리·도구

- [문서 구조 및 유지 규칙](docs-strategy.md)
- [스킬 빠른 참조](skills-reference.md)
- [OMX guidance 스키마 계약](guidance-schema.md)

## 읽고 추가할 때

- 영역별 결정과 컨벤션은 각 영역의 `adr.yaml`, `code-convention.yaml`을 읽는다. Word 가이드·과거 작업 기록을 현재 구현 규칙으로 간주하지 않는다.
- 새 문서는 해당 영역에 추가하고, 안내 문서에 상대 링크를 연결한다. 이동할 때는 저장소의 참조 경로도 함께 수정한다.
- AI 평가의 데이터·실행 스크립트·결과는 `ai/evaluation/`에 함께 보존한다. 결과 파일이나 SQL을 정리 목적으로 재생성·실행하지 않는다.
