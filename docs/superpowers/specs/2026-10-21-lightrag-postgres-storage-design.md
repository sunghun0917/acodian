# LightRAG 기본 PostgreSQL 저장소 전환 설계

- 작성일: 2026-10-21
- 상태: 사용자 승인 후 직접 구현·독립 리뷰 보완 완료, 실제 PG 통합 검증·전체 재색인 대기
- 대상: AX-WMS 활성 `ai/app/light/v4/`, `lightrag-hku==1.4.10`
- 관련 분석: [동시성·정합성 분석](../../ai/lightrag-concurrency-consistency-analysis.md)
- 이 문서는 승인된 설계 명세다. 코드 구현은 반영했으며 DB 생성·데이터 초기화·재색인은 수행하지 않았다.

## 1. 목적과 성공 기준

현재 JSON 기반 KV·문서 상태 저장소를 LightRAG 기본 PostgreSQL 구현으로 전환한다. 배포 전 개발 환경이므로 기존 색인 데이터를 이관하거나 구버전과 호환하지 않고, 새 저장소에 전체 재색인한다.

성공 기준:

1. `PGKVStorage`·`PGDocStatusStorage`가 같은 LightRAG 전용 DB를 사용한다.
2. 검색 후 본문·업무 메타데이터는 기존 업무 DB에서 조회한다. 전체 업무일지 snapshot을 색인에 중복 저장하지 않는다.
3. 색인 당시 팀 ID는 기본 `doc_status.metadata.indexed_team_id`에 보존하여 팀 이동·영구 삭제 후에도 기존 색인 위치를 찾는다.
4. confirmed 추출 캐시·확정 KG 출처 청크·기본 추적 정보는 재색인 과정에서 PG에 생성되며, 기존 삭제·공유 노드 재구성 기능을 유지한다.
5. 팀 workspace 격리와 현재 접근 범위·삭제 여부 검증을 유지한다.
6. 새 커스텀 PG 저장소·업무 테이블·별도 저장소 추상화를 추가하지 않는다.

## 2. 확정된 범위와 제외 사항

### 포함

- 같은 PostgreSQL 서버 안의 별도 LightRAG DB 사용.
- 기본 JSON 저장소 두 종류를 기본 PG 저장소 두 종류로 교체.
- 업무일지 snapshot 및 형식 버전 제거.
- 팀 스키마 marker와 해당 marker 기반 호환성 검사 제거.
- 최신 업무 원본 조회로 검색 문맥·참조 구성.
- 기존 팀 소유권 조회를 `doc_status.metadata` 기반으로 변경.
- 관련 설정·문서·회귀 테스트·외부 저장소 통합 테스트 정비.

### 제외

- JSON 데이터 변환·이관 도구, 구버전 snapshot 호환 코드.
- 무중단 전환, 자동 이중 쓰기, 버전 게시·세대별 동시 서비스.
- 전역 workspace 중복 색인 제거.
- 업무일지/workspace 락 축소·제거, 다중 worker 쓰기 조정 개선.
- KV·Neo4j·Qdrant를 하나의 트랜잭션으로 묶는 기능.
- 원본 업무 DB와 색인의 동일 시점 snapshot 보장.
- Neo4j·Qdrant 교체, LightRAG 버전 업그레이드.
- API 서버의 요청 계약 변경, 새로운 권한 체계.

PG 전환은 동시성 문제 A·B의 해결 완료를 의미하지 않는다. 현재 앱 락과 native pipeline 제약은 유지한다.

## 3. 저장 구조와 연결 경계

```text
기존 PostgreSQL 서버
├─ 기존 업무 DB
│  └─ 업무일지·팀·사용자·태그·의존 관계
└─ axwms_lightrag                 # LightRAG 전용 DB 이름
   ├─ LIGHTRAG_DOC_FULL
   ├─ LIGHTRAG_DOC_CHUNKS
   ├─ LIGHTRAG_LLM_CACHE
   ├─ LIGHTRAG_FULL_ENTITIES / LIGHTRAG_FULL_RELATIONS
   ├─ LIGHTRAG_ENTITY_CHUNKS / LIGHTRAG_RELATION_CHUNKS
   └─ LIGHTRAG_DOC_STATUS

Neo4j: 기존 그래프 저장소 유지
Qdrant: 기존 벡터 저장소 유지
```

- 업무 DB 조회는 기존 `app/store/session.py`의 session factory와 업무 DB 연결 설정을 유지한다.
- LightRAG는 별도 PG 연결 설정으로 `axwms_lightrag`에 접속한다. 기존 업무 DB용 DSN을 이 DB로 바꾸지 않는다.
- 전역과 모든 팀 adapter는 같은 LightRAG DB 안에서 `workspace`로 구분한다. 팀마다 DB를 만들지 않는다.
- 기본 PG 저장소의 테이블 생성·초기화 기능을 사용한다. 앱이 같은 테이블을 별도 ORM/Alembic 모델로 중복 관리하지 않는다.
- 전용 계정은 LightRAG DB 초기화·읽기·쓰기 권한을 갖고 업무 테이블 변경 권한은 갖지 않도록 분리한다. 업무 원본 조회 연결은 별도로 유지한다.
- 같은 서버의 CPU·메모리·I/O·장애는 공유한다. DB 분리를 성능·장애의 물리적 격리로 설명하지 않는다.

LightRAG 생성 인자:

```python
kv_storage="PGKVStorage"
doc_status_storage="PGDocStatusStorage"
```

벡터·그래프는 기존 Qdrant·Neo4j 설정을 유지한다. 불필요한 PostgreSQL 벡터 기능은 비활성화한다.

### 3.1 설정 전달과 workspace 안전성

- 기존 `Settings`를 설정 진입점으로 사용하고 `.env.example`에 LightRAG 전용 접속 항목을 설명한다. 실제 비밀값은 문서·테스트·로그에 넣지 않는다.
- LightRAG 1.4.10은 `POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DATABASE`를 읽는다. Pydantic이 `.env`를 읽는 것만으로 native의 `os.environ` 조회가 설정되는 것으로 가정하지 않는다. 기존 adapter의 외부 저장소 설정 전달 패턴에 맞춘다.
- `POSTGRES_ENABLE_VECTOR=false`로 PG 벡터 사용을 끈다. Qdrant를 그대로 사용한다.
- `POSTGRES_WORKSPACE` 또는 `config.ini`의 `[postgres].workspace`가 전역/팀 workspace를 강제 덮어쓰지 못하게 한다. 해당 override가 있으면 초기화 전에 명확한 설정 오류로 거절한다.
- storage 초기화 전뿐 아니라 초기화 후에도 PG storage의 실제 workspace가 기대값과 같은지 검증한다. 불일치 시 adapter를 사용 가능 상태로 공개하지 않고 연결을 정리한다.
- 기존 Qdrant·Neo4j namespace 검증은 제거하지 않는다.

근거: 설치 소스 `kg/postgres_impl.py:1710–1767, 1860–1868, 1893–1910`, 앱 `service/lightrag_adapter.py:483–519, 559–569`.

## 4. 데이터별 처리

| 데이터 | 전환 후 처리 |
|---|---|
| `full_docs.content` | 기본 문서 본문으로 유지. native 색인·재처리를 위해 필요 |
| `worklog_source` | 저장·조회 제거 |
| `worklog_source_schema_version` | 제거 |
| `worklog-team-scope-schema` | marker 저장·검사 제거 |
| `doc_status.metadata.indexed_team_id` | 전역 workspace의 문서 상태에 색인 팀 ID 저장 |
| `confirmed:extract:{chunk_id}` | 기본 `LIGHTRAG_LLM_CACHE`에 재생성 |
| 확정 KG 출처 청크 | 기본 `LIGHTRAG_DOC_CHUNKS`에 재생성 |
| `llm_cache_list` 및 문서·엔티티·관계 추적 필드 | 기본 구조와 기존 생성 로직 유지 |

snapshot 제거는 LightRAG 기본 `full_docs`·`text_chunks`까지 없앤다는 뜻이 아니다. confirmed 캐시와 출처 청크도 제거하지 않는다.

## 5. 색인 당시 팀 ID와 변경 흐름

### 5.1 저장 위치와 형식

전역 workspace의 `worklog-{id}` 문서 상태 레코드에 다음 필드를 추가한다.

```json
{
  "metadata": {
    "indexed_team_id": 10
  }
}
```

위 예시는 추가 필드만 보여준다. 실제로는 기존 상태 레코드 전체와 기존 metadata를 보존하고 해당 필드만 병합한다. metadata·문서 상태를 팀 ID만 있는 값으로 덮어쓰지 않는다.

- 팀 ID는 양의 정수로 검증한다.
- 이 값은 마지막으로 색인에 사용한 팀 ID다. 최신 업무 DB의 현재 팀 ID와 의미가 다르다.
- `PGDocStatusStorage.upsert()`가 metadata를 저장하는 것은 확인했다. native 본문 재처리는 metadata를 교체할 수 있으므로 본문 처리 완료 후 소유권을 기록하며, 이후 앱의 상태 갱신은 기존 metadata를 보존한다.

근거: 설치 소스 `kg/postgres_impl.py:3768–3846`, 앱 `store/worklog_custom_kg_tracking.py:75–78`.

### 5.2 신규 색인

```text
기존 업무일지 락 획득
→ 업무 DB에서 색인 입력 일괄 조회
→ 전역 본문 색인 및 processed 확인
→ 전역 Custom KG·추적 정보 저장
→ 전역 doc_status에 indexed_team_id 저장
→ 팀 본문 색인
→ 팀 Custom KG·추적 정보 저장
→ 요청 결과 및 기존 캐시 무효화 처리
```

- 전체 snapshot 저장과 팀 marker 게시 단계는 제거한다.
- 팀 색인 전에 소유권 저장을 완료한다. 팀 쓰기가 일부 실패해도 정리 대상 workspace를 찾을 수 있어야 한다.
- 소유권 저장에 실패하면 팀 색인을 시작하지 않고 요청 실패로 처리한다.
- 기존 업무일지 락·workspace mutation 락과 timeout 처리는 유지한다.

### 5.3 삭제와 재색인

```text
전역 doc_status에서 indexed_team_id 확인
→ 기존 팀 workspace 삭제
→ 전역 workspace 삭제
→ 재색인이면 최신 업무 DB 자료로 신규 색인
```

- 현재 업무 행이 팀을 옮겼거나 영구 삭제되어도 소유권 값으로 옛 팀을 찾는다.
- 팀 삭제 실패 시 전역 문서 상태와 소유권을 보존하여 재시도를 지원한다.
- 전역 문서 상태가 존재하는데 소유권이 없거나 잘못됐다면 현재 팀으로 추측하여 삭제하지 않고 명시적으로 실패한다. 구버전 snapshot fallback은 만들지 않는다.
- 이미 없는 문서의 반복 삭제는 기존 native/API의 멱등 처리 범위를 유지한다.
- 관리자가 상태 레코드를 직접 없애 소유권이 유실된 비정상 데이터는 자동 추측하지 않는다. 개발 환경에서는 정확한 색인 namespace를 지정해 정리·전체 재색인한다.
- 삭제 후 재색인 실패 시 이전 정상 색인이 자동 복원되지는 않는다. 기존 구조의 한계로 남기며 성공으로 보고하지 않는다.

## 6. 검색 후 최신 원본 조회

```text
기존 허용 팀 범위 검증
→ 허용 팀 workspace만 검색
→ 후보 업무일지 ID 추출
→ 해당 팀 doc_status 처리 상태 확인
→ 기존 source row reader로 업무 DB 자료 일괄 조회
→ 현재 팀·삭제 여부·허용 범위 검증
→ 선행 업무일지도 같은 기준으로 재조회·필터
→ 최신 본문·확정 관계로 문맥과 참조 구성
→ 기존 답변 모델/SSE 경로 사용
```

### 6.1 재사용과 권한 경계

- `WorklogLightSourceRowReader`·`LightWorklogSourceStore`를 재사용한다. 조회용 별도 업무 DB 접근 계층이나 API 호출을 추가하지 않는다.
- 현재 store의 업무일지 soft delete·팀 삭제 필터를 유지한다.
- 요청의 기존 검증된 `allowed_team_ids`와 원본의 현재 팀 ID를 다시 대조한다. 검색 후보라는 이유만으로 권한을 인정하지 않는다.
- 선행 업무일지도 실제 원본을 재조회해 현재 접근 가능한 경우에만 제목·내용·관계를 문맥에 포함한다. 현재 store가 반환한 predecessor 제목을 검증 없이 복사하지 않는다.
- 일반 응답과 SSE가 같은 문맥 구성 경로를 사용한다.
- 기존 참조 형식, 후보·선행 문서 수 제한, 관계 문맥 길이 제한을 유지한다.
- 전체 scope에 대한 전역 검색 fallback은 추가하지 않는다.

근거: 앱 `service/worklog_source_row_reader.py:10–17`, `store/worklog_source_store.py`, `service/worklog_query_service.py:174–247`.

### 6.2 누락·오류·최신성 정책

- 원본에 없거나 삭제됐거나 현재 허용 팀 밖인 후보는 제외한다.
- 해당 팀 색인에 문서 상태가 없거나 처리 완료 전인 후보는 답변 근거에서 제외한다. 잘못된 상태 레코드 형식 등 데이터 오류는 명시적으로 실패 처리한다.
- 모든 후보가 제외되면 기존 검색 결과 없음 경로로 처리한다.
- 업무 DB 조회 자체가 실패하면 기존 오류 응답 경로를 사용한다. 오래된 그래프·벡터 본문만으로 답변하는 fallback은 만들지 않는다.
- snapshot 제거 후에도 native `processed` 확인을 앱 전체 색인 완료 또는 트랜잭션 보장으로 해석하지 않는다.
- 검색 색인은 이전 내용이고 답변 본문은 최신 내용일 수 있다. 이 시점 차이는 사용자가 수용한 범위다.
- 여기서 최신 원본은 조회 시점에 업무 DB가 반환한 자료를 뜻한다. 여러 SQL·여러 저장소가 요청 전체에서 같은 시점을 본다는 보장은 추가하지 않는다.
- 기존 답변 캐시의 접근 범위는 확대하지 않는다. DB 재검증을 우회하는 새 캐시 경로를 만들지 않는다.

## 7. 전환·재색인과 복구 경계

배포 전 개발 환경이며 무중단 전환을 하지 않는다. 이번 설계 승인 자체가 임의 DB·collection·volume 삭제 권한을 뜻하지는 않는다.

1. 업무 DB와 별도의 `axwms_lightrag` DB 및 권한을 준비한다. LightRAG가 기존 업무 DB에 테이블을 생성하지 않는지 확인한다.
2. 한 번의 전환을 위한 새 base workspace `axwms_pg_v1`을 사용한다. 기존 팀 workspace 생성 규칙은 유지하여 새 base에서 파생한다.
3. PG·Neo4j·Qdrant 모두 같은 새 workspace 계열로 초기화되는지 확인한다. 기존 JSON과 옛 그래프·벡터 색인을 새 PG 상태와 섞지 않는다.
4. 색인 요청을 실행하는 주체를 하나로 제한하고, 검증된 전체 대상 ID로 기존 색인 경로를 실행한다.
5. 기존 캐시 버전 증가 기능으로 전환 전 답변 캐시를 무효화하고 대표 조회·삭제·재색인을 검증한다. Redis 전체 초기화는 하지 않는다.
6. 실패 항목은 기록하고 해당 workspace의 정리·재색인을 재시도한다. 실패한 전체 작업을 완료로 표시하지 않는다.

새 base workspace는 일회성 재구축 대상 분리를 위한 것이며, 읽기 버전 게시 시스템을 새로 만드는 것이 아니다. 옛 JSON·그래프·벡터 데이터의 실제 삭제는 정확한 대상 범위를 확인한 별도 실행 단계로 남긴다. 업무 DB나 공유 Neo4j/Qdrant 전체를 초기화하지 않는다.

## 8. 변경 책임 범위

| 영역 | 필요한 변경 |
|---|---|
| `ai/app/light/v4/service/lightrag_adapter.py` | 기본 PG 저장소 지정, PG 설정 전달, 초기화 전후 workspace 검증, snapshot/marker 제거, 소유권 metadata 처리 |
| `ai/app/light/v4/service/worklog_index_service.py` | snapshot 저장 대신 팀 소유권 저장, 기존 삭제 순서·실패 보존 유지 |
| `ai/app/light/v4/service/worklog_team_scope.py` | snapshot·marker 경로 제거, 팀별 검색·상태 검증·격리 유지 |
| `ai/app/light/v4/service/worklog_query_service.py` | 기존 source reader로 최신 원본 조회, 현재 팀·삭제·선행 관계 필터 |
| `ai/app/light/v4/service/worklog_source_row_reader.py` 및 `store/worklog_source_store.py` | 기존 조회 재사용. 이 설계에 필요한 경계 수정만 허용 |
| `ai/app/light/v4/store/worklog_custom_kg_tracking.py` | 기본 cache/chunk/추적 구조 유지. metadata 보존과 PG 동작 검증 |
| `ai/app/config/settings.py`, `ai/.env.example` | 업무 DB와 LightRAG DB 연결 분리 및 비밀값 없는 예시 |
| 관련 AI 테스트·실행 문서 | snapshot/marker 의존 기대값 교체, PG·권한·실패 회귀 검증 |
| `infra` 설정이 필요한 경우 | 기존 서버에 전용 DB와 연결을 공급하는 범위만 반영. 새 PostgreSQL 서버 추가나 배포 구조 재설계는 제외 |

`v3` 보존 코드는 변경하지 않는다. API·웹 계약을 바꾸지 않는다. 신규 의존성은 기본 PG backend에 실제로 필요한 경우에만 식별하며, 이미 설치된 패키지를 우선 재사용한다.

## 9. 검증 계약

### 단위·회귀 테스트

- 모든 adapter가 기본 PG KV·문서 상태 클래스를 사용하며 Qdrant·Neo4j는 유지된다.
- 업무 DB DSN과 LightRAG DB 설정이 분리되고, 필수 설정 누락·잘못된 workspace override가 명확히 거절된다.
- 초기화 중 workspace가 덮어써지면 adapter를 공개하지 않고 자원을 정리한다.
- `worklog_source`, 형식 버전, 팀 marker를 저장·조회하지 않는다.
- native metadata와 상태·청크 필드를 보존하며 `indexed_team_id`를 저장·회수한다.
- 팀 쓰기 전 소유권 저장, 팀 삭제 실패 후 소유권 보존을 확인한다.
- 팀 이동 후 옛 팀 삭제, 영구 삭제 후 정리, 반복 삭제·재색인·부분 실패를 검증한다.
- 최신 본문·작성자·태그·팀·선행 관계가 답변 문맥에 반영된다.
- 삭제·권한 밖·미완료 문서와 접근 불가 predecessor가 답변·참조에 포함되지 않는다.
- 업무 DB 장애 시 그래프 본문 fallback 없이 실패하며, 일반 응답/SSE의 경계가 같다.
- Custom KG 공유 노드 기여분, confirmed 캐시, 출처 청크, 추적 목록 및 캐시 검증 로직을 유지한다.

### 격리된 외부 저장소 통합 테스트

- 전용 테스트 PG DB·독립 workspace·격리된 Neo4j/Qdrant 대상으로만 실행한다.
- PG 초기화·필수 의존성·스키마 생성·연결 종료를 확인한다.
- cache/chunk/status metadata를 실제 저장·재조회하고 프로세스 재시작 후에도 확인한다.
- 팀 두 개와 공유 노드를 가진 문서들을 색인하여 팀 격리, 한 문서 삭제 후 남은 노드·관계, 팀 이동 및 원본 영구 삭제 후 정리를 확인한다.
- 전역·팀에 동일 문서 ID가 있어도 `workspace`로 구분되는지 검증한다.
- 이전 JSON과 옛 그래프·벡터 데이터가 새 요청에 섞이지 않는지 확인한다.
- 검증용 초기화가 업무 DB와 타 workspace를 건드리지 않는지 확인한다.

기존 관련 테스트 127개 통과는 이전 읽기 전용 분석 시점의 증거다. 이 명세의 구현 또는 실제 PG 통합 검증이 완료됐다는 뜻이 아니다. 처리량·동시 요청의 일관성 보장은 별도 후속 검증 대상이다.

## 10. 남는 한계와 다음 단계

- 재색인의 선삭제 구조, 저장소 간 부분 반영, 같은 workspace 수정 대기는 남는다.
- `indexed_team_id`는 전체 snapshot을 대체하는 소유권 최소 정보이지 transaction·게시 marker가 아니다.
- 기본 PG 저장소는 namespace별 정해진 매핑을 사용하며 임의의 `full_docs` 필드를 자동 보존하지 않는다.
- source 조회와 LightRAG PG 쓰기는 서로 다른 DB 연결이다. 이번에 원본 수정·색인을 단일 transaction으로 묶지 않는다.
- 별도 DB여도 서버 부하·장애는 공유하며 실제 성능은 측정하지 않았다.

문서 자체 검토와 사용자 승인 후 [구현 계획](../plans/2026-10-21-lightrag-postgres-storage.md)에 따라 현재 세션에서 직접 구현했다. 최종 독립 리뷰의 정상 PG 누락 응답·삭제 오류 전달 지적을 회귀 테스트로 보완했다. 실제 DB 변경·재색인·커밋은 하지 않았다.
