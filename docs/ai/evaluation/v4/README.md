# V4: ID 포함 기준답변 재채점

V3의 50문항·검색 문맥·생성 답변을 그대로 사용한다. V4는 작성자·팀을 묻는 18문항의 기준답변에 검증된 `User:<ID>` 또는 `Team:<ID>`를 이름과 함께 추가한 **채점 기준 버전**이다. 문항 ID는 원본 응답과 일대일로 대조할 수 있도록 `v3-*`를 유지한다. 재인덱싱·검색·답변 재생성은 하지 않는다.

`benchmark-v4.jsonl`은 질문/기준답변 공개 파일이다. ID는 V3의 비공개 `structured_gold`와 고정 SQL의 이름을 대조해 넣는다. 기존 V3 벤치마크와 결과는 수정하지 않는다.

기준답변에 의존하는 `Context Recall`(주평가)과 `Factual Correctness`(보조)만 변경된 18문항 × 2모드에서 Ragas로 다시 채점한다. `Faithfulness`·`Answer Relevancy`는 저장된 점수를 재사용한다. ID가 포함된 기준답변으로도 Ragas의 의미 기반 점수가 반드시 정답 1이 되는 것은 아니며, 수동 정답 보정은 하지 않는다.

```powershell
ai/.venv/Scripts/python.exe -B -X utf8 -m unittest discover -s docs/ai/evaluation/v4 -p 'test_*.py' -v
ai/.venv/Scripts/python.exe -B -X utf8 docs/ai/evaluation/v4/regrade_v4.py
```

결과:
- [RAG 최적화 및 V4 종합 평가 보고서](../../report/rag-optimization-and-evaluation-report.md) (2026-10-15 갱신: Baseline / Reranker / 시작 시점 v4 개선·퇴행 비교)
- [Baseline 재채점 보고서](results-ragas-gemini-3.8-flash-20260929/report.md)
- [Reranker ON 평가 보고서](results-ragas-reranker-v4-mix-only-20260930/report.md)

API 호출이 중단되면 이미 채점한 행은 보존되며 같은 명령으로 이어서 채점한다.

## 현재 `app.light.v4` 평가 (2026-10-15)

[시작 시점 v4 실행 보고서](results-current-v4-20261015/report.md): 50문항·150지표 채점 완료, 생성·채점 오류 0건. 실행 중 소스 변경으로 종료 검증은 `source_changed`이며 **최신 코드 평가로 간주하지 않는다**. 이전 평가와의 비교 및 조건 차이는 종합 보고서 4.4~4.7절에 정리했다.

`run_ragas_reranker.py`는 이제 위의 과거 v3 답변 재채점과 별개로 **현재 v4 서비스의 실제 검색·답변**을 평가한다. 이름은 기존 실행 경로 호환을 위해 유지하며, reranker 사용 여부는 `.env`의 `LIGHTRAG_RERANK_ENABLED`를 따른다.

- `benchmark-v4.jsonl` 50문항, `mix`만 실행한다. 전사 `allowedTeamIds=null`, Redis/native 답변·키워드 캐시 OFF.
- 현재 서비스 프롬프트와 답변 모델(`LIGHTRAG_ANSWER_LLM_MODEL`, 미지정 시 기존 모델)을 사용한다. 정답과 gold ID는 생성에 전달하지 않는다.
- 실제 답변 호출의 그래프·본문 JSON 문맥으로 Faithfulness, Answer Relevancy, Context Recall 세 지표를 새로 채점한다. Context Precision/Factual Correctness는 제외한다.
- 기존 200건 본문·processed 상태·v4 snapshot을 확인한다. 실제 Neo4j/Qdrant global namespace, 그래프·vector/payload 해시를 매 실행·재개 계약에 포함한다. 삽입·삭제·재색인은 하지 않는다.
- 새 결과 폴더에 저장하고, 다른 코드·모델·데이터·인덱스 결과와 재개 병합을 거부한다. 답변 생성 후 즉시, 각 유료 지표 완료 후 즉시 저장한다. 진행 중인 호출 자체는 중단 시 다시 필요할 수 있다.
- 생성 token 사용량은 현재 adapter가 반환하지 않아 `generation_usage=null`이다. 정확한 청구액은 provider에서 확인한다.

저장소 루트 PowerShell:

```powershell
ai/.venv/Scripts/python.exe -B -X utf8 -m unittest discover -s docs/ai/evaluation/v4 -p 'test_*.py' -v
ai/.venv/Scripts/python.exe -B -X utf8 docs/ai/evaluation/v4/run_ragas_reranker.py --output docs/ai/evaluation/v4/results-current-v4-20261015
# 위 실행과 같은 명령으로 미완료 문항/지표만 재개한다.
ai/.venv/Scripts/python.exe -B -X utf8 docs/ai/evaluation/v4/run_ragas_reranker.py --output docs/ai/evaluation/v4/results-current-v4-20261015 --report-only
```

유료 실행이다. 할당량·청구 오류는 결과를 보존하고 중단한다. 이전 v3 평가와는 전체 시스템 비교이며 개별 변경의 인과 효과가 아니고, 이 실행은 팀 권한/SSE를 평가하지 않는다. 과거 결과 디렉터리를 출력으로 사용하지 않는다.
