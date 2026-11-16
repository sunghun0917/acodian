# LightRAG v4 전환 (팀별 검색, PostgreSQL 저장소, 1단계 안정화)

> **현재 상태:** 완료. 지금 서비스 코드는 `ai/app/light/v4/`이고, v3 코드는 삭제됐다(2026-11-13 커밋 `32c18af5`)(차이는 [v3 → v4 변경 기록](v3-v4-differences.md)). 11-12 단순화로 일부 안전 장치는 제거됐지만 팀 workspace, 완료 게이트, 보상 삭제, DB 재조회는 유지된다.
> **기간:** 2026-10-13 (팀 사전 필터 계획) → 10-14 (리뷰 결함 수정) → 10-21 (PostgreSQL 저장소) → 10-24 (1단계 안정화)
> **한 줄 요약:** 팀마다 LightRAG workspace를 따로 두어 검색 단계부터 권한을 나누고, LightRAG의 KV/문서 상태를 JSON 파일에서 전용 PostgreSQL DB로 옮겼다. 부분 실패한 색인은 문서별로 되돌리고, 전역과 팀 색인이 모두 끝나야만 검색에 노출되게 했다.

---

## 1. 배경: 무엇이 문제였나

### 권한
- v3는 전역 workspace 하나에서 검색한 뒤 권한으로 걸렀다(사후 필터). 허용 안 된 후보가 상위를 차지하면 재현율이 떨어졌다.

### 저장소
- LightRAG의 KV, 문서 상태가 로컬 JSON 파일이었다. 여러 컨테이너가 공유하기 어렵고 운영에 부적합했다.

### 부분 실패 (10-24 시점)
업무일지 하나를 **전역 + 팀 workspace에 각각 본문과 KG로** 저장한다. 저장소가 여러 개라 한 트랜잭션으로 묶을 수 없다.

| 문제 | 영향 |
|---|---|
| 일부만 성공한 색인이 남음 | 다음 실행이 부분 문서를 건너뛰거나 정리가 막힘 |
| 배치 중 하나만 실패해도 전체 실패 처리 | 성공한 문서까지 재처리 |
| LightRAG의 `processed`가 "전역, 팀 모두 성공"을 뜻하지 않음 | 작업 중인 문서가 답변에 들어갈 수 있음 |
| 전사 검색은 LightRAG 답변을 그대로 사용, 캐시도 DB 확인을 우회 | **삭제된 업무 내용이 답변에 남음** |
| LightRAG의 `status=failure`를 모두 "결과 없음"으로 취급 | 실제 장애가 정상 빈 응답처럼 보임 |

## 2. 목표와 완료 기준

- 검색 단계부터 허용 팀 범위로 제한한다.
- LightRAG 전용 PostgreSQL DB를 쓴다. **업무 DB와 분리한다.**
- 문서별로 실패, 보상, 재시도한다.
- 전역과 팀의 본문, KG, 소유권 기록이 **모두** 성공해야 검색에 노출한다.
- 일반 응답과 SSE 모두 LLM 호출 전에 최신 업무 DB를 확인한다.
- 범위 밖: Redis 분산 락, 다중 writer, 저장소 간 원자적 트랜잭션, 무중단 전환.

## 3. 선택지와 결정

### 팀 권한을 어떻게 나눌까
| 대안 | 문제 | 선택 |
|---|---|---|
| 벡터 payload, 그래프 노드에 `team_id` 필드 추가 후 필터 | LightRAG가 payload 필드를 고정해서 **임의 필드가 유지되지 않음**. 모든 저장소 구현을 고쳐야 함 | X |
| **팀별 LightRAG workspace** (기존 workspace 격리 기능 재사용) | Qdrant workspace 필터, Neo4j workspace 라벨로 공유 User/Tag까지 물리적으로 분리 | **O** |

**비용:** 허용 팀이 여러 개면 팀별로 병렬 검색해야 한다. 전역 workspace도 함께 유지하므로(전사 권한, 롤백용) 색인과 저장 공간이 늘어난다.

### LightRAG KV, 문서 상태 저장소
| 대안 | 판단 |
|---|---|
| JSON 유지 | 공유, 운영 어려움 |
| 직접 만든 PG 저장소 | 유지 부담 |
| **LightRAG 기본 `PGKVStorage`, `PGDocStatusStorage` + 전용 DB `axwms_lightrag`** | **선택**. 벡터는 Qdrant, 그래프는 Neo4j 유지 |

- 기존 JSON 데이터는 이관하지 않고 **전체 재색인**했다(배포 전 개발 환경).
- 색인 당시 팀 ID는 `doc_status.metadata.indexed_team_id`에 저장한다. 업무가 다른 팀으로 옮겨도 옛 색인 위치를 찾을 수 있다.

### 부분 실패를 어떻게 정리할까
| 대안 | 판단 |
|---|---|
| 과거 색인으로 되돌리는 롤백 | 저장소마다 이전 상태를 보관해야 해서 무거움 |
| **Saga 보상: 이번에 쓴 부분 데이터만 삭제** | **선택**. 첫 쓰기 전에 `workspace × document_id` 삭제 작업을 등록하고, 실패하면 팀 → 전역 역순으로 정리 |

**포기한 것 (트레이드오프):** 보상 목록이 메모리에 있어서 프로세스가 강제 종료되면 사라진다. 답변 캐시를 꺼서 매번 검색, 검증, 생성을 한다. 후보마다 소유권과 상태를 조회하므로 DB 부하가 늘 수 있다.

## 4. 구현: 어떻게 동작하나

### 저장 구조
```text
PostgreSQL 서버
├─ 업무 DB          : 업무일지, 팀, 사용자, 태그, 의존 관계 (원본)
└─ axwms_lightrag   : LIGHTRAG_DOC_FULL, DOC_CHUNKS, LLM_CACHE,
                      FULL_ENTITIES, FULL_RELATIONS, ..., DOC_STATUS
Qdrant              : 청크, 엔티티, 관계 벡터 (workspace별)
Neo4j               : 지식 그래프 (workspace별)
```
초기화할 때 세 저장소의 workspace 이름이 일치하는지 검증한다. `LIGHTRAG_PG_DSN`이 업무 DB와 같으면 거부한다.

### 색인 흐름
```text
업무 잠금 → 최신 원본 조회
  → 상태 STAGING으로 시작 (신규 문서의 첫 쓰기 전 보상 등록)
  → 전역 본문 → 전역 KG → 팀 소유권 기록 → 팀 본문 → 팀 Custom KG
  → native processed, KG 추적 확인
  → 전역 metadata에 COMPLETED 게시 → 다시 읽어서 확인
  ─ 실패 시: 팀 → 전역 순서로 이번 데이터 삭제
            보상까지 실패하면 COMPENSATION_FAILED (소유권 보존, 재시도 가능)
```

### 검색 흐름
```text
LightRAG(허용 팀 workspace들)에서 후보 ID만 찾기
  → 후보마다 확인:
     - 전역 COMPLETED인가
     - 업무 DB에 살아 있는가 (is_deleted=false, 팀 deleted_at IS NULL)
     - indexed_team_id == 현재 팀인가 (팀 이동 후 옛 색인 차단)
     - 팀 workspace processed인가
     - DB 조회 직후 다시 COMPLETED 확인 (still_completed)
  → 검증된 원본의 제목, 내용, 작성자, 팀, 태그, 허용된 직접 선행 관계만 LLM에 전달
  → 일반 응답과 SSE가 같은 경로 사용
```
- `allowedTeamIds=null`이면 전사 범위, `[]`이면 바로 빈 응답.
- LLM 프롬프트에 "확정 관계는 권한 필터됨, 없는 관계를 추측하지 말 것"을 넣는다.

### 핵심 코드 위치 (`ai/app/light/v4/` 기준)
| 역할 | 파일 |
|---|---|
| LightRAG 연결, PG 설정 분리, 완료 게이트, 삭제 후조건 | `service/lightrag_adapter.py` |
| 팀 workspace 검색, 색인 경계 | `service/worklog_team_scope.py` |
| 색인, 보상 | `service/worklog_index_service.py` |
| 질의 (후보 → 검증 → 생성) | `service/worklog_query_service.py` |
| 업무 원본 조회 | `store/worklog_source_store.py`, `service/worklog_source_row_reader.py` |
| 업무 잠금 | `service/worklog_mutation_lock.py` |
| 팀 벡터 저장소 초기화 (자동 이관 금지) | `store/worklog_team_vector_store.py` |
| Custom KG 삭제 추적 보완 | `store/worklog_custom_kg_tracking.py` |

## 5. 결과와 검증

### 1단계 안정화 실서버 검증 (10-24)
별도 API, AI, Redis와 새 DB 2개, workspace를 분리한 실제 Qdrant/Neo4j. **실제 Gemini 생성과 임베딩 호출.** 가상 업무 1건.

| 시나리오 | 결과 |
|---|---|
| 등록 → 201, 원본과 Outbox 동시 저장, 32.17초 후 전역 COMPLETED | 통과 |
| 전사/팀 × 일반/SSE 검색 | 통과 |
| PG 쓰기 실패 1회 주입 (DB trigger) → 보상 후 문서 상태 0개, 해제 후 재요청 성공 | 통과 |
| 보상 삭제까지 실패 → COMPENSATION_FAILED, 소유권 보존, 해제 후 재시도 성공 | 통과 |
| 전역 상태만 STAGING으로 변경 → 검색 결과 비어 있음, SSE 토큰 0 | 통과 |
| 업무 DB에서 `is_deleted=true` → 문맥과 참조에서 제외 | 통과 |
| doc-status 테이블 장애 → 일반 500 `QUERY_FAILED`, SSE error 이벤트 | 통과 |
| Redis 정지 → 색인, 검색 정상 (캐시 의존 제거 확인) | 통과 |
| API 삭제 → Qdrant 청크/엔티티/관계 벡터, Neo4j 노드 모두 0 | 통과 |

AI 회귀 테스트 592 passed, 3 skipped.

### PostgreSQL 저장소 전환 (10-21)
- 524 passed. 실제 PG/Neo4j/Qdrant 통합 3건은 당시 Docker가 없어 건너뜀(10-24 실서버 검증에서 확인)
- 최종 리뷰에서 2건 지적 → 수정: 모든 문서가 없을 때 빈 결과 정규화, **LightRAG가 PG 삭제 실패를 삼키는 문제**(삭제 후 다시 읽어서 확인하는 후조건 추가)

## 6. 진행 중 바뀐 결정 (날짜순)

| 날짜 | 내용 |
|---|---|
| 10-13 | **처음 목표: AI의 업무 DB 재조회를 0회로.** 색인할 때 팀, 작성자, 선행 관계를 같이 저장하고 검색 결과만으로 답변을 만드는 계획 |
| 10-13 | payload에 team_id를 넣는 대신 **팀별 workspace 재사용**으로 결정 |
| 10-13 | 보안 게이트: 실제 저장소 round-trip과 최신성 증명 전에는 DB 검증을 없애지 않기로 함 |
| 10-14 | 리뷰 결함 7건 수정: `/index` 내부 토큰 누락, 같은 작성자를 참조하는 업무의 동시 색인/삭제 경합, native insert가 정상 반환했지만 실제 상태는 failed인 경우, 빈 팀 검색 등 |
| **10-21** | **방향 전환: 색인에 업무 snapshot을 저장하지 않고, 검색 후 업무 DB에서 최신 원본을 읽는다.** snapshot 대신 소유권 최소 정보(`indexed_team_id`)만 남긴다. 설계서에 이유는 따로 적혀 있지 않다. 10-13 계획이 "최신성 증명 전에는 DB 검증 유지"를 조건으로 걸었는데, 그 증명이 끝나지 않은 상태였던 것으로 보인다 |
| 10-21 | LightRAG 기본 PG 저장소로 전환, 기존 JSON 데이터는 이관 없이 재색인 |
| 10-24 | 1단계 안정화: Saga 보상, 전역 COMPLETED 게이트, 전사 검색도 DB 검증, **답변 캐시 비활성화** |
| 11-10 ~ 11-13 | v3 코드 삭제. 11-10에 차이 기록을 남기고 11-13에 커밋 (이름에 v3가 남은 설정값과 Redis 키는 기존 데이터 때문에 유지) |
| 11-12 | 단순화로 원격 쓰기 안전 장치 제거 ([AI 작업 신뢰성](../../../api/report/ai-task-reliability/README.md) 참고). 이 문서의 게이트와 보상은 유지 |

**정리:** 결과적으로 "DB 재조회를 없애서 빠르게"보다 "최신성과 권한을 확실히"를 택한 셈이다. 색인에 원본 복사본을 두면 복사본이 최신인지 따로 보장해야 한다.

## 7. 한계와 위험

- **강한 원자성 아님:** PG, Qdrant, Neo4j를 하나의 트랜잭션으로 묶지 못한다. 보상으로 정리할 뿐이다.
- **강제 종료:** 보상 목록과 pending 상태가 메모리라서 SIGKILL 후에는 자동 재개되지 않는다.
- **단일 프로세스:** 메모리 락이라 여러 Worker, 별도 CLI, 다중 writer를 제어하지 못한다.
- **동일 시점 snapshot 아님:** 완료 확인, DB 검증, 답변 생성 사이에 원본이 바뀌는 것을 원자적으로 막지는 않는다.
- **재색인 중 검색 공백:** 삭제 후 재삽입 방식이라 이미 완료된 문서도 재색인 중에는 잠시 안 보일 수 있다.
- **LightRAG 1.4.10 의존:** 삭제 후조건 확인, 팀 벡터 초기화 등이 라이브러리 내부 동작에 기대고 있어 버전을 올리면 다시 검증해야 한다.
- **보안:** 팀 workspace로 물리 격리, 검색 후 DB에서 권한과 삭제 여부 재검증, 팀 이동 후 옛 색인 차단, 다른 팀 선행 업무의 제목은 팀 사본에서 제거.

## 8. 앞으로 개선할 점

1. 영속 작업 기록으로 강제 종료 후 보상 재개
2. 원본 revision을 색인 상태와 연결해 오래된 완료가 최신을 덮지 않게
3. 다중 Worker 전 분산 락 (소유자 토큰, 만료, 연장)
4. 답변 캐시 복원 (참조 문서의 revision과 권한을 HIT 전에 확인)
5. 재색인 중 무공백 (새 세대 workspace 구축 후 전환)

## 9. 스스로 점검 질문

- **Q. 왜 팀마다 workspace를 따로 두나?**
  A. 검색 단계부터 다른 팀 데이터를 아예 안 보게 하려고. 사후 필터는 다른 팀 후보가 상위를 차지하면 재현율이 떨어지고, LLM 입력에 섞일 위험도 있다.
- **Q. LightRAG 전용 DB를 업무 DB와 왜 분리하나?**
  A. 검색용 파생 데이터와 원본을 섞지 않기 위해서다. LightRAG가 만드는 테이블이 업무 스키마와 마이그레이션에 영향을 주지 않는다.
- **Q. Saga 보상과 롤백의 차이는?**
  A. 롤백은 이전 상태로 되돌린다. 여기서의 보상은 이번 실행이 쓴 부분 데이터만 지운다. 이전 색인을 복원하지는 않는다.
- **Q. `processed`가 있는데 왜 COMPLETED 게이트를 따로 두나?**
  A. `processed`는 LightRAG가 한 workspace에서 처리를 끝냈다는 뜻일 뿐이다. 전역과 팀, 본문과 KG, 소유권까지 모두 끝났는지는 따로 기록해야 안다.
- **Q. 검색 결과에 본문이 있는데 왜 업무 DB를 다시 읽나?**
  A. 색인은 삭제, 팀 이동, 수정보다 늦게 반영될 수 있다. 원본 DB가 최신 진실이라서 LLM에 넣기 전에 확인한다.
- **Q. 10-13 계획과 반대로 DB 재조회를 유지한 이유는?**
  A. 원본 문서에 이유가 직접 적혀 있지는 않다. 다만 색인에 원본 복사본을 저장하면 복사본이 최신인지 증명해야 하는데, 비동기 색인 구조에서는 이게 어렵다. 읽기 한 번 더 하는 비용이 오답 위험보다 싸다고 설명할 수 있다.

## 10. 원본 문서

| 문서 | 내용 |
|---|---|
| [phase1-stabilization-2026-10-24.html](phase1-stabilization-2026-10-24.html) | **1단계 안정화 보고서**: 문제, 설계, 실서버 검증, 트레이드오프, 후속 과제 |
| [../../guide/rag-flow.html](../../guide/rag-flow.html) | 등록, 수정, 삭제, 검색 흐름도 |
| [v3-v4-differences.md](v3-v4-differences.md) | **v3 → v4 차이 정리** (v3 삭제 전 기록) |
| [research-concurrency-consistency.md](research-concurrency-consistency.md) | 동시성, 정합성 분석 (2026-10-21, PG 전환 직전이라 색인 snapshot 시절 기준) |
| [research-postgres-migration.md](research-postgres-migration.md) | LightRAG KV → PostgreSQL 전환 조사 (2026-10-19, 공식 문서와 소스 조사) |
| [../../guide/lightrag-postgres-setup.md](../../guide/lightrag-postgres-setup.md) | PG 전환, 재색인 운영 절차 |
| [../../guide/lightrag-index-rebuild.md](../../guide/lightrag-index-rebuild.md) | 1단계 인덱스 전환 (별도 workspace 재구축) 절차 |
| [../../guide/lightrag-team-search.md](../../guide/lightrag-team-search.md) | 팀별 검색 현재 동작 설명 |
| [design-postgres-storage-2026-10-21.md](design-postgres-storage-2026-10-21.md) | PG 저장소 설계 |
| [plan-postgres-storage-2026-10-21.md](plan-postgres-storage-2026-10-21.md) | PG 저장소 구현 계획 |
| `.omx/plans/rag-team-prefilter-db-replacement-2026-10-13.md` | 팀 사전 필터, DB 재조회 제거 계획 (이후 방향 변경) |
| `.omx/plans/lightrag-v4-review-fixes-2026-10-14.md` | 리뷰 결함 7건 수정 계획 |
| `.omx/plans/lightrag-v4-phase1-stabilization-2026-10-24.md` | 1단계 안정화 계획 |
| `.omx/plans/lightrag-v4-phase1-review-2026-10-24.md` | 1단계 계획 검토 |
| `.superpowers/sdd/rag-team-prefilter-db-replacement-2026-10-13/` | 팀 사전 필터 진행 기록, 검토 |
| `.superpowers/sdd/2026-10-21-lightrag-postgres-storage/` | PG 전환 진행 기록, 검토 |
| `ai/data/lightrag-v4-reindex-20261014-200809/` | v4 재색인 실행 기록 |
