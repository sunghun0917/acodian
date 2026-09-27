"""SELECT-only, one-hop source reader for explicitly trusted offline evaluations."""

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, replace

from sqlalchemy.ext.asyncio import AsyncSession

from app.light.v3.store.worklog_source_store import LightWorklogSourceRow, LightWorklogSourceStore
from app.store.session import get_session_factory


@dataclass(frozen=True)
class TrustedEvaluationScope:
    """Caller-verified offline corpus/team scope; NOT a runtime authorization check."""

    allowed_team_ids: frozenset[int]
    corpus_worklog_ids: frozenset[int]

    def __post_init__(self) -> None:
        """Reject missing or malformed scope rather than expanding access implicitly."""
        for values in (self.allowed_team_ids, self.corpus_worklog_ids):
            if not values or any(type(value) is not int or value <= 0 for value in values):
                raise ValueError('Trusted evaluation requires nonempty positive integer scopes')


class ScopedWorklogRelationReader:
    """Reuse source-store deletion checks, then sanitize both ends before returning."""

    def __init__(
        self,
        session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]] | None = None,
        source_store: LightWorklogSourceStore | None = None,
    ) -> None:
        """Allow isolated test injection without introducing another database engine."""
        self._session_factory = session_factory
        self._source_store = source_store or LightWorklogSourceStore()

    async def fetch(
        self, seed_ids: list[int], scope: TrustedEvaluationScope,
    ) -> dict[int, LightWorklogSourceRow]:
        """Read at most five seeds and their direct predecessors; never traverse twice."""
        seeds = list(dict.fromkeys(i for i in seed_ids if type(i) is int and i in scope.corpus_worklog_ids))[:5]
        if not seeds:
            return {}
        factory = self._session_factory or get_session_factory()
        async with factory() as session:
            raw = await self._source_store.fetch_worklog_sources(session, seeds)
            sources = {i: row for i, row in raw.items() if i in seeds and row.worklog_id == i and row.team_id in scope.allowed_team_ids}
            predecessor_ids = list(dict.fromkeys(
                p.worklog_id for row in sources.values() for p in row.direct_predecessors
                if p.worklog_id in scope.corpus_worklog_ids and p.worklog_id not in sources
            ))
            predecessors = await self._source_store.fetch_worklog_sources(session, predecessor_ids) if predecessor_ids else {}
        allowed = {**sources, **{i: row for i, row in predecessors.items() if i in predecessor_ids and row.worklog_id == i and row.team_id in scope.allowed_team_ids}}
        # Do not return source-store's raw predecessor titles: they may cross scope.
        return {i: replace(row, direct_predecessors=[
            replace(p, title=allowed[p.worklog_id].title)
            for p in row.direct_predecessors if p.worklog_id in allowed
        ] if i in sources else []) for i, row in allowed.items()}
