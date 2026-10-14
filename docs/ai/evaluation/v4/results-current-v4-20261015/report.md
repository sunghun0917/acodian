# 시작 시점 LightRAG v4 · V4 벤치마크 Ragas 평가

> 실행 도중 소스 변경으로 종료 동일성 검증이 실패했다. 이 점수는 시작 시점 코드의 결과이며 **최신 코드 평가로 간주하지 않는다**.
> 유료 답변/지표는 보존했다. 기존 계약을 바꾸거나 자동 유료 재실행하지 않았다.

- 상태: **complete** · 생성 50/50 · 채점 완료 50/50 · 유효 점수 150/150
- 호출: `app.light.v4` · `mix` · 전사 `allowedTeamIds=null` · Redis/native 답변 캐시 OFF.
- 답변: `gemini-3.6-flash` · judge: `gemini-3.8-flash` · 임베딩: `gemini-embedding-001`.
- 실제 옵션: `{"mode": "mix", "top_k": 10, "chunk_top_k": 5, "enable_rerank": false, "rerank_model": "bongsoo/klue-cross-encoder-v1", "response_type": "Multiple Paragraphs", "allowed_team_ids": null}`.
- 기존 200건 인덱스 재사용. 삽입·삭제·재색인 없음. 실제 v4 서비스 프롬프트/답변 모델 사용.
- 실제 문맥·프롬프트·답변·참조·오류·시간을 rows JSON에 보존. 정답/reference는 생성에 전달하지 않는다.

## 분류별 / split별 평균

| 범위 | Faithfulness | Answer relevancy | Context recall |
|:---|---:|---:|---:|
| Overall | 0.7682 (50/50) | 0.8961 (50/50) | 0.8617 (50/50) |
| simple_fact | 0.8607 (10/10) | 0.9170 (10/10) | 1.0000 (10/10) |
| direct_relation | 0.7416 (12/12) | 0.9292 (12/12) | 1.0000 (12/12) |
| multi_relation | 0.7336 (14/14) | 0.8953 (14/14) | 0.6429 (14/14) |
| dependency_closure | 0.7096 (8/8) | 0.8613 (8/8) | 0.7604 (8/8) |
| verified_absence | 0.8262 (6/6) | 0.8432 (6/6) | 1.0000 (6/6) |
| development | 0.7941 (25/25) | 0.9076 (25/25) | 0.8433 (25/25) |
| evaluation | 0.7423 (25/25) | 0.8846 (25/25) | 0.8800 (25/25) |

## 시간과 오류

- retrieval_seconds: 1.684초 (n=50)
- total_seconds: 10.793초 (n=50)
- 생성 오류 0건, 채점 오류 0건. 실패는 0점으로 넣지 않는다.

## 해석 범위와 제한

- Context Precision/Factual Correctness는 채점하지 않는다. 그래프 문맥 단위와 이름/ID 표기 차이로 기존 점수도 확정 정답률은 아니다.
- 6개 관계 부재 문항은 답변 가능한 빈 목록 정답이며 verified_absence로 별도 집계한다.
- 주평가도 그래프 경로/방향/집합의 결정적 판정은 아니다. structured_gold는 생성에 사용하지 않는다.
- 합성 200건에서 파생한 50문항으로 독립 holdout이 아니다. development/evaluation 각 25문항을 구분한다.
- 전사 normal query만 평가. 팀 권한/SSE/운영 전체 데이터는 검증하지 않는다.
- 이전 v3와 프롬프트·모델·인덱스·파이프라인이 다를 수 있어 전체 시스템 비교이며 개별 변경의 인과 효과가 아니다.
- 지연에는 모델 호출/재시도가 포함된다. judge 1회 채점으로 변동성/신뢰구간은 측정하지 않았다.
- v4 adapter는 생성 token 사용량을 반환하지 않으므로 generation_usage=null. 정확한 비용은 provider 청구 내역에서 확인한다.
