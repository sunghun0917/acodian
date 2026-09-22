from app.pr_review.cli import _render
from app.pr_review.models import ReviewFinding, ReviewResult


def test_render_includes_marker_and_finding() -> None:
    body = _render(
        ReviewResult(
            summary="검토 결과입니다.",
            findings=[ReviewFinding(severity="P2", title="검증 누락", body="입력 검증이 필요합니다.")],
        ),
        "1234567890abcdef",
    )

    assert "<!-- ax-wms-pr-review-bot -->" in body
    assert "[P2] 검증 누락" in body
