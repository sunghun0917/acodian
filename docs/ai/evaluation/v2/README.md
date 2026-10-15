# V2: Naive RAG와 LightRAG Mix의 관계 검색 가치 평가

> **핵심 요약**: 동일한 질문 세트에 대해 **본문 사실 → 직접 관계 → 다중 관계**로 질의 탐색 깊이가 복잡해질 때 LightRAG Mix 모드가 제공하는 가치를 측정합니다. 동결된 V1 합성 업무일지 **200건 및 직접 선행관계 77개**를 기반으로 총 **80문항**으로 구성됩니다.

---

## 📊 1. 문항 구성 및 평가 기준

| 분류 | 문항 수 | 세부 구성 | 주요 평가 기준 |
| :--- | :---: | :--- | :--- |
| **단순 사실** | 20 | 본문에 명시된 구체적 사실 질의 | 기준 사실 일치도 및 근거 충실도 |
| **직접 관계** | 20 | 작성자 7 / 소속 팀 7 / 직접 선행업무 6 | 정답 ID 및 관계 방향 일치 |
| **다중 관계** | 20 | 선행업무 작성자 7 / 팀 7 / 선행의 선행 6 | 최종 답변 및 다단계 관계 경로 일치 |
| **전체 선행 탐색** | 12 | 모든 직·간접 선행업무 (최대 4단계) | 업무 ID 집합 Precision, Recall, F1, 완전 일치 |
| **가까운 관계 부재** | 8 | 실제 업무에 등록된 선행관계가 없는 경우 | 잘못된 환각 관계 생성률 |

- `hop_count`: 정답 도출에 필요한 관계 홉 수 (전체 선행 탐색의 경우 최장 경로의 깊이).
- `dependency_paths`: 업무 간 의존 경로만 기록하며, 작성자·팀 연결은 `relation_evidence`에 별도로 분리 기록.

---

## ❓ 2. 질문 예시

- **단순 사실**: "「WMS 로그 누락 보정 작업 진행」에서 수정 작업과 번갈아 수행한 활동은 무엇인가?"
- **직접 관계**: "제목과 시작 업무 ID로 지정한 업무의 작성자는 누구인가?"
- **다중 관계**: "그 업무의 직접 선행업무 제목과 각 선행업무의 소속 팀은 어디인가?"
- **전체 선행 탐색**: "현재 200건 코퍼스에 등록된 모든 직·간접 선행업무를 빠짐없이 알려 달라."
- **관계 부재**: "실제 업무에 등록된 직접 선행업무가 있는지 물어본다." *(정답: 빈 목록)*

> [!NOTE]
> **관계 부재 8문항의 특성**: 답변 불가능(Unanswerable) 문항이 아니라, 코퍼스 전체의 관계망을 확인한 후 "등록된 선행관계가 없음"을 확인하여 답할 수 있는 **검증된 부재(`answerable=true`, `answer_kind=verified_absence`)** 문항입니다. 따라서 일반적인 정보 부족 거절과 혼합 채점하지 않습니다.

---

## 🔬 3. 시스템 비교 설계

| 실험군 | 입력 데이터 | 해석 관점 |
| :--- | :--- | :--- |
| **Naive Baseline** | V1 본문 인덱스 | 텍스트 본문 기반의 전통적 벡터 검색 시스템 |
| **LightRAG Mix** | V1 본문 + 등록된 그래프 관계 인덱스 | 지식 그래프 구조가 결합된 하이브리드 검색 시스템 |

> [!IMPORTANT]
> **해석상 주의점**: 입력 정보량(그래프 포함 여부)이 다른 시스템 수준의 비교이므로, 본 결과가 순수하게 '그래프 토폴로지 알고리즘만의 효과'라고 단정하지 않습니다. 또한 본문 내에 작성자나 팀이 자연스럽게 언급된 경우도 존재하므로 모든 관계 문제가 Naive에서 불가능하다고 전제하지 않습니다.

- **대상 식별 방식**: 신규 질문은 제목과 **시작 업무 ID**로 대상을 식별합니다 (모호한 제목 식별 능력은 본 평가의 평가 대상이 아님).
- **작성일 미요구**: 기존 본문에 없는 작성일 정보는 질문하지 않습니다.

---

## 📂 4. 파일 구성 및 기본 검증

| 파일명 | 주요 용도 |
| :--- | :--- |
| **`benchmark-v2.jsonl`** | 평가용 4필드 (`id`, `category`, `question`, `reference`). **(인덱싱 절대 금지)** |
| **`validation-details.jsonl`** | `id`로 연결하는 split, 세부분류, 구조화 정답, 원천 근거. **(인덱싱 절대 금지)** |
| **`manifest.json`** | 원천/산출물 SHA-256 해시, 분포 통계, 그래프 토폴로지, 데이터 한계 명시 |
| **`build_benchmark.py`** | 표준 라이브러리만 사용하는 재현 생성기 및 정적 검증기 |
| **`test_benchmark.py`** | 순환, 분기, 정답 변조, 누락, 방향, split, 데이터 유출 방지 회귀 테스트 |

### 🛠️ 검증 명령어 (저장소 루트 실행, 비용 없음)
```powershell
ai/.venv/Scripts/python.exe -B -X utf8 docs/ai/evaluation/v2/build_benchmark.py
ai/.venv/Scripts/python.exe -B -X utf8 docs/ai/evaluation/v2/build_benchmark.py --check
ai/.venv/Scripts/python.exe -B -X utf8 -m unittest discover -s docs/ai/evaluation/v2 -p 'test_*.py' -v
```

---

## 🎯 5. 정답 구조 및 채점 유의사항

상세 검증 필드는 `validation-details.jsonl`에만 저장되며 평가 파일과 `id`로 1:1 매핑됩니다:

- `root_worklog_id`: 질문의 대상이 되는 시작 업무 ID.
- `structured_gold.worklog_ids`: 반환되어야 할 정답 업무 ID 집합 (작성자/팀 단일 질문에는 대상 업무 포함).
- `structured_gold.author_ids / team_ids`: 기대 작성자 및 팀 ID.
- `structured_gold.relations`: 중간 의존 경로 및 최종 팀/작성자 관계 쌍.
- `expected_worklog_ids`: 근거를 제공하는 업무 집합 (답 목록과 다를 수 있음. 시작 업무는 선행업무 정답 목록에 미포함).
- `evidence` / `relation_evidence`: 검증 가능한 원문 인용 및 SQL 확정 관계.

> [!TIP]
> RAGAS 지표는 근거 충실도/답변 관련성 등의 보조 지표로 활용하고, 관계 질문에 대해서는 **ID/방향/집합 정밀 일치 채점**을 병행합니다.

---

## ⚠️ 6. 데이터셋 분할 및 구조적 한계

- **Split 기준**: 의존관계의 **전체 약연결 컴포넌트(Weakly Connected Component)** 단위로 development와 evaluation을 분리하여 두 split 간 근거 업무 ID가 겹치지 않도록 격리.
- **Holdout 한계**: 원천 전체가 V1에서 기사용된 합성 데이터이므로, evaluation split은 **완전한 미노출 독립 코퍼스 holdout이 아닙니다.**
- **그래프 구조 한계**: 실제 최대 깊이 4단계, 분기 노드 **0개**, 순환 노드 **0개**인 단순 DAG 구조입니다. (분기/순환 처리는 단위 테스트용 소형 그래프에서만 검증).
- **작성자 vs 담당자**: 원천에 등록된 작성자는 업무일지 작성자이며, 실제 업무 수행 담당자와 일치하지 않을 수 있습니다.

---

## 🚀 7. 관계 보완 OFF 전체 평가 실행

`run_evaluation.py`는 V2 80문항 × 2개 모드(Naive/Mix) = **총 160개 응답**을 실행합니다.

```powershell
# 1. 평가 실행 (유료 모델 호출, ai/.env 설정 사용, 신규 결과 디렉터리에 실시간 저장)
ai/.venv/Scripts/python.exe -B -X utf8 docs/ai/evaluation/v2/run_evaluation.py

# 2. 오프라인 결과 재집계 (API 키 호출 없이 저장된 원시 데이터로 통계 및 보고서 재생성)
ai/.venv/Scripts/python.exe -B -X utf8 docs/ai/evaluation/v2/run_evaluation.py --report-only
```

- **기본 출력 경로**: `results-judge-gemini-3.8-flash-20260928/`
- **재개(Resume) 안전성**: `run.json`에 코드·벤치마크·원천·모델 계약을 보관하며, 조건 불일치 시 오염 방지를 위해 재개를 거부합니다.
- **생성/평가 모델 분리**:
  - 답변 생성: `ai/.env`의 기존 LightRAG 설정 모델 유지
  - RAGAS 5지표 판정(Judge): `gemini-3.8-flash` 고정
