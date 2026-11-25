"""전사 문서 정리의 명시적 승인과 팀 workspace 보호를 검증한다."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.util import delete_global_worklog_index as cleanup


def make_adapter():
    return SimpleNamespace(
        list_document_ids=AsyncMock(return_value=[]),
        delete_document=AsyncMock(),
        close=AsyncMock(),
    )


def test_deletes_every_global_document_and_reports_failures():
    adapter = make_adapter()

    async def delete(document_id):
        if document_id == "worklog-8":
            raise RuntimeError("delete failed")

    adapter.delete_document.side_effect = delete
    result = asyncio.run(cleanup.delete_global_index(
        global_adapter=adapter, document_ids=["worklog-7", "worklog-8", "worklog-9"],
    ))

    assert result == {"deleted": 2, "failed": ["worklog-8"]}
    assert [call.args[0] for call in adapter.delete_document.await_args_list] == [
        "worklog-7", "worklog-8", "worklog-9",
    ]


@pytest.mark.parametrize("name", [
    "worklog_scope_v1_abc_team_3", "worklog_scope_v1_abc_team_3_b",
    " worklog_scope_v1_abc_team_3 ", "", "  ",
])
def test_refuses_team_or_empty_workspace(name):
    with pytest.raises(ValueError):
        cleanup.assert_global_workspace(name)


def test_accepts_explicit_global_workspace():
    cleanup.assert_global_workspace("axwms_pg_v1")


def test_requires_explicit_confirmation():
    with pytest.raises(SystemExit):
        cleanup.parse_args([])
    assert cleanup.parse_args(["--confirm-delete-global-index"]).confirm_delete_global_index


def test_delete_refuses_team_workspace_before_any_deletion(monkeypatch):
    adapter = make_adapter()
    monkeypatch.setattr(cleanup.settings, "lightrag_workspace", "worklog_scope_v1_abc_team_3_b")

    with pytest.raises(ValueError):
        asyncio.run(cleanup.delete_global_index(global_adapter=adapter, document_ids=["worklog-7"]))
    adapter.delete_document.assert_not_awaited()


def test_delete_rejects_non_worklog_document_before_any_deletion():
    adapter = make_adapter()
    with pytest.raises(ValueError):
        asyncio.run(cleanup.delete_global_index(
            global_adapter=adapter, document_ids=["worklog-7", "file-9"],
        ))
    adapter.delete_document.assert_not_awaited()


def test_empty_global_index_is_safe():
    adapter = make_adapter()
    assert asyncio.run(cleanup.delete_global_index(global_adapter=adapter, document_ids=[])) == {
        "deleted": 0, "failed": [],
    }
    adapter.delete_document.assert_not_awaited()


def test_main_requires_confirmation_before_constructing_adapter(monkeypatch):
    calls = []
    monkeypatch.setattr(cleanup, "get_lightrag_worklog_index_adapter", lambda: calls.append(True))
    with pytest.raises(SystemExit):
        asyncio.run(cleanup.main([]))
    assert calls == []


def test_main_refuses_configured_team_before_constructing_adapter(monkeypatch):
    calls = []
    monkeypatch.setattr(cleanup.settings, "lightrag_workspace", "worklog_scope_v1_abc_team_3")
    monkeypatch.setattr(cleanup, "get_lightrag_worklog_index_adapter", lambda: calls.append(True))
    with pytest.raises(ValueError):
        asyncio.run(cleanup.main(["--confirm-delete-global-index"]))
    assert calls == []


def test_main_lists_documents_before_deletion_and_skips_other_documents(monkeypatch, capsys):
    adapter = make_adapter()
    adapter.list_document_ids.return_value = ["worklog-7", "file-9", "worklog-8"]

    async def delete(document_id):
        adapter.list_document_ids.assert_awaited_once()

    adapter.delete_document.side_effect = delete
    monkeypatch.setattr(cleanup.settings, "lightrag_workspace", "axwms_pg_v1")
    monkeypatch.setattr(cleanup, "get_lightrag_worklog_index_adapter", lambda: adapter)

    assert asyncio.run(cleanup.main(["--confirm-delete-global-index"])) == 0
    assert json.loads(capsys.readouterr().out) == {"deleted": 2, "failed": []}
    assert [call.args[0] for call in adapter.delete_document.await_args_list] == ["worklog-7", "worklog-8"]
    adapter.close.assert_awaited_once()


def test_main_returns_failure_exit_code_and_closes_adapter(monkeypatch, capsys):
    adapter = make_adapter()
    adapter.list_document_ids.return_value = ["worklog-7"]
    adapter.delete_document.side_effect = RuntimeError("delete failed")
    monkeypatch.setattr(cleanup.settings, "lightrag_workspace", "axwms_pg_v1")
    monkeypatch.setattr(cleanup, "get_lightrag_worklog_index_adapter", lambda: adapter)

    assert asyncio.run(cleanup.main(["--confirm-delete-global-index"])) == 2
    assert json.loads(capsys.readouterr().out) == {"deleted": 0, "failed": ["worklog-7"]}
    adapter.close.assert_awaited_once()


def test_main_closes_adapter_and_deletes_nothing_when_listing_fails(monkeypatch):
    adapter = make_adapter()
    adapter.list_document_ids.side_effect = RuntimeError(
        "listing failed",
    )
    monkeypatch.setattr(cleanup.settings, "lightrag_workspace", "axwms_pg_v1")
    monkeypatch.setattr(cleanup, "get_lightrag_worklog_index_adapter", lambda: adapter)

    with pytest.raises(RuntimeError, match="listing failed"):
        asyncio.run(cleanup.main(["--confirm-delete-global-index"]))
    adapter.delete_document.assert_not_awaited()
    adapter.close.assert_awaited_once()
