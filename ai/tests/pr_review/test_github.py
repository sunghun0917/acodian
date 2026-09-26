import json
from unittest.mock import patch

import httpx

from app.pr_review.github import GitHubPullRequestClient


def test_each_review_creates_a_new_comment() -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(201, json={"id": len(requests)})

    reviewer = GitHubPullRequestClient(
        api_url="https://api.github.com",
        token="token",
        repository="owner/repo",
        pull_number=1,
    )
    for body in ("first review", "second review"):
        client = httpx.Client(transport=httpx.MockTransport(handle))
        with patch("app.pr_review.github.httpx.Client", return_value=client):
            reviewer.create_review_comment(body)

    assert [(request.method, str(request.url)) for request in requests] == [
        ("POST", "https://api.github.com/repos/owner/repo/issues/1/comments"),
        ("POST", "https://api.github.com/repos/owner/repo/issues/1/comments"),
    ]
    assert [json.loads(request.content) for request in requests] == [
        {"body": "first review"},
        {"body": "second review"},
    ]
