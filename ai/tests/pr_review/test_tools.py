from pathlib import Path

import pytest

from app.pr_review.tools import RepositoryTools


def test_read_match_context_rejects_path_outside_repository(tmp_path: Path) -> None:
    tools = RepositoryTools(tmp_path)

    with pytest.raises(ValueError, match="escapes"):
        tools.read_match_context("../secret.txt", "secret")


def test_read_match_context_returns_only_matching_context(tmp_path: Path) -> None:
    source = tmp_path / "sample.py"
    source.write_text("before\nneedle\nafter\n", encoding="utf-8")

    result = RepositoryTools(tmp_path).read_match_context("sample.py", "needle")

    assert "1-before" in result
    assert "2:needle" in result
    assert "3-after" in result


def test_read_match_context_limits_output_to_40_lines_on_each_side(tmp_path: Path) -> None:
    source = tmp_path / "sample.py"
    source.write_text("\n".join(f"line-{index}" for index in range(1, 102)), encoding="utf-8")

    result = RepositoryTools(tmp_path).read_match_context("sample.py", "line-51")

    assert "11-line-11" in result
    assert "51:line-51" in result
    assert "91-line-91" in result
    assert "10-line-10" not in result
    assert "92-line-92" not in result
