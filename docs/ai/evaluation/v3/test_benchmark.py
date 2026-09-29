"""Offline contracts for the ID-free V3 benchmark."""

import json
import sys
import unittest
from collections import Counter

from build_benchmark import HERE, SELECTED, build

sys.path.insert(0, str(HERE.parent / 'v1'))
from generate_corpus import read_rows


class V3BenchmarkTests(unittest.TestCase):
    def test_generated_files_match_sources(self):
        for name, data in zip(("benchmark-v3.jsonl", "validation-details.jsonl", "manifest.json"), build()):
            self.assertEqual((HERE / name).read_bytes(), data)

    def test_category_split_and_gold_provenance(self):
        public = [json.loads(line) for line in (HERE / "benchmark-v3.jsonl").read_text(encoding="utf-8").splitlines()]
        private = [json.loads(line) for line in (HERE / "validation-details.jsonl").read_text(encoding="utf-8").splitlines()]
        v2_details = {row["id"]: row for row in map(json.loads, (
            HERE.parent / "v2/validation-details.jsonl").read_text(encoding="utf-8").splitlines())}
        self.assertEqual(len(public), len(private), 50)
        self.assertEqual(Counter(row["category"] for row in public),
                         {category: len(ids) for category, ids in SELECTED.items()})
        self.assertEqual(Counter(row["split"] for row in private),
                         {"development": 25, "evaluation": 25})
        self.assertEqual(Counter(row["anchor_style"] for row in private),
                         {"title_or_existing_fact": 30, "title_paraphrase": 14,
                          "content_paraphrase": 6})
        self.assertLessEqual(max(Counter(row["root_worklog_id"] for row in private).values()), 4)
        components = {}
        for row in private:
            components.setdefault(row["component_id"], set()).add(row["split"])
        self.assertTrue(all(len(splits) == 1 for splits in components.values()))
        self.assertEqual(len({row["question"] for row in public}), 50)
        for case, gold in zip(public, private):
            self.assertEqual(set(case), {"id", "category", "question", "reference"})
            self.assertEqual(case["id"], gold["id"])
            self.assertNotIn("ID", case["question"])
            self.assertNotIn("식별 정보:", case["reference"])
            source = v2_details[gold["source_v2_id"]]
            for field in ("root_worklog_id", "split", "component_id", "structured_gold",
                          "relation_evidence", "dependency_paths", "absence_check"):
                self.assertEqual(gold[field], source[field], (case["id"], field))

    def test_gold_relations_against_frozen_corpus(self):
        data = read_rows(HERE.parent / "v1" / "evaluation-corpus-v1.sql")
        worklogs = {row["worklog_id"]: row for row in data["tb_worklog"]}
        dependencies = {(row["worklog_id"], row["depends_on_worklog_id"])
                        for row in data["tb_worklog_dependency"]}
        parents = {root: {target for source, target in dependencies if source == root}
                   for root in worklogs}
        titles = Counter(row["title"] for row in worklogs.values())
        details = [json.loads(line) for line in (
            HERE / "validation-details.jsonl").read_text(encoding="utf-8").splitlines()]
        for case in details:
            root = case["root_worklog_id"]
            self.assertIn(root, worklogs)
            self.assertEqual(titles[worklogs[root]["title"]], 1, case["id"])
            anchor = case["anchor_evidence"]
            if case["anchor_style"] == "content_paraphrase":
                self.assertEqual(anchor["worklog_id"], root)
                self.assertIn(anchor["quote"], worklogs[root]["work_content"])
            else:
                self.assertIsNone(anchor)
            for edge in case["structured_gold"]["relations"]:
                source, target = edge["source_worklog_id"], edge["target_id"]
                if edge["relation"] == "DEPENDS_ON":
                    self.assertIn((source, target), dependencies, case["id"])
                elif edge["relation"] == "AUTHORED_BY":
                    self.assertEqual(worklogs[source]["author_id"], target, case["id"])
                elif edge["relation"] == "BELONGS_TO":
                    self.assertEqual(worklogs[source]["team_id"], target, case["id"])
                else:
                    self.fail(f"Unknown relation: {edge['relation']}")
            if case["answer_kind"] == "verified_absence":
                self.assertFalse(any(source == root for source, _ in dependencies), case["id"])
            subtype = case["subtype"]
            if subtype == "all_ancestors":
                reached, pending = set(), list(parents[root])
                while pending:
                    target = pending.pop()
                    if target not in reached:
                        reached.add(target)
                        pending.extend(parents[target] - reached)
                expected = reached
            elif subtype == "dependency":
                expected = parents[root]
            elif subtype == "dependency_dependency":
                expected = {grandparent for parent in parents[root]
                            for grandparent in parents[parent]}
            elif subtype.startswith("dependency_"):
                expected = parents[root]
            elif subtype == "no_registered_dependency":
                expected = set()
            else:
                expected = {root}
            self.assertEqual(set(case["structured_gold"]["worklog_ids"]), expected, case["id"])


if __name__ == "__main__":
    unittest.main()
