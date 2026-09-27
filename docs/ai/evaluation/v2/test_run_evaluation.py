"""Offline boundary/resume/report tests; no model, database, or smoke dataset."""
import asyncio
import copy
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import run_evaluation as runner


class EvaluationTests(unittest.TestCase):
    def test_generation_and_judge_models_are_separate_and_pinned(self):
        settings = SimpleNamespace(lightrag_llm_model='gemini-3.5-flash-lite',
            lightrag_embedding_model='gemini-embedding-001', model_dump=lambda **kwargs: {})
        with patch.object(runner.base, 'verify_local', return_value={}):
            contract = runner.contract(settings)
        self.assertEqual('gemini-3.5-flash-lite', contract['model'])
        self.assertEqual('gemini-3.8-flash', contract['judge_model'])
        changed = {**contract, 'judge_model': 'another-judge'}
        with self.assertRaisesRegex(ValueError, 'drift'):
            runner.check_contract({'contract': contract}, changed)
        settings.lightrag_llm_model = runner.JUDGE_MODEL
        with self.assertRaisesRegex(ValueError, 'must differ'):
            runner.contract(settings)

    def test_all_eighty_and_absence_answerable(self):
        cases = runner.load_cases()
        self.assertEqual(80, len(cases))
        absent = [c for c in cases if c['answer_kind'] == 'verified_absence']
        self.assertEqual(8, len(absent))
        self.assertTrue(all(c['answerable'] for c in absent))

    def test_generation_boundary_withholds_gold(self):
        case = runner.load_cases()[25]
        actual = runner.generation_case(case)
        self.assertEqual({'question': case['question'], 'expected_worklog_ids': []}, actual)
        self.assertNotIn('reference', actual)
        self.assertNotIn('structured_gold', actual)
        self.assertNotIn('category', actual)

    def test_all_repairs_off_builtin_mix_on(self):
        self.assertTrue(runner.FLAGS['builtin_mix_graph'])
        self.assertFalse(any(v for k, v in runner.FLAGS.items() if k != 'builtin_mix_graph'))
        self.assertFalse(runner.base.PARAMS['enable_rerank'])

    def test_contract_detects_drift(self):
        contract = {'hashes': {'benchmark': 'a'}, 'features': runner.FLAGS}
        runner.check_contract({'contract': contract}, contract)
        changed = copy.deepcopy(contract)
        changed['hashes']['benchmark'] = 'b'
        with self.assertRaisesRegex(ValueError, 'drift'):
            runner.check_contract({'contract': contract}, changed)

    def test_billing_stops_but_rate_limit_does_not(self):
        self.assertTrue(runner.quota_exhausted({'score_errors': {'x': {'status_code': 402}}}))
        self.assertTrue(runner.quota_exhausted({'generation_error': {'message': 'prepayment credits depleted'}}))
        self.assertFalse(runner.quota_exhausted({'generation_error': {'status_code': 429, 'message': 'rate limit'}}))

    def test_complete_requires_all_five_scores(self):
        row = {'response': 'answer', 'scores': {m: 0.0 for m in runner.METRICS}}
        self.assertTrue(runner.row_complete(row))
        row['scores']['factual_correctness'] = None
        self.assertFalse(runner.row_complete(row))
        row['scores']['factual_correctness'] = float('nan')
        self.assertFalse(runner.row_complete(row))

    def test_report_missing_scores_not_zero(self):
        cases = runner.load_cases()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            (output/'rows').mkdir()
            case = cases[0]
            runner.base.save(output/'rows'/f"{case['id']}--naive.json", {
                **case, 'mode': 'naive', 'response': 'answer', 'scores': {'faithfulness': 1.0},
                'retrieval_seconds': 1, 'total_seconds': 2})
            meta = {'contract': {'model': 'offline', 'judge_model': 'gemini-3.8-flash', 'embedding_model': 'offline'}}
            summary = runner.report(output, meta, cases)
            self.assertEqual('incomplete', summary['status'])
            self.assertEqual(1, summary['valid_scores'])
            report = (output/'report.md').read_text(encoding='utf-8')
            self.assertIn('1.0000 (1/20)', report)
            self.assertIn('N/A (0/20)', report)
            self.assertIn('verified_absence', report)
            self.assertIn('평가 모델: `gemini-3.8-flash`', report)

    def test_offline_report_rejects_input_drift(self):
        with self.assertRaisesRegex(ValueError, 'Report input drift'):
            runner.check_report_inputs({'contract': {'hashes': {}}})

    def test_unknown_rows_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            (output/'rows').mkdir()
            runner.base.save(output/'rows'/'foreign--mix.json', {'id': 'foreign', 'mode': 'mix'})
            with self.assertRaisesRegex(ValueError, 'Unexpected'):
                runner.read_rows(output, runner.load_cases())


class ScoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_factual_success_and_resume(self):
        row = {'question': 'q', 'reference': 'gold', 'response': 'answer', 'scores': {}, 'score_errors': {}}
        factual = AsyncMock()
        factual.ascore.return_value.value = 0.8
        with patch.object(runner.base, 'evaluate_row', new=AsyncMock()):
            await runner.score_row(row, None, {}, factual)
            await runner.score_row(row, None, {}, factual)
        self.assertEqual(0.8, row['scores']['factual_correctness'])
        factual.ascore.assert_awaited_once_with(response='answer', reference='gold')

    async def test_factual_billing_failure_can_resume_after_payment(self):
        row = {'reference': 'gold', 'response': 'answer', 'scores': {'factual_correctness': None},
               'score_errors': {'factual_correctness': {'status_code': 402, 'message': 'credits depleted'}}}
        factual = AsyncMock()
        factual.ascore.return_value.value = 0.6
        with patch.object(runner.base, 'evaluate_row', new=AsyncMock()):
            await runner.score_row(row, None, {}, factual)
        self.assertEqual(0.6, row['scores']['factual_correctness'])
        self.assertNotIn('factual_correctness', row['score_errors'])
        factual.ascore.assert_awaited_once()

    async def test_factual_failure_is_null_and_retry_bounded(self):
        row = {'reference': 'gold', 'response': 'answer', 'scores': {}, 'score_errors': {}}
        factual = AsyncMock()
        factual.ascore.side_effect = ValueError('bad judge')
        with patch.object(runner.base, 'evaluate_row', new=AsyncMock()), patch.object(runner.asyncio, 'sleep', new=AsyncMock()):
            await runner.score_row(row, None, {}, factual)
        self.assertIsNone(row['scores']['factual_correctness'])
        self.assertEqual(2, factual.ascore.await_count)
        self.assertIn('factual_correctness', row['score_errors'])

    async def test_offline_report_does_not_import_api(self):
        with tempfile.TemporaryDirectory(dir=runner.HERE) as directory:
            output = Path(directory)
            (output/'rows').mkdir()
            hashes = {str((runner.HERE/n).relative_to(runner.base.ROOT)): runner.base.sha(runner.HERE/n) for n in ('benchmark-v2.jsonl', 'validation-details.jsonl')}
            runner.base.save(output/'run.json', {'contract': {'model': 'offline', 'judge_model': 'gemini-3.8-flash', 'embedding_model': 'offline', 'hashes': hashes}})
            args = type('Args', (), {'output': str(output), 'report_only': True})()
            with patch.object(runner, 'contract', side_effect=AssertionError('API settings accessed')):
                await runner.main(args)
            self.assertTrue((output/'report.md').exists())


if __name__ == '__main__':
    unittest.main()
