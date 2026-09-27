"""Bounded context repair for trusted offline evaluation, disabled on runtime paths.

The caller must verify that baseline graph records and full_docs belong to the
provided scope. Scope is an evaluation boundary, not a substitute for user auth.
Only ranked chunk provenance can seed repair; no question or gold data is read.
"""

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from app.light.v3.store.worklog_relation_scope_store import ScopedWorklogRelationReader, TrustedEvaluationScope
from app.light.v3.store.worklog_source_store import LightWorklogSourceRow

ANCHOR = re.compile(r'Confirmed worklog relation source for Worklog:([1-9][0-9]*)')
BODY_ID = re.compile(r'^worklog_id: ([1-9][0-9]*)$', re.MULTILINE)
REFERENCE = re.compile(r'^\[([^\]]+)\] worklog://([1-9][0-9]*)$')


class ContextTokenizer(Protocol):
    """Use the retrieval model's real tokenizer rather than character estimates."""

    def encode(self, text: str) -> list[int]:
        """Encode text using the caller-selected tokenizer."""
        ...

    def decode(self, tokens: list[int]) -> str:
        """Decode a bounded prefix using the same tokenizer."""
        ...


@dataclass(frozen=True)
class ContextRepairResult:
    """Exact generated context and auditable views of its retained records."""

    exact_context: str
    units: list[str]
    chunks: list[str]
    used_worklog_ids: list[int]
    stats: dict[str, Any]


def _json(record: dict[str, Any]) -> str:
    """Serialize the identical record for generation and metric input."""
    return json.dumps(record, ensure_ascii=False)


def _body_section(records: list[dict[str, Any]], ids: list[int]) -> str:
    """Rebuild references only for retained chunks, with no stale citations."""
    if not records:
        return ''
    refs = '\n'.join(f'[{i}] worklog://{wid}' for i, wid in enumerate(ids, 1))
    return 'Document Chunks:\n' + '\n'.join(map(_json, records)) + '\nReference Document List:\n' + refs + '\n'


def _relations(rows: dict[int, LightWorklogSourceRow]) -> list[dict[str, Any]]:
    """Expose names and confirmed one-hop facts, with explicit database provenance."""
    records = []
    for wid, row in rows.items():
        for relation, target, name in [('AUTHORED_BY', row.author_id, row.author_name), ('BELONGS_TO', row.team_id, row.team_name)]:
            records.append(dict(source_worklog_id=wid, relation=relation, target_id=target, target_name=name,
                description=f'Worklog:{wid} ({row.title}) {relation} {name} ({target})', provenance='postgres:tb_worklog'))
        for predecessor in row.direct_predecessors:
            records.append(dict(source_worklog_id=wid, relation='DEPENDS_ON', target_id=predecessor.worklog_id,
                target_name=predecessor.title, description=f'Worklog:{wid} ({row.title}) DEPENDS_ON Worklog:{predecessor.worklog_id} ({predecessor.title})', provenance='postgres:tb_worklog_dependency'))
    return records


async def repair_worklog_context(
    exact_context: str,
    full_docs: Mapping[str, Mapping[str, Any]],
    *,
    scope: TrustedEvaluationScope,
    tokenizer: ContextTokenizer,
    body_token_budget: int = 6000,
    relation_token_budget: int = 6000,
    max_chunks: int = 5,
    relation_reader: ScopedWorklogRelationReader | None = None,
) -> ContextRepairResult:
    """Repair only retrieved candidates under fixed body/graph token budgets.

    full_docs and the existing graph must already be trusted for the ENTIRE
    evaluation scope. This helper must not be wired to a user-facing route as
    an authorization filter. Missing/mismatched chunk provenance fails closed.
    """
    if body_token_budget < 0 or relation_token_budget < 0 or not 1 <= max_chunks <= 5:
        raise ValueError('Budgets must be nonnegative and max_chunks between 1 and 5')
    references: dict[str, set[int]] = {}
    candidates, graph = [], []
    original_units: list[str] = []
    stats: dict[str, Any] = dict(anchors_replaced=0, dropped_chunks=0, duplicate_chunks=0,
        truncated_chunks=0, omitted_graph_records=0, dropped_reasons=[], trusted_evaluation_only=True)
    for line in exact_context.splitlines():
        match = REFERENCE.fullmatch(line.strip())
        if match:
            references.setdefault(match[1], set()).add(int(match[2]))
        if not line.strip().startswith('{'):
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        if 'content' in record:
            candidates.append(record)
            original_units.append(line)
        elif 'entity' in record or 'entity1' in record:
            graph.append(record)
            original_units.append(line)
    selected, used_ids, seen_contents = [], [], set()
    for candidate in candidates:
        content = candidate.get('content')
        reason = None
        if not isinstance(content, str):
            reason = 'invalid_content'
        anchor = ANCHOR.fullmatch(content.strip()) if isinstance(content, str) else None
        matches = BODY_ID.findall(content) if isinstance(content, str) else []
        wid = int(anchor[1]) if anchor else int(matches[0]) if len(matches) == 1 else None
        if wid not in scope.corpus_worklog_ids:
            reason = 'unknown_worklog'
        elif references.get(str(candidate.get('reference_id'))) != {wid}:
            reason = 'invalid_provenance'
        if not reason and anchor:
            replacement = full_docs.get(f'worklog-{wid}', {}).get('content')
            if not isinstance(replacement, str) or BODY_ID.findall(replacement) != [str(wid)]:
                reason = 'missing_or_invalid_full_doc'
            else:
                content = replacement
                stats['anchors_replaced'] += 1
        if reason:
            stats['dropped_chunks'] += 1
            stats['dropped_reasons'].append(reason)
            continue
        if wid in used_ids or content in seen_contents:
            stats['duplicate_chunks'] += 1
            continue
        if len(selected) >= max_chunks:
            stats['dropped_chunks'] += 1
            stats['dropped_reasons'].append('chunk_cap')
            continue
        record = dict(reference_id=str(len(selected) + 1), content=content)
        proposed = _body_section(selected + [record], used_ids + [wid])
        if len(tokenizer.encode(proposed)) > body_token_budget:
            tokens = tokenizer.encode(content)
            low, high = 0, len(tokens)
            while low < high:
                mid = (low + high + 1) // 2
                record['content'] = tokenizer.decode(tokens[:mid])
                if len(tokenizer.encode(_body_section(selected + [record], used_ids + [wid]))) <= body_token_budget:
                    low = mid
                else:
                    high = mid - 1
            record['content'] = tokenizer.decode(tokens[:low])
            if BODY_ID.findall(record['content']) != [str(wid)] or not low:
                stats['dropped_chunks'] += 1
                stats['dropped_reasons'].append('body_budget')
                continue
            stats['truncated_chunks'] += 1
        selected.append(record)
        used_ids.append(wid)
        seen_contents.add(content)
    body = _body_section(selected, used_ids)
    added = _relations(await relation_reader.fetch(used_ids, scope)) if relation_reader and used_ids else []
    # Reserve the independent graph budget for confirmed repair before baseline KG.
    graph_units: list[str] = []
    for record in added + graph:
        unit = _json(record)
        if unit in graph_units:
            continue
        proposed = '\n'.join(graph_units + [unit]) + '\n'
        if len(tokenizer.encode(proposed)) <= relation_token_budget:
            graph_units.append(unit)
        else:
            stats['omitted_graph_records'] += 1
    graph_text = '\n'.join(graph_units) + '\n' if graph_units else ''
    combined = graph_text + body
    # Tokenizers can merge across a section boundary; enforce the combined cap too.
    while graph_units and len(tokenizer.encode(combined)) > body_token_budget + relation_token_budget:
        graph_units.pop()
        stats['omitted_graph_records'] += 1
        combined = ('\n'.join(graph_units) + '\n' if graph_units else '') + body
    stats.update(body_tokens=len(tokenizer.encode(body)), relation_tokens=len(tokenizer.encode('\n'.join(graph_units) + '\n' if graph_units else '')),
        total_tokens=len(tokenizer.encode(combined)), added_relation_records=sum('"provenance": "postgres:' in u for u in graph_units))
    # Preserve the baseline prompt byte-for-byte when repair made no changes.
    # Otherwise formatting alone would confound the anchor-only ablation.
    if relation_reader is None and not any(stats[key] for key in (
        'anchors_replaced', 'dropped_chunks', 'duplicate_chunks',
        'truncated_chunks', 'omitted_graph_records',
    )):
        original_tokens = len(tokenizer.encode(exact_context))
        if original_tokens <= body_token_budget + relation_token_budget:
            stats['total_tokens'] = original_tokens
            return ContextRepairResult(exact_context, original_units,
                [r['content'] for r in candidates], used_ids, stats)
    return ContextRepairResult(combined, graph_units + [_json(r) for r in selected],
        [r['content'] for r in selected], used_ids, stats)
