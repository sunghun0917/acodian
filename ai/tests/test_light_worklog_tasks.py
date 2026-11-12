import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from app.config.settings import settings
from app.light.v4.service.lightrag_adapter import LightRagConfigurationError
from app.store.worklog_index_result_store import find_pending_light_index_result, persist_light_index_result
from app.task import light_worklog_tasks as tasks


@pytest.fixture(autouse=True)
def isolated_results(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "worklog_light_index_result_dir", tmp_path)


def run_task(client, service=None, operation="INDEX", request=None):
    with patch.object(tasks, "WorklogIndexClient") as clients, patch.object(tasks, "LightWorklogIndexService") as services:
        clients.return_value.__aenter__.return_value = client
        services.return_value = service or AsyncMock()
        return asyncio.run(tasks._execute_worklog_light_index_async(
            worklog_id=101, request_id=request or str(uuid4()), execution_id=str(uuid4()), operation=operation,
        ))


def item(indexed=True, error=None, deleted=True):
    return SimpleNamespace(items=[SimpleNamespace(indexed=indexed, error=error, deleted=deleted)])


def test_task_start_skipped_when_api_declines():
    client, service = AsyncMock(), AsyncMock()
    client.report_start.return_value = False
    assert run_task(client, service)["status"] == "SKIPPED"
    service.index_worklogs.assert_not_awaited()
    client.report_result.assert_not_awaited()


@pytest.mark.parametrize("operation,method", [("INDEX", "index_worklogs"), ("REINDEX", "reindex_worklogs"), ("DELETE", "delete_worklogs")])
def test_success_reports_completed(operation, method):
    client, service = AsyncMock(), AsyncMock()
    client.report_start.return_value = True
    getattr(service, method).return_value = item()
    assert run_task(client, service, operation)["status"] == "COMPLETED"
    getattr(service, method).assert_awaited_once_with([101])
    assert client.report_result.await_args.kwargs["status"] == "COMPLETED"


def test_task_converges_to_cleanup_when_source_deleted_during_index():
    client, service = AsyncMock(), AsyncMock()
    client.report_start.return_value = True
    service.index_worklogs.return_value = item(indexed=False, error="WORKLOG_NOT_FOUND")
    service.delete_worklogs.return_value = item()
    assert run_task(client, service)["status"] == "COMPLETED"
    service.delete_worklogs.assert_awaited_once_with([101])


def test_failure_uses_outbox_retry_without_celery_republication():
    client, service = AsyncMock(), AsyncMock()
    client.report_start.return_value = True
    client.report_attempt_failed.return_value = True
    service.index_worklogs.return_value = item(indexed=False, error="LIGHTRAG_COMPENSATION_FAILED")
    assert run_task(client, service)["status"] == "RETRY_PENDING"
    client.report_attempt_failed.assert_awaited_once()
    client.report_result.assert_not_awaited()


def test_exhausted_attempt_reports_failure_once():
    client, service = AsyncMock(), AsyncMock()
    client.report_start.return_value = True
    client.report_attempt_failed.return_value = False
    service.index_worklogs.return_value = item(indexed=False, error="LIGHTRAG_INSERT_FAILED")
    assert run_task(client, service)["status"] == "FAILED"
    assert client.report_result.await_args.kwargs["status"] == "FAILED"


def test_configuration_error_is_terminal():
    client, service = AsyncMock(), AsyncMock()
    client.report_start.return_value = True
    service.index_worklogs.side_effect = LightRagConfigurationError("missing configuration")
    assert run_task(client, service)["status"] == "FAILED"
    client.report_attempt_failed.assert_not_awaited()


def test_lost_start_response_never_mutates():
    client, service = AsyncMock(), AsyncMock()
    client.report_start.side_effect = TimeoutError()
    with pytest.raises(TimeoutError):
        run_task(client, service)
    assert client.report_start.await_count == 1
    service.index_worklogs.assert_not_awaited()
    client.report_result.assert_not_awaited()


def test_callback_failure_keeps_result_without_reexecuting_index():
    client, service = AsyncMock(), AsyncMock()
    client.report_start.return_value = True
    service.index_worklogs.return_value = item()
    client.report_result.side_effect = OSError("API unavailable")
    request = str(uuid4())
    assert run_task(client, service, request=request)["status"] == "COMPLETED"
    assert asyncio.run(find_pending_light_index_result(101, request)) is not None
    assert run_task(client, service, request=request)["status"] == "COMPLETED"
    assert service.index_worklogs.await_count == 1
    assert client.report_start.await_count == 1


def test_result_is_retained_before_callback_and_first_result_wins():
    request, execution = str(uuid4()), str(uuid4())
    async def receive(**payload):
        saved = await find_pending_light_index_result(101, request)
        assert saved is not None and saved.status == payload["status"]
    client = AsyncMock()
    client.report_result.side_effect = receive
    with patch.object(tasks, "WorklogIndexClient") as clients:
        clients.return_value.__aenter__.return_value = client
        asyncio.run(persist_light_index_result(101, request, execution, "FAILED", "first"))
        record = asyncio.run(tasks._persist_and_report(101, request, execution, "COMPLETED"))
    assert record.status == "FAILED"


def test_cancelled_execution_does_not_report_success_or_reschedule():
    client, service = AsyncMock(), AsyncMock()
    client.report_start.return_value = True
    service.index_worklogs.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        run_task(client, service)
    client.report_result.assert_not_awaited()
    client.report_attempt_failed.assert_not_awaited()


@pytest.mark.parametrize("operation", ["INDEX", "REINDEX"])
def test_empty_index_response_is_never_reported_completed(operation):
    client, service = AsyncMock(), AsyncMock()
    client.report_start.return_value = True
    client.report_attempt_failed.return_value = False
    getattr(service, "index_worklogs" if operation == "INDEX" else "reindex_worklogs").return_value = SimpleNamespace(items=[])
    assert run_task(client, service, operation)["status"] == "FAILED"


def test_all_celery_tasks_share_hard_limit():
    from app.task.celery_app import celery_app
    assert celery_app.conf.task_time_limit == settings.celery_task_time_limit_seconds
    assert tasks.execute_worklog_light_index.max_retries == 0
