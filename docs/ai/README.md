# AI 문서 안내

FastAPI, LightRAG의 설계, 실행 가이드, 평가 자료를 관리한다. 코드 작업 규칙은 [ai/AGENTS.md](../../ai/AGENTS.md)를 먼저 읽는다.

## 폴더 구조

| 위치 | 내용 |
|---|---|
| `adr.yaml`, `code-convention.yaml` | 설계 결정과 코드 컨벤션 (1차 기준) |
| `guide/` | 현재 동작 설명과 운영 절차 |
| `report/<주제>/` | 작업별 종합 정리(`README.md`)와 근거가 되는 원본 보고서, 조사 자료, 측정 원본 |
| `evaluation/` | RAG 평가 데이터, 실행 스크립트, 실행별 결과 (경로 유지) |

## 규칙

- [설계 결정 기록](adr.yaml), [코드 컨벤션](code-convention.yaml)
- [FastAPI 표준 가이드 (Word 참고 자료)](AX-WMS_FastAPI_표준가이드.docx)

## 가이드 (`guide/`)

| 문서 | 내용 |
|---|---|
| [lightrag-team-search.md](guide/lightrag-team-search.md) | LightRAG v4 팀별 검색과 권한 문맥, 현재 동작 |
| [lightrag-postgres-setup.md](guide/lightrag-postgres-setup.md) | LightRAG 전용 PostgreSQL DB 준비, 전환, 재색인 절차 |
| [lightrag-index-rebuild.md](guide/lightrag-index-rebuild.md) | 별도 workspace에 새 인덱스를 만들어 전환하는 절차 (1단계 인덱스 전환) |
| [langfuse-tracing.md](guide/langfuse-tracing.md) | LightRAG v4 요청의 Langfuse 추적 설정과 확인 |
| [pgvector-semantic-search.md](guide/pgvector-semantic-search.md) | LightRAG 이전 pgvector 업무일지 임베딩 저장(1부)과 시맨틱 검색(2부) |
| [rag-flow.html](guide/rag-flow.html) | 등록, 수정, 삭제, 검색 처리 흐름도 |

## 작업별 보고서 (`report/`)

각 폴더의 `README.md`가 그 주제의 종합 정리다. 전체 작업 목록은 [전체 문서 안내](../README.md#작업별-종합-정리)에 있다.

| 폴더 | 주제 | 폴더 안의 원본 |
|---|---|---|
| [rag-evaluation](report/rag-evaluation/README.md) | RAG 검색 품질과 평가 (RAGAS, 벤치마크, 응답 속도) | [speed-and-ragas-report.md](report/rag-evaluation/speed-and-ragas-report.md) (속도 최적화, RAGAS 종합 보고서) |
| [rag-cache](report/rag-cache/README.md) | RAG 답변 캐시와 증분 재색인 | [cache-and-reindex-report-2026-10-06.md](report/rag-cache/cache-and-reindex-report-2026-10-06.md), [semantic-cache-real-embedding-2026-10-11.md](report/rag-cache/semantic-cache-real-embedding-2026-10-11.md), [측정 원본 JSON](report/rag-cache/semantic-cache-real-embedding-2026-10-11-raw.json), 설계 [design-scope-cache-2026-10-04.md](report/rag-cache/design-scope-cache-2026-10-04.md), 계획 [plan-scope-cache-2026-10-04.md](report/rag-cache/plan-scope-cache-2026-10-04.md) |
| [lightrag-v4](report/lightrag-v4/README.md) | LightRAG v4 전환 (팀별 검색, PostgreSQL 저장소, 1단계 안정화) | [phase1-stabilization-2026-10-24.html](report/lightrag-v4/phase1-stabilization-2026-10-24.html), [v3-v4-differences.md](report/lightrag-v4/v3-v4-differences.md), [research-concurrency-consistency.md](report/lightrag-v4/research-concurrency-consistency.md), [research-postgres-migration.md](report/lightrag-v4/research-postgres-migration.md), 설계 [design-postgres-storage-2026-10-21.md](report/lightrag-v4/design-postgres-storage-2026-10-21.md), 계획 [plan-postgres-storage-2026-10-21.md](report/lightrag-v4/plan-postgres-storage-2026-10-21.md) |
| team-index-mvcc | 전사 인덱스 제거와 팀 인덱스 두 벌 번갈아 쓰기. **계획, 미구현** (종합 정리 README 없음) | 설계 [design.md](report/team-index-mvcc/design.md), 계획 [plan.md](report/team-index-mvcc/plan.md) |

AI 작업 실행, 결과 전달, 수동 재시도, 11-12 단순화는 api와 ai에 걸친 작업이라 [api/report/ai-task-reliability](../api/report/ai-task-reliability/README.md)에 모았다. 단순화 결과 원본은 [simplification-2026-11-12.md](../api/report/ai-task-reliability/simplification-2026-11-12.md)다.

보고서는 작성 당시의 기록이다. 현재 구현과 다를 수 있으니 각 README의 "현재 상태"를 먼저 본다. 작업별 설계서(`design-*.md`)와 구현 계획(`plan-*.md`)도 해당 주제 폴더에 함께 있다.

## 평가 데이터, 실행, 결과 (`evaluation/`)

- [평가 자료 안내, v1 코퍼스](evaluation/README.md)
- [v2 벤치마크](evaluation/v2/README.md)
- [v3 응답 시간 최적화, 채점](evaluation/v3/README.md)
- [v4 실서비스 평가](evaluation/v4/README.md)

`evaluation/`에는 실행 스크립트, 테스트, SQL, JSONL 데이터, manifest, 실행별 결과가 함께 있다. 경로와 원본 해시를 유지하며, 평가용 SQL을 운영 DB에 실행하지 않는다.

[전체 문서 안내](../README.md)
