"""Offline V2 regression tests. Synthetic graphs are tests, not benchmark facts."""
import copy
import unittest
from build_benchmark import build, paths_from, topology, validate, split_cases


class BenchmarkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases,cls.manifest=build()

    def test_source_derived_artifacts(self):
        validate(self.cases)
        self.assertEqual(self.manifest['categories'],{'simple_fact':20,'direct_relation':20,'multi_relation':20,'dependency_closure':12,'relation_absence':8})

    def test_public_validation_split_roundtrip(self):
        public, details = split_cases(self.cases)
        self.assertEqual(len(public), len(details))
        for source, row, detail in zip(self.cases, public, details):
            self.assertEqual(set(row), {"id", "category", "question", "reference"})
            self.assertFalse({"question", "reference", "category"} & detail.keys())
            self.assertNotIn("answer_text", detail["structured_gold"])
            self.assertEqual(source, row | detail)

    def test_relation_reference_has_target_ids(self):
        for case in self.cases:
            if case["category"] == "simple_fact":
                continue
            gold = case["structured_gold"]
            for wid in gold["worklog_ids"]:
                self.assertIn(f"업무 ID: {wid}", case["reference"])
            for uid in gold["author_ids"]:
                self.assertIn(f"작성자 ID: {uid}", case["reference"])
            for tid in gold["team_ids"]:
                self.assertIn(f"팀 ID: {tid}", case["reference"])

    def test_branch_closure_dedup(self):
        paths=paths_from(1,{1:[2,3],2:[4],3:[4],4:[]})
        self.assertEqual({p[-1] for p in paths},{2,3,4})
        self.assertEqual(len(paths),4)

    def test_cycle_rejected(self):
        with self.assertRaisesRegex(ValueError,'cycle'):
            topology({1:{},2:{}},{(1,2),(2,1)})

    def test_foreign_endpoint_rejected(self):
        with self.assertRaisesRegex(ValueError,'endpoint'):
            topology({1:{}},{(1,2)})

    def test_tampered_gold_rejected(self):
        altered=copy.deepcopy(self.cases)
        altered[0]['structured_gold']['worklog_ids']=[999]
        with self.assertRaises(ValueError):validate(altered)

    def test_incomplete_closure_rejected(self):
        altered=copy.deepcopy(self.cases)
        closure=next(c for c in altered if c['category']=='dependency_closure')
        closure['structured_gold']['worklog_ids'].pop()
        with self.assertRaises(ValueError):validate(altered)

    def test_wrong_direction_rejected(self):
        altered=copy.deepcopy(self.cases)
        edge=next(c for c in altered if c['subtype']=='dependency')['relation_evidence'][0]
        edge['source_worklog_id'],edge['target_id']=edge['target_id'],edge['source_worklog_id']
        with self.assertRaises(ValueError):validate(altered)

    def test_root_identification_uses_existing_body_id(self):
        for case in self.cases:
            if case["provenance"]["reused_v1_question_id"]:
                continue
            self.assertIn(f"업무 ID: {case['root_worklog_id']}",case["question"])
            self.assertNotIn("작성된",case["question"])

    def test_absence_is_answerable_empty_set(self):
        negatives=[c for c in self.cases if c['category']=='relation_absence']
        self.assertEqual(len(negatives),8)
        for c in negatives:
            self.assertTrue(c['answerable'])
            self.assertEqual(c['answer_kind'],'verified_absence')
            self.assertEqual(c['structured_gold']['worklog_ids'],[])

    def test_component_split_and_coverage(self):
        groups={s:{c['component_id'] for c in self.cases if c['split']==s} for s in ['development','evaluation']}
        self.assertFalse(groups['development'] & groups['evaluation'])
        for split in self.manifest['split_by_category'].values():self.assertEqual(len(split),5)
        self.assertEqual(max(c['hop_count'] for c in self.cases),4)


if __name__=='__main__':unittest.main()
