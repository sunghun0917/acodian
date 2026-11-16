# LightRAG 로컬 KV → PostgreSQL 전환 조사

## 문서 상태와 조사 범위

- 조사 기준일: **2026-10-19**
- 프로젝트 사용 버전: **`lightrag-hku==1.4.10`**
- 조사 시점 공식 최신 배포판: **1.5.7**, 2026-09-02 배포
- 문서 성격: **조사 보고서 및 설계 입력 자료**. 구현 계획이나 확정된 설계가 아니다.
- 조사 대상: 공식 문서, 버전별 소스, 이전 도구, 릴리즈, 관련 이슈와 현재 프로젝트 코드.
- 인터넷 전체를 빠짐없이 조사했다는 의미의 전수 조사는 보장하지 않는다. 전환 판단에 필요한 주요 지원 범위와 위험을 중심으로 조사했다.
- 실제 DB 연결·데이터 이전·성능·복구 테스트는 수행하지 않았다.

### 근거 구분

- **공식 확인:** 공식 문서·업스트림 소스로 확인한 동작.
- **프로젝트 확인:** 현재 저장소의 코드·설정에서 확인한 사용 방식.
- **설계 추천:** 확인한 제약으로부터 도출한 제안이며, 구현 승인을 뜻하지 않는다.
- **미검증:** 실제 환경에서 재현·성능·복구 검증이 필요한 사항.

현재 버전의 동작과 최신 버전의 변경 사항을 분리했다. 최신 `main` 문서에 있는 기능이 설치 버전에도 있다고 가정하지 않는다. [공식 최신 릴리즈](https://github.com/HKUDS/LightRAG/releases/tag/v1.5.7), [PyPI](https://pypi.org/project/lightrag-hku/)

## 1. 핵심 결론

**로컬 KV를 PostgreSQL로 전환하는 방향은 타당하다. 다만 설정만 바꾸면 기존 데이터가 보존되고 색인 전체에 트랜잭션이 생기는 방식은 아니다.**

주요 발견은 다음 다섯 가지다.

1. 현재 사용하는 **LightRAG 1.4.10도 PostgreSQL KV를 지원한다.**
2. 기본 PG 구현은 프로젝트가 추가한 **업무일지 snapshot·팀 marker를 그대로 저장하지 못한다.**
3. 공식 마이그레이션 도구는 **전체 KV가 아니라 일부 LLM 캐시만 이전한다.**
4. 프로젝트의 **`confirmed:extract:*` 캐시는 공식 이전 도구에서 누락된다.**
5. KV를 PG로 옮겨도 **Neo4j·Qdrant를 포함한 색인 전체 rollback은 보장되지 않는다.**

따라서 우선 작업은 DB 연결보다 **LightRAG 기본 데이터와 앱 전용 데이터의 저장 계약을 구분하는 것**이다.

## 2. 공식 외부 저장소 지원 범위

### 2.1 현재 버전에서 지원하는 구현

| 저장소 | KV 구현 | 문서 처리 상태 구현 |
|---|---|---|
| 로컬 JSON | `JsonKVStorage` | `JsonDocStatusStorage` |
| PostgreSQL | `PGKVStorage` | `PGDocStatusStorage` |
| MongoDB | `MongoKVStorage` | `MongoDocStatusStorage` |
| Redis | `RedisKVStorage` | `RedisDocStatusStorage` |

PostgreSQL 연결부터 모두 새로 구현할 필요는 없다. README 일부 목록과 달리 **Redis 문서 상태 저장소도 실제 코드에는 존재한다.** [1.4.10 저장소 등록 코드](https://github.com/HKUDS/LightRAG/blob/v1.4.10/lightrag/kg/__init__.py)

### 2.2 권장 전환 범위

```text
현재                          전환 후
KV JSON 7종          ─────→   PostgreSQL
문서 상태 JSON       ─────→   PostgreSQL

Qdrant 벡터                  그대로 유지
Neo4j 그래프                 그대로 유지
```

**설계 추천:** 이번 작업을 KV와 문서 상태 저장소 전환에 한정한다. 벡터·그래프까지 PostgreSQL로 옮길 필요는 없다.

## 3. 로컬 JSON 8종의 PG 테이블 매핑

기본 PG 구현은 JSON 파일 전체를 JSONB 하나로 넣는 방식이 아니라, 용도별 테이블과 필드로 변환한다.

| 로컬 데이터 종류 | PG 테이블 | 주요 역할 |
|---|---|---|
| `full_docs` | `LIGHTRAG_DOC_FULL` | 문서 본문 |
| `text_chunks` | `LIGHTRAG_DOC_CHUNKS` | 청크 본문·원문 연결·캐시 연결 |
| `full_entities` | `LIGHTRAG_FULL_ENTITIES` | 문서별 엔티티 목록 |
| `full_relations` | `LIGHTRAG_FULL_RELATIONS` | 문서별 관계 목록 |
| `entity_chunks` | `LIGHTRAG_ENTITY_CHUNKS` | 엔티티에 기여한 청크 추적 |
| `relation_chunks` | `LIGHTRAG_RELATION_CHUNKS` | 관계에 기여한 청크 추적 |
| `llm_response_cache` | `LIGHTRAG_LLM_CACHE` | 추출·요약·질의 등의 캐시 |
| `doc_status` | `LIGHTRAG_DOC_STATUS` | 처리 상태·청크 목록·처리 metadata |

팀마다 물리 테이블을 새로 만드는 것이 아니라, 기본적으로 같은 테이블의 **`workspace` 값으로 데이터를 구분한다.** [1.4.10 PostgreSQL 구현](https://github.com/HKUDS/LightRAG/blob/v1.4.10/lightrag/kg/postgres_impl.py)

여기서 8종은 workspace별 데이터 종류 수다. 프로젝트 전체에 JSON 파일이 여덟 개만 있다는 뜻은 아니다.

테이블 분리 문제는 다음 두 범주로 나뉜다.

- **LightRAG 기본 데이터:** 공식 구현이 용도별 테이블을 이미 정한다.
- **앱이 추가한 snapshot·팀 소유권·marker:** 보존 위치와 조회 계약을 별도로 결정해야 한다.

## 4. 프로젝트에서 먼저 해결해야 할 호환성 문제

### 4.1 업무일지 snapshot 보존

**프로젝트 확인:** 앱은 `full_docs`에 본문 외에도 다음 정보를 추가한다.

```text
worklog_source_schema_version
worklog_source
  └ 업무일지 ID, 작성자, 팀, 제목, 내용, 태그, 선행 업무 등
```

확인 위치: `ai/app/light/v4/service/lightrag_adapter.py`의 `save_source_rows()`와 snapshot 조회 경로.

기본 PG `full_docs` 저장 경로는 지정 필드만 매핑한다. **`worklog_source` 같은 임의 필드를 자동 저장·복원하지 않는다.** 테이블에 `meta` 컬럼이 있다는 사실만으로 해결되지 않는다. [PG full_docs 저장 코드](https://github.com/HKUDS/LightRAG/blob/v1.4.10/lightrag/kg/postgres_impl.py)

현재 상태로 전환하면 본문은 남아도 **검색 응답 구성과 팀 소유권 판단에 필요한 앱 데이터가 누락될 수 있다.**

### 4.2 팀 workspace marker 호환성

**프로젝트 확인:** 현재 `worklog-team-scope-schema` marker는 다음 형태다.

```json
{
  "schema_version": 1,
  "team_id": 123
}
```

확인 위치: `ai/app/light/v4/service/lightrag_adapter.py`의 marker 저장·검증 경로.

기본 PG `full_docs` 저장 코드는 `content`를 필수로 읽는다. 따라서 **본문 없는 현재 marker는 그대로 저장할 수 없다.**

`content`만 추가하는 것도 충분하지 않다. `schema_version`, `team_id`의 보존·조회 경로까지 해결해야 한다.

이 marker는 workspace 데이터 형식과 팀을 확인하는 용도다. **MVCC 버전이나 색인 전체의 원자적 게시 완료 표시가 아니다.**

### 4.3 `confirmed:extract:*` 캐시 보존

**프로젝트 확인:** 앱은 아래 키를 만들어 청크의 `llm_cache_list`와 연결한다.

```text
confirmed:extract:{chunk_id}
```

확인 위치:

- `ai/app/light/v4/store/worklog_custom_kg_tracking.py`
- `ai/app/light/v4/store/worklog_extraction_cache.py`

이는 단순히 없어져도 되는 질의 캐시와 성격이 다르다. **확정된 custom KG의 추출 결과와 재구성 경로에 연결되므로 이전 범위에 포함해야 한다.**

### 4.4 데이터 배치에 대한 설계 추천

**LightRAG 기본 테이블은 공식 구현을 사용하고, 앱 전용 snapshot·팀 marker는 별도 앱 테이블로 분리하는 안**이 유력하다.

이는 공식 요구사항이나 확정 설계가 아니다. 커스텀 필드 손실과 라이브러리 결합을 줄이기 위한 제안이며, 실제 테이블 구조·트랜잭션 경계·기존 호출부 변경 범위는 후속 설계가 필요하다.

## 5. 공식 마이그레이션 도구의 실제 범위

### 5.1 전체 KV 이전은 공식 캐시 도구의 역할이 아니다

1.4.10 공식 문서는 저장소 구현 간 일반적인 데이터 이전을 지원하지 않는다고 설명한다. 대신 LLM 캐시를 이전한 다음 문서를 다시 입력하는 경로를 안내한다. [공식 저장소 변경 안내](https://github.com/HKUDS/LightRAG/blob/v1.4.10/lightrag/api/README.md)

공식 도구:

```bash
python -m lightrag.tools.migrate_llm_cache
```

| 항목 | 공식 캐시 도구 |
|---|---|
| `full_docs` | 이전하지 않음 |
| `text_chunks` | 이전하지 않음 |
| 엔티티·관계 추적 4종 | 이전하지 않음 |
| `doc_status` | 이전하지 않음 |
| 추출·요약 캐시 일부 | 이전 |
| 전체 검색 캐시 | 이전하지 않음 |

같은 키가 대상에 있으면 덮어쓸 수 있다. 체크포인트 기반 이어하기 도구로 간주해서도 안 된다. [공식 캐시 이전 가이드](https://github.com/HKUDS/LightRAG/blob/v1.4.10/lightrag/tools/README_MIGRATE_LLM_CACHE.md)

### 5.2 프로젝트 캐시가 누락되는 이유

1.4.10 JSON 입력 필터는 다음 prefix만 선택한다.

```text
default:extract:*
default:summary:*
```

따라서 **`confirmed:extract:*`는 도구가 성공해도 이전되지 않는다.** 조사한 최신 코드에서도 프로젝트 prefix는 포함되지 않는다. [1.4.10 실제 필터](https://github.com/HKUDS/LightRAG/blob/v1.4.10/lightrag/tools/migrate_llm_cache.py), [조사 시점 최신 코드의 필터](https://github.com/HKUDS/LightRAG/blob/aaebfe1bb8d55deb6a4304d357a3bf4c92fcd40a/lightrag/tools/migrate_llm_cache.py)

**8종 전체와 프로젝트 커스텀 데이터를 자동 보존하는 공식 이전 도구는 조사 범위에서 찾지 못했다.**

## 6. 가능한 이전 방식

### A. 원본으로 재구축

```text
보존할 캐시 이전
→ 새로운 저장소 구성
→ 프로젝트 색인 로직으로 원본 재처리
→ 결과 검증
→ 서비스 전환
```

적합한 조건:

- 색인을 원본 업무일지로부터 재생성할 수 있다.
- 기존 색인 상태를 그대로 복사할 필요가 적다.
- 재색인 시간과 비용을 감당할 수 있다.

프로젝트는 팀 workspace와 custom KG 처리가 있으므로 **공식 파일 업로드만으로 재구축이 충분하다고 볼 수 없다.**

또한 기존 Neo4j·Qdrant는 그대로 두고 빈 PG KV에 무작정 재색인하는 방식의 안전성을 가정하면 안 된다. 세 저장소의 ID·기여 관계가 맞도록 재구축 범위를 정해야 한다.

### B. 기존 데이터를 변환하여 복사

```text
일관된 시점의 데이터 확보
→ 8종 데이터와 앱 metadata 변환
→ 대상 PG에 적재
→ ID·참조 관계 검증
→ 서비스 전환
```

적합한 조건:

- 현재 청크 ID·추적 정보·캐시를 유지해야 한다.
- 불필요한 LLM 재호출을 줄여야 한다.
- 기존 상태를 최대한 보존해야 한다.

별도 importer와 검증이 필요하다. **행 수가 같다는 것만으로 성공 판정하면 안 된다.**

어느 방식을 선택할지는 재생성 가능한 개발 데이터인지, 반드시 보존해야 하는 운영 데이터인지에 따라 달라진다.

## 7. PostgreSQL 전환의 보장 범위

| 문제 | 전환 효과 |
|---|---|
| 로컬 JSON 파일 영속화 관리 | DB로 책임 이전 |
| 프로세스 로컬 파일 의존 | 감소 |
| SQL 트랜잭션·조건부 갱신 구현 기반 | 확보 |
| 백업·복구 체계 | DB 도구 활용 가능 |
| 색인 전체 rollback | 자동 해결 안 됨 |
| 검색이 색인 도중 상태를 절대 보지 않음 | 자동 보장 안 됨 |
| 수정 실패 시 이전 색인 유지 | 별도 설계 필요 |
| 삭제 즉시 검색 제외 | 별도 설계 필요 |
| 오래된 요청이 최신 결과를 덮어씀 | 별도 제어 필요 |
| 여러 서버의 writer 동시 실행 | 별도 조정 필요 |

### 7.1 PG 도입과 pipeline transaction은 다르다

LightRAG 공식 개발 문서도 graph·KV·vector·doc-status 사이에 공통 트랜잭션이 없으며 중간 상태가 발생한다고 명시한다. [공식 일관성 설명](https://github.com/HKUDS/LightRAG/blob/aaebfe1bb8d55deb6a4304d357a3bf4c92fcd40a/AGENTS.md)

```text
PG 청크 저장 성공
→ Qdrant 저장 성공
→ Neo4j 갱신 실패
```

이때 PG가 앞선 작업을 알아서 취소하지 않는다. **동일 PG로 옮긴 KV와 doc-status도 명시적 트랜잭션 연결 없이 자동으로 pipeline 단위의 원자성을 얻는 것은 아니다.** [asyncpg transaction 사용법](https://magicstack.github.io/asyncpg/current/usage.html#transactions)

### 7.2 MVCC가 검색 전체 snapshot을 자동 제공하지 않는다

PostgreSQL 기본 `READ COMMITTED`는 일반적으로 문장마다 snapshot이 달라질 수 있다. PG를 여러 번 읽고 Neo4j·Qdrant도 조회하는 검색 전체가 하나의 snapshot이 되는 것은 아니다. [PostgreSQL 격리 수준](https://www.postgresql.org/docs/18/transaction-iso.html)

**PG 전환은 정합성을 구현할 기반이지 완성된 정합성 정책 자체가 아니다.**

## 8. workspace·락·연결 설정의 주의점

### 8.1 공통 `POSTGRES_WORKSPACE` 설정

공식 구현은 저장소별 `POSTGRES_WORKSPACE`를 인스턴스 workspace보다 우선한다. 팀별 인스턴스를 만들면서 이 값을 하나로 고정하면 **다른 팀 데이터가 같은 PG workspace로 모일 수 있다.** 공식 예제도 호환성 목적 외에는 설정하지 않도록 안내한다. [공식 환경변수 예제](https://github.com/HKUDS/LightRAG/blob/v1.4.10/env.example)

설정 문자열만이 아니라 **초기화 후 실제 적용된 workspace**를 확인해야 한다.

### 8.2 asyncio 락은 분산락으로 바뀌지 않는다

LightRAG의 프로세스 공유 조정과 앱의 `asyncio` 락을 여러 서버·pod 사이의 조정 수단으로 확대 해석하면 안 된다. 현재 공식 배포 문서도 worker 간 조정 범위를 단일 인스턴스 안으로 제한한다. [공식 배포 제약](https://github.com/HKUDS/LightRAG/blob/aaebfe1bb8d55deb6a4304d357a3bf4c92fcd40a/k8s-deploy/K8S-README.md)

**설계 추천:** PG 전환 직후 기존 mutation 락을 제거하지 않는다. 여러 writer 허용 여부와 조정 방식은 별도 검증한다.

### 8.3 현재 앱에서는 `.env` 변경만으로 전환되지 않는다

**프로젝트 확인:** `ai/app/light/v4/service/lightrag_adapter.py`의 `_build_lightrag()`는 `vector_storage`, `graph_storage`를 전달하지만 `kv_storage`, `doc_status_storage`는 전달하지 않는다.

따라서 앱 설정과 생성부 연결이 필요하다. 아래는 연결해야 할 선택값을 나타내는 예시이며, 완전한 실행 코드는 아니다.

```python
kv_storage="PGKVStorage"
doc_status_storage="PGDocStatusStorage"
```

Qdrant를 계속 사용한다면 다음 설정으로 DB의 pgvector 기능을 끌 수 있다.

```env
POSTGRES_ENABLE_VECTOR=false
```

KV·상태 저장만을 위해 pgvector나 AGE를 DB에 설치할 필요는 없다. Python 패키지 의존성 유무와 DB 확장 설치 필요성은 별개다. [1.4.10 공식 설정](https://github.com/HKUDS/LightRAG/blob/v1.4.10/env.example)

## 9. 현재 버전 이후의 관련 수정

| 버전 | 관련 변경 | 전환 시 의미 |
|---|---|---|
| 1.4.11 | 실패 문서의 청크 metadata 보존, PG KV 일괄 upsert 개선 | 실패 후 정리·적재 성능 검증 필요 |
| 1.4.12 | 문서 삭제를 재시도에 안전하도록 수정 | outbox 재시도와 관련 |
| 1.4.13 | PG doc-status pool·pagination 수정 | 상태별 전체 조회 검증 필요 |
| 1.5.3 | Qdrant/PG clear 후 legacy 데이터 재등장 수정 | 초기화·재시작 검증 필요 |
| 1.5.5 | 락 취소·worker 복구, 캐시 관련 수정 | 취소·재시작·캐시 재사용 검증 필요 |

각 내용은 공식 릴리즈에 포함된 변경이다. 이번 조사에서 해당 버그를 직접 재현하지는 않았다. [1.4.11](https://github.com/HKUDS/LightRAG/releases/tag/v1.4.11), [1.4.12](https://github.com/HKUDS/LightRAG/releases/tag/v1.4.12), [1.4.13](https://github.com/HKUDS/LightRAG/releases/tag/v1.4.13), [1.5.3](https://github.com/HKUDS/LightRAG/releases/tag/v1.5.3), [1.5.5](https://github.com/HKUDS/LightRAG/releases/tag/v1.5.5)

**설계 추천:** 현재 버전 고정도 무위험은 아니다. 다만 라이브러리 업그레이드와 저장소 변경은 원인을 구분할 수 있도록 별도 검증 축으로 관리한다.

## 10. 전환 검증 체크리스트

다음은 조사 결과에 근거한 권장 항목이다. 아직 실행·통과한 항목이 아니다.

### 10.1 데이터 보존

- [ ] workspace별 8종 레코드 수·키 비교
- [ ] 업무일지 snapshot과 색인 당시 팀 정보 보존
- [ ] 청크 ID와 `full_doc_id` 연결 확인
- [ ] 엔티티·관계의 청크 기여 정보 확인
- [ ] `llm_cache_list`와 `confirmed:*` 캐시 연결 확인
- [ ] 문서 상태·metadata·시간값의 의미상 동등성 확인
- [ ] JSONB 변환 가능 여부 사전 검사

JSONB에는 NUL 문자, 잘못된 Unicode, 지원 범위를 벗어난 숫자 등의 제약이 있다. [PostgreSQL JSON 제약](https://www.postgresql.org/docs/18/datatype-json.html)

### 10.2 실패·동시성

- [ ] 중간 단계 실패 후 같은 요청 재시도
- [ ] 동일 outbox 이벤트 중복 전달
- [ ] 같은 업무일지 연속 수정
- [ ] 색인 중 삭제와 팀 이동
- [ ] 공유 엔티티를 가진 문서 중 하나만 삭제
- [ ] 프로세스 강제 종료 후 재시작
- [ ] 여러 writer를 사용할 경우 서버 간 충돌
- [ ] doc-status 상태별 조회와 페이지 경계
- [ ] clear 후 재시작 시 과거 데이터 재등장 여부

### 10.3 서비스 정책

앞선 설계 논의에서 합의한 목표이며, 현재 구현이 이미 보장한다는 뜻은 아니다.

- [ ] 신규 색인 완료 전에는 검색에 노출되지 않음
- [ ] 재색인 실패 시 이전 색인을 계속 제공
- [ ] 삭제 성공 후 시작한 검색에서는 제외
- [ ] 삭제 전에 근거를 확보한 진행 중 답변은 마무리 허용

연속 수정 중 중간 버전을 게시할지, 최신 버전만 게시할지는 후속 결정이 필요하다.

### 10.4 전환·복구

- [ ] scheduler·수동 배치·기존 서버 등 모든 writer를 중지하거나 drain
- [ ] JSON·Neo4j·Qdrant를 복구 가능한 일관된 상태로 확보
- [ ] 대상 데이터 검증 후 서비스 쓰기 전환
- [ ] 전환 후 새 쓰기 발생 시 재처리·복구 방안 마련
- [ ] 실제 백업 복원 테스트
- [ ] 대상 PG 버전·TLS·권한·연결 수·timeout 검증
- [ ] 검색 지연·색인 시간·pool 대기·락 대기 측정

PG 백업 자체와 여러 저장소의 일관된 복구는 별개다. 전환 후 새 쓰기가 생겼다면 옛 JSON로 단순 복귀할 경우 최신 데이터가 누락될 수 있다. [PostgreSQL 백업·복구 문서](https://www.postgresql.org/docs/18/backup.html)

## 11. 권장 후속 순서

아래는 후속 설계를 위한 추천이며 실행 승인된 계획이 아니다.

1. **앱 전용 데이터 계약 정리**
   - snapshot·팀 소유권·marker·`confirmed` 캐시의 보존 위치와 조회 경로를 정한다.
2. **목표 버전과 이전 방식 결정**
   - 재구축인지 기존 데이터 보존 이전인지 결정하고, 업그레이드 영향을 분리 검증한다.
3. **KV·doc-status만 PG로 전환**
   - Neo4j·Qdrant와 기존 mutation 락은 우선 유지한다.
4. **실패·재시도·삭제·팀 격리 검증**
   - 단순 등록 성공이 아니라 부분 실패와 재시작까지 확인한다.
5. **검색 게시 정책과 병렬성 개선**
   - 이전 색인 유지, 최신 요청 판별, 삭제 가시성, 락 범위 축소를 별도 설계한다.

**첫 설계 주제는 현재 JSON에 섞인 LightRAG 기본 데이터와 앱 전용 데이터의 저장 계약 분리다.**

## 12. 미검증 사항과 핵심 위험

- 실제 DB 연결·이전·성능·복구 테스트는 수행하지 않았다.
- 대상 PostgreSQL 버전, 운영 데이터 규모, 중단 허용 시간, 실제 동시 writer 배치는 확정하지 않았다.
- JSON→PG 전환의 성능 개선 폭은 측정 전에는 보장할 수 없다.
- 가장 큰 위험은 **커스텀 데이터의 조용한 누락**과 **PG 전환을 색인 전체 트랜잭션 보장으로 오해하는 것**이다.
