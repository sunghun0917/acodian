# V4: ID 포함 기준답변 재채점 및 실서비스 평가

> **핵심 요약**: V4는 작성자·팀을 묻는 18문항의 기준답변에 검증된 `User:<ID>` 또는 `Team:<ID>`를 보완한 **채점 기준 버전**이며, 2026-10-15부터는 과거 답변 재채점뿐만 아니라 **현재 `app.light.v4` 실서비스의 실제 검색·답변 평가**까지 포괄합니다.

---

## 📌 1. V4 평가 개요

- **데이터 기반**: V3의 50문항, 검색 문맥, 생성 답변을 기반으로 합니다.
- **기준답변 보완**: 작성자 및 팀을 묻는 18문항의 기준답변에 검증된 `User:<ID>` 또는 `Team:<ID>`를 이름과 함께 병기했습니다.
- **대조 유지**: 문항 ID는 원본 응답과 1:1 대조가 가능하도록 `v3-*` 체계를 그대로 유지합니다.
- **비파괴 원칙**: 기존 V3 벤치마크 원본과 결과 파일은 수정하지 않으며, 재인덱싱·검색·답변 재생성 없이 재채점합니다.
- **공개 파일**: `benchmark-v4.jsonl` (질문 및 기준답변 공개 파일)
  - ID는 V3 비공개 `structured_gold`와 고정 SQL의 이름을 대조하여 주입했습니다.

---

## 🎯 2. 재채점 지표 및 규칙

- **재채점 대상 지표**: 기준답변에 직접 의존하는 **`Context Recall` (주평가)**과 **`Factual Correctness` (보조)**만 변경된 18문항 × 2개 모드(Naive/Mix)에서 Ragas로 재채점합니다.
- **재사용 지표**: `Faithfulness`와 `Answer Relevancy`는 기존에 저장된 점수를 그대로 재사용합니다.
- **주의사항**: ID가 포함된 기준답변이라도 Ragas의 LLM-as-a-Judge 특성상 의미 기반 점수가 무조건 1.0이 되는 것은 아니며, 인위적인 수동 정답 보정은 일체 하지 않습니다.

---

## 📊 3. 최종 V4 RAGAS 정량 평가 결과

| 평가 지표 (0.0 ~ 1.0) | Naive Baseline | LightRAG Mix Baseline | LightRAG Mix + Reranker | **app.light.v4 (시작 시점, 2026-10-15)** |
| :--- | :---: | :---: | :---: | :---: |
| **Main Overall (44문항)** | | | | |
| Faithfulness | 0.9110 | **0.9396** | 0.8920 | 0.7603 |
| Answer Relevancy | 0.3015 | 0.8316 | 0.8496 | **0.9033** |
| Context Recall | 0.3580 | 0.6307 | 0.6572 | **0.8428** |
| **전체 Overall (50문항, 부재 포함)** | | | | |
| Faithfulness | 0.9217 | 0.9152 | 0.8717 | 0.7682 |
| Answer Relevancy | 0.2826 | 0.7665 | 0.8116 | 0.8961 |
| Context Recall | 0.3950 | 0.6750 | 0.6983 | 0.8617 |

> [!NOTE]
> - Naive/Mix는 저장된 V3 답변의 재채점, Reranker는 V3 adapter 기반 별도 검색·답변 생성, `app.light.v4`는 실제 V4 서비스의 답변 생성 평가입니다. 파이프라인·모델·검색 조건이 달라 통제된 A/B 비교가 아닙니다.
> - V4 결과는 실행 도중 소스 변경이 감지된 `source_changed` 상태로, **평가 시작 시점 코드 기준**이며 최신 작업 트리 전체의 성능으로 일반화하지 않습니다.

---

## 🚀 4. 현재 `app.light.v4` 실제 평가 규격 (2026-10-15)

`run_ragas_reranker.py`는 과거 v3 재채점과 별개로 **현재 v4 서비스의 실제 검색·답변 품질**을 라이브 평가합니다. (파일 이름은 기존 실행 경로 호환을 위해 유지하며, reranker 활성화 여부는 `.env`의 `LIGHTRAG_RERANK_ENABLED` 설정을 따릅니다.)

- **평가 대상**: `benchmark-v4.jsonl` 50문항, `mix` 모드 단독 실행. (전사 권한 `allowedTeamIds=null`, Redis/Native 답변 및 키워드 캐시 OFF)
- **모델 및 프롬프트**: 현재 서비스의 프롬프트와 답변 모델(`LIGHTRAG_ANSWER_LLM_MODEL`, 미지정 시 기존 모델) 사용. 정답과 gold ID는 생성 단계에 절대 전달하지 않음.
- **측정 지표**: 실제 답변 호출의 그래프·본문 JSON 문맥을 바탕으로 `Faithfulness`, `Answer Relevancy`, `Context Recall` 3개 지표를 신규 채점 (Context Precision / Factual Correctness 제외).
- **데이터 무결성 계약**:
  - 기존 200건 본문, processed 상태, v4 snapshot 사전 확인.
  - 실제 Neo4j/Qdrant 글로벌 네임스페이스 및 그래프·벡터/페이로드 해시를 매 실행/재개 계약에 포함.
  - 데이터의 삽입/삭제/재색인은 일체 수행하지 않음.
- **원자적 저장 및 격리**:
  - 새 결과 디렉터리에 저장하며, 다른 코드·모델·데이터·인덱스 결과와의 재개 병합을 엄격히 차단.
  - 답변 생성 즉시, 그리고 각 유료 지표 평가 완료 즉시 실시간 저장하여 중단 시에도 안전하게 이어서 실행 가능.
- **토큰 사용량**: 생성 token 사용량은 현재 adapter가 반환하지 않아 `generation_usage=null`로 기록됩니다. (정확한 비용은 Gemini API Provider 청구 내역에서 확인)

---

## 💻 5. 실행 명령어

저장소 루트 PowerShell에서 실행합니다:

### 1) V4 기준답변 재채점 (기존 v3 답변 대상)
```powershell
ai/.venv/Scripts/python.exe -B -X utf8 -m unittest discover -s docs/ai/evaluation/v4 -p 'test_*.py' -v
ai/.venv/Scripts/python.exe -B -X utf8 docs/ai/evaluation/v4/regrade_v4.py
```

### 2) 현재 v4 실서비스 평가 실행
```powershell
# 단위 테스트 사전 검증
ai/.venv/Scripts/python.exe -B -X utf8 -m unittest discover -s docs/ai/evaluation/v4 -p 'test_*.py' -v

# 실서비스 RAGAS 평가 실행 (유료 API 호출 발생, 미완료 문항 자동 재개 지원)
ai/.venv/Scripts/python.exe -B -X utf8 docs/ai/evaluation/v4/run_ragas_reranker.py --output "docs/ai/evaluation/v4/results-app-light-v4-<run-id>"

# 외부 유료 호출 없이 이미 저장된 원시 데이터로 통계 및 마크다운 보고서만 재생성
ai/.venv/Scripts/python.exe -B -X utf8 docs/ai/evaluation/v4/run_ragas_reranker.py --output docs/ai/evaluation/v4/results-current-v4-20261015 --report-only
```

새 평가 실행에는 새로운 출력 디렉터리를 사용합니다. 동일 디렉터리에서 재개하려면 소스 코드·평가 설정·모델·벤치마크·인덱스 계약이 모두 같아야 합니다. 기존 `results-current-v4-20261015`는 `source_changed` 실행이므로 재평가 대상으로 재사용하지 않고, `--report-only`로 기존 보고서만 재생성합니다.

```
```

> [!CAUTION]
> **유료 API 실행 주의**: 실제 Gemini API 호출 비용이 발생합니다. 할당량 소진 또는 청구 오류 발생 시 기작업된 행은 영구 보존되며 안전하게 중단됩니다.

---

## 🔗 6. 관련 보고서 링크

- [RAG 최적화 및 V4 종합 평가 보고서](../../report/rag-optimization-and-evaluation-report.md) (2026-10-15 최신 갱신)
- [시작 시점 v4 실실행 보고서](results-current-v4-20261015/report.md)
- [Baseline 재채점 보고서](results-ragas-gemini-3.8-flash-20260929/report.md)
- [Reranker ON 평가 보고서](results-ragas-reranker-v4-mix-only-20260930/report.md)
