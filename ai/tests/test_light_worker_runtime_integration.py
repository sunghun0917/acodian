"""격리 Redis와 AI 결과 보관함에서 Linux 워커 장애 복구를 검증한다."""

import asyncio
import os
import subprocess
import sys
import time
from uuid import uuid4

import pytest
from celery import Celery
from celery.contrib.testing.worker import start_worker
from celery.exceptions import TimeLimitExceeded
from celery.result import allow_join_result


BROKER_URL = os.getenv("WORKLOG_WORKER_PROBE_BROKER_URL", "")
pytestmark = pytest.mark.skipif(
    sys.platform == "win32" or not BROKER_URL,
    reason="Requires Linux prefork and explicitly isolated probe Redis",
)


@pytest.fixture(autouse=True)
def isolated_result_dir(monkeypatch, tmp_path):
    from app.config.settings import settings
    monkeypatch.setattr(settings, "worklog_light_index_result_dir", tmp_path)
    monkeypatch.setenv("WORKLOG_LIGHT_INDEX_RESULT_DIR", str(tmp_path))


def test_retained_result_survives_worker_process_restart():
    from app.store.worklog_index_result_store import find_pending_light_index_result
    request, execution = str(uuid4()), str(uuid4())
    code = ("import asyncio; from app.store.worklog_index_result_store import persist_light_index_result; "
            f"asyncio.run(persist_light_index_result(101, {request!r}, {execution!r}, 'COMPLETED'))")
    child = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=20)
    assert child.returncode == 0, child.stderr
    record = asyncio.run(find_pending_light_index_result(101, request))
    assert record.status == "COMPLETED" and record.execution_id == execution


def test_real_result_survives_unreachable_http_and_broker(monkeypatch):
    from app.config.settings import settings
    from app.task.light_worklog_tasks import _persist_and_report
    from app.store.worklog_index_result_store import find_pending_light_index_result
    monkeypatch.setattr(settings, "api_base_url", "http://127.0.0.1:1")
    request, execution = str(uuid4()), str(uuid4())

    record = asyncio.run(_persist_and_report(102, request, execution, "COMPLETED"))
    assert asyncio.run(find_pending_light_index_result(102, request)) == record


def test_hard_limit_replaces_hung_prefork_child_and_runs_next_task(monkeypatch):
    from app.task.celery_app import celery_app
    assert celery_app.conf.task_time_limit == 540
    monkeypatch.setenv("CELERY_BROKER_URL", BROKER_URL)
    monkeypatch.setenv("CELERY_RESULT_BACKEND", BROKER_URL)
    app = Celery("isolated_deadline_probe", broker=BROKER_URL, backend=BROKER_URL)
    app.conf.update(task_time_limit=1, task_acks_late=True, worker_prefetch_multiplier=1,
                    task_default_queue="isolated_deadline_probe")
    assert app.conf.broker_url == BROKER_URL

    @app.task(name="isolated_probe.pid")
    def pid():
        return os.getpid()

    @app.task(name="isolated_probe.hang")
    def hang():
        while True:
            time.sleep(1)

    try:
        with start_worker(app, pool="prefork", concurrency=1, perform_ping_check=False, shutdown_timeout=20):
            with allow_join_result():
                before = pid.delay().get(timeout=20)
                with pytest.raises(TimeLimitExceeded):
                    hang.delay().get(timeout=20)
                after = pid.delay().get(timeout=20)
        assert before != after
    finally:
        app.close()
