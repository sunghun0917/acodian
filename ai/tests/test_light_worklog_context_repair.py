import asyncio
import json
from contextlib import asynccontextmanager

from unittest import TestCase

from app.light.v3.service.worklog_context_repair import repair_worklog_context
from app.light.v3.store.worklog_relation_scope_store import (
    ScopedWorklogRelationReader, TrustedEvaluationScope,
)
from app.light.v3.store.worklog_source_store import LightWorklogSourceRow, LightWorklogPredecessor


class Tokenizer:
    def encode(self, text):
        return list(text)

    def decode(self, tokens):
        return ''.join(tokens)


def context(contents):
    records = [json.dumps({'reference_id': str(i), 'content': c}) for i, (_, c) in enumerate(contents, 1)]
    refs = [f'[{i}] worklog://{wid}' for i, (wid, _) in enumerate(contents, 1)]
    return '\n'.join(records + refs)


def body(wid, text='body'):
    return f'source_type: WORKLOG\nworklog_id: {wid}\ntitle: title\n\nwork_content:\n{text}'


def repair(text, docs=None, **kwargs):
    return asyncio.run(repair_worklog_context(text, docs or {}, scope=TrustedEvaluationScope(frozenset({1}), frozenset(range(1, 10))), tokenizer=Tokenizer(), **kwargs))


def test_anchor_replace_deduplicate_rank_and_references():
    result = repair(context([(1, 'Confirmed worklog relation source for Worklog:1'), (1, body(1)), (2, body(2))]), {'worklog-1': {'content': body(1)}})
    assert result.chunks == [body(1), body(2)]
    assert result.used_worklog_ids == [1, 2]
    assert 'Confirmed worklog' not in result.exact_context
    assert '[1] worklog://1' in result.exact_context
    assert '[2] worklog://2' in result.exact_context
    assert '[3]' not in result.exact_context
    assert result.stats['anchors_replaced'] == 1


def test_missing_doc_unknown_malformed_and_conflicting_provenance_drop():
    result = repair(context([(1, 'Confirmed worklog relation source for Worklog:1'), (2, body(3)), (99, body(99)), (4, 'worklog_id: nope')]))
    assert result.chunks == []
    assert result.used_worklog_ids == []


def test_budget_and_chunk_cap():
    result = repair(context([(i, body(i, 'x' * 500)) for i in range(1, 9)]), body_token_budget=350, relation_token_budget=100)
    assert len(result.exact_context) <= 450
    assert len(result.chunks) <= 5
    assert result.stats['truncated_chunks'] > 0
    assert all(json.loads(u) for u in result.units)
    assert len(repair(context([(i, body(i)) for i in range(1, 9)])).chunks) == 5


def test_empty_scope_fails_closed():
    with TestCase().assertRaises(ValueError):
        TrustedEvaluationScope(frozenset(), frozenset({1}))
    with TestCase().assertRaises(ValueError):
        TrustedEvaluationScope(frozenset({1}), frozenset())
    with TestCase().assertRaises(ValueError):
        TrustedEvaluationScope(frozenset({True}), frozenset({1}))


def source(wid, team=1, predecessors=()):
    return LightWorklogSourceRow(wid, f'title-{wid}', None, 'body', wid, f'author-{wid}', team, f'team-{team}', [], [LightWorklogPredecessor(p, f'title-{p}') for p in predecessors])


def test_scoped_reader_onehop_and_no_forbidden_predecessor_leak():
    calls = []
    rows = {1: source(1, predecessors=(2, 3, 99)), 2: source(2, predecessors=(4,)), 3: source(3, team=2)}

    class Store:
        async def fetch_worklog_sources(self, session, worklog_ids):
            calls.append(worklog_ids)
            return {i: rows[i] for i in worklog_ids if i in rows}

    @asynccontextmanager
    async def session():
        yield object()

    reader = ScopedWorklogRelationReader(session, Store())
    scope = TrustedEvaluationScope(frozenset({1}), frozenset({1, 2, 3, 4}))
    fetched = asyncio.run(reader.fetch([1], scope))
    assert set(fetched) == {1, 2}
    assert [p.worklog_id for p in fetched[1].direct_predecessors] == [2]
    assert fetched[2].direct_predecessors == []
    assert calls == [[1], [2, 3]]
    result = asyncio.run(repair_worklog_context(context([(1, body(1))]), {}, scope=scope, tokenizer=Tokenizer(), relation_reader=reader))
    assert 'author-2' in result.exact_context
    assert 'title-3' not in result.exact_context
    assert 'title-99' not in result.exact_context
    assert 'Worklog:4' not in result.exact_context


def test_deleted_sources_absent_and_unknown_seeds_do_not_query():
    class Store:
        async def fetch_worklog_sources(self, session, worklog_ids):
            return {}  # SourceStore excludes deleted worklogs / teams in SQL.

    @asynccontextmanager
    async def session():
        yield object()

    reader = ScopedWorklogRelationReader(session, Store())
    scope = TrustedEvaluationScope(frozenset({1}), frozenset({1}))
    assert asyncio.run(reader.fetch([1], scope)) == {}
    assert asyncio.run(reader.fetch([99, '1', True], scope)) == {}


def test_graph_budget_does_not_displace_body_and_refs_match_units():
    graph = '\n'.join(json.dumps({'entity': f'e{i}', 'description': 'x' * 120}) for i in range(10))
    result = repair(graph + '\n' + context([(1, body(1))]), body_token_budget=300, relation_token_budget=180)
    assert result.chunks == [body(1)]
    assert result.stats['omitted_graph_records'] > 0
    assert result.stats['body_tokens'] <= 300
    assert result.stats['relation_tokens'] <= 180
    assert result.stats['total_tokens'] == len(result.exact_context) <= 480
    records = [json.loads(u) for u in result.units]
    assert [r['reference_id'] for r in records if 'content' in r] == ['1']


def test_missing_ambiguous_reference_and_wrong_anchor_replacement_fail_closed():
    raw = json.dumps({'reference_id': '1', 'content': body(1)})
    assert repair(raw).chunks == []
    assert repair(raw + '\n[1] worklog://1\n[1] worklog://2').chunks == []
    anchor = context([(1, 'Confirmed worklog relation source for Worklog:1')])
    assert repair(anchor, {'worklog-1': {'content': body(2)}}).chunks == []


def test_reader_excludes_cross_team_seed_before_expansion():
    calls = []

    class Store:
        async def fetch_worklog_sources(self, session, worklog_ids):
            calls.append(worklog_ids)
            return {1: source(1, team=2, predecessors=(2,))}

    @asynccontextmanager
    async def session():
        yield object()

    reader = ScopedWorklogRelationReader(session, Store())
    scope = TrustedEvaluationScope(frozenset({1}), frozenset({1, 2}))
    assert asyncio.run(reader.fetch([1], scope)) == {}
    assert calls == [[1]]


def test_noop_preserves_exact_context_and_original_json_units():
    graph_line = '  {"entity":"Worklog:1", "description":"known"}'
    chunk_line = json.dumps({'reference_id': '17', 'content': body(1)}, separators=(',', ':'))
    original = '\nOriginal graph heading\n```json\n' + graph_line + '\n```\n\nOriginal chunks heading\n```json\n' + chunk_line + '\n```\n\nOriginal reference heading\n[17] worklog://1\n\n'
    result = repair(original)
    assert result.exact_context == original
    assert result.units == [graph_line, chunk_line]
    assert result.chunks == [body(1)]
    assert result.used_worklog_ids == [1]
    assert result.stats['total_tokens'] == len(original)


def test_noop_does_not_preserve_original_when_headers_exceed_budget():
    original = 'very long heading ' * 100 + '\n' + context([(1, body(1))])
    result = repair(original, body_token_budget=300, relation_token_budget=100)
    assert result.exact_context != original
    assert result.stats['total_tokens'] == len(result.exact_context) <= 400
