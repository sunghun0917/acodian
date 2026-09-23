from pathlib import Path

import pytest

from app.pr_review.tools import RepositoryTools


def test_read_file_rejects_path_outside_repository(tmp_path: Path) -> None:
    tools = RepositoryTools(tmp_path)

    with pytest.raises(ValueError, match="escapes"):
        tools.read_file("../secret.txt")


def test_read_file_returns_repository_content(tmp_path: Path) -> None:
    source = tmp_path / "sample.py"
    source.write_text("answer = 42\n", encoding="utf-8")

    assert RepositoryTools(tmp_path).read_file("sample.py") == "answer = 42\n"
