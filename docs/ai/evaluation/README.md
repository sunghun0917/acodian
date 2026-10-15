# Naive / Mix 평가용 합성 코퍼스 v1 (200건)

> [!WARNING]
> - **데이터 및 파일럿 질문 정적 패키지**: 본 문서는 평가용 데이터셋과 질문의 정적 규약이며 최종 성능 결과 보고서가 아닙니다.
> - **평가 전용 스키마**: SQL은 `evaluation_v1` 전용 최소 스키마입니다. 현재 운영 LightRAG `source_store`가 그대로 읽을 수 없으므로, **운영 마이그레이션이나 운영 DB에 절대 직접 실행하지 마세요.**
> - **색인 금지**: 질문, 기준답변, reference/evidence, README, manifest는 검색 코퍼스에 넣지 않습니다. 업무일지 원문 200건만 평가용 인덱스에 사용합니다.
> - **해시 기준선 보호**: 검증 실패 시 변경 원인을 먼저 조사합니다. 해시 실패를 없애기 위해 코퍼스·manifest를 임의 재생성하지 않습니다.

---

## 📌 1. 범위와 표본 추출 기준

planner의 `.omx/plans/ragas-naive-mix-data-benchmark-v1.md`에 근거하여 전체 622건 중 **200건**을 엄선했습니다. 기존 seed 본문, ID, 값은 일체 수정하지 않고 모든 컬럼을 보존했습니다.

- **팀 균형 표본 (10개 팀 × 20건 = 총 200건)**:
  - 팀 101~110 각 20건씩 추출 (※ 원본 팀 비율을 보존하는 비율 표본은 아님).
- **상태 분포 유지 (Hamilton 최대잔여법)**:
  - 각 팀 내에서 원본 상태 비율에 최대잔여법을 적용하여 배정.
  - 최종 상태 분포: `CANCELLED 7`, `COMPLETED 87`, `IN_PROGRESS 62`, `ON_HOLD 18`, `PENDING 26`
- **결정적 난수 추출**:
  - `SHA256("evaluation-v1:200:<원본 ID>")` 오름차순으로 선택하여 편향 방지.
- **포함 및 제외 범위**:
  - **포함**: 작성자 34명, 팀 10개, 부서 3개, 양 끝이 선택된 직접 의존관계 77개.
  - **제외**: 팀 111~115, 미선택 업무일지 422건, 코퍼스 밖 선행업무, 태그·상태 이력, 계정 비밀번호/연락처 등.

---

## 📂 2. 파일 구성

| 파일 | 용도 및 설명 |
| :--- | :--- |
| **`v1/evaluation-corpus-v1.sql`** | 합성 원문 데이터 및 최소 참조 테이블, 외래키(FK) 정의 |
| **`v1/corpus-manifest-v1.json`** | 원본 3개 파일 SHA-256, 선택된 ID, 분포, SQL 및 질문 해시 무결성 검증용 |
| **`v1/benchmark-v1.jsonl`** | 한국어 질문, 기준답변, 본문 및 관계 근거 50문항 |
| **`v1/generate_corpus.py`** | 표준 라이브러리 기반의 결정적 SQL/manifest 재생성 스크립트 |
| **`v1/validate_benchmark.py`** | 해시, 값 보존, 추출, 관계, 근거, 부재(Unanswerable) 정적 검사기 |
| **`v1/test_validate_benchmark.py`** | 검증기 자체의 잘못된 ID, 따옴표, 중복 등 실패 감지 회귀 테스트 (15개) |

---

## 🛡️ 3. SQL 적용 계약과 보안

- **격리된 전용 PostgreSQL 환경**: 빈 전용 평가 DB에서 `BEGIN` → 스키마/테이블 생성 → INSERT → `COMMIT` 단계를 거칩니다.
- **안전성**: `DROP`, `TRUNCATE`, `DELETE`, `UPDATE`, `ON CONFLICT` 및 앱 자동 실행 경로는 일체 포함하지 않습니다.
- **보안 및 개인정보**: 원본 파일의 합성 연락처 및 인증정보는 복사하지 않으며, LLM에는 오직 제목, 요청내용, 업무내용, 확정 관계만 전달합니다.

---

## 🎯 4. 질문 계약과 해석 분류 (50문항)

1. **`shared_text` (35문항, 주평가)**:
   - 단일 근거 15문항 + 다중 근거 10문항 + 유사 업무 구분 10문항
2. **`graph_enriched` (10문항, 보조평가)**:
   - 작성자, 소속 팀, 직접 선행 관계 질문 (관계 방향: 후행 Worklog → 선행 Worklog)
3. **`unanswerable` (5문항, 거절/환각 평가)**:
   - 코퍼스에 없는 주제를 질문하여 빈 `expected_worklog_ids`를 기대하는 문항 (정답 거절률 및 환각률 별도 측정)

> [!NOTE]
> 50문항은 수작업 개발 파일럿 세트입니다. 통계적 우열이나 완전한 독립 holdout을 주장하지 않으며, 주평가와 관계평가, 거절평가는 반드시 분리하여 집계합니다.

---

## 🧪 5. 검증 및 재현 명령어

외부 라이브러리, 네트워크, 외부 DB 없이 Python 3.11+ 표준 환경에서 즉시 실행 가능합니다.

```powershell
# 1. 벤치마크 정적 무결성 검사
python -B docs/ai/evaluation/v1/validate_benchmark.py

# 2. 검증기 회귀 테스트 실행 (15개 테스트)
python -B -m unittest discover -s docs/ai/evaluation/v1 -p test_validate_benchmark.py -v
```

*(※ 원본 파일이 변경되어 재생성이 꼭 필요한 경우에만 `python -B docs/ai/evaluation/v1/generate_corpus.py`를 실행합니다.)*

---

## 📈 6. 세부 평가 버전 링크

- **[V2: 관계 복잡도별 80문항 벤치마크](v2/README.md)** (단순 사실 20 / 직접 관계 20 / 다중 관계 20 / 전체 선행 12 / 부재 8)
- **[V3: RAG 응답 시간 최적화 및 50문항 채점](v3/README.md)**
- **[V4: ID 포함 기준답변 재채점 및 app.light.v4 실서비스 평가](v4/README.md)**
