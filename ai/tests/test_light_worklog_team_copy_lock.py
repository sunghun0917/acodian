"""팀 쓰기 임대와 사본 읽기 토큰의 수명 및 취소 경계."""

import asyncio
from contextlib import asynccontextmanager
import os
from types import SimpleNamespace
from uuid import uuid4

import pytest
from redis.asyncio import Redis
from redis.exceptions import ConnectionError

from app.light.v4.service import worklog_team_copy_lock as locking
from app.light.v4.service.worklog_team_copy_lock import (
    LostWriteLockError, TeamCopyLocks,
)


def lock_settings(ttl=0.3, query_timeout=0.1):
    return SimpleNamespace(
        lightrag_insert_timeout_seconds=ttl / 2,
        lightrag_query_timeout_seconds=query_timeout,
        redis_url="redis://unused.invalid:6379/0",
    )


class PointerStore:
    def __init__(self, *, flip=False, fail=False):
        self.reads = 0
        self.flip = flip
        self.fail = fail

    async def read_copy(self, team_id):
        self.reads += 1
        if self.fail and self.reads == 2:
            raise RuntimeError("pointer unavailable")
        return "b" if self.flip and self.reads > 1 else "a"


class FakeRedis:
    """Lua 자체는 검증하지 않고 Redis 경계의 결과만 주입한다."""

    def __init__(self):
        self.values = {}
        self.readers = {}
        self.renew_failure = False
        self.renewed = asyncio.Event()
        self.renewals = 0

    async def set(self, key, token, *, nx, px):
        assert nx and px > 0
        now = asyncio.get_running_loop().time()
        if key in self.values and self.values[key][1] > now:
            return None
        self.values[key] = (token, now + px / 1000)
        return True

    async def get(self, key):
        value, expiry = self.values.get(key, (None, 0))
        return value.encode() if expiry > asyncio.get_running_loop().time() else None

    async def eval(self, script, numkeys, key, *args):
        assert numkeys == 1
        if script == locking._RENEW_WRITE_LOCK:
            self.renewals += 1
            self.renewed.set()
            if self.renew_failure:
                raise ConnectionError("renewal unavailable")
            if await self.get(key) != args[0].encode():
                return 0
            self.values[key] = (args[0], asyncio.get_running_loop().time() + args[1] / 1000)
            return 1
        if script == locking._RELEASE_WRITE_LOCK:
            if await self.get(key) == args[0].encode():
                del self.values[key]
                return 1
            return 0
        if script == locking._REGISTER_READER:
            self.readers.setdefault(key, set()).add(args[0])
            assert args[1] == 60100
            return 1
        if script == locking._COUNT_READERS:
            return len(self.readers.get(key, set()))
        raise AssertionError("unexpected script")

    async def zrem(self, key, token):
        self.readers.get(key, set()).discard(token)


@asynccontextmanager
async def backend(real):
    if real:
        client = Redis.from_url(os.environ["RAG_SCOPE_TEST_REDIS_URL"])
        team = uuid4().int
    else:
        client = FakeRedis()
        team = 3
    try:
        yield client, team
    finally:
        if real:
            await client.delete(write_key(team), reader_key(team, "a"), reader_key(team, "b"))
            await client.aclose()


redis_required = pytest.mark.skipif(
    not os.environ.get("RAG_SCOPE_TEST_REDIS_URL"),
    reason="requires explicit RAG_SCOPE_TEST_REDIS_URL for Redis Lua integration",
)
backends = pytest.mark.parametrize("real", [False, pytest.param(True, marks=redis_required)])


def write_key(team):
    return f"worklog:team-copy:v1:write:{team}"


def reader_key(team, copy):
    return f"worklog:team-copy:v1:readers:{team}:{copy}"


@backends
def test_team_write_lock_is_exclusive(real):
    async def run():
        async with backend(real) as (client, team):
            first = TeamCopyLocks(settings_obj=lock_settings(), client=client)
            second = TeamCopyLocks(settings_obj=lock_settings(), client=client)
            order = []

            async def job(locks, name):
                async with locks.team_write_lock(team):
                    order.append(f"{name}-in")
                    await asyncio.sleep(0.01)
                    order.append(f"{name}-out")

            await asyncio.gather(job(first, "x"), job(second, "y"))
            assert order in (["x-in", "x-out", "y-in", "y-out"],
                             ["y-in", "y-out", "x-in", "x-out"])
    asyncio.run(run())


@backends
def test_write_lock_is_renewed_while_held(real):
    async def run():
        async with backend(real) as (client, team):
            locks = TeamCopyLocks(settings_obj=lock_settings(), client=client)
            async with locks.team_write_lock(team) as lease:
                await asyncio.sleep(0.6)
                await lease.ensure_owned()
            assert await client.get(write_key(team)) is None
    asyncio.run(run())


@backends
def test_lost_write_lock_is_detected_and_does_not_delete_successor(real):
    async def run():
        async with backend(real) as (client, team):
            locks = TeamCopyLocks(settings_obj=lock_settings(), client=client)
            async with locks.team_write_lock(team) as lease:
                if real:
                    await client.set(write_key(team), "successor", px=1000)
                else:
                    client.values[write_key(team)] = (
                        "successor", asyncio.get_running_loop().time() + 1,
                    )
                with pytest.raises(LostWriteLockError):
                    await lease.ensure_owned()
            assert await client.get(write_key(team)) == b"successor"
    asyncio.run(run())


@backends
def test_readers_are_shared_and_drain_waits(real):
    async def run():
        async with backend(real) as (client, team):
            locks = TeamCopyLocks(settings_obj=lock_settings(), client=client)
            async with locks.read_copy(team, PointerStore()) as c1:
                async with locks.read_copy(team, PointerStore()) as c2:
                    assert c1 == c2 == "a"
                    with pytest.raises(TimeoutError):
                        await locks.wait_readers_drained(team, "a", timeout=0.02)
                    await locks.wait_readers_drained(team, "b", timeout=0.02)
                with pytest.raises(TimeoutError):
                    await locks.wait_readers_drained(team, "a", timeout=0.02)
            await locks.wait_readers_drained(team, "a", timeout=0.02)
    asyncio.run(run())


@backends
def test_reader_rechecks_pointer_after_register(real):
    async def run():
        async with backend(real) as (client, team):
            locks = TeamCopyLocks(settings_obj=lock_settings(), client=client)

            class CheckedPointerStore(PointerStore):
                async def read_copy(self, team_id):
                    if self.reads in (1, 3):
                        key = reader_key(team_id, "a" if self.reads == 1 else "b")
                        count = await client.zcard(key) if real else len(client.readers.get(key, set()))
                        assert count == 1, "pointer recheck must follow token registration"
                    return await super().read_copy(team_id)

            async with locks.read_copy(team, CheckedPointerStore(flip=True)) as copy:
                assert copy == "b"
                await locks.wait_readers_drained(team, "a", timeout=0.02)
                with pytest.raises(TimeoutError):
                    await locks.wait_readers_drained(team, "b", timeout=0.02)
            await locks.wait_readers_drained(team, "b", timeout=0.02)
    asyncio.run(run())


def test_renewal_failure_permanently_invalidates_lease():
    async def run():
        client = FakeRedis()
        client.renew_failure = True
        locks = TeamCopyLocks(settings_obj=lock_settings(ttl=0.06), client=client)
        async with locks.team_write_lock(3) as lease:
            await asyncio.wait_for(client.renewed.wait(), 1)
            with pytest.raises(LostWriteLockError):
                await lease.ensure_owned()
            client.renew_failure = False
            with pytest.raises(LostWriteLockError):
                await lease.ensure_owned()
    asyncio.run(run())


@backends
def test_missing_lock_stops_renewal_and_invalidates_lease(real):
    async def run():
        async with backend(real) as (client, team):
            locks = TeamCopyLocks(settings_obj=lock_settings(ttl=0.06), client=client)
            async with locks.team_write_lock(team) as lease:
                if real:
                    await client.delete(write_key(team))
                else:
                    del client.values[write_key(team)]
                await asyncio.sleep(0.04)
                with pytest.raises(LostWriteLockError):
                    await lease.ensure_owned()
    asyncio.run(run())


def test_cancelled_waiting_writer_does_not_release_current_owner():
    async def run():
        client = FakeRedis()
        locks = TeamCopyLocks(settings_obj=lock_settings(), client=client)
        async with locks.team_write_lock(3) as owner:
            async def waiter():
                async with locks.team_write_lock(3):
                    pytest.fail("must not enter an owned lock")

            task = asyncio.create_task(waiter())
            await asyncio.sleep(0.01)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await owner.ensure_owned()
    asyncio.run(run())


def test_reader_registration_response_failure_still_cleans_token():
    async def run():
        client = FakeRedis()
        original_eval = client.eval

        async def lost_response(script, *args):
            value = await original_eval(script, *args)
            if script == locking._REGISTER_READER:
                raise ConnectionError("registration response lost")
            return value

        client.eval = lost_response
        locks = TeamCopyLocks(settings_obj=lock_settings(), client=client)
        with pytest.raises(ConnectionError):
            async with locks.read_copy(3, PointerStore()):
                pytest.fail("must not read without confirmed registration")
        await locks.wait_readers_drained(3, "a", timeout=0.02)
    asyncio.run(run())


def test_ownership_check_connection_failure_is_lost_lock():
    async def run():
        client = FakeRedis()
        locks = TeamCopyLocks(settings_obj=lock_settings(), client=client)
        async with locks.team_write_lock(3) as lease:
            original_get = client.get

            async def unavailable(key):
                raise ConnectionError("ownership unavailable")

            client.get = unavailable
            with pytest.raises(LostWriteLockError):
                await lease.ensure_owned()
            client.get = original_get
            with pytest.raises(LostWriteLockError):
                await lease.ensure_owned()
    asyncio.run(run())


def test_cancelled_writer_stops_renewal_and_releases_lock():
    async def run():
        client = FakeRedis()
        locks = TeamCopyLocks(settings_obj=lock_settings(ttl=0.06), client=client)
        entered = asyncio.Event()

        async def job():
            async with locks.team_write_lock(3):
                entered.set()
                await asyncio.Event().wait()

        task = asyncio.create_task(job())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        count = client.renewals
        await asyncio.sleep(0.04)
        assert client.renewals == count
        assert await client.get(write_key(3)) is None
        async with locks.team_write_lock(3) as lease:
            await lease.ensure_owned()
    asyncio.run(run())


def test_exception_releases_reader_and_pointer_failure_cleans_registered_token():
    async def run():
        client = FakeRedis()
        locks = TeamCopyLocks(settings_obj=lock_settings(), client=client)
        with pytest.raises(RuntimeError):
            async with locks.read_copy(3, PointerStore()):
                raise RuntimeError("query failed")
        await locks.wait_readers_drained(3, "a", timeout=0.02)
        with pytest.raises(RuntimeError, match="pointer unavailable"):
            async with locks.read_copy(3, PointerStore(fail=True)):
                pytest.fail("must not enter before pointer recheck")
        await locks.wait_readers_drained(3, "a", timeout=0.02)
    asyncio.run(run())


def test_cancelled_reader_releases_token():
    async def run():
        client = FakeRedis()
        locks = TeamCopyLocks(settings_obj=lock_settings(), client=client)
        entered = asyncio.Event()

        async def job():
            async with locks.read_copy(3, PointerStore()):
                entered.set()
                await asyncio.Event().wait()

        task = asyncio.create_task(job())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await locks.wait_readers_drained(3, "a", timeout=0.02)
    asyncio.run(run())


@redis_required
def test_expired_reader_token_is_ignored_using_redis_time():
    async def run():
        async with backend(True) as (client, team):
            locks = TeamCopyLocks(settings_obj=lock_settings(), client=client)
            seconds, micros = await client.time()
            now_ms = seconds * 1000 + micros / 1000
            await client.zadd(reader_key(team, "a"), {"expired": now_ms - 1000})
            await locks.wait_readers_drained(team, "a", timeout=0.1)
            assert await client.zcard(reader_key(team, "a")) == 0
            async with locks.read_copy(team, PointerStore()):
                score = (await client.zrange(reader_key(team, "a"), 0, -1, withscores=True))[0][1]
                seconds, micros = await client.time()
                remaining = score - (seconds * 1000 + micros / 1000)
                assert 59000 < remaining <= 60100
    asyncio.run(run())
