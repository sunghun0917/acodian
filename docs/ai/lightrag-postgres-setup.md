# LightRAG PostgreSQL 전환·재색인

## 범위

- LightRAG 1.4.10의 기본 `PGKVStorage`·`PGDocStatusStorage`를 사용한다.
- 기존 PostgreSQL 서버에 `axwms_lightrag` DB를 별도로 둔다. 업무 DB는 옮기지 않는다.
- JSON 데이터 이관·구버전 호환·무중단 전환은 하지 않는다. 새 workspace에 전체 재색인한다.
- 기존 전역·팀 색인, Neo4j·Qdrant와 mutation 락은 유지한다. PG 전환만으로 저장소 간 일관성이나 수정 병렬성이 보장되지는 않는다.

이 문서는 저장소 전환 결정·실행 절차·실패 후 재시도의 기준이다. 팀별 검색과 권한 문맥의 구현 설명은 [팀별 검색](rag-team-prefilter.md)에 둔다.

## 1. DB와 계정 준비

아래는 관리자 실행 절차이며 앱 기동 시 자동 실행하지 않는다. 대상 서버·DB를 확인한 뒤 수행한다. 기존 role/DB가 있으면 owner와 용도를 확인하고 그대로 사용하며, 임의 drop·비밀번호 변경은 하지 않는다.

```sql
-- 관리자 psql 접속에서 대상 확인
SELECT current_database(), inet_server_addr(), inet_server_port();
SELECT rolname FROM pg_roles WHERE rolname = 'axwms_lightrag';
SELECT datname, pg_get_userbyid(datdba) AS owner
FROM pg_database WHERE datname = 'axwms_lightrag';

-- 해당 role/DB가 없는 경우에만 각각 실행
CREATE ROLE axwms_lightrag LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE;
```

psql의 `\password axwms_lightrag`로 비밀번호를 비표시 입력한다. 실제 비밀번호를 SQL 파일·CLI 인자·셸 기록에 넣지 않는다.

```sql
CREATE DATABASE axwms_lightrag OWNER axwms_lightrag;
REVOKE CONNECT ON DATABASE axwms_lightrag FROM PUBLIC;
GRANT CONNECT ON DATABASE axwms_lightrag TO axwms_lightrag;
```

새 계정에는 기존 업무 테이블의 변경 권한을 주지 않는다. 업무 원본 조회는 기존 SQLAlchemy 연결을 그대로 사용한다. LightRAG는 전용 계정으로 전용 DB의 기본 테이블을 생성한다. 업무 DB용 Alembic에 LightRAG 테이블을 중복 정의하지 않는다.

`POSTGRES_DB`나 새 DSN을 설정하는 것만으로 기존 PostgreSQL volume 안에 DB가 생기지는 않는다. 새 PostgreSQL 서버나 volume은 필요 없다. 로컬 Compose의 PostgreSQL 포트는 `5434`, 컨테이너 내부는 `postgres:5432`다.

## 2. 연결 설정

| 설정 | 대상 |
|---|---|
| 기존 `PGVECTOR_DSN` | 업무 원본 DB. 계정·DB 이름을 유지하고 실행 위치에 맞게 주소 지정 |
| `LIGHTRAG_PG_DSN` | `axwms_lightrag` 전용 DB |
| `LIGHTRAG_WORKSPACE` | 새 base workspace `axwms_pg_v1` |

`ai/.env.example`, `infra/.env.example`의 값은 개발·렌더용 예시일 뿐이다. 실제 `.env`에서 DSN의 비밀번호를 URL 인코딩하여 설정한다. DSN은 기본 PostgreSQL TCP URL을 사용하고 query parameter는 받지 않는다. TLS가 필요한 환경은 native `POSTGRES_SSL_*` 설정을 별도로 공급한다. URI에 TLS query parameter를 넣었다가 무시되는 방식은 허용하지 않는다.

`Settings`가 읽은 DSN을 adapter가 native `POSTGRES_HOST/PORT/USER/PASSWORD/DATABASE`로 전달한다. `POSTGRES_ENABLE_VECTOR=false`이며 벡터는 Qdrant에 남는다. 비어 있는 DSN, 업무 DB와 같은 이름, 잘못된 접속 정보는 설정 오류다.

`POSTGRES_WORKSPACE`와 CWD `config.ini`의 `[postgres].workspace` override는 제거해야 한다. PG가 전역·팀 workspace를 하나로 합치지 못하도록 초기화 전후 실제 namespace를 검증한다. AI와 worker에는 같은 DSN·base workspace를 공급하되, 이번 변경이 여러 worker의 쓰기 조정을 해결한 것은 아니다.

### 로컬 실행

저장소 루트에서 `infra/compose.local.yml`을 사용한다. AI 서버와 Celery worker는 기존 `ai/Dockerfile`로 빌드하며 `ai/.env`를 읽는다. 프로필 없이 저장소·AI·worker·reranker를 함께 실행한다.

| 접속 위치 | PostgreSQL 주소 |
|---|---|
| 로컬 Python·Windows pgAdmin | `127.0.0.1:5434` |
| 같은 Compose의 AI·worker | `postgres:5432` |

AI를 컨테이너로 실행할 때는 `ai/.env`의 **두 DSN 모두** 내부 주소를 사용해야 한다. `localhost`는 PostgreSQL 컨테이너가 아니라 AI 컨테이너 자신이다. 업무 DB 이름·계정·비밀번호는 기존 값을 유지한다. `.env.example`의 비밀번호는 예시이며, `.env`를 수정해도 PostgreSQL 계정 비밀번호가 바뀌지는 않는다.

```powershell
docker compose -f infra/compose.local.yml up -d --build
```

이미 빌드된 이미지에서 `.env`만 변경했다면 AI와 worker를 재생성한다. 단순 `restart`로 새 환경변수가 반영되지는 않는다.

```powershell
docker compose -f infra/compose.local.yml up -d --no-deps --force-recreate ai ai_worker ai_index_worker
```

로컬 Python AI 서버가 실행 중이면 먼저 종료하여 `8200` 포트 충돌을 피한다. API 콜백 기본 주소는 호스트의 `8100` 포트이므로 API도 컨테이너에서 접근 가능한 주소로 실행해야 한다. 컨테이너별 데이터 volume은 유지하고 `down -v`로 초기화하지 않는다. reranker는 첫 실행 시 모델을 내려받을 수 있다.

배포용 `infra/compose.deploy.yml`은 서버 환경파일에서 DSN·workspace를 받아 AI와 worker에 함께 전달한다. 로컬 `ai/.env`와 배포 환경파일을 혼용하지 않는다.

일반 AI Worker `ai_worker`는 `ai_general`만 소비한다. `ai_index_worker`는 `worklog_index`만 소비하며 동시 실행 수는 1이다. 기존 `celery` 큐는 소비하지 않으므로 이전 버전의 대기·재시도 작업이 남아 있다면 별도의 소비자가 필요하다. 롤백 시 구버전 Worker가 `ai_general`을 소비하지 못하므로 해당 큐를 처리할 호환 Worker를 유지하거나 안전하게 처리한 뒤 종료한다. Redis나 큐를 삭제하지 않는다. 직접 색인 실행이나 다른 인스턴스가 동시에 쓰지 않는지도 별도로 확인한다.

일반 AI와 색인은 태그 생성 완료를 기다리지 않고 병렬로 실행한다. 색인이 AI 태그 저장보다 먼저 원본을 읽으면 확정 태그 관계가 다음 재색인까지 누락될 수 있으며, 태그 콜백은 재색인을 요청하지 않는다. 이는 수용된 동작이며 Worker 분리로 기존 경합의 빈도가 달라질 수 있다.

### pgAdmin 조회

Windows pgAdmin에는 Host `127.0.0.1`, Port `5434`, Maintenance database·Username `axwms_lightrag`로 등록한다. Password에는 URL 인코딩하지 않은 원래 계정 비밀번호를 넣는다. `Schemas → public → Tables`에서 기본 테이블을 확인한다.

## 3. 저장·조회 의미

- `worklog_source`, `worklog_source_schema_version`, 팀 schema marker는 저장하지 않는다.
- 전역 `doc_status.metadata.indexed_team_id`만 팀 소유권 최소 정보로 보존한다. 기존 native metadata·청크 목록은 유지한다.
- confirmed 추출 캐시·확정 KG 출처 청크·기본 추적 정보는 재색인으로 생성한다. 삭제·공유 노드 재구성에 필요하므로 제거하지 않는다.
- 검색은 허용 팀 workspace에서 후보와 processed 상태를 확인한 뒤 기존 source reader로 현재 업무 DB 자료를 조회한다.
- 현재 팀·업무/팀 삭제 여부를 확인하고, predecessor도 현재 접근 가능한 원본만 문맥에 사용한다. 업무 DB 장애 시 그래프 본문으로 우회하지 않는다.
- 색인은 이전 내용이고 답변 본문은 최신 내용일 수 있다. 여러 SQL·저장소의 동일 시점 snapshot은 보장하지 않는다.

별도 앱 테이블이나 커스텀 PG 저장소는 추가하지 않는다. 같은 테이블의 `workspace` 열로 전역·팀 데이터를 구분한다. 실제 PostgreSQL 테이블 이름은 아래와 같다.

| 저장 데이터 | 기본 테이블 |
|---|---|
| 문서 본문 | `lightrag_doc_full` |
| 청크 본문·문서/캐시 연결 | `lightrag_doc_chunks` |
| 문서별 엔티티·관계 목록 | `lightrag_full_entities`, `lightrag_full_relations` |
| 엔티티·관계의 출처 청크 추적 | `lightrag_entity_chunks`, `lightrag_relation_chunks` |
| 추출·응답 캐시와 `confirmed:extract:*` | `lightrag_llm_cache` |
| 처리 상태·청크 목록·팀 소유권 metadata | `lightrag_doc_status` |

기본 추적 정보는 LightRAG의 삭제·공유 노드 재구성 구조다. 앱이 추가한 confirmed 캐시·확정 KG 출처 청크도 같은 구조로 저장하므로 JSON 파일을 별도 PG 테이블로 복제할 필요는 없다.

### LightRAG 1.4.10 호환성 보완

기본 PG 클래스·테이블은 유지한다. 다만 이 버전의 문서 상태 일괄 조회는 전부 미존재일 때 `[]`를 반환하므로 이를 정상 누락으로 처리한다.

또한 기본 PG `delete()`는 DB 예외를 로그만 남기고 반환한다. adapter는 해당 인스턴스의 삭제 메서드만 같은 native client·parameterized SQL을 사용하는 함수로 연결하여 예외를 전달하고 잔존 레코드를 재조회한다. 팀 삭제가 실패했는데 전역 소유권을 지우는 것을 막기 위한 버전 고정 보완이며 별도 PG 저장소 클래스나 테이블은 추가하지 않는다. 라이브러리 업그레이드 시 이 훅과 회귀 테스트를 재검토해야 한다. 저장소 간 rollback을 제공하는 것은 아니다.

## 4. 새 workspace 전체 재색인

1. AI 검색·색인 사용을 중지하고 위 DB·계정·설정을 준비한다. 실제 업무 DB는 초기화하지 않는다.
2. base workspace를 `axwms_pg_v1`로 지정한다. 기존 팀 workspace 생성 규칙이 새 base에서 별도 팀 namespace를 파생한다.
3. PG·Neo4j·Qdrant가 모두 새 workspace 계열을 사용하는지 확인한다. 기존 전역/팀 색인을 새 PG 상태와 섞지 않는다.
4. 현재 유효한 전체 업무 ID를 확인하고 각 업무의 현재 색인 요청을 API Outbox 경로로 등록한다. 내부 인증을 유지하고 색인 실행 주체는 하나로 제한한다.
5. 접수 응답과 별도로 업무별 결과 콜백·상태를 기록한다. 실패/timeout을 rollback으로 가정하지 말고 소유권·native 상태를 확인하여 정리·재시도한다.
6. 기존 cache의 `bump_version()`으로 전환 전 응답 cache를 무효화한다. Redis 전체를 비우지 않는다.
7. 대표 검색과 SSE, 팀 이동 후 재색인, 원본 삭제 후 색인 삭제, 공유 사용자·태그 보존을 확인한다.

전체 대상은 업무 DB에서 현재 유효한 ID로 조회한다. DB 이름이나 ID 연속성을 추측하지 않는다.

```sql
SELECT w.worklog_id, w.team_id
FROM tb_worklog w
JOIN tb_team t ON t.team_id = w.team_id
JOIN tb_user u ON u.user_id = w.author_id
WHERE w.is_deleted = false AND t.deleted_at IS NULL
ORDER BY w.team_id, w.worklog_id;
```

현재 `POST /ai/light/worklogs-v4/index`의 요청은 `worklogIds`와 UUID `requestId`를 필요로 하며 내부 토큰으로 인증한다. 응답은 문서별 `worklogId`, `requestId`, `taskId`가 담긴 HTTP `202` 접수 결과다. 색인 성공 결과가 아니므로 Spring의 현재 요청·실행에 대한 완료 콜백과 저장소 상태를 확인한다. Worker의 시작 승인에는 업무별 현재 `light_index_request_id`가 일치해야 하므로 임의 UUID를 만들어 직접 호출하면 작업이 건너뛰어질 수 있다. 일괄 재구축 시에는 먼저 업무별 요청 권위를 등록하는 절차를 별도로 마련해야 하며, 이 문서에는 그 절차가 구현되어 있지 않다. 모델의 최대 배치 크기는 10건이며 실제 설정 제한은 더 작을 수 있다. Redis 전체 삭제는 필요 없다.

### 2026-10-29 격리 E2E 확인

별도 Spring Boot·업무 DB·Redis·일반 AI/색인 Worker와 별도 LightRAG PostgreSQL·Qdrant·Neo4j에서 가상 업무 한 건으로 확인했다. 실제 Gemini 요약과 태그 2건 생성, 전역·팀 색인 완료, 본문 수정 후 양쪽 문서의 재색인, 소프트 삭제 후 전역·팀 문서 및 해당 Outbox 행 정리를 확인했다. 기존 로컬 업무 DB와 색인 저장소는 사용하지 않았고 테스트 자원은 종료했다.

첨부파일 요약, 검색 결과와 생성 태그 각각의 색인 관계는 이 E2E에서 확인하지 않았다. 별도 일반·SSE 검색 통합 테스트는 테스트 격리 설정의 전역 adapter 연결 오류로 실패했으므로 검색 E2E 성공 근거로 취급하지 않는다.

새 전역 workspace에도 기존 안전한 Qdrant 초기화 함수를 사용하여 legacy collection 이관·삭제를 실행하지 않는다. 옛 JSON·그래프·벡터 데이터 삭제는 정확한 대상 namespace를 확인한 별도 작업이다. 공유 collection·Neo4j DB·PostgreSQL volume 전체를 삭제하지 않는다.

팀 색인 전에 전역 소유권 기록을 완료한다. 팀 삭제가 실패하면 전역 소유권을 유지한다. 상태는 존재하지만 소유권이 없거나 손상됐다면 현재 팀으로 추측하지 않고 실패한다. 이 경우 개발용 대상 namespace의 정리·재구축으로 복구한다. 삭제 후 재색인 실패 시 이전 색인은 자동 복원되지 않는다.

### 실패 후 재시도와 보상 한계

- 오류 발생 시 추가 batch 요청을 중단하고 원인을 확인한다. Gemini 크레딧 소진 같은 외부 오류는 자원·설정이 복구되기 전 반복 요청하지 않는다.
- 완료 판정은 현재 업무일지의 전역·해당 팀 상태가 모두 `processed`이고, 전역 `metadata.indexed_team_id`가 현재 대상 팀과 일치하는지 확인한다. 이는 처리 완료 확인이지 그래프·벡터 내용의 전수 정합성 증명은 아니다.
- 이번 초기 구축에서는 원본이 바뀌지 않은 실패 문서를 같은 `/index`로 재요청했다. native 본문 재처리와 확정 KG upsert를 진행했고 완료 상태를 다시 확인했다. 이미 완료된 문서는 재요청하지 않았다.
- 변경된 원본·팀 이동·고아 데이터까지 같은 방식으로 복구된다고 가정하지 않는다. 저장된 소유권과 출처 추적을 확인해 기존 `/reindex`·`/delete` 경로를 판단해야 한다.
- **실패 전 상태로 되돌리는 보상 트랜잭션은 구현하지 않았다.** PG·Neo4j·Qdrant 일부에 기록된 데이터는 실패 후 남을 수 있다.
- 재요청 시 native가 `dup-*` 상태 레코드를 남길 수 있다. `metadata.is_duplicate=true`와 `original_doc_id`를 확인하고 원본 `worklog-*`의 현재 상태를 조회한다. `dup-*`의 `failed`만 보고 실제 업무일지가 아직 실패했다고 해석하지 않는다. 이 기록은 자동 삭제하지 않았다.

원본 업무일지 상태를 확인하는 예시는 다음과 같다. 팀 workspace도 같은 방식으로 확인한다.

```sql
SELECT workspace, id, status, metadata
FROM lightrag_doc_status
WHERE workspace = 'axwms_pg_v1' AND id LIKE 'worklog-%'
ORDER BY id;
```

### 동시성 문제는 별도 범위

PostgreSQL MVCC는 Neo4j·Qdrant를 포함한 검색 전체에 같은 시점의 snapshot을 제공하지 않는다. 기본 PG 저장소의 개별 호출도 앱 색인 전체를 하나의 SQL 트랜잭션으로 묶지 않는다. 조회 중 삭제·재색인이 겹치면 저장소별 중간 상태를 조합할 위험은 남는다.

현재 업무일지 락과 긴 workspace 쓰기 락은 유지한다. 전역 workspace 쓰기에서도 팀 간 경합이 가능하다. 공유 추적 목록을 읽고 추가한 뒤 덮어쓰므로 락만 제거하면 갱신 유실 위험이 있다. 앱의 asyncio 락은 프로세스·이벤트 루프 경계이며, 다중 worker 쓰기 안전성이나 같은 팀의 병렬 수정은 이번 전환으로 보장하지 않는다.

락 범위 축소·원자적 추적 갱신·버전 분리 게시의 필요성은 조회/수정 경합, 공유 노드 갱신, 실패 주입, 팀 이동, 다중 worker 테스트와 처리량 측정 후 판단한다. 이 해법들을 구현 완료로 간주하지 않는다.

## 5. 검증

기본 테스트는 `ai/`에서 실행한다.

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'
./.venv/Scripts/python.exe -B -m pytest -p no:cacheprovider tests -q
```

실제 PG 왕복 테스트는 아래를 모두 명시해야 실행된다.

- `RAG_SCOPE_TEST_ID`: 테스트 실행 표식
- `RAG_SCOPE_TEST_PG_DSN`: `127.0.0.1`, 명시한 비기본 포트, `test_`로 시작하는 별도 DB
- 통합 Qdrant/Neo4j 테스트에는 기존 `RAG_SCOPE_TEST_QDRANT_URL`·`RAG_SCOPE_TEST_NEO4J_URI`도 필요

PG 포트 `5432/5434`, 업무/일반 LightRAG DB 이름, 외부 host는 거절한다. Qdrant/Neo4j는 기존처럼 기본 포트 `6333/7687`을 거절한다. 테스트는 고유 workspace의 생성 데이터만 정리하며 DB·테이블을 drop하지 않는다. LLM·임베딩·업무 source reader를 fixture로 대체한 통합 테스트를 실제 모델·업무 SQL 검증으로 해석하지 않는다.

```powershell
./.venv/Scripts/python.exe -B -m pytest -p no:cacheprovider tests/test_light_worklog_pg_storage_integration.py tests/test_light_worklog_team_storage_integration.py -q
```

명시적 설정이 없으면 실제 저장소 테스트는 skip한다. 실제 DB 준비·전체 재색인·부하 검증 결과는 단위 테스트 통과와 구분해서 기록한다.

### 2026-10-21 로컬 실행 결과

- 실제 PostgreSQL·Neo4j·Qdrant와 Gemini를 사용해 유효 업무일지 200건의 초기 색인을 완료했다. 전역 200건과 10개 팀 각 20건의 `processed`, 전역 200건의 팀 소유권 metadata를 확인했다.
- 최초 80건 완료 후 Gemini `402 RESOURCE_EXHAUSTED`의 선불 크레딧 소진으로 중단했다. 재개 후 남은 120건의 API 응답과 PG 상태를 확인했다. 이는 재시도 완료이며 rollback·실패 보상 검증이 아니다.
- 전역 상태 테이블에는 재요청으로 생긴 `dup-*` 10건이 별도로 남는다. 원본 200건은 `processed`다. 그래프·벡터의 중복/잔존 데이터 전수 검사는 하지 않았다.
- 로컬 회귀 테스트는 Python 3.11.6에서 526개 통과, 격리 외부 저장소 테스트 3개 skip이다. 실제 색인은 Python 3.12 컨테이너에서 실행했지만, Python 3.12의 전체 테스트 실행과 동등하지 않다.
- 실제 AI 조회 API 스모크 7개를 확인했다. 내부 토큰 누락 시 403, 빈 팀 범위의 빈 응답, 팀 101/102 정상 조회, 팀 101 범위에서 팀 102 문서 제외, 팀 102 SSE, 전사 조회가 통과했다. 정상 조회에서 기대한 업무일지 1/73과 원본 작성자 정보를 확인했고, SSE는 token 12개와 done 1개로 종료됐다.
- 해당 표본의 일반·SSE·전사 조회는 약 8.5–12.8초였다. rerank 비활성 상태에서 수행한 소수 요청의 관찰값이며 처리량·전체 검색 품질 평가가 아니다.
- 광범위한 정답 품질 평가, Spring API의 사용자 권한 계산까지 포함한 종단 간 검증, 삭제·팀 이동 장애 복구, 조회/수정 동시성, 다중 worker 및 처리량 검증은 남아 있다. 운영 배포·운영 backfill 완료를 뜻하지 않는다.
