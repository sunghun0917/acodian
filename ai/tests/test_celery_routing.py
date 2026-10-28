import pytest

from app.task.celery_app import celery_app


@pytest.mark.parametrize(
    ("task_name", "expected_queue"),
    [
        ("file.summary", "ai_general"),
        ("worklog.summary", "ai_general"),
        ("worklog.tagging", "ai_general"),
        ("worklog.embedding", "ai_general"),
        ("worklog.light_index", "worklog_index"),
        ("worklog.light_index_result", "worklog_index"),
    ],
)
def test_task_routes_to_expected_queue(task_name, expected_queue):
    route = celery_app.amqp.router.route({}, task_name)
    assert route["queue"].name == expected_queue


def test_explicit_index_queue_is_preserved():
    route = celery_app.amqp.router.route({"queue": "worklog_index"}, "worklog.light_index")
    assert route["queue"].name == "worklog_index"
