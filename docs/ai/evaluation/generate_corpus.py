"""Deterministic offline corpus build; never connects to a database."""
import hashlib
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent
SEED = ROOT / "api/src/main/resources/db/dev-seed"
SOURCES = [SEED / name for name in ("3_ibank-worklog-seed-v6.sql", "1_ibank-team-seed.sql", "6_ibank-worklog-dependency.sql")]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_rows(path):
    """Read this repository's one-tuple-per-line INSERT seed subset, failing closed."""
    result = defaultdict(list)
    table = None
    columns = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        match = re.match(r"INSERT INTO (\w+) \(([^)]+)\) VALUES", line)
        if match:
            table, raw = match.groups()
            columns = [s.strip() for s in raw.split(",")]
        elif line.startswith("INSERT INTO"):
            table = None
        elif line.strip().startswith("(") and table in {"tb_department", "tb_user", "tb_team", "tb_worklog", "tb_worklog_dependency"}:
            assert table, "tuple without INSERT"
            tokens = re.findall(r"'(?:''|[^'])*'|NULL|true|false|-?\d+(?:\.\d+)?", line)
            assert len(tokens) == len(columns), f"unsupported SQL tuple in {path}"
            values = []
            for token in tokens:
                if token.startswith("'"):
                    value = token[1:-1].replace("''", "'")
                elif token == "NULL":
                    value = None
                elif token in ("true", "false"):
                    value = token == "true"
                else:
                    value = float(token) if "." in token else int(token)
                values.append(value)
            result[table].append(dict(zip(columns, values)))
    return dict(result)


def select(rows):
    """20 per team; Hamilton status quotas; SHA-256 ranking independent of benchmark."""
    selected = []
    for team in range(101, 111):
        groups = defaultdict(list)
        for row in rows:
            if row["team_id"] == team:
                groups[row["status_code"]].append(row)
        total = sum(map(len, groups.values()))
        quota = {status: len(group) * 20 // total for status, group in groups.items()}
        order = sorted(groups, key=lambda status: (-(len(groups[status]) * 20 % total), status))
        for status in order[:20 - sum(quota.values())]:
            quota[status] += 1
        for status, group in sorted(groups.items()):
            ranked = sorted(group, key=lambda row: hashlib.sha256(f"evaluation-v1:200:{row['worklog_id']}".encode()).hexdigest())
            selected.extend(ranked[:quota[status]])
    return sorted(selected, key=lambda row: row["worklog_id"])


def corpus():
    rows = select(read_rows(SOURCES[0])["tb_worklog"])
    refs = read_rows(SOURCES[1])
    ids = {r["worklog_id"] for r in rows}
    authors = {r["author_id"] for r in rows}
    teams = {r["team_id"] for r in rows}
    users = [{k: r[k] for k in ("user_id", "department_id", "user_name")} for r in refs["tb_user"] if r["user_id"] in authors]
    team_rows = [{k: r[k] for k in ("team_id", "department_id", "team_name", "status_code", "deleted_at")} for r in refs["tb_team"] if r["team_id"] in teams]
    dept_ids = {r["department_id"] for r in users + team_rows if r["department_id"] is not None}
    departments = [{k: r[k] for k in ("department_id", "department_name")} for r in refs["tb_department"] if r["department_id"] in dept_ids]
    edges = [r for r in read_rows(SOURCES[2])["tb_worklog_dependency"] if r["worklog_id"] in ids and r["depends_on_worklog_id"] in ids]
    return {"tb_department": departments, "tb_user": users, "tb_team": team_rows, "tb_worklog": rows, "tb_worklog_dependency": edges}


def literal(value):
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    return str(value)


def sql(data):
    lines = ["-- Synthetic evaluation only. Empty dedicated PostgreSQL database required.", "-- Not an application migration; not rerunnable; no credentials or contact fields.", "BEGIN;", "CREATE SCHEMA evaluation_v1;", "SET LOCAL search_path TO evaluation_v1;"]
    for table, rows in data.items():
        definitions = []
        for column, value in rows[0].items():
            typ = "BOOLEAN" if isinstance(value, bool) else "BIGINT" if column.endswith("_id") else "NUMERIC" if column == "actual_hours" else "TEXT"
            definitions.append(f"{column} {typ}")
        pk = {"tb_department": "department_id", "tb_user": "user_id", "tb_team": "team_id", "tb_worklog": "worklog_id", "tb_worklog_dependency": "worklog_id, depends_on_worklog_id"}[table]
        definitions.append(f"PRIMARY KEY ({pk})")
        fks = {"tb_user": [("department_id", "tb_department", "department_id")], "tb_team": [("department_id", "tb_department", "department_id")], "tb_worklog": [("author_id", "tb_user", "user_id"), ("team_id", "tb_team", "team_id")], "tb_worklog_dependency": [("worklog_id", "tb_worklog", "worklog_id"), ("depends_on_worklog_id", "tb_worklog", "worklog_id")]}
        for column, target, key in fks.get(table, []):
            definitions.append(f"FOREIGN KEY ({column}) REFERENCES {target} ({key})")
        lines.append(f"CREATE TABLE {table} (" + ", ".join(definitions) + ");")
        lines.append(f"INSERT INTO {table} (" + ", ".join(rows[0]) + ") VALUES")
        lines.append(",\n".join("  (" + ", ".join(literal(v) for v in row.values()) + ")" for row in rows) + ";")
    lines.append("COMMIT;")
    return "\n".join(lines) + "\n"


def main():
    data = corpus()
    HERE.joinpath("evaluation-corpus-v1.sql").write_text(sql(data), encoding="utf-8", newline="\n")
    rows = data["tb_worklog"]
    manifest = {"version": "v1-200", "purpose": "development-pilot, not holdout", "selection": {"method": "20 per team; Hamilton largest remainder by status; SHA256 evaluation-v1:200:<id>", "benchmark_independent": True, "source_count": 622, "count": 200}, "source_files": [{"path": str(p.relative_to(ROOT)).replace("\\", "/"), "sha256": digest(p)} for p in SOURCES], "worklog_ids": [r["worklog_id"] for r in rows], "team_counts": dict(sorted(Counter(str(r["team_id"]) for r in rows).items())), "status_counts": dict(sorted(Counter(r["status_code"] for r in rows).items())), "table_counts": {t: len(r) for t, r in data.items()}, "excluded": ["422 unselected original worklogs", "teams 111-115", "tags", "status history", "user skills/evaluations", "contact and credential fields"], "artifacts": {name: digest(HERE / name) for name in ("evaluation-corpus-v1.sql", "benchmark-v1.jsonl") if (HERE / name).exists()}}
    HERE.joinpath("corpus-manifest-v1.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps(manifest["table_counts"]))


if __name__ == "__main__":
    main()
