# 권한 범위별 RAG 2-Stage Cache Implementation Plan

> **For agentic workers:** 구현은 각 작업의 실패 테스트 → 최소 변경 → 통과 확인 순서로 진행한다. 커밋은 사용자 명시 지시 전까지 하지 않는다.

**Goal:** 팀 접근 범위를 지키는 LightRAG 응답에 Redis Exact/Semantic 캐시를 적용한다.

**Architecture:** API가 권한 범위를 확인하고 인증된 내부 요청으로 AI에 전달한다. AI는 LightRAG 후보를 원본 DB 권한으로 재검증한 뒤 LLM에 허용 본문만 주고, 같은 권한 범위의 응답만 Redis에서 재사용한다.

**Tech Stack:** Spring Boot, FastAPI, LightRAG 1.4.10, Redis Open Source 8.10 Search, redis-py 7.4, pytest/JUnit.

**Spec:** `docs/superpowers/specs/2026-10-04-rag-scope-cache-design.md`

## Global Constraints

- `allowedTeamIds`는 API가 principal에서 산출한다. 누락은 거절, 명시적 `null`은 인증된 전체 권한으로만 허용한다.
- Exact → Semantic(768 FLOAT32, cosine ≥ 0.90) → RAG. TTL 24시간. namespace version 자리만 두고 무효화는 제외한다.
- 비스트리밍에만 캐시를 적용한다. SSE는 권한 검사만 공유한다.
- 원본 DB가 확인한 업무일지 본문 외 KG 병합 설명을 LLM에 전달하지 않는다.
- 운영 Redis 볼륨/컨테이너 변경과 git commit/push는 실행하지 않는다.

## Review Focus

- 직접 AI 호출의 팀 목록 위조 및 `null`/누락 해석.
- 혼합 팀 KG entity·선행 업무 설명이 LLM으로 전달되는지.
- 캐시 범위가 다른 사용자 간 Exact/Semantic 교차 적중.
- Redis 장애·미지원 Redis Search에서 권한 없는 폴백이 발생하는지.
- 빈 권한·삭제 문서·출처 불명 후보 처리.

### Task 1: 내부 질의 권한 경계

**Files:** `api/.../AiWorklogSearchProperties.java`, `api/.../LightRagWorklogSearchClient.java`, `api/src/main/resources/application.yml`, `ai/app/config/settings.py`, `ai/app/light/v3/model/worklog_query.py`, `ai/app/light/v3/router/worklog_query.py`, 관련 테스트, compose 환경변수.

- [x] 토큰 누락/오류와 `allowedTeamIds` 누락 직접 AI 호출의 실패 테스트를 작성·실패 확인한다.
- [x] API→AI 내부 토큰 헤더, AI의 상수시간 비교, 명시적 `allowedTeamIds` 계약을 구현한다.
- [x] API·AI 라우터/클라이언트 테스트를 통과시킨다.

### Task 2: LLM 전 원본 권한 검사

**Files:** `ai/app/light/v3/service/lightrag_adapter.py`, `ai/app/light/v3/service/worklog_query_service.py`, `ai/app/light/v3/store/worklog_source_store.py`, 관련 테스트.

- [x] 팀 밖·삭제 문서·혼합 KG 설명·빈 권한의 LLM 입력 실패 테스트를 작성·실패 확인한다.
- [x] `aquery_data`에서 후보 ID를 수집하고 원본 DB `team_id`로 제한한 본문·references만 조립한다. 독립 생성 함수가 필터링된 입력만 받게 한다.
- [x] 비스트리밍과 SSE의 권한 테스트를 통과시키고, 기존 응답 계약을 검증한다.

### Task 3: 범위별 Redis 2-Stage Cache

**Files:** `ai/app/light/v3/service/worklog_query_cache.py`(신규), `ai/app/light/v3/service/worklog_query_service.py`, `ai/app/config/settings.py`, `ai/.env.example`, 관련 테스트.

- [x] 권한 집합 정규화, Exact 적중, Semantic 범위 필터·0.90 임계값, TTL, Redis 장애 폴백 실패 테스트를 작성·실패 확인한다.
- [x] Redis Search index 생성/검증, Exact `SET EX`, Semantic HASH + VECTOR 검색을 구현하고 서비스에 연결한다.
- [x] Mock/격리 Redis 테스트에서 각 단계의 미호출·범위 격리·장애 폴백을 통과시킨다.

### Task 4: 인프라와 검증

**Files:** `infra/compose.local.yml`, `infra/compose.deploy.yml`, `infra/.env.example`, `docs/infra/*` 해당 운영 가이드, 관련 검증 스크립트.

- [x] Redis 8.10 Search 지원 이미지(`redis:8.10.0-alpine`)와 AI·API 토큰 환경변수를 compose에 반영한다. 운영 업그레이드 전 백업 절차를 문서화한다.
- [x] 로컬 `compose.local.yml`의 `axwms-redis` (6379 포트)를 `redis:8.10.0-alpine`으로 통합 기동하고, `FT.CREATE`·범위 TAG KNN·TTL을 실측 검증한다.
- [x] 대상 pytest, API JUnit, compose config, 전체 AI 테스트를 실행하고, FastAPI 실제 HTTP 엔드포인트 네트워크 왕복(RTT) 벤치마크를 수행하여 증거(Cold 9.08s -> Exact 6.68ms / Semantic 482ms)와 결과를 기록한다.
