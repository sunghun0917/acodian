from unittest.mock import patch

from app.pr_review.github import BOT_LOGIN, MARKER, GitHubPullRequestClient


class FakeResponse:
    def __init__(self, payload: object = None) -> None:
        self._payload = payload

    def json(self) -> object:
        return self._payload

    def raise_for_status(self) -> None:
        pass


class FakeClient:
    def __init__(self) -> None:
        self.patched_urls: list[str] = []
        self.created_bodies: list[str] = []

    def __enter__(self) -> "FakeClient":
        return self

    def __exit__(self, *_: object) -> None:
        pass

    def get(self, *_: object, **__: object) -> FakeResponse:
        return FakeResponse(
            [
                {"id": 1, "body": MARKER, "user": {"login": "reviewer"}},
                {"id": 2, "body": MARKER, "user": {"login": BOT_LOGIN}},
            ]
        )

    def patch(self, url: str, **_: object) -> FakeResponse:
        self.patched_urls.append(url)
        return FakeResponse()

    def post(self, _: str, **kwargs: object) -> FakeResponse:
        self.created_bodies.append(str(kwargs["json"]["body"]))
        return FakeResponse()


def test_upsert_updates_only_the_bot_comment() -> None:
    fake = FakeClient()

    with patch("app.pr_review.github.httpx.Client", return_value=fake):
        GitHubPullRequestClient(
            api_url="https://api.github.com",
            token="token",
            repository="owner/repo",
            pull_number=1,
        ).upsert_review_comment("review")

    assert fake.patched_urls == ["https://api.github.com/repos/owner/repo/issues/1/comments/2"]
    assert fake.created_bodies == []
