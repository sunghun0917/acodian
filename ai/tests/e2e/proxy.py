"""Capture only indexing acceptance metadata in the isolated test network."""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import redis


def acceptance_event(path, status, request, response):
    operations = {f"/ai/light/worklogs-v4/{op}": op.upper() for op in ("index", "reindex", "delete")}
    if status != 202 or path not in operations:
        return None
    return {"status": status, "operation": operations[path], "worklog_ids": request["worklogIds"],
            "request_id": request["requestId"], "task_ids": [item["taskId"] for item in response["items"]]}


def read_body(stream, headers):
    limit = 1024 * 1024
    if headers.get("Transfer-Encoding", "").lower() != "chunked":
        length = int(headers.get("Content-Length", "0"))
        if length > limit:
            raise ValueError("E2E request too large")
        return stream.read(length)
    chunks = []
    size = 0
    while True:
        length = int(stream.readline(8192).split(b";", 1)[0].strip(), 16)
        size += length
        if size > limit:
            raise ValueError("E2E request too large")
        if length == 0:
            if stream.readline(8192) != b"\r\n":
                raise ValueError("Unexpected E2E chunk trailer")
            return b"".join(chunks)
        chunk = stream.read(length)
        if len(chunk) != length or stream.read(2) != b"\r\n":
            raise ValueError("Incomplete E2E chunk")
        chunks.append(chunk)


def dispatch_barrier(broker, request):
    for worklog_id in request["worklogIds"]:
        prefix = f"e2e:dispatch:{worklog_id}"
        if broker.get(f"{prefix}:armed") == b"1":
            broker.rpush(f"{prefix}:arrived", request["requestId"])
            if not broker.blpop(f"{prefix}:release", timeout=60):
                raise TimeoutError("E2E dispatch barrier expired")


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        self.connection.settimeout(30)
        body = read_body(self.rfile, self.headers)
        request = json.loads(body)
        broker = redis.Redis(host="redis", port=6379)
        dispatch_barrier(broker, request)
        with httpx.Client(timeout=30, trust_env=False) as client:
            result = client.post("http://ai:8200" + self.path, content=body,
                                 headers={"Content-Type": "application/json", "X-AI-Internal-Token": self.headers.get("X-AI-Internal-Token", "")})
        if result.status_code == 202:
            event = acceptance_event(self.path, result.status_code, request, result.json())
            if event is not None:
                for worklog_id in event["worklog_ids"]:
                    key = f"e2e:accepted:{worklog_id}"
                    broker.rpush(key, json.dumps(event))
                    broker.expire(key, 120)
        self.send_response(result.status_code)
        self.send_header("Content-Type", result.headers.get("Content-Type", "application/json"))
        self.send_header("Content-Length", str(len(result.content)))
        self.end_headers()
        self.wfile.write(result.content)

    def log_message(self, *_args):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8300), Handler).serve_forever()
