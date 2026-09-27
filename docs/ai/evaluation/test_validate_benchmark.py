"""Mutation regressions; no disk writes, network, DB, or third-party dependencies."""
import hashlib
import json
import unittest
from unittest.mock import patch
from generate_corpus import HERE, corpus
from validate_benchmark import validate, validate_cases


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.data = corpus()
        self.cases = [json.loads(s) for s in (HERE / "benchmark-v1.jsonl").read_text(encoding="utf-8").splitlines()]

    def test_valid_pack(self):
        validate()

    def test_original_24_preserved(self):
        original = b"".join((HERE / "benchmark-v1.jsonl").read_bytes().splitlines(keepends=True)[:24])
        self.assertEqual(hashlib.sha256(original).hexdigest(),
                         "743cd11bdeb90a8a8167cea975d8d30b0df32356edca6be2b65b36aff22458f5")

    def test_missing_case(self):
        with self.assertRaisesRegex(ValueError, "expected 50 cases"):
            validate_cases(self.cases[:-1], self.data)

    def test_wrong_category_distribution(self):
        self.cases[0]["category"] = "multi_evidence"
        with self.assertRaisesRegex(ValueError, "category distribution"):
            validate_cases(self.cases, self.data)

    def test_wrong_scope_distribution(self):
        self.cases[0]["information_scope"] = "graph_enriched"
        with self.assertRaisesRegex(ValueError, "scope distribution"):
            validate_cases(self.cases, self.data)

    def test_duplicate_question(self):
        self.cases[1]["question"] = self.cases[0]["question"]
        with self.assertRaisesRegex(ValueError, "duplicate question"):
            validate_cases(self.cases, self.data)

    def test_empty_reference(self):
        self.cases[-1]["reference"] = " "
        with self.assertRaisesRegex(ValueError, "empty text"):
            validate_cases(self.cases, self.data)

    def test_unknown_id(self):
        self.cases[0]["expected_worklog_ids"] = [999999]
        with self.assertRaisesRegex(ValueError, "unknown worklog"):
            validate_cases(self.cases, self.data)

    def test_wrong_quote(self):
        self.cases[0]["evidence"][0]["quote"] = "NONEXISTENT_EVIDENCE"
        with self.assertRaisesRegex(ValueError, "invalid quote"):
            validate_cases(self.cases, self.data)

    def test_duplicate_id(self):
        self.cases[1]["id"] = self.cases[0]["id"]
        with self.assertRaisesRegex(ValueError, "duplicate case"):
            validate_cases(self.cases, self.data)

    def test_outside_relation(self):
        self.cases[16]["relation_evidence"][0]["target_id"] = 999999
        with self.assertRaisesRegex(ValueError, "invalid dependency"):
            validate_cases(self.cases, self.data)

    def test_reversed_relation(self):
        r = self.cases[16]["relation_evidence"][0]
        r["source_worklog_id"], r["target_id"] = r["target_id"], r["source_worklog_id"]
        with self.assertRaisesRegex(ValueError, "invalid dependency"):
            validate_cases(self.cases, self.data)

    def test_source_drift(self):
        with patch("validate_benchmark.digest", return_value="changed"):
            with self.assertRaisesRegex(ValueError, "source hash mismatch"):
                validate()

    def test_negative_topic_present(self):
        self.cases[-1]["absence_check"]["terms"] = [self.data["tb_worklog"][0]["title"]]
        with self.assertRaisesRegex(ValueError, "negative topic found"):
            validate_cases(self.cases, self.data)

    def test_artifact_drift(self):
        from generate_corpus import digest
        with patch("validate_benchmark.digest", side_effect=lambda p: "changed" if p.name.endswith(".jsonl") else digest(p)):
            with self.assertRaisesRegex(ValueError, "artifact hash mismatch"):
                validate()


if __name__ == "__main__":
    unittest.main()
