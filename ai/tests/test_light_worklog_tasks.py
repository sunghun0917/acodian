import contextvars
import asyncio
import uuid
import httpx
import pytest
from unittest.mock import AsyncMock, patch

from app.light.v4.model.worklog_index import (
    WorklogLightIndexExecutionItem,
    WorklogLightIndexExecutionResponse,
    WorklogLightDeleteExecutionItem,
    WorklogLightDeleteExecutionResponse,
)
from app.light.v4.service.lightrag_adapter import LightRagConfigurationError
from app.task.light_worklog_tasks import (
    execute_worklog_light_index,
    send_worklog_light_index_result,
)
from app.task.worker_runner import worker_runner
from app.store.worklog_index_result_store import PendingWorklogIndexResult


@pytest.fixture(autouse=True)
def result_store(monkeypatch, tmp_path):
    from app.config.settings import settings
    monkeypatch.setattr(settings, "worklog_light_index_result_dir", tmp_path)
    with patch("app.task.light_worklog_tasks.persist_light_index_result", new_callable=AsyncMock, create=True) as store:
        store.side_effect = lambda worklog_id, request_id, execution_id, status, error=None: PendingWorklogIndexResult(
            worklog_id, request_id, execution_id, status, error)
        yield store


@pytest.fixture(autouse=True)
def reset_runner():
    worker_runner.close()
    execute_worklog_light_index.request.retries = 0
    send_worklog_light_index_result.request.retries = 0
    yield
    worker_runner.close()
    execute_worklog_light_index.request.retries = 0
    send_worklog_light_index_result.request.retries = 0


def test_task_start_skipped_when_api_declines():
    worklog_id = 101
    request_id = str(uuid.uuid4())

    with patch("app.task.light_worklog_tasks.WorklogIndexClient") as mock_client_cls, \
         patch("app.task.light_worklog_tasks.LightWorklogIndexService") as mock_service_cls:

        mock_client = AsyncMock()
        mock_client.report_start.return_value = False
        mock_client_cls.return_value.__aenter__.return_value = mock_client

        mock_service = AsyncMock()
        mock_service_cls.return_value = mock_service

        result = execute_worklog_light_index(
            worklog_id=worklog_id,
            request_id=request_id,
            operation="INDEX",
        )

        assert result["status"] == "SKIPPED"
        mock_client.report_start.assert_awaited_once()
        mock_service.index_worklogs.assert_not_called()
        mock_service.delete_worklogs.assert_not_called()
        mock_client.report_result.assert_not_called()


def test_task_index_success_reports_completed():
    worklog_id = 101
    request_id = str(uuid.uuid4())

    with patch("app.task.light_worklog_tasks.WorklogIndexClient") as mock_client_cls, \
         patch("app.task.light_worklog_tasks.LightWorklogIndexService") as mock_service_cls:

        mock_client = AsyncMock()
        mock_client.report_start.return_value = True
        mock_client_cls.return_value.__aenter__.return_value = mock_client

        mock_service = AsyncMock()
        mock_service.index_worklogs.return_value = WorklogLightIndexExecutionResponse(
            items=[WorklogLightIndexExecutionItem(worklogId=101, indexed=True)]
        )
        mock_service_cls.return_value = mock_service

        result = execute_worklog_light_index(
            worklog_id=worklog_id,
            request_id=request_id,
            operation="INDEX",
        )

        assert result["status"] == "COMPLETED"
        mock_client.report_start.assert_awaited_once()
        mock_service.index_worklogs.assert_awaited_once_with([101])
        mock_client.report_result.assert_awaited_once()
        kwargs = mock_client.report_result.await_args.kwargs
        assert kwargs["worklog_id"] == 101
        assert kwargs["request_id"] == request_id
        assert kwargs["status"] == "COMPLETED"


def test_task_delete_success_reports_completed():
    worklog_id = 101
    request_id = str(uuid.uuid4())

    with patch("app.task.light_worklog_tasks.WorklogIndexClient") as mock_client_cls, \
         patch("app.task.light_worklog_tasks.LightWorklogIndexService") as mock_service_cls:

        mock_client = AsyncMock()
        mock_client.report_start.return_value = True
        mock_client_cls.return_value.__aenter__.return_value = mock_client

        mock_service = AsyncMock()
        mock_service.delete_worklogs.return_value = WorklogLightDeleteExecutionResponse(
            items=[WorklogLightDeleteExecutionItem(worklogId=101, deleted=True)]
        )
        mock_service_cls.return_value = mock_service

        result = execute_worklog_light_index(
            worklog_id=worklog_id,
            request_id=request_id,
            operation="DELETE",
        )

        assert result["status"] == "COMPLETED"
        mock_service.delete_worklogs.assert_awaited_once_with([101])
        mock_client.report_result.assert_awaited_once()
        kwargs = mock_client.report_result.await_args.kwargs
        assert kwargs["worklog_id"] == 101
        assert kwargs["status"] == "COMPLETED"


def test_task_converges_to_cleanup_when_source_deleted_during_index():
    worklog_id = 101
    request_id = str(uuid.uuid4())

    with patch("app.task.light_worklog_tasks.WorklogIndexClient") as mock_client_cls, \
         patch("app.task.light_worklog_tasks.LightWorklogIndexService") as mock_service_cls:

        mock_client = AsyncMock()
        mock_client.report_start.return_value = True
        mock_client_cls.return_value.__aenter__.return_value = mock_client

        mock_service = AsyncMock()
        mock_service.index_worklogs.return_value = WorklogLightIndexExecutionResponse(
            items=[WorklogLightIndexExecutionItem(worklogId=101, indexed=False, error="WORKLOG_NOT_FOUND")]
        )
        mock_service.delete_worklogs.return_value = WorklogLightDeleteExecutionResponse(
            items=[WorklogLightDeleteExecutionItem(worklogId=101, deleted=True)]
        )
        mock_service_cls.return_value = mock_service

        result = execute_worklog_light_index(
            worklog_id=worklog_id,
            request_id=request_id,
            operation="INDEX",
        )

        assert result["status"] == "COMPLETED"
        mock_service.delete_worklogs.assert_awaited_once_with([101])
        mock_client.report_result.assert_awaited_once()
        assert mock_client.report_result.await_args.kwargs["status"] == "COMPLETED"


def test_task_retries_on_index_failure_with_exponential_backoff():
    worklog_id = 101
    request_id = str(uuid.uuid4())

    with patch("app.task.light_worklog_tasks.WorklogIndexClient") as mock_client_cls, \
         patch("app.task.light_worklog_tasks.LightWorklogIndexService") as mock_service_cls, \
         patch.object(execute_worklog_light_index, "retry", side_effect=Exception("CeleryRetry")) as mock_retry:

        mock_client = AsyncMock()
        mock_client.report_start.return_value = True
        mock_client_cls.return_value.__aenter__.return_value = mock_client

        mock_service = AsyncMock()
        mock_service.index_worklogs.side_effect = RuntimeError("transient db error")
        mock_service_cls.return_value = mock_service

        execute_worklog_light_index.request.retries = 2

        with pytest.raises(Exception, match="CeleryRetry"):
            execute_worklog_light_index(
                worklog_id=worklog_id,
                request_id=request_id,
                operation="INDEX",
            )

        mock_retry.assert_called_once()
        retry_kwargs = mock_retry.call_args[1]
        assert retry_kwargs["countdown"] == 4  # 2 ** 2
        assert retry_kwargs["max_retries"] == 5


def test_task_exhausted_retries_reports_failed():
    worklog_id = 101
    request_id = str(uuid.uuid4())

    with patch("app.task.light_worklog_tasks.WorklogIndexClient") as mock_client_cls, \
         patch("app.task.light_worklog_tasks.LightWorklogIndexService") as mock_service_cls:

        mock_client = AsyncMock()
        mock_client.report_start.return_value = True
        mock_client_cls.return_value.__aenter__.return_value = mock_client

        mock_service = AsyncMock()
        mock_service.index_worklogs.side_effect = RuntimeError("persistent failure")
        mock_service_cls.return_value = mock_service

        execute_worklog_light_index.request.retries = 5  # max retries reached

        with pytest.raises(RuntimeError, match="persistent failure"):
            execute_worklog_light_index(
                worklog_id=worklog_id,
                request_id=request_id,
                operation="INDEX",
            )

        mock_client.report_result.assert_awaited_once()
        kwargs = mock_client.report_result.await_args.kwargs
        assert kwargs["status"] == "FAILED"
        assert "persistent failure" in kwargs["error"]


def test_task_non_retryable_error_reports_failed_without_retry():
    worklog_id = 101
    request_id = str(uuid.uuid4())

    with patch("app.task.light_worklog_tasks.WorklogIndexClient") as mock_client_cls, \
         patch("app.task.light_worklog_tasks.LightWorklogIndexService") as mock_service_cls, \
         patch.object(execute_worklog_light_index, "retry") as mock_retry:

        mock_client = AsyncMock()
        mock_client.report_start.return_value = True
        mock_client_cls.return_value.__aenter__.return_value = mock_client

        mock_service = AsyncMock()
        mock_service.index_worklogs.side_effect = LightRagConfigurationError("API key missing")
        mock_service_cls.return_value = mock_service

        with pytest.raises(LightRagConfigurationError):
            execute_worklog_light_index(
                worklog_id=worklog_id,
                request_id=request_id,
                operation="INDEX",
            )

        mock_retry.assert_not_called()
        mock_client.report_result.assert_awaited_once()
        assert mock_client.report_result.await_args.kwargs["status"] == "FAILED"


def test_task_result_report_failure_keeps_durable_result_without_redis(result_store):
    worklog_id = 101
    request_id = str(uuid.uuid4())

    with patch("app.task.light_worklog_tasks.WorklogIndexClient") as mock_client_cls, \
         patch("app.task.light_worklog_tasks.LightWorklogIndexService") as mock_service_cls, \
         patch("app.task.light_worklog_tasks.celery_app") as mock_celery:

        mock_client = AsyncMock()
        mock_client.report_start.return_value = True
        mock_client.report_result.side_effect = RuntimeError("network timeout to spring")
        mock_client_cls.return_value.__aenter__.return_value = mock_client

        mock_service = AsyncMock()
        mock_service.index_worklogs.return_value = WorklogLightIndexExecutionResponse(
            items=[WorklogLightIndexExecutionItem(worklogId=101, indexed=True)]
        )
        mock_service_cls.return_value = mock_service

        result = execute_worklog_light_index(
            worklog_id=worklog_id,
            request_id=request_id,
            operation="INDEX",
        )

        result_store.assert_awaited_once()
        mock_celery.send_task.assert_not_called()
        assert result["status"] == "COMPLETED"


def test_result_delivery_survives_spring_and_redis_outage(result_store):
    worklog_id = 101
    request_id = str(uuid.uuid4())

    with patch("app.task.light_worklog_tasks.WorklogIndexClient") as mock_client_cls, \
         patch("app.task.light_worklog_tasks.LightWorklogIndexService") as mock_service_cls, \
         patch("app.task.light_worklog_tasks.celery_app") as mock_celery:

        mock_client = AsyncMock()
        mock_client.report_start.return_value = True
        mock_client.report_result.side_effect = RuntimeError("network timeout to spring")
        mock_client_cls.return_value.__aenter__.return_value = mock_client

        mock_service = AsyncMock()
        mock_service.index_worklogs.return_value = WorklogLightIndexExecutionResponse(
            items=[WorklogLightIndexExecutionItem(worklogId=101, indexed=True)]
        )
        mock_service_cls.return_value = mock_service

        mock_celery.send_task.side_effect = RuntimeError("redis down")

        result = execute_worklog_light_index(worklog_id=worklog_id, request_id=request_id, operation="INDEX")
        assert result["status"] == "COMPLETED"
        result_store.assert_awaited_once()
        mock_celery.send_task.assert_not_called()


def test_legacy_result_task_retains_for_ai_callback_replay(result_store):
    with patch("app.task.light_worklog_tasks.WorklogIndexClient") as mock_client_cls, \
         patch.object(send_worklog_light_index_result, "retry", side_effect=Exception("CeleryRetry")) as mock_retry:
        mock_client = AsyncMock()
        mock_client.report_result.side_effect = RuntimeError("spring still down")
        mock_client_cls.return_value.__aenter__.return_value = mock_client

        send_worklog_light_index_result.request.retries = 1

        result = send_worklog_light_index_result(
            worklog_id=101, request_id=str(uuid.uuid4()), execution_id=str(uuid.uuid4()), status="COMPLETED")
        assert result["status"] == "COMPLETED"
        result_store.assert_awaited_once()
        mock_retry.assert_not_called()


def test_start_transient_failure_retries_with_same_execution_id():
    with patch("app.task.light_worklog_tasks.WorklogIndexClient") as clients, \
         patch("app.task.light_worklog_tasks.LightWorklogIndexService") as services, \
         patch("asyncio.sleep", new_callable=AsyncMock):
        client = AsyncMock()
        client.report_start.side_effect = [httpx.ConnectError("temporary"), True]
        clients.return_value.__aenter__.return_value = client
        services.return_value.index_worklogs = AsyncMock(return_value=WorklogLightIndexExecutionResponse(
            items=[WorklogLightIndexExecutionItem(worklogId=101, indexed=True)]))
        result = execute_worklog_light_index(101, str(uuid.uuid4()), "INDEX")
        assert result["status"] == "COMPLETED"
        assert client.report_start.await_count == 2
        assert client.report_start.await_args_list[0] == client.report_start.await_args_list[1]


def test_all_celery_tasks_have_hard_deadline():
    from app.task.celery_app import celery_app
    assert celery_app.conf.task_time_limit == 540


@pytest.mark.parametrize("status", [429, 503, 403])
def test_start_retries_only_transient_http_statuses(status, result_store):
    with patch("app.task.light_worklog_tasks.WorklogIndexClient") as clients, \
         patch("app.task.light_worklog_tasks.LightWorklogIndexService") as services, \
         patch("asyncio.sleep", new_callable=AsyncMock):
        client = AsyncMock()
        request = httpx.Request("POST", "http://api.test/start")
        client.report_start.side_effect = [httpx.HTTPStatusError("start error", request=request,
            response=httpx.Response(status, request=request)), False]
        clients.return_value.__aenter__.return_value = client
        if status == 403:
            with pytest.raises(httpx.HTTPStatusError):
                execute_worklog_light_index(101, str(uuid.uuid4()), "INDEX")
            assert client.report_start.await_count == 1
        else:
            assert execute_worklog_light_index(101, str(uuid.uuid4()), "INDEX")["status"] == "SKIPPED"
            assert client.report_start.await_count == 2
        services.assert_not_called()
        result_store.assert_not_awaited()


def test_start_retries_are_bounded_and_never_claim_index_failure(result_store):
    with patch("app.task.light_worklog_tasks.WorklogIndexClient") as clients, \
         patch("app.task.light_worklog_tasks.LightWorklogIndexService") as services, \
         patch("asyncio.sleep", new_callable=AsyncMock):
        client = AsyncMock()
        client.report_start.side_effect = httpx.ConnectError("api unavailable")
        clients.return_value.__aenter__.return_value = client
        with pytest.raises(httpx.ConnectError):
            execute_worklog_light_index(101, str(uuid.uuid4()), "INDEX")
        assert client.report_start.await_count == 3
        services.assert_not_called()
        result_store.assert_not_awaited()


def test_cooperative_index_deadline_cancels_work_and_persists_failure(monkeypatch, result_store):
    from app.config.settings import settings
    monkeypatch.setattr(settings, "worklog_light_index_timeout_seconds", 0.01)
    cleaned = []

    async def blocked(_ids):
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.append(True)

    with patch("app.task.light_worklog_tasks.WorklogIndexClient") as clients, \
         patch("app.task.light_worklog_tasks.LightWorklogIndexService") as services:
        client = AsyncMock()
        client.report_start.return_value = True
        clients.return_value.__aenter__.return_value = client
        services.return_value.index_worklogs = blocked
        execute_worklog_light_index.request.retries = 5
        with pytest.raises(TimeoutError):
            execute_worklog_light_index(101, str(uuid.uuid4()), "INDEX")
        assert cleaned == [True]
        assert result_store.await_args.args[3:] == ("FAILED", "TimeoutError")
        assert client.report_result.await_args.kwargs["status"] == "FAILED"


def test_failed_result_is_durable_when_both_callback_and_broker_fail(result_store):
    with patch("app.task.light_worklog_tasks.WorklogIndexClient") as clients, \
         patch("app.task.light_worklog_tasks.LightWorklogIndexService") as services, \
         patch("app.task.light_worklog_tasks.celery_app") as celery:
        client = AsyncMock()
        client.report_start.return_value = True
        client.report_result.side_effect = httpx.ConnectError("API unavailable")
        clients.return_value.__aenter__.return_value = client
        services.return_value.index_worklogs = AsyncMock(side_effect=RuntimeError("index failed"))
        celery.send_task.side_effect = RuntimeError("Redis unavailable")
        execute_worklog_light_index.request.retries = 5
        with pytest.raises(RuntimeError, match="index failed"):
            execute_worklog_light_index(101, str(uuid.uuid4()), "INDEX")
        assert result_store.await_args.args[3:] == ("FAILED", "index failed")
        celery.send_task.assert_not_called()


def test_result_retention_precedes_callback(result_store):
    with patch("app.task.light_worklog_tasks.WorklogIndexClient") as clients, \
         patch("app.task.light_worklog_tasks.LightWorklogIndexService") as services:
        client = AsyncMock()
        client.report_start.return_value = True
        clients.return_value.__aenter__.return_value = client
        services.return_value.index_worklogs = AsyncMock(return_value=WorklogLightIndexExecutionResponse(
            items=[WorklogLightIndexExecutionItem(worklogId=101, indexed=True)]))
        async def check_commit(**_kwargs):
            result_store.assert_awaited_once()
        client.report_result.side_effect = check_commit
        execute_worklog_light_index(101, str(uuid.uuid4()), "INDEX")


def test_result_retention_failure_is_not_hidden_or_reported_as_success(result_store):
    result_store.side_effect = OSError("result volume unavailable")
    with patch("app.task.light_worklog_tasks.WorklogIndexClient") as clients, \
         patch("app.task.light_worklog_tasks.LightWorklogIndexService") as services, \
         patch("asyncio.sleep", new_callable=AsyncMock):
        client = AsyncMock()
        client.report_start.return_value = True
        clients.return_value.__aenter__.return_value = client
        services.return_value.index_worklogs = AsyncMock(return_value=WorklogLightIndexExecutionResponse(
            items=[WorklogLightIndexExecutionItem(worklogId=101, indexed=True)]))
        with pytest.raises(OSError, match="result volume unavailable"):
            execute_worklog_light_index(101, str(uuid.uuid4()), "INDEX")
        assert result_store.await_count == 1
        client.report_result.assert_not_awaited()
        services.return_value.index_worklogs.assert_awaited_once()


@pytest.mark.parametrize("operation", ["INDEX", "REINDEX"])
def test_empty_index_response_is_never_reported_completed(operation, result_store):
    with patch("app.task.light_worklog_tasks.WorklogIndexClient") as clients, \
         patch("app.task.light_worklog_tasks.LightWorklogIndexService") as services:
        client = AsyncMock()
        client.report_start.return_value = True
        clients.return_value.__aenter__.return_value = client
        service = services.return_value
        method = "index_worklogs" if operation == "INDEX" else "reindex_worklogs"
        setattr(service, method, AsyncMock(return_value=WorklogLightIndexExecutionResponse(items=[])))
        execute_worklog_light_index.request.retries = 5
        with pytest.raises(RuntimeError, match="returned no result"):
            execute_worklog_light_index(101, str(uuid.uuid4()), operation)
        assert result_store.await_args.args[3] == "FAILED"


def test_retry_publication_failure_is_visible_and_left_to_outbox(result_store, caplog):
    from celery.exceptions import Reject
    with patch("app.task.light_worklog_tasks.WorklogIndexClient") as clients, \
         patch("app.task.light_worklog_tasks.LightWorklogIndexService") as services, \
         patch.object(execute_worklog_light_index, "retry", side_effect=Reject("broker unavailable", requeue=False)):
        client = AsyncMock()
        client.report_start.return_value = True
        clients.return_value.__aenter__.return_value = client
        services.return_value.index_worklogs = AsyncMock(side_effect=RuntimeError("transient failure"))
        with pytest.raises(Reject):
            execute_worklog_light_index(101, str(uuid.uuid4()), "INDEX")
        result_store.assert_not_awaited()
        assert "Light retry publication failed" in caplog.text


def test_duplicate_task_replays_cached_result_without_start_or_index(result_store):
    from app.store.worklog_index_result_store import persist_light_index_result as retain
    request, execution = str(uuid.uuid4()), str(uuid.uuid4())
    asyncio.run(retain(101, request, execution, "COMPLETED"))
    with patch("app.task.light_worklog_tasks.WorklogIndexClient") as clients, \
         patch("app.task.light_worklog_tasks.LightWorklogIndexService") as services:
        client = AsyncMock()
        clients.return_value.__aenter__.return_value = client
        result = execute_worklog_light_index(101, request, "INDEX")
        assert result["execution_id"] == execution
        assert result["status"] == "COMPLETED"
        client.report_start.assert_not_awaited()
        services.assert_not_called()
        result_store.assert_not_awaited()
        assert client.report_result.await_args.kwargs["execution_id"] == execution


def test_worker_runner_contextvar_isolation():
    test_var = contextvars.ContextVar("test_var", default="default")

    async def step(val: str):
        test_var.set(val)
        return test_var.get()

    res1 = worker_runner.run(step("val1"))
    assert res1 == "val1"
    # Outside contextvar should still be default
    assert test_var.get() == "default"

    res2 = worker_runner.run(step("val2"))
    assert res2 == "val2"
    assert test_var.get() == "default"
