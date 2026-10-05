# 업무일지 캐시 HTTP 벤치마크 (2026-10-06)

## 목적

- 질문: 격리된 Redis에서 업무일지 질의의 Exact HIT, Semantic HIT, MISS 및 두 무효화 방식의 **외부 HTTP 클라이언트 관측 지연**은 얼마인가?
- 경계: `httpx` 클라이언트에서 요청 직전 `perf_counter_ns()`부터 응답 본문 수신 완료까지. 서버 내부 메서드 시간, fixture 생성·검증·정리 시간은 포함하지 않는다.
- 성공 기준: 각 경로 100회 HTTP 2xx, 예상 캐시 경로 확인, 원표본 저장. 이전 Python 메서드 직접 호출 결과와 비교하지 않는다.

## 환경과 격리

- Windows 10.0.26200, Python 3.11.6, FastAPI/uvicorn 및 `httpx`는 `ai/.venv`의 프로젝트 설치본. Docker Desktop의 독립 `redis:8.10.0-alpine` 컨테이너를 `127.0.0.1:6388`에만 바인딩했다.
- HTTP 서버와 클라이언트는 동일 호스트의 별도 프로세스이며 `127.0.0.1:8099` loopback을 사용했다. 테스트 서버는 운영과 같은 `POST /ai/light/worklogs-v3/query` 라우터·`LightWorklogQueryService`·`WorklogQueryCache`를 호출하되, 외부 Gemini/LightRAG adapter와 embedding만 결정적 fixture로 대체했다.
- 무효화는 **테스트 전용** `POST /bench/invalidate/{scan_del|incr}` 경로다. 운영 API 성능으로 해석하지 않는다. 서버는 UUID 기반 자체 Redis namespace만 사용하고, `FLUSHDB` 및 운영 Redis(6379), Qdrant, Neo4j에 접근하지 않았다.
- 단일 클라이언트·동시성 1. 개발자 PC/loopback/합성 모델 fixture이므로 배포망, 실제 모델 호출, 동시 트래픽을 대표하지 않는다.

## 데이터와 파이프라인

- 질의: `{"query":"benchmark worklog summary","allowedTeamIds":null}`을 캐시에 먼저 저장한다. Exact는 동일 문자열, Semantic은 `summarize the benchmark worklog`(서로 다른 signature지만 동일한 결정적 768차원 벡터), MISS는 `benchmark miss 0`부터 `benchmark miss 99`까지 고유 문자열·직교 벡터를 사용한다. 답변은 `fixture answer <호출 번호>`로 식별한다. 실제 업무일지 본문·개인정보는 없다.
- 사전 준비: 기본 질의 1회 seed, 각 질의 경로 10회 준비 호출. 이후 100회씩 측정한다. 캐시 경로 검증은 HTTP 측정 후 별도 `/bench/stats` 호출 및 응답 식별자로 수행했다. 측정 구간의 adapter 호출 100회(모두 MISS), embedding 호출 200회(Semantic+MISS)를 확인했다.
- 무효화 데이터: 매 방식·반복마다 논리 답변 1,000개를 물리 Redis 키 2,000개(Exact 문자열 1,000 + Semantic HASH 1,000)로 준비했다. HASH 예: `scope`(64자리), `version`, `vector`(768×FLOAT32 = 3,072바이트), `response`를 포함한다. TTL 3,600초. 전체 Redis 검색 모집단은 해당 독립 컨테이너이며, SCAN 대상 키는 2,000개, 대상 일치율은 이 시험 namespace 안에서 100%, SCAN count 100이다. 두 방식 모두 10회 준비 호출 뒤, 실행 순서를 번갈아 각 100회 측정했다. seed·결과 확인·정리는 타이머 밖이며 `INCR` 후 남는 이전 키는 자체 namespace에서만 정리했다.
- 서버 프로세스·컨테이너 생성과 cleanup은 타이머 밖이다. 질의 HTTP에는 라우팅, JSON 처리, Redis 왕복, fixture embedding/adapter 실행, 응답 직렬화가 포함된다. 무효화 HTTP에는 테스트 전용 라우팅 및 실제 `invalidate_all()` 또는 `bump_version()` 실행이 포함된다.

## 결과

| HTTP 경로 | 2xx/시도 | 중앙값 (ms) | p95 (ms) | 최소–최대 (ms) |
|---|---:|---:|---:|---:|
| 운영 query 라우터 · Exact HIT | 100/100 | 2.646 | 3.858 | 2.073–4.357 |
| 운영 query 라우터 · Semantic HIT | 100/100 | 3.409 | 5.632 | 2.634–12.417 |
| 운영 query 라우터 · MISS (fixture adapter) | 100/100 | 4.660 | 7.154 | 3.618–11.266 |
| 테스트 전용 무효화 HTTP · SCAN+DEL | 100/100 | 39.959 | 58.072 | 30.961–66.458 |
| 테스트 전용 무효화 HTTP · INCR | 100/100 | 2.540 | 3.905 | 1.903–4.057 |

- [원표본 JSON](http-worklog-cache-benchmark-20261006.json): 각 경로의 100개 클라이언트 RTT, 요약치, 호출 카운터를 보존한다. 표의 수치는 이 JSON에서 읽었다.
- 재현 명령(PowerShell, `C:\acodian`에서):

```powershell
docker run --rm --name axwms-http-bench-20261006-qa -p 127.0.0.1:6388:6379 -d redis:8.10.0-alpine
cd ai
# 별도 터미널에서 서버 실행 (127.0.0.1에만 바인딩)
.venv\Scripts\python.exe -B bench_http_worklog_cache.py server --redis-url redis://127.0.0.1:6388/0 --port 8099
# 클라이언트 터미널에서
.venv\Scripts\python.exe -B bench_http_worklog_cache.py run --base-url http://127.0.0.1:8099 --runs 100 --entries 1000
```

## 해석과 한계

- 표는 **같은 합성 격리 조건 내 HTTP 왕복 시간**만 보여준다. 특히 SCAN+DEL 대비 INCR은 테스트 전용 endpoint의 지연 비교이지 업무일지 수정 API 완료 시간, 질의 전후 개선율, 운영 처리량이 아니다.
- 현재 TTL-only 운영 버전과 신규 버전의 동등 조건 HTTP 전후 측정은 하지 않았다. 실제 Gemini/LightRAG 및 DB 최신성, 업무일지 수정 ACK→최종 인덱싱 완료 시간도 이 시험에 포함되지 않았다. 해당 측정은 격리된 전체 스택과 상태 조회 HTTP 경로가 준비된 뒤 별도로 수행해야 한다.
- 하나의 개발 호스트에서 한 번 수행한 표본이라 OS 스케줄링·로컬 Docker 부하의 영향을 받을 수 있다. 이 값을 SLA나 배포 환경 예측치로 사용하지 않는다.
