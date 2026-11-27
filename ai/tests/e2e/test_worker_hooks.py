import asyncio
import importlib.util
import json
from pathlib import Path

import pytest


def load_hooks():
    spec = importlib.util.spec_from_file_location("e2e_worker_hooks", Path(__file__).with_name("worker_hooks.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_barrier_waits_only_when_armed_and_times_out():
    hooks = load_hooks()

    class Redis:
        def __init__(self, armed, released):
            self.armed, self.released, self.events = armed, released, []

        async def get(self, key):
            return self.armed

        async def rpush(self, key, value):
            self.events.append((key, value))

        async def blpop(self, key, timeout):
            self.events.append((key, timeout))
            return self.released

    async def check():
        inactive = Redis(None, None)
        await hooks.barrier(inactive, "prepared", 7)
        assert inactive.events == []
        active = Redis(b"1", (b"key", b"release"))
        await hooks.barrier(active, "prepared", 7)
        assert active.events == [("e2e:prepared:7:arrived", "1"), ("e2e:prepared:7:release", 60)]
        with pytest.raises(TimeoutError):
            await hooks.barrier(Redis(b"1", None), "callback", 7)

    asyncio.run(check())


def test_provider_stub_handles_index_keyword_and_answer_modes():
    hooks = load_hooks()

    async def check():
        keywords = json.loads(await hooks.fake_llm("fixture", keyword_extraction=True))
        assert keywords["high_level_keywords"] and keywords["low_level_keywords"]
        assert await hooks.fake_llm("fixture", answer_generation=True) == "E2E deterministic answer"
        assert await hooks.fake_llm("fixture") == "<|COMPLETE|>"

    asyncio.run(check())


def test_install_barrier_precedes_locked_team_sync(monkeypatch):
    from redis import asyncio as redis_module
    from app.light.v4.service import lightrag_adapter
    from app.light.v4.service.worklog_team_copy_writer import TeamCopyWriter
    from app.task import light_worklog_tasks

    hooks = load_hooks()
    events = []

    async def original_sync(self, team_id, worklog_id):
        events.append(("sync", team_id, worklog_id))

    async def record_barrier(redis, phase, worklog_id):
        events.append((phase, worklog_id))

    monkeypatch.setenv("WORKLOG_NORMAL_E2E_ENABLED", "1")
    monkeypatch.setenv("CELERY_BROKER_URL", "redis://redis:6379/0")
    monkeypatch.setattr(redis_module, "from_url", lambda url: object())
    monkeypatch.setattr(hooks, "barrier", record_barrier)
    monkeypatch.setattr(TeamCopyWriter, "_sync_team", original_sync)
    # install이 교체하는 속성을 monkeypatch가 테스트 종료 시 되돌린다.
    for obj, name in [
        (light_worklog_tasks, "_persist_and_report"),
        (light_worklog_tasks.WorklogIndexClient, "report_start"),
        (lightrag_adapter, "_build_gemini_embedding_func"),
        (lightrag_adapter, "_build_gemini_llm_model_func"),
        (lightrag_adapter.LightRagWorklogIndexAdapter, "_get_initialized_rag"),
    ]:
        monkeypatch.setattr(obj, name, getattr(obj, name))
    hooks.install()
    writer = object.__new__(TeamCopyWriter)
    asyncio.run(writer._sync_team(3, 7))
    assert events == [("prepared", 7), ("sync", 3, 7)]
