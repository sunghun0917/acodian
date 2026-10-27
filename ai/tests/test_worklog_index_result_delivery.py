import asyncio
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from app.config.settings import settings
from app.service.worklog_index_result_delivery import report_stored_result, deliver_pending_results_once
from app.store.worklog_index_result_store import persist_light_index_result, find_pending_light_index_result


@pytest.fixture(autouse=True)
def isolated_result_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "worklog_light_index_result_dir", tmp_path)


def test_failed_callback_keeps_result_and_only_success_acknowledges(tmp_path):
    record = asyncio.run(persist_light_index_result(101, str(uuid4()), str(uuid4()), "COMPLETED"))
    client = AsyncMock()
    client.report_result.side_effect = OSError("API unavailable")
    assert asyncio.run(report_stored_result(record, client)) is False
    assert (tmp_path / record.filename).exists()
    client.report_result.side_effect = None
    assert asyncio.run(report_stored_result(record, client)) is True
    assert not (tmp_path / record.filename).exists()


def test_cancelled_callback_never_acknowledges(tmp_path):
    record = asyncio.run(persist_light_index_result(101, str(uuid4()), str(uuid4()), "COMPLETED"))
    client = AsyncMock()
    client.report_result.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(report_stored_result(record, client))
    assert (tmp_path / record.filename).exists()


def test_restarted_delivery_reads_disk_without_reindexing(tmp_path):
    request = str(uuid4())
    record = asyncio.run(persist_light_index_result(101, request, str(uuid4()), "COMPLETED"))
    with patch("app.service.worklog_index_result_delivery.WorklogIndexClient") as clients:
        client = AsyncMock()
        clients.return_value.__aenter__.return_value = client
        asyncio.run(deliver_pending_results_once())
        client.report_result.assert_awaited_once()
        assert client.report_result.await_args.kwargs["execution_id"] == record.execution_id
    assert asyncio.run(find_pending_light_index_result(101, request)) is None


def test_twenty_failed_callbacks_do_not_block_next_result(tmp_path):
    records = [asyncio.run(persist_light_index_result(worklog_id, str(uuid4()), str(uuid4()), "COMPLETED"))
               for worklog_id in range(100, 121)]
    with patch("app.service.worklog_index_result_delivery.WorklogIndexClient") as clients:
        client = AsyncMock()

        async def response(**payload):
            if payload["worklog_id"] < 120:
                raise OSError("temporary callback failure")

        client.report_result.side_effect = response
        clients.return_value.__aenter__.return_value = client
        cursor = asyncio.run(deliver_pending_results_once())
        asyncio.run(deliver_pending_results_once(cursor))
        assert client.report_result.await_count == 21
    assert len(list(tmp_path.glob("*.json"))) == 20
    assert not (tmp_path / records[-1].filename).exists()
