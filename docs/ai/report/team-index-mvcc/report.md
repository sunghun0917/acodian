# 전사 인덱스 제거와 팀 인덱스 두 벌 번갈아 쓰기 구현 보고서

- **대상 기능**: LightRAG 업무일지 색인(쓰기)과 챗봇 조회(읽기)의 격리. 전사 인덱스 제거.
- **분석 범위**: 브랜치 `refactor/ai/team-index-mvcc`의 미커밋 변경 전체(`ai/`, `api/`). `web/next-env.d.ts`는 Next 개발 서버 자동 생성물이라 제외.
- **변경 전 기준**: HEAD `179334b0`. 변경 후 기준: 2026-11-25 현재 worktree(미커밋). 수정 27개 파일(+1510, -2307), 새 파일 12개.
- **설계와 계획**: [`design.md`](design.md)(2026-11-17 작성, 11-19 개정), [`plan.md`](plan.md)(Task 1~9).
- **대화 범위**: 이 세션(2026-11-25)의 사용자 지시와 Orca 오케스트레이션 작업자 보고. 설계를 정한 11-17~11-19 세션 대화는 직접 보지 못했고, 그 결정은 `design.md` 개정 이력으로만 확인했다.

---

## 1. 환경

| 항목 | 값 | 출처 |
|---|---|---|
| AI 서버 | Python, FastAPI, Celery 색인 워커 1개 | `ai/AGENTS.md`, design 5절 |
| 실행한 Python | 3.11.6 (계획 문서는 3.12 표기) | 테스트 경고 원문 `FutureWarning: ... (3.11.6)` |
| LightRAG | `lightrag-hku==1.4.10` | `ai/requirements` |
| Redis 클라이언트 | `redis==7.4.0` (`redis.asyncio`) | `ai/requirements` |
| DB | `SQLAlchemy==2.0.49`, `asyncpg==0.31.0` | `ai/requirements` |
| 저장소 | LightRAG 전용 PostgreSQL(`LIGHTRAG_PG_DSN`, 업무 DB와 분리), Qdrant, Neo4j | design 5절 |
| api | Spring Boot, 검색 서비스가 AI에 허용 팀 목록 전달 | `LightRagWorklogSearchService.java` |
| 통합 테스트 환경 | 테스트 전용 컨테이너 `pgvector/pgvector:pg17`(55432), `redis:7-alpine`(56379), `qdrant/qdrant:v1.17.1`(56333), `neo4j:5.26-community`(57687, 인증 없음). 기본 포트와 운영 DB 이름은 테스트 코드가 거부한다 | `tests/test_light_worklog_pg_storage_integration.py:14` `validate_test_pg_dsn`, `test_light_worklog_team_storage_integration.py:43` |

환경 설정 자체는 바뀌지 않았다. 새 표 두 개는 LightRAG DB에 런타임에 만든다(업무 DB Alembic 대상 아님).

## 2. 문제사항

**확인 사실 (design 1절, HEAD 코드)**

LightRAG는 업무일지 하나를 PG KV(청크, 문서 상태), Qdrant(벡터), Neo4j(그래프) 세 곳에 서로 다른 시점에 쓴다. 세 저장소를 하나의 트랜잭션으로 묶을 수 없다(LightRAG #3400).

1. **색인 중 챗봇이 반쯤 바뀐 데이터를 본다.** 색인과 조회가 같은 workspace를 같은 시간에 쓴다.
2. **수정 중에 업무일지가 챗봇에서 사라진다.** HEAD의 재색인은 `STAGING → 옛 문서 삭제 → 새 문서 저장 → COMPLETED` 순서다(`179334b0:ai/app/light/v4/service/worklog_index_service.py:228, 260, 278`). 그 사이에는 업무일지가 안 보이고, 실패하면 보상 삭제 뒤 옛 내용이 돌아오지 않는다.
3. **검색과 답변 시점이 다르다.** 검색은 인덱스로 하고, 본문은 업무 DB 최신 행에서 읽는다.
4. **전사 인덱스가 병렬화를 막는다.** 업무일지 하나를 전사 workspace와 팀 workspace 두 곳에 쓴다(`179334b0:.../worklog_index_service.py:271, 274` 전사 보상과 `save_indexed_team_ids`). 전사 칸은 모든 팀이 같이 고치므로 팀 단위 락으로 나눌 수 없다.

**LightRAG 1.4.10에서 확인한 제약 (design 1절, Codex 재현)**
- 버전을 고려한 문서 수정 기능이 없다.
- 문서 삭제 중 공유 엔티티 재구성에 실패해도 `success`를 돌려준다.
- LLM 추출 캐시 키에 workspace가 들어가지 않는다. 캐시 저장소를 공유하면 다른 workspace에서도 그대로 다시 쓸 수 있다.

## 3. 해결아이디어

design 6절에 기록된 검토 대안이다. 11-17~11-19 세션에서 논의됐고, 이 세션에서는 문서로만 확인했다.

| 방식 | 판단 |
|---|---|
| 한 인덱스 + 버전 표로 검색 후 거르기 (11-17 첫 안) | 폐기. 커밋 안 된 버전이 검색 상위 자리를 차지하고, 공유 엔티티 설명이 덮어써져 되돌릴 수 없다 |
| 한 인덱스 + 검색 전 가리기 | 공유 엔티티 벡터가 이미 바뀌어 순위가 흔들린다. LightRAG 내부 의존이 크다 |
| 팀 읽기/쓰기 락 | 격리는 되지만 챗봇이 수십 초에서 분 단위로 기다린다 |
| 세대 전환(팀 전체 새로 만들기) | 수정 한 건마다 팀 전체 비용이 든다 |
| **두 벌 번갈아 쓰기** | **채택.** 격리가 완전하고, 챗봇이 기다리지 않고, 바뀐 업무일지만 다시 넣는다 |

이 세션에서 사용자가 내린 결정은 다음과 같다.
- "오케스트레이션을 활용해서 구현해줘". 구현은 Codex, 검토는 Claude Opus. 배정은 plan.md "실행 배정(사용자 확정 2026-11-17)"을 따랐다.
- Codex hook 승인(사용자가 직접 승인).
- "도커 켰어 통합 테스트 돌려줘".

## 4. 해결방식

**구현 완료** (Task 1~9 전부, 미커밋)

| Task | 책임 | 주요 파일 |
|---|---|---|
| 1 | LightRAG DB에 읽기 포인터 표, 사본 내용 표(`READY`/`WORKING`, 지문, 스냅샷, 삭제 전 기록) | `ai/app/light/v4/store/worklog_team_copy_store.py` (신규) |
| 2 | Redis 팀 쓰기 락(연장형, 토큰 비교 해제), 사본 읽기 토큰(Redis `TIME` 만료) | `ai/app/light/v4/service/worklog_team_copy_lock.py` (신규) |
| 3 | 사본 a/b 어댑터, LLM 캐시 공유, 사본 비우기(`drop_all`), 반영(`apply_to_copy`), 재구성(`rebuild_copy`) | `lightrag_adapter.py`, `worklog_team_scope.py`, `worklog_team_copy_apply.py` (신규) |
| 4 | 쓰기 흐름 `sync_worklog`. INDEX/REINDEX/DELETE를 하나로 합침 | `worklog_team_copy_writer.py` (신규), `worklog_index_service.py` |
| 5 | 챗봇 조회: 읽기 사본만 검색, 그 사본의 스냅샷으로 문맥, 권한은 업무 DB 최신 | `worklog_query_service.py`, `worklog_team_scope.py`, `worklog_query_cache.py`, `model/worklog_query.py` |
| 6 | api가 DIRECTOR에게도 전체 팀 목록 전달, `null` 허용 제거 | `LightRagWorklogSearchService.java`, `InternalLightRagWorklogQueryApiDto.java` |
| 7 | 초기화 스크립트(기존 팀 workspace를 사본 a로 등록, b 재구성) | `ai/app/util/init_worklog_team_copies.py` (신규) |
| 8 | 재구축 스크립트 다시 작성 | `ai/app/util/rebuild_light_worklogs_v4.py` |
| 9 | 전사 인덱스 정리 스크립트(사람이 실행) | `ai/app/util/delete_global_worklog_index.py` (신규) |

**진행 방식 (확인 사실, Orca run `run_471f5c480049`)**
- 순서: 1 → 2 → 3 → 4 → 5 → (6, 7, 8, 9 병렬).
- 고위험 Task는 Codex `gpt-6.1-sol`/`high`, Task 6은 `gpt-6.1-terra`/`high`가 구현했다.
- 검토는 매번 새 Claude Opus 세션이 했다: Task 1+2, 3, 4, 5, 6~9 다섯 번.
- 검토 지적은 원래 구현자에게 돌려보내 고쳤다.

**작업 중 coordinator가 정한 해석** (작업자 질문에 대한 답. 사용자 결정이 아니다)
- 업무일지가 이미 삭제됐는데 INDEX/REINDEX가 오면, 삭제로 맞춘 뒤 성공(`indexed=True`)으로 응답한다.
- 초기화 스크립트: `--seed-only` 옵션을 둔다. 발견한 팀 중 포인터가 b인 팀이 하나라도 있으면 전체 실행을 거부한다.
- 재구축 스크립트: 팀 목록은 업무 DB 전체 팀에서 가져오고, 팀마다 전체 ID를 스캔한다(shortcut 주석). 포인터 전환 뒤 캐시 버전을 올린다.

## 5. 핵심코드

**5-1. 조건부 포인터 전환 = 커밋** (`ai/app/light/v4/store/worklog_team_copy_store.py:150` `flip_read_copy`, 신규)

```python
UPDATE worklog_team_copy_pointer SET read_copy = :new{broken_update}
WHERE team_id = :team_id AND read_copy = :expected
RETURNING team_id
```

처음 읽은 포인터가 그대로일 때만 바뀐다. 0건이면 다른 쓰기가 끼어든 것이다. 망가짐 표시도 같은 UPDATE에서 같이 바꾼다(재구축의 `mark_other_broken=True`). 행이 없는 팀은 같은 트랜잭션 안의 `INSERT ... ON CONFLICT DO NOTHING`으로 처리하므로, 동시에 여러 건이 와도 1건만 성공한다.

**5-2. 쓰기 흐름** (`ai/app/light/v4/service/worklog_team_copy_writer.py:39-91` `_sync_team`, 신규, 발췌)

```python
async with self._locks.team_write_lock(team_id) as lease:
    read = await self._store.read_copy(team_id)
    write = other_copy(read)
    await self._locks.wait_readers_drained(team_id, write, timeout=...)   # 옛 사본 독자 배출
    # 망가졌으면 읽기 사본 READY 스냅샷으로 재구성, 아니면 다른 행만 읽기 사본 상태로 되돌림(청소)
    ...
    await lease.ensure_owned()
    current = (await self._source_reader.fetch_source_rows([worklog_id])).get(worklog_id)  # 락 안 재조회
    if snapshot_fingerprint(desired) == have_fp:
        return                                                            # 지문이 같으면 할 일 없음
    try:
        await apply_to_copy(..., copy=write, desired=desired)
    except (CopyBrokenError, LostWriteLockError):
        raise
    except Exception:
        await apply_to_copy(..., desired=<읽기 사본의 이전 스냅샷>)         # 보상, 그 뒤 raise
        raise
    await lease.ensure_owned()
    if not await self._store.flip_read_copy(team_id, expected=read, new=write):
        raise LostWriteLockError(...)
```

반영할 내용은 항상 락 안에서 업무 DB를 다시 읽어 얻는다. 늦게 처리된 옛 요청도 최신 내용을 반영하게 된다. 실패하면 포인터를 바꾸지 않으므로 읽기 사본은 그대로다. 비교는 버전 번호가 아니라 지문으로 하므로, 태그만 바뀐 경우도 잡는다.

**5-3. 읽기 토큰: 등록 뒤 포인터 재확인** (`ai/app/light/v4/service/worklog_team_copy_lock.py:109` `read_copy`, 신규)

```python
while True:
    copy = await store.read_copy(team_id)
    ...
    try:
        await self._redis.eval(_REGISTER_READER, 1, key, token, self._reader_ttl_ms)  # Redis TIME + TTL
        if await store.read_copy(team_id) == copy:
            yield copy
            return
    finally:
        await self._redis.zrem(key, token)
```

토큰을 등록한 사이에 포인터가 바뀌었으면 다시 시도한다. 그래서 쓰기 쪽 배출 확인과 경쟁하지 않는다. 예외나 취소가 나도 `finally`에서 토큰을 지운다. 프로세스가 죽은 경우에는 Redis 시계 기준 TTL이 지나면 정리된다.

**5-4. 쓰기 락: 토큰 비교 해제** (`worklog_team_copy_lock.py:16-24` Lua, `:92` `team_write_lock`)

```lua
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
```

`SET NX PX`로 잡고, TTL의 1/3 간격으로 연장한다. 연장과 해제 모두 토큰이 같을 때만 하므로 남의 락은 지우지 않는다.

**5-5. 검토에서 고친 부분: 끊긴 반영 뒤 삭제 기록 합치기** (`ai/app/light/v4/service/worklog_team_copy_apply.py:28-34`)

```python
footprint = await adapter.document_footprint(document_id)
if entry is not None and entry.state == "WORKING" and entry.footprint is not None:
    footprint = DocumentFootprint(
        entry.footprint.chunk_ids | footprint.chunk_ids, ...)
```

반영이 중간에 실패하면 새 내용 일부(새 청크, 새 엔티티)가 남는다. 처음 구현은 다음 작업이 옛 기록만으로 "삭제 후 남은 참조"를 확인했다. 그래서 새 조각 쪽의 숨은 삭제 실패를 놓치고, 망가진 사본이 게시될 수 있었다(Opus 검토가 코드로 확인). 저장된 기록에 현재 기록을 합쳐 확인하도록 고쳤다.

**5-6. api** (`LightRagWorklogSearchService.java` `resolveAllowedTeamIds`, diff)

```java
-            case WorklogVisibilityScope.All ignored -> null;
+            case WorklogVisibilityScope.All ignored -> worklogRepository.findVisibleTeamIds(scope);
```

HEAD에서는 DIRECTOR가 `null`을 보내 전사 인덱스를 검색했다. 이제 업무가 있는 팀 전체 목록을 보낸다.

## 6. 전후 해당 서비스 로직

**시나리오 A: 업무일지 7번 수정 (REINDEX)**

| 단계 | 변경 전 (HEAD) | 변경 후 |
|---|---|---|
| 진입 | api Outbox → Celery `reindex_worklogs` | 같음. `reindex_worklogs` → `sync_worklog(7)` |
| 대상 | 전사 workspace와 팀 workspace(같은 칸을 챗봇도 읽음) | 그 팀의 쓰기 사본(챗봇이 읽지 않는 쪽) |
| 처리 | 전사 문서 `STAGING` → 옛 문서 삭제 → 새 문서 넣기 → 소유 팀 저장 → `COMPLETED` | 팀 쓰기 락 → 쓰기 사본 독자 배출 → 청소 → 락 안 재조회 → 지문 비교 → 반영(`WORKING` → `READY`) → 소유 확인 → 조건부 포인터 전환 |
| 그동안 챗봇 | 7번이 안 보이거나 세 저장소가 어긋난 상태를 본다 | 이전 읽기 사본의 완성된 7번을 본다 |
| 실패 시 | 보상 삭제. 옛 내용은 돌아오지 않음 | 쓰기 사본만 이전 상태로 보상. 포인터가 그대로라 챗봇은 이전 내용을 계속 본다 |
| 동시성 | 프로세스 안 락(`worklog_mutation_locks`) | Redis 팀 락이라 프로세스를 넘어서 통함. 나중에 워커를 늘리면 팀 단위로 병렬화할 수 있다 |

**시나리오 B: 챗봇 질문 (팀원, 부서 총괄, DIRECTOR)**

| 단계 | 변경 전 | 변경 후 |
|---|---|---|
| api 권한 | DIRECTOR는 `allowedTeamIds=null` | 모든 역할이 팀 목록을 보냄. 빈 목록이면 AI를 호출하지 않음(Task 6 테스트) |
| AI 검색 | `null`이면 전사 workspace, 아니면 팀 workspace(`179334b0:.../worklog_query_service.py:97, 121`) | 팀마다 읽기 토큰 등록 → 그 팀 읽기 사본만 검색 (`worklog_team_scope.py:95` `read_copies`) |
| 문맥 | 업무 DB 최신 행 | 검색한 사본의 `READY` 스냅샷. 키는 (현재 팀, 업무일지) |
| 권한 거르기 | 업무 DB | 업무 DB 최신 행으로만(삭제 여부, 현재 팀). 스냅샷의 팀 정보는 권한 판단에 쓰지 않음 |
| 토큰 해제 | 해당 없음 | 검색과 문맥 생성이 끝나면 LLM 호출 전에 해제. 예외와 취소에도 해제 |
| 호환성 | | 응답 모델은 그대로. AI는 `allowed_team_ids`를 필수(`min_length=1`)로 받는다. **api와 ai는 같이 배포해야 한다**(구 api가 `null`을 보내면 422) |

**시나리오 C: 처음 전환 (사람이 실행)**
- 변경 전: 해당 없음.
- 변경 후: Outbox 대기 0 확인 → 워커 정지 → `init_worklog_team_copies` → 워커 재기동 → 확인 뒤 `delete_global_worklog_index`. 순서는 design 7-12, 7-13.

## 7. 평가 및 결과

**요구사항별 평가 (design 2절 목표, 4절 실패 조건 기준)**

| 요구사항 | 평가 | 근거 |
|---|---|---|
| 재색인 중에도 챗봇은 완성된 인덱스만 본다 | 충족 | 쓰기 사본과 읽기 사본 분리, 조건부 전환. Task 4, 5 검토 통과 |
| 챗봇이 기다리지 않는다 | 충족(코드 기준) | 읽기는 토큰 등록만 하고 락을 기다리지 않는다. 지연은 측정하지 않았다 |
| 답변 문맥 = 검색한 사본의 스냅샷 | 충족 | Task 5 검토: 스냅샷은 (현재 팀, 업무일지) 키로 고름 |
| 실패해도 이전 내용이 보인다 | 충족 | 모든 실패 경로에서 포인터 불변(Task 4 검토) |
| 업무일지 하나의 반복 실패가 팀을 막지 않는다 | 충족 | `test_poison_worklog_does_not_block_team` 통과 |
| 태그만 바뀐 변경 반영 | 충족 | `test_tag_only_change_is_applied` 통과 |
| 전환 직후 옛 사본 독자 대기 | 충족 | `test_next_job_waits_for_readers_before_cleanup` 통과 |
| 끊긴 `WORKING` 복구 | 충족 | `test_working_row_is_restored_from_read_copy_by_next_job` 통과 |
| 팀 이동 중 DIRECTOR 조회 | 충족 | `test_team_move_keeps_current_team_snapshot_for_director`(팀 번호 대소 양쪽) 통과 |
| 삭제한 업무일지 즉시 제외 | 충족 | 업무 DB 최신 행 기준 거르기(Task 5 검토) |
| 전사 인덱스 제거 | 충족(코드) / 미실행(데이터) | 조회와 쓰기 경로에 전사 참조 없음. 기존 전사 데이터 삭제는 사람이 스크립트로 실행해야 한다 |
| LLM 비용이 2배가 되지 않는다(plan 중단 조건) | 충족(통합 테스트) | `test_copy_b_insert_hits_shared_llm_cache` 실제 PG/Qdrant/Neo4j에서 통과. LLM은 가짜 함수라 실제 Gemini 비용은 측정하지 않았다 |
| api 재시도, 알림, 수동 재시도 유지 | 충족 | `test_light_worklog_tasks` 변경 없음(Task 4 검토) |

**실행한 테스트 (2026-11-25, 이 세션)**

| 실행 | 결과 원문 |
|---|---|
| ai 전체, 단위만 (최종) | `2 failed, 727 passed, 34 skipped` |
| ai 전체, 실제 PG/Redis/Qdrant/Neo4j 연결 (최종) | `2 failed, 747 passed, 14 skipped, 2 warnings in 103.86s` |
| api 전체 (Testcontainers) | `BUILD SUCCESSFUL in 4m 12s`, XML 합계 `tests=681 failures=0 errors=0 skipped=7` |
| Task별 Codex 보고 | T1 `17 passed, 8 skipped`, T2 `13 passed, 7 skipped`, T3 수정 후 `69 passed, 2 skipped`, T4 `50 passed`, T5 `135 passed, 2 skipped`, T6 `tests=5 failures=0`, T7 `18 passed`, T8 `25 passed`, T9 `80 passed` |

- 실패 2건은 `tests/pr_review/test_tools.py::test_read_match_context_*`(`FileNotFoundError`)이다. 이번 변경과 관계없다(그 파일은 diff에 없다). 테스트가 예전 OneDrive 한글 경로를 잡는 Windows 환경 문제로 보이며(추론), 실행마다 재현 여부가 다르다.
- 통합 실행에서 처음 실패한 `test_drop_all_only_clears_own_workspace`는 테스트 데이터 문제였다. Neo4j 노드에 필수 필드 `entity_id`가 빠져 있었다(`ValueError: Neo4j: node properties must contain an 'entity_id' field`). `ai/tests/test_light_worklog_team_copy_apply.py:461`에 필드를 추가해 통과시켰다. 실제 코드는 바뀌지 않았다.
- 검토 지적을 고칠 때는 수정 전 코드에서 새 테스트가 실패하는 것을 먼저 확인했다. Task 3: 7개 실패, Task 7: `DID NOT RAISE`, Task 8: 3개 실패.

**남은 검증 공백**
- 건너뛴 14개: 격리 E2E 10개(`WORKLOG_NORMAL_E2E_ENABLED` 필요), Linux prefork 워커 런타임 3개, 워커 격리 1개. 모두 원래부터 있던 테스트다.
- 실제 Gemini를 쓴 색인 비용, 응답 지연, 큰 팀 재구성 시간은 측정하지 않았다.
- 초기화, 재구축, 전사 삭제 스크립트는 실제 데이터로 실행하지 않았다.

## 8. 트레이드오프

| 얻은 것 | 감수한 것 |
|---|---|
| 완전한 읽기 격리, 챗봇 대기 없음 | 저장 공간과 임베딩 비용이 팀마다 2배. LLM 추출 비용은 캐시 공유로 늘지 않음(통합 테스트 확인) |
| 바뀐 업무일지만 다시 넣음 | 쓰기 한 번에 "청소(이전 변경 따라잡기) + 반영"으로, 지연이 이전보다 길다(추론, 측정 안 함) |
| 프로세스를 넘는 Redis 락. 워커만 늘리면 팀 단위 병렬화 가능 | Redis가 색인과 조회 모두의 의존성이 됨 |
| 전사 인덱스 제거로 팀 단위 독립 | DIRECTOR 질문은 팀 수만큼 검색을 합침. 팀 사이 시점은 맞추지 않음(design 3절, 받아들인 한계) |
| 지문 비교로 태그와 선행 업무 변경도 잡음 | 스냅샷 JSON을 표에 저장 |
| 포트폴리오 범위로 단순화(2026-11-12 결정) | 큰 팀 재구성 시간, 청소 반복 실패는 보류(design 12절) |
| INDEX/REINDEX/DELETE를 `sync_worklog` 하나로 | 소스가 없는 INDEX도 성공으로 응답(coordinator 해석). api 쪽 의미가 "색인됨"에서 "최신 상태로 맞춰짐"으로 바뀜 |

## 9. 추가 개선점

**필수 (배포 전)**
1. **api와 ai를 같이 배포한다.** 구 api가 DIRECTOR에게 `null`을 보내면 새 ai가 422를 돌려준다(Task 5 검토 확인).
2. **전환 절차를 실행한다.** Outbox 대기 0 확인 → 색인 워커 정지 → `python -m app.util.init_worklog_team_copies` → 워커 재기동 → 챗봇 확인 → `delete_global_worklog_index`. 실제 데이터로 아직 실행하지 않았다.
3. **격리 E2E 10개를 실행한다.** 전체 서비스 흐름(api Outbox → 워커 → 조회)을 끝까지 확인한 적이 아직 없다.

**선택**
- 오류 메시지 정리: `worklog_team_scope.py:96-106`의 `except Exception`이 검색과 문맥 생성 예외까지 "Team read copy lookup failed"로 감싼다(Task 5 검토).
- 죽은 코드 제거: `light_worklog_tasks.py:84`의 `ERROR_WORKLOG_NOT_FOUND` 분기, `worklog_index_service.py:26,31` 상수, `LightRagWorklogSearchService.java:46`의 항상 참인 조건(Task 4, 6~9 검토).
- 재구축 스크립트의 팀별 전체 ID 스캔(shortcut 주석): 업무일지가 많아지면 팀별 조회 메서드를 추가한다.
- 테스트 이름: `test_missing_lock_stops_renewal_and_invalidates_lease`는 이름과 달리 연장 중단을 단언하지 않는다(Task 1+2 검토).
- 팀 검색 시간 초과 응답이 500에서 504로 바뀌었다. 계획의 단일 기한 요구 때문으로 보인다(Task 5 검토 추측). 504로 둘지 사용자가 확인해야 한다.
- 전사 삭제 시 LightRAG가 남은 공유 엔티티를 재구성하면서 Gemini를 호출할 수 있다(Task 9 검토 추측). 실행 전에 비용을 확인한다.

## 10. 근거와 한계

**근거**
- 설계와 계획: [`design.md`](design.md), [`plan.md`](plan.md)(체크박스 45개 모두 완료 표시. Task 6~9는 coordinator가 표시)
- 코드: 위 5절의 `path:line`. 변경 전은 `179334b0`의 `git show`로 확인
- 검토: Orca run `run_471f5c480049`의 Opus 검토 보고 5건(Task 1+2, 3, 4, 5, 6~9)
- 테스트: 7절 표의 원문. 최종 ai/api 전체와 통합 실행은 coordinator가 직접 실행했고, Task별 수치는 Codex 보고다

**한계**
- 11-17~11-19 설계 세션 대화는 보지 못했다. 그 결정은 `design.md` 개정 이력으로만 확인했다.
- Task별 테스트 수치는 Codex 작업자 보고이고, coordinator는 최종 전체 실행만 직접 확인했다.
- 코드 수준 확인은 대부분 Opus 검토자가 했다. coordinator는 핵심 흐름(5절), 검토 지적의 수정 결과, 스크립트의 비공개 멤버 접근 여부만 직접 읽었다.
- LLM과 임베딩은 통합 테스트에서도 가짜 함수다. 실제 Gemini 동작과 비용은 검증하지 않았다.
- 성능(지연, 처리량)은 측정하지 않았다.
- 모든 변경은 미커밋이다.
