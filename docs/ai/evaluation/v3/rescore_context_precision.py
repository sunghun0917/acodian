"""Score saved V3 generation contexts, including graph records, without retrieval."""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import math
import os
from pathlib import Path
import sys
import types

import run_evaluation as runner

METRIC = 'context_precision_all'


def score_inputs(row: dict) -> dict:
    """Use the exact ordered context records shown to generation, not text chunks."""
    return dict(user_input=row['question'], reference=row['reference'], retrieved_contexts=row['contexts'])


async def main(output: Path) -> None:
    """Resume only the additional Ragas score and rebuild the V3 report."""
    output = output.resolve()
    if not output.is_relative_to(runner.HERE) or output == runner.HERE:
        raise ValueError('Output must be a child of evaluation/v3')
    cases = runner.load_cases()
    meta = runner.base.read(output / 'run.json')
    runner.check_report_inputs(meta)
    rows = runner.read_rows(output, cases)
    if len(rows) != 100 or any(not runner.row_complete(row) or not row.get('contexts') for row in rows):
        raise ValueError('Expected 100 fully scored rows with saved generation contexts')

    os.environ['RAGAS_DO_NOT_TRACK'] = 'true'
    os.environ['HF_HUB_OFFLINE'] = '1'
    from dotenv import load_dotenv
    from openai import AsyncOpenAI
    try:
        import datasets
    except ImportError as exc:
        if 'pyarrow' not in str(exc):
            raise
        # Ragas context precision does not use HF Dataset; avoid a blocked optional DLL.
        module = types.ModuleType('datasets')
        module.Dataset = type('UnavailableDataset', (), {})
        sys.modules['datasets'] = module
    from ragas.llms import llm_factory
    from ragas.metrics.collections import ContextPrecision
    load_dotenv(runner.base.AI / '.env', override=True)
    sys.path.insert(0, str(runner.base.AI))
    from app.config.settings import settings

    key = settings.gemini_api_key_candidates[0]
    client = AsyncOpenAI(api_key=key, base_url='https://generativelanguage.googleapis.com/v1beta/openai/', timeout=120, max_retries=2)
    judge = llm_factory(meta['contract']['judge_model'], provider='openai', client=client, temperature=0, max_tokens=8192)
    metric = ContextPrecision(llm=judge)
    async def score_one(row: dict) -> Exception | None:
        path = output / 'rows' / f"{row['id']}--{row['mode']}.json"
        try:
            result = await asyncio.wait_for(metric.ascore(**score_inputs(row)), 600)
            value = float(result.value)
            if not math.isfinite(value):
                raise ValueError('Non-finite context precision score')
            row['scores'][METRIC] = value
            row.setdefault('score_errors', {}).pop(METRIC, None)
            row['context_precision_all_scored_at'] = datetime.now(timezone.utc).isoformat()
            runner.base.save(path, row)
            print('SCORED', row['id'], row['mode'], value, flush=True)
            return None
        except Exception as exc:
            row.setdefault('score_errors', {})[METRIC] = runner.base.safe_error(exc, METRIC)
            runner.base.save(path, row)
            return exc

    pending = [row for row in rows if row.get('scores', {}).get(METRIC) is None]
    try:
        for offset in range(0, len(pending), 3):
            errors = await asyncio.gather(*(score_one(row) for row in pending[offset:offset + 3]))
            runner.report(output, meta, cases)
            if any(errors):
                raise RuntimeError('Context precision batch failed; inspect row score_errors and resume')
    finally:
        await client.close()
    summary = runner.report(output, meta, cases)
    if summary['status'] != 'complete':
        raise RuntimeError('Context precision rescoring incomplete')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', default='docs/ai/evaluation/v3/results-judge-gemini-3.8-flash-20260929')
    args = parser.parse_args()
    asyncio.run(main(runner.base.ROOT / args.output))
