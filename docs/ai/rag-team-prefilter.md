# LightRAG v4 팀별 검색

## 현재 상태

업무일지 검색·색인·재색인·삭제 API는 **v4만 사용**한다. v3 소스와 테스트는 이전 버전으로 보존하고, v3 라우터는 현재 서버에 등록하지 않는다.
팀 제한 검색은 팀 workspace의 후보와 source snapshot으로 문맥을 만들며, 검색 중 PostgreSQL을 다시 조회하지 않는다.
기존 데이터를 v4에 일괄 색인하거나 실제 데이터로 검색하는 검증은 아직 수행하지 않았다.

```text
API가 계산한 allowedTeamIds
  → 허용 팀 workspace만 검색 (Qdrant + Neo4j + KV)
  → 업무일지 후보 ID 합치기
  → 같은 workspace의 source snapshot에서 본문·작성자·팀·관계 구성
  → Gemini 응답 / SSE
```

`allowedTeamIds=null`인 전사 조회도 v4 API가 전역 workspace에서 처리한다. 인덱싱 시에는 원본을 PostgreSQL에서 읽지만, 팀 제한 검색 시에는 다시 조회하지 않는다.

## 왜 workspace로 나눴나

설치된 LightRAG 1.4.10은 arbitrary `team_id` 필드를 custom KG와 vector payload에
그대로 보존하지 않는다. 기본 vector query도 외부 `query_filter` 인자를 받지 않는다.
따라서 builder에 `team_id`만 추가하거나 쿼리에 WHERE 한 줄만 추가하는 것으로는 부족하다.

이번에는 **기존 라이브러리의 workspace 사전 필터를 재사용**했다.
새 서버를 팀마다 띄우는 방식이 아니라 같은 Qdrant/Neo4j 서버 안에서 데이터를 나눈다.

| 저장소 | 실제 격리 경계 |
|---|---|
| Qdrant | 본문/엔티티/관계 모두 `workspace_id` payload filter, workspace별 point ID |
| Neo4j | workspace label을 가진 노드끼리만 저장/조회/확장 |
| JSON KV | `working_dir/<workspace>/` 아래 원본/청크/캐시/문서 상태 |

workspace는 원래 workspace, 임베딩 모델/차원의 해시와 양수 팀 ID로 결정한다.
팀 A와 B가 `User:24`, `Tag:1`을 공유해도 서로 다른 물리 검색 레코드가 된다.
응답과 후보 추출의 `Worklog:<ID>`, `worklog://<ID>`는 바꾸지 않는다.
adapter pool의 설정은 생성 시 복사하고, 요청별 필터를 singleton이나 환경변수에 덮어쓰지 않는다.

## 인덱싱과 실패 처리

- v4 인덱싱은 전역 workspace와 팀 workspace를 함께 갱신한다. `null` 전사 조회는 전역 workspace를 사용하고, 팀 제한 조회는 허용된 팀 workspace만 사용한다.
- 동일한 DB 조회 결과로 전사/팀 인덱스를 함께 만든다.
- 동일 업무일지의 원본 조회부터 색인·재색인·삭제 완료까지 직렬화한다.
  같은 workspace의 본문/KG 변경과 native 삭제도 공유 잠금으로 직렬화해 공유 노드 참조 유실을 막는다.
  이 잠금은 현재 단일 프로세스·이벤트 루프용이며, 다중 worker/인스턴스에서는 분산 잠금이 필요하다.
- native `ainsert` 반환만으로 성공을 판단하지 않는다. 요청 문서 상태가 모두 `processed`여야
  후속 Custom KG·snapshot·팀 marker 저장을 진행한다.
- 전역 `full_docs`에 색인 당시 팀과 구조화된 원본을 저장한 뒤 팀 사본을 쓴다.
- 팀 `full_docs`에도 작성자/팀/태그/본문/직접 선행 정보를 구조화해 저장한다.
- 다른 팀 또는 소속 미확인 선행 업무는 팀 KG에 넣지 않는다. 팀 원본 snapshot에는
  관계 ID/소속만 남기고 해당 선행 업무의 제목은 비운다.
- 허용된 직접 선행 관계는 팀 snapshot의 ID와 소속 검증 후 문맥에 구성한다.
- 삭제/재색인은 저장된 소속으로 팀 사본을 먼저 삭제하고 전역 사본을 마지막에 삭제한다.
- native 삭제의 `fail`/`not_allowed`/알 수 없는 상태는 실패로 처리해 전역 소유권을 남긴다.
  이미 없는 문서의 `not_found`만 재시도를 위한 멱등 성공으로 취급한다.
- 확정 Custom KG의 청크·엔티티·관계도 native 문서 삭제 추적에 등록한다.
  공유 작성자/태그를 복원할 확정 데이터는 extraction-cache 형식으로 보존한다.
  이 데이터는 LLM 추측 결과가 아니며, 남은 문서의 확정 관계를 재구성할 때 사용한다.
  이 보완은 전역/팀 사본에 공통 적용되며, **과거 미추적 KG의 자동 정리는 하지 않는다.**
- 본문/KG/snapshot 저장 후 호환 스키마 marker를 게시한다. **이 marker는 원본 최신성,
  전체 backfill 완료 또는 DB 제거가 안전함을 증명하는 readiness/version이 아니다.**
- 스키마 누락/불일치나 검색 실패 시 전역 검색으로 우회하지 않는다.
- 일반/SSE가 같은 후보·권한 문맥 경로를 사용한다. `[]`는 adapter/DB/LLM 호출 없이 종료한다.
- 팀 제한 검색은 구 응답 캐시를 우회한다. 전사 경로의 캐시는 유지한다.
- 본문 색인이 일부만 성공하거나 timeout/실패하면 이미 쓰인 데이터가 있을 수 있으므로
  전사 캐시도 무효화한다. 쓰기 전 실패였더라도 보수적으로 캐시 버전이 증가할 수 있다.
- index/reindex/delete/query는 내부 토큰을 요구한다. Java 색인 클라이언트도 토큰 없이 호출하지 않는다.
- API의 기존 권한 SQL에서 미삭제 업무가 없는 팀을 검색 대상에서 제외한다.
  AI 검색 후 업무를 DB에서 다시 읽는 방식은 추가하지 않는다.
  업무가 있지만 v4 색인되지 않은 팀은 제외하지 않으며, 스키마 검증에서 계속 차단한다.
  내부 AI API에 빈 팀 ID를 직접 전달하면 여전히 미준비 workspace 오류가 발생한다.

기본 Qdrant 초기화에는 legacy collection 자동 이관/일부 삭제가 있다.
팀 workspace 초기화에는 별도 인스턴스 초기화 함수를 연결하여 **전역 legacy 데이터를
팀 데이터로 재분류하거나 legacy collection을 삭제하지 않는다**.
기존 collection의 벡터 차원을 확인하고, 신규 collection과 workspace index만 구성한다.
이 경계는 1.4.10의 private storage 속성을 쓰므로 라이브러리 업그레이드 시 재검증이 필요하다.

## 남은 작업과 검증

팀 검색을 끄는 설정은 없다. **기존 전역 인덱스만으로는 팀 제한 검색이 동작하지 않는다.**
기존 업무일지를 v4 팀 workspace에 색인해야 한다. 현재 API 경로 전환만으로 과거 데이터가 자동 backfill되지는 않는다.

1. 격리된 실제 Qdrant/Neo4j 환경의 두 팀·공유 사용자/태그 저장→조회 roundtrip은 검증했다.
2. 전역/팀 색인의 **전체 backfill**을 검증한다. 빈 팀은 API 검색 대상에서 제외하고,
   업무가 있는 미준비 팀 workspace를 요청하면 오류로 차단한다.
   과거 미추적 Custom KG가 있는 인덱스는 새 namespace로 전체 구축해야 한다.
   기존 인덱스에 일반 재색인만 호출해도 잔존 KG가 모두 사라진다고 가정하면 안 된다.
3. 삭제/팀 비활성화/이름 변경 및 outbox 미처리 상태를 원본 커밋과 연결해 검증한다.
4. 모델/차원/working directory 변경 또는 원본과 인덱스가 다른 기간 후 새 색인을 검증한다.
   기존 marker만 보고 이전 팀 사본을 재사용하면 안 된다.
5. 원본 변경의 authoritative version과 인덱스 완료 version, 캐시·SSE 중간 변경을 검증한다.
6. 전체 backfill 후 실제 업무일지 검색에서 작성자·팀 이름과 ID 문맥, 답변을 검증한다.

현재 변경은 위 3–6의 최신성 게이트/API mutation 전파 및 전체 backfill 완료를 보장하지 않는다.
수동 팀 이동·원본 수정 이후 outbox 처리 전에는 팀 검색 사본도 오래된 상태일 수 있다.
v4 검색이 DB 재조회 없이 동작하므로 stale snapshot 위험은 검색 결과에 직접 영향을 준다.

## 검증 증거와 한계

- fake adapter/source 기반 일반/SSE/동시 팀 요청·빈 scope·실패 경로 회귀 테스트.
- 실제 설치된 LightRAG 생성자에서 vector/graph/KV workspace 설정과 KV 파일 경로 검사.
- 실제 Qdrant **local in-memory** 엔진으로 본문/엔티티/관계 payload와 top-k 사전 격리 검사.
- 실제 Neo4j storage가 드라이버에 보내는 batch 노드/관계/인접 확장 Cypher 검사.
- 실제 JsonKVStorage upsert/get_by_id를 사용하는 스키마 marker·원본 snapshot 회귀 검사.
- 실제 LightRAG Custom KG 삽입/삭제/캐시 기반 재구성을 메모리 store에서 실행하여
  공유 사용자/태그 보존, 마지막 문서 삭제, 태그 변경 재색인 및 부분 실패 후 삭제 검사.
- 별도 Qdrant 1.17.1/Neo4j 5.26 테스트 컨테이너에서 실제 LightRAG
  본문·Custom KG·snapshot 저장, 세 벡터 검색, 그래프 확장, 동시 팀 검색, 삭제 및
  일반/SSE 문맥 경로 통합 테스트 **2개 통과**. 임베딩/LLM/DB 원본 reader는 고정 fixture다.
  2026-10-14 재실행에서도 2개 통과했으며, 일반/SSE 조회 중 DB 연결 시 실패하는 spy로 재조회 없음을 확인했다.
- 동시 색인/삭제·공유 KG 참조 경쟁·미완료 문서 상태 및 내부 인증 회귀 테스트.
- 별도 PostgreSQL Testcontainers에서 빈 팀/삭제 업무만 있는 팀 제외와 기존 권한 범위 보존 검증.
- semantic cache 벤치마크는 캐시를 유지하는 전사(`allowedTeamIds=null`) 경로만 측정한다.
  기존 실제 모델 실측 보고서를 새 결과로 덮어쓰지 않았다.
- Qdrant 서버 payload index 성능 측정, 운영 backfill, 실제 모델 평가 및 최신성 게이트 미실행.

Qdrant local에서는 payload index 성능을 검증할 수 없다는 경고가 나온다.
팀별 검색은 허용 팀마다 `top_k`를 적용한 뒤 후보를 합친다. 전체 팀 집합의 단일 top-k와
같은 랭킹 계약이 아니며, 키워드 추출/색인/저장 비용도 늘 수 있다. 속도 향상을 주장하지 않는다.

테스트는 `ai/`에서 실행한다.

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q
```

통합 테스트는 `RAG_SCOPE_TEST_ID`, `RAG_SCOPE_TEST_QDRANT_URL`,
`RAG_SCOPE_TEST_NEO4J_URI`를 별도 테스트 컨테이너의 loopback 임의 포트로 지정한 뒤
`tests/test_light_worklog_team_storage_integration.py`를 실행한다.
기본 서비스 포트 6333/7687은 거부하며 환경변수 미지정 시 skip한다.
테스트용 Neo4j 인증은 `neo4j/scope_contract_test`이고 운영 서버에 연결하지 않는다.

현재 로컬 venv는 Python 3.11.6이다. 프로젝트 기준 Python 3.12 검증은 CI/별도 환경에서 필요하다.
별도 lint/typecheck 도구를 새로 설치하지 않았다.
