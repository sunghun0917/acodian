# V3: ID 없는 자연어 질문 50문항

V2의 고정 200건 업무일지와 등록 관계를 그대로 사용한다. **질문에는 업무·작성자·팀 ID나 ID 출력 요청을 넣지 않는다.** 검색과 답변 생성에는 `benchmark-v3.jsonl`의 `question`만 전달한다. `validation-details.jsonl`의 시작 업무 ID, 정답 ID, 관계, 근거는 채점 전용이며 인덱싱하거나 프롬프트에 주입하지 않는다.

| 유형 | 문항 |
|---|---:|
| 단순 사실 | 10 |
| 직접 관계 | 12 |
| 다중 관계 | 14 |
| 전체 선행 탐색 | 8 |
| 관계 부재 | 6 |

총 50문항이며 development/evaluation은 각 25문항이다. 기존 V2 질문에서 ID만 제거한 유형 30건, 제목을 의역한 질문 14건, 업무 본문의 식별 단서를 의역한 질문 6건을 포함한다. 본문 단서는 비공개 정답 파일의 `anchor_evidence`와 원천 SQL 본문을 대조해 검증한다. 각 문항의 V2 원본 ID는 `source_v2_id`에 기록한다. V2 80문항과 그 결과는 변경하지 않는다.

50문항은 시작 업무 27개·의존관계 컴포넌트 22개를 사용한다. 한 시작 업무는 최대 4문항이며, 같은 컴포넌트를 development와 evaluation 양쪽에 배치하지 않는다. 질문 간 업무 재사용이 있으므로 50개 독립 표본으로 통계 해석하면 안 된다.

## 재현·검사

```powershell
ai/.venv/Scripts/python.exe -B -X utf8 docs/ai/evaluation/v3/build_benchmark.py --check
ai/.venv/Scripts/python.exe -B -X utf8 -m unittest discover -s docs/ai/evaluation/v3 -p 'test_*.py' -v
```

원천 V2를 의도적으로 바꾸고 V3를 재생성할 때만 `--check` 없이 실행한다. `manifest.json`에 원천과 산출물의 SHA-256을 기록한다. 이 세 파일은 모두 인덱싱하지 않는다.

## 채점 경계

V3는 시작 업무 식별과 관계 탐색을 **함께** 평가한다. 결과는 최소한 `시작 업무 식별`, `관계 방향·경로`, `최종 업무/작성자/팀`, `전체 선행업무 집합`, `검증된 부재`, `모호성 처리`로 분리해 집계한다. 답변에 ID를 요구하지 않았으므로, 출력 문자열의 숫자 유무만으로 정오를 판정하지 않는다. 채점기는 이름·관계 설명을 비공개 `structured_gold`와 대조해야 한다.

전체 선행업무와 관계 부재의 정답은 **고정 200건 유도 부분그래프** 기준이며 운영 DB 전체에 대한 주장이 아니다. 기존 합성 코퍼스를 재사용하므로 새로운 독립 데이터셋이 아니다. 설명형 질문의 시작 업무 식별이 유일한지 독립 사람 검수는 아직 수행하지 않았다. LightRAG 직접 호출 기반 RAGAS 평가는 아래 실행 결과에서 확인한다. 실제 서비스 HTTP 연동 평가와 자동 구조화 채점은 아직 실행되지 않았다.

## Naive / 기본 Mix RAGAS 평가 결과

2026-09-29 KST, 50문항 × 두 모드의 100개 응답을 생성했다. 원래 RAGAS 5지표 500개를 채점했으나, **현행 주평가는 Faithfulness·Answer Relevancy·Context Recall 3지표(300개 점수)**만 사용한다. 추가 다중홉·reranker는 사용하지 않았다.

- [현행 주평가·Context Precision 제외 사유](results-judge-gemini-3.8-flash-20260929/report.md)
- [과거 5지표 탐색 비교](results-judge-gemini-3.8-flash-20260929/comparison.md)
- [최종 무결성 검증](results-judge-gemini-3.8-flash-20260929/verification.json)

원점수는 모두 보존했다. Factual F1에는 짧은 기준답변의 질문 맥락 누락에 따른 감점 사례가 있으므로 정답률로 사용하지 않는다. 실행·재집계 명령은 비교 요약에 있다.

## 제외된 탐색 지표

`context_precision_chunks`, `factual_correctness`, 부분 재채점된 `context_precision_all`의 원점수는 행 JSON에 보존한다. 세 지표는 현행 주평가와 모드 비교에서 제외하며, 전체 문맥 재채점은 재개하지 않는다. 제외 사유와 남은 평가 한계는 report에 명시했다.
