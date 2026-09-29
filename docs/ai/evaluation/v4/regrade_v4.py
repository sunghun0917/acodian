"""Regrade saved V3 answers against V4 references with verified User/Team IDs."""
from __future__ import annotations

import argparse
import asyncio
import copy
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import statistics
import sys
import types

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / 'v3'))
import run_evaluation as runner

sys.path.insert(0, str(runner.HERE.parent / 'v1'))
from generate_corpus import read_rows

SOURCE = runner.HERE / 'results-judge-gemini-3.8-flash-20260929'
TARGET = HERE / 'results-judge-gemini-3.8-flash-20260929'
BENCHMARK = HERE / 'benchmark-v4.jsonl'
RESCORED = ('context_recall', 'factual_correctness')


def revised_cases() -> tuple[list[dict], set[str]]:
    """Add verified identity IDs to references without changing questions or gold."""
    cases = runner.load_cases()
    source = read_rows(runner.HERE.parent / 'v1' / 'evaluation-corpus-v1.sql')
    names = {
        'User': {row['user_id']: row['user_name'] for row in source['tb_user']},
        'Team': {row['team_id']: row['team_name'] for row in source['tb_team']},
    }
    changed = set()
    for case in cases:
        gold = case['structured_gold']
        kind = 'User' if gold['author_ids'] else 'Team' if gold['team_ids'] else None
        if kind is None:
            continue
        ids = gold['author_ids'] if kind == 'User' else gold['team_ids']
        if len(ids) != 1 or names[kind][ids[0]] not in case['reference']:
            raise ValueError(f"Reference/identity mismatch: {case['id']}")
        case['reference'] += f' ({kind}:{ids[0]})'
        changed.add(case['id'])
    return cases, changed


def prepare() -> tuple[list[dict], set[str], dict, list[dict]]:
    """Freeze a new benchmark/run while keeping all source rows immutable."""
    cases, changed = revised_cases()
    benchmark = ''.join(json.dumps({k: case[k] for k in ('id', 'category', 'question', 'reference')},
                                   ensure_ascii=False) + '\n' for case in cases)
    if BENCHMARK.exists() and BENCHMARK.read_text(encoding='utf-8') != benchmark:
        raise ValueError('ID-aware benchmark drift')
    BENCHMARK.write_text(benchmark, encoding='utf-8')
    old_meta = runner.base.read(SOURCE / 'run.json')
    original = runner.load_cases()
    old_rows = runner.read_rows(SOURCE, original)
    if len(old_rows) != 100 or any(not runner.row_complete(row) for row in old_rows):
        raise ValueError('Expected 100 complete source rows')
    old_cases = {case['id']: case for case in original}
    revised = {case['id']: case for case in cases}
    TARGET.mkdir(exist_ok=True)
    (TARGET / 'rows').mkdir(exist_ok=True)
    meta = dict(started_at=datetime.now(timezone.utc).isoformat(), contract=old_meta['contract'],
                source_run=str(SOURCE.relative_to(runner.base.ROOT)),
                benchmark=str(BENCHMARK.relative_to(runner.base.ROOT)),
                benchmark_sha256=hashlib.sha256(benchmark.encode()).hexdigest(),
                changed_reference_ids=sorted(changed), regenerated_answers=False)
    if (TARGET / 'run.json').exists():
        previous = runner.base.read(TARGET / 'run.json')
        if any(previous[key] != meta[key] for key in ('contract', 'source_run', 'benchmark',
                                                     'benchmark_sha256', 'changed_reference_ids', 'regenerated_answers')):
            raise ValueError('ID-aware regrade contract drift')
        meta = previous
    else:
        runner.base.save(TARGET / 'run.json', meta)
    for source_row in old_rows:
        case_id = source_row['id']
        if source_row['question'] != old_cases[case_id]['question'] or source_row['reference'] != old_cases[case_id]['reference']:
            raise ValueError(f'Source row/benchmark mismatch: {case_id}')
        path = TARGET / 'rows' / f"{case_id}--{source_row['mode']}.json"
        if path.exists():
            row = runner.base.read(path)
            if row['reference'] != revised[case_id]['reference'] or row['response'] != source_row['response']:
                raise ValueError(f'Regrade row drift: {path.name}')
            continue
        row = copy.deepcopy(source_row)
        row['original_reference'] = row['reference']
        row['reference'] = revised[case_id]['reference']
        if case_id in changed:
            for metric in RESCORED:
                row['scores'].pop(metric, None)
                row.get('score_errors', {}).pop(metric, None)
        runner.base.save(path, row)
    return cases, changed, meta, old_rows


def write_report(cases: list[dict], changed: set[str], meta: dict, old_rows: list[dict]) -> dict:
    """Show the reference change and old/new factual scores without conflating runs."""
    summary = runner.report(TARGET, meta, cases)
    rows = runner.read_rows(TARGET, cases)
    original = {(row['id'], row['mode']): row for row in old_rows}
    lines = (TARGET / 'report.md').read_text(encoding='utf-8').splitlines()
    lines[0] += ' · ID 포함 기준답변 재채점'
    lines[2:2] = [f'- 변경 범위: 기준답변 {len(changed)}문항 × 2모드. 검색·답변 생성 없음; 기존 답변 재사용.',
                  '- Faithfulness·Answer Relevancy는 원점수를 재사용했다. 변경된 기준답변에 의존하는 Context Recall·Factual Correctness만 다시 채점했다.',
                  '- 주평가 3지표의 complete와 별개로 아래 Factual Correctness는 보조 지표이며 ID 일치의 확정 판정은 아니다.']
    lines += ['', '## ID 포함 기준답변의 Factual Correctness (보조)', '',
              '| 모드 | 기존 평균 | 새 기준답변 평균 | 재채점 완료/대상 |', '|:---|---:|---:|---:|']
    for mode in runner.MODES:
        group = [row for row in rows if row['id'] in changed and row['mode'] == mode]
        before = statistics.mean(original[(row['id'], mode)]['scores']['factual_correctness'] for row in group)
        after = [row['scores']['factual_correctness'] for row in group
                 if row.get('scores', {}).get('factual_correctness') is not None]
        lines.append(f"| {mode} | {before:.4f} | {statistics.mean(after):.4f}" +
                     f" | {len(after)}/{len(group)} |" if after else
                     f"| {mode} | {before:.4f} | N/A | 0/{len(group)} |")
    (TARGET / 'report.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return summary


async def main() -> None:
    """Resume only the two reference-dependent metrics for changed references."""
    cases, changed, meta, old_rows = prepare()
    rows = runner.read_rows(TARGET, cases)
    pending = [row for row in rows if row['id'] in changed and any(
        row.get('scores', {}).get(metric) is None for metric in RESCORED)]
    if not pending:
        write_report(cases, changed, meta, old_rows)
        return
    os.environ['RAGAS_DO_NOT_TRACK'] = 'true'
    os.environ['HF_HUB_OFFLINE'] = '1'
    try:
        import datasets
    except ImportError as exc:
        if 'pyarrow' not in str(exc):
            raise
        module = types.ModuleType('datasets')
        module.Dataset = type('UnavailableDataset', (), {})
        sys.modules['datasets'] = module
    from dotenv import load_dotenv
    from openai import AsyncOpenAI
    from ragas.llms import llm_factory
    from ragas.metrics.collections import ContextRecall, FactualCorrectness
    load_dotenv(runner.base.AI / '.env', override=True)
    sys.path.insert(0, str(runner.base.AI))
    from app.config.settings import settings
    client = AsyncOpenAI(api_key=settings.gemini_api_key_candidates[0],
                         base_url='https://generativelanguage.googleapis.com/v1beta/openai/',
                         timeout=120, max_retries=2)
    judge = llm_factory(meta['contract']['judge_model'], provider='openai', client=client,
                        temperature=0, max_tokens=8192)
    scorers = {'context_recall': ContextRecall(llm=judge),
               'factual_correctness': FactualCorrectness(llm=judge, mode='f1', atomicity='high', coverage='high')}
    try:
        for row in pending:
            path = TARGET / 'rows' / f"{row['id']}--{row['mode']}.json"
            for metric in RESCORED:
                if row.get('scores', {}).get(metric) is not None:
                    continue
                args = (dict(user_input=row['question'], reference=row['reference'],
                             retrieved_contexts=row['contexts']) if metric == 'context_recall'
                        else dict(response=row['response'], reference=row['reference']))
                try:
                    result = await asyncio.wait_for(scorers[metric].ascore(**args), 240)
                    value = float(result.value)
                    if not math.isfinite(value):
                        raise ValueError('Non-finite score')
                    row['scores'][metric] = value
                    row.setdefault('score_errors', {}).pop(metric, None)
                except Exception as exc:
                    row.setdefault('score_errors', {})[metric] = runner.base.safe_error(exc, metric)
                    runner.base.save(path, row)
                    write_report(cases, changed, meta, old_rows)
                    raise
                runner.base.save(path, row)
                print('SCORED', row['id'], row['mode'], metric, value, flush=True)
            write_report(cases, changed, meta, old_rows)
    finally:
        await client.close()
    if any(row.get('scores', {}).get(metric) is None for row in runner.read_rows(TARGET, cases)
           if row['id'] in changed for metric in RESCORED):
        raise RuntimeError('ID-aware regrade incomplete')


if __name__ == '__main__':
    argparse.ArgumentParser(description=__doc__).parse_args()
    asyncio.run(main())
