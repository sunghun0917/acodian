"""Offline structural validation. Does not claim semantic entailment or DB execution."""
import json
import re
from collections import Counter
from generate_corpus import ROOT, HERE, SOURCES, corpus, digest, read_rows, sql


def require(condition, message):
    if not condition:
        raise ValueError(message)


def validate_cases(cases, data):
    worklogs = {r["worklog_id"]: r for r in data["tb_worklog"]}
    edges = {(r["worklog_id"], r["depends_on_worklog_id"]) for r in data["tb_worklog_dependency"]}
    require(len(cases) == 50, "expected 50 cases")
    require(len({c["id"] for c in cases}) == len(cases), "duplicate case ID")
    require(len({c["question"].strip() for c in cases}) == len(cases), "duplicate question")
    require(Counter(c["category"] for c in cases) == {"single_fact": 15, "multi_evidence": 10, "disambiguation": 10, "direct_relation": 10, "unanswerable": 5}, "category distribution")
    require(Counter(c["information_scope"] for c in cases) == {"shared_text": 35, "graph_enriched": 10, "unanswerable": 5}, "scope distribution")
    fields = {"title", "request_content", "work_content"}
    searchable = " ".join(str(value) for rows in data.values() for row in rows for value in row.values()).casefold()
    for case in cases:
        cid = case["id"]
        require(case["question"].strip() and case["reference"].strip(), f"{cid}: empty text")
        require(not re.search(r"worklog[-_: ]?\d+|\uc5c5\ubb34\uc77c\uc9c0\s*(?:ID|\ubc88\ud638)\s*[:=]?\s*\d+", case["question"], re.I), f"{cid}: ID in question")
        expected = case["expected_worklog_ids"]
        require(len(expected) == len(set(expected)), f"{cid}: duplicate expected ID")
        require(set(expected) <= set(worklogs), f"{cid}: unknown worklog ID")
        if not case["answerable"]:
            require(case["category"] == case["information_scope"] == "unanswerable", f"{cid}: negative scope")
            require(not expected and not case["evidence"] and not case["relation_evidence"], f"{cid}: negative evidence")
            check = case["absence_check"]
            require(check["scope"] and check["method"] and check["terms"], f"{cid}: absence protocol")
            require(all(term and term.casefold() not in searchable for term in check["terms"]), f"{cid}: negative topic found")
            continue
        require(expected and case["evidence"], f"{cid}: missing evidence")
        seen = set()
        for evidence in case["evidence"]:
            wid, field, quote = evidence["worklog_id"], evidence["source_field"], evidence["quote"]
            require(wid in expected, f"{cid}: evidence ID not expected")
            require(field in fields and quote and quote in (worklogs[wid][field] or ""), f"{cid}: invalid quote")
            seen.add(wid)
        require(seen == set(expected), f"{cid}: uncovered expected IDs")
        if case["category"] == "single_fact":
            require(len(expected) == 1, f"{cid}: single cardinality")
        if case["category"] in ("multi_evidence", "disambiguation"):
            require(len(expected) >= 2, f"{cid}: multiple sources required")
        require((case["information_scope"] == "graph_enriched") == bool(case["relation_evidence"]), f"{cid}: relation scope")
        for relation in case["relation_evidence"]:
            source, target, kind = relation["source_worklog_id"], relation["target_id"], relation["relation"]
            require(source in expected, f"{cid}: relation source outside evidence")
            if kind == "DEPENDS_ON":
                require(relation["target_type"] == "worklog" and target in expected and (source, target) in edges, f"{cid}: invalid dependency direction or endpoint")
            elif kind in ("AUTHORED_BY", "BELONGS_TO"):
                field, target_type = ("author_id", "user") if kind == "AUTHORED_BY" else ("team_id", "team")
                require(relation["target_type"] == target_type and worklogs[source][field] == target, f"{cid}: invalid relation")
            else:
                raise ValueError(f"{cid}: unknown relation")


def validate():
    manifest = json.loads((HERE / "corpus-manifest-v1.json").read_text(encoding="utf-8"))
    require({r["path"] for r in manifest["source_files"]} == {p.relative_to(ROOT).as_posix() for p in SOURCES}, "source file set")
    for source in manifest["source_files"]:
        require(digest(ROOT / source["path"]) == source["sha256"], "source hash mismatch: " + source["path"])
    require(set(manifest["artifacts"]) == {"evaluation-corpus-v1.sql", "benchmark-v1.jsonl"}, "artifact set")
    for name, expected in manifest["artifacts"].items():
        require(digest(HERE / name) == expected, "artifact hash mismatch: " + name)
    data = corpus()
    generated = HERE / "evaluation-corpus-v1.sql"
    require(generated.read_text(encoding="utf-8") == sql(data), "SQL differs from deterministic source projection")
    actual = read_rows(generated)
    require(actual == data, "SQL roundtrip mismatch")
    rows = actual["tb_worklog"]
    require(len(rows) == len({r["worklog_id"] for r in rows}) == 200, "worklog count or duplicate ID")
    require(manifest["worklog_ids"] == [r["worklog_id"] for r in rows], "manifest IDs")
    require(manifest["team_counts"] == dict(Counter(str(r["team_id"]) for r in rows)) == {str(t): 20 for t in range(101, 111)}, "team distribution")
    require(manifest["status_counts"] == dict(Counter(r["status_code"] for r in rows)), "status distribution")
    require(manifest["table_counts"] == {t: len(r) for t, r in actual.items()}, "table counts")
    require(all(not r["is_deleted"] for r in rows) and all(r["deleted_at"] is None for r in actual["tb_team"]), "deleted source")
    for table, column, parent, key in [("tb_user", "department_id", "tb_department", "department_id"), ("tb_team", "department_id", "tb_department", "department_id"), ("tb_worklog", "author_id", "tb_user", "user_id"), ("tb_worklog", "team_id", "tb_team", "team_id"), ("tb_worklog_dependency", "worklog_id", "tb_worklog", "worklog_id"), ("tb_worklog_dependency", "depends_on_worklog_id", "tb_worklog", "worklog_id")]:
        targets = {r[key] for r in actual[parent]}
        require(all(r[column] is None or r[column] in targets for r in actual[table]), f"FK violation: {table}.{column}")
    serialized = generated.read_text(encoding="utf-8")
    require(not re.search(r"(?im)^\s*(DROP|TRUNCATE|DELETE|UPDATE)\b|ON CONFLICT", serialized), "unsafe SQL")
    require(not re.search(r"password_hash|profile_image_url|\bemail\b|\bphone\b|\$2[aby]\$|[\w.+-]+@[\w.-]+", serialized), "contact/credential leakage")
    cases = [json.loads(line) for line in (HERE / "benchmark-v1.jsonl").read_text(encoding="utf-8").splitlines()]
    validate_cases(cases, actual)
    print("PASS: 200 worklogs / 10 teams / 77 dependencies / 50 cases; hashes, SQL roundtrip, FK, quotes, relations, negative scans")


if __name__ == "__main__":
    validate()
