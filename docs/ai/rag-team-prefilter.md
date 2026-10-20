# LightRAG v4 팀별 검색

## 현재 상태

업무일지 검색·색인·재색인·삭제 API는 **v4만 사용**한다. v3 소스와 테스트는 이전 버전으로 보존하고, v3 라우터는 현재 서버에 등록하지 않는다.
팀 제한 검색은 팀 workspace의 후보·processed 상태를 확인한 뒤 업무 PostgreSQL의 현재 원본으로 문맥을 만든다.
KV·문서 상태는 별도 DB의 기본 PG 저장소를 사용한다. [DB 준비·재색인 절차](lightrag-postgres-setup.md)를 따른다.
로컬 업무 데이터의 전체 초기 색인과 팀별·전사·SSE 실제 조회 스모크를 완료했다. 실행 결과와 실패 후 재시도 한계는 [PG 운영 가이드](lightrag-postgres-setup.md)에 기록한다. 광범위한 검색 정답 품질 평가는 별도다.

```text
API가 계산한 allowedTeamIds
  → 허용 팀 workspace만 검색 (Qdrant + Neo4j + KV)
  → 업무일지 후보 ID 합치기
  → 같은 workspace의 processed 상태 확인
  → 업무 DB에서 현재 본문·작성자·팀·태그·접근 가능한 선행 관계 구성
  → Gemini 응답 / SSE
```

`allowedTeamIds=null`인 전사 조회는 기존처럼 전역 workspace에서 처리한다. 최신 업무 DB 문맥 구성은 팀 제한 조회 경로에 적용한다.

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
| PG KV·문서 상태 | 전용 DB의 기본 `LIGHTRAG_*` 테이블, `workspace` 조건으로 구분 |

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
  후속 Custom KG·전역 소유권 저장·팀 색인을 진행한다.
- 전역 `doc_status.metadata.indexed_team_id`에 팀 ID만 보존한 뒤 팀 사본을 쓴다.
- `worklog_source` snapshot·형식 버전·팀 schema marker는 제거했다. 기본 `full_docs.content`는 native 색인을 위해 유지한다.
- 다른 팀 또는 소속 미확인 선행 업무는 팀 KG에 넣지 않는다.
- 조회 시 선행 업무도 현재 DB에서 재조회하고 현재 팀·삭제·처리 상태를 확인한 경우만 문맥에 넣는다.
- 삭제/재색인은 저장된 소속으로 팀 사본을 먼저 삭제하고 전역 사본을 마지막에 삭제한다.
- native 삭제의 `fail`/`not_allowed`/알 수 없는 상태는 실패로 처리해 전역 소유권을 남긴다.
  이미 없는 문서의 `not_found`만 재시도를 위한 멱등 성공으로 취급한다.
- 확정 Custom KG의 청크·엔티티·관계도 native 문서 삭제 추적에 등록한다.
  공유 작성자/태그를 복원할 확정 데이터는 extraction-cache 형식으로 보존한다.
  이 데이터는 LLM 추측 결과가 아니며, 남은 문서의 확정 관계를 재구성할 때 사용한다.
  이 보완은 전역/팀 사본에 공통 적용되며, **과거 미추적 KG의 자동 정리는 하지 않는다.**
- 미완료 문서는 문맥에서 제외하고 잘못된 상태 레코드·검색 실패 시 전역 검색으로 우회하지 않는다.
- 일반/SSE가 같은 후보·권한 문맥 경로를 사용한다. `[]`는 adapter/DB/LLM 호출 없이 종료한다.
- 팀 제한 검색은 구 응답 캐시를 우회한다. 전사 경로의 캐시는 유지한다.
- 본문 색인이 일부만 성공하거나 timeout/실패하면 이미 쓰인 데이터가 있을 수 있으므로
  전사 캐시도 무효화한다. 쓰기 전 실패였더라도 보수적으로 캐시 버전이 증가할 수 있다.
- index/reindex/delete/query는 내부 토큰을 요구한다. Java 색인 클라이언트도 토큰 없이 호출하지 않는다.
- API의 기존 권한 SQL에서 미삭제 업무가 없는 팀을 검색 대상에서 제외한다.
  AI도 검색 후 업무 DB를 다시 조회하여 현재 소속과 삭제 여부를 확인한다.
  업무가 있지만 v4 색인되지 않은 팀은 전체 backfill이 필요하다. marker로 준비 여부를 보장하지 않는다.

기본 Qdrant 초기화에는 legacy collection 자동 이관/일부 삭제가 있다.
전역·팀 workspace 초기화에는 기존 안전한 인스턴스 초기화 함수를 연결하여 **전역 legacy 데이터를
팀 데이터로 재분류하거나 legacy collection을 삭제하지 않는다**.
기존 collection의 벡터 차원을 확인하고, 신규 collection과 workspace index만 구성한다.
이 경계는 1.4.10의 private storage 속성을 쓰므로 라이브러리 업그레이드 시 재검증이 필요하다.

## 남은 작업과 검증

팀 검색을 끄는 설정은 없다. **기존 전역 인덱스만으로는 팀 제한 검색이 동작하지 않는다.**
새 환경에서는 기존 업무일지를 v4 팀 workspace에 색인해야 한다. 현재 API 경로 전환만으로 과거 데이터가 자동 backfill되지는 않는다.

1. 격리된 실제 Qdrant/Neo4j 환경의 두 팀·공유 사용자/태그 저장→조회 roundtrip은 검증했다.
2. 로컬 전역/팀 초기 색인은 완료했으며 운영 환경의 **전체 backfill**은 별도로 검증한다. 빈 팀은 API 검색 대상에서 제외하고,
   미색인·미완료 문서는 후보가 없거나 processed 필터에서 제외된다.
   과거 미추적 Custom KG가 있는 인덱스는 새 namespace로 전체 구축해야 한다.
   기존 인덱스에 일반 재색인만 호출해도 잔존 KG가 모두 사라진다고 가정하면 안 된다.
3. 삭제/팀 비활성화/이름 변경 및 outbox 미처리 상태를 원본 커밋과 연결해 검증한다.
4. 모델/차원/working directory 변경 또는 원본과 인덱스가 다른 기간 후 새 색인을 검증한다.
   PG 전환 시 새 base workspace로 구축하여 이전 팀 사본과 섞지 않는다.
5. 원본 변경의 authoritative version과 인덱스 완료 version, 캐시·SSE 중간 변경을 검증한다.
6. 전체 backfill 후 실제 업무일지 검색에서 작성자·팀 이름과 ID 문맥, 답변을 검증한다.

현재 변경은 위 3–6의 최신성 게이트/API mutation 전파 및 운영 환경 backfill 완료를 보장하지 않는다.
수동 팀 이동·원본 수정 이후 outbox 처리 전에는 팀 검색 사본도 오래된 상태일 수 있다.
팀 조회의 본문은 현재 원본이지만 검색 후보·그래프·벡터는 갱신 전일 수 있다. 동일 시점 읽기를 보장하지 않는다.

## 검증 증거와 한계

현재 PG 전환의 실행 절차·회귀 테스트 명령·로컬 색인 결과는 [PG 운영 가이드](lightrag-postgres-setup.md)를 따른다. 이 문서에는 팀 검색 경계의 검증 범위만 둔다.

- fake adapter/source로 일반/SSE, 현재 원본 재조회, 이전 팀 자료 제외, 빈 scope, 실패 경로를 검사한다.
- 실제 LightRAG 생성자와 초기화 이후 vector/graph/KV workspace 경계 및 설정 불일치 시 자원 정리를 검사한다.
- Qdrant local in-memory 엔진으로 본문·엔티티·관계의 payload filter와 top-k 사전 격리를 검사한다. payload index 성능은 이 엔진에서 검증할 수 없다.
- Neo4j storage가 드라이버에 전달하는 workspace 한정 Cypher와 native Custom KG 삭제·공유 노드 재구성을 검사한다. 메모리 store/드라이버 대역 검사를 실제 서버 장애 복구 증거로 간주하지 않는다.
- 기존 두 팀·공유 사용자/태그의 Qdrant/Neo4j 통합 테스트 2개 통과 이력은 PG 전환 전 JSON snapshot 기반의 과거 증거다. 현재 PG 통합 테스트로 대체 해석하지 않는다.
- PG를 포함한 별도 저장소 통합 테스트는 명시적인 격리 테스트 DB·포트가 필요하다. 일반 로컬 업무 DB에 실행하지 않는다.

팀별 검색은 허용 팀마다 `top_k`를 적용한 뒤 후보를 합친다. 전체 팀 집합의 단일 top-k와 같은 랭킹 계약이 아니며, 전역/팀 중복 색인으로 저장·추출 비용이 늘 수 있다. 속도 향상을 주장하지 않는다.
