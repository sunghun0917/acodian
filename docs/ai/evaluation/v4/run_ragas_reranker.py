"""Evaluate the current app.light.v4 mix pipeline on the frozen 50-case V4 benchmark."""
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
import re
import time

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / 'v1'))
import run_ragas as base

METRICS = ['faithfulness', 'answer_relevancy', 'context_recall']
REPORT_METRICS = ('faithfulness', 'answer_relevancy', 'context_recall')
MODES = ('mix',)
JUDGE_MODEL = 'gemini-3.8-flash'


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
    return {'question': case['question']}


def contract(settings, output: Path | None = None) -> dict:
    """Fingerprint current pipeline/models/frozen data/index, never credential values."""
    if JUDGE_MODEL in settings.lightrag_answer_llm_model_candidates:
        raise ValueError('Generation and judge models must differ')
    paths = [Path(__file__), Path(base.__file__), HERE/'benchmark-v4.jsonl',
             HERE.parent/'v3'/'validation-details.jsonl', HERE.parent/'v3'/'manifest.json',
             HERE.parent/'v1'/'evaluation-corpus-v1.sql', HERE.parent/'v1'/'generate_corpus.py',
             base.AI/'app/config/settings.py', *sorted((base.AI/'app/light/v4').rglob('*.py'))]
    relevant = {k: v for k, v in settings.model_dump(mode='json').items() if k.startswith('lightrag_')}
    from app.light.v4.prompt.worklog_query_prompt import build_worklog_query_system_prompt
    prompt = build_worklog_query_system_prompt(worklog_detail_base_url=settings.worklog_detail_base_url)
    preflight = base.verify_local(settings)
    directory = Path(settings.lightrag_working_dir).resolve()
    docs = base.read(directory/'kv_store_full_docs.json')
    if any(d.get('worklog_source_schema_version') != 1 or not d.get('worklog_source') for d in docs.values()):
        raise ValueError('Index does not contain current v4 source snapshots')
    preflight['index_hashes'] = {p.name: base.sha(p) for p in sorted(directory.glob('kv_store_*.json'))
                               if 'llm_response_cache' not in p.name}
    return dict(pipeline='app.light.v4', benchmark_version='v4', row_id_prefix='v4',
                model=settings.lightrag_answer_llm_model_candidates[0],
                answer_models=settings.lightrag_answer_llm_model_candidates,
                keyword_models=settings.lightrag_llm_model_candidates,
                judge_model=JUDGE_MODEL, embedding_model=settings.lightrag_embedding_model,
                versions={n: importlib.metadata.version(n) for n in ('ragas', 'lightrag-hku', 'google-genai', 'instructor')},
                hashes={p.relative_to(base.ROOT).as_posix(): base.sha(p) for p in paths}, preflight=preflight,
                params=dict(mode='mix', top_k=settings.lightrag_query_top_k,
                            chunk_top_k=settings.lightrag_query_chunk_top_k,
                            enable_rerank=settings.lightrag_rerank_enabled,
                            rerank_model=settings.lightrag_rerank_model,
                            response_type=settings.lightrag_query_response_type, allowed_team_ids=None),
                prompt_sha256=hashlib.sha256(prompt.encode()).hexdigest(),
                configuration_sha256=hashlib.sha256(json.dumps(relevant, sort_keys=True).encode()).hexdigest(),
                metrics=METRICS, judge_temperature=0, response_cache=False, keyword_cache=False)


def check_external_chunks(chunks: dict, payloads: list[dict]) -> None:
    """Require native vector payloads to match local chunk IDs, text and provenance."""
    indexed = {p['id']: p for p in payloads}
    if len(indexed) != len(payloads) or set(indexed) != set(chunks):
        raise ValueError('Qdrant/global local chunk ID mismatch')
    for chunk_id, row in chunks.items():
        if any(indexed[chunk_id].get(k) != row.get(k) for k in ('content', 'full_doc_id', 'file_path')):
            raise ValueError('Qdrant/global local chunk content or provenance mismatch')


async def verify_external_index(settings) -> dict:
    """Read and fingerprint the live native graph/vector scope before any paid query/resume."""
    from qdrant_client import QdrantClient, models
    from neo4j import AsyncGraphDatabase
    # This frozen benchmark is a global/base index, never an arbitrary environment override.
    if settings.lightrag_workspace or os.environ.get('NEO4J_WORKSPACE', ''):
        raise ValueError('Frozen V4 evaluation requires the global/base workspace')
    directory = Path(settings.lightrag_working_dir).resolve()
    docs = base.read(directory/'kv_store_full_docs.json')
    chunks = base.read(directory/'kv_store_text_chunks.json')
    def digest(value):
        return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()
    result = {'workspace': 'base', 'qdrant': {}}
    client = QdrantClient(url=settings.lightrag_qdrant_url, api_key=settings.lightrag_qdrant_api_key or None, timeout=120)
    try:
        rag = build_adapter(settings)._build_lightrag()
        for namespace in ('chunks', 'entities', 'relationships'):
            name = getattr(rag, namespace+'_vdb').final_namespace
            points, offset = [], None
            while True:
                batch, offset = client.scroll(name, limit=128, offset=offset, with_payload=True, with_vectors=True,
                    scroll_filter=models.Filter(must=[models.FieldCondition(key='workspace_id', match=models.MatchValue(value='_'))]))
                points.extend(batch)
                if offset is None:
                    break
            if not points:
                raise ValueError('Empty native global Qdrant collection: '+name)
            if namespace == 'chunks':
                check_external_chunks(chunks, [p.payload for p in points])
            result['qdrant'][name] = {'count': len(points), 'sha256': digest([
                {'id': str(p.id), 'payload': p.payload, 'vector': p.vector} for p in sorted(points, key=lambda p: str(p.id))])}
    finally:
        client.close()
    driver = AsyncGraphDatabase.driver(settings.neo4j_uri, auth=(settings.neo4j_user, settings.neo4j_password))
    try:
        async with driver.session(database=settings.lightrag_neo4j_database) as session:
            nodes = await (await session.run('MATCH (n:base) RETURN elementId(n) AS id, labels(n) AS labels, properties(n) AS properties ORDER BY id')).data()
            edges = await (await session.run('MATCH (a:base)-[r]->(b:base) RETURN elementId(r) AS id, elementId(a) AS source, elementId(b) AS target, type(r) AS type, properties(r) AS properties ORDER BY id')).data()
        expected = {'Worklog:'+k.removeprefix('worklog-') for k in docs}
        actual = {n['properties']['entity_id'] for n in nodes if n['properties'].get('entity_id', '').startswith('Worklog:')}
        if actual != expected:
            raise ValueError('Neo4j/global local canonical Worklog ID mismatch')
        result['neo4j'] = {'nodes': len(nodes), 'edges': len(edges), 'sha256': digest({'nodes': nodes, 'edges': edges})}
    finally:
        await driver.close()
    return result


def check_contract(previous: dict, current: dict) -> None:
    """Reject changed/missing pipeline, versions, hashes or index inputs on resume."""
    old = previous.get('contract', {})
    if old != current:
        changed = sorted(k for k in set(old) | set(current) if old.get(k) != current.get(k))
        raise ValueError('Resume contract drift: '+', '.join(changed)+'; use a new --output directory')


def check_report_inputs(meta: dict) -> None:
    """Offline reports cannot relabel changed benchmark data or historical v3 runs."""
    if meta['contract'].get('pipeline') != 'app.light.v4':
        raise ValueError('Historical v3 output is immutable; use its saved report')
    for path in (HERE/'benchmark-v4.jsonl', HERE.parent/'v3'/'validation-details.jsonl'):
        if meta['contract']['hashes'].get(path.relative_to(base.ROOT).as_posix()) != base.sha(path):
            raise ValueError('Report input drift: '+path.name)


def context_units(system_prompt: str) -> tuple[str, list[str], list[str]]:
    """Keep the actual native graph/body JSON records sent to the answer model."""
    if '\n---Context---\n' not in system_prompt:
        raise ValueError('Missing actual v4 generation context')
    context = system_prompt.split('\n---Context---\n', 1)[1]
    units, chunks = [], []
    for line in context.splitlines():
        if line.strip().startswith('{'):
            item = json.loads(line)
            units.append(line)
            if 'content' in item:
                chunks.append(item['content'])
    if not units:
        raise ValueError('No native JSON context records in actual v4 generation prompt')
    return context, units, chunks


def build_adapter(settings):
    """Observe real v4 generation without replacing its prompt, model or retrieval."""
    from app.light.v4.service.lightrag_adapter import LightRagWorklogIndexAdapter

    class EvaluationAdapter(LightRagWorklogIndexAdapter):
        def _build_lightrag(self):
            rag = super()._build_lightrag()
            rag.enable_llm_cache = False
            rag.llm_response_cache.global_config['enable_llm_cache'] = False
            return rag

        async def _complete_answer(self, prompt, **kwargs):
            self.system_prompt = kwargs.get('system_prompt', '')
            self.retrieval_seconds = time.perf_counter() - self.started
            return await super()._complete_answer(prompt, **kwargs)

        async def query_worklogs(self, options):
            result = await super().query_worklogs(options)
            self.raw = result.raw
            return result

    return EvaluationAdapter(settings_obj=settings)


def build_service(settings, adapter):
    """Use the production service with no Redis answer cache."""
    from app.light.v4.service.worklog_query_service import LightWorklogQueryService
    return LightWorklogQueryService(settings_obj=settings, adapter_factory=lambda: adapter, cache=None)


async def retrieve_generate(adapter, service, case: dict) -> dict:
    """Generate one global-scope answer and retain exactly the evidence shown to its LLM."""
    from app.light.v4.model.worklog_query import WorklogLightQueryRequest
    adapter.system_prompt, adapter.raw = '', {}
    adapter.started = time.perf_counter()
    response = await service.query_worklogs(WorklogLightQueryRequest(query=case['question'], allowedTeamIds=None))
    total_seconds = time.perf_counter() - adapter.started
    if not response.answer.strip():
        raise ValueError('Empty v4 generated answer')
    context, units, chunks = context_units(adapter.system_prompt)
    ids = sorted({int(x) for c in chunks for x in re.findall(r'worklog_id:\s*(\d+)', c)})
    return dict(response=response.answer, contexts=units, chunks=chunks, exact_context=context,
                system_prompt=adapter.system_prompt, references=[r.model_dump(mode='json') for r in response.references],
                retrieval=adapter.raw.get('data', {}), retrieval_metadata=adapter.raw.get('metadata', {}),
                retrieved_worklog_ids=ids, retrieval_seconds=adapter.retrieval_seconds,
                total_seconds=total_seconds, generation_usage=None)


def quota_exhausted(row: dict) -> bool:
    """Stop on billing exhaustion rather than repeatedly spending failing calls."""
    errors = list(row.get('score_errors', {}).values())
    if row.get('generation_error'):
        errors.append(row['generation_error'])
    for error in errors:
        message = str(error.get('message', '')).lower()
        if error.get('status_code') in (402, 429) or any(x in message for x in ('402', '429', 'quota', 'prepayment', 'resource_exhausted')) or 'credit' in message and 'exhaust' in message:
            return True
    return False


def generation_error(error: Exception) -> dict:
    """Keep the sanitized provider cause so v4 wrappers cannot hide billing/quota failures."""
    while error.__cause__ is not None:
        error = error.__cause__
    return base.safe_error(error, 'retrieval_generation')


async def score_row(row: dict, path: Path, judge, metrics: dict) -> None:
    """Checkpoint each completed paid metric before starting the next one."""
    for name, metric in metrics.items():
        if valid_score(row.get('scores', {}).get(name)):
            continue
        row.setdefault('scores', {}).pop(name, None)
        row.setdefault('score_errors', {}).pop(name, None)
        await base.evaluate_row(row, judge, {name: metric})
        base.save(path, row)
        if quota_exhausted({'score_errors': {name: row['score_errors'].get(name, {})}}):
            raise RuntimeError('Billing/quota exhausted; saved scores can be resumed')


def valid_score(value) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1


def row_complete(row: dict) -> bool:
    """A nonempty answer and all finite primary scores are required for completion."""
    return bool(row.get('response')) and all(valid_score(row.get('scores', {}).get(m)) for m in METRICS)


def read_rows(output: Path, cases: list[dict]) -> list[dict]:
    """Reject stale/foreign rows instead of quietly aggregating unrelated data."""
    allowed = {(c['id'], mode): c for c in cases for mode in MODES}
    rows, seen = [], set()
    for path in sorted((output/'rows').glob('*.json')):
        row = base.read(path)
        key = (row['id'], row['mode'])
        if key not in allowed or key in seen or path.name != f'{key[0]}--{key[1]}.json':
            raise ValueError('Unexpected or duplicate result row: '+path.name)
        if any(row.get(k) != allowed[key][k] for k in ('question', 'reference')):
            raise ValueError('Result question/reference drift: '+path.name)
        seen.add(key)
        rows.append(row)
    return rows


def report(output: Path, meta: dict, cases: list[dict]) -> dict:
    """Use 50-row denominators and exclude failed scores, not replace them with zero."""
    rows = read_rows(output, cases)
    expected = len(cases) * len(MODES)
    summary = dict(expected_rows=expected, generated_rows=sum(bool(r.get('response')) for r in rows),
                   complete_rows=sum(row_complete(r) for r in rows), expected_scores=expected * len(METRICS),
                   valid_scores=sum(valid_score(r.get('scores', {}).get(m)) for r in rows for m in METRICS),
                   generation_errors=sum(bool(r.get('generation_error')) for r in rows),
                   score_errors=sum(sum(m in r.get('score_errors', {}) for m in METRICS) for r in rows))
    summary['status'] = 'complete' if summary['complete_rows'] == expected else 'incomplete'
    summary['validation_status'] = meta.get('final_validation', {}).get('status', 'pending')
    config = meta['contract']
    lines = ['# 현재 LightRAG v4 · V4 벤치마크 Ragas 평가', '',
             f"- 상태: **{summary['status']}** · 생성 {summary['generated_rows']}/{expected} · 채점 완료 {summary['complete_rows']}/{expected} · 유효 점수 {summary['valid_scores']}/{summary['expected_scores']}",
             f"- 호출: `{config['pipeline']}` · `mix` · 전사 `allowedTeamIds=null` · Redis/native 답변 캐시 OFF.",
             f"- 답변: `{config['model']}` · judge: `{config['judge_model']}` · 임베딩: `{config['embedding_model']}`.",
             f"- 실제 옵션: `{json.dumps(config['params'], ensure_ascii=False)}`.",
             '- 기존 200건 인덱스 재사용. 삽입·삭제·재색인 없음. 실제 v4 서비스 프롬프트/답변 모델 사용.',
             '- 실제 문맥·프롬프트·답변·참조·오류·시간을 rows JSON에 보존. 정답/reference는 생성에 전달하지 않는다.', '',
             '## 분류별 / split별 평균', '',
             '| 범위 | Faithfulness | Answer relevancy | Context recall |', '|:---|---:|---:|---:|']
    if summary['validation_status'] == 'source_changed':
        lines[0] = '# 시작 시점 LightRAG v4 · V4 벤치마크 Ragas 평가'
        lines[2:2] = ['> 실행 도중 소스 변경으로 종료 동일성 검증이 실패했다. 이 점수는 시작 시점 코드의 결과이며 **최신 코드 평가로 간주하지 않는다**.',
                      '> 유료 답변/지표는 보존했다. 기존 계약을 바꾸거나 자동 유료 재실행하지 않았다.', '']
    elif summary['validation_status'] not in ('passed', 'pending'):
        lines[2:2] = ['> 종료 입력 동일성 검증 실패. run.json의 final_validation을 확인한다.', '']

    def label(case):
        return 'verified_absence' if case['answer_kind'] == 'verified_absence' else case['category']

    groups = [('Overall', {c['id'] for c in cases})]
    groups += [(category, {c['id'] for c in cases if label(c) == category}) for category in dict.fromkeys(label(c) for c in cases)]
    groups += [(split, {c['id'] for c in cases if c['split'] == split}) for split in ('development', 'evaluation')]
    summary['groups'] = {}
    for name, ids in groups:
        cells, values_by_metric = [name], {}
        for metric in METRICS:
            values = [r['scores'][metric] for r in rows if r['id'] in ids and valid_score(r.get('scores', {}).get(metric))]
            mean = statistics.mean(values) if values else None
            values_by_metric[metric] = {'mean': mean, 'n': len(values), 'expected': len(ids)}
            cells.append(f'{mean:.4f} ({len(values)}/{len(ids)})' if values else f'N/A (0/{len(ids)})')
        summary['groups'][name] = values_by_metric
        lines.append('| '+' | '.join(cells)+' |')
    lines += ['', '## 시간과 오류', '']
    for key in ('retrieval_seconds', 'total_seconds'):
        values = [r[key] for r in rows if key in r]
        lines.append(f'- {key}: {statistics.mean(values):.3f}초 (n={len(values)})' if values else f'- {key}: N/A')
    lines += [f"- 생성 오류 {summary['generation_errors']}건, 채점 오류 {summary['score_errors']}건. 실패는 0점으로 넣지 않는다.", '',
              '## 해석 범위와 제한', '',
              '- Context Precision/Factual Correctness는 채점하지 않는다. 그래프 문맥 단위와 이름/ID 표기 차이로 기존 점수도 확정 정답률은 아니다.',
              '- 6개 관계 부재 문항은 답변 가능한 빈 목록 정답이며 verified_absence로 별도 집계한다.',
              '- 주평가도 그래프 경로/방향/집합의 결정적 판정은 아니다. structured_gold는 생성에 사용하지 않는다.',
              '- 합성 200건에서 파생한 50문항으로 독립 holdout이 아니다. development/evaluation 각 25문항을 구분한다.',
              '- 전사 normal query만 평가. 팀 권한/SSE/운영 전체 데이터는 검증하지 않는다.',
              '- 이전 v3와 프롬프트·모델·인덱스·파이프라인이 다를 수 있어 전체 시스템 비교이며 개별 변경의 인과 효과가 아니다.',
              '- 지연에는 모델 호출/재시도가 포함된다. judge 1회 채점으로 변동성/신뢰구간은 측정하지 않았다.',
              '- v4 adapter는 생성 token 사용량을 반환하지 않으므로 generation_usage=null. 정확한 비용은 provider 청구 내역에서 확인한다.']
    (output/'report.md').write_text('\n'.join(lines)+'\n', encoding='utf-8')
    base.save(output/'summary.json', summary)
    return summary


async def finalize_run(output: Path, meta: dict, settings, cases: list[dict]) -> None:
    """Persist final input verification failures as well as successful completion."""
    final = None
    try:
        final = contract(settings, output)
        final['preflight']['external'] = await verify_external_index(settings)
        check_contract(meta, final)
    except Exception as exc:
        validation = {'status': 'failed', 'error': base.safe_error(exc, 'final_validation')}
        if final is not None and 'external' in final['preflight']:
            old = meta['contract']
            validation.update(
                status='source_changed' if old['hashes'] != final['hashes'] else 'inputs_changed',
                changed_source_files=sorted(k for k in set(old['hashes']) | set(final['hashes'])
                                            if old['hashes'].get(k) != final['hashes'].get(k)),
                index_unchanged=old['preflight'] == final['preflight'],
            )
        meta['final_validation'] = validation
        base.save(output/'run.json', meta)
        report(output, meta, cases)
        raise
    meta['finished_at'] = datetime.now(timezone.utc).isoformat()
    meta['index_unchanged'] = True
    meta['final_validation'] = {'status': 'passed', 'index_unchanged': True}
    base.save(output/'run.json', meta)
    report(output, meta, cases)


async def main(args) -> None:
    """Run/resume 50 current v4 mix queries, or report offline without paid calls."""
    output = (base.ROOT/args.output).resolve()
    if not output.is_relative_to(HERE) or output == HERE:
        raise ValueError('Output must be a child of evaluation/v4')
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
    current = contract(settings, output)
    current['preflight']['external'] = await verify_external_index(settings)
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
    base.save(output/'index-preflight.json', current['preflight'])
    read_rows(output, cases)
    from google import genai
    from openai import AsyncOpenAI
    from ragas.llms import llm_factory
    from ragas.embeddings import GoogleEmbeddings
    from ragas.metrics.collections import Faithfulness, AnswerRelevancy, ContextRecall
    key = settings.gemini_api_key_candidates[0]
    client = genai.Client(api_key=key)
    judge_client = AsyncOpenAI(api_key=key, base_url='https://generativelanguage.googleapis.com/v1beta/openai/', timeout=120, max_retries=2)
    adapter = build_adapter(settings)
    try:
        judge = llm_factory(current['judge_model'], provider='openai', client=judge_client, temperature=0, max_tokens=8192)
        embeddings = GoogleEmbeddings(client=client, model=current['embedding_model'])
        metrics = dict(faithfulness=Faithfulness(llm=judge), answer_relevancy=AnswerRelevancy(llm=judge, embeddings=embeddings),
                       context_recall=ContextRecall(llm=judge))
        service = build_service(settings, adapter)
        for i, case in enumerate(cases):
            for mode in (MODES if i % 2 == 0 else tuple(reversed(MODES))):
                path = output/'rows'/f"{case['id']}--{mode}.json"
                row = base.read(path) if path.exists() else {**case, 'mode': mode}
                if row_complete(row):
                    continue
                if 'response' not in row:
                    for attempt in range(2):
                        try:
                            row.update(await retrieve_generate(adapter, service, generation_case(case)))
                            expected = set(case['expected_worklog_ids'])
                            row['id_recall'] = len(expected & set(row['retrieved_worklog_ids']))/len(expected) if expected else None
                            row.pop('generation_error', None)
                            base.save(path, row)
                            break
                        except Exception as exc:
                            row['generation_error'] = generation_error(exc)
                            base.save(path, row)
                            if quota_exhausted(row):
                                raise RuntimeError('Billing exhausted; saved rows can be resumed') from None
                            if attempt == 0:
                                await asyncio.sleep(3)
                if 'response' in row:
                    await score_row(row, path, judge, metrics)
                    base.save(path, row)
                print('DONE', case['id'], mode, json.dumps(row.get('scores', {})), flush=True)
                report(output, meta, cases)
                if quota_exhausted(row):
                    raise RuntimeError('Billing exhausted; saved rows can be resumed')
    finally:
        report(output, meta, cases)
        await adapter.close()
        await judge_client.close()
        await client.aio.aclose()
    if report(output, meta, cases)['status'] != 'complete':
        raise RuntimeError('Incomplete rows remain; rerun the same command to resume')
    await finalize_run(output, meta, settings, cases)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', default='docs/ai/evaluation/v4/results-current-v4-' + datetime.now(timezone.utc).strftime('%Y%m%d'))
    parser.add_argument('--report-only', action='store_true')
    asyncio.run(main(parser.parse_args()))
