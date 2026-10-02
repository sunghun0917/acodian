# AX-WMS 공통 아키텍처 및 인터페이스 원칙

모든 AI 에이전트(Codex, Antigravity)는 모듈 간 작업 시 다음 원칙을 따릅니다.

## 1. 모듈 경계 준수
- `api/` (Spring Boot): 비즈니스 규칙, 조직/업무/권한/파일/알림 시스템 기준.
- `ai/` (FastAPI): AI 파이프라인(요약, 태그, 청킹, 임베딩, 시맨틱 검색, 리랭킹).
- `web/` (Next.js): 사용자 UI 및 프론트엔드 인터랙션.
- `infra/` (Docker/Nginx): 배포 및 게이트웨이 인프라.

## 2. 인터페이스 계약 우선 원칙 (Contract-First)
- `api/`와 `ai/` 간의 통신은 사전에 합의된 HTTP/REST 규격을 엄격히 준수합니다.
- Spring Boot의 DTO 필드명(CamelCase)과 FastAPI의 Pydantic Schema(snake_case) 간 매핑 규칙을 사전에 검증합니다.
- 한쪽 모듈에서 임의로 스키마를 변경하여 상대 모듈의 런타임 장애(422/500 에러)를 유발하지 않습니다.
