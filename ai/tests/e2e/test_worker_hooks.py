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
