# 전사 인덱스 제거와 팀 인덱스 두 벌 번갈아 쓰기 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 팀마다 LightRAG 사본 두 벌을 두고 "쓰기 사본 배출 확인 → 청소 → 반영 → 조건부 포인터 전환"으로, 챗봇이 항상 완성된 사본만 검색하고 그 사본의 스냅샷으로 답하게 한다. 전사 인덱스를 없앤다.

**Architecture:** LightRAG DB에 읽기 포인터 표와 사본 내용 표(`READY`/`WORKING`, 지문, 스냅샷)를 둔다. Redis에 팀 쓰기 락(연장형 배타)과 사본 읽기 토큰(Redis 시계 기반 공유)을 둔다. 색인 워커는 팀 쓰기 락 안에서 쓰기 사본을 읽기 사본 상태로 맞추고, 업무 DB 최신 행을 다시 읽어 반영한 뒤 포인터를 바꾼다. 챗봇은 토큰을 등록한 사본만 검색하고 그 사본의 스냅샷으로 문맥을 만들며 권한만 업무 DB 최신으로 확인한다.

**Tech Stack:** Python 3.12, FastAPI, Celery, LightRAG 1.4.10, SQLAlchemy async + asyncpg, redis 7.4 (`redis.asyncio`), pytest / Spring Boot, JUnit

**Spec:** [`docs/ai/report/team-index-mvcc/design.md`](design.md)

## Global Constraints

- 커밋은 사용자가 명시적으로 승인할 때만 한다 (`CLAUDE.md` 4.1). 각 Task 끝의 "변경 확인"은 커밋이 아니다.
- 코드를 고치기 전에 `ai/AGENTS.md`(ai), `api/AGENTS.md`(api)를 읽고 따른다. api 신규/변경 메서드는 Javadoc 규칙(`SB-020`, `ADR-023`)을 지킨다.
- 두 표는 LightRAG DB(`settings.lightrag_pg_dsn`)에 둔다. 업무 DB에 테이블을 만들지 않는다.
- 사본 이름 `a`, `b`. 사본 `a` workspace = 지금 팀 workspace 이름(`worklog_scope_v1_{hash}_team_{id}`) 그대로, 사본 `b` = 뒤에 `_b`.
- LightRAG 문서 ID는 지금처럼 `worklog-{id}`.
- **변경 여부는 지문으로 판단한다. 버전 번호(`tb_worklog.version`)는 쓰지 않는다.**
- **반영할 내용은 항상 팀 쓰기 락 안에서 업무 DB를 다시 읽어 얻는다.** 요청에 실린 내용이나 락 밖에서 읽은 내용으로 반영하지 않는다 (락 밖 읽기는 팀 순서 결정에만 쓴다).
- **청소는 쓰기 사본을 읽기 사본의 `READY` 상태로 되돌리기만 한다.** 다른 업무일지의 새 내용을 색인하지 않는다.
- 쓰기 사본을 고치는 모든 경로(청소, 반영, 다시 만들기)는 시작 전에 그 사본의 읽기 토큰이 0인지 확인한다.
- 한 작업은 옛 읽기 사본을 고치지 않는다 (설계 7-5).
- 팀 쓰기 락은 한 번에 하나만 잡는다.
- 읽기 토큰 만료 계산과 정리는 Redis `TIME`으로 한다.
- 색인 워커 1개 구조를 유지한다. 포트폴리오 프로젝트이므로 설계 4절의 실패만 막는다.
- 문서와 커밋 메시지에 가운뎃점(U+00B7)을 쓰지 않는다.
- ai 테스트: `cd ai; .\.venv\Scripts\python.exe -m pytest <경로> -q` (Windows), `.venv/bin/pytest <경로> -q` (POSIX). api 테스트: `cd api; ./gradlew test --tests "<패턴>"`

## Review Focus

1. **전환 직후 작업이 끊긴 뒤 다음 작업**: 포인터 전환 뒤 옛 사본을 읽는 질문이 남아 있으면, 다음 작업은 그 사본(이제 쓰기 사본)을 청소하기 전에 질문이 끝나길 기다려야 한다. → Task 4 `test_next_job_waits_for_readers_before_cleanup`
2. **반영 중 프로세스 종료**: `WORKING`이 남은 채 다른 업무일지 요청이 오면 그 업무일지를 읽기 사본 상태로 되돌린 뒤 진행하고, 빠진 사본을 게시하지 않아야 한다. → Task 4 `test_working_row_is_restored_from_read_copy_by_next_job`
3. **반복 실패하는 업무일지**: 7번이 매번 실패해도 8번 요청은 성공해야 하고 7번은 이전 내용으로 남아야 한다. → Task 4 `test_poison_worklog_does_not_block_team`
4. **태그만 바뀐 요청**: 업무일지 행은 그대로이고 태그만 추가된 `REINDEX`가 반영되어야 한다. → Task 4 `test_tag_only_change_is_applied`
5. **두 팀을 다 보는 사람의 팀 이동 중 조회**: 새 팀 스냅샷이 남고 옛 팀 스냅샷이 덮어쓰지 않아야 한다(팀 번호 대소 양쪽). → Task 5 `test_team_move_keeps_current_team_snapshot_for_director`

---

### Task 1: 읽기 포인터 표와 사본 내용 표

**Files:**
- Create: `ai/app/light/v4/store/worklog_team_copy_store.py`
- Test: `ai/tests/test_light_worklog_team_copy_store.py`

**Interfaces:**
- Produces:
  - `TeamCopy = Literal["a", "b"]`, `def other_copy(copy: TeamCopy) -> TeamCopy`
  - `def snapshot_fingerprint(row: LightWorklogSourceRow | None) -> str | None` (태그는 `tag_id`, 선행 업무는 `worklog_id` 기준 정렬 후 정규화 JSON의 sha256. `None`이면 `None`)
  - `def snapshot_to_json(row) -> dict` / `def snapshot_from_json(data) -> LightWorklogSourceRow`
  - `@dataclass(frozen=True) class DocumentFootprint: chunk_ids: frozenset[str]; entity_names: frozenset[str]; relation_keys: frozenset[tuple[str, str]]` (JSON 직렬화 함수 포함)
  - `@dataclass(frozen=True) class CopyEntry: team_id: int; copy: TeamCopy; worklog_id: int; state: Literal["READY", "WORKING"]; fingerprint: str | None; snapshot: LightWorklogSourceRow | None; footprint: DocumentFootprint | None`
  - `class WorklogTeamCopyStore`
    - `async def read_copy(self, team_id) -> TeamCopy` (행 없으면 `"a"`)
    - `async def flip_read_copy(self, team_id, *, expected: TeamCopy, new: TeamCopy, mark_other_broken: bool = False) -> bool` (조건부 UPDATE 한 문장. `mark_other_broken`이면 같은 UPDATE에서 `broken_copy = expected`. 행이 없으면 `expected == "a"`일 때 INSERT. 바뀌면 `True`)
    - `async def broken_copy(self, team_id) -> TeamCopy | None` / `async def set_broken_copy(self, team_id, copy: TeamCopy | None) -> None`
    - `async def entries(self, team_id, copy, worklog_ids: list[int] | None = None) -> dict[int, CopyEntry]`
    - `async def mark_working(self, team_id, copy, worklog_id, footprint: DocumentFootprint) -> None` (upsert, 지문과 스냅샷은 지우고 footprint 저장)
    - `async def put_ready(self, team_id, copy, row: LightWorklogSourceRow) -> None`
    - `async def remove(self, team_id, copy, worklog_id) -> None`
    - `async def clear_copy(self, team_id, copy) -> None`
    - `async def teams_containing(self, worklog_id) -> set[int]`
    - `async def differing_worklog_ids(self, team_id, *, read: TeamCopy) -> list[int]` (쓰기 사본 쪽이 `WORKING`이거나, 한쪽에만 있거나, 지문이 다른 업무일지)
    - `async def close(self) -> None`
  - `def get_worklog_team_copy_store() -> WorklogTeamCopyStore` (모듈 싱글턴)

- [x] **Step 1: 실패하는 테스트 작성**

```python
def test_fingerprint_ignores_tag_order_and_detects_tag_change():
    base = make_row(7, team=3, tags=[tag(1), tag(2)])
    assert snapshot_fingerprint(base) == snapshot_fingerprint(make_row(7, team=3, tags=[tag(2), tag(1)]))
    assert snapshot_fingerprint(base) != snapshot_fingerprint(make_row(7, team=3, tags=[tag(1)]))
    assert snapshot_fingerprint(None) is None

def test_snapshot_json_roundtrip():
    row = make_row(7, team=3, tags=[tag(1)], direct_predecessors=[pred(5, team=3)])
    assert snapshot_from_json(snapshot_to_json(row)) == row

@pg_required   # test_light_worklog_pg_storage_integration.py의 skipif 패턴
async def test_flip_is_conditional(store):
    assert await store.read_copy(3) == "a"
    assert await store.flip_read_copy(3, expected="a", new="b") is True
    assert await store.flip_read_copy(3, expected="a", new="b") is False
    assert await store.read_copy(3) == "b"

@pg_required
async def test_flip_and_mark_other_broken_is_one_update(store):
    assert await store.flip_read_copy(3, expected="a", new="b", mark_other_broken=True) is True
    assert await store.read_copy(3) == "b" and await store.broken_copy(3) == "a"

@pg_required
async def test_working_row_keeps_footprint_and_clears_fingerprint(store):
    fp = DocumentFootprint(frozenset({"chunk-1"}), frozenset({"Tag:5"}), frozenset())
    await store.put_ready(3, "b", row(7)); await store.mark_working(3, "b", 7, fp)
    e = (await store.entries(3, "b", [7]))[7]
    assert e.state == "WORKING" and e.fingerprint is None and e.snapshot is None and e.footprint == fp

@pg_required
async def test_differing_ids_include_working_presence_and_fingerprint(store):
    await store.put_ready(3, "a", row(7)); await store.put_ready(3, "b", row(7))          # 같음
    await store.put_ready(3, "a", row(8)); await store.mark_working(3, "b", 8, empty_fp)  # b WORKING
    await store.put_ready(3, "a", row(9))                                               # b에 없음
    await store.put_ready(3, "a", row(10, title="x")); await store.put_ready(3, "b", row(10, title="y"))
    await store.put_ready(3, "b", row(11))                                              # a에 없음
    assert sorted(await store.differing_worklog_ids(3, read="a")) == [8, 9, 10, 11]
```

- [x] **Step 2: 실패 확인** — Run: `pytest tests/test_light_worklog_team_copy_store.py -q` / Expected: FAIL (`ModuleNotFoundError`)

- [x] **Step 3: 구현** — `settings.lightrag_pg_dsn`으로 SQLAlchemy async 엔진(`postgresql+asyncpg`). 첫 사용 때 `CREATE TABLE IF NOT EXISTS` 두 개: 포인터 `(team_id PK, read_copy CHECK IN ('a','b'), broken_copy NULL CHECK IN ('a','b'))`, 내용 `(team_id, copy, worklog_id, state CHECK IN ('READY','WORKING'), fingerprint NULL, snapshot JSONB NULL, footprint JSONB NULL, PK(team_id, copy, worklog_id))`.

- [x] **Step 4: 통과 확인** — Run: Step 2 명령 / Expected: PASS (PG 환경변수 없으면 PG 테스트는 skip)

- [x] **Step 5: 변경 확인** (커밋하지 않음)

---

### Task 2: Redis 팀 쓰기 락(연장형)과 사본 읽기 토큰

**Files:**
- Create: `ai/app/light/v4/service/worklog_team_copy_lock.py`
- Test: `ai/tests/test_light_worklog_team_copy_lock.py` (새 의존성을 추가하지 않는다. `fakeredis`가 설치돼 있지 않으면 Lua를 쓰는 부분은 실제 Redis가 있을 때만 도는 통합 테스트로, 나머지는 주입한 가짜 클라이언트로 검증)

**Interfaces:**
- Consumes: Task 1 `WorklogTeamCopyStore.read_copy`, `TeamCopy`
- Produces:
  - `class LostWriteLockError(RuntimeError)`
  - `class TeamWriteLease` — `async def ensure_owned(self) -> None` (아니면 `LostWriteLockError`)
  - `class TeamCopyLocks`
    - `__init__(self, *, settings_obj=settings, client: Redis | None = None) -> None`
    - `def team_write_lock(self, team_id: int) -> AsyncContextManager[TeamWriteLease]` — 잡힐 때까지 대기, 만료 `lightrag_insert_timeout_seconds * 2`, 그 1/3 간격으로 백그라운드 연장. 연장 실패 시 이후 `ensure_owned`가 실패. 나갈 때 연장 태스크 취소 후 토큰 비교 삭제
    - `def read_copy(self, team_id: int, store) -> AsyncContextManager[TeamCopy]` — 포인터 읽기 → 토큰 등록(Redis TIME + `lightrag_query_timeout_seconds + 60`) → 포인터 재조회(PG, 캐시 없음) → 바뀌었으면 토큰 지우고 반복. 나갈 때 토큰 삭제
    - `async def wait_readers_drained(self, team_id: int, copy: TeamCopy, *, timeout: float) -> None` — 만료 토큰 정리(Redis TIME) 후 개수 0이 될 때까지, 시간 초과면 `TimeoutError`

- [x] **Step 1: 실패하는 테스트 작성**

```python
async def test_team_write_lock_is_exclusive():
    order = []
    async def job(name):
        async with locks.team_write_lock(3):
            order.append(f"{name}-in"); await asyncio.sleep(0.01); order.append(f"{name}-out")
    await asyncio.gather(job("x"), job("y"))
    assert order in (["x-in", "x-out", "y-in", "y-out"], ["y-in", "y-out", "x-in", "x-out"])

async def test_write_lock_is_renewed_while_held():
    locks = TeamCopyLocks(settings_obj=short_ttl_settings(ttl=0.3), client=redis)
    async with locks.team_write_lock(3) as lease:
        await asyncio.sleep(0.6)                      # TTL보다 오래
        await lease.ensure_owned()                    # 연장 덕에 여전히 소유

async def test_lost_write_lock_is_detected():
    async with locks.team_write_lock(3) as lease:
        await redis.delete(write_lock_key(3))         # 만료 흉내
        with pytest.raises(LostWriteLockError):
            await lease.ensure_owned()

async def test_readers_are_shared_and_drain_waits():
    async with locks.read_copy(3, store) as c1, locks.read_copy(3, store) as c2:
        assert c1 == c2 == "a"
        with pytest.raises(TimeoutError):
            await locks.wait_readers_drained(3, "a", timeout=0.05)
    await locks.wait_readers_drained(3, "a", timeout=0.05)

async def test_reader_rechecks_pointer_after_register():
    store.flip_after_first_read(3, "b")               # 첫 포인터 조회 직후 b로 바뀜
    async with locks.read_copy(3, store) as copy:
        assert copy == "b"
        await locks.wait_readers_drained(3, "a", timeout=0.05)

@redis_required
async def test_expired_reader_token_is_ignored_using_redis_time():
    await register_token_with_expiry_in_past(redis, team=3, copy="a")
    await locks.wait_readers_drained(3, "a", timeout=0.05)
```

- [x] **Step 2: 실패 확인** — Run: `pytest tests/test_light_worklog_team_copy_lock.py -q` / Expected: FAIL

- [x] **Step 3: 구현** — 쓰기 락: `SET key token NX PX`, 연장과 삭제는 "값이 내 토큰일 때만" Lua. 읽기 토큰: 키 `…:readers:{team}:{copy}` 정렬 집합, 등록은 `TIME`으로 만료 시각을 계산해 `ZADD`하는 Lua, 정리와 개수는 `TIME` 기준 `ZREMRANGEBYSCORE` 후 `ZCARD`하는 Lua. 연장 태스크는 컨텍스트 안에서 만들고 나갈 때 반드시 취소한다(`worker_runner`가 실행마다 남은 태스크를 취소하므로 밖에 남기지 않는다).

- [x] **Step 4: 통과 확인** — Run: Step 2 명령 / Expected: PASS

- [x] **Step 5: 변경 확인** (커밋하지 않음)

---

### Task 3: 팀 사본 어댑터, LLM 캐시 공유, 사본에 반영하기

**Files:**
- Modify: `ai/app/light/v4/service/worklog_team_scope.py` (`adapter_for`)
- Modify: `ai/app/light/v4/service/lightrag_adapter.py` (공유 캐시 생성자 인자, `_verify_storage_workspaces` 예외, 문서별 청크/엔티티/관계 목록 조회, 저장소 전체 비우기)
- Create: `ai/app/light/v4/service/worklog_team_copy_apply.py`
- Test: `ai/tests/test_light_worklog_team_scope.py`, `ai/tests/test_light_worklog_team_copy_apply.py`, `ai/tests/test_light_worklog_pg_configuration.py` (workspace 검사 예외)

**Interfaces:**
- Consumes: Task 1 store; 기존 `build_worklog_light_document`, `build_worklog_custom_kg_document`, `to_light_worklog_source`, `to_light_worklog_custom_kg_source`, 어댑터 `index_documents`, `index_custom_kg_documents`, `delete_document`
- Produces:
  - `LightRagTeamScopeAdapter.adapter_for(self, team_id: int, copy: TeamCopy = "a")` — 캐시 키 `(team_id, copy)`. 사본 `b`는 workspace 뒤에 `_b`, `shared_llm_cache_from=adapter_for(team_id, "a")`
  - `LightRagWorklogIndexAdapter.__init__(..., shared_llm_cache_from: "LightRagWorklogIndexAdapter | None" = None)` — 초기화 때 `rag.llm_response_cache`를 그 어댑터의 (감싼) 캐시 저장소로 바꾸고, workspace 검사는 `llm_response_cache`만 그 workspace를 허용
  - `async def LightRagWorklogIndexAdapter.document_footprint(self, document_id: str) -> DocumentFootprint` (Task 1 타입. `doc_status`의 `chunks_list`, `full_entities`, `full_relations`)
  - `async def LightRagWorklogIndexAdapter.find_stale_references(self, footprint: DocumentFootprint) -> list[str]` (그래프 엔티티와 관계의 `source_id`를 `GRAPH_FIELD_SEP`로 나눠 footprint 청크가 남은 항목 이름. 같은 workspace의 다른 문서 `chunks_list`에 있는 청크는 제외. 벡터는 보지 않음, 설계 12절 1번)
  - `async def LightRagWorklogIndexAdapter.drop_all(self) -> None` (그 workspace의 저장소 중 **`llm_response_cache`를 뺀** 나머지 `drop`. 각 반환값의 `status`가 `success`가 아니면 `LightRagDeleteFailedError`)
  - `class CopyBrokenError(RuntimeError)`
  - `async def apply_to_copy(pool, store, *, lease: TeamWriteLease, team_id: int, copy: TeamCopy, worklog_id: int, desired: LightWorklogSourceRow | None) -> None` — 설계 7-6. 시작 전 `lease.ensure_owned()`. 표 행이 이미 `WORKING`이고 footprint가 있으면 그것을 쓰고, 아니면 새로 읽어 `mark_working(..., footprint)`. 5번에서 남은 참조가 있으면 `store.set_broken_copy(team_id, copy)` 후 `CopyBrokenError`
  - `async def rebuild_copy(pool, store, *, lease, team_id: int, copy: TeamCopy, rows: list[LightWorklogSourceRow]) -> None` — `set_broken_copy(team_id, copy)` **먼저** → `drop_all` → `clear_copy` → 각 행 `apply_to_copy` → `set_broken_copy(team_id, None)`

- [x] **Step 1: 실패하는 테스트 작성**

```python
def test_copy_b_workspace_has_suffix_and_a_keeps_current_name():
    assert pool.adapter_for(3, "a").workspace == current_team_workspace_name(3)
    assert pool.adapter_for(3, "b").workspace == current_team_workspace_name(3) + "_b"

def test_workspace_check_allows_only_llm_cache_to_be_shared():
    verify(storages_with(llm_response_cache_ws="team_3", others_ws="team_3_b"), expected="team_3_b",
           shared_cache_ws="team_3")                                   # 통과
    with pytest.raises(LightRagConfigurationError):
        verify(storages_with(text_chunks_ws="team_3", others_ws="team_3_b"), expected="team_3_b",
               shared_cache_ws="team_3")

async def test_apply_marks_working_first_and_ready_last():
    await apply_to_copy(pool, store, lease=lease, team_id=3, copy="b", worklog_id=7, desired=row(7, team=3))
    assert store.log == ["working:3:b:7", "ready:3:b:7"]
    assert fake(3, "b").calls == ["footprint:worklog-7", "delete:worklog-7", "check_stale",
                                  "index:worklog-7", "custom_kg:worklog-7"]
    assert lease.checks == 1

async def test_apply_failure_leaves_working_with_footprint():
    fake(3, "b").fail_on = "index"
    with pytest.raises(Exception):
        await apply_to_copy(pool, store, lease=lease, team_id=3, copy="b", worklog_id=7, desired=row(7, team=3))
    assert store.state(3, "b", 7) == "WORKING" and store.footprint(3, "b", 7) is not None

async def test_apply_reuses_saved_footprint_after_crash():
    store.mark_working(3, "b", 7, fp_with_tag5); fake(3, "b").doc_status_missing = True   # 삭제 직후 죽음
    fake(3, "b").stale_after_delete = ["Tag:5"]
    with pytest.raises(CopyBrokenError):
        await apply_to_copy(pool, store, lease=lease, team_id=3, copy="b", worklog_id=7, desired=row(7, team=3))
    assert "footprint:worklog-7" not in fake(3, "b").calls and store.broken(3) == "b"

async def test_apply_none_removes_row():
    await apply_to_copy(pool, store, lease=lease, team_id=3, copy="b", worklog_id=7, desired=None)
    assert fake(3, "b").calls[-1] == "check_stale" and not store.has(3, "b", 7)

async def test_stale_reference_after_delete_marks_copy_broken():
    fake(3, "b").stale_after_delete = ["Tag:5"]
    with pytest.raises(CopyBrokenError):
        await apply_to_copy(pool, store, lease=lease, team_id=3, copy="b", worklog_id=7, desired=row(7, team=3))
    assert store.broken(3) == "b"

async def test_shared_chunk_used_by_other_document_is_not_stale():
    fake(3, "b").other_docs_chunks = {"chunk-shared"}; fake(3, "b").remaining_refs = {"Tag:5": {"chunk-shared"}}
    await apply_to_copy(pool, store, lease=lease, team_id=3, copy="b", worklog_id=7, desired=None)
    assert store.broken(3) is None

async def test_apply_stops_when_lease_lost():
    lease.lost = True
    with pytest.raises(LostWriteLockError):
        await apply_to_copy(pool, store, lease=lease, team_id=3, copy="b", worklog_id=7, desired=row(7, team=3))
    assert fake(3, "b").calls == []

async def test_rebuild_copy_marks_broken_before_drop_and_clears_after():
    await rebuild_copy(pool, store, lease=lease, team_id=3, copy="b", rows=[row(7, team=3), row(8, team=3)])
    assert events[:2] == ["broken:3:b", "drop_all:3:b"] and store.ready_ids(3, "b") == [7, 8]
    assert store.broken(3) is None

async def test_drop_all_keeps_shared_llm_cache_and_raises_on_error_status():
    adapter = fake_rag_adapter(drop_results={"text_chunks": {"status": "error"}})
    with pytest.raises(LightRagDeleteFailedError):
        await adapter.drop_all()
    assert "llm_response_cache" not in adapter.dropped

async def test_failed_drop_keeps_broken_and_does_not_reapply():
    fake(3, "b").drop_fails = True
    with pytest.raises(LightRagDeleteFailedError):
        await rebuild_copy(pool, store, lease=lease, team_id=3, copy="b", rows=[row(7, team=3)])
    assert store.broken(3) == "b" and not store.has(3, "b", 7)
```

통합 테스트 (환경변수 있을 때만):
- `test_copy_b_insert_hits_shared_llm_cache` — 사본 a에 넣은 문서를 사본 b에 넣을 때 추출 LLM 호출 0회 (설계 11절 1번)
- `test_hidden_rebuild_failure_is_detected` — 기존 `test_light_worklog_custom_kg_tracking.py`의 `native_rag`, `index_body_and_kg`로 1, 2번을 넣고 `Tag:5` 복구 중 `upsert_node` 실패를 주입한 뒤 2번 `apply_to_copy(desired=None)`가 `CopyBrokenError` (설계 11절 2번, Codex 재현 시나리오)
- `test_drop_all_only_clears_own_workspace` — 사본 b `drop_all` 뒤 사본 a와 다른 팀 데이터, **공유 LLM 캐시 행**이 남아 있음 (설계 11절 3번)

- [x] **Step 2: 실패 확인** — Run: `pytest tests/test_light_worklog_team_scope.py tests/test_light_worklog_team_copy_apply.py tests/test_light_worklog_pg_configuration.py -q` / Expected: FAIL

- [x] **Step 3: 구현** — Interfaces대로. `apply_to_copy`의 넣기는 지금 `index_source_rows`의 팀 내 선행 업무 필터 규칙을 그대로 옮긴다.

- [x] **Step 4: 통과 확인** — Run: Step 2 명령 / Expected: PASS

- [x] **Step 5: 변경 확인** (통합 테스트 `test_copy_b_insert_hits_shared_llm_cache`가 실패하면 진행을 멈추고 사용자에게 알린다: 임베딩 외에 LLM 비용도 2배가 됨. 커밋하지 않음)

---

### Task 4: 쓰기 흐름과 색인 서비스 교체

**Files:**
- Create: `ai/app/light/v4/service/worklog_team_copy_writer.py`
- Modify: `ai/app/light/v4/service/worklog_index_service.py`
- Modify: `ai/app/light/v4/service/lightrag_adapter.py` (호출하는 곳이 없어지는 `set_document_indexing_status`, `save_indexed_team_ids` 삭제. `get_processed_worklog_ids`, `get_document_team_id`는 Task 7이 쓰므로 남김)
- Test: `ai/tests/test_light_worklog_team_copy_writer.py`, `ai/tests/test_light_worklog_v4_index_service.py`, `ai/tests/test_light_worklog_v4_index_saga.py`, `ai/tests/test_light_worklog_tasks.py`
- Modify (기대 변경 또는 삭제): `ai/tests/test_light_worklog_pg_ownership.py` (`save_indexed_team_ids` 호출부), `ai/tests/test_light_worklog_v4_completion.py` (`set_document_indexing_status` 호출부와 `global_adapter_factory` 의존)

**Interfaces:**
- Consumes: Task 1 store, Task 2 `team_write_lock`, `TeamWriteLease.ensure_owned`, `wait_readers_drained`, Task 3 `apply_to_copy`, `rebuild_copy`, `CopyBrokenError`
- Produces:
  - `class TeamCopyWriter`
    - `__init__(self, *, pool, store, locks, source_reader, cache: WorklogQueryCache | None, settings_obj=settings) -> None`
    - `async def sync_worklog(self, worklog_id: int) -> None` — 설계 7-5, 7-7
  - `LightWorklogIndexService.__init__(self, *, cache=None, settings_obj=settings, writer: TeamCopyWriter | None = None)` — `adapter_factory`, `team_adapter_factory`, `source_reader` 인자는 없앤다
  - `index_worklogs`, `reindex_worklogs`, `delete_worklogs`의 시그니처와 응답 모델은 그대로. 셋 다 `sync_worklog(id)`를 부른다 (어느 쪽이든 업무 DB 최신 상태로 맞춘다)

- [x] **Step 1: 실패하는 테스트 작성** (가짜 pool은 사본별 문서 집합과 호출 순서, 가짜 locks는 배출 대기와 소유를 기록한다)

```python
async def test_update_applies_write_copy_then_flips_and_leaves_old_copy():
    seed_both(3, row(7, title="v3")); db.set(row(7, team=3, title="v4"))
    await writer.sync_worklog(7)
    assert events == ["drain:3:b", "apply:3:b:7", "flip:3:a->b"]
    assert store.snapshot(3, "b", 7).title == "v4" and store.snapshot(3, "a", 7).title == "v3"

async def test_next_job_cleans_old_copy_to_read_state_first():
    seed_both(3, row(7, title="v3")); db.set(row(7, team=3, title="v4"))
    await writer.sync_worklog(7); events.clear()
    db.set(row(8, team=3))
    await writer.sync_worklog(8)
    assert events[:2] == ["drain:3:a", "restore:3:a:7"] and store.snapshot(3, "a", 7).title == "v4"

async def test_next_job_waits_for_readers_before_cleanup():
    seed_both(3, row(7)); store.set_pointer(3, "b"); locks.readers[(3, "a")] = 1
    db.set(row(8, team=3))
    task = asyncio.create_task(writer.sync_worklog(8)); await asyncio.sleep(0.01)
    assert "restore:3:a:7" not in events and "apply:3:a:8" not in events
    locks.readers[(3, "a")] = 0; await task

async def test_tag_only_change_is_applied():
    seed_both(3, row(7, tags=[])); db.set(row(7, team=3, tags=[tag(5)]))
    await writer.sync_worklog(7)
    assert store.snapshot(3, "b", 7).tags == [tag(5)] and "flip:3:a->b" in events

async def test_same_content_is_noop():
    seed_both(3, row(7)); db.set(row(7, team=3))
    await writer.sync_worklog(7)
    assert "apply:3:b:7" not in events and "flip:3:a->b" not in events

async def test_read_copy_working_row_is_not_skipped():
    seed_both(3, row(7)); store.mark_working(3, "a", 7, empty_fp); db.set(row(7, team=3))
    await writer.sync_worklog(7)
    assert "apply:3:b:7" in events and "flip:3:a->b" in events

async def test_compensation_checks_lease_first():
    seed_both(3, row(7, title="v3")); db.set(row(7, team=3, title="v4"))
    pool.fail_on = ("b", 7, "v4"); locks.lose_after_first_apply = True
    with pytest.raises(Exception):
        await writer.sync_worklog(7)
    assert events.count("apply:3:b:7") == 1                         # 락을 잃었으니 보상하지 않음

async def test_late_old_request_uses_latest_db_row():
    seed_both(3, row(7, title="v5")); db.set(row(7, team=3, title="v5"))
    await writer.sync_worklog(7)                         # v4 시절 요청이 늦게 처리됨
    assert "apply:3:b:7" not in events and store.snapshot(3, "a", 7).title == "v5"

async def test_failed_apply_compensates_and_keeps_pointer():
    seed_both(3, row(7, title="v3")); db.set(row(7, team=3, title="v4")); pool.fail_on = ("b", 7, "v4")
    with pytest.raises(Exception):
        await writer.sync_worklog(7)
    assert store.read_copy_of(3) == "a"
    assert store.state(3, "b", 7) == "READY" and store.snapshot(3, "b", 7).title == "v3"

async def test_working_row_is_restored_from_read_copy_by_next_job():
    seed_both(3, row(7, title="v3")); store.mark_working(3, "b", 7); pool.drop(3, "b", 7)   # 보상도 못 하고 죽음
    db.set(row(7, team=3, title="v4")); db.set(row(8, team=3))
    await writer.sync_worklog(8)
    assert store.snapshot(3, "b", 7).title == "v3"                 # 남의 것은 되돌리기만
    assert pool.has(3, "b", 7) and store.read_copy_of(3) == "b"

async def test_poison_worklog_does_not_block_team():
    seed_both(3, row(7, title="v3")); db.set(row(7, team=3, title="v4")); pool.fail_on = ("*", 7, "v4")
    for _ in range(4):
        with pytest.raises(Exception):
            await writer.sync_worklog(7)
    db.set(row(8, team=3))
    await writer.sync_worklog(8)
    assert store.snapshot(3, store.read_copy_of(3), 7).title == "v3"
    assert store.has(3, store.read_copy_of(3), 8)

async def test_new_worklog_failure_is_cleaned_to_absent():
    db.set(row(9, team=3)); pool.fail_on = ("b", 9, None)
    with pytest.raises(Exception):
        await writer.sync_worklog(9)
    assert not store.has(3, "b", 9) and not pool.has(3, "b", 9)

async def test_broken_copy_is_rebuilt_before_next_change():
    seed_both(3, row(7)); store.set_broken(3, "b"); db.set(row(8, team=3))
    await writer.sync_worklog(8)
    assert events[:2] == ["drain:3:b", "rebuild:3:b"] and store.broken(3) is None

async def test_lost_lock_before_flip_does_not_flip():
    seed_both(3, row(7, title="v3")); db.set(row(7, team=3, title="v4")); locks.lose_before_flip = True
    with pytest.raises(LostWriteLockError):
        await writer.sync_worklog(7)
    assert store.read_copy_of(3) == "a"

async def test_team_move_indexes_new_team_then_removes_old():
    seed_both(3, row(7, team=3)); db.set(row(7, team=4))
    await writer.sync_worklog(7)
    assert events.index("flip:4:a->b") < events.index("flip:3:a->b")
    assert store.has(4, "b", 7) and not store.has(3, "b", 7)
    assert locks.max_concurrent_team_locks == 1

async def test_delete_removes_from_read_copy_after_flip():
    seed_both(3, row(7)); db.delete(7)
    await writer.sync_worklog(7)
    assert not store.has(3, store.read_copy_of(3), 7)

def test_index_service_has_no_global_adapter():
    with pytest.raises(TypeError):
        LightWorklogIndexService(adapter_factory=lambda: object())
```

- [x] **Step 2: 실패 확인** — Run: `pytest tests/test_light_worklog_team_copy_writer.py tests/test_light_worklog_v4_index_service.py tests/test_light_worklog_v4_index_saga.py -q` / Expected: FAIL

- [x] **Step 3: 구현** — 팀 하나 처리 (설계 7-5):

```text
async with locks.team_write_lock(team) as lease:
  read = store.read_copy(team); write = other_copy(read)
  locks.wait_readers_drained(team, write, timeout=배출 최대 대기)
  if store.broken_copy(team) == write:
      rebuild_copy(..., lease=lease, copy=write, rows=[e.snapshot for e in store.entries(team, read).values() if READY])
  else:
      for id in store.differing_worklog_ids(team, read=read):
          r = store.entries(team, read, [id]).get(id)
          apply_to_copy(..., lease=lease, copy=write, worklog_id=id, desired=r.snapshot if r and READY else None)
  lease.ensure_owned()
  current = source_reader.fetch_source_rows([worklog_id]).get(worklog_id)
  desired = current if current and current.team_id == team else None
  have = store.entries(team, read, [worklog_id]).get(worklog_id)
  have_fp = have.fingerprint if have and have.state == "READY" else ("<working>" if have else None)
  if snapshot_fingerprint(desired) == have_fp: return
  try: apply_to_copy(..., lease=lease, copy=write, desired=desired)
  except (CopyBrokenError, LostWriteLockError): raise
  except Exception:
      try: apply_to_copy(..., lease=lease, copy=write,                            # 바로 보상 (안에서 소유 확인)
                         desired=have.snapshot if have and have.state == "READY" else None)
      except Exception: logger.exception(...)                                    # 다음 작업이 청소
      raise
  lease.ensure_owned()
  if not store.flip_read_copy(team, expected=read, new=write): raise LostWriteLockError
  cache.bump_version() (있으면)
```

팀 순서: 락 밖에서 읽은 현재 팀(있으면) 먼저, 그다음 `store.teams_containing(id)`의 나머지. 전사 칸 쓰기, `STAGING`/`COMPLETED`, `_await_compensation`, `_compensate_document`를 지운다. 실패는 기존 에러 코드 상수로 응답 항목에 담는다. 배출 최대 대기는 작업 기한(`worklog_light_index_timeout_seconds`)보다 짧게 둔다.

- [x] **Step 4: 통과 확인** — Run: Step 2 명령 + `pytest tests/test_light_worklog_tasks.py tests/test_light_worklog_pg_ownership.py tests/test_light_worklog_v4_completion.py -q` / Expected: PASS

- [x] **Step 5: 변경 확인** (`rg "set_document_indexing_status|save_indexed_team_ids" ai/app`가 Task 8 전까지 `rebuild_light_worklogs_v4.py`만 남는지. 커밋하지 않음)

---

### Task 5: 챗봇 조회를 읽기 사본 + 스냅샷 + 업무 DB 권한 확인으로 교체

**Files:**
- Modify: `ai/app/light/v4/service/worklog_team_scope.py` (`query_data`, `read_source_rows`, 생성자)
- Modify: `ai/app/light/v4/service/worklog_query_service.py` (`query_worklogs`, `query_worklogs_stream`, `_indexed_context`, `_indexed_context_from_data`)
- Modify: `ai/app/light/v4/model/worklog_query.py` (`allowed_team_ids`)
- Modify: `ai/app/light/v4/service/worklog_query_cache.py` (`scope_key`)
- Test: `ai/tests/test_light_worklog_team_scope.py`, `ai/tests/test_light_worklog_v4_query_service.py`, `ai/tests/test_light_worklog_v4_query_contract.py`, `ai/tests/test_light_worklog_v4_query_safety.py`, `ai/tests/test_light_worklog_v4_query_cache.py`, `ai/tests/test_light_worklogs_v4_query_router.py`
- Modify (기대 변경): `ai/tests/test_light_worklog_team_storage_integration.py` (최신 DB 본문을 기대하는 부분을 읽기 사본 스냅샷 기대로)

**Interfaces:**
- Consumes: Task 1 `entries`, Task 2 `read_copy`, Task 3 `adapter_for(team, copy)`; 기존 `WorklogLightSourceRowReader.fetch_source_rows`
- Produces:
  - `LightRagTeamScopeAdapter.__init__(self, *, settings_obj=settings, adapter_factory=LightRagWorklogIndexAdapter, source_reader=None, store=None, locks=None)` — `global_adapter_factory` 인자는 없앤다
  - `def read_copies(self, team_ids: tuple[int, ...]) -> AsyncContextManager[dict[int, TeamCopy]]`
  - `async def query_data(self, options, copies: dict[int, TeamCopy]) -> dict`
  - `async def read_source_rows(self, worklog_ids: list[int], copies: dict[int, TeamCopy]) -> dict[int, LightWorklogSourceRow]` — (팀, 업무일지)로 스냅샷을 모은 뒤 업무 DB 현재 팀과 같은 것만 남김
  - `WorklogLightQueryRequest.allowed_team_ids: list[PositiveInt] = Field(alias="allowedTeamIds", min_length=1)`
  - `WorklogQueryCache.scope_key(allowed_team_ids: list[int]) -> str`
  - `query_worklogs_stream`은 지금처럼 **async generator**를 반환한다 (라우터 `worklog_query.py`가 `await` 없이 `async for`로 씀). 토큰은 문맥 생성 직후, 첫 조각을 내보내기 전에 해제한다
  - 답변 생성은 `pool.adapter_for(min(copies), copies[min(copies)]).complete_scoped(...)`

- [x] **Step 1: 실패하는 테스트 작성**

```python
async def test_query_searches_only_read_copy():
    store.set_pointer(3, "a")
    await service.query_worklogs(request(allowed=[3]))
    assert pool.queried == [(3, "a")]

async def test_context_uses_read_copy_snapshot_not_current_source():
    store.seed_ready(3, "a", row(7, team=3, title="결제 API 타임아웃"))
    reader.current[7] = row(7, team=3, title="재시도 로직")
    async with pool.read_copies((3,)) as copies:
        assert (await pool.read_source_rows([7], copies))[7].title == "결제 API 타임아웃"

async def test_working_or_absent_is_hidden():
    store.mark_working(3, "a", 7); reader.current[7] = row(7, team=3)
    async with pool.read_copies((3,)) as copies:
        assert await pool.read_source_rows([7, 8], copies) == {}

async def test_deleted_in_source_is_hidden_before_index_delete():
    store.seed_ready(3, "a", row(7, team=3)); reader.current.pop(7, None)
    async with pool.read_copies((3,)) as copies:
        assert await pool.read_source_rows([7], copies) == {}

async def test_team_moved_worklog_is_hidden_from_old_team():
    store.seed_ready(3, "a", row(7, team=3)); reader.current[7] = row(7, team=4)
    async with pool.read_copies((3,)) as copies:
        assert await pool.read_source_rows([7], copies) == {}

@pytest.mark.parametrize("new_team,old_team", [(3, 4), (4, 3)])
async def test_team_move_keeps_current_team_snapshot_for_director(new_team, old_team):
    store.seed_ready(new_team, "a", row(7, team=new_team, title="new"))
    store.seed_ready(old_team, "a", row(7, team=old_team, title="old"))
    reader.current[7] = row(7, team=new_team)
    async with pool.read_copies((3, 4)) as copies:
        assert (await pool.read_source_rows([7], copies))[7].title == "new"

async def test_query_moves_to_new_copy_when_pointer_flips_during_register():
    store.flip_after_first_read(3, "b")
    await service.query_worklogs(request(allowed=[3]))
    assert pool.queried == [(3, "b")]

async def test_reader_token_released_before_llm_call():
    llm.on_call = lambda: held.append(locks.live_readers(3, "a"))
    await service.query_worklogs(request(allowed=[3]))
    assert held == [0]

async def test_stream_is_async_generator_and_token_released_before_first_chunk():
    stream = service.query_worklogs_stream(request(allowed=[3]))
    first = await anext(stream)
    assert locks.live_readers(3, "a") == 0 and first
    await stream.aclose()

def test_query_request_rejects_null_and_empty_team_ids():
    for value in (None, []):
        with pytest.raises(ValidationError):
            WorklogLightQueryRequest.model_validate({"query": "q", "allowedTeamIds": value})

async def test_all_team_query_never_touches_global():
    await service.query_worklogs(request(allowed=[1, 2, 3]))
    assert sorted(t for t, _ in pool.queried) == [1, 2, 3] and global_factory_calls == 0
```

선행 업무: `test_predecessor_uses_its_own_copy_snapshot_and_permission` — 선행 업무 5번이 같은 팀 읽기 사본에 `READY`로 없거나 권한 밖이면 `DEPENDS_ON Worklog:5`가 문맥에 나오지 않는다.

- [x] **Step 2: 실패 확인** — Run: Files의 테스트 전부 / Expected: FAIL

- [x] **Step 3: 구현** — 토큰 등록부터 문맥 생성까지를 `lightrag_query_timeout_seconds` 하나의 기한 안에서 한다. `allowed_team_ids is None` 분기와 전사 `query_data` 호출을 지운다. 기존 예외 변환은 그대로.

- [x] **Step 4: 통과 확인** — Run: Step 2 명령 / Expected: PASS

- [x] **Step 5: 변경 확인** (`rg "get_lightrag_worklog_index_adapter" ai/app/light/v4/service`가 정의 파일 외에는 없어야 한다. 커밋하지 않음)

---

### Task 6: api가 DIRECTOR에게도 전체 팀 목록을 보내기

**Files:**
- Modify: `api/src/main/java/com/ibank/axwms/domain/worklog/service/search/LightRagWorklogSearchService.java` (`resolveAllowedTeamIds`)
- Modify: `api/src/main/java/com/ibank/axwms/domain/worklog/dto/InternalLightRagWorklogQueryApiDto.java` (`null` 허용 제거)
- Test: `api/src/test/java/com/ibank/axwms/domain/worklog/service/search/LightRagWorklogSearchServiceTest.java`

**Interfaces:**
- Produces: AI 서버로 보내는 `allowedTeamIds`는 항상 비어 있지 않은 리스트 (빈 리스트면 지금처럼 AI를 부르지 않음)

- [x] **Step 1: 실패하는 테스트 작성**

```java
@Test
void allScope_sendsAllVisibleTeamIds() {
    given(worklogRepository.findVisibleTeamIds(any(WorklogVisibilityScope.All.class)))
            .willReturn(List.of(1L, 2L, 3L));
    service.search(directorRequest());
    then(aiClient).should().query(argThat(req -> req.allowedTeamIds().equals(List.of(1L, 2L, 3L))));
}

@Test
void allScope_withNoTeams_doesNotCallAi() {
    given(worklogRepository.findVisibleTeamIds(any(WorklogVisibilityScope.All.class))).willReturn(List.of());
    service.search(directorRequest());
    then(aiClient).shouldHaveNoInteractions();
}
```

(테스트 클래스의 기존 mock 이름과 요청 helper를 그대로 쓴다)

- [x] **Step 2: 실패 확인** — Run: `cd api; ./gradlew test --tests "*LightRagWorklogSearchServiceTest"` / Expected: FAIL

- [x] **Step 3: 구현** — `case WorklogVisibilityScope.All ignored -> worklogRepository.findVisibleTeamIds(scope);`. 메서드 Javadoc의 "DIRECTOR는 전사 인덱스를 사용" 문구를 새 동작으로 고친다. DTO `of`의 `null` 분기를 없앤다.

- [x] **Step 4: 통과 확인** — Run: Step 2 명령 / Expected: PASS

- [x] **Step 5: 변경 확인** (커밋하지 않음)

---

### Task 7: 초기화 스크립트

**Files:**
- Create: `ai/app/util/init_worklog_team_copies.py`
- Test: `ai/tests/test_init_worklog_team_copies.py`

**Interfaces:**
- Consumes: Task 1 store, Task 2 `team_write_lock`, Task 3 `rebuild_copy`; 기존 전사 어댑터 `get_processed_worklog_ids(ids)`, `get_document_team_id(doc_id)`, 팀 어댑터 `get_processed_worklog_ids(ids, require_completed=False)`, `WorklogLightSourceRowReader.fetch_source_ids/fetch_source_rows`
- Produces: `async def init_copies(*, reader, outbox_checker, global_adapter, pool, store, locks, batch_size: int) -> dict` → `{"seeded": int, "skipped": int, "removed_after_recheck": int, "rebuilt_teams": int}`; `class OutboxChecker` — `async def pending_count(self) -> int` (`SELECT COUNT(*) FROM tb_worklog_light_outbox WHERE NOT terminal_failed`. 완료된 요청은 Outbox에서 지워지므로 이 수가 대기 요청 수다. `attempts`, `lease_until`, `operation`으로 더 거르지 않는다), `async def worklog_ids_with_rows(self) -> set[int]` (대기든 최종 실패든 행이 있는 업무일지 전부); CLI `python -m app.util.init_worklog_team_copies [--batch-size N] [--seed-only]`

- [x] **Step 1: 실패하는 테스트 작성**

```python
async def test_refuses_when_index_requests_are_pending():
    outbox_checker.pending = 2
    with pytest.raises(SystemExit):
        await init_copies(...)

async def test_seed_copy_a_only_for_completed_matching_team_documents():
    reader.current = {7: row(7, team=3), 8: row(8, team=3), 9: row(9, team=4)}
    global_adapter.completed = {7, 9}; global_adapter.teams = {7: 3, 9: 4}
    pool.processed = {(3, "a"): {7}, (4, "a"): set()}
    result = await init_copies(...)
    assert result["seeded"] == 1 and result["skipped"] == 2
    assert store.state(3, "a", 7) == "READY" and store.snapshot(3, "a", 7) == reader.current[7]

async def test_worklogs_with_outbox_rows_are_not_seeded():
    reader.current = {7: row(7, team=3), 8: row(8, team=3)}
    global_adapter.completed = {7, 8}; global_adapter.teams = {7: 3, 8: 3}; pool.processed = {(3, "a"): {7, 8}}
    outbox_checker.rows = {8}                                       # 최종 실패로 남은 요청
    await init_copies(...)
    assert store.has(3, "a", 7) and not store.has(3, "a", 8)

async def test_worklog_changed_during_seed_is_removed_on_recheck():
    ...  # 7, 8 모두 seed 대상
    outbox_checker.rows_after_seed = {7}                            # seed 중에 7번 수정됨
    result = await init_copies(...)
    assert result["removed_after_recheck"] == 1 and not store.has(3, "a", 7)

async def test_builds_copy_b_from_copy_a_snapshots():
    ...  # 위와 같은 준비
    await init_copies(...)
    assert rebuilt == [(3, "b", [7])] and store.broken(3) is None

async def test_rerun_is_safe():
    await init_copies(...); await init_copies(...)
    assert store.count(3, "a") == 1
```

- [x] **Step 2: 실패 확인** — Run: `pytest tests/test_init_worklog_team_copies.py -q` / Expected: FAIL

- [x] **Step 3: 구현** — 설계 7-12. 시작할 때 `pending_count() > 0`이면 거부. Outbox에 행이 있는 업무일지는 seed하지 않는다. seed가 끝나면 `worklog_ids_with_rows()`를 다시 읽어 그사이 생긴 업무일지의 사본 `a` 행을 지운다. 이미 사본 `a`에 `READY` 행이 있는 업무일지는 덮어쓰지 않는다. 사본 `b`는 팀마다 `team_write_lock` 안에서 `rebuild_copy`(안에서 망가짐을 먼저 표시).

- [x] **Step 4: 통과 확인** — Run: Step 2 명령 / Expected: PASS

- [x] **Step 5: 변경 확인** (커밋하지 않음)

---

### Task 8: 재구축 스크립트 단순화

**Files:**
- Modify (사실상 다시 작성): `ai/app/util/rebuild_light_worklogs_v4.py`
- Test: `ai/tests/test_light_worklog_v4_migration.py` (별도 target, manifest, 동결 검증 테스트는 지우고 새 동작 테스트로 바꿈)

**Interfaces:**
- Consumes: Task 1 store, Task 2 locks, Task 3 `rebuild_copy`; `WorklogLightSourceRowReader`
- Consumes (추가): Task 7 `OutboxChecker.pending_count`
- Produces: `async def rebuild_team(*, team_id: int, reader, pool, store, locks) -> None`; `async def rebuild_all(*, reader, outbox_checker, pool, store, locks, team_ids: list[int]) -> dict` → `{"rebuilt": list[int], "failed": dict[int, str]}`; CLI 인자 `--confirm-real-data-export`(필수), `--confirm-index-worker-stopped`(필수), `--team`(선택, 반복 가능)

- [x] **Step 1: 실패하는 테스트 작성**

```python
async def test_rebuild_refuses_when_index_requests_are_pending():
    outbox_checker.pending = 1
    with pytest.raises(SystemExit):
        await rebuild_all(..., team_ids=[3])

def test_rebuild_requires_worker_stopped_confirmation():
    with pytest.raises(SystemExit):
        parse_args(["--confirm-real-data-export"])

async def test_rebuild_team_rebuilds_both_copies():
    store.set_pointer(3, "a"); reader.team_rows[3] = [row(7, team=3), row(8, team=3)]
    await rebuild_team(team_id=3, reader=reader, pool=pool, store=store, locks=locks)
    assert rebuilt == [(3, "b", [7, 8]), (3, "a", [7, 8])]
    assert store.read_copy_of(3) == "b" and store.broken(3) is None
    assert store_calls.count("flip_and_mark_other_broken") == 1          # 전환과 표시가 한 번에

async def test_rebuild_waits_for_write_copy_readers():
    locks.readers[(3, "b")] = 1
    task = asyncio.create_task(rebuild_team(team_id=3, ...)); await asyncio.sleep(0.01)
    assert rebuilt == []
    locks.readers[(3, "b")] = 0; await task

async def test_rebuild_all_reports_failures_and_continues():
    pool.fail_team = 3
    result = await rebuild_all(..., team_ids=[3, 4])
    assert list(result["failed"]) == [3] and result["rebuilt"] == [4]

def test_rebuild_requires_export_confirmation():
    with pytest.raises(SystemExit):
        parse_args([])
```

- [x] **Step 2: 실패 확인** — Run: `pytest tests/test_light_worklog_v4_migration.py -q` / Expected: FAIL

- [x] **Step 3: 구현** — 설계 7-14. `rebuild_all`은 시작할 때 `pending_count() > 0`이면 거부. `rebuild_team`은 `team_write_lock` → 쓰기 사본 배출 확인 → 업무 DB에서 그 팀 생존 업무일지 전부를 락 안에서 읽어 `rebuild_copy` → `ensure_owned` → `flip_read_copy(..., mark_other_broken=True)` → 반대쪽 배출 확인 → 새 읽기 사본의 `READY` 스냅샷으로 반대쪽 `rebuild_copy`. 실패가 있으면 종료 코드 2.

- [x] **Step 4: 통과 확인** — Run: Step 2 명령 / Expected: PASS

- [x] **Step 5: 변경 확인** (`rg "set_document_indexing_status|save_indexed_team_ids|target_workspace" ai/app`가 비어 있어야 한다. 커밋하지 않음)

---

### Task 9: 기존 전사 인덱스 정리 스크립트

**Files:**
- Create: `ai/app/util/delete_global_worklog_index.py`
- Test: `ai/tests/test_delete_global_worklog_index.py`

**Interfaces:**
- Consumes: 기존 전사 `LightRagWorklogIndexAdapter.delete_document(document_id)`, 전사 workspace `doc_status`의 문서 ID 목록
- Produces: `async def delete_global_index(*, global_adapter, document_ids: list[str]) -> dict` → `{"deleted": int, "failed": list[str]}`; `def assert_global_workspace(name: str) -> None`; CLI는 `--confirm-delete-global-index` 없이 실행되지 않음

- [x] **Step 1: 실패하는 테스트 작성**

```python
async def test_deletes_every_global_document_and_reports_failures():
    global_adapter.fail = {"worklog-8"}
    result = await delete_global_index(global_adapter=global_adapter, document_ids=["worklog-7", "worklog-8"])
    assert result == {"deleted": 1, "failed": ["worklog-8"]}

def test_refuses_team_workspace():
    with pytest.raises(ValueError):
        assert_global_workspace("worklog_scope_v1_abc_team_3")

def test_requires_explicit_confirmation():
    with pytest.raises(SystemExit):
        parse_args([])
```

- [x] **Step 2: 실패 확인** — Run: `pytest tests/test_delete_global_worklog_index.py -q` / Expected: FAIL

- [x] **Step 3: 구현** — 전사 workspace(`settings.lightrag_workspace`)의 `doc_status`에서 `worklog-` 문서 ID를 모두 읽어 하나씩 `delete_document`. workspace 이름이 `worklog_scope_`로 시작하면 거부한다.

- [x] **Step 4: 통과 확인** — Run: Step 2 명령 / Expected: PASS

- [x] **Step 5: 전체 확인**

Run: `cd ai; .\.venv\Scripts\python.exe -m pytest tests -q` 그리고 `cd api; ./gradlew test`
Expected: 전부 PASS (PG, Redis, 외부 서비스가 필요한 테스트는 기존처럼 skip)

Run: `rg "get_lightrag_worklog_index_adapter" ai/app`
Expected: `lightrag_adapter.py`(정의), `init_worklog_team_copies.py`, `delete_global_worklog_index.py`만 나온다

---

## 실행 배정 (사용자 확정 2026-11-17)

| Task | 구현 | 검토 |
|---|---|---|
| 1, 2, 3, 4, 5, 7, 8, 9 | Codex `gpt-6.1-sol` / `high` | Claude Opus |
| 6 | Codex `gpt-6.1-terra` / `high` | Claude Opus |

순서: 1 → 2 → 3 → 4 → 5 → (6, 7, 8, 9는 서로 독립이라 병렬 가능). Task 3의 LLM 캐시 공유 통합 테스트가 실패하면 진행을 멈추고 사용자에게 알린다. Task 2, 3, 4, 5가 고위험 구간이라 각각 끝날 때마다 검토한다.
