import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import run_ragas_reranker as runner

sys.path.insert(0, str(runner.base.AI))
from app.config.settings import Settings
from app.light.v4.model.worklog_query import WorklogLightQueryResponse
from app.light.v4.service.lightrag_adapter import LightRagWorklogIndexAdapter


class V4RunnerTests(unittest.TestCase):
    def test_only_current_mix_and_primary_metrics(self):
        self.assertEqual(runner.MODES, ('mix',))
        self.assertEqual(runner.METRICS, ['faithfulness', 'answer_relevancy', 'context_recall'])
        cases = runner.load_cases()
        self.assertEqual(len(cases), 50)
        self.assertEqual(sum(c['split'] == 'evaluation' for c in cases), 25)
        self.assertEqual(runner.generation_case(cases[0]), {'question': cases[0]['question']})

    def test_actual_context_keeps_graph_and_body_not_instructions(self):
        prompt = 'instructions\n---Context---\n```json\n{"entity":"User:15"}\n{"content":"worklog_id: 158"}\n```\n'
        context, units, chunks = runner.context_units(prompt)
        self.assertNotIn('instructions', context)
        self.assertEqual(len(units), 2)
        self.assertEqual(chunks, ['worklog_id: 158'])
        with self.assertRaises(ValueError):
            runner.context_units('missing context')

    def test_resume_rejects_v3_models_versions_and_missing_hashes(self):
        current = {'pipeline': 'app.light.v4', 'model': 'answer', 'judge_model': 'judge',
                   'versions': {'ragas': '0.4.3'}, 'hashes': {'adapter.py': 'abc'},
                   'preflight': {'indexed_documents': 200}}
        runner.check_contract({'contract': current}, current)
        for key, value in [('pipeline', 'app.light.v3'), ('model', 'other'),
                           ('versions', {'ragas': 'other'}), ('hashes', {}),
                           ('preflight', {'indexed_documents': 199})]:
            previous = copy.deepcopy(current)
            previous[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                runner.check_contract({'contract': previous}, current)

    def test_judge_cannot_be_an_answer_fallback(self):
        settings = Settings(_env_file=None, lightrag_answer_llm_model=runner.JUDGE_MODEL)
        with self.assertRaisesRegex(ValueError, 'judge'):
            runner.contract(settings)

    def test_wrapped_provider_quota_is_not_lost(self):
        try:
            try:
                raise ValueError('429 RESOURCE_EXHAUSTED quota')
            except ValueError as cause:
                raise RuntimeError('LightRAG generation failed') from cause
        except RuntimeError as error:
            details = runner.generation_error(error)
        self.assertTrue(runner.quota_exhausted({'generation_error': details}))
        self.assertNotIn('quota', runner.generation_error(ValueError('regular failure'))['message'])

    def test_external_chunks_must_match_local_content_and_provenance(self):
        chunks = {'chunk-1': {'content': 'body', 'full_doc_id': 'worklog-1', 'file_path': 'worklog://1'}}
        payloads = [{'id': 'chunk-1', 'workspace_id': '_', **chunks['chunk-1']}]
        runner.check_external_chunks(chunks, payloads)
        for key, value in [('content', 'changed'), ('full_doc_id', 'worklog-2'), ('id', 'other')]:
            wrong = copy.deepcopy(payloads)
            wrong[0][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                runner.check_external_chunks(chunks, wrong)
        with self.assertRaises(ValueError):
            runner.check_external_chunks(chunks, payloads * 2)

    def test_report_uses_50_and_excludes_failed_scores(self):
        cases = runner.load_cases()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            (output / 'rows').mkdir()
            row = {**cases[0], 'mode': 'mix', 'response': 'answer',
                   'scores': {'faithfulness': 1.0, 'answer_relevancy': None, 'context_recall': 0.5},
                   'score_errors': {'answer_relevancy': {'message': 'failed'}}}
            runner.base.save(output / 'rows' / f"{row['id']}--mix.json", row)
            meta = {'contract': {'pipeline': 'app.light.v4', 'model': 'answer',
                                'judge_model': 'judge', 'embedding_model': 'embedding',
                                'params': {'enable_rerank': False}}}
            summary = runner.report(output, meta, cases)
            self.assertEqual(summary['expected_rows'], 50)
            self.assertEqual(summary['expected_scores'], 150)
            self.assertEqual(summary['valid_scores'], 2)
            self.assertEqual(summary['complete_rows'], 0)
            self.assertEqual(summary['score_errors'], 1)
            self.assertEqual(summary['status'], 'incomplete')
            self.assertNotIn('naive', (output / 'report.md').read_text(encoding='utf-8'))
            meta['final_validation'] = {'status': 'source_changed', 'index_unchanged': True,
                                        'changed_source_files': ['adapter.py']}
            summary = runner.report(output, meta, cases)
            self.assertEqual(summary['validation_status'], 'source_changed')
            self.assertIn('최신 코드', (output / 'report.md').read_text(encoding='utf-8'))
            row['scores']['answer_relevancy'] = float('nan')
            self.assertFalse(runner.row_complete(row))


class V4GenerationTests(unittest.IsolatedAsyncioTestCase):
    async def test_final_preflight_failure_is_persisted_not_left_pending(self):
        meta = {'contract': {'pipeline': 'app.light.v4', 'model': 'answer',
                            'judge_model': 'judge', 'embedding_model': 'embedding', 'params': {}}}
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            (output / 'rows').mkdir()
            with patch.object(runner, 'contract', side_effect=ValueError('Index mismatch')):
                with self.assertRaisesRegex(ValueError, 'Index mismatch'):
                    await runner.finalize_run(output, meta, None, runner.load_cases())
            saved = runner.base.read(output / 'run.json')
            self.assertEqual(saved['final_validation']['status'], 'failed')
            self.assertEqual(saved['final_validation']['error']['message'], 'Index mismatch')
            self.assertEqual(runner.base.read(output / 'summary.json')['validation_status'], 'failed')
            self.assertIn('검증 실패', (output / 'report.md').read_text(encoding='utf-8'))

    async def test_resume_retries_quota_metric_after_preserved_success(self):
        row = {'response': 'answer', 'scores': {'faithfulness': 1.0},
               'score_errors': {'answer_relevancy': {'status_code': 429, 'message': 'quota'}}}
        seen = []
        async def evaluate(current, judge, metrics):
            name = next(iter(metrics))
            seen.append(name)
            if name == 'answer_relevancy':
                current['score_errors'].pop(name, None)
                current['scores'][name] = 0.8
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'row.json'
            with patch.object(runner.base, 'evaluate_row', side_effect=evaluate):
                await runner.score_row(row, path, None, {'faithfulness': object(), 'answer_relevancy': object()})
            self.assertEqual(seen, ['answer_relevancy'])
            self.assertEqual(runner.base.read(path)['scores']['answer_relevancy'], 0.8)

    async def test_scores_are_checkpointed_before_next_metric(self):
        row = {'response': 'answer', 'scores': {}}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'row.json'
            async def evaluate(current, judge, metrics):
                name = next(iter(metrics))
                if name == 'answer_relevancy':
                    saved = runner.base.read(path)
                    self.assertEqual(saved['scores']['faithfulness'], 1.0)
                    raise RuntimeError('interrupted')
                current['scores'][name] = 1.0
            with patch.object(runner.base, 'evaluate_row', side_effect=evaluate):
                with self.assertRaisesRegex(RuntimeError, 'interrupted'):
                    await runner.score_row(row, path, None, {'faithfulness': object(), 'answer_relevancy': object()})
            self.assertEqual(runner.base.read(path)['scores'], {'faithfulness': 1.0})

    async def test_uses_v4_service_actual_answer_model_and_no_cache(self):
        settings = Settings(_env_file=None, lightrag_answer_llm_model='answer-model')
        adapter = runner.build_adapter(settings)
        self.assertIsInstance(adapter, LightRagWorklogIndexAdapter)
        fake_rag = type('Rag', (), {'llm_model_func': AsyncMock(return_value='actual answer')})()
        prompt = 'rules\n---Context---\n{"entity":"User:15"}\n{"content":"worklog_id: 158"}'

        async def query(request):
            self.assertIsNone(request.allowed_team_ids)
            self.assertEqual(request.query, 'question')
            await adapter._complete_answer('question', system_prompt=prompt)
            adapter.raw = {'data': {'chunks': []}, 'metadata': {}}
            return WorklogLightQueryResponse(answer='actual answer', references=[])

        with patch.object(adapter, '_get_initialized_rag', AsyncMock(return_value=fake_rag)):
            service = runner.build_service(settings, adapter)
            self.assertIsNone(service._cache)
            with patch.object(service, 'query_worklogs', side_effect=query):
                row = await runner.retrieve_generate(adapter, service, {'question': 'question'})
        self.assertEqual(row['response'], 'actual answer')
        self.assertEqual(row['system_prompt'], prompt)
        self.assertEqual(row['retrieved_worklog_ids'], [158])
        self.assertEqual(len(row['contexts']), 2)
        fake_rag.llm_model_func.assert_awaited_once_with('question', answer_generation=True, system_prompt=prompt)

    async def test_native_answer_cache_disabled(self):
        settings = Settings(_env_file=None)
        rag = type('Rag', (), {'enable_llm_cache': True, 'llm_response_cache':
                              type('Cache', (), {'global_config': {'enable_llm_cache': True}})()})()
        with patch.object(LightRagWorklogIndexAdapter, '_build_lightrag', return_value=rag):
            adapter = runner.build_adapter(settings)
            built = adapter._build_lightrag()
        self.assertFalse(built.enable_llm_cache)
        self.assertFalse(built.llm_response_cache.global_config['enable_llm_cache'])


if __name__ == '__main__':
    unittest.main()
