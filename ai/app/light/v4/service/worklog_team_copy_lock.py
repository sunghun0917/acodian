"""팀별 연장형 쓰기 잠금과 Redis 시계 기반 사본 읽기 토큰."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from uuid import uuid4

from redis.asyncio import Redis

from app.config.settings import Settings, settings
from app.light.v4.store.worklog_team_copy_store import TeamCopy, WorklogTeamCopyStore


_PREFIX = "worklog:team-copy:v1"
_RENEW_WRITE_LOCK = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('PEXPIRE', KEYS[1], ARGV[2])
end
return 0
"""
_RELEASE_WRITE_LOCK = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""
_REGISTER_READER = """
local now = redis.call('TIME')
local now_ms = tonumber(now[1]) * 1000 + tonumber(now[2]) / 1000
return redis.call('ZADD', KEYS[1], now_ms + tonumber(ARGV[2]), ARGV[1])
"""
_COUNT_READERS = """
local now = redis.call('TIME')
local now_ms = tonumber(now[1]) * 1000 + tonumber(now[2]) / 1000
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now_ms)
return redis.call('ZCARD', KEYS[1])
"""


class LostWriteLockError(RuntimeError):
    """쓰기 임대가 만료되었거나 소유를 더 이상 확인할 수 없다."""


class TeamWriteLease:
    """연장 실패 이후에는 다시 유효해지지 않는 팀 쓰기 임대."""

    def __init__(self, client: Redis, key: str, token: str) -> None:
        self._redis = client
        self._key = key
        self._token = token
        self._lost = False

    async def ensure_owned(self) -> None:
        """실제 Redis 소유 토큰을 확인하고 잃은 임대의 사용을 막는다."""
        if self._lost:
            raise LostWriteLockError("Team write lease was lost")
        try:
            value = await self._redis.get(self._key)
        except Exception as exc:
            self._lost = True
            raise LostWriteLockError("Cannot verify team write lease") from exc
        if self._lost or value not in (self._token, self._token.encode()):
            self._lost = True
            raise LostWriteLockError("Team write lease was lost")

    async def _renew(self, ttl_ms: int) -> None:
        try:
            while True:
                await asyncio.sleep(ttl_ms / 3000)
                if not await self._redis.eval(
                    _RENEW_WRITE_LOCK, 1, self._key, self._token, ttl_ms,
                ):
                    return
        except Exception:
            # 통신이 회복돼도 소유를 잃었을 가능성이 있는 임대는 재사용하지 않는다.
            return
        finally:
            self._lost = True


class TeamCopyLocks:
    """여러 프로세스 사이의 팀 쓰기와 사본 읽기 배출을 조율한다."""

    def __init__(
        self, *, settings_obj: Settings = settings, client: Redis | None = None,
    ) -> None:
        self._redis = client if client is not None else Redis.from_url(settings_obj.redis_url)
        self._write_ttl_ms = int(settings_obj.lightrag_insert_timeout_seconds * 2000)
        self._reader_ttl_ms = int((settings_obj.lightrag_query_timeout_seconds + 60) * 1000)

    @asynccontextmanager
    async def team_write_lock(self, team_id: int) -> AsyncIterator[TeamWriteLease]:
        """팀 쓰기를 직렬화하고 컨텍스트가 끝날 때 연장과 임대를 정리한다."""
        key = f"{_PREFIX}:write:{team_id}"
        token = uuid4().hex
        while not await self._redis.set(key, token, nx=True, px=self._write_ttl_ms):
            await asyncio.sleep(min(0.05, self._write_ttl_ms / 3000))
        lease = TeamWriteLease(self._redis, key, token)
        renewal = asyncio.create_task(lease._renew(self._write_ttl_ms))
        try:
            yield lease
        finally:
            renewal.cancel()
            with suppress(asyncio.CancelledError):
                await renewal
            await self._redis.eval(_RELEASE_WRITE_LOCK, 1, key, token)

    @asynccontextmanager
    async def read_copy(
        self, team_id: int, store: WorklogTeamCopyStore,
    ) -> AsyncIterator[TeamCopy]:
        """토큰 등록 뒤 포인터를 다시 읽어 안정된 사본만 빌려준다."""
        while True:
            copy = await store.read_copy(team_id)
            key = f"{_PREFIX}:readers:{team_id}:{copy}"
            token = uuid4().hex
            try:
                await self._redis.eval(_REGISTER_READER, 1, key, token, self._reader_ttl_ms)
                if await store.read_copy(team_id) == copy:
                    yield copy
                    return
            finally:
                await self._redis.zrem(key, token)

    async def wait_readers_drained(
        self, team_id: int, copy: TeamCopy, *, timeout: float,
    ) -> None:
        """Redis 시계로 만료 토큰을 치우며 사본의 독자가 없어질 때까지 기다린다."""
        key = f"{_PREFIX}:readers:{team_id}:{copy}"
        async with asyncio.timeout(timeout):
            while await self._redis.eval(_COUNT_READERS, 1, key):
                await asyncio.sleep(0.05)
