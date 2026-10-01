# V4: ID 포함 기준답변 재채점

V3의 50문항·검색 문맥·생성 답변을 그대로 사용한다. V4는 작성자·팀을 묻는 18문항의 기준답변에 검증된 `User:<ID>` 또는 `Team:<ID>`를 이름과 함께 추가한 **채점 기준 버전**이다. 문항 ID는 원본 응답과 일대일로 대조할 수 있도록 `v3-*`를 유지한다. 재인덱싱·검색·답변 재생성은 하지 않는다.

`benchmark-v4.jsonl`은 질문/기준답변 공개 파일이다. ID는 V3의 비공개 `structured_gold`와 고정 SQL의 이름을 대조해 넣는다. 기존 V3 벤치마크와 결과는 수정하지 않는다.

기준답변에 의존하는 `Context Recall`(주평가)과 `Factual Correctness`(보조)만 변경된 18문항 × 2모드에서 Ragas로 다시 채점한다. `Faithfulness`·`Answer Relevancy`는 저장된 점수를 재사용한다. ID가 포함된 기준답변으로도 Ragas의 의미 기반 점수가 반드시 정답 1이 되는 것은 아니며, 수동 정답 보정은 하지 않는다.

```powershell
ai/.venv/Scripts/python.exe -B -X utf8 -m unittest discover -s docs/ai/evaluation/v4 -p 'test_*.py' -v
ai/.venv/Scripts/python.exe -B -X utf8 docs/ai/evaluation/v4/regrade_v4.py
```

결과:
- [V4 종합 최종 평가 보고서](final-report.md) (Baseline vs Reranker 비교 분석 종합)
- [Baseline 재채점 보고서](results-ragas-gemini-3.8-flash-20260929/report.md)
- [Reranker ON 평가 보고서](results-ragas-reranker-v4-mix-only-20260930/report.md)

API 호출이 중단되면 이미 채점한 행은 보존되며 같은 명령으로 이어서 채점한다.
