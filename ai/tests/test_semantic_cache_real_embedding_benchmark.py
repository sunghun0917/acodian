"""LightRAG v4 응답 캐시 비활성화 시 벤치마크 차단 검증."""

import asyncio
import socket

import pytest

from benchmarks import semantic_cache_real_embedding as benchmark


@pytest.mark.parametrize("entry", ["server", "run"])
def test_disabled_response_cache_benchmark_stops_before_external_calls(monkeypatch, entry):
    def unexpected(*args, **kwargs):
        raise AssertionError("disabled benchmark must not contact external services")

    monkeypatch.setattr(socket, "create_connection", unexpected)
    with pytest.raises(RuntimeError, match="response cache.*disabled"):
        if entry == "server":
            benchmark.make_server()
        else:
            asyncio.run(benchmark.run())
