"""Full V3 baseline evaluation: vanilla naive/mix, no relation or anchor repair."""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import statistics
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / 'v1'))
import run_ragas as base
base.PARAMS['enable_rerank'] = True

METRICS = [*base.METRICS, 'factual_correctness']
REPORT_METRICS = ('faithfulness', 'answer_relevancy', 'context_recall')
MODES = ('naive', 'mix')
JUDGE_MODEL = 'gemini-3.8-flash'
FLAGS = dict(postgres_relation_repair=False, anchor_replacement=False,
             extra_neo4j_expansion=False, rerank=True, builtin_mix_graph=True)


def load_cases() -> list[dict]:
    """Load frozen V4 questions and join validation metadata by source ID."""
    def lines(path):
        return [json.loads(line) for line in path.read_text(encoding='utf-8-sig').splitlines() if line.strip()]
    cases = lines(HERE / 'benchmark-v4.jsonl')
    details = lines(HERE.parent / 'v3' / 'validation-details.jsonl')
    indexed = {item['id']: item for item in details}
    if len(cases) != 50 or len({c['id'] for c in cases}) != 50 or len(indexed) != 50:
        raise ValueError('Expected exactly 50 unique V4 benchmark and validation IDs')
    for case in cases:
        source_id = case['id']
        if set(case) != {'id', 'category', 'question', 'reference'} or source_id not in indexed:
            raise ValueError('V4 benchmark does not match frozen validation metadata')
        case.update(indexed[source_id])
        case['id'] = source_id.replace('v3-', 'v4-', 1)
        if case['answerable'] is not True:
            raise ValueError('V4 verified absence is answerable, not abstention')
    if {c['split'] for c in cases} != {'development', 'evaluation'} or sum(c['split'] == 'development' for c in cases) != 25:
        raise ValueError('Expected 25 development and 25 evaluation cases')
    return cases

def generation_case(case: dict) -> dict:
    """Only question enters retrieval/generation; gold IDs are withheld."""
    return {'question': case['question'], 'expected_worklog_ids': []}


def contract(settings, output: Path | None = None) -> dict:
    """Fingerprint executable/data/local index inputs without recording credentials."""
    if settings.lightrag_llm_model == JUDGE_MODEL:
        raise ValueError('Generation and judge models must differ')
    paths = [Path(__file__), Path(base.__file__), HERE/'benchmark-v4.jsonl', HERE.parent/'v3'/'validation-details.jsonl', HERE.parent/'v3'/'manifest.json',
             HERE.parent/'v1'/'evaluation-corpus-v1.sql', HERE.parent/'v1'/'generate_corpus.py',
             base.AI/'app/light/v3/service/lightrag_adapter.py']
    if output is not None and (output/'index-preflight.json').exists():
        paths.append(output/'index-preflight.json')
    relevant = {k: v for k, v in settings.model_dump(mode='json').items()
                if k.startswith('lightrag_') and not any(s in k for s in ('key', 'password', 'secret', 'token'))}
    return dict(model=settings.lightrag_llm_model, judge_model=JUDGE_MODEL, embedding_model=settings.lightrag_embedding_model,
                versions={n: importlib.metadata.version(n) for n in ('ragas', 'lightrag-hku', 'google-genai', 'instructor')},
                hashes={p.relative_to(base.ROOT).as_posix(): base.sha(p) for p in paths},
                preflight=base.verify_local(settings), params=base.PARAMS, features=FLAGS,
                prompt_sha256=hashlib.sha256(base.PROMPT.encode()).hexdigest(),
                configuration_sha256=hashlib.sha256(json.dumps(relevant, sort_keys=True).encode()).hexdigest(),
                temperature=0, metrics=METRICS, factual=dict(mode='f1', atomicity='high', coverage='high'),
                response_cache=False, keyword_cache=True)


def check_contract(previous: dict, current: dict) -> None:
    """Fail rather than mixing different code, models, data, or index versions."""
    prev_c = previous.get('contract', {})
    for key in ('model', 'judge_model', 'embedding_model', 'params', 'features', 'prompt_sha256'):
        if prev_c.get(key) != current.get(key):
            raise ValueError(f'Resume contract drift on {key}: {prev_c.get(key)} != {current.get(key)}')
    # Check hashes with normalized posix paths
    prev_hashes = {Path(k).as_posix(): v for k, v in prev_c.get('hashes', {}).items()}
    curr_hashes = {Path(k).as_posix(): v for k, v in current.get('hashes', {}).items()}
    for path_key, expected_sha in curr_hashes.items():
        if path_key in prev_hashes and prev_hashes[path_key] != expected_sha:
            raise ValueError(f'Resume contract drift on file {path_key}')


def check_report_inputs(meta: dict) -> None:
    """Offline reports may change formatting, but never relabel changed input data."""
    hashes = {Path(k).as_posix(): v for k, v in meta.get('contract', {}).get('hashes', {}).items()}
    for path in (HERE/'benchmark-v4.jsonl', HERE.parent/'v3'/'validation-details.jsonl'):
        key = path.relative_to(base.ROOT).as_posix()
        if hashes.get(key) != base.sha(path):
            raise ValueError('Report input drift: '+path.name)


def quota_exhausted(row: dict) -> bool:
    """Stop on billing exhaustion rather than repeatedly spending failing calls."""
    errors = list(row.get('score_errors', {}).values())
    if row.get('generation_error'):
        errors.append(row['generation_error'])
    for error in errors:
        message = str(error.get('message', '')).lower()
        if error.get('status_code') == 402 or '402' in message or 'prepayment' in message or 'credit' in message and 'exhaust' in message:
            return True
    return False


async def score_row(row: dict, judge, metrics: dict, factual) -> None:
    """Reuse four baseline metrics and score factual F1 independently, resumably."""
    if row.get('scores', {}).get('factual_correctness') is None:
        row.setdefault('score_errors', {}).pop('factual_correctness', None)
    await base.evaluate_row(row, judge, metrics)
    if quota_exhausted(row) or row.get('scores', {}).get('factual_correctness') is not None:
        return
    for attempt in range(2):
        try:
            result = await asyncio.wait_for(factual.ascore(response=row['response'], reference=row['reference']), 240)
            value = float(result.value)
            if not math.isfinite(value):
                raise ValueError('Non-finite factual score')
            row['scores']['factual_correctness'] = value
            row['score_errors'].pop('factual_correctness', None)
            return
        except Exception as exc:
            row['scores']['factual_correctness'] = None
            row['score_errors']['factual_correctness'] = base.safe_error(exc, 'factual_correctness')
            if quota_exhausted(row):
                return
            if attempt == 0:
                await asyncio.sleep(3)


def row_complete(row: dict) -> bool:
    """An answer plus all finite scores is required for completion."""
    return 'response' in row and all(isinstance(row.get('scores', {}).get(m), (int, float))
        and math.isfinite(row['scores'][m]) for m in METRICS)


def read_rows(output: Path, cases: list[dict]) -> list[dict]:
    """Reject stale/foreign rows instead of quietly aggregating unrelated data."""
    allowed = {(c['id'], mode) for c in cases for mode in MODES}
    rows, seen = [], set()
    for path in sorted((output/'rows').glob('*.json')):
        row = base.read(path)
        key = (row['id'], row['mode'])
        if key not in allowed or key in seen or path.name != f'{key[0]}--{key[1]}.json':
            raise ValueError('Unexpected or duplicate result row: '+path.name)
        seen.add(key)
        rows.append(row)
    return rows


def report(output: Path, meta: dict, cases: list[dict]) -> dict:
    """Build category/paired summaries offline, retaining valid denominators."""
    rows = read_rows(output, cases)
    completed = sum('response' in r and all(r.get('scores', {}).get(m) is not None for m in REPORT_METRICS) for r in rows)
    generated = sum('response' in r for r in rows)
    rescored = sum(r.get('scores', {}).get('context_precision_all') is not None for r in rows)
    score_count = sum(r.get('scores', {}).get(m) is not None for r in rows for m in REPORT_METRICS)
    summary = dict(expected_rows=100, generated_rows=generated, complete_rows=completed,
                   expected_scores=300, valid_scores=score_count,
                   generation_errors=sum(bool(r.get('generation_error')) for r in rows),
                   score_errors=sum(sum(m in r.get('score_errors', {}) for m in REPORT_METRICS) for r in rows),
                   status='complete' if completed == 100 and score_count == 300 else 'incomplete')
    config = meta['contract']
    lines = ['# V4 Naive / LightRAG Mix 평가 — 관계 보완 OFF', '',
             f"- 상태: **{summary['status']}** · 생성 {generated}/100 · 주평가 3지표 채점 완료 {completed}/100 · 유효 점수 {score_count}/300",
             f"- 생성 모델: `{config['model']}` · 평가 모델: `{config['judge_model']}` · 임베딩: `{config['embedding_model']}` · temperature=0",
             '- Reranker: ON (bongsoo/klue-cross-encoder-v1).',
             '- 기존 200건 인덱스 재사용. 재인덱싱/삽입 없음. 기존 검색과 동일한 top_k=10, chunk_top_k=5.',
             '- All 50 questions run in both modes; mode order alternates, answer cache is OFF, keyword cache may be reused.',
             '- 실제 생성 프롬프트·전체 문맥·본문 청크·답변·사용량·시간은 문항 JSON에 보존. 추가 판정 trace는 수집하지 않음.',
             '', '## 분류별 평균', '',
             '점수는 0~1, 높을수록 좋음. 각 셀은 **평균 (유효 n/대상)**. 실패는 0점으로 넣지 않음.', '',
             '| 유형 | 모드 | Faithfulness | Answer relevancy | Context recall |',
             '|:---|:---|---:|---:|---:|']
    def label(case):
        return 'verified_absence' if case['answer_kind'] == 'verified_absence' else case['category']
    def mean(values, n):
        return f'{statistics.mean(values):.4f} ({len(values)}/{n})' if values else f'N/A (0/{n})'
    categories = ['Overall', *dict.fromkeys(label(c) for c in cases)]
    keyed = {(r['id'], r['mode']): r for r in rows}
    for category in categories:
        ids = {c['id'] for c in cases if category == 'Overall' or label(c) == category}
        for mode in MODES:
            group = [r for r in rows if r['id'] in ids and r['mode'] == mode]
            cells = [category, mode]
            for metric in REPORT_METRICS:
                values = [r['scores'][metric] for r in group if r.get('scores', {}).get(metric) is not None]
                cells.append(mean(values, len(ids)))
            lines.append('| '+' | '.join(cells)+' |')
    lines += ['', '## Split means', '',
              '| Split | Mode | Faithfulness | Answer relevancy | Context recall |',
              '|:---|:---|---:|---:|---:|']
    for split in ('development', 'evaluation'):
        ids = {c['id'] for c in cases if c['split'] == split}
        for mode in MODES:
            group = [r for r in rows if r['id'] in ids and r['mode'] == mode]
            cells = [split, mode]
            for metric in REPORT_METRICS:
                values = [r['scores'][metric] for r in group if r.get('scores', {}).get(metric) is not None]
                cells.append(mean(values, len(ids)))
            lines.append('| '+' | '.join(cells)+' |')
    lines += ['', '## 대응 문항 차이: Mix − Naive', '',
              '| 유형 | 지표 | 평균 차이 | 대응 n |', '|:---|:---|---:|---:|']
    for category in categories:
        for metric in REPORT_METRICS:
            diffs = []
            for case in cases:
                if category != 'Overall' and label(case) != category:
                    continue
                a = keyed.get((case['id'], 'naive'), {}).get('scores', {}).get(metric)
                b = keyed.get((case['id'], 'mix'), {}).get('scores', {}).get(metric)
                if a is not None and b is not None:
                    diffs.append(b-a)
            value = f'{statistics.mean(diffs):+.4f}' if diffs else 'N/A'
            lines.append(f'| {category} | {metric} | {value} | {len(diffs)} |')
    lines += ['', '## 시간과 오류', '', '| 모드 | 검색 평균 초 (n) | 검색+생성 평균 초 (n) |', '|:---|---:|---:|']
    for mode in MODES:
        group = [r for r in rows if r['mode'] == mode and 'response' in r]
        lines.append('| '+mode+' | '+' | '.join(mean([r[k] for r in group if k in r], 50) for k in ('retrieval_seconds', 'total_seconds'))+' |')
    lines += ['', f"- 생성 오류: {summary['generation_errors']}, 주평가 채점 오류: {summary['score_errors']}. 상세는 각 rows JSON의 오류 필드 확인.",
              '- API 대기/재시도/캐시가 시간에 영향을 준다. 운영 HTTP 지연 또는 순수 검색 엔진 성능이 아니다.',
              '', '## 해석 범위와 제한', '',
              '- Six verified-absence cases have answerable=true and are not abstention cases; report them separately.',
              '- Context Precision 제외: 본문 청크만 채점하면 Neo4j 엔티티·관계가 빠지고, 전체 문맥을 개별 레코드로 채점하면 엔티티·관계·청크의 단위 차이와 유형별 직렬화 순서가 점수를 왜곡한다. 어느 쪽도 그래프 관계·방향·경로 정확도를 대표하지 않는다.',
              f'- 제외된 탐색 지표의 원점수는 행 JSON에 보존했다. 전체 문맥 Context Precision은 {rescored}/100건만 재채점됐으며 주평가·모드 비교에서 제외한다.',
              '- Factual Correctness 제외: 짧은 reference와 이름/ID 표기 차이로 실제 정답과 점수가 어긋날 수 있다. 원점수만 보존하며 정답률로 해석하지 않는다.',
              '- 주평가 3지표도 그래프 정답 여부를 직접 판정하지 않는다. 시작 업무 식별, 관계/경로, 최종 집합, 부재의 structured_gold 채점은 아직 수행하지 않았다.',
              '- Naive 본문과 Mix 그래프의 정보량이 다르므로 시스템 구성 비교이며 그래프 구조만의 효과가 아니다.',
              '- Development/evaluation each contain 25 cases and relation components do not cross splits.',
              '- Unique starting-task identification for paraphrased questions has not been independently human-reviewed.',
              '- Fifty questions derive from V2 and reuse the same synthetic corpus; this is not independent holdout evidence.',
              '- Questions omit task IDs and jointly test starting-task identification and relationship traversal; related cases are not 50 independent samples.',
              '- 전체 탐색/관계 부재는 원천 200건의 유도 부분그래프 범위. 운영 전체/권한/실시간 상태/분기·순환 성능은 평가하지 않는다.',
              '- 1회 채점으로 judge 변동성/신뢰구간을 측정하지 않았다. 단일 전체 평균으로 유형 차이를 숨기지 않는다.',
              '- 로컬 200건 원문·처리 상태 해시를 검증한다. 외부 Neo4j/Qdrant 무결성은 별도 preflight 증거가 필요하다.',
              '- 생성에는 질문과 검색 문맥만 사용한다. reference와 validation-details의 gold는 검색/생성에 주입하지 않는다.']
    (output/'report.md').write_text('\n'.join(lines)+'\n', encoding='utf-8')
    base.save(output/'summary.json', summary)
    return summary


async def main(args) -> None:
    """Run all 100 pairs or regenerate the saved report without importing API SDKs."""
    output = (base.ROOT/args.output).resolve()
    if not output.is_relative_to(HERE) or output == HERE:
        raise ValueError('Output must be a child of evaluation/v3')
    cases = load_cases()
    if args.report_only:
        meta = base.read(output/'run.json')
        check_report_inputs(meta)
        report(output, meta, cases)
        return
    os.environ['RAGAS_DO_NOT_TRACK'] = 'true'
    os.environ['HF_HUB_OFFLINE'] = '1'
    os.chdir(base.AI)
    sys.path.insert(0, str(base.AI))
    from dotenv import load_dotenv
    load_dotenv(base.AI/'.env', override=True)
    from app.config.settings import settings
    if os.environ.get('RERANK_EVAL_DOCKER_NETWORK') == '1':
        settings = settings.model_copy(update={
            'neo4j_uri': 'bolt://axwms-neo4j:7687',
            'lightrag_qdrant_url': 'http://axwms-qdrant:6333',
            'lightrag_rerank_binding_host': 'http://reranker/rerank',
        })
    from google import genai
    from openai import AsyncOpenAI
    from ragas.llms import llm_factory
    from ragas.embeddings import GoogleEmbeddings
    from ragas.metrics.collections import Faithfulness, AnswerRelevancy, ContextPrecision, ContextRecall, FactualCorrectness
    current = contract(settings, output)
    current['benchmark_version'] = 'v4'
    current['row_id_prefix'] = 'v4'
    output.mkdir(parents=True, exist_ok=True)
    (output/'rows').mkdir(exist_ok=True)
    if (output/'run.json').exists():
        meta = base.read(output/'run.json')
        check_contract(meta, current)
    else:
        if any((output/'rows').glob('*.json')):
            raise ValueError('Rows exist without resume metadata')
        meta = dict(started_at=datetime.now(timezone.utc).isoformat(), contract=current)
        base.save(output/'run.json', meta)
    read_rows(output, cases)
    key = settings.gemini_api_key_candidates[0]
    client = genai.Client(api_key=key)
    judge_client = AsyncOpenAI(api_key=key, base_url='https://generativelanguage.googleapis.com/v1beta/openai/', timeout=120, max_retries=2)
    judge = llm_factory(current['judge_model'], provider='openai', client=judge_client, temperature=0, max_tokens=8192)
    embeddings = GoogleEmbeddings(client=client, model=current['embedding_model'])
    metrics = dict(faithfulness=Faithfulness(llm=judge), answer_relevancy=AnswerRelevancy(llm=judge, embeddings=embeddings),
                   context_precision_chunks=ContextPrecision(llm=judge), context_recall=ContextRecall(llm=judge))
    factual = FactualCorrectness(llm=judge, mode='f1', atomicity='high', coverage='high')
    rag = None
    try:
        rag = await base.setup_rag(settings.model_copy(update={'lightrag_llm_fallback_models': ''}))
        for i, case in enumerate(cases):
            for mode in (MODES if i % 2 == 0 else tuple(reversed(MODES))):
                path = output/'rows'/f"{case['id']}--{mode}.json"
                row = base.read(path) if path.exists() else {**case, 'mode': mode, 'features': FLAGS}
                if row_complete(row):
                    continue
                if 'response' not in row:
                    for attempt in range(2):
                        try:
                            row.update(await base.retrieve_generate(rag, client, current['model'], generation_case(case), mode))
                            expected = set(case['expected_worklog_ids'])
                            row['id_recall'] = len(expected & set(row['retrieved_worklog_ids']))/len(expected) if expected else None
                            row.pop('generation_error', None)
                            base.save(path, row)
                            break
                        except Exception as exc:
                            row['generation_error'] = base.safe_error(exc, 'retrieval_generation')
                            base.save(path, row)
                            if quota_exhausted(row):
                                raise RuntimeError('Billing exhausted; saved rows can be resumed') from None
                            if attempt == 0:
                                await asyncio.sleep(3)
                if 'response' in row:
                    await score_row(row, judge, metrics, factual)
                    base.save(path, row)
                print('DONE', case['id'], mode, json.dumps(row.get('scores', {})), flush=True)
                report(output, meta, cases)
                if quota_exhausted(row):
                    raise RuntimeError('Billing exhausted; saved rows can be resumed')
    finally:
        report(output, meta, cases)
        if rag is not None:
            await rag.finalize_storages()
        await judge_client.close()
        await client.aio.aclose()
    if report(output, meta, cases)['status'] != 'complete':
        raise RuntimeError('Incomplete rows remain; rerun the same command to resume')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', default='docs/ai/evaluation/v4/results-ragas-reranker-v4-bongsoo-20260930')
    parser.add_argument('--report-only', action='store_true')
    asyncio.run(main(parser.parse_args()))
