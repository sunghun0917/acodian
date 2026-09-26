"""GitHub Pull Request 코멘트 게시 클라이언트다."""

from __future__ import annotations

import httpx


MARKER = "<!-- ax-wms-pr-review-bot -->"


class GitHubPullRequestClient:
    """리뷰 실행마다 새로운 PR 요약 코멘트를 게시한다."""

    def __init__(self, *, api_url: str, token: str, repository: str, pull_number: int) -> None:
        self._api_url = api_url.rstrip("/")
        self._token = token
        self._repository = repository
        self._pull_number = pull_number

    def create_review_comment(self, body: str) -> None:
        """기존 코멘트를 변경하지 않고 새 리뷰 코멘트를 게시한다."""
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self._token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        comments_url = f"{self._api_url}/repos/{self._repository}/issues/{self._pull_number}/comments"
        with httpx.Client(timeout=20) as client:
            created = client.post(comments_url, headers=headers, json={"body": body})
            created.raise_for_status()
