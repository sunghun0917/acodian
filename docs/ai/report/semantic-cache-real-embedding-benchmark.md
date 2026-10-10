# Semantic Cache 실임베딩 HTTP 측정 (2026-10-11)

## 결과

| 경로 | 표본 | HTTP 왕복 중앙값 | 최소–최대 | 포함 단계 |
|---|---:|---:|---:|---|
| Semantic HIT | 10 | **388.203 ms** | 367.710–401.183 ms | Exact 조회, 실제 Gemini 질문 임베딩, Redis 벡터 검색, 응답 |
| Exact HIT | 10 | **2.338 ms** | 2.216–2.821 ms | Redis Exact 조회, 응답; Gemini 호출 없음 |

두 경로 모두 준비 요청 2회 후 순차적으로 10회씩 측정했다. `n=10`이므로 p95나 SLA를 산출하지 않는다. 이 값은 **로컬 AI FastAPI 엔드포인트**의 HTTP 왕복이며, Spring API 인증·권한 조회·네트워크 게이트웨이까지 포함한 사용자 E2E가 아니다. 실제 캐시 MISS/RAG 답변 생성 시간도 아니다.

## 측정 경로와 검증

- 재현 준비: `docker run -d --name axwms-semantic-bench-20261011 -p 127.0.0.1:6388:6379 redis:8.10.0-alpine --save '' --appendonly no` (빈 전용 컨테이너만 사용). 측정 후 `docker stop axwms-semantic-bench-20261011` 및 `docker rm axwms-semantic-bench-20261011`.
- 실행: `cd ai && .venv/Scripts/python.exe -m benchmarks.semantic_cache_real_embedding run` (`ai/.env`의 Gemini 키 필요; 기존 원시 JSON이 있으면 덮어쓰지 않고 중단).
- 측정 코드: [`ai/benchmarks/semantic_cache_real_embedding.py`](../../../ai/benchmarks/semantic_cache_real_embedding.py)
- 원시 표본·계수: [`semantic-cache-real-embedding-raw.json`](semantic-cache-real-embedding-raw.json)
- 환경: 개발자 PC, Python 3.11.6, FastAPI 0.136.0, `httpx` 0.28.1, Redis 8.10.0 전용 Docker 컨테이너(`127.0.0.1:6388`), `gemini-embedding-001` 768차원, 동시성 1. 프로젝트 권장 Python 3.12와는 다르므로 결과를 배포 환경에 일반화하지 않는다.
- 별도 프로세스의 FastAPI에 제품 `POST /ai/light/worklogs-v3/query` 라우터와 `LightWorklogQueryService`를 연결했다. Redis는 실제 `WorklogQueryCache`의 `FT.SEARCH`를 사용했다. 벤치마크 전용 wrapper는 시간·호출 횟수만 수집하고, LightRAG adapter는 호출 시 오류를 내도록 차단했다.
- 질문 A와 다른 문자열인 질문 B를 Gemini로 각각 한 번 임베딩했다. cosine 유사도 **0.945944**를 확인하고 A의 답변을 고유 marker로 Redis에 저장했다. 이후 B 요청은 Exact MISS → 실임베딩 → Semantic HIT를, A 요청은 Exact HIT를 확인했다. 모든 응답이 저장한 marker와 일치했다.
- Gemini 호출 총 **14회** = 사전 유사도 검증 2회 + Semantic 준비 2회 + Semantic 측정 10회. Exact 준비·측정 12회 동안 호출 수는 늘지 않았다. Semantic HIT 12회, LightRAG adapter 진입 0회였다. Redis는 빈 전용 컨테이너만 사용했고 공유 데이터를 삭제하지 않았다.
- 계측된 Semantic 측정 10회의 Gemini `embed()` 중앙값은 **379.099 ms**, Redis `get_semantic()` 중앙값은 **2.173 ms**였다. 이 내부 구간은 HTTP 전체와 단순 합산하는 독립 비용 분해가 아니며 계측 오버헤드를 포함한다.

## 해석과 한계

과거 보고서의 **Semantic HIT p95 5.63 ms**는 실제 Redis 벡터 검색을 포함했으나 질문 임베딩을 fixture로 대체한 100회 로컬 HTTP 실험이다. 이번 **388.203 ms**는 실 Gemini 임베딩을 포함한 다른 조건의 10회 실험이다. 두 수치를 직접 비교해 개선·퇴보율을 주장하지 않는다. Exact HIT와 Semantic HIT도 수행 단계가 다르므로 단순 속도 경쟁으로 해석하지 않는다.

질문 한 쌍, 저장된 답변 한 건, 단일 PC·단일 Redis, 순차 요청, 일시적인 Gemini 네트워크 상태에 대한 결과다. 실제 데이터 분포에서의 캐시 적중률, 동시 부하, Qdrant·Neo4j 검색, LLM 생성, Spring API까지의 E2E는 측정하지 않았다. 표본 10개로 운영 지연의 상한을 추정할 수 없다.
