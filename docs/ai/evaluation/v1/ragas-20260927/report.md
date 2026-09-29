# naive RAG / mix RAGAS 정량 평가 보고서

- 생성 시각(UTC): 2026-09-26T19:52:10.650052+00:00
- 대상: 합성 업무일지 200건, 개발 파일럿 50문항. 독립 holdout 또는 운영 성능 검증이 아님.
- 생성·평가 모델: `gemini-3.5-flash-lite`, 임베딩: `gemini-embedding-001`
- 패키지: `{'ragas': '0.4.3', 'lightrag-hku': '1.4.10', 'google-genai': '1.73.1', 'instructor': '1.3.2'}`
- 두 모드 동일 프롬프트·temperature=0·반복 1회. 문항별 실행 순서를 교차 배치. 응답 캐시 비활성화.
- 검색 키워드 캐시는 활성화되어 재사용 가능하다. 응답은 매번 직접 생성하고 사전 1문항 결과는 전체 실행에서 재사용했다.
- 기존 인덱스를 재사용하며 DB/그래프/벡터 문서를 삽입·삭제하지 않음.
- 검색은 LightRAG only_need_prompt, 생성은 해당 프롬프트를 Gemini에 직접 전달. 운영 HTTP 응답 경로의 부하 시험이 아님.
- 검색 설정: `{'top_k': 10, 'chunk_top_k': 5, 'max_entity_tokens': 3000, 'max_relation_tokens': 3000, 'max_total_tokens': 12000, 'enable_rerank': False, 'stream': False, 'include_references': True, 'only_need_prompt': True, 'response_type': 'Short Korean answer'}`

## 핵심 결론

**현재 파일럿의 본문 질문에서는 naive가 문맥 정밀도·재현율에서 높았으며 mix의 일괄 대체 우위는 확인되지 않았다.** 관계 질문에서는 mix가 일부 관계를 회수했지만 충분한 관계 답변 품질은 확보하지 못했다. 이 결론은 현재 모델·검색 예산·인덱스 구성에만 해당한다.

- 본문 35문항: naive → mix에서 Faithfulness **1.0000 → 0.9857**, 청크 정밀도 **0.6505 → 0.5735**, Context recall **0.9905 → 0.9714**. Answer relevancy는 **0.8671 → 0.8923**으로 증가했다.
- 관계 10문항: Context recall **0.0000 → 0.1833**으로 증가했지만 절대 수준은 낮다. 정답 거절로도 Faithfulness가 높게 나올 수 있어 관계 정답률로 해석하면 안 된다.
- 답변 불가: 두 모드 모두 **5/5** 정답 거절. 대부분 쉬운 범위 밖 질문이므로 강한 환각 억제 능력을 입증하지 않는다.
- 검색+생성 평균 지연: naive **1.63초**, mix **3.69초**(약 **2.26배**). 키워드 캐시가 활성화된 단일 실행의 값이다.
- 완료 여부: **100/100 응답**, **360/360 유효 RAGAS 점수**, **10/10 거절 판정**. 원자료의 생성·채점 오류 및 결측 **0건**.

## 측정 결과

| 범위 | 모드 | 생성 성공/대상 | Faithfulness | Answer relevancy | Chunk context precision | Context recall | 본문 청크 ID recall |
|---|---|---:|---:|---:|---:|---:|---:|
| shared_text | naive | 35/35 | 1.0000 (n=35/35) | 0.8671 (n=35/35) | 0.6505 (n=35/35) | 0.9905 (n=35/35) | 1.0000 (n=35/35) |
| shared_text | mix | 35/35 | 0.9857 (n=35/35) | 0.8923 (n=35/35) | 0.5735 (n=35/35) | 0.9714 (n=35/35) | 0.9714 (n=35/35) |
| graph_enriched | naive | 10/10 | 0.8900 (n=10/10) | 0.1495 (n=10/10) | 0.0000 (n=10/10) | 0.0000 (n=10/10) | 0.8000 (n=10/10) |
| graph_enriched | mix | 10/10 | 0.8800 (n=10/10) | 0.4896 (n=10/10) | 0.0000 (n=10/10) | 0.1833 (n=10/10) | 0.6500 (n=10/10) |

## 대응 문항 차이 (mix − naive)

| 범위 | 지표 | 평균 차이 | 대응 문항 수 |
|---|---|---:|---:|
| shared_text | faithfulness | -0.0143 | 35 |
| shared_text | answer_relevancy | +0.0252 | 35 |
| shared_text | context_precision_chunks | -0.0770 | 35 |
| shared_text | context_recall | -0.0190 | 35 |
| graph_enriched | faithfulness | -0.0100 | 10 |
| graph_enriched | answer_relevancy | +0.3400 | 10 |
| graph_enriched | context_precision_chunks | +0.0000 | 10 |
| graph_enriched | context_recall | +0.1833 | 10 |

## 답변 불가 5문항 및 지연

| 모드 | 정답 거절/유효 채점 | 생성 성공 | 생성 포함 평균/p50/p95 초 |
|---|---:|---:|---:|
| naive | 5/5 (대상 5) | 50/50 | 1.63/1.48/2.27 |
| mix | 5/5 (대상 5) | 50/50 | 3.69/3.51/4.63 |

## 해석 및 한계

- Faithfulness/Context recall은 생성에 실제 전달된 그래프·청크 JSON 기록 전체를 사용한다. 원본 그래프의 추가 필드를 임의로 채점 근거에 보태지 않는다.
- Context precision은 두 모드의 텍스트 청크 순위만 평가한다. 그래프 검색 precision을 측정한 값이 아니다.
- Answer relevancy는 의미적 질문 관련성이지 정답 정확도가 아니다. 본문 청크 ID recall은 chunks의 worklog_id: 표기만 추출하는 보수적 보조 지표로, 그래프 및 출처 anchor의 Worklog: ID는 포함하지 않는다.
- graph_enriched는 mix만 추가 관계 정보에 접근하므로 순수 검색 알고리즘 효과가 아닌 시스템 구성 차이다. 본문 주평가와 합산하지 않는다.
- 답변 불가 문항은 일반 RAGAS 평균에서 제외하고 별도 LLM 거절 판정으로 집계한다. 이 판정은 RAGAS 표준 지표가 아니다.
- 호출 오류/NaN은 0점으로 바꾸지 않으며, 유효 표본 수와 대응 비교 표본 수를 공개한다.
- Faithfulness는 거절 답변에서도 높을 수 있으므로 정답률로 해석하면 안 된다. 정답 정확성 전용 지표는 이번 4지표에 포함하지 않았다.
- 동일 모델이 답변 생성과 채점을 수행하므로 자기평가 편향이 있을 수 있다. 단일 실행, 합성 문장 반복, 쉬운 음성 문항 때문에 일반화·통계적 우열을 주장하지 않는다.
- disambiguation-008의 유사 기록 396/432 모호성을 수정하지 않고 원본 50문항을 유지했다.
- 지연은 직렬 검색·생성 벽시계 시간이며 RAGAS 채점 시간은 제외한다. 캐시·서비스 warm-up 및 API 변동의 영향을 받는다.
- 생성 토큰 사용량만 각 row에 저장한다. 검색 키워드/채점/임베딩을 포함한 총 비용은 계측하지 않았으므로 비용 우열을 주장하지 않는다.

## 재현 및 원자료

- benchmark SHA-256: `b21d7d5faeb67e377424c632ee9d0388dd9eeddf17b02a840aa602f0c18bcc7b`
- corpus SQL SHA-256: `eb825533aaee69447a6192186d303063ead52f22f31d30ca6b70255865791c80`
- `run.json`: 모델·환경·해시·인덱스 확인 기록. `rows/*.json`: 응답, 실제 문맥, 검색 결과, 점수, 오류, 지연.
- 실행: `ai/.venv/Scripts/python.exe -B docs/ai/evaluation/v1/run_ragas.py --output docs/ai/evaluation/ragas-new-run --limit 50`

## 대표 실패 및 채점 해석 사례

| 문항 | 관찰 | 해석 |
|---|---|---|
| `multi_evidence-002` | naive는 274·275 본문을 모두 검색했지만 mix는 275의 보정 작업 본문을 놓쳤다. mix는 해당 부분을 알 수 없다고 답했고 Context recall/Faithfulness가 각각 0.5였다. | 그래프에서 업무 ID가 보이는 것과 필요한 상세 본문을 회수하는 것은 다르다. |
| `multi_evidence-010` | mix는 기준 근거 573 본문을 놓치고 유사 기록 609를 답변에 사용했다. Context recall은 0.5지만 Faithfulness는 1.0이다. | 검색 문맥에 충실한 답변도 질문이 지정한 단계·업무와 다를 수 있다. |
| `graph-005` | mix는 선행 70의 팀(101) 대신 후행 339의 Team:106을 제시하고 제목에 '이관'을 추가했다. | 직접 선행 edge만 찾는 것으로 부족하다. 선행 노드의 제목과 팀을 정확히 연결해야 한다. |
| `graph-007` | mix는 직접 선행 192의 제목을 맞혔지만 그 팀은 알 수 없다고 답했다. Context recall은 0.0이었다. | 부분적으로 맞는 사실이 있어도 복합 reference에 대한 자동 채점은 낮게 나올 수 있다. 원자 주장별 판정 이유를 저장하지 않아 이 원인을 확정하지는 않는다. |

### 검색 슬롯에 포함된 관계 출처 anchor

실제 500개 청크 슬롯 중 **51개**는 업무 본문이 아니라 `Confirmed worklog relation source for Worklog:<ID>` 형태의 custom KG 출처 anchor였다. **mix 47개(32개 응답), naive 4개(1개 응답)**이며 나머지 449개는 코퍼스 본문의 연속 부분문자열과 일치한다.

따라서 위 **Chunk context precision은 출처 anchor를 포함한 반환 청크 슬롯의 정밀도**다. 순수 본문 청크만의 정밀도가 아니다. `multi_evidence-010`의 mix는 5개 슬롯 중 2개가 572/573 anchor여서 실제 본문은 3개뿐이었다. `multi_evidence-002`도 274 anchor가 1개 슬롯을 차지했다. 이 구성은 상세 근거 회수 차이의 가능한 원인이지만 인과효과를 분리 측정한 것은 아니다.

권장 후속 실험은 **anchor를 최종 본문 슬롯에서 제외하는 비교**, **선행업무→팀/작성자 근거 회수 보완**, **본문/관계 질문 분리 평가**다. 현재 점수를 보고 튜닝한 뒤에는 별도 holdout으로 검증해야 한다. 이번 실행에서는 벤치마크나 검색 알고리즘을 점수에 맞춰 수정하지 않았다.

## 인덱스 및 실행 검증

- PostgreSQL 업무일지 **200건**, 직접 의존관계 **77건**을 평가 코퍼스의 실제 컬럼 값과 대조했다.
- 로컬 full_docs 본문 **200/200 완전 일치**, doc_status **200/200 processed**.
- Qdrant: chunks **400**, entities **592**, relationships **1,014**. 400 chunks는 본문 200건과 custom KG 출처 anchor 200건을 포함한다. 모든 vector provenance가 코퍼스 범위 안이며 200개 업무일지를 모두 포함한다.
- Neo4j: Worklog 노드 **200개**, 작성자·팀·직접 선행관계의 확정 사실 **477개** 확인. 모든 LLM 추출 관계의 정답성을 보증하는 검사는 아니다.
- 독립 집계로 평균·지연 및 100개 결과의 실제 프롬프트/문맥 정합성을 확인했다. 회귀 테스트 **20개 통과**(기존 15개 + 실행기 5개).
- 전체 실행의 PowerShell wrapper는 INFO 수준 stderr를 NativeCommandError로 분류해 종료 코드 1을 반환했다. 로그에는 Python traceback이 없고 마지막 storage finalize까지 완료됐다. 종료 코드만으로 성공을 주장하지 않고 위 원자료 완전성을 별도 검사했다. 외부 호출 없는 최종 보고서 재생성은 종료 코드 0이었다.
- `index-verification.json`, `validation.json`, `runner-executed.py`를 실행 증빙으로 보관했다. 원본 실행기 사본의 SHA-256은 `run.json`과 일치한다. 현재 `run_ragas.py`는 완료 후 재개 해시 검사·오류 기록·거절 판정 재사용을 보강했으므로 코드 해시가 다르다. 새 출력 폴더에서 재실행해야 하며 완료된 점수를 변경하지 않았다.
- API 키를 산출물에 기록하지 않았다. 이 보고서의 사례 분석은 원자료 수동 검토 결과이며 재생성되는 자동 통계 부분과 구분한다.

## 문항별 점수

| ID | 모드 | F | AR | CP | CR | 오류 |
|---|---|---:|---:|---:|---:|---|
| disambiguation-001 | mix | 1.0000 | 0.8062 | 0.3333 | 1.0000 | {} |
| disambiguation-001 | naive | 1.0000 | 0.9789 | 0.5000 | 1.0000 | {} |
| disambiguation-002 | mix | 1.0000 | 0.9111 | 0.0000 | 1.0000 | {} |
| disambiguation-002 | naive | 1.0000 | 0.9132 | 0.0000 | 1.0000 | {} |
| disambiguation-003 | mix | 1.0000 | 0.8974 | 0.4167 | 1.0000 | {} |
| disambiguation-003 | naive | 1.0000 | 0.9877 | 0.4167 | 1.0000 | {} |
| disambiguation-004 | mix | 1.0000 | 0.8422 | 0.4500 | 1.0000 | {} |
| disambiguation-004 | naive | 1.0000 | 0.7584 | 0.7000 | 1.0000 | {} |
| disambiguation-005 | mix | 1.0000 | 0.9434 | 0.0000 | 1.0000 | {} |
| disambiguation-005 | naive | 1.0000 | 0.9530 | 0.0000 | 1.0000 | {} |
| disambiguation-006 | mix | 1.0000 | 0.9925 | 0.3333 | 1.0000 | {} |
| disambiguation-006 | naive | 1.0000 | 0.9008 | 0.5000 | 1.0000 | {} |
| disambiguation-007 | mix | 1.0000 | 0.8762 | 0.0000 | 1.0000 | {} |
| disambiguation-007 | naive | 1.0000 | 0.8821 | 0.0000 | 0.6667 | {} |
| disambiguation-008 | mix | 1.0000 | 0.9215 | 0.0000 | 1.0000 | {} |
| disambiguation-008 | naive | 1.0000 | 0.7994 | 0.0000 | 1.0000 | {} |
| disambiguation-009 | mix | 1.0000 | 0.9540 | 0.3333 | 1.0000 | {} |
| disambiguation-009 | naive | 1.0000 | 0.7178 | 0.5000 | 1.0000 | {} |
| disambiguation-010 | mix | 1.0000 | 0.8083 | 0.0000 | 1.0000 | {} |
| disambiguation-010 | naive | 1.0000 | 0.9182 | 0.0000 | 1.0000 | {} |
| graph-001 | mix | 1.0000 | 0.8091 | 0.0000 | 0.3333 | {} |
| graph-001 | naive | 1.0000 | 0.7400 | 0.0000 | 0.0000 | {} |
| graph-002 | mix | 1.0000 | 0.0000 | 0.0000 | 0.0000 | {} |
| graph-002 | naive | 0.6000 | 0.7551 | 0.0000 | 0.0000 | {} |
| graph-003 | mix | 1.0000 | 0.8335 | 0.0000 | 0.0000 | {} |
| graph-003 | naive | 0.5000 | 0.0000 | 0.0000 | 0.0000 | {} |
| graph-004 | mix | 1.0000 | 0.7554 | 0.0000 | 0.0000 | {} |
| graph-004 | naive | 0.8000 | 0.0000 | 0.0000 | 0.0000 | {} |
| graph-005 | mix | 0.8000 | 0.8600 | 0.0000 | 0.5000 | {} |
| graph-005 | naive | 1.0000 | 0.0000 | 0.0000 | 0.0000 | {} |
| graph-006 | mix | 1.0000 | 0.8700 | 0.0000 | 0.5000 | {} |
| graph-006 | naive | 1.0000 | 0.0000 | 0.0000 | 0.0000 | {} |
| graph-007 | mix | 1.0000 | 0.7676 | 0.0000 | 0.0000 | {} |
| graph-007 | naive | 1.0000 | 0.0000 | 0.0000 | 0.0000 | {} |
| graph-008 | mix | 1.0000 | 0.0000 | 0.0000 | 0.0000 | {} |
| graph-008 | naive | 1.0000 | 0.0000 | 0.0000 | 0.0000 | {} |
| graph-009 | mix | 0.0000 | 0.0000 | 0.0000 | 0.0000 | {} |
| graph-009 | naive | 1.0000 | 0.0000 | 0.0000 | 0.0000 | {} |
| graph-010 | mix | 1.0000 | 0.0000 | 0.0000 | 0.5000 | {} |
| graph-010 | naive | 1.0000 | 0.0000 | 0.0000 | 0.0000 | {} |
| multi_evidence-001 | mix | 1.0000 | 0.9244 | 1.0000 | 1.0000 | {} |
| multi_evidence-001 | naive | 1.0000 | 0.9552 | 1.0000 | 1.0000 | {} |
| multi_evidence-002 | mix | 0.5000 | 0.8506 | 0.0000 | 0.5000 | {} |
| multi_evidence-002 | naive | 1.0000 | 0.9400 | 0.2000 | 1.0000 | {} |
| multi_evidence-003 | mix | 1.0000 | 0.9377 | 0.0000 | 1.0000 | {} |
| multi_evidence-003 | naive | 1.0000 | 0.9573 | 0.0000 | 1.0000 | {} |
| multi_evidence-004 | mix | 1.0000 | 0.9519 | 1.0000 | 1.0000 | {} |
| multi_evidence-004 | naive | 1.0000 | 0.9553 | 1.0000 | 1.0000 | {} |
| multi_evidence-005 | mix | 1.0000 | 0.9218 | 1.0000 | 1.0000 | {} |
| multi_evidence-005 | naive | 1.0000 | 0.7773 | 1.0000 | 1.0000 | {} |
| multi_evidence-006 | mix | 1.0000 | 0.9227 | 0.0000 | 1.0000 | {} |
| multi_evidence-006 | naive | 1.0000 | 0.8969 | 0.0000 | 1.0000 | {} |
| multi_evidence-007 | mix | 1.0000 | 0.8175 | 1.0000 | 1.0000 | {} |
| multi_evidence-007 | naive | 1.0000 | 0.9567 | 1.0000 | 1.0000 | {} |
| multi_evidence-008 | mix | 1.0000 | 0.9737 | 0.2000 | 1.0000 | {} |
| multi_evidence-008 | naive | 1.0000 | 0.9457 | 0.8333 | 1.0000 | {} |
| multi_evidence-009 | mix | 1.0000 | 0.8105 | 0.2500 | 1.0000 | {} |
| multi_evidence-009 | naive | 1.0000 | 0.8765 | 0.5833 | 1.0000 | {} |
| multi_evidence-010 | mix | 1.0000 | 0.8212 | 0.0000 | 0.5000 | {} |
| multi_evidence-010 | naive | 1.0000 | 0.8914 | 0.2500 | 1.0000 | {} |
| single_fact-001 | mix | 1.0000 | 0.7927 | 0.8333 | 1.0000 | {} |
| single_fact-001 | naive | 1.0000 | 0.9521 | 1.0000 | 1.0000 | {} |
| single_fact-002 | mix | 1.0000 | 0.9643 | 1.0000 | 1.0000 | {} |
| single_fact-002 | naive | 1.0000 | 0.9576 | 1.0000 | 1.0000 | {} |
| single_fact-003 | mix | 1.0000 | 0.6642 | 0.7556 | 1.0000 | {} |
| single_fact-003 | naive | 1.0000 | 0.6345 | 1.0000 | 1.0000 | {} |
| single_fact-004 | mix | 1.0000 | 0.9882 | 1.0000 | 1.0000 | {} |
| single_fact-004 | naive | 1.0000 | 0.6607 | 1.0000 | 1.0000 | {} |
| single_fact-005 | mix | 1.0000 | 0.8863 | 0.8333 | 1.0000 | {} |
| single_fact-005 | naive | 1.0000 | 0.5844 | 1.0000 | 1.0000 | {} |
| single_fact-006 | mix | 1.0000 | 0.8512 | 0.3333 | 1.0000 | {} |
| single_fact-006 | naive | 1.0000 | 0.8571 | 0.5333 | 1.0000 | {} |
| single_fact-007 | mix | 1.0000 | 0.9181 | 1.0000 | 1.0000 | {} |
| single_fact-007 | naive | 1.0000 | 0.8189 | 1.0000 | 1.0000 | {} |
| single_fact-008 | mix | 1.0000 | 0.8345 | 1.0000 | 1.0000 | {} |
| single_fact-008 | naive | 1.0000 | 0.8234 | 1.0000 | 1.0000 | {} |
| single_fact-009 | mix | 1.0000 | 0.8706 | 1.0000 | 1.0000 | {} |
| single_fact-009 | naive | 1.0000 | 0.9096 | 1.0000 | 1.0000 | {} |
| single_fact-010 | mix | 1.0000 | 0.9624 | 1.0000 | 1.0000 | {} |
| single_fact-010 | naive | 1.0000 | 0.7338 | 1.0000 | 1.0000 | {} |
| single_fact-011 | mix | 1.0000 | 0.9536 | 1.0000 | 1.0000 | {} |
| single_fact-011 | naive | 1.0000 | 0.9259 | 1.0000 | 1.0000 | {} |
| single_fact-012 | mix | 1.0000 | 0.9560 | 1.0000 | 1.0000 | {} |
| single_fact-012 | naive | 1.0000 | 0.9535 | 1.0000 | 1.0000 | {} |
| single_fact-013 | mix | 1.0000 | 0.9049 | 1.0000 | 1.0000 | {} |
| single_fact-013 | naive | 1.0000 | 0.8261 | 1.0000 | 1.0000 | {} |
| single_fact-014 | mix | 1.0000 | 0.9398 | 1.0000 | 1.0000 | {} |
| single_fact-014 | naive | 1.0000 | 0.8897 | 1.0000 | 1.0000 | {} |
| single_fact-015 | mix | 1.0000 | 0.8592 | 1.0000 | 1.0000 | {} |
| single_fact-015 | naive | 1.0000 | 0.8592 | 0.7500 | 1.0000 | {} |
| unanswerable-001 | mix | — | — | — | — | {} |
| unanswerable-001 | naive | — | — | — | — | {} |
| unanswerable-002 | mix | — | — | — | — | {} |
| unanswerable-002 | naive | — | — | — | — | {} |
| unanswerable-003 | mix | — | — | — | — | {} |
| unanswerable-003 | naive | — | — | — | — | {} |
| unanswerable-004 | mix | — | — | — | — | {} |
| unanswerable-004 | naive | — | — | — | — | {} |
| unanswerable-005 | mix | — | — | — | — | {} |
| unanswerable-005 | naive | — | — | — | — | {} |

## 지표 공식 문서

- [Faithfulness](https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/faithfulness/)
- [Answer relevancy](https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/answer_relevance/)
- [Context precision](https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/context_precision/)
- [Context recall](https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/context_recall/)
