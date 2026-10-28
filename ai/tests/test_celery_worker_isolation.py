"""전용 Redis에서 두 Worker의 독립 실행을 검증한다."""

import os
import time
from uuid import uuid4

import pytest
from celery import Celery
from celery.contrib.testing.worker import start_worker
from redis import Redis


BROKER_URL = os.getenv("WORKLOG_WORKER_PROBE_BROKER_URL", "")
pytestmark = pytest.mark.skipif(not BROKER_URL, reason="Requires explicitly isolated probe Redis")


def test_workers_consume_queues_independently():
    redis = Redis.from_url(BROKER_URL)
    prefix = "worker-isolation-" + uuid4().hex
    general_queue, index_queue = (f"{prefix}-{name}" for name in ("general", "index"))
    general = Celery(f"{prefix}-general", broker=BROKER_URL, backend=BROKER_URL)
    index = Celery(f"{prefix}-index", broker=BROKER_URL, backend=BROKER_URL)
    keys = [f"{prefix}-{name}" for name in ("general-start", "general-release", "index-start", "index-release")]

    def block(start_key, release_key):
        redis.set(start_key, "started", ex=60)
        deadline = time.monotonic() + 20
        while not redis.exists(release_key):
            if time.monotonic() >= deadline:
                raise TimeoutError("test release was not sent")
            time.sleep(0.05)
        return "done"

    def echo(value):
        return value

    for app in (general, index):
        app.task(name=f"{prefix}.block")(block)
        app.task(name=f"{prefix}.echo")(echo)

    def wait_started(key):
        deadline = time.monotonic() + 15
        while not redis.exists(key):
            if time.monotonic() >= deadline:
                pytest.fail("worker did not start the blocked task")
            time.sleep(0.05)

    try:
        with start_worker(general, pool="solo", concurrency=1, queues=[general_queue],
                          perform_ping_check=False, shutdown_timeout=25), \
             start_worker(index, pool="solo", concurrency=1, queues=[index_queue],
                          perform_ping_check=False, shutdown_timeout=25):
            general.send_task(f"{prefix}.block", args=keys[:2], queue=general_queue)
            wait_started(keys[0])
            result = index.send_task(f"{prefix}.echo", args=["index"], queue=index_queue)
            assert result.get(timeout=15, disable_sync_subtasks=False) == "index"
            redis.set(keys[1], "release", ex=60)

            index.send_task(f"{prefix}.block", args=keys[2:], queue=index_queue)
            wait_started(keys[2])
            result = general.send_task(f"{prefix}.echo", args=[general_queue], queue=general_queue)
            assert result.get(timeout=15, disable_sync_subtasks=False) == general_queue
            redis.set(keys[3], "release", ex=60)
    finally:
        redis.delete(*keys)
        general.close()
        index.close()
        redis.close()
