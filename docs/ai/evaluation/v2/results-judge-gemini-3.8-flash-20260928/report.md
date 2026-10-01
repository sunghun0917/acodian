# V2 Naive / LightRAG Mix 평가 — 관계 보완 OFF

- 상태: **complete** · 생성 160/160 · 채점 완료 160/160 · 유효 점수 800/800
- 생성 모델: `gemini-3.5-flash-lite` · 평가 모델: `gemini-3.8-flash` · 임베딩: `gemini-embedding-001` · temperature=0
- PostgreSQL 관계 보완, anchor 치환·중복 보정, 추가 Neo4j 확장, reranker: **모두 OFF**. Mix 내장 그래프 검색은 ON.
- 기존 200건 인덱스 재사용. 재인덱싱/삽입 없음. 기존 검색과 동일한 top_k=10, chunk_top_k=5.
- 80문항 모두 naive/mix 실행. 문항별 순서 교차, 응답 캐시 OFF, 검색 키워드 캐시 재사용 가능.
- 실제 생성 프롬프트·전체 문맥·본문 청크·답변·사용량·시간은 문항 JSON에 보존. 추가 판정 trace는 수집하지 않음.

## 분류별 평균

점수는 0~1, 높을수록 좋음. 각 셀은 **평균 (유효 n/대상)**. 실패는 0점으로 넣지 않음.

| 유형 | 모드 | Faithfulness | Answer relevancy | Chunk precision | Context recall | Factual F1 |
|:---|:---|---:|---:|---:|---:|---:|
| Overall (전체 80문항) | naive | 0.9542 (80/80) | 0.2413 (80/80) | 0.3992 (80/80) | 0.5813 (80/80) | 0.3124 (80/80) |
| Overall (전체 80문항) | mix | 0.9171 (80/80) | 0.8488 (80/80) | 0.3644 (80/80) | 0.7771 (80/80) | 0.2607 (80/80) |
| **Main Overall (Verified Absence 제외 72문항)** | **naive** | **0.9560 (72/72)** | **0.2681 (72/72)** | **0.3389 (72/72)** | **0.5347 (72/72)** | **0.3110 (72/72)** |
| **Main Overall (Verified Absence 제외 72문항)** | **mix** | **0.9148 (72/72)** | **0.9045 (72/72)** | **0.3100 (72/72)** | **0.7523 (72/72)** | **0.2607 (72/72)** |
| simple_fact | naive | 0.9417 (20/20) | 0.8739 (20/20) | 0.9517 (20/20) | 1.0000 (20/20) | 0.2970 (20/20) |
| simple_fact | mix | 0.9583 (20/20) | 0.8920 (20/20) | 0.9394 (20/20) | 1.0000 (20/20) | 0.1165 (20/20) |
| direct_relation | naive | 1.0000 (20/20) | 0.0461 (20/20) | 0.0417 (20/20) | 0.3833 (20/20) | 0.2460 (20/20) |
| direct_relation | mix | 0.8958 (20/20) | 0.9406 (20/20) | 0.0433 (20/20) | 0.7000 (20/20) | 0.3175 (20/20) |
| multi_relation | naive | 0.9583 (20/20) | 0.0000 (20/20) | 0.1350 (20/20) | 0.2917 (20/20) | 0.3815 (20/20) |
| multi_relation | mix | 0.8882 (20/20) | 0.8706 (20/20) | 0.0667 (20/20) | 0.5333 (20/20) | 0.2505 (20/20) |
| dependency_closure | naive | 0.9028 (12/12) | 0.0753 (12/12) | 0.1528 (12/12) | 0.4167 (12/12) | 0.3250 (12/12) |
| dependency_closure | mix | 0.9181 (12/12) | 0.9216 (12/12) | 0.1111 (12/12) | 0.7917 (12/12) | 0.4233 (12/12) |
| verified_absence (분리 집계) | naive | 0.9375 (8/8) | 0.0000 (8/8) | 0.9417 (8/8) | 1.0000 (8/8) | 0.3250 (8/8) |
| verified_absence (분리 집계) | mix | 0.9375 (8/8) | 0.3474 (8/8) | 0.8542 (8/8) | 1.0000 (8/8) | 0.2612 (8/8) |

> **💡 Verified Absence 제외 이유**:
> - `verified_absence`(8문항)는 등록된 관계가 없음을 확인하는 부재 검증 문항입니다. 모델이 완곡하게 "확인되지 않는다/알 수 없다"로 답변하면 Ragas의 `AnswerRelevancy`가 이를 비단정적(`noncommittal=1`)으로 판정하여 코사인 유사도와 무관하게 **강제 0점(`score = cosine_sim * 0`)**을 부과하는 구조적 한계가 있습니다.
> - 이로 인해 naive 0.0000, mix 0.3474로 산출되어 전체 평균을 크게 왜곡하므로, 긍정 관계 탐색 및 사실 답변에 대한 순수 RAG 성능을 정확히 측정하기 위해 이를 분리한 **Main Overall(72문항)** 지표를 제공합니다.



## 대응 문항 차이: Mix − Naive

| 유형 | 지표 | 평균 차이 | 대응 n |
|:---|:---|---:|---:|
| simple_fact | faithfulness | +0.0167 | 20 |
| simple_fact | answer_relevancy | +0.0181 | 20 |
| simple_fact | context_precision_chunks | -0.0122 | 20 |
| simple_fact | context_recall | +0.0000 | 20 |
| simple_fact | factual_correctness | -0.1805 | 20 |
| direct_relation | faithfulness | -0.1042 | 20 |
| direct_relation | answer_relevancy | +0.8944 | 20 |
| direct_relation | context_precision_chunks | +0.0017 | 20 |
| direct_relation | context_recall | +0.3167 | 20 |
| direct_relation | factual_correctness | +0.0715 | 20 |
| multi_relation | faithfulness | -0.0701 | 20 |
| multi_relation | answer_relevancy | +0.8706 | 20 |
| multi_relation | context_precision_chunks | -0.0683 | 20 |
| multi_relation | context_recall | +0.2417 | 20 |
| multi_relation | factual_correctness | -0.1310 | 20 |
| dependency_closure | faithfulness | +0.0153 | 12 |
| dependency_closure | answer_relevancy | +0.8464 | 12 |
| dependency_closure | context_precision_chunks | -0.0417 | 12 |
| dependency_closure | context_recall | +0.3750 | 12 |
| dependency_closure | factual_correctness | +0.0983 | 12 |
| verified_absence | faithfulness | +0.0000 | 8 |
| verified_absence | answer_relevancy | +0.3474 | 8 |
| verified_absence | context_precision_chunks | -0.0875 | 8 |
| verified_absence | context_recall | +0.0000 | 8 |
| verified_absence | factual_correctness | -0.0638 | 8 |

## 시간과 오류

| 모드 | 검색 평균 초 (n) | 검색+생성 평균 초 (n) |
|:---|---:|---:|
| naive | 0.5047 (80/80) | 1.8105 (80/80) |
| mix | 1.8367 (80/80) | 3.2899 (80/80) |

- 생성 오류: 0, 채점 오류: 0. 상세는 각 rows JSON의 오류 필드 확인.
- API 대기/재시도/캐시가 시간에 영향을 준다. 운영 HTTP 지연 또는 순수 검색 엔진 성능이 아니다.

## 해석 범위와 제한

- 관계 부재 8문항은 answerable=true인 빈 관계 정답이며 거절 문항이 아니다. verified_absence로 주 관계 평균과 분리한다.
- Chunk precision은 본문 청크만, Faithfulness/Recall은 그래프를 포함한 실제 생성 문맥을 평가한다.
- Factual F1은 자동 사실 비교이며 정답률이 아니다. 정답 ID/경로/목록의 결정적 채점은 이 실행기에 아직 포함하지 않았다.
- Naive 본문과 Mix 그래프의 정보량이 다르므로 시스템 구성 비교이며 그래프 구조만의 효과가 아니다.
- V1 질문 15개 재사용, 동일 합성 코퍼스 노출. 독립 holdout 일반화 증거가 아니다.
- 제목+시작 업무 ID로 대상을 지정하여 식별 난도가 낮다. 연관 문항이 있어 80개 독립 표본으로 해석하지 않는다.
- 전체 탐색/관계 부재는 원천 200건의 유도 부분그래프 범위. 운영 전체/권한/실시간 상태/분기·순환 성능은 평가하지 않는다.
- 1회 채점으로 judge 변동성/신뢰구간을 측정하지 않았다. 단일 전체 평균으로 유형 차이를 숨기지 않는다.
- 로컬 200건 원문·처리 상태 해시를 검증한다. 외부 Neo4j/Qdrant 무결성은 별도 preflight 증거가 필요하다.
- 생성에는 질문과 검색 문맥만 사용한다. reference와 validation-details의 gold는 검색/생성에 주입하지 않는다.
