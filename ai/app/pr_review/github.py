"""GitHub Pull Request 코멘트 게시 클라이언트다."""

from __future__ import annotations

import httpx


MARKER = "<!-- ax-wms-pr-review-bot -->"
BOT_LOGIN = "github-actions[bot]"


class GitHubPullRequestClient:
    """봇의 단일 PR 요약 코멘트를 생성하거나 갱신한다."""

    def __init__(self, *, api_url: str, token: str, repository: str, pull_number: int) -> None:
        self._api_url = api_url.rstrip("/")
        self._token = token
        self._repository = repository
        self._pull_number = pull_number

    def upsert_review_comment(self, body: str) -> None:
        """기존 봇 코멘트를 갱신하고, 없으면 새로 게시한다."""
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self._token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        comments_url = f"{self._api_url}/repos/{self._repository}/issues/{self._pull_number}/comments"
        with httpx.Client(timeout=20) as client:
            response = client.get(comments_url, headers=headers, params={"per_page": 100})
            response.raise_for_status()
            existing = next(
                (
                    comment
                    for comment in response.json()
                    if MARKER in comment.get("body", "")
                    and comment.get("user", {}).get("login") == BOT_LOGIN
                ),
                None,
            )
            if existing:
                update = client.patch(f"{comments_url}/{existing['id']}", headers=headers, json={"body": body})
                update.raise_for_status()
                return
            created = client.post(comments_url, headers=headers, json={"body": body})
            created.raise_for_status()
