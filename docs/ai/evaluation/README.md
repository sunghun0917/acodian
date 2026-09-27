# naive / mix 평가용 합성 코퍼스 v1 (200건)

> **데이터와 파일럿 질문의 정적 패키지이며 성능 결과가 아닙니다.** 실제 PostgreSQL 적용, 인덱싱, RAGAS/LLM 호출은 하지 않았습니다.
> **SQL은 `evaluation_v1` 전용 최소 스키마입니다. 현재 운영 LightRAG source_store가 그대로 읽을 수 없습니다.** 후속 로더에서 컬럼/스키마 매핑 및 날짜 타입 변환을 구현·검증해야 합니다. 운영 마이그레이션에 넣거나 운영 DB에 실행하지 마세요.

## 범위와 추출

planner의 `.omx/plans/ragas-naive-mix-data-benchmark-v1.md`를 바탕으로 사용자의 최신 결정에 따라 622건 전체 대신 **200건**을 사용합니다. 기존 seed 본문/ID/값은 수정하지 않고, 선택한 업무일지의 모든 컬럼을 보존했습니다.

- 팀 101~110 각각 20건: **팀 균형 표본이지 원본 팀 비율을 보존하는 표본은 아닙니다.**
- 각 팀 안에서는 원본 상태 비율에 최대잔여법(Hamilton)을 적용합니다. 정수 몫을 먼저 배정하고 잔여가 큰 상태부터 1건씩 추가하며 동률은 상태명 순입니다.
- 각 팀·상태 안에서 `SHA256("evaluation-v1:200:<원본 ID>")` 오름차순으로 선택합니다. 날짜나 정답 질문을 보고 추가·교체하지 않았습니다.
- 추출을 먼저 고정한 후 질문을 작성했습니다. 정답 문서뿐 아니라 나머지 문서도 검색 코퍼스에 남깁니다. 일부 작성자·날짜의 균형까지 보장하지는 않습니다.
- 작성자 34명, 팀 10개, 부서 3개, 양 끝이 선택된 직접 의존관계 77개. 코퍼스 밖 선행업무는 제외하므로 전체 원본의 의존관계 그래프는 아닙니다.
- 팀 111~115, 미선택 업무일지 422건, 태그·상태 이력·사용자 평가/스킬·알림·계정 연락처/비밀번호는 제외합니다.

상태 분포: CANCELLED 7, COMPLETED 87, IN_PROGRESS 62, ON_HOLD 18, PENDING 26

## 파일

| 파일 | 용도 |
|---|---|
| `evaluation-corpus-v1.sql` | 합성 원문과 최소 참조 테이블 및 FK |
| `corpus-manifest-v1.json` | 원본 3개 파일 SHA-256, 선택 ID, 분포, SQL/질문 해시 |
| `benchmark-v1.jsonl` | 한국어 질문·기준 답변·본문 및 관계 근거 50문항 |
| `generate_corpus.py` | 표준 라이브러리 기반 결정적 SQL/manifest 재생성 |
| `validate_benchmark.py` | 해시/값 보존/추출/관계/근거/부재 검사 |
| `test_validate_benchmark.py` | 잘못된 ID, quote, 중복, 관계 방향/범위, 해시 변경, 부재 실패 회귀검사 |

## SQL 적용 계약과 보안

- 빈 **전용 PostgreSQL 평가 DB**를 전제로 `BEGIN` → 스키마/테이블 생성 → INSERT → `COMMIT`으로 작성했습니다. 이미 존재하는 스키마에서는 실패하며 재실행 안전한 upsert가 아닙니다.
- DROP/TRUNCATE/DELETE/UPDATE/ON CONFLICT나 앱 자동 실행 경로는 없습니다.
- 평가 스키마의 날짜/시각은 원본 문자열을 보존하는 TEXT입니다. 계정은 이름·ID·부서 ID만 담으며 로그인 가능한 사용자 테이블이 아닙니다. 앱 전체 스키마와의 호환을 주장하지 않습니다.
- 생성기는 필요한 원본 파일만 로컬에서 읽습니다. 원본 파일의 합성 연락처·인증정보는 출력에 복사하지 않습니다. LLM에는 제목·요청내용·업무내용과 필요한 확정 관계만 전달해야 합니다.
- 현재 운영 경로의 allowedTeamIds 격리 계약은 이번에 검증하지 않았습니다. 내부 합성 데이터 평가로만 제한합니다.

## 질문 계약과 해석

- shared_text: 단일 근거 15 + 다중 근거 10 + 유사 업무 구분 10 = **35문항 주평가**.
- graph_enriched: 작성자·팀·직접 선행 관계 **10문항 보조평가**. 관계는 후행 Worklog → 선행 Worklog 방향입니다. 질문은 코퍼스 안의 직접 선행업무로 한정합니다.
- unanswerable: **5문항**. 빈 expected_worklog_ids와 부재 확인 범위/용어를 기록했습니다. 문맥 recall 일반 평균에 넣지 말고 정답 거절률·환각률을 별도로 측정합니다.
- 본문 quote는 실제 연속 부분 문자열입니다. 확인/의심/보정 기록을 확정 원인이나 해결 완료로 확대하지 않습니다.
- 관계 문항의 본문 인용은 대상을 특정하기 위한 앵커로, 자연스러운 사용자 질의보다 쉬울 수 있습니다. 주평가와 혼합 평균을 내지 않습니다.
- 50문항은 수작업 개발 파일럿입니다. 독립 holdout, 통계적 우열, 대체 정답의 완전성은 보장하지 않습니다. 반복 합성 표현이 있어 ID exact recall은 보수적 보조지표로 사용하고, 추가 정답 후보는 원문으로 검토 후 버전업해야 합니다.
- 부재 검사는 범위 전체의 키워드 부재와 주제 검토에 기반합니다. 키워드 부재만으로 모든 의미적 동의어의 부재를 증명하지는 않습니다. 현재 음성 문항은 범위 밖 주제 위주의 쉬운 음성입니다.

## 검증 및 재현

저장소 루트에서 Python 3.11+로 실행합니다. 외부 라이브러리·네트워크·DB가 필요 없습니다.

```powershell
python -B docs/ai/evaluation/validate_benchmark.py
python -B -m unittest discover -s docs/ai/evaluation -p test_validate_benchmark.py -v
```

현재 검증: 200건/10팀/77관계/50문항 정적 검사 PASS, 회귀검사 15개 PASS. 검증기는 원문 의미 함의나 실제 SQL 엔진 동작을 판정하지 않습니다. 본문/관계의 의미 검토와 실제 DB 적용 테스트는 별도 단계입니다.

재생성이 필요할 때만 다음 명령을 사용합니다. **원본이 바뀌어 검증이 실패한 경우 변경 이유를 먼저 검토하세요.** 재생성은 해시 기준선을 새로 기록하므로 검증 실패를 숨기기 위한 용도로 쓰지 않습니다. benchmark 파일 자체는 재생성하지 않습니다.

```powershell
python -B docs/ai/evaluation/generate_corpus.py
python -B docs/ai/evaluation/validate_benchmark.py
```

## 후속 실행 인계

1. 별도 평가 DB/로더와 인덱스에서 매핑을 검증하고, 코퍼스의 200건만 인덱싱합니다. **질문·reference·evidence·README·manifest는 인덱싱 금지**입니다.
2. naive/mix에 동일한 생성 모델, 프롬프트, 검색 예산, 코퍼스·인덱스 버전, 캐시 정책, 반복 횟수를 고정합니다. 실행기 구현과 RAGAS 버전 계약 확인은 이번 범위 밖입니다.
3. 원래 text builder는 작성자/팀/태그/선행관계를 제외하고 custom KG만 그 관계를 주입합니다. 따라서 정보 접근을 맞추지 않은 결과는 순수 검색 모드 효과가 아니라 **시스템 구성 차이**입니다.
4. 주평가/관계평가/거절평가 결과를 분리합니다. 정보 동등성이 필요하면 naive에도 관계 텍스트를 제공하는 별도 baseline을 설계합니다.
5. 유료 호출 예산 및 실행 대상은 별도 승인 후 확정합니다. 기존 smoke 결과를 이번 50문항 성능으로 취급하지 않습니다.

## 50문항 확장 (2026-09-27)

- 기존 24문항의 ID·질문·정답·근거와 파일 바이트를 보존하고 26문항을 추가했습니다. 첫 24줄 SHA-256은 `743cd11bdeb90a8a8167cea975d8d30b0df32356edca6be2b65b36aff22458f5`이며 회귀검사로 확인합니다.
- 추가 구성: 단일 근거 7 / 다중 근거 6 / 유사 업무 구분 6 / 직접 관계 6 / 답변 불가 1. 보류·취소·후속 전환도 다뤄 완료 기록만 편중하지 않았습니다.
- 중복 검토: 질문·ID 완전 중복을 검사하고, 신규 본문 문항은 기존 문항 및 다른 신규 본문 문항과 정답 업무일지를 겹치지 않게 선택했습니다. 같은 제목의 캐시 분석 두 건은 초기화 전후 비교와 재등장 맥락으로 구별합니다. 합성 원문의 반복 표현 자체를 제거한 것은 아니며 의미적 중복의 완전한 부재를 보장하지 않습니다.
- 연관성 검토: 다중 근거는 같은 주제의 서로 다른 단계, 구분 문항은 보류/취소·수정/재검증·재검증/승인 검토 등을 대조합니다. 추가 직접 관계 6문항 중 4문항은 팀 간 선행관계입니다. 본문 주제 유사성을 실제 의존관계로 추정하지 않고 등록된 후행 → 선행 방향과 선행업무의 팀을 검증합니다.
- 관계 문항은 본문 문항과 평가 목적이 다르므로 근거 업무가 일부 겹칠 수 있습니다. 신규 관계 문항도 제목을 앵커로 제공하므로 검색 난이도가 낮아질 수 있습니다.
- 추가 답변 불가 문항도 코퍼스 밖 주제의 쉬운 음성입니다. 어려운 근접 음성이나 독립 holdout을 확보한 것으로 해석하지 않습니다.
- 확장 전 benchmark는 24문항이지만 검증기는 50문항 기준이었고 manifest의 benchmark 해시도 불일치했습니다. 원본 3개 파일과 SQL 해시가 유지된 것을 확인한 후, 검토한 50문항 파일의 benchmark 해시만 갱신했습니다. 코퍼스 200건과 직접 관계 77개는 바꾸지 않았으며 DB 실행·인덱싱·유료 호출은 하지 않았습니다.

## Query-time 문맥 개선 실험 (2026-09-27)

계획: `.omx/plans/rag-query-context-improvement.md`.

- 실행기: `run_ragas_ablation.py`, 공유 구현: `ai/app/light/v3/service/worklog_context_repair.py`.
- 기존 `ragas-20260927/` 결과와 benchmark/corpus를 변경하지 않고 검색 문맥을 고정 재생한다.
- `baseline_rescore`: 기존 응답을 공통 judge로 재채점한다. 기존 점수와 새 점수를 구분한다.
- `anchor_only`: 관계 출처 anchor를 같은 업무 원문으로 치환하고 중복 제거한다. 후보 밖 문서로 빈 슬롯을 채우지 않는다. 변경이 없으면 원문과 기존 답변을 재사용한다.
- `anchor_relation`: mix에만 사전 SELECT로 고정한 DB 스냅샷의 작성자·팀 이름 및 직접 선행업무 정보를 보완한다. `mix+DB relation repair`이며 순수 mix 알고리즘 효과로 해석하지 않는다.
- 생성 문맥과 채점 문맥은 동일하다. gold는 생성 후 평가에만 사용한다. RAGAS 판정 이유는 `judge_trace`에 보관한다.
- 표준 4지표와 FactualCorrectness를 기록하고, gold quote AP/recall 및 방향 있는 관계 edge recall을 별도 보조 지표로 기록한다.
- 운영 API 기본 동작은 변경하지 않는다. trusted evaluation scope는 사용자 인증/인가가 아니며, `allowedTeamIds` 격리 구현 전 운영 활성화를 보류한다.
- 재인덱싱/DB 수정 없음. 결과는 `ragas-context-repair-20260927/`, 초기 smoke 결과는 `ragas-context-repair-smoke-20260927/`에 보존한다.

```powershell
# 사용자 승인된 ai/.env API 설정 사용. 실제 모델 호출과 비용이 발생한다.
ai/.venv/Scripts/python.exe -X utf8 -B docs/ai/evaluation/run_ragas_ablation.py --smoke
ai/.venv/Scripts/python.exe -X utf8 -B docs/ai/evaluation/run_ragas_ablation.py

# 외부 호출 없이 원자료로 자동 통계 재생성
ai/.venv/Scripts/python.exe -X utf8 -B docs/ai/evaluation/run_ragas_ablation.py --report-only
```

재개는 원본 파일·실행 코드·DB 스냅샷·모델 계약이 같을 때만 허용한다. 다른 출력 폴더에서 새 실행하려면 동결 baseline의 해시를 담은 `baseline-integrity.json`을 먼저 준비해야 한다. 최종 보고서에 추가한 수동 해석은 자동 재보고로 덮어쓸 수 있으므로 재보고 전에 별도 보존한다. 이 실험은 동일 개발 벤치마크에 대한 개선이며 독립 holdout 검증은 아니다.
