"""Offline contracts for fixed-retrieval RAG ablation."""
import unittest
import asyncio
from run_ragas_ablation import deterministic_metrics, extract_relation_facts, check_contract, FrozenSourceStore

class AblationTests(unittest.TestCase):
    def test_gold_partial_support_is_positive(self):
        row={'chunks':['first fact','second fact','noise'],'contexts':[],
             'evidence':[{'quote':'first fact'},{'quote':'second fact'}],
             'relation_evidence':[],'expected_worklog_ids':[]}
        m=deterministic_metrics(row)
        self.assertEqual(m['gold_quote_recall'],1.0)
        self.assertEqual(m['gold_quote_average_precision'],1.0)

    def test_missing_gold_is_not_fabricated(self):
        row={'chunks':['noise'],'contexts':[],'evidence':[{'quote':'missing'}],
             'relation_evidence':[],'expected_worklog_ids':[]}
        self.assertEqual(deterministic_metrics(row)['gold_quote_recall'],0.0)
        self.assertEqual(deterministic_metrics(row)['gold_quote_average_precision'],0.0)

    def test_unanswerable_quote_metrics_are_not_applicable(self):
        row={'chunks':[],'contexts':[],'evidence':[],'relation_evidence':[],'expected_worklog_ids':[]}
        self.assertIsNone(deterministic_metrics(row)['gold_quote_recall'])

    def test_relation_direction_is_not_reversed(self):
        facts=extract_relation_facts(['{"description":"Worklog:463 directly depends on Worklog:192 (title); source_type=CONFIRMED"}'])
        self.assertIn((463,'DEPENDS_ON',192),facts)
        self.assertNotIn((192,'DEPENDS_ON',463),facts)

    def test_relation_evidence_does_not_create_context_facts(self):
        row={'chunks':[],'contexts':[],'evidence':[],'expected_worklog_ids':[],
             'relation_evidence':[{'source_worklog_id':463,'relation':'DEPENDS_ON','target_id':192}]}
        self.assertEqual(deterministic_metrics(row)['relation_fact_recall'],0.0)

    def test_snapshot_ignores_later_mutation(self):
        rows={1:{'name':'original'}}
        store=FrozenSourceStore(rows)
        rows[1]['name']='changed'
        result=asyncio.run(store.fetch_worklog_sources(None,[1,2]))
        self.assertEqual(result,{1:{'name':'original'}})

    def test_changed_repair_contract_fails_resume(self):
        with self.assertRaises(ValueError):
            check_contract({'repair_sha':'before'},{'repair_sha':'after'})

if __name__=='__main__': unittest.main()
