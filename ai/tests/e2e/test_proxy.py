import importlib.util
import io
from pathlib import Path


def test_acceptance_event_contains_only_trace_fields():
    spec = importlib.util.spec_from_file_location("e2e_proxy", Path(__file__).with_name("proxy.py"))
    proxy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(proxy)
    request = {"worklogIds": [3], "requestId": "request", "unexpected": "private"}
    response = {"items": [{"worklogId": 3, "requestId": "request", "taskId": "task", "unexpected": "private"}]}
    assert proxy.acceptance_event("/ai/light/worklogs-v4/index", 202, request, response) == {
        "status": 202, "operation": "INDEX", "worklog_ids": [3], "request_id": "request", "task_ids": ["task"]}
    assert proxy.acceptance_event("/unknown", 202, request, response) is None
    assert proxy.acceptance_event("/ai/light/worklogs-v4/index", 503, request, response) is None


def test_proxy_reads_chunked_request_without_content_length():
    spec = importlib.util.spec_from_file_location("e2e_proxy", Path(__file__).with_name("proxy.py"))
    proxy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(proxy)
    assert proxy.read_body(io.BytesIO(b"3\r\nabc\r\n2\r\nde\r\n0\r\n\r\n"), {"Transfer-Encoding": "chunked"}) == b"abcde"


def test_dispatch_barrier_reports_then_waits():
    spec = importlib.util.spec_from_file_location("e2e_proxy", Path(__file__).with_name("proxy.py"))
    proxy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(proxy)

    class Broker:
        def __init__(self):
            self.events = []
        def get(self, key):
            return b"1"
        def rpush(self, key, value):
            self.events.append((key, value))
        def blpop(self, key, timeout):
            self.events.append((key, timeout))
            return (key, b"release")

    broker = Broker()
    proxy.dispatch_barrier(broker, {"worklogIds": [3], "requestId": "request"})
    assert broker.events == [("e2e:dispatch:3:arrived", "request"), ("e2e:dispatch:3:release", 60)]
