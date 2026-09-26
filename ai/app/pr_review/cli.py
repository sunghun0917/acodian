"""GitHub Actions에서 실행되는 PR 리뷰 에이전트 진입점이다."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path
from typing import Any

from app.pr_review.github import GitHubPullRequestClient, MARKER
from app.pr_review.models import ReviewResult
from app.pr_review.openai_client import OpenAIResponsesClient
from app.pr_review.service import PullRequestReviewService
from app.pr_review.tools import RepositoryTools


def _load_event(path: Path) -> dict[str, Any]:
    """GitHub event payload를 읽는다."""
    return json.loads(path.read_text(encoding="utf-8"))


def _collect_diff(repository_root: Path, base_sha: str, head_sha: str) -> str:
    """PR base와 head 사이의 diff를 외부 diff 도구 없이 수집한다."""
    completed = subprocess.run(
        ["git", "diff", "--no-ext-diff", "--unified=30", f"{base_sha}...{head_sha}"],
        cwd=repository_root,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return completed.stdout[:80_000]


def _render(result: ReviewResult, head_sha: str) -> str:
    """구조화된 모델 결과를 GitHub Markdown 코멘트로 렌더링한다."""
    lines = [MARKER, "## AI PR 리뷰", "", result.summary, "", f"검토 커밋: `{head_sha[:12]}`"]
    if not result.findings:
        lines.extend(["", "근거 기반으로 확인된 조치 필요 항목이 없습니다."])
    for finding in result.findings:
        lines.extend(["", f"### [{finding.severity}] {finding.title}", "", finding.body])
        if finding.evidence:
            lines.extend(["", "근거: " + "; ".join(finding.evidence)])
    lines.extend(["", "_이 리뷰는 자동 생성되었습니다. 최종 판단은 리뷰어가 수행합니다._"])
    return "\n".join(lines)


def _run(args: argparse.Namespace) -> None:
    """이벤트를 해석하고 리뷰를 생성해 새 PR 코멘트를 게시한다."""
    event = _load_event(Path(args.event_path))
    pull_request = event.get("pull_request")
    if not isinstance(pull_request, dict):
        raise ValueError("pull_request event payload is required")
    base, head = pull_request.get("base"), pull_request.get("head")
    if not isinstance(base, dict) or not isinstance(head, dict):
        raise ValueError("pull request refs are required")

    pr_root = Path(args.pr_root).resolve()
    head_sha = str(head["sha"])
    diff = _collect_diff(pr_root, str(base["sha"]), head_sha)
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is required")

    result = PullRequestReviewService(
        OpenAIResponsesClient(
            api_key=api_key,
            model=os.environ.get("PR_REVIEW_MODEL", "gpt-5.6-terra"),
        ),
        RepositoryTools(pr_root),
    ).review(
        title=str(pull_request.get("title", "")),
        description=str(pull_request.get("body") or ""),
        diff=diff,
    )
    GitHubPullRequestClient(
        api_url=os.environ.get("GITHUB_API_URL", "https://api.github.com"),
        token=os.environ["GITHUB_TOKEN"],
        repository=os.environ["GITHUB_REPOSITORY"],
        pull_number=int(event["number"]),
    ).create_review_comment(_render(result, head_sha))


def main() -> None:
    """명령행 인자를 파싱하고 PR 리뷰를 실행한다."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--event-path", required=True)
    parser.add_argument("--pr-root", required=True)
    _run(parser.parse_args())


if __name__ == "__main__":
    main()
