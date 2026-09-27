"""Offline regression checks for evaluation boundaries, without provider calls."""
import asyncio
import json
import os
import unittest
from unittest.mock import patch
from run_ragas import context_units, check_resume, safe_error, evaluate_row

class EvaluationBoundaryTests(unittest.TestCase):
    def test_exact_graph_and_chunk_context(self):
        prompt='<EVALUATION_CONTEXT>\nEntities\n'+json.dumps({'entity':'x','description':'y'})+'\n'+json.dumps({'content':'body'})+'\n</EVALUATION_CONTEXT>'
        _, units, chunks=context_units(prompt)
        self.assertEqual(len(units),2)
        self.assertEqual(chunks,['body'])

    def test_empty_context_fails_closed(self):
        with self.assertRaises(ValueError):
            context_units('<EVALUATION_CONTEXT>\nno records\n</EVALUATION_CONTEXT>')

    def test_resume_rejects_code_drift_but_allows_report(self):
        keys=['model','embedding_model','versions','benchmark_sha256','corpus_sha256','prompt_sha256','params','preflight','script_sha256']
        a=dict.fromkeys(keys,'same'); b={**a,'script_sha256':'changed'}
        with self.assertRaisesRegex(ValueError,'script_sha256'):
            check_resume(a,b)
        check_resume(a,b,report_only=True)

    def test_error_redacts_credentials(self):
        with patch.dict(os.environ,{'GEMINI_API_KEY':'secret-123'}):
            error=safe_error(ValueError('api_key=secret-123 failure'),'score')
        self.assertNotIn('secret-123',json.dumps(error))
        self.assertEqual(error['stage'],'score')

    def test_successful_abstention_is_not_rejudged(self):
        row={'answerable':False,'abstention':{'correct_abstention':True,'reason':'known'}}
        asyncio.run(evaluate_row(row,None,{}))
        self.assertTrue(row['abstention']['correct_abstention'])

if __name__=='__main__':
    unittest.main()
