from importlib import import_module

from app.router import api_router


def test_v3_and_v4_worklog_implementations_are_preserved_separately() -> None:
    v3 = import_module("app.light.v3.service.worklog_index_service")
    v4 = import_module("app.light.v4.service.worklog_index_service")

    assert v3.LightWorklogIndexService is not v4.LightWorklogIndexService
    assert v3.LightWorklogIndexService.__module__.startswith("app.light.v3.")
    assert v4.LightWorklogIndexService.__module__.startswith("app.light.v4.")


def test_only_v4_worklog_routes_are_registered() -> None:
    paths = {route.path for route in api_router.routes}

    assert "/light/worklogs-v4/query" in paths
    assert "/light/worklogs-v4/index" in paths
    assert "/light/worklogs-v3/query" not in paths
    assert "/light/worklogs-v3/index" not in paths
