"""Opt-in isolated normal-flow E2E; incomplete scenarios must never pass silently."""

# Credential literals in this module are synthetic E2E-only fixture values.

import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import time
import uuid

import pytest


enabled = os.environ.get("WORKLOG_NORMAL_E2E_ENABLED") == "1"
pytestmark = pytest.mark.skipif(not enabled, reason="WORKLOG_NORMAL_E2E_ENABLED=1 and isolated E2E service URLs are required")

EXPECTED = {
    "WORKLOG_NORMAL_E2E_API_URL": "http://api:8100/api",
    "WORKLOG_NORMAL_E2E_AI_URL": "http://ai:8200/ai",
    "WORKLOG_NORMAL_E2E_BROKER_URL": "redis://redis:6379/0",
    "PGVECTOR_DSN": "postgresql://e2e:e2e-only-password@postgres:5432/e2e",
    "LIGHTRAG_PG_DSN": "postgresql://e2e:e2e-only-password@postgres:5432/rag_e2e",
}
if enabled and (any(os.environ.get(key) != value for key, value in EXPECTED.items())
        or not Path("/.dockerenv").is_file()
        or not re.fullmatch(r"[0-9a-f]{64}", os.environ.get("WORKLOG_NORMAL_E2E_NETWORK_ID", ""))):
    pytest.fail("E2E requires the verified isolated compose runner and all explicit service URLs", pytrace=False)

import httpx
import psycopg
from psycopg import sql
import redis
from celery import Celery
from neo4j import GraphDatabase
from qdrant_client import QdrantClient, models


def _jwt():
    def encode(value):
        return base64.urlsafe_b64encode(value).rstrip(b"=")
    header = encode(b'{"alg":"HS256","typ":"JWT"}')
    body = encode(json.dumps({"sub": "1", "email": "fixture@example.invalid", "roleCode": "DIRECTOR",
                              "tokenType": "ACCESS", "exp": int(time.time()) + 300}).encode())
    unsigned = header + b"." + body
    signature = hmac.new(b"e2e-only-secret-abcdefghijklmnopqrstuvwxyz-1234567890", unsigned, hashlib.sha256).digest()
    return (unsigned + b"." + encode(signature)).decode()


def _query(sql, params=()):
    with psycopg.connect(os.environ["PGVECTOR_DSN"], options="-c default_transaction_read_only=on") as conn:
        return conn.execute(sql, params).fetchall()


def _post(client, path, *, expected, **kwargs):
    response = client.post(path, **kwargs)
    assert response.status_code == expected, f"{path}: HTTP {response.status_code}, expected {expected}"
    return response


def _new_fixture(client):
    suffix = uuid.uuid4().hex[:12]
    bootstrap = {"Authorization": "Bearer " + _jwt()}
    department_name = "E2E department " + suffix
    _post(client, "/departments", expected=201, headers=bootstrap, json={"departmentName": department_name})
    department_id = _query("select department_id from tb_department where department_name = %s", (department_name,))[0][0]
    signup = {"department_id": department_id, "user_name": "E2E fixture", "email": f"{suffix}@example.invalid",
              "password": "E2e-only-password-123!", "title_name": "본부장", "join_date": "2026-01-01", "employment_status": "ACTIVE"}
    _post(client, "/auth/signup", expected=201, files={"request": (None, json.dumps(signup), "application/json")})
    login = _post(client, "/auth/login", expected=200, json={"email": signup["email"], "password": signup["password"]})
    authorization = {"Authorization": login.headers["Authorization"]}
    user_id = _query("select user_id from tb_user where email = %s", (signup["email"],))[0][0]
    team = {"teamName": "E2E team " + suffix, "addAdmin": user_id,
            "addUsers": [{"userId": user_id, "isLeader": True, "teamRole": "fixture"}], "statusCode": "ACTIVE"}
    _post(client, "/teams", expected=201, headers=authorization, json=team)
    team_id = _query("select team_id from tb_team where team_name = %s", (team["teamName"],))[0][0]
    return authorization, team_id


def _create_worklog(client, authorization, team_id, title, content, tags=(), predecessors=()):
    worklog = {"teamId": team_id, "title": title, "workContent": content, "statusCode": "PENDING",
               "importanceCode": "HIGH", "actualHours": 1, "tagIds": list(tags), "predecessorWorklogIds": list(predecessors)}
    response = _post(client, "/worklogs", expected=201, headers=authorization,
                     files={"request": (None, json.dumps(worklog), "application/json")})
    return response.json()["data"]["worklogId"]


def _wait_index(worklog_id, request_id=None):
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        row = _query("select light_index_request_id, light_index_execution_id, light_index_status from tb_worklog where worklog_id=%s", (worklog_id,))[0]
        if (request_id is None or str(row[0]) == str(request_id)) and row[2] in ("COMPLETED", "FAILED"):
            assert row[2] == "COMPLETED", f"index failed for worklog {worklog_id}"
            return row
        time.sleep(0.5)
    raise AssertionError(f"index deadline expired for worklog {worklog_id}")


def _add_ai_tag(client, worklog_id):
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        task = _query("select request_id, input_hash, execution_state from tb_worklog_ai_post_process_outbox "
                      "where worklog_id=%s and task_kind='TAG'", (worklog_id,))[0]
        if task[2] == "QUEUED":
            break
        time.sleep(0.2)
    assert task[2] == "QUEUED"
    identity = {"requestId": str(task[0]), "executionId": str(uuid.uuid4()),
                "inputHash": task[1], "taskKind": "TAG", "targetId": worklog_id}
    internal = {"X-AI-Internal-Token": "e2e-only-internal-token"}
    started = _post(client, f"/internal/worklogs/{worklog_id}/ai-tasks/start", expected=200, headers=internal, json=identity).json()
    assert started["execute"]
    result = {**identity, "status": "COMPLETED", "existingTagIds": [],
              "newTags": [{"tagName": "E2E-" + uuid.uuid4().hex, "description": "E2E tag"}]}
    assert _post(client, f"/internal/worklogs/{worklog_id}/ai-tasks/result", expected=200, headers=internal, json=result).json()["accepted"]
    return _query("select max(tag_id) from tb_worklog_tag where worklog_id=%s", (worklog_id,))[0][0]


def _graph_edges(worklog_id, target, keyword):
    with GraphDatabase.driver("bolt://neo4j:7687", auth=("neo4j", "e2e-only-password")) as graph:
        with graph.session() as session:
            return [set(row["labels"]) for row in session.run(
                "MATCH (w)-[r]->(t) WHERE w.entity_id=$worklog AND t.entity_id=$target AND r.keywords CONTAINS $keyword RETURN labels(w) AS labels",
                worklog=f"Worklog:{worklog_id}", target=target, keyword=keyword)]


def _has_both_workspaces(labels, team_id):
    return any("axwms_pg_v1" in item for item in labels) and any(
        any(label.endswith(f"_team_{team_id}") for label in item) for item in labels)


def test_n1_create_and_index(record_property):
    with httpx.Client(base_url=EXPECTED["WORKLOG_NORMAL_E2E_API_URL"], timeout=10, trust_env=False) as client:
        authorization, team_id = _new_fixture(client)
        worklog = {"teamId": team_id, "title": "E2E fixture", "workContent": "E2E fixture content",
                   "statusCode": "PENDING", "importanceCode": "HIGH", "actualHours": 1,
                   "tagIds": [], "predecessorWorklogIds": []}
        last_id, is_called = _query("select last_value, is_called from tb_worklog_worklog_id_seq")[0]
        worklog_id = last_id + int(is_called)
        broker = redis.Redis.from_url(EXPECTED["WORKLOG_NORMAL_E2E_BROKER_URL"])
        barrier_key = f"e2e:prepared:{worklog_id}"
        broker.set(f"{barrier_key}:armed", "1", ex=120)
        try:
            created = _post(client, "/worklogs", expected=201, headers=authorization,
                            files={"request": (None, json.dumps(worklog), "application/json")})
            assert created.json()["data"]["worklogId"] == worklog_id
            inserted = _query("select w.xmin::text, o.xmin::text from tb_worklog w "
                              "join tb_worklog_light_outbox o using(worklog_id) where w.worklog_id=%s", (worklog_id,))
            assert len(inserted) == 1 and inserted[0][0] == inserted[0][1], "source and outbox insertion transaction was not observed"
            assert broker.blpop(f"{barrier_key}:arrived", timeout=45), "prepared barrier timed out"
            pending = _query("select w.light_index_request_id, w.light_index_status, o.operation, o.revision, o.request_id "
                             "from tb_worklog w join tb_worklog_light_outbox o using(worklog_id) where w.worklog_id=%s", (worklog_id,))
            assert len(pending) == 1
            initial_request, status, operation, revision, outbox_request = pending[0]
            assert status == "RUNNING" and operation == "INDEX" and revision == 1 and initial_request == outbox_request
            record_property("outbox_snapshot", f"initial={operation},revision={revision},status={status};final_count=0")
            assert _query("select count(*) from tb_notification where ai_request_id=%s and ai_result='COMPLETED'", (initial_request,))[0][0] == 0
            active = Celery(broker=EXPECTED["WORKLOG_NORMAL_E2E_BROKER_URL"]).control.inspect(timeout=3).active() or {}
            matching = [task for tasks in active.values() for task in tasks if task.get("name") == "worklog.light_index"
                        and task.get("args") == [worklog_id, str(initial_request), "INDEX"]]
            assert len(matching) == 1
            active_task_id = matching[0]["id"]
            accepted = broker.blpop(f"e2e:accepted:{worklog_id}", timeout=15)
            assert accepted, "AI HTTP acceptance was not recorded"
            event = json.loads(accepted[1])
            assert event == {"status": 202, "operation": "INDEX", "worklog_ids": [worklog_id],
                             "request_id": str(initial_request), "task_ids": [active_task_id]}
        finally:
            broker.delete(f"{barrier_key}:armed")
            broker.rpush(f"{barrier_key}:release", "1")
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            row = _query("select light_index_request_id, light_index_execution_id, light_index_status from tb_worklog where worklog_id=%s", (worklog_id,))[0]
            if row[2] in ("COMPLETED", "FAILED"):
                break
            time.sleep(0.5)
        assert row[2] == "COMPLETED", f"N1 status={row[2]} request={row[0]} execution={row[1]}"
        request_id, execution_id, _ = row
        record_property("worklog_id", worklog_id)
        record_property("request_id", str(request_id))
        record_property("execution_id", str(execution_id))
        assert _query("select count(*) from tb_worklog_light_outbox where worklog_id=%s", (worklog_id,))[0][0] == 0
        assert _query("select count(*) from tb_notification where ai_request_id=%s and ai_task_kind='INDEX' and ai_result='COMPLETED'", (request_id,))[0][0] == 1

        with psycopg.connect(os.environ["LIGHTRAG_PG_DSN"], options="-c default_transaction_read_only=on") as conn:
            statuses = conn.execute("select workspace, status from lightrag_doc_status where id=%s", (f"worklog-{worklog_id}",)).fetchall()
        workspaces = {workspace for workspace, status in statuses if status == "processed"}
        assert "axwms_pg_v1" in workspaces
        team_workspaces = {workspace for workspace in workspaces if workspace.endswith(f"_team_{team_id}")}
        assert len(team_workspaces) == 1
        team_workspace = team_workspaces.pop()
        with GraphDatabase.driver("bolt://neo4j:7687", auth=("neo4j", "e2e-only-password")) as graph:
            with graph.session() as session:
                labels = [set(item["labels"]) for item in session.run(
                    "MATCH (n) WHERE n.entity_id=$name RETURN labels(n) AS labels", name=f"Worklog:{worklog_id}")]
        assert any("axwms_pg_v1" in item for item in labels)
        assert any(team_workspace in item for item in labels)
        qdrant = QdrantClient(url="http://qdrant:6333")
        for workspace in ("axwms_pg_v1", team_workspace):
            points, _ = qdrant.scroll("lightrag_vdb_chunks_e2e_deterministic_768d", limit=1, with_payload=False,
                scroll_filter=models.Filter(must=[
                    models.FieldCondition(key="workspace_id", match=models.MatchValue(value=workspace)),
                    models.FieldCondition(key="full_doc_id", match=models.MatchValue(value=f"worklog-{worklog_id}")),
                ]))
            assert points, f"missing chunk vector in {workspace}"

        deadline = time.monotonic() + 10
        task_id = None
        while time.monotonic() < deadline and task_id is None:
            for key in broker.scan_iter(match="celery-task-meta-*", count=100):
                task = json.loads(broker.get(key))
                result = task.get("result") or {}
                if isinstance(result, dict) and result.get("request_id") == str(request_id):
                    assert task["status"] == "SUCCESS" and result["status"] == "COMPLETED"
                    assert result["worklog_id"] == worklog_id and result["execution_id"] == str(execution_id)
                    task_id = key.decode().removeprefix("celery-task-meta-")
                    break
            if task_id is None:
                time.sleep(0.2)
        assert task_id, "Celery result was not recorded before the deadline"
        assert task_id == active_task_id
        record_property("task_id", task_id)


def test_n2_replacement_after_prepared_source(record_property):
    broker = redis.Redis.from_url(EXPECTED["WORKLOG_NORMAL_E2E_BROKER_URL"])
    with httpx.Client(base_url=EXPECTED["WORKLOG_NORMAL_E2E_API_URL"], timeout=10, trust_env=False) as client:
        authorization, team_id = _new_fixture(client)
        last_id, is_called = _query("select last_value, is_called from tb_worklog_worklog_id_seq")[0]
        worklog_id = last_id + int(is_called)
        prepared, callback, dispatch = (f"e2e:{phase}:{worklog_id}" for phase in ("prepared", "callback", "dispatch"))
        broker.set(f"{prepared}:armed", "1", ex=120)
        broker.set(f"{callback}:armed", "1", ex=120)
        try:
            worklog = {"teamId": team_id, "title": "E2E old", "workContent": "E2E old content",
                       "statusCode": "PENDING", "importanceCode": "HIGH", "actualHours": 1,
                       "tagIds": [], "predecessorWorklogIds": []}
            created = _post(client, "/worklogs", expected=201, headers=authorization,
                            files={"request": (None, json.dumps(worklog), "application/json")})
            assert created.json()["data"]["worklogId"] == worklog_id
            assert broker.blpop(f"{prepared}:arrived", timeout=45), "A source barrier timed out"
            accepted = broker.blpop(f"e2e:accepted:{worklog_id}", timeout=15)
            assert accepted, "A acceptance missing"
            event_a = json.loads(accepted[1])
            request_a = event_a["request_id"]
            task_a = event_a["task_ids"][0]
            assert event_a["operation"] == "INDEX" and event_a["worklog_ids"] == [worklog_id]

            broker.set(f"{dispatch}:armed", "1", ex=120)
            response = client.patch(f"/worklogs/{worklog_id}", headers=authorization,
                                    files={"request": (None, json.dumps({"workContent": "E2E new content"}), "application/json")})
            assert response.status_code == 200
            current = _query("select w.light_index_request_id, w.light_index_status, w.light_index_execution_id, "
                             "o.operation, o.revision, o.attempts from tb_worklog w "
                             "join tb_worklog_light_outbox o using(worklog_id) where w.worklog_id=%s", (worklog_id,))[0]
            request_b = str(current[0])
            assert request_b != request_a and current[1] == "PENDING" and current[3] == "REINDEX" and current[4] == 2
            record_property("outbox_snapshot", f"B={current[3]},revision={current[4]},status={current[1]},attempts={current[5]};final_count=0")
            source_updated = _query("select work_content=%s from tb_worklog where worklog_id=%s", ("E2E new content", worklog_id))[0][0]
            assert source_updated, "B source commit was not observed"

            broker.delete(f"{prepared}:armed")
            broker.rpush(f"{prepared}:release", "1")
            assert broker.blpop(f"{callback}:arrived", timeout=50), "A callback barrier timed out"
            with psycopg.connect(os.environ["LIGHTRAG_PG_DSN"], options="-c default_transaction_read_only=on") as conn:
                old = conn.execute("select workspace, content like %s from lightrag_doc_full where id=%s",
                                   ("%E2E old content%", f"worklog-{worklog_id}")).fetchall()
            assert len(old) == 2 and all(item[1] for item in old), "A did not publish its prepared old source"
            broker.delete(f"{callback}:armed")
            broker.rpush(f"{callback}:release", "1")
            deadline = time.monotonic() + 20
            result_a = None
            while time.monotonic() < deadline:
                raw = broker.get(f"celery-task-meta-{task_a}")
                if raw:
                    result_a = json.loads(raw)
                    break
                time.sleep(0.2)
            assert result_a and result_a["status"] == "SUCCESS" and result_a["result"]["status"] == "COMPLETED"
            assert _query("select count(*) from tb_notification where ai_request_id=%s", (request_a,))[0][0] == 0
            after_a = _query("select w.light_index_request_id, w.light_index_status, w.light_index_execution_id, "
                             "o.operation, o.revision, o.attempts from tb_worklog w "
                             "join tb_worklog_light_outbox o using(worklog_id) where w.worklog_id=%s", (worklog_id,))[0]
            assert after_a[:-1] == current[:-1], "A result changed B authority or outbox"
            arrived_b = broker.blpop(f"{dispatch}:arrived", timeout=45)
            assert arrived_b and arrived_b[1].decode() == request_b
            broker.delete(f"{dispatch}:armed")
            broker.rpush(f"{dispatch}:release", "1")
            accepted_b = broker.blpop(f"e2e:accepted:{worklog_id}", timeout=20)
            assert accepted_b, "B acceptance missing"
            event_b = json.loads(accepted_b[1])
            assert event_b["operation"] == "REINDEX" and event_b["request_id"] == request_b
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                final = _query("select light_index_request_id, light_index_execution_id, light_index_status from tb_worklog where worklog_id=%s", (worklog_id,))[0]
                if str(final[0]) == request_b and final[2] in ("COMPLETED", "FAILED"):
                    break
                time.sleep(0.5)
            assert final[2] == "COMPLETED"
            with psycopg.connect(os.environ["LIGHTRAG_PG_DSN"], options="-c default_transaction_read_only=on") as conn:
                latest = conn.execute("select workspace, content like %s from lightrag_doc_full where id=%s",
                                      ("%E2E new content%", f"worklog-{worklog_id}")).fetchall()
            assert len(latest) == 2 and all(item[1] for item in latest), "B latest source missing in global/team storage"
            assert _query("select count(*) from tb_notification where ai_request_id=%s and ai_result='COMPLETED'", (request_b,))[0][0] == 1
            assert _query("select count(*) from tb_worklog_light_outbox where worklog_id=%s", (worklog_id,))[0][0] == 0
            for key, value in {"worklog_id": worklog_id, "request_a": request_a, "task_a": task_a,
                               "execution_a": result_a["result"]["execution_id"], "request_b": request_b,
                               "task_b": event_b["task_ids"][0], "execution_b": final[1]}.items():
                record_property(key, str(value))
        finally:
            for prefix in (prepared, callback, dispatch):
                broker.delete(f"{prefix}:armed")
                broker.rpush(f"{prefix}:release", "1")


def test_n3_intermediate_request_is_skipped(record_property):
    broker = redis.Redis.from_url(EXPECTED["WORKLOG_NORMAL_E2E_BROKER_URL"])
    with httpx.Client(base_url=EXPECTED["WORKLOG_NORMAL_E2E_API_URL"], timeout=10, trust_env=False) as client:
        authorization, team_id = _new_fixture(client)
        last_id, is_called = _query("select last_value, is_called from tb_worklog_worklog_id_seq")[0]
        worklog_id = last_id + int(is_called)
        prepared = f"e2e:prepared:{worklog_id}"
        start_c = None
        broker.set(f"{prepared}:armed", "1", ex=120)
        try:
            worklog = {"teamId": team_id, "title": "E2E A", "workContent": "E2E A content",
                       "statusCode": "PENDING", "importanceCode": "HIGH", "actualHours": 1,
                       "tagIds": [], "predecessorWorklogIds": []}
            created = _post(client, "/worklogs", expected=201, headers=authorization,
                            files={"request": (None, json.dumps(worklog), "application/json")})
            assert created.json()["data"]["worklogId"] == worklog_id
            assert broker.blpop(f"{prepared}:arrived", timeout=45), "A source barrier timed out"
            accepted_a = broker.blpop(f"e2e:accepted:{worklog_id}", timeout=15)
            assert accepted_a
            event_a = json.loads(accepted_a[1])

            response = client.patch(f"/worklogs/{worklog_id}", headers=authorization,
                                    files={"request": (None, json.dumps({"workContent": "E2E B content"}), "application/json")})
            assert response.status_code == 200
            request_b = str(_query("select light_index_request_id from tb_worklog where worklog_id=%s", (worklog_id,))[0][0])
            accepted_b = broker.blpop(f"e2e:accepted:{worklog_id}", timeout=40)
            assert accepted_b, "B was not published"
            event_b = json.loads(accepted_b[1])
            assert event_b["request_id"] == request_b and event_b["operation"] == "REINDEX"
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                claim = _query("select claim_token is null from tb_worklog_light_outbox where worklog_id=%s", (worklog_id,))[0][0]
                if claim:
                    break
                time.sleep(0.1)
            assert claim, "B dispatch did not commit"

            response = client.patch(f"/worklogs/{worklog_id}", headers=authorization,
                                    files={"request": (None, json.dumps({"workContent": "E2E C content"}), "application/json")})
            assert response.status_code == 200
            current = _query("select w.light_index_request_id, w.light_index_status, o.revision, o.attempts "
                             "from tb_worklog w join tb_worklog_light_outbox o using(worklog_id) where w.worklog_id=%s", (worklog_id,))[0]
            request_c = str(current[0])
            assert request_c not in (event_a["request_id"], request_b) and current[1:3] == ("PENDING", 3) and current[3] in (0, 1)
            record_property("outbox_snapshot", f"C=REINDEX,revision={current[2]},status={current[1]},attempts={current[3]};final_count=0")
            start_c = f"e2e:start:{request_c}"
            broker.set(f"{start_c}:armed", "1", ex=120)
            accepted_c = broker.blpop(f"e2e:accepted:{worklog_id}", timeout=40)
            assert accepted_c, "C was not published"
            event_c = json.loads(accepted_c[1])
            assert event_c["request_id"] == request_c and event_c["operation"] == "REINDEX"
            broker.delete(f"{prepared}:armed")
            broker.rpush(f"{prepared}:release", "1")
            assert broker.blpop(f"{start_c}:arrived", timeout=50), "C start barrier timed out"
            raw_b = broker.get(f"celery-task-meta-{event_b['task_ids'][0]}")
            assert raw_b
            result_b = json.loads(raw_b)
            assert result_b["status"] == "SUCCESS" and result_b["result"]["status"] == "SKIPPED"
            raw_a = broker.get(f"celery-task-meta-{event_a['task_ids'][0]}")
            assert raw_a and json.loads(raw_a)["result"]["status"] == "COMPLETED"
            result_a = json.loads(raw_a)
            assert _query("select count(*) from tb_notification where ai_request_id in (%s,%s)",
                          (event_a["request_id"], request_b))[0][0] == 0
            after_b = _query("select w.light_index_request_id, w.light_index_status, o.revision, o.attempts "
                             "from tb_worklog w join tb_worklog_light_outbox o using(worklog_id) where w.worklog_id=%s", (worklog_id,))[0]
            assert after_b[:3] == current[:3] and after_b[3] == 1, "A or B changed C authority"
            broker.delete(f"{start_c}:armed")
            broker.rpush(f"{start_c}:release", "1")
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                final = _query("select light_index_request_id, light_index_execution_id, light_index_status from tb_worklog where worklog_id=%s", (worklog_id,))[0]
                if str(final[0]) == request_c and final[2] in ("COMPLETED", "FAILED"):
                    break
                time.sleep(0.5)
            assert final[2] == "COMPLETED"
            assert _query("select count(*) from tb_notification where ai_request_id=%s and ai_result='COMPLETED'", (request_c,))[0][0] == 1
            assert _query("select count(*) from tb_worklog_light_outbox where worklog_id=%s", (worklog_id,))[0][0] == 0
            for key, value in {"worklog_id": worklog_id, "request_a": event_a["request_id"], "task_a": event_a["task_ids"][0],
                               "execution_a": result_a["result"]["execution_id"], "request_b": request_b,
                               "task_b": event_b["task_ids"][0], "execution_b": result_b["result"]["execution_id"], "request_c": request_c,
                               "task_c": event_c["task_ids"][0], "execution_c": final[1]}.items():
                record_property(key, str(value))
        finally:
            for prefix in (prepared, start_c):
                if prefix:
                    broker.delete(f"{prefix}:armed")
                    broker.rpush(f"{prefix}:release", "1")


@pytest.mark.parametrize("ordering", ["early", "late"])
def test_n8_tag_commit_reindexes(ordering, record_property):
    broker = redis.Redis.from_url(EXPECTED["WORKLOG_NORMAL_E2E_BROKER_URL"])
    with httpx.Client(base_url=EXPECTED["WORKLOG_NORMAL_E2E_API_URL"], timeout=10, trust_env=False) as client:
        authorization, team_id = _new_fixture(client)
        last_id, is_called = _query("select last_value, is_called from tb_worklog_worklog_id_seq")[0]
        worklog_id = last_id + int(is_called)
        prestart = f"e2e:prestart:{worklog_id}"
        if ordering == "early":
            broker.set(f"{prestart}:armed", "1", ex=120)
        try:
            worklog = {"teamId": team_id, "title": "E2E TAG", "workContent": "E2E TAG content",
                       "statusCode": "PENDING", "importanceCode": "HIGH", "actualHours": 1,
                       "tagIds": [], "predecessorWorklogIds": []}
            created = _post(client, "/worklogs", expected=201, headers=authorization,
                            files={"request": (None, json.dumps(worklog), "application/json")})
            assert created.json()["data"]["worklogId"] == worklog_id
            initial_request = str(_query("select light_index_request_id from tb_worklog where worklog_id=%s", (worklog_id,))[0][0])
            if ordering == "early":
                assert broker.blpop(f"{prestart}:arrived", timeout=45), "initial index pre-start barrier timed out"
            else:
                deadline = time.monotonic() + 120
                while time.monotonic() < deadline:
                    state = _query("select light_index_status from tb_worklog where worklog_id=%s", (worklog_id,))[0][0]
                    if state in ("COMPLETED", "FAILED"):
                        break
                    time.sleep(0.5)
                assert state == "COMPLETED"
                assert _query("select count(*) from tb_notification where ai_request_id=%s and ai_result='COMPLETED'", (initial_request,))[0][0] == 1

            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                task = _query("select request_id, input_hash, execution_state from tb_worklog_ai_post_process_outbox "
                              "where worklog_id=%s and task_kind='TAG'", (worklog_id,))[0]
                if task[2] == "QUEUED":
                    break
                time.sleep(0.2)
            assert task[2] == "QUEUED"
            tag_request, input_hash, _ = task
            execution_id = str(uuid.uuid4())
            identity = {"requestId": str(tag_request), "executionId": execution_id,
                        "inputHash": input_hash, "taskKind": "TAG", "targetId": worklog_id}
            internal_headers = {"X-AI-Internal-Token": "e2e-only-internal-token"}
            start = _post(client, f"/internal/worklogs/{worklog_id}/ai-tasks/start", expected=200,
                          headers=internal_headers, json=identity).json()
            assert start["execute"]
            result = {**identity, "status": "COMPLETED", "existingTagIds": [],
                      "newTags": [{"tagName": "E2E-" + uuid.uuid4().hex, "description": "E2E tag"}]}
            applied = _post(client, f"/internal/worklogs/{worklog_id}/ai-tasks/result", expected=200,
                            headers=internal_headers, json=result).json()
            assert applied["accepted"]
            tag_ids = [item[0] for item in _query("select tag_id from tb_worklog_tag where worklog_id=%s order by tag_id", (worklog_id,))]
            assert len(tag_ids) == 1
            updated = _query("select w.light_index_request_id, o.operation from tb_worklog w "
                             "join tb_worklog_light_outbox o using(worklog_id) where w.worklog_id=%s", (worklog_id,))[0]
            request_b = str(updated[0])
            assert request_b != initial_request and updated[1] == "REINDEX"
            record_property("outbox_snapshot", f"latest={updated[1]};final_count=0")
            if ordering == "early":
                broker.delete(f"{prestart}:armed")
                broker.rpush(f"{prestart}:release", "1")

            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                final = _query("select light_index_request_id, light_index_execution_id, light_index_status from tb_worklog where worklog_id=%s", (worklog_id,))[0]
                if str(final[0]) == request_b and final[2] in ("COMPLETED", "FAILED"):
                    break
                time.sleep(0.5)
            assert str(final[0]) == request_b and final[2] == "COMPLETED"
            assert _query("select count(*) from tb_notification where ai_request_id=%s and ai_result='COMPLETED'", (request_b,))[0][0] == 1
            assert _query("select count(*) from tb_notification where ai_request_id=%s and ai_result='COMPLETED'", (initial_request,))[0][0] == (0 if ordering == "early" else 1)
            with GraphDatabase.driver("bolt://neo4j:7687", auth=("neo4j", "e2e-only-password")) as graph:
                with graph.session() as session:
                    labels = [set(row["labels"]) for row in session.run(
                        "MATCH (w)-[r]->(t) WHERE w.entity_id=$worklog AND t.entity_id=$tag AND r.keywords CONTAINS 'HAS_TAG' RETURN labels(w) AS labels",
                        worklog=f"Worklog:{worklog_id}", tag=f"Tag:{tag_ids[0]}")]
                    actual_tags = [(set(row["labels"]), row["tag"]) for row in session.run(
                        "MATCH (w)-[r]->(t) WHERE w.entity_id=$worklog AND r.keywords CONTAINS 'HAS_TAG' RETURN labels(w) AS labels, t.entity_id AS tag",
                        worklog=f"Worklog:{worklog_id}")]
            assert any("axwms_pg_v1" in labels_for_node for labels_for_node in labels)
            assert any(any(label.endswith(f"_team_{team_id}") for label in labels_for_node) for labels_for_node in labels)
            assert {tag for labels_for_node, tag in actual_tags if "axwms_pg_v1" in labels_for_node} == {f"Tag:{tag_ids[0]}"}
            assert {tag for labels_for_node, tag in actual_tags if any(label.endswith(f"_team_{team_id}") for label in labels_for_node)} == {f"Tag:{tag_ids[0]}"}
            duplicate = _post(client, f"/internal/worklogs/{worklog_id}/ai-tasks/result", expected=200,
                              headers=internal_headers, json=result).json()
            assert duplicate["accepted"]
            assert str(_query("select light_index_request_id from tb_worklog where worklog_id=%s", (worklog_id,))[0][0]) == request_b
            assert _query("select count(*) from tb_worklog_light_outbox where worklog_id=%s", (worklog_id,))[0][0] == 0
            accepted = []
            while raw := broker.lpop(f"e2e:accepted:{worklog_id}"):
                accepted.append(json.loads(raw))
            initial_tasks = [event["task_ids"][0] for event in accepted if event["request_id"] == initial_request]
            reindex_tasks = [event["task_ids"][0] for event in accepted if event["request_id"] == request_b]
            assert len(initial_tasks) == len(reindex_tasks) == 1
            for key, value in {"worklog_id": worklog_id, "initial_request": initial_request, "tag_request": tag_request,
                               "initial_task": initial_tasks[0], "tag_execution": execution_id, "reindex_request": request_b,
                               "reindex_task": reindex_tasks[0], "reindex_execution": final[1]}.items():
                record_property(key, str(value))
        finally:
            if ordering == "early":
                broker.delete(f"{prestart}:armed")
                broker.rpush(f"{prestart}:release", "1")


@pytest.mark.parametrize("ordering", ["result_first", "replacement_first"])
def test_n6_callback_commit_order(ordering, record_property):
    broker = redis.Redis.from_url(EXPECTED["WORKLOG_NORMAL_E2E_BROKER_URL"])
    with httpx.Client(base_url=EXPECTED["WORKLOG_NORMAL_E2E_API_URL"], timeout=10, trust_env=False) as client:
        authorization, team_id = _new_fixture(client)
        last_id, is_called = _query("select last_value, is_called from tb_worklog_worklog_id_seq")[0]
        worklog_id = last_id + int(is_called)
        callback, dispatch = (f"e2e:{phase}:{worklog_id}" for phase in ("callback", "dispatch"))
        broker.set(f"{callback}:armed", "1", ex=120)
        try:
            worklog = {"teamId": team_id, "title": "E2E callback A", "workContent": "E2E callback content",
                       "statusCode": "PENDING", "importanceCode": "HIGH", "actualHours": 1,
                       "tagIds": [], "predecessorWorklogIds": []}
            created = _post(client, "/worklogs", expected=201, headers=authorization,
                            files={"request": (None, json.dumps(worklog), "application/json")})
            assert created.json()["data"]["worklogId"] == worklog_id
            assert broker.blpop(f"{callback}:arrived", timeout=60), "A callback barrier timed out"
            accepted_a = broker.blpop(f"e2e:accepted:{worklog_id}", timeout=10)
            assert accepted_a
            event_a = json.loads(accepted_a[1])
            request_a = event_a["request_id"]
            record_property("outbox_snapshot", "initial=INDEX;replacement=REINDEX;final_count=0")
            with psycopg.connect(os.environ["LIGHTRAG_PG_DSN"], options="-c default_transaction_read_only=on") as conn:
                count = conn.execute("select count(*) from lightrag_doc_status where id=%s and status='processed'", (f"worklog-{worklog_id}",)).fetchone()[0]
            assert count == 2, "A global/team storage was not completed before callback"

            if ordering == "replacement_first":
                broker.set(f"{dispatch}:armed", "1", ex=120)
                response = client.patch(f"/worklogs/{worklog_id}", headers=authorization,
                                        files={"request": (None, json.dumps({"title": "E2E callback B"}), "application/json")})
                assert response.status_code == 200
                snapshot = _query("select w.light_index_request_id, w.light_index_status, w.light_index_execution_id, "
                                  "o.operation, o.revision, o.attempts from tb_worklog w "
                                  "join tb_worklog_light_outbox o using(worklog_id) where w.worklog_id=%s", (worklog_id,))[0]
                request_b = str(snapshot[0])
                assert request_b != request_a and snapshot[1] == "PENDING"

            broker.delete(f"{callback}:armed")
            broker.rpush(f"{callback}:release", "1")
            deadline = time.monotonic() + 20
            result_a = None
            while time.monotonic() < deadline:
                raw = broker.get(f"celery-task-meta-{event_a['task_ids'][0]}")
                if raw:
                    result_a = json.loads(raw)
                    break
                time.sleep(0.2)
            assert result_a and result_a["status"] == "SUCCESS" and result_a["result"]["status"] == "COMPLETED"
            assert _query("select count(*) from tb_notification where ai_request_id=%s and ai_result='COMPLETED'", (request_a,))[0][0] == (1 if ordering == "result_first" else 0)

            if ordering == "result_first":
                assert _query("select light_index_status from tb_worklog where worklog_id=%s", (worklog_id,))[0][0] == "COMPLETED"
                response = client.patch(f"/worklogs/{worklog_id}", headers=authorization,
                                        files={"request": (None, json.dumps({"title": "E2E callback B"}), "application/json")})
                assert response.status_code == 200
                request_b = str(_query("select light_index_request_id from tb_worklog where worklog_id=%s", (worklog_id,))[0][0])
                assert request_b != request_a
            else:
                after = _query("select w.light_index_request_id, w.light_index_status, w.light_index_execution_id, "
                               "o.operation, o.revision, o.attempts from tb_worklog w "
                               "join tb_worklog_light_outbox o using(worklog_id) where w.worklog_id=%s", (worklog_id,))[0]
                assert after[:-1] == snapshot[:-1]
                arrived = broker.blpop(f"{dispatch}:arrived", timeout=45)
                assert arrived and arrived[1].decode() == request_b
                broker.delete(f"{dispatch}:armed")
                broker.rpush(f"{dispatch}:release", "1")

            accepted_b = broker.blpop(f"e2e:accepted:{worklog_id}", timeout=45)
            assert accepted_b
            event_b = json.loads(accepted_b[1])
            assert event_b["request_id"] == request_b and event_b["operation"] == "REINDEX"
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                final = _query("select light_index_request_id, light_index_execution_id, light_index_status from tb_worklog where worklog_id=%s", (worklog_id,))[0]
                if str(final[0]) == request_b and final[2] in ("COMPLETED", "FAILED"):
                    break
                time.sleep(0.5)
            assert final[2] == "COMPLETED"
            assert _query("select count(*) from tb_notification where ai_request_id=%s and ai_result='COMPLETED'", (request_b,))[0][0] == 1
            assert _query("select count(*) from tb_notification where ai_request_id=%s and ai_result='COMPLETED'", (request_a,))[0][0] == (1 if ordering == "result_first" else 0)
            internal = {"X-AI-Internal-Token": "e2e-only-internal-token"}
            _post(client, f"/internal/worklogs/{worklog_id}/light-index/result", expected=200, headers=internal,
                  json={"requestId": request_b, "executionId": str(final[1]), "status": "COMPLETED", "error": None})
            assert _query("select light_index_request_id, light_index_execution_id, light_index_status from tb_worklog where worklog_id=%s", (worklog_id,))[0] == final
            assert _query("select count(*) from tb_notification where ai_request_id=%s and ai_result='COMPLETED'", (request_b,))[0][0] == 1
            assert _query("select count(*) from tb_worklog_light_outbox where worklog_id=%s", (worklog_id,))[0][0] == 0
            for key, value in {"worklog_id": worklog_id, "request_a": request_a, "task_a": event_a["task_ids"][0],
                               "execution_a": result_a["result"]["execution_id"], "request_b": request_b,
                               "task_b": event_b["task_ids"][0], "execution_b": final[1]}.items():
                record_property(key, str(value))
        finally:
            for prefix in (callback, dispatch):
                broker.delete(f"{prefix}:armed")
                broker.rpush(f"{prefix}:release", "1")


def test_n4_reindex_preserves_shared_graph(record_property):
    broker = redis.Redis.from_url(EXPECTED["WORKLOG_NORMAL_E2E_BROKER_URL"])
    with httpx.Client(base_url=EXPECTED["WORKLOG_NORMAL_E2E_API_URL"], timeout=10, trust_env=False) as client:
        authorization, team_id = _new_fixture(client)
        other_id = _create_worklog(client, authorization, team_id, "E2E shared D", "E2E shared content")
        _wait_index(other_id)
        shared_tag = _add_ai_tag(client, other_id)
        assert shared_tag is not None
        request_d = _query("select light_index_request_id from tb_worklog where worklog_id=%s", (other_id,))[0][0]
        _wait_index(other_id, request_d)
        worklog_id = _create_worklog(client, authorization, team_id, "E2E old title", "E2E old text", [shared_tag], [other_id])
        _wait_index(worklog_id)
        new_tag = _add_ai_tag(client, worklog_id)
        assert new_tag is not None and new_tag != shared_tag
        request_tag = _query("select light_index_request_id from tb_worklog where worklog_id=%s", (worklog_id,))[0][0]
        _wait_index(worklog_id, request_tag)
        author_id = _query("select author_id from tb_worklog where worklog_id=%s", (other_id,))[0][0]
        for target, keyword in ((f"User:{author_id}", "AUTHORED_BY"), (f"Team:{team_id}", "BELONGS_TO"), (f"Tag:{shared_tag}", "HAS_TAG")):
            assert _has_both_workspaces(_graph_edges(other_id, target, keyword), team_id)
            assert _has_both_workspaces(_graph_edges(worklog_id, target, keyword), team_id)
        assert _has_both_workspaces(_graph_edges(worklog_id, f"Worklog:{other_id}", "DEPENDS_ON"), team_id)
        patch = {"title": "E2E latest title", "workContent": "E2E latest text", "tagIds": [new_tag],
                 "removeTagIds": [shared_tag], "predecessorWorklogIds": []}
        response = client.patch(f"/worklogs/{worklog_id}", headers=authorization,
                                files={"request": (None, json.dumps(patch), "application/json")})
        assert response.status_code == 200
        latest_request = _query("select light_index_request_id from tb_worklog where worklog_id=%s", (worklog_id,))[0][0]
        assert latest_request != request_tag
        final = _wait_index(worklog_id, latest_request)
        assert _query("select tag_id from tb_worklog_tag where worklog_id=%s order by tag_id", (worklog_id,)) == [(new_tag,)]
        assert _graph_edges(worklog_id, f"Tag:{shared_tag}", "HAS_TAG") == []
        assert _graph_edges(worklog_id, f"Worklog:{other_id}", "DEPENDS_ON") == []
        assert _has_both_workspaces(_graph_edges(worklog_id, f"Tag:{new_tag}", "HAS_TAG"), team_id)
        for target, keyword in ((f"User:{author_id}", "AUTHORED_BY"), (f"Team:{team_id}", "BELONGS_TO"), (f"Tag:{shared_tag}", "HAS_TAG")):
            assert _has_both_workspaces(_graph_edges(other_id, target, keyword), team_id), "shared D relationship was damaged"
        with psycopg.connect(os.environ["LIGHTRAG_PG_DSN"], options="-c default_transaction_read_only=on") as conn:
            docs = conn.execute("select id, content like %s, content like %s from lightrag_doc_full where id in (%s,%s)",
                                ("%E2E latest text%", "%E2E old text%", f"worklog-{worklog_id}", f"worklog-{other_id}")).fetchall()
            workspaces = [item[0] for item in conn.execute("select workspace from lightrag_doc_status where id=%s", (f"worklog-{worklog_id}",)).fetchall()]
        assert len(docs) == 4
        assert all(latest and not old for doc_id, latest, old in docs if doc_id == f"worklog-{worklog_id}")
        assert sum(doc_id == f"worklog-{other_id}" for doc_id, _, _ in docs) == 2
        qdrant = QdrantClient(url="http://qdrant:6333")
        for workspace in workspaces:
            for doc_id in (worklog_id, other_id):
                points, _ = qdrant.scroll("lightrag_vdb_chunks_e2e_deterministic_768d", limit=100, with_payload=True,
                    scroll_filter=models.Filter(must=[
                        models.FieldCondition(key="workspace_id", match=models.MatchValue(value=workspace)),
                        models.FieldCondition(key="full_doc_id", match=models.MatchValue(value=f"worklog-{doc_id}")),
                    ]))
                assert points, "missing document vectors"
                if doc_id == worklog_id:
                    contents = [(point.payload or {}).get("content", "") for point in points]
                    has_latest = any("E2E latest text" in value for value in contents)
                    has_old = any("E2E old text" in value for value in contents)
                    assert has_latest and not has_old, "Qdrant does not contain only the latest source"
        assert _query("select count(*) from tb_notification where ai_request_id=%s and ai_result='COMPLETED'", (latest_request,))[0][0] == 1
        assert _query("select count(*) from tb_worklog_light_outbox where worklog_id=%s", (worklog_id,))[0][0] == 0
        events = []
        while raw := broker.lpop(f"e2e:accepted:{worklog_id}"):
            events.append(json.loads(raw))
        matching = [event for event in events if event["request_id"] == str(latest_request)]
        assert len(matching) == 1 and matching[0]["operation"] == "REINDEX"
        record_property("outbox_snapshot", "latest=REINDEX;final_count=0")
        for key, value in {"worklog_id": worklog_id, "shared_worklog_id": other_id, "request_id": latest_request,
                           "task_id": matching[0]["task_ids"][0], "execution_id": final[1]}.items():
            record_property(key, str(value))


def test_n5_delete_after_prepared_source(record_property):
    broker = redis.Redis.from_url(EXPECTED["WORKLOG_NORMAL_E2E_BROKER_URL"])
    with httpx.Client(base_url=EXPECTED["WORKLOG_NORMAL_E2E_API_URL"], timeout=10, trust_env=False) as client:
        authorization, team_id = _new_fixture(client)
        other_id = _create_worklog(client, authorization, team_id, "E2E delete D", "E2E shared content")
        _wait_index(other_id)
        shared_tag = _add_ai_tag(client, other_id)
        request_d = _query("select light_index_request_id from tb_worklog where worklog_id=%s", (other_id,))[0][0]
        _wait_index(other_id, request_d)
        author_id = _query("select author_id from tb_worklog where worklog_id=%s", (other_id,))[0][0]
        last_id, is_called = _query("select last_value, is_called from tb_worklog_worklog_id_seq")[0]
        worklog_id = last_id + int(is_called)
        prepared = f"e2e:prepared:{worklog_id}"
        broker.set(f"{prepared}:armed", "1", ex=120)
        try:
            created = _create_worklog(client, authorization, team_id, "E2E delete A", "E2E delete content", [shared_tag])
            assert created == worklog_id
            assert broker.blpop(f"{prepared}:arrived", timeout=45), "A source barrier timed out"
            accepted_a = broker.blpop(f"e2e:accepted:{worklog_id}", timeout=15)
            assert accepted_a
            event_a = json.loads(accepted_a[1])
            response = client.patch(f"/worklogs/{worklog_id}", headers=authorization,
                                    files={"request": (None, json.dumps({"title": "E2E delete B"}), "application/json")})
            assert response.status_code == 200
            request_b = str(_query("select light_index_request_id from tb_worklog where worklog_id=%s", (worklog_id,))[0][0])
            accepted_b = broker.blpop(f"e2e:accepted:{worklog_id}", timeout=40)
            assert accepted_b
            event_b = json.loads(accepted_b[1])
            assert event_b["request_id"] == request_b and event_b["operation"] == "REINDEX"
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                released = _query("select claim_token is null from tb_worklog_light_outbox where worklog_id=%s", (worklog_id,))[0][0]
                if released:
                    break
                time.sleep(0.1)
            assert released
            response = client.delete(f"/worklogs/{worklog_id}", headers=authorization)
            assert response.status_code == 200
            latest = _query("select w.light_index_request_id, w.is_deleted, o.operation, o.revision from tb_worklog w "
                            "join tb_worklog_light_outbox o using(worklog_id) where w.worklog_id=%s", (worklog_id,))[0]
            request_delete = str(latest[0])
            assert latest[1:] == (True, "DELETE", 3) and request_delete not in (event_a["request_id"], request_b)
            record_property("outbox_snapshot", f"latest={latest[2]},revision={latest[3]},deleted={latest[1]};final_count=0")
            internal = {"X-AI-Internal-Token": "e2e-only-internal-token"}
            for stale in (event_a["request_id"], request_b):
                result = _post(client, f"/internal/worklogs/{worklog_id}/light-index/start", expected=200,
                               headers=internal, json={"requestId": stale, "executionId": str(uuid.uuid4())}).json()
                assert result["execute"] is False
            accepted_delete = broker.blpop(f"e2e:accepted:{worklog_id}", timeout=40)
            assert accepted_delete
            event_delete = json.loads(accepted_delete[1])
            assert event_delete["request_id"] == request_delete and event_delete["operation"] == "DELETE"
            broker.delete(f"{prepared}:armed")
            broker.rpush(f"{prepared}:release", "1")
            final = _wait_index(worklog_id, request_delete)
            raw_b = broker.get(f"celery-task-meta-{event_b['task_ids'][0]}")
            assert raw_b and json.loads(raw_b)["result"]["status"] == "SKIPPED"
            raw_a = broker.get(f"celery-task-meta-{event_a['task_ids'][0]}")
            assert raw_a and json.loads(raw_a)["result"]["status"] == "COMPLETED"
            assert _query("select count(*) from tb_notification where ai_request_id in (%s,%s)",
                          (event_a["request_id"], request_b))[0][0] == 0
            assert _query("select count(*) from tb_worklog_light_outbox where worklog_id=%s", (worklog_id,))[0][0] == 0
            assert client.get(f"/worklogs/{worklog_id}", headers=authorization).status_code == 404
            with psycopg.connect(os.environ["LIGHTRAG_PG_DSN"], options="-c default_transaction_read_only=on") as conn:
                assert conn.execute("select count(*) from lightrag_doc_full where id=%s", (f"worklog-{worklog_id}",)).fetchone()[0] == 0
                assert conn.execute("select count(*) from lightrag_doc_status where id=%s", (f"worklog-{worklog_id}",)).fetchone()[0] == 0
                assert conn.execute("select count(*) from lightrag_doc_status where id=%s and status='processed'", (f"worklog-{other_id}",)).fetchone()[0] == 2
                workspaces = [row[0] for row in conn.execute("select workspace from lightrag_doc_status where id=%s", (f"worklog-{other_id}",)).fetchall()]
            with GraphDatabase.driver("bolt://neo4j:7687", auth=("neo4j", "e2e-only-password")) as graph:
                with graph.session() as session:
                    count = session.run("MATCH (w) WHERE w.entity_id=$name RETURN count(w) AS count", name=f"Worklog:{worklog_id}").single()["count"]
            assert count == 0
            for target, keyword in ((f"User:{author_id}", "AUTHORED_BY"), (f"Team:{team_id}", "BELONGS_TO"), (f"Tag:{shared_tag}", "HAS_TAG")):
                assert _has_both_workspaces(_graph_edges(other_id, target, keyword), team_id), "shared D relationship was damaged"
            qdrant = QdrantClient(url="http://qdrant:6333")
            for workspace in workspaces:
                points, _ = qdrant.scroll("lightrag_vdb_chunks_e2e_deterministic_768d", limit=1, with_payload=False,
                    scroll_filter=models.Filter(must=[
                        models.FieldCondition(key="workspace_id", match=models.MatchValue(value=workspace)),
                        models.FieldCondition(key="full_doc_id", match=models.MatchValue(value=f"worklog-{other_id}")),
                    ]))
                assert points, "shared D document vector was damaged"
            for collection in qdrant.get_collections().collections:
                offset = None
                for _ in range(100):
                    points, offset = qdrant.scroll(collection.name, offset=offset, limit=100, with_payload=True, with_vectors=False)
                    assert all(f"worklog://{worklog_id}" not in str((point.payload or {}).get("file_path", "")) for point in points)
                    if offset is None:
                        break
                else:
                    raise AssertionError("Qdrant inspection page limit exceeded")
            response = httpx.post(
                EXPECTED["WORKLOG_NORMAL_E2E_AI_URL"] + "/light/worklogs-v4/query",
                headers={"X-AI-Internal-Token": "e2e-only-internal-token"},
                json={"query": "E2E shared", "allowedTeamIds": [team_id]}, timeout=90, trust_env=False,
            )
            assert response.status_code == 200, f"isolated AI query returned HTTP {response.status_code}"
            paths = {item["filePath"] for item in response.json()["references"]}
            assert f"worklog://{other_id}" in paths, "search did not return surviving shared document"
            assert f"worklog://{worklog_id}" not in paths, "search returned deleted document"
            for key, value in {"worklog_id": worklog_id, "shared_worklog_id": other_id, "request_a": event_a["request_id"],
                               "task_a": event_a["task_ids"][0], "execution_a": json.loads(raw_a)["result"]["execution_id"],
                               "request_b": request_b, "task_b": event_b["task_ids"][0], "execution_b": json.loads(raw_b)["result"]["execution_id"],
                               "request_delete": request_delete, "task_delete": event_delete["task_ids"][0], "execution_delete": final[1]}.items():
                record_property(key, str(value))
        finally:
            broker.delete(f"{prepared}:armed")
            broker.rpush(f"{prepared}:release", "1")


def test_n7_mutation_and_rollback_in_real_postgres(record_property):
    broker = redis.Redis.from_url(EXPECTED["WORKLOG_NORMAL_E2E_BROKER_URL"])
    with httpx.Client(base_url=EXPECTED["WORKLOG_NORMAL_E2E_API_URL"], timeout=10, trust_env=False) as client:
        authorization, team_id = _new_fixture(client)
        worklog_id = _create_worklog(client, authorization, team_id, "E2E rollback source", "E2E rollback content")
        initial = _wait_index(worklog_id)
        response = client.patch(f"/worklogs/{worklog_id}", headers=authorization,
                                files={"request": (None, json.dumps({"actualHours": 2}), "application/json")})
        assert response.status_code == 200
        after_metadata = _query("select light_index_request_id, light_index_execution_id, light_index_status, actual_hours=2 "
                                "from tb_worklog where worklog_id=%s", (worklog_id,))[0]
        assert after_metadata[:3] == initial and after_metadata[3]
        assert _query("select count(*) from tb_worklog_light_outbox where worklog_id=%s", (worklog_id,))[0][0] == 0
        bad_patch = {"title": "E2E rollback changed", "removeFileIds": [999999999999]}
        response = client.patch(f"/worklogs/{worklog_id}", headers=authorization,
                                files={"request": (None, json.dumps(bad_patch), "application/json")})
        assert 400 <= response.status_code < 500
        unchanged = _query("select title=%s, light_index_request_id, light_index_execution_id, light_index_status "
                           "from tb_worklog where worklog_id=%s", ("E2E rollback source", worklog_id))[0]
        assert unchanged[0] and unchanged[1:] == initial
        assert _query("select count(*) from tb_worklog_light_outbox where worklog_id=%s", (worklog_id,))[0][0] == 0
        dispatch = f"e2e:dispatch:{worklog_id}"
        broker.set(f"{dispatch}:armed", "1", ex=120)
        try:
            response = client.patch(f"/worklogs/{worklog_id}", headers=authorization,
                                    files={"request": (None, json.dumps({"title": "E2E valid change"}), "application/json")})
            assert response.status_code == 200
            intent = _query("select w.light_index_request_id, o.request_id, o.operation, o.revision "
                            "from tb_worklog w join tb_worklog_light_outbox o using(worklog_id) where w.worklog_id=%s", (worklog_id,))[0]
            assert intent[0] != initial[0] and intent[0] == intent[1] and intent[2:] == ("REINDEX", 1)
            held_dispatch = broker.blpop(f"{dispatch}:arrived", timeout=45)
            assert held_dispatch and held_dispatch[1].decode() == str(intent[0]), "replacement dispatch was not held"
            before_source = _query("select title=%s, light_index_request_id, light_index_execution_id, light_index_status, xmin::text "
                                   "from tb_worklog where worklog_id=%s", ("E2E valid change", worklog_id))[0]
            before_outbox = _query("select request_id, operation, revision, attempts from tb_worklog_light_outbox "
                                   "where worklog_id=%s", (worklog_id,))
            before_notifications = _query("select count(*) from tb_notification where reference_id=%s", (worklog_id,))[0][0]
            before_published = broker.llen(f"e2e:accepted:{worklog_id}")
            assert before_source[0] and len(before_outbox) == 1
            probe = f"e2e_n7_{worklog_id}"
            sequence, function, trigger = f"{probe}_seq", f"{probe}_fail", f"{probe}_trigger"
            created_sequence = created_function = created_trigger = False
            with psycopg.connect(os.environ["PGVECTOR_DSN"], autocommit=True) as conn:
                try:
                    conn.execute(sql.SQL("CREATE SEQUENCE {}").format(sql.Identifier(sequence)))
                    created_sequence = True
                    body = f"BEGIN PERFORM nextval('public.{sequence}'); RAISE EXCEPTION 'E2E_DEFERRED_ROLLBACK'; END"
                    conn.execute(sql.SQL("CREATE FUNCTION {}() RETURNS trigger LANGUAGE plpgsql AS {}").format(
                        sql.Identifier(function), sql.Literal(body)))
                    created_function = True
                    conn.execute(sql.SQL("CREATE CONSTRAINT TRIGGER {} AFTER INSERT OR UPDATE ON tb_worklog_light_outbox "
                                         "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
                                         "WHEN (NEW.worklog_id = {} AND NEW.operation = 'REINDEX') EXECUTE FUNCTION {}()").format(
                                             sql.Identifier(trigger), sql.Literal(worklog_id), sql.Identifier(function)))
                    created_trigger = True
                    response = client.patch(f"/worklogs/{worklog_id}", headers=authorization,
                                            files={"request": (None, json.dumps({"title": "E2E deferred rollback"}), "application/json")})
                    assert response.status_code >= 400, "deferred transaction failure was not returned"
                    assert conn.execute(sql.SQL("SELECT last_value, is_called FROM {}").format(sql.Identifier(sequence))).fetchone() == (1, True), "deferred trigger did not fire"
                    after_source = _query("select title=%s, light_index_request_id, light_index_execution_id, light_index_status, xmin::text "
                                          "from tb_worklog where worklog_id=%s", ("E2E valid change", worklog_id))[0]
                    after_outbox = _query("select request_id, operation, revision, attempts from tb_worklog_light_outbox "
                                          "where worklog_id=%s", (worklog_id,))
                    assert after_source == before_source, "source changed despite deferred rollback"
                    assert after_outbox == before_outbox, "outbox authority changed despite deferred rollback"
                    assert _query("select count(*) from tb_notification where reference_id=%s", (worklog_id,))[0][0] == before_notifications
                    assert broker.llen(f"e2e:accepted:{worklog_id}") == before_published
                finally:
                    if created_trigger:
                        conn.execute(sql.SQL("DROP TRIGGER {} ON tb_worklog_light_outbox").format(sql.Identifier(trigger)))
                    if created_function:
                        conn.execute(sql.SQL("DROP FUNCTION {}()").format(sql.Identifier(function)))
                    if created_sequence:
                        conn.execute(sql.SQL("DROP SEQUENCE {}").format(sql.Identifier(sequence)))
            assert _query("select count(*) from pg_trigger where tgname=%s and not tgisinternal", (trigger,))[0][0] == 0
            record_property("rollback_probe", "deferred_trigger_fired=1;source_xmin_unchanged;outbox_unchanged;trigger_dropped=1")
            record_property("outbox_snapshot", f"latest={intent[2]},revision={intent[3]};rollback_unchanged=1;final_count=0")
        finally:
            broker.delete(f"{dispatch}:armed")
            broker.rpush(f"{dispatch}:release", "1")
        final = _wait_index(worklog_id, intent[0])
        events = []
        while raw := broker.lpop(f"e2e:accepted:{worklog_id}"):
            events.append(json.loads(raw))
        initial_tasks = [event["task_ids"][0] for event in events if event["request_id"] == str(initial[0])]
        final_tasks = [event["task_ids"][0] for event in events if event["request_id"] == str(final[0])]
        assert len(initial_tasks) == len(final_tasks) == 1
        assert _query("select count(*) from tb_worklog_light_outbox where worklog_id=%s", (worklog_id,))[0][0] == 0
        for key, value in {"worklog_id": worklog_id, "initial_request": initial[0], "initial_task": initial_tasks[0],
                           "initial_execution": initial[1], "request_id": final[0], "task_id": final_tasks[0],
                           "execution_id": final[1]}.items():
            record_property(key, str(value))
