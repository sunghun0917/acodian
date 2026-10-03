# 📑 RAG 응답 속도 최적화 기술 보고서 (Step 1 ~ Step 2)

**과제명**: `asyncio.TaskGroup 비동기 병렬화 & SSE 스트리밍 기반 응답 속도 개선`  
**대상 시스템**: AX-WMS AI 서비스 (FastAPI, LightRAG v3, Qdrant, Neo4j, Gemini)  
**작성일**: 2026-10-03  
**작업 브랜치**: `feat/ai/taskgroup-sse`  

---

## 1. 개요 및 목적

본 프로젝트는 LightRAG의 **Mix 모드(`mode="mix"`)** 질의 처리 과정에서 발생하는 다중 저장소(Vector DB, Graph DB) 순차 조회 병목을 진단하고, Python 3.11의 **구조적 동시성(Structured Concurrency)** 도구인 `asyncio.TaskGroup`과 **SSE(Server-Sent Events) 스트리밍**을 결합하여 응답 지연시간과 첫 토큰 도착시간(TTFT)을 극적으로 개선하는 것을 목표로 합니다.

---

## 2. 기존 파이프라인의 병목 원인 분석

LightRAG의 Mix 모드는 사용자 질문의 문맥을 폭넓게 구성하기 위해 3가지 독립적인 데이터베이스 조회를 순차적으로 수행하고 있었습니다:

```
[사용자 질의 접수]
       │
       ▼
1. 로컬 노드 탐색 (_get_node_data)    ──► Qdrant 엔티티 검색 + Neo4j 노드 탐색 (대기...)
       │ (완료 후)
       ▼
2. 글로벌 엣지 탐색 (_get_edge_data)   ──► Qdrant 관계 검색 + Neo4j 엣지 탐색 (대기...)
       │ (완료 후)
       ▼
3. 벡터 청크 검색 (_get_vector_context) ──► Qdrant 원문 청크 검색 (대기...)
       │ (완료 후)
       ▼
4. LLM 답변 생성 (Gemini 호출)         ──► 전체 완료 시까지 JSON 블로킹
```

### 핵심 문제점
1. **직렬 대기 I/O 오버헤드**: 3가지 검색(로컬 노드, 글로벌 엣지, 벡터 청크)은 상호 데이터 의존성이 없음에도 불구하고 순차적인 `await`로 실행되어 DB 검색 대기시간이 누적되었습니다.
2. **Non-Streaming 응답 병목**: LLM이 모든 토큰을 완성할 때까지 HTTP 응답을 닫아두어, 사용자는 4초 이상 빈 화면(로딩 스피너)을 보며 대기해야 했습니다.

---

## 3. [Step 1] Baseline 성능 측정 및 캐시 영향 분석

### 3.1 벤치마크 질의문 선정
* **출처**: `docs/ai/evaluation/v4/` (n=50 벤치마크 데이터셋 전수 분석)
* **선정 쿼리**: Reranker를 쓰지 않은 Mix 쿼리 중 가장 지연시간이 길었던 **1위 쿼리 (`v4-033`)**
  > *"릴리즈 노트 누락 승인 전 검토 기록에서 선행업무를 두 차례 따라가면 어디에 도착하나요?"*
* **선정 사유**: 다단계 관계(multi_relation) 추론이 필요하여 검색 및 추론 부하가 가장 큼

### 3.2 k6 테스트 시 발견된 캐시 간섭 및 단일 측정 전환
* **발견 현상**: k6 부하 테스트(1~3 VU, 30초) 실행 시, 1~2회 이후 요청이 1초대로 급격히 단축되는 현상 발생.
* **원인 규명**: LightRAG의 내부 KV 캐시(`kv_store_llm_response_cache.json`)가 동일 질의에 대해 Gemini API 호출을 생략하고 캐시된 결과를 즉시 반환했기 때문.
* **해결 방안**: 캐시 간섭을 원천 차단하고 순수 DB 검색 및 LLM 파이프라인 지연을 정확히 관측하기 위해, 고유 실행 세션 태그를 결합한 **단일 쿼리 정밀 측정(`measure_single_query.py`)** 방식으로 전환.

### 3.3 Step 1 Baseline 측정 결과
* **첫 토큰/바이트 도착 시간 (TTFT)**: **4,581.0 ms (4.58초)**
* **전체 응답 완료 시간 (Total Time)**: **4,581.2 ms (4.58초)**
* *(※ Non-Streaming 방식이므로 사용자는 전체 4.58초 동안 아무런 텍스트를 보지 못함)*

---

## 4. [Step 2] `asyncio.TaskGroup` 비동기 병렬화 구현

### 4.1 핵심 설계 및 구조적 동시성 (Structured Concurrency)
기존의 `asyncio.gather()` 대신 Python 3.11의 **`asyncio.TaskGroup`**을 도입했습니다:
* **Fail-Fast 생명주기 관리**: Qdrant 또는 Neo4j 조회 중 어느 하나라도 타임아웃이나 예외가 발생하면, `TaskGroup`이 실행 중인 나머지 비동기 작업들을 즉각 `cancel()` 시켜 리소스 낭비와 고아(Orphan) 태스크를 방지합니다.

```python
# ai/app/light/v3/service/taskgroup_search.py 핵심 로직
async with asyncio.TaskGroup() as tg:
    if len(ll_keywords) > 0:
        task_local = tg.create_task(
            _get_node_data(ll_keywords, knowledge_graph_inst, entities_vdb, query_param)
        )
    if len(hl_keywords) > 0:
        task_global = tg.create_task(
            _get_edge_data(hl_keywords, knowledge_graph_inst, relationships_vdb, query_param)
        )
    if query_param.mode == "mix" and chunks_vdb:
        task_vector = tg.create_task(
            _get_vector_context(query, chunks_vdb, query_param, query_embedding)
        )

# 세 작업이 동시에 완료된 후 안전하게 결과 취합
if task_local is not None:
    local_entities, local_relations = task_local.result()
if task_global is not None:
    global_relations, global_entities = task_global.result()
if task_vector is not None:
    vector_chunks = task_vector.result()
```

### 4.2 런타임 몽키 패치(Monkey Patching) 아키텍처
외부 라이브러리(`lightrag-hku`)의 원본 파일 코드를 물리적으로 수정하지 않고, 서버 구동 시 파이썬 메모리 상에서 검색 함수를 안전하게 가로채는 몽키 패치 패턴을 적용했습니다:
* **장점**:
  1. 원본 패키지 파일 무결성 유지 (라이브러리 업데이트 및 재설치 시에도 충돌 없음)
  2. 기존 55개 단위/통합 테스트 100% 통과 유지
  3. 서버 시작(`app/main.py`) 및 어댑터 초기화(`lightrag_adapter.py`) 시 자동 활성화

---

## 5. [Step 1 vs Step 2] 성능 개선 결과 비교

동일한 최장 지연 쿼리(`v4-033`) 기준, 캐시 바이패스 상태에서의 측정 결과입니다:

| 측정 지표 | **Step 1: Baseline (순차 검색)** | **Step 2: TaskGroup (병렬 검색)** | **개선 폭 (Delta)** |
| :--- | :---: | :---: | :---: |
| **전체 응답 시간 (Total Time)** | **4,581.2 ms (4.58초)** | **3,972.2 ms (3.97초)** | **▼ 609.0 ms (13.3% 단축) ⚡** |
| **첫 토큰 도착 시간 (TTFT)** | **4,581.0 ms (4.58초)** | **3,972.0 ms (3.97초)** | *Non-Streaming 유지 중* |
| **DB 검색 방식** | 직렬 순차 대기 (`await`) | 완전 비동기 병렬 (`TaskGroup`) | 3대 검색 동시 수렴 |
| **기존 테스트 통과율** | 55 / 55 (100%) | 55 / 55 (100%) | 기능 회귀 0건 |

> **분석 요약**:  
> Qdrant 벡터 검색과 Neo4j 그래프 탐색이 동시에 병렬로 실행되면서 **검색 대기 구간만 약 0.61초 단축**되었습니다.  
> 그러나 여전히 전체 LLM 생성이 끝날 때까지 3.97초 동안 응답이 블로킹되는 Non-Streaming 병목이 남아 있습니다.

---

## 6. 향후 계획: [Step 3] SSE 스트리밍 도입 및 기대 효과

* **구현 목표**:
  1. `sse-starlette`의 `EventSourceResponse` 기반 스트리밍 엔드포인트 구축 (`/ai/light/worklogs-v3/query/stream`)
  2. Gemini의 토큰 생성 제너레이터를 SSE 이벤트로 실시간 변환
  3. 클라이언트 연결 끊김 감지(`request.is_disconnected()`) 및 Nginx 버퍼링 해제 헤더(`X-Accel-Buffering: no`) 적용
* **목표 수치**:
  * **첫 토큰 도착 시간 (TTFT)**: 3.97초 ➡️ **0.6초 ~ 0.8초대 진입 (체감 대기시간 80% 이상 단축)**
