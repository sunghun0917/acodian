"""격리 Spring 테스트 서버에 실제 AI 내부 클라이언트로 콜백을 보낸다."""

import argparse
import asyncio
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import socket
from tempfile import TemporaryDirectory
from threading import Thread
import time
from urllib.parse import urlparse
from uuid import UUID
from unittest.mock import patch

import httpx

from app.client.worklog_index_client import WorklogIndexClient
from app.config.settings import settings


async def run(args: argparse.Namespace) -> dict[str, bool | int]:
    """로컬 테스트 서버에만 접속하고 기대한 HTTP 실패도 명시적으로 검증한다."""
    endpoint = urlparse(settings.api_base_url)
    assert endpoint.hostname in {"127.0.0.1", "localhost"} and endpoint.port
    result: dict[str, bool | int] = {"httpStatus": 200}
    try:
        async with WorklogIndexClient() as client:
            if args.mode != "result":
                execute = await client.report_start(args.worklog_id, str(args.request_id), str(args.execution_id))
                result["execute"] = execute
                if not execute or args.mode == "start":
                    assert args.expected_http_status == 200
                    return result
            await client.report_result(
                worklog_id=args.worklog_id,
                request_id=str(args.request_id),
                execution_id=str(args.execution_id),
                status=args.status,
                error="INDEX_FAILED" if args.status == "FAILED" else None,
            )
    except httpx.HTTPStatusError as exc:
        assert exc.response.status_code == args.expected_http_status
        return {"httpStatus": exc.response.status_code}
    assert args.expected_http_status == 200
    return result


def run_pipeline(args: argparse.Namespace) -> dict[str, bool | int | str]:
    """실제 AI HTTP·Redis·Celery·Spring을 연결하고 외부 색인 엔진만 대체한다."""
    import uvicorn
    from celery.contrib.testing.worker import start_worker
    from celery.result import allow_join_result
    from app.light.v4.model.worklog_index import (
        WorklogLightDeleteExecutionItem, WorklogLightDeleteExecutionResponse,
        WorklogLightIndexExecutionItem, WorklogLightIndexExecutionResponse,
    )
    from app.light.v4.service.lightrag_adapter import LightRagConfigurationError
    from app.task.celery_app import celery_app

    endpoint = urlparse(settings.api_base_url)
    assert endpoint.hostname in {"127.0.0.1", "localhost"} and endpoint.port
    assert not celery_app.conf.task_always_eager
    calls: list[str] = []
    callback_failures: list[bool] = []
    spring_origin = f"{endpoint.scheme}://{endpoint.netloc}"

    class ResultProxy(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            if self.path.endswith("/result") and not callback_failures:
                callback_failures.append(True)
                self.send_response(503)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            response = httpx.post(spring_origin + self.path, content=body, timeout=10,
                headers={"X-AI-Internal-Token": self.headers.get("X-AI-Internal-Token", ""), "Content-Type": "application/json"})
            self.send_response(response.status_code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response.content)))
            self.end_headers()
            self.wfile.write(response.content)

        def log_message(self, *_args):
            return

    class ExternalIndexStub:
        async def index_worklogs(self, ids):
            calls.append("INDEX")
            if args.status == "FAILED":
                raise LightRagConfigurationError("E2E_FINAL_INDEX_FAILURE")
            return WorklogLightIndexExecutionResponse(items=[WorklogLightIndexExecutionItem(worklogId=ids[0], indexed=True)])

        async def reindex_worklogs(self, ids):
            calls.append("REINDEX")
            return WorklogLightIndexExecutionResponse(items=[WorklogLightIndexExecutionItem(worklogId=ids[0], indexed=True)])

        async def delete_worklogs(self, ids):
            calls.append("DELETE")
            return WorklogLightDeleteExecutionResponse(items=[WorklogLightDeleteExecutionItem(worklogId=ids[0], deleted=True)])

    with TemporaryDirectory(prefix="worklog-callback-e2e-") as folder:
        settings.worklog_light_index_result_dir = Path(folder)
        proxy = None
        proxy_thread = None
        if args.mode == "pipeline-retry":
            proxy = HTTPServer(("127.0.0.1", 0), ResultProxy)
            proxy_thread = Thread(target=proxy.serve_forever, daemon=True)
            proxy_thread.start()
            settings.api_base_url = f"http://127.0.0.1:{proxy.server_port}/api"
        from app.main import app
        from app.store.worklog_index_result_store import find_pending_light_index_result
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        ai_port = sock.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, log_level="warning", access_log=False))
        server_thread = Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
        server_thread.start()
        try:
            deadline = time.monotonic() + 10
            while not server.started and server_thread.is_alive() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert server.started, "AI HTTP server did not start"
            with patch("app.task.light_worklog_tasks.LightWorklogIndexService", ExternalIndexStub):
                with start_worker(celery_app, pool="solo", concurrency=1, queues=["worklog_index"],
                                  perform_ping_check=False, shutdown_timeout=10, loglevel="ERROR"):
                    response = httpx.post(f"http://127.0.0.1:{ai_port}{settings.api_prefix}/light/worklogs-v4/{args.operation.lower()}",
                        json={"worklogIds": [args.worklog_id], "requestId": str(args.request_id)}, timeout=10,
                        headers={"X-AI-Internal-Token": settings.ai_internal_token})
                    assert response.status_code == 202, response.text
                    task_id = response.json()["items"][0]["taskId"]
                    with allow_join_result():
                        try:
                            outcome = celery_app.AsyncResult(task_id).get(timeout=20)
                            assert outcome["status"] == args.status
                        except LightRagConfigurationError:
                            assert args.status == "FAILED"
                    if args.mode == "pipeline-retry":
                        deadline = time.monotonic() + 15
                        while time.monotonic() < deadline:
                            if asyncio.run(find_pending_light_index_result(args.worklog_id, str(args.request_id))) is None:
                                break
                            time.sleep(0.05)
                        else:
                            raise AssertionError("Retained result was not delivered after HTTP recovery")
                        assert callback_failures == [True]
            assert calls == [args.operation], "Callback retry must not re-execute indexing"
            return {"httpStatus": 202, "indexCalls": len(calls), "status": args.status,
                    "callbackRetried": bool(callback_failures)}
        finally:
            server.should_exit = True
            server_thread.join(timeout=10)
            sock.close()
            if proxy is not None:
                proxy.shutdown()
                proxy.server_close()
                proxy_thread.join(timeout=5)
                assert not proxy_thread.is_alive(), "Callback proxy did not stop"
            assert not server_thread.is_alive(), "AI HTTP server did not stop"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worklog-id", type=int, required=True)
    parser.add_argument("--request-id", type=UUID, required=True)
    parser.add_argument("--execution-id", type=UUID, required=True)
    parser.add_argument("--status", choices=("COMPLETED", "FAILED"), default="COMPLETED")
    parser.add_argument("--mode", choices=("start", "result", "complete", "pipeline", "pipeline-retry"), default="complete")
    parser.add_argument("--operation", choices=("INDEX", "REINDEX", "DELETE"), default="INDEX")
    parser.add_argument("--expected-http-status", type=int, default=200)
    args = parser.parse_args()
    result = run_pipeline(args) if args.mode.startswith("pipeline") else asyncio.run(run(args))
    print(json.dumps(result, separators=(",", ":")))
