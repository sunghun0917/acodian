# V4 Naive / LightRAG Mix 평가 — 관계 보완 OFF

- 상태: **incomplete** · 생성 50/100 · 주평가 3지표 채점 완료 50/100 · 유효 점수 150/300
- 생성 모델: `gemini-3.5-flash-lite` · 평가 모델: `gemini-3.8-flash` · 임베딩: `gemini-embedding-001` · temperature=0
- Reranker: ON (bongsoo/klue-cross-encoder-v1).
- 기존 200건 인덱스 재사용. 재인덱싱/삽입 없음. 기존 검색과 동일한 top_k=10, chunk_top_k=5.
- All 50 questions run in both modes; mode order alternates, answer cache is OFF, keyword cache may be reused.
- 실제 생성 프롬프트·전체 문맥·본문 청크·답변·사용량·시간은 문항 JSON에 보존. 추가 판정 trace는 수집하지 않음.

## 분류별 평균

점수는 0~1, 높을수록 좋음. 각 셀은 **평균 (유효 n/대상)**. 실패는 0점으로 넣지 않음.

| 유형 | 모드 | Faithfulness | Answer relevancy | Context recall |
|:---|:---|---:|---:|---:|
| Overall (전체 50문항) | mix | 0.8717 (50/50) | 0.8116 (50/50) | 0.6983 (50/50) |
| **Main Overall (Verified Absence 제외 44문항)** | **mix** | **0.8920 (44/44)** | **0.8496 (44/44)** | **0.6572 (44/44)** |
| simple_fact | mix | 0.9667 (10/10) | 0.8850 (10/10) | 1.0000 (10/10) |
| direct_relation | mix | 0.9444 (12/12) | 0.9252 (12/12) | 0.6667 (12/12) |
| multi_relation | mix | 0.8571 (14/14) | 0.8142 (14/14) | 0.5000 (14/14) |
| dependency_closure | mix | 0.7812 (8/8) | 0.7539 (8/8) | 0.4896 (8/8) |
| verified_absence (분리 집계) | mix | 0.7222 (6/6) | 0.5326 (6/6) | 1.0000 (6/6) |

> **💡 Verified Absence 제외 이유**:
> - `verified_absence`(6문항)는 200건 코퍼스 상 선행 관계가 없음을 확인하는 문항입니다. 모델이 방어적으로 "정보가 확인되지 않는다"고 답했을 때, Ragas의 `AnswerRelevancy` 메트릭이 이를 비단정적/회피형(`noncommittal=1`)으로 분류하여 코사인 유사도와 무관하게 **강제 0점(`score = cosine_sim * 0`)**을 부과하는 도구적 한계가 존재합니다.
> - 이로 인해 `verified_absence`의 Answer Relevancy 평균이 0.5326으로 비정상 왜곡되어 전체 평균을 왜곡하므로, 긍정 관계 탐색 및 사실 질문의 순수 RAG 성능을 객관적으로 평가하기 위해 이를 제외한 **Main Overall(44문항)** 지표를 분리 보고합니다.



## Split means

| Split | Mode | Faithfulness | Answer relevancy | Context recall |
|:---|:---|---:|---:|---:|
| development | mix | 0.8807 (25/25) | 0.7610 (25/25) | 0.6900 (25/25) |
| evaluation | mix | 0.8627 (25/25) | 0.8621 (25/25) | 0.7067 (25/25) |

## 대응 문항 차이: Mix − Naive

| 유형 | 지표 | 평균 차이 | 대응 n |
|:---|:---|---:|---:|
| Overall | faithfulness | N/A | 0 |
| Overall | answer_relevancy | N/A | 0 |
| Overall | context_recall | N/A | 0 |
| simple_fact | faithfulness | N/A | 0 |
| simple_fact | answer_relevancy | N/A | 0 |
| simple_fact | context_recall | N/A | 0 |
| direct_relation | faithfulness | N/A | 0 |
| direct_relation | answer_relevancy | N/A | 0 |
| direct_relation | context_recall | N/A | 0 |
| multi_relation | faithfulness | N/A | 0 |
| multi_relation | answer_relevancy | N/A | 0 |
| multi_relation | context_recall | N/A | 0 |
| dependency_closure | faithfulness | N/A | 0 |
| dependency_closure | answer_relevancy | N/A | 0 |
| dependency_closure | context_recall | N/A | 0 |
| verified_absence | faithfulness | N/A | 0 |
| verified_absence | answer_relevancy | N/A | 0 |
| verified_absence | context_recall | N/A | 0 |

## 시간과 오류

| 모드 | 검색 평균 초 (n) | 검색+생성 평균 초 (n) |
|:---|---:|---:|
| mix | 104.5116 (50/50) | 106.0126 (50/50) |

- 생성 오류: 0, 주평가 채점 오류: 0. 상세는 각 rows JSON의 오류 필드 확인.
- API 대기/재시도/캐시가 시간에 영향을 준다. 운영 HTTP 지연 또는 순수 검색 엔진 성능이 아니다.

## 해석 범위와 제한

- Six verified-absence cases have answerable=true and are not abstention cases; report them separately.
- Context Precision 제외: 본문 청크만 채점하면 Neo4j 엔티티·관계가 빠지고, 전체 문맥을 개별 레코드로 채점하면 엔티티·관계·청크의 단위 차이와 유형별 직렬화 순서가 점수를 왜곡한다. 어느 쪽도 그래프 관계·방향·경로 정확도를 대표하지 않는다.
- 제외된 탐색 지표의 원점수는 행 JSON에 보존했다. 전체 문맥 Context Precision은 0/100건만 재채점됐으며 주평가·모드 비교에서 제외한다.
- Factual Correctness 제외: 짧은 reference와 이름/ID 표기 차이로 실제 정답과 점수가 어긋날 수 있다. 원점수만 보존하며 정답률로 해석하지 않는다.
- 주평가 3지표도 그래프 정답 여부를 직접 판정하지 않는다. 시작 업무 식별, 관계/경로, 최종 집합, 부재의 structured_gold 채점은 아직 수행하지 않았다.
- Naive 본문과 Mix 그래프의 정보량이 다르므로 시스템 구성 비교이며 그래프 구조만의 효과가 아니다.
- Development/evaluation each contain 25 cases and relation components do not cross splits.
- Unique starting-task identification for paraphrased questions has not been independently human-reviewed.
- Fifty questions derive from V2 and reuse the same synthetic corpus; this is not independent holdout evidence.
- Questions omit task IDs and jointly test starting-task identification and relationship traversal; related cases are not 50 independent samples.
- 전체 탐색/관계 부재는 원천 200건의 유도 부분그래프 범위. 운영 전체/권한/실시간 상태/분기·순환 성능은 평가하지 않는다.
- 1회 채점으로 judge 변동성/신뢰구간을 측정하지 않았다. 단일 전체 평균으로 유형 차이를 숨기지 않는다.
- 로컬 200건 원문·처리 상태 해시를 검증한다. 외부 Neo4j/Qdrant 무결성은 별도 preflight 증거가 필요하다.
- 생성에는 질문과 검색 문맥만 사용한다. reference와 validation-details의 gold는 검색/생성에 주입하지 않는다.
