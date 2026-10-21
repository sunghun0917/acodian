# AX-WMS LightRAG 동시성·정합성 분석

- 작성일: 2026-10-21
- 분석 대상: 활성 `ai/app/light/v4/` 및 설치된 `lightrag-hku==1.4.10`
- 작업 범위: 코드 수정 없는 현재 동작 분석 및 해결 방향 비교
- 검증: 관련 테스트 127개 통과
- 미수행: 실제 PostgreSQL 이전, 외부 DB 동시 요청 재현, 장애 주입, 성능 측정

## 1. 결론

두 문제 모두 코드상 근거가 있다. 다만 실제 외부 DB를 연결한 동시 요청에서 발생 빈도와 처리량은 아직 측정하지 않았다.

- **문제 A — 조회 정합성:** 조회와 수정이 공유하는 전체 작업 단위의 일관성 경계가 없다.
- **문제 B — 수정 직렬화:** 앱이 `ainsert()` 전체를 workspace 락으로 감싸므로, 오래 걸리는 추출·임베딩까지 같은 workspace에서 대기한다. 전역 workspace에서도 팀 간 경합이 가능하다.
- `PGKVStorage`로 교체하는 것만으로 두 문제가 해결되지는 않는다.
- workspace 락만 제거하는 것도 안전하지 않다. 추적 정보 갱신 유실뿐 아니라 LightRAG의 pipeline 완료 처리·삭제 거절 문제도 생긴다.

**권장 순서는 정합성 경계 확정 → 동시성 테스트 → 병목 측정 → 긴 작업과 공유 반영 구간 분리 → 필요할 때 버전 게시다. PG 이전은 이 순서와 분리해서 판단한다.**

## 2. 현재 구성과 실제 흐름

| 항목 | 현재 구성 |
|---|---|
| AI 서버 | FastAPI |
| LightRAG | `lightrag-hku==1.4.10` |
| KV | `JsonKVStorage` |
| 문서 상태 | `JsonDocStatusStorage` |
| 그래프 | Neo4j |
| 벡터 | Qdrant |
| 격리 단위 | 팀별 workspace, 별도의 전역 workspace에도 색인 |
| 검토 중 | 기본 `PGKVStorage`·`PGDocStatusStorage` 이전 |

이하 앱 경로는 `ai/app/light/v4/`, LightRAG 경로는 `ai/.venv/Lib/site-packages/lightrag/` 기준이다. 설치 소스 경로는 로컬 검증용이며 저장소에 포함되는 소스 경로가 아니다.

### 2.1 조회

```text
허용 팀별 workspace 확인
  → 그래프·벡터·청크 검색
  → 검색 결과에서 업무일지 ID 추출
  → full_docs의 worklog_source snapshot + doc_status 조회
  → 선행 업무일지 snapshot 추가 조회
  → 답변 문맥 구성
  → LLM 답변 생성
```

근거:

- `service/worklog_team_scope.py:80–115`
- `service/worklog_query_service.py:174–247`
- `service/lightrag_adapter.py:284–317`

현재 본문·메타데이터는 원본 업무 DB가 아니라 색인 snapshot에서 가져온다.

조회는 앱의 mutation 락을 획득하지 않는다. LightRAG의 `aquery_data()`도 pipeline의 `busy`가 끝날 때까지 기다리지 않는다.

현재 방어 수준은 다음과 같다.

| 상태 | 현재 처리 |
|---|---|
| 검색 후 문서가 삭제되어 `full_docs`에 없음 | 해당 문서를 제외 |
| snapshot 형식이 잘못됐거나 상태가 `processed`가 아님 | 조회 실패 |
| snapshot의 업무일지 ID·팀 ID가 기대값과 다름 | 조회 실패 |

일부 잘못된 근거 사용은 막지만, 검색 당시와 snapshot 조회 당시가 같은 상태였다는 보장은 없다.

### 2.2 신규 색인

```text
업무일지별 락 획득
  → 원본 DB에서 색인 자료 준비
  → 전역 workspace: 본문 색인
  → 전역 workspace: Custom KG 및 추적 정보
  → 전역 workspace: snapshot 저장
  → 팀 workspace: 본문 색인
  → 팀 workspace: Custom KG 및 추적 정보
  → 팀 workspace: snapshot 저장
  → 팀 스키마 marker 저장
```

근거: `service/worklog_index_service.py:227–259`, `service/worklog_team_scope.py:55–78`

본문·Custom KG·snapshot 전체를 한 번의 workspace 락으로 묶은 구조는 아니다. 본문 색인과 Custom KG 등 내부 단계에서 따로 획득·해제한다.

### 2.3 재색인·삭제

```text
업무일지별 락 획득
  → 전역 snapshot에서 기존 색인 팀 확인
  → 기존 팀 workspace 색인 삭제
  → 전역 workspace 색인 삭제
  → 재색인이라면 최신 원본을 조회해 다시 색인
```

근거: `service/worklog_index_service.py:139–165, 219–225`

재색인은 기존 데이터를 먼저 없앤 뒤 새 데이터를 만든다. 새 색인에 실패하면 자동으로 이전 정상 상태로 돌아가지 않는다.

### 2.4 저장소별 공개·반영 시점

| 저장소 | 현재 동작 |
|---|---|
| JsonKVStorage | `upsert/delete`가 메모리를 변경하면 이후 조회에서 보일 수 있음. callback은 JSON 파일 영속화 |
| JsonDocStatusStorage | 메모리 갱신 후 `upsert`에서 파일 저장도 수행 |
| Neo4j | 노드·관계 변경 호출별 트랜잭션으로 반영 |
| Qdrant | 개별 upsert/delete 요청별 반영. `wait=True`가 다른 저장소까지 묶지는 않음 |
| PGKVStorage로 교체 시 | 개별 SQL 실행별 반영. 색인 전체를 묶는 트랜잭션은 기본 제공 경로에 없음 |

**JSON의 flush나 LightRAG의 `index_done_callback()`은 전체 색인을 한 번에 공개하는 commit이 아니다.**

근거:

- `kg/json_kv_impl.py:77–177`
- `kg/json_doc_status_impl.py:199–207`
- `kg/neo4j_impl.py:1049–1060`
- `kg/qdrant_impl.py:558–560`

`_insert_done()`도 저장소별 callback을 각각 호출한다. 병렬 문서 중 하나가 namespace 전체 JSON을 저장할 때, 다른 문서의 처리 중 메모리 변경까지 함께 영속화할 수 있는 구조다. 개별 파일 저장과 파이프라인 전체 정합성을 구분해야 한다.

## 3. 문제 A와 B의 원인

### 3.1 문제 A: 저장소별 정상 처리와 업무 수정 전체 완료가 다름

기본 본문 색인도 다음처럼 진행된다.

```text
PROCESSING 상태·청크 KV·청크 벡터 저장
  → 엔티티·관계 추출
  → 그래프·벡터·추적 정보 병합
  → PROCESSED
  → 각 저장소 callback
```

여기에 앱이 Custom KG와 snapshot을 추가로 저장한다.

따라서 기본 `doc_status=processed`는 앱의 Custom KG와 snapshot까지 모두 완료됐다는 의미가 아니다.

근거: LightRAG `lightrag.py:1890–1939, 2022–2068`

또한 `worklog-team-scope-schema`는 호환성·초기 준비 marker다. 기존 marker가 남아 있는 상태에서 다음 수정이 시작되므로 수정 중 여부나 현재 게시 버전을 나타내지 않는다.

#### 확인된 사실과 미재현 위험

| 구분 | 내용 |
|---|---|
| 코드로 확인 | 조회와 수정 전체를 격리하는 공통 경계 없음 |
| 코드로 확인 | 검색과 snapshot 조회는 별도 단계 |
| 코드로 확인 | 재색인이 삭제부터 시작하며 여러 저장소를 단계적으로 변경 |
| 코드로 확인 | 삭제된 snapshot은 제외하고, 불완전한 snapshot은 오류 처리 |
| 아직 외부 DB로 미재현 | 동시 요청에서 실제 결과 누락·실패·서로 다른 시점의 데이터 조합 |
| 아직 미측정 | 발생 빈도, 영향받는 쿼리 비율, 사용자 지연 |

이 문제는 PG 기준으로 반드시 미커밋 데이터를 읽는 dirty read인 것은 아니다. 각 단계는 이미 커밋됐지만 업무 작업 전체는 미완료인 상태를 읽는 문제도 포함한다.

### 3.2 문제 B: 긴 앱 락과 LightRAG 내부 제약이 함께 존재

| 계층 | 보호 범위와 영향 |
|---|---|
| 업무일지 락 | 같은 업무일지 수정·삭제를 직렬화. 다른 업무일지는 통과 |
| 앱 workspace 락 | `ainsert()` 전체, 삭제, Custom KG 쓰기를 보호. 긴 색인 작업도 대기 대상 |
| LightRAG pipeline `busy` | 같은 workspace에서 queue processor 중복 실행 억제 |
| LightRAG entity/relation keyed lock | 같은 엔티티·관계 병합 충돌 보호 |
| JSON storage 락 | 개별 namespace 읽기·쓰기 보호. 여러 저장소 전체 작업은 보호하지 않음 |

근거:

- `service/worklog_mutation_lock.py:13–27`
- `service/lightrag_adapter.py:168–185, 229`
- LightRAG `lightrag.py:1668–1707`
- LightRAG `operate.py:2537–2554, 2638–2664`

#### 전역 workspace도 병목 후보

팀 색인에 앞서 공통 전역 workspace에 먼저 쓴다. 따라서 서로 다른 팀의 업무일지도 전역 본문 색인 구간에서 같은 락을 기다릴 수 있다. 경합 가능성은 코드로 확인했지만 비중은 아직 측정하지 않았다.

#### LightRAG 내부는 문서 하나씩만 처리하지 않음

queue processor 내부에서는 `max_parallel_insert`의 semaphore로 여러 문서를 병렬 처리한다. 앱이 요청별 `ainsert()` 전체를 잠그는 것과는 다른 구조다.

근거: LightRAG `lightrag.py:1777–1778, 2150–2152`

#### 앱 락 제거 후에는 완료 의미도 달라짐

pipeline이 이미 바쁘면 후속 호출은 `request_pending=True`로 표시하고 돌아올 수 있다. 현재 어댑터는 `ainsert()`가 반환되면 곧바로 문서가 `processed`인지 검사한다.

따라서 단순 락 제거 시 다음 문제가 생길 수 있다.

- 아직 처리 중인 요청을 실패로 판단
- ingestion 중 삭제가 `not_allowed`로 거절됨
- Custom KG 추적 목록의 갱신 유실

LightRAG의 Custom KG 삽입은 기본 ingestion의 `busy` 예약 경로를 통과하지 않으므로 내부 pipeline만 믿어서도 안 된다.

근거: LightRAG `lightrag.py:1668–1707, 2263–2436, 3032–3076`

## 4. 기본 PG 저장소 이전의 효과와 한계

### 4.1 얻을 수 있는 것

- JSON 파일 영속화를 DB 기반 영속화로 전환
- SQL 문장 단위 원자성
- DB 트랜잭션·행 잠금·원자적 갱신을 활용할 기반
- 여러 프로세스가 동일한 KV 데이터를 조회하는 기반

기반을 제공하는 것과 현재 코드가 그 기능을 사용하는 것은 다르다.

설치된 `PGKVStorage.upsert()`는 항목별로 `db.execute()`를 호출하고, 실행마다 pool에서 connection을 얻는다. 현재 앱의 색인 전체를 하나의 transaction으로 묶는 인터페이스가 아니다. `PGDocStatusStorage`도 항목별로 실행한다.

근거: `kg/postgres_impl.py:468–478, 1664–1678, 2190–2296, 3821–3846`

### 4.2 자동 해결되지 않는 것

- KV·Neo4j·Qdrant 사이의 일관된 읽기
- 여러 PG 조회가 동일 시점을 보는 것
- 삭제 후 재색인 실패 시 이전 상태 복원
- 공유 추적 목록의 read-modify-write 갱신 유실
- 앱 workspace 락과 LightRAG pipeline 제약
- 앱의 `asyncio.Lock`을 다른 worker와 공유하는 것

PostgreSQL의 기본 `READ COMMITTED`는 문장마다 snapshot이 달라질 수 있다. 같은 PG 안에서도 여러 조회의 동일 시점 읽기가 필요하면 트랜잭션 경계와 적절한 격리 수준이 필요하다. 그것도 Neo4j·Qdrant까지 확장되지는 않는다.

참고: [PostgreSQL 트랜잭션 격리 문서](https://www.postgresql.org/docs/18/transaction-iso.html)

### 4.3 현재 데이터의 PG 호환성

| 데이터 | 기본 PG 저장소와의 관계 |
|---|---|
| `confirmed:extract:{chunk_id}` | 기본 cache 필드로 저장 가능 |
| 확정 KG 출처 청크 | 기본 text_chunks 구조로 저장 가능 |
| `llm_cache_list`, 문서·엔티티·관계 추적 정보 | 기본 구조 유지 대상 |
| `worklog_source` 및 형식 버전 | 현재 `full_docs`에 추가한 필드는 기본 PG 매핑에서 저장되지 않음 |
| `worklog-team-scope-schema` | 현재 레코드는 `content`가 없어 그대로 PG full_docs에 upsert하면 실패 |

근거: `kg/postgres_impl.py:2195–2240`

confirmed 캐시와 출처 청크 때문에 별도 PG 저장소가 필요한 것은 아니다. 현재 snapshot·marker 사용처를 정리하지 않은 채 설정만 PG로 바꾸는 것이 직접적인 호환성 문제다.

문서·청크·엔티티·관계의 추적 필드는 LightRAG 기본 삭제·재구성 구조다. 앱이 추가한 확정 KG의 기여분도 여기에 등록한다는 점을 앱 독자 스키마와 구분해야 한다.

### 4.4 제거·대체 검토 중인 앱 데이터

| 데이터 | 검토 방향 | 주의점 |
|---|---|---|
| `worklog_source` | 검색 후 원본 업무 DB 조회로 대체 | 옛 색인 후보에 최신 본문을 붙이는 의미 변화, 현재 권한·팀·삭제 검증 |
| `worklog_source_schema_version` | snapshot 제거 시 함께 제거 | snapshot을 계속 사용한다면 호환성 검사도 유지 |
| `worklog-team-scope-schema` | 구버전 혼재 정리 후 제거 검토 | 팀 격리·workspace 검증까지 제거하면 안 됨 |
| 색인 당시 팀 정보 | API의 기존 팀 ID 전달 또는 최소 소유권 정보 보존 | 현재 팀 ID만으로 이동 전·영구 삭제 후 색인 위치를 찾을 수 없음 |
| `confirmed:extract:*` 및 확정 KG 출처 청크 | 기본 cache·chunk 구조로 보존 | 현재 Custom KG 삭제·공유 노드 재구성에 필요 |

snapshot을 최신 업무 DB 조회로 대체하면 중복은 줄지만, 이것을 동일 버전 조회라고 볼 수는 없다.

## 5. 최소 변경안 비교

### 5.1 먼저 구분할 요구사항

1. 잘못 섞인 결과를 반환하지 않으면 된다. 잠깐 기다리거나 재시도해도 된다.
2. 수정 중에도 기다리지 않고 이전 완료 버전으로 정상 조회해야 한다.

현재의 제자리 삭제·갱신 구조에서는 2번이 훨씬 어렵다.

| 안 | 읽기 정합성 | 같은 팀 수정 병렬성 | 한계 |
|---|---|---|---|
| A. 전체 수정과 문맥 조회에 공통 읽기/쓰기 경계 | 정상 완료 구간끼리 격리 가능 | 낮음 | 수정 중 조회 대기. 실패 복구 전 공개 방지도 필요 |
| B. 긴 준비 작업 병렬화 + 짧은 반영 구간 보호 | 반영 중 조회를 막으면 가능 | 준비 작업은 병렬 | LightRAG의 추출·반영 분리와 완료 처리 조정 필요 |
| C. 변경 세대 검증 후 조회 재시도 | 겹친 결과를 버리는 방식으로 보호 가능 | writer 측 개선은 별도 | 쓰기가 계속되면 조회 재시도·기아. 실패 상태 관리 필요 |
| D. 별도 버전에 작성 후 게시 전환 | 이전 완료 버전 조회 가능 | 공유 데이터 설계에 따라 가능 | 변경량·저장 비용·정리 복잡도가 가장 큼 |

### 5.2 A: 당장 정합성부터 확보하는 가장 작은 방향

읽기 보호 범위:

```text
검색 후보 획득 → snapshot/본문·선행 문서 조회 → 답변 문맥 확정
```

쓰기 보호 범위:

```text
기존 삭제 → 새 본문 → Custom KG → snapshot → 완료 확인
```

LLM 답변 생성·SSE 전송 전체까지 읽기 락을 유지할 필요는 없다.

현재 단계별 workspace 락에 조회만 참여시키면 단계 사이의 미완료 상태를 읽을 수 있다. 상위의 전체 작업 경계를 잡아야 하며, 기존 비재진입 락을 그대로 중첩 획득하면 교착될 수 있다.

수정 실패 후 락만 풀면 부분 상태가 공개된다. 복구 전 해당 범위를 조회 불가로 처리하는 등 실패 정책까지 있어야 정합성 대책이 완성된다.

작은 안정화안이지만 문제 B를 해결하는 최종안은 아니다.

### 5.3 B: 두 목표를 절충하는 우선 검토안

```text
원본 준비·추출·임베딩 등 긴 작업: 가능한 범위에서 병렬
공유 그래프·추적 정보 반영: 필요한 범위만 보호
조회: 반영 구간에만 대기
```

긴 계산 때문에 팀 전체가 기다리는 시간을 줄이는 것을 목표로 한다.

그러나 현재 `ainsert()`는 계산과 저장을 함께 수행한다. 락 위치 한 줄을 옮기는 작업은 아니며 다음을 함께 다뤄야 한다.

- pipeline의 요청별 완료 대기
- ingestion과 삭제의 실행 조정
- Custom KG와 native 경로가 공유하는 자원 잠금
- 추적 목록의 원자적 변경
- 반영 실패 시 재시도·복구

공유 노드 재구성에도 LLM 작업이 포함될 수 있어 반영 구간이 실제로 짧아지는지는 측정해야 한다.

### 5.4 C: 변경 세대 검증과 재시도

조회 전후의 변경 세대와 진행 중인 수정 여부를 검사하여, 수정과 겹친 결과는 반환하지 않고 재시도하는 방향이다. 전체 조회를 긴 읽기 락으로 보호하지 않는 대안이지만 이전 버전 데이터를 제공하는 방식은 아니다.

여러 writer가 겹치는 경우, writer 실패, worker 종료, 여러 팀 조회까지 포함하는 공통 프로토콜이 필요하다. 완료 시 숫자 하나만 올리거나 시작·종료 상태를 프로세스 메모리에만 두는 것으로는 충분하지 않다.

### 5.5 D: 버전 분리·게시가 필요한 조건

수정 중에도 이전 완료 상태로 계속 조회해야 한다면 별도 버전에 준비하고 완료 후 게시하는 방식이 유력하다.

문서·청크에만 버전을 붙이면 부족하다. 공유 엔티티 설명, 공유 관계와 벡터, source 추적 목록이 기존 조회 버전과 함께 제자리 변경되면 여전히 섞인다.

팀 workspace 전체를 새 세대로 만드는 방법은 격리 경계가 명확하지만 재색인 비용이 크다. 문서별 버전은 비용을 줄일 수 있지만 공유 노드 기여분 처리까지 바뀐다.

지금 버전 테이블부터 추가하는 것을 최소 변경안으로 권하지는 않는다.

## 6. 락 축소·DB 원자적 갱신·버전 게시의 필요성

### 6.1 락 범위 축소

문제 B를 개선하려면 필요하다. 전체 락 제거가 아니라 긴 계산과 공유 변경의 분리가 먼저다.

전역 workspace에 본문·KG를 중복 색인하는 비용이 있으므로, 전역 색인의 실제 소비자를 확인해 불필요하다면 줄이는 것이 우선 후보다. 현재 전역 snapshot은 기존 팀 소유권 조회에 사용하므로 무작정 삭제할 수 없다.

### 6.2 DB 원자적 갱신 또는 동등한 충돌 보호

공유 추적 목록을 병렬 변경하려면 필요하다.

현재 `_extend_record()`:

```text
기존 목록 읽기 → 항목 추가 → 목록 전체 덮어쓰기
```

근거: `store/worklog_custom_kg_tracking.py:9–16`

PG의 UPSERT도 애플리케이션에서 이미 계산한 낡은 목록을 덮어쓰면 갱신 유실을 막지 못한다.

대안은 다음 중 하나다.

- 동일 자원 락 안에서 read-modify-write
- DB 안에서 목록 추가·제거를 원자적으로 수행
- 행 잠금 또는 낙관적 충돌 검사·재시도

삽입 경로만 바꾸면 부족하다. native 삭제·재구성도 같은 규칙에 참여해야 한다. 목록뿐 아니라 그래프 설명과 벡터의 병합 충돌도 별도로 보호해야 한다.

### 6.3 버전 게시

- 짧은 조회 대기를 허용한다면 필수는 아니다.
- 수정 중 이전 정상 버전을 계속 제공하려면 필요성이 커진다.
- 단순 `processed=True`나 완료 marker 추가만으로 기존 데이터가 보존되지는 않는다.

## 7. 실패·삭제·팀 이동·다중 worker

| 상황 | 현재 위험 | 필요한 처리 방향 |
|---|---|---|
| 삭제 후 재색인 실패 | 이전 정상 색인이 이미 없음 | 원본으로 재색인·정리 재시도. 이전 상태 계속 제공이 필요하면 별도 버전 보존 |
| Custom KG 반영 중 실패 | 추적 정보와 실제 KG가 일부만 존재 | 현재 선기록 추적·confirmed 캐시를 유지하고 삭제/재구성의 반복 실행 가능성 검증 |
| 팀 이동 | 현재 DB의 새 팀만으로 옛 색인 위치를 찾을 수 없음 | 기존 색인 팀 정보를 보존하거나 변경 이벤트에 전달 |
| 영구 삭제 | 원본에서 팀·복구 정보를 다시 얻을 수 없음 | 삭제 전에 색인 소유권 확보, 정리 완료까지 필요한 최소 정보 보존 |
| 공유 사용자·태그 노드 | 한 문서 삭제가 다른 문서 기여분에 영향 | 남은 chunk·confirmed 캐시 기반 재구성 및 동시 추가와의 충돌 보호 |
| timeout·취소 | 일부 외부 쓰기는 이미 반영됐을 수 있음 | timeout을 rollback으로 취급하지 말고 상태 확인·정리·재시도 |
| 다중 worker | 현재 업무일지/workspace asyncio 락은 공유되지 않음 | worker 간 공통 조정 또는 명시적 단일 writer 운영 필요 |

앱 락은 이벤트 루프별 프로세스 로컬 락이다. LightRAG의 multiprocess 공유 락도 같은 공유 런타임 범위의 기능이며, 독립 worker·서버가 같은 PG에 연결한다고 자동 공유되지 않는다.

근거: `service/worklog_mutation_lock.py:10–24`, LightRAG `kg/shared_storage.py:1222–1256`

팀 이동·권한 변경은 일반적인 색인 지연과 구분해야 한다. 이전 완료 버전을 유지하더라도 현재 접근 권한이 사라진 문서를 계속 제공하면 안 된다.

추가로 삭제 API는 현재 `any_success`일 때만 조회 캐시 버전을 올린다. 부분 삭제 후 모두 실패로 반환되는 경우의 캐시 무효화도 장애 테스트에 포함해야 한다.

근거: `service/worklog_index_service.py:194–215`

## 8. 구현 전 필요한 테스트와 측정

### 8.1 정합성 재현 테스트

`sleep`으로 타이밍을 추측하지 말고 이벤트·배리어로 실행 순서를 고정한다.

| 테스트 | 고정할 중단 지점 | 확인할 결과 |
|---|---|---|
| 조회 중 삭제 | 후보 ID 획득 직후 | 누락·오류·재시도 정책 |
| 조회 중 재색인 | 기존 삭제 후, 새 snapshot 저장 전 | 서로 다른 버전 조합 여부 |
| 저장소 부분 갱신 | Qdrant 완료 후 Neo4j/KV 대기 | 미완료 데이터가 문맥에 들어가는지 |
| Custom KG 실패 | 추적 정보 저장 후 native KG 실패 | 정리 후 다른 문서 기여분 보존 |
| 공유 노드 동시 수정 | 두 요청이 같은 추적 목록을 읽은 직후 | 양쪽 기여분 유지 |
| 삭제와 다른 문서 추가 | 공유 노드 재구성 직전 | 새 기여분 유실 여부 |
| 팀 이동·영구 삭제 | 원본 팀 변경/행 삭제 후 정리 | 옛 workspace 잔존 및 권한 누출 여부 |
| worker 간 동시 수정 | 별도 프로세스로 같은 문서·공유 노드 수정 | 프로세스 로컬 테스트와 차이 |

### 8.2 LightRAG 동작 검증

- pipeline이 busy일 때 후속 `ainsert()`의 반환 시점
- 반환 후 현재 status 검사에서 발생하는 실패
- ingestion 중 삭제의 `not_allowed`
- timeout·worker 종료 후 재처리 가능 여부
- JSON→PG 전환 후 confirmed 캐시·출처 청크·기본 추적 정보 왕복 보존

### 8.3 처리량 측정

다음 조합을 각각 비교한다.

- 같은 팀 / 다른 팀
- 공유 노드가 많은 문서 / 적은 문서
- 동시성 1·2·4·8
- 조회만 / 수정만 / 조회·수정 혼합

측정 항목:

- 업무일지 락·전역 workspace 락·팀 workspace 락의 대기시간과 보유시간
- 추출·임베딩·그래프 병합·삭제 재구성 시간
- 조회 p50/p95/p99, 오류·재시도율
- 재색인 처리량과 완료 지연
- 결과 누락·버전 혼합·추적 정보 유실
- 장애 후 복구시간과 잔존 데이터

### 8.4 이번에 실행한 검증

`ai/`에서 다음 관련 테스트를 실행했다. bytecode와 pytest cache 파일 생성을 비활성화했다.

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'
./.venv/Scripts/python.exe -B -m pytest -p no:cacheprovider tests/test_light_worklog_custom_kg_tracking.py tests/test_light_worklog_extraction_cache.py tests/test_light_worklog_team_scope.py tests/test_light_worklog_v4_index_service.py tests/test_lightrag_v4_adapter.py -q
```

결과: **127 passed in 4.00s**

이 결과는 관련 기존 회귀 테스트의 통과다. 실제 Neo4j·Qdrant·PG 동시 요청의 정합성이나 처리량을 검증한 결과가 아니다.

## 9. 최종 권고와 검증 한계

PG 교체부터 시작하기보다 먼저 조회가 허용하는 대기 수준과 실패 시 공개 정책을 정하고 동시성 테스트를 만드는 것이 맞다. 그다음 전역 중복 색인 비용과 긴 workspace 락을 줄이되, native pipeline 및 공유 추적 갱신을 함께 조정해야 한다.

불필요한 테이블·커스텀 저장소·추상화를 먼저 추가하지 않는다. 특히 confirmed 캐시와 출처 청크를 유지하기 위해 커스텀 PG 저장소가 필수라고 가정하지 않는다.

**미수행:** 실제 PG 이전, 외부 DB 동시 요청·장애 주입·성능 측정. 분석 시점의 설치된 LightRAG 1.4.10 동작을 기준으로 하며 최신 upstream 개선을 포함하지 않는다.

**핵심 위험:** PG 전환이나 workspace 락 제거만 먼저 적용하면 정합성은 해결되지 않은 채 기존 보호만 약해질 수 있다.
