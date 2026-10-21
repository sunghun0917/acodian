# AI 문서 안내

FastAPI·LightRAG의 설계, 실행 가이드, 평가 자료를 관리한다. 코드 작업 규칙은 [ai/AGENTS.md](../../ai/AGENTS.md)를 먼저 읽는다.

## 규칙·실행 가이드

- [설계 결정 기록](adr.yaml) · [코드 컨벤션](code-convention.yaml)
- [FastAPI 표준 가이드 (Word 참고 자료)](AX-WMS_FastAPI_표준가이드.docx)
- [Langfuse 연동](langfuse.md)
- [LightRAG PostgreSQL 전환·재색인](lightrag-postgres-setup.md)
- [팀별 검색·권한 문맥](rag-team-prefilter.md)
- [업무일지 임베딩 전략](worklog-embedding-strategy.md)
- [업무일지 시맨틱 검색 전략](worklog-semantic-search-strategy.md)

## 조사·분석

- [LightRAG 동시성·일관성 분석](lightrag-concurrency-consistency-analysis.md)
- [LightRAG KV PostgreSQL 전환 조사](lightrag-kv-postgres-migration-research.md)

## 평가 데이터·실행·결과

- [평가 자료 안내·v1 코퍼스](evaluation/README.md)
- [v2 벤치마크](evaluation/v2/README.md)
- [v3 응답 시간 최적화·채점](evaluation/v3/README.md)
- [v4 실서비스 평가](evaluation/v4/README.md)

`evaluation/`에는 실행 스크립트, 테스트, SQL·JSONL 데이터, manifest, 실행별 결과가 함께 있다. 경로와 원본 해시를 유지하며, 평가용 SQL을 운영 DB에 실행하지 않는다.

## 보고서·흐름도

- [RAG 최적화·평가 보고서](report/rag-optimization-and-evaluation-report.md)
- [RAG 처리 흐름도 (HTML)](report/rag-flow-guide.html)
- [시맨틱 캐시 실임베딩 벤치마크](report/semantic-cache-real-embedding-benchmark.md) · [측정 원본](report/semantic-cache-real-embedding-raw.json)
- [업무일지 RAG 캐시·재색인 보고서](report/worklog-rag-cache-and-reindex-report.md)

보고서는 측정 당시의 기록이다. 작업별 설계·구현 계획은 [superpowers/](../superpowers/)에 보관한다. [전체 문서 안내](../README.md)
