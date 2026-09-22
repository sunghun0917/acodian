"""PR 리뷰용으로 격리된 읽기 전용 저장소 도구를 제공한다."""

from __future__ import annotations

import subprocess
from pathlib import Path


MAX_OUTPUT_CHARS = 12_000


class RepositoryTools:
    """허용된 작업 트리 밖을 읽지 않는 PR 분석 도구 모음이다."""

    def __init__(self, repository_root: Path) -> None:
        self._root = repository_root.resolve()

    def read_file(self, path: str) -> str:
        """저장소 내부 텍스트 파일의 앞부분을 반환한다."""
        target = self._resolve(path)
        if not target.is_file():
            return f"File not found: {path}"
        try:
            return self._truncate(target.read_text(encoding="utf-8", errors="replace"))
        except OSError as exc:
            return f"Unable to read {path}: {exc.__class__.__name__}"

    def search_code(self, query: str) -> str:
        """고정된 rg 인자로 저장소에서 문자열을 검색한다."""
        query = query.strip()
        if not query or len(query) > 200:
            return "Search query must contain 1 to 200 characters."
        return self._run("rg", "--line-number", "--max-count", "30", "--glob", "!.git", "--", query)

    def get_git_history(self, path: str = "") -> str:
        """최근 커밋 제목을 반환하며, 필요하면 단일 파일로 범위를 좁힌다."""
        args = ["log", "--format=%h %s", "-20"]
        if path:
            self._resolve(path)
            args.extend(["--", path])
        return self._run("git", *args)

    def find_tests(self, path: str = "") -> str:
        """테스트 파일 후보를 반환한다. path는 파일명 또는 기능 키워드다."""
        query = path.strip()
        output = self._run("rg", "--files", "--glob", "!.git", "--glob", "*test*", "--glob", "*spec*")
        if not query:
            return output
        candidates = [line for line in output.splitlines() if query.casefold() in line.casefold()]
        return self._truncate("\n".join(candidates) or "No matching test files found.")

    def execute(self, name: str, arguments: dict[str, object]) -> str:
        """허용 목록의 도구만 인자로 실행한다."""
        if name == "read_file":
            return self.read_file(str(arguments.get("path", "")))
        if name == "search_code":
            return self.search_code(str(arguments.get("query", "")))
        if name == "get_git_history":
            return self.get_git_history(str(arguments.get("path", "")))
        if name == "find_tests":
            return self.find_tests(str(arguments.get("path", "")))
        return f"Tool is not allowed: {name}"

    def _resolve(self, path: str) -> Path:
        if not path or Path(path).is_absolute():
            raise ValueError("A repository-relative path is required.")
        target = (self._root / path).resolve()
        if self._root not in target.parents and target != self._root:
            raise ValueError("Path escapes the repository root.")
        return target

    def _run(self, command: str, *args: str) -> str:
        try:
            completed = subprocess.run(
                [command, *args],
                cwd=self._root,
                check=False,
                capture_output=True,
                text=True,
                timeout=15,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return f"Tool execution failed: {exc.__class__.__name__}"
        output = completed.stdout if completed.returncode in (0, 1) else completed.stderr
        return self._truncate(output.strip() or "No results.")

    @staticmethod
    def _truncate(value: str) -> str:
        if len(value) <= MAX_OUTPUT_CHARS:
            return value
        return f"{value[:MAX_OUTPUT_CHARS]}\n[truncated]"
