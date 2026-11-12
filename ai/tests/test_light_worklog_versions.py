from importlib import import_module
from importlib.util import find_spec

from app.router import api_router


def test_only_v4_worklog_implementation_remains() -> None:
    v4 = import_module("app.light.v4.service.worklog_index_service")

    assert v4.LightWorklogIndexService.__module__.startswith("app.light.v4.")
    # v3는 삭제됐다. 차이 기록: docs/ai/lightrag-v3-v4-differences.md
    assert find_spec("app.light.v3") is None


def test_only_v4_worklog_routes_are_registered() -> None:
    paths = {route.path for route in api_router.routes}

    assert "/light/worklogs-v4/query" in paths
    assert "/light/worklogs-v4/index" in paths
    assert "/light/worklogs-v3/query" not in paths
    assert "/light/worklogs-v3/index" not in paths
