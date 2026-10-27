import asyncio
import json
from uuid import uuid4

import pytest

from app.config.settings import settings
from app.store import worklog_index_result_store as store


@pytest.fixture(autouse=True)
def isolated_result_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "worklog_light_index_result_dir", tmp_path)


def test_first_terminal_result_is_retained_across_reads(tmp_path):
    request, execution = str(uuid4()), str(uuid4())
    first = asyncio.run(store.persist_light_index_result(101, request, execution, "FAILED", "x" * 1200))
    second = asyncio.run(store.persist_light_index_result(101, request, execution, "COMPLETED"))
    assert second == first
    assert second.status == "FAILED" and len(second.error) == 1000
    assert asyncio.run(store.find_pending_light_index_result(101, request)) == first
    assert len(list(tmp_path.glob("*.json"))) == 1
    assert list(tmp_path.glob(".pending-*")) == []


@pytest.mark.parametrize("status", ["RUNNING", "PENDING", "invalid"])
def test_nonterminal_results_are_not_retained(status, tmp_path):
    with pytest.raises(ValueError):
        asyncio.run(store.persist_light_index_result(101, str(uuid4()), str(uuid4()), status))
    assert list(tmp_path.iterdir()) == []


def test_storage_failure_does_not_publish_partial_record(monkeypatch, tmp_path):
    def unavailable(*_args):
        raise OSError("volume unavailable")
    monkeypatch.setattr(store.os, "fsync", unavailable)
    with pytest.raises(OSError):
        asyncio.run(store.persist_light_index_result(101, str(uuid4()), str(uuid4()), "COMPLETED"))
    assert list(tmp_path.iterdir()) == []


def test_ack_only_removes_the_delivered_execution(tmp_path):
    request = str(uuid4())
    first = asyncio.run(store.persist_light_index_result(101, request, str(uuid4()), "FAILED"))
    second = asyncio.run(store.persist_light_index_result(101, request, str(uuid4()), "COMPLETED"))
    asyncio.run(store.acknowledge_light_index_result(first))
    asyncio.run(store.acknowledge_light_index_result(first))
    assert list(tmp_path.glob("*.json")) == [tmp_path / second.filename]


def test_corrupt_records_do_not_starve_other_results(tmp_path, caplog):
    for number in range(20):
        (tmp_path / f"000-corrupt-{number:02}.json").write_text("not json", encoding="utf-8")
    valid = asyncio.run(store.persist_light_index_result(101, str(uuid4()), str(uuid4()), "COMPLETED"))
    first, cursor = asyncio.run(store.list_pending_light_index_results())
    second, _ = asyncio.run(store.list_pending_light_index_results(cursor))
    assert first == [] and second == [valid]
    assert "Invalid retained index result" in caplog.text


def test_identifiers_and_filename_are_validated(tmp_path):
    with pytest.raises(ValueError):
        asyncio.run(store.persist_light_index_result(101, "../../escape", str(uuid4()), "COMPLETED"))
    record = asyncio.run(store.persist_light_index_result(101, str(uuid4()), str(uuid4()), "COMPLETED"))
    path = tmp_path / record.filename
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["worklog_id"] = 999
    path.write_text(json.dumps(payload), encoding="utf-8")
    records, _ = asyncio.run(store.list_pending_light_index_results())
    assert records == []
