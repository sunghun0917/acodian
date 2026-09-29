import tempfile
import unittest
from pathlib import Path

import run_evaluation as runner
import rescore_context_precision as rescorer


class RunnerTests(unittest.TestCase):
    def test_cases_and_generation_boundary(self):
        cases = runner.load_cases()
        self.assertEqual(len(cases), 50)
        self.assertEqual({c['split'] for c in cases}, {'development', 'evaluation'})
        self.assertEqual(sum(c['split'] == 'development' for c in cases), 25)
        self.assertEqual(sum(c['split'] == 'evaluation' for c in cases), 25)
        for case in cases:
            self.assertEqual(runner.generation_case(case),
                             {'question': case['question'], 'expected_worklog_ids': []})
        self.assertEqual(runner.MODES, ('naive', 'mix'))
        self.assertEqual(len(runner.METRICS), 5)
        self.assertEqual(runner.REPORT_METRICS, ('faithfulness', 'answer_relevancy', 'context_recall'))
        self.assertFalse(any(runner.FLAGS[k] for k in
                             ('postgres_relation_repair', 'anchor_replacement',
                              'extra_neo4j_expansion', 'rerank')))
        self.assertTrue(runner.FLAGS['builtin_mix_graph'])

    def test_report_denominators_and_splits(self):
        cases = runner.load_cases()
        with tempfile.TemporaryDirectory() as name:
            output = Path(name)
            (output / 'rows').mkdir()
            row = {**cases[0], 'mode': 'naive', 'response': 'answer',
                   'scores': {metric: 1.0 for metric in runner.METRICS}}
            runner.base.save(output / 'rows' / f"{cases[0]['id']}--naive.json", row)
            meta = {'contract': {'model': 'generator', 'judge_model': runner.JUDGE_MODEL,
                                 'embedding_model': 'embedding'}}
            summary = runner.report(output, meta, cases)
            self.assertEqual(summary['expected_rows'], 100)
            self.assertEqual(summary['expected_scores'], 300)
            self.assertEqual(summary['complete_rows'], 1)
            self.assertEqual(summary['valid_scores'], 3)
            self.assertEqual(summary['status'], 'incomplete')
            report = (output / 'report.md').read_text(encoding='utf-8')
            self.assertIn('Overall | naive', report)
            self.assertIn('development | naive', report)
            self.assertIn('evaluation | mix', report)
            self.assertIn('(1/50)', report)
            self.assertIn('(1/25)', report)
            self.assertIn('100', report)
            self.assertIn('300', report)
            self.assertIn('| 유형 | 모드 | Faithfulness | Answer relevancy | Context recall |', report)
            self.assertNotIn('| Factual F1 |', report)
            self.assertNotIn('| Chunk precision |', report)
            self.assertIn('Context Precision 제외:', report)

    def test_context_precision_uses_all_generation_records(self):
        row = {'question': '질문', 'reference': '정답',
               'contexts': ['{"entity":"A"}', '{"content":"본문"}'], 'chunks': ['본문']}
        self.assertEqual(rescorer.score_inputs(row)['retrieved_contexts'], row['contexts'])
        self.assertEqual(rescorer.METRIC, 'context_precision_all')

if __name__ == '__main__':
    unittest.main()
