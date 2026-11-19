# RAG 답변 캐시와 증분 재색인

> **현재 상태:** 구현 완료 (2026-10-06, v3 기준. v3 코드는 2026-11-13 커밋 `32c18af5`로 삭제됐다). **하지만 지금 v4 질의 경로에서는 답변 캐시가 꺼져 있다.** 캐시 HIT가 "최신 DB 재조회와 색인 완료 검증"을 건너뛸 수 있어서, 그 검증을 캐시가 지원할 때까지 비활성화했다. `worklog_query_cache.py` 클래스는 v4에 남아 있지만 질의 서비스가 캐시를 쓰지 않고, Celery 색인 작업도 캐시 없이 색인 서비스를 만들어서 아래 4절의 버전 증가(`INCR`)도 지금은 실행되지 않는다. 증분 재색인(문서 1건만 삭제 후 재삽입)은 v4에서도 유지된다. 재색인 전달 방식은 이후 Outbox, Celery로 바뀌었다([Celery 전환](../../../api/report/worklog-index-celery/README.md) 참고).
> **기간:** 2026-10-04 (설계) → 10-05~06 (구현, 측정) → 10-11 (실제 임베딩 재측정)
> **한 줄 요약:** 같은 질문이나 비슷한 질문은 Redis에서 바로 답하고(Exact, Semantic 2단계), 팀 권한별로 캐시를 분리했다. 업무가 바뀌면 그 문서만 다시 색인하고, 캐시는 전역 버전 번호를 `INCR` 한 번으로 올려서 무효화한다.

---

## 1. 배경: 무엇이 문제였나

1. **권한 누출 위험:** LightRAG가 전체 지식 그래프와 문서를 검색해서, 다른 팀의 업무 내용이 LLM 입력이나 인용(references)에 들어갈 수 있었다.
2. **반복 질문 비용:** 같은 질문도 매번 임베딩, 그래프 탐색, LLM 생성을 거쳤다(3~8초, API 비용).
3. **수정, 삭제 후 옛 답변(Ghost Read):**
   - 업무가 바뀌어도 Redis에 옛 답변이 남았다.
   - LightRAG의 문서 상태(`kv_store_doc_status.json`) 때문에 같은 ID로 다시 넣으면 재색인이 **건너뛰어졌다.**
   - Neo4j, Qdrant에 삭제된 관계와 벡터가 남았다.

## 2. 목표와 완료 기준

- Spring이 로그인 사용자로 계산한 `allowedTeamIds`를 AI 검색과 캐시 모두의 보안 경계로 쓴다.
- Exact(문자열 일치) → Semantic(코사인 유사도 0.90 이상) → RAG 순서. 성공 응답만 24시간 저장.
- Redis 장애는 RAG로 우회해도 되지만, **권한 범위를 확인 못 하면 실패로 닫는다(fail-closed).**
- 수정, 삭제된 문서 1건만 정리하고 다시 색인한다.
- 키를 하나씩 찾아 지우지 않고 무효화한다.

## 3. 선택지와 결정

### 캐시를 무슨 단위로 나눌까
| 대안 | 문제 | 선택 |
|---|---|---|
| 사용자 ID별 | 같은 권한의 사용자끼리 재사용이 안 돼 적중률 낮음 | X |
| 팀 하나별 | 여러 팀을 보는 사용자의 종합 답변을 구분 못 함 | X |
| 팀별 LightRAG workspace | 색인 중복, 팀 간 관계 손실 | X |
| 캐시 키만 분리, 생성 후 references만 필터 | **LLM 입력 단계에서 이미 누출** | X |
| **허용 팀 집합(정렬, 중복 제거)의 SHA-256을 scope로** + 원본 DB 재검증 | 같은 권한 집합끼리 재사용, 입력 단계부터 차단 | **O** |

### 문서가 바뀌면 어떻게 다시 색인할까
| 대안 | 문제 | 선택 |
|---|---|---|
| Shadow Workspace (변경마다 전체를 별도 폴더에 재색인) | 문서가 늘면 수십 초 지연, LLM 비용 폭증 | **폐기** |
| **`adelete_by_doc_id`로 그 문서만 지우고 다시 넣기** | LightRAG 참조 카운팅으로 공유 노드는 보존 | **O** |

### 캐시 무효화
| 방식 | 1,000건 기준 중앙값 | 단점 | 선택 |
|---|---:|---|---|
| `SCAN` + `DEL` | 39.96ms | 키 수에 비례, Redis 단일 스레드 블로킹 | X |
| 문서별 영향 캐시 역추적 | (미구현) | RAG 답변은 여러 문서, 관계가 섞여 있어 역추적이 사실상 불가능 | X |
| **전역 버전 `INCR`** | **2.54ms (약 15.7배)** | 무관한 캐시까지 모두 MISS (False Invalidation) | **O** |

**포기한 것 (트레이드오프):** 문서 하나만 바뀌어도 모든 캐시가 MISS가 된다. 그래도 "조금 느려도 최신 답변"이 "빠르지만 틀린 답변"보다 낫다고 판단했다. 옛 키는 지우지 않고 24시간 TTL로 자연 만료된다.

## 4. 구현: 어떻게 동작하나

### 질의 흐름

> 아래는 v3 당시 흐름이다. 지금 v4에서는 응답 캐시 단계가 실행되지 않는다(맨 위 "현재 상태" 참고).

```text
Spring API → (X-AI-Internal-Token 상수 시간 검증) → AI
  → scope = SHA-256(정렬된 allowedTeamIds)  ※ null = 전체 권한, [] = 즉시 빈 응답
  → 현재 version을 한 번 읽음 (이 요청 동안 같은 값 사용)
  → [1단계] Exact: GET {prefix}:exact:{version}:{scope}:{signature}
  → [2단계] Semantic: 질문 임베딩(768차원) → FT.SEARCH KNN 1, 같은 scope, cos ≥ 0.90
  → MISS: LightRAG aquery_data로 후보만 찾기
        → 원본 PostgreSQL에서 team_id 권한, is_deleted 재검증
        → 검증된 본문만 Gemini에 전달 → 답변
        → 같은 version 키로 저장 (TTL 24h)
```
- **signature**: 질문 + 검색 옵션(top_k, rerank, 모델 등)의 해시.
- **KG의 병합된 엔티티/관계 설명은 LLM에 넣지 않는다.** 여러 팀 문서가 섞여 있을 수 있기 때문이다.
- 버전 조회에 실패하면 캐시 HIT를 반환하지 않고 RAG로 간다.

### 수정, 삭제 흐름
```text
업무 수정/삭제 → DB 커밋 + Light Outbox 기록
  → 1. adelete_by_doc_id('worklog-{id}')
       └ 실패/timeout → 2단계 중단, 실패 응답 → Outbox 재시도 (Fail-Fast)
  → 2. 최신 DB 내용으로 ainsert() + ainsert_custom_kg()
  → 3. INCR 버전 키
```
- **Dirty Invalidation:** 텍스트는 들어갔는데 KG 저장이 실패한 경우처럼 저장소가 이미 바뀌었으면, 최종 실패여도 버전을 올린다. 옛 답변이 남는 것을 막기 위해서다.
- 모든 삭제가 실패해 저장소가 안 바뀌었으면 버전을 올리지 않는다.

### 핵심 코드 위치
| 역할 | 파일 |
|---|---|
| 캐시 (scope, Exact, Semantic, version) | `ai/app/light/v4/service/worklog_query_cache.py` (현재 질의, 색인 경로 모두 미사용) |
| 질의 서비스 | `ai/app/light/v4/service/worklog_query_service.py` |
| 색인, 재색인 | `ai/app/light/v4/service/worklog_index_service.py` |
| 측정 스크립트 | `ai/benchmarks/semantic_cache_real_embedding.py` |

v4 캐시 클래스의 키 접두사는 `worklog:query:v3:relations-v1`, 인덱스는 `idx:worklog:query:v3:relations-v1`이다. 이름에 v3가 남아 있지만 의도적으로 유지한 이름이다.

## 5. 결과와 검증

### 1,000건 HTTP 측정 (10-06, 로컬 Docker Redis, 각 100회)
| 경로 | 중앙값 | p95 |
|---|---:|---:|
| Exact HIT | 2.65ms | 3.86ms |
| Semantic HIT (임베딩은 fixture) | 3.41ms | 5.63ms |
| MISS (합성 fixture 어댑터) | 4.66ms | 7.15ms |
| 무효화 `SCAN + DEL` | 39.96ms | 58.07ms |
| 무효화 `INCR` | **2.54ms** | 3.91ms |

### 실제 Gemini 임베딩 포함 재측정 (10-11, 각 10회)
| 경로 | 중앙값 |
|---|---:|
| Exact HIT | 2.34ms |
| Semantic HIT | **388.2ms** (그중 Gemini 임베딩 약 379ms, Redis 검색 약 2ms) |

→ **Semantic HIT의 실제 비용은 거의 다 임베딩 호출이다.** 10-06의 3.41ms는 임베딩을 fixture로 대체한 값이라 실제 사용자 체감과 다르다.

### 테스트
- AI 관련 89건 통과 (삭제 실패 시 색인 건너뛰기, 전건 삭제 실패 시 버전 미증가, KG 부분 실패 시 버전 증가 등)
- API 전체 빌드, 테스트 통과 (Flyway V19 Light Outbox 포함)

## 6. 진행 중 바뀐 결정

| 시점 | 내용 |
|---|---|
| 10-04 설계 | 버전 키는 키 형식에만 넣고 무효화는 나중에 하기로 함 (그동안 최대 24시간 옛 답변 가능) |
| 10-05 | 첫 측정은 Python 메서드 호출 시간(47.6ms vs 0.98ms)이었다. "사용자 기준이 아니다"라는 판단으로 **외부 HTTP 왕복 기준으로 다시 측정** |
| 10-05 | 수정 경로(`updateWorklog`)가 재색인 이벤트를 아예 발행하지 않던 결손 발견. 버전만 올려서는 최신 답변이 보장되지 않아 재색인 연결부터 함 |
| 10-05 | LightRAG `ainsert`는 이미 있는 ID를 건너뛴다는 것을 라이브러리 소스에서 확인 → 삭제 후 재삽입 방식으로 |
| 10-06 | 과거 Shadow Workspace 방식 폐기 |
| 10-06 | AI PR 리뷰 지적 반영: 삭제 실패 시 Fail-Fast, 부분 실패 시 Dirty Invalidation |
| 10-11 | 실제 임베딩 포함 Semantic HIT 재측정 → 388ms |
| 10-13 이후 | 권한 필터를 "사후 필터"에서 "팀별 workspace 검색"으로 강화 ([LightRAG v4 전환](../lightrag-v4/README.md) 참고) |
| v4 전환 | **답변 캐시 비활성화.** v4는 후보마다 최신 DB 원본, 색인 팀 소유권, 색인 완료 상태를 검증하는데, 캐시 HIT는 이 검증을 건너뛴다. 캐시가 이 검증을 지원할 때까지 끔 |

## 7. 한계와 위험

- **사후 권한 필터 (10-06 시점):** LightRAG 상위 후보를 뽑은 뒤 권한으로 거르므로, 허용 안 된 후보가 많으면 재현율이 떨어진다. 이후 팀 사전 필터로 개선했다.
- **Semantic 오답 위험:** 질문이 비슷해도 정답이 다를 수 있다. 임계값 0.90의 품질은 따로 평가하지 않았다.
- **재색인 완료 전 공백:** DB 커밋 후 재색인과 버전 증가가 끝날 때까지 옛 답변이 나올 수 있다.
- **INCR 실패:** 색인은 성공했는데 `INCR`이 실패하면 Redis 복구 후 옛 캐시가 다시 유효해질 수 있다.
- **False Invalidation:** 문서 하나 변경에 전체 캐시 MISS. 변경이 잦으면 적중률이 낮다.
- **측정 범위:** 단일 PC, 순차 요청. 실제 적중률, 동시 부하, Spring을 포함한 E2E는 측정하지 않았다.
- **보안:** 내부 토큰 상수 시간 비교. `allowedTeamIds` 누락을 전체 권한으로 해석하지 않는다(누락과 명시적 null 구분). 클라이언트가 팀 목록을 위조할 수 없도록 Spring이 계산해서 내부 토큰 경로로만 보낸다.

## 8. 앞으로 개선할 점

1. **v4에서 답변 캐시 다시 켜기:** 캐시 HIT 시에도 참조 업무의 최신 상태(삭제, 팀 이동, 색인 완료)를 가볍게 확인하는 방법이 필요하다
1. 팀 단위 버전(`version:team:{id}`)으로 무효화 범위 축소
2. 태그, 도메인 단위 버전
3. Semantic 임계값 품질 평가 (비슷한 질문, 다른 정답 사례)
4. Semantic 단계의 임베딩 비용 절감 (임베딩 캐시 등)
5. 완료된 Outbox 레코드 정리 배치

## 9. 스스로 점검 질문

- **Q. 왜 키를 지우지 않고 버전을 올리나?**
  A. `SCAN + DEL`은 키 수에 비례해 느리고 Redis를 블로킹한다. 버전을 키에 포함시키면 `INCR` 한 번으로 옛 키 전부를 "안 보이게" 만들 수 있고, 옛 키는 TTL로 알아서 사라진다.
- **Q. 문서별로 영향받는 캐시만 지우면 더 효율적이지 않나?**
  A. RAG 답변 하나는 여러 문서의 청크와 그래프 관계가 섞여서 만들어진다. 어떤 답변이 어떤 문서에 의존하는지 정확히 추적하기 어렵고, 하나라도 놓치면 옛 답변이 나간다.
- **Q. 캐시 scope를 왜 팀 집합의 해시로 만드나?**
  A. 볼 수 있는 팀이 같은 사용자끼리는 같은 답을 공유해도 안전하다. 순서와 중복을 정리하고 해시하면 같은 집합은 같은 키가 된다.
- **Q. Redis가 죽으면?**
  A. 캐시 없이 RAG로 진행한다(가용성). 하지만 권한 범위를 확인 못 하면 실패로 닫는다(보안). 둘을 구분하는 게 핵심이다.
- **Q. 생성 후에 references만 걸러내면 안 되나?**
  A. LLM이 이미 다른 팀 내용을 읽고 답변에 섞었을 수 있다. 입력 단계에서 막아야 한다.
- **Q. Semantic HIT가 왜 388ms나 걸리나?**
  A. 비슷한 질문을 찾으려면 질문을 먼저 임베딩해야 하고, 그게 외부 Gemini 호출이다. Redis 벡터 검색 자체는 2ms 정도다.

## 10. 원본 문서

| 문서 | 내용 |
|---|---|
| [cache-and-reindex-report-2026-10-06.md](cache-and-reindex-report-2026-10-06.md) | **종합 보고서**: 2단계 캐시, 증분 재색인, INCR 무효화, 1,000건 측정 |
| [semantic-cache-real-embedding-2026-10-11.md](semantic-cache-real-embedding-2026-10-11.md) | 실제 임베딩 포함 재측정 |
| [semantic-cache-real-embedding-2026-10-11-raw.json](semantic-cache-real-embedding-2026-10-11-raw.json) | 재측정 원본 |
| [design-scope-cache-2026-10-04.md](design-scope-cache-2026-10-04.md) | 권한 범위 캐시 설계 |
| [plan-scope-cache-2026-10-04.md](plan-scope-cache-2026-10-04.md) | 구현 계획 |
| `.omx/plans/portfolio-4-redis-namespace-versioning.md` | 버전 무효화 계획 |
| `.omx/plans/worklog-rag-cache-http-reindex-resolution.md` | 재색인 교체 계약, HTTP 측정 기준 계획 |
| `.omx/plans/semantic-cache-real-embedding-benchmark.md` | 실제 임베딩 재측정 계획 |
