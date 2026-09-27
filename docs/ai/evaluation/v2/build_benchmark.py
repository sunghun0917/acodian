"""Offline V2 benchmark generation; no model, database, or index mutations."""
import argparse
import hashlib
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from generate_corpus import read_rows, digest

SOURCE = HERE.parent / "evaluation-corpus-v1.sql"
V1 = HERE.parent / "benchmark-v1.jsonl"

# Manually authored short questions and verbatim answer spans from existing text.
FACTS = [
 (15,"수정 작업과 번갈아 수행한 활동은 무엇인가?","수정과 기록을 번갈아 가며 진행했다"),
 (28,"취소 처리하더라도 맥락을 남긴 이유는 무엇인가?","맥락까지 끊기면 나중에 같은 질문이 다시 올 수 있어"),
 (33,"직접 작업보다 먼저 정리한 판단 사항은 무엇인가?","누구에게 우선순위를 주고 어떤 판단 기준으로 넘길지"),
 (51,"진행 보고와 함께 드러내려 한 두 가지는 무엇인가?","운영 영향과 남은 리스크"),
 (36,"단순 원인 추정을 피하기 위해 무엇을 비교했는가?","이전 대응분과 현재 흔적을 같은 축에서 비교"),

]


def require(ok, message):
    """Reject invalid artifacts instead of producing a partial benchmark."""
    if not ok:
        raise ValueError(message)


def topology(worklogs, edges):
    """Return directed adjacency and stable weak-component labels."""
    adjacency = {wid: [] for wid in worklogs}
    neighbors = {wid: set() for wid in worklogs}
    for source, target in sorted(edges):
        require(source in worklogs and target in worklogs, "unknown edge endpoint")
        adjacency[source].append(target)
        neighbors[source].add(target)
        neighbors[target].add(source)
    labels = {}
    for wid in sorted(worklogs):
        if wid in labels:
            continue
        pending = [wid]
        while pending:
            node = pending.pop()
            if node in labels:
                continue
            labels[node] = wid
            pending.extend(sorted(neighbors[node] - labels.keys()))
    for wid in worklogs:
        paths_from(wid, adjacency)
    return adjacency, labels


def paths_from(start, adjacency):
    """Enumerate dependency paths; reject cycles instead of calling them complete."""
    paths = []
    def visit(node, path):
        for target in adjacency[node]:
            require(target not in path, "dependency cycle")
            extended = path + [target]
            paths.append(extended)
            visit(target, extended)
    visit(start, [start])
    return paths


def relation(source, kind, target):
    return {"source_worklog_id": source, "relation": kind, "target_id": target,
            "target_type": {"DEPENDS_ON": "worklog", "AUTHORED_BY": "user", "BELONGS_TO": "team"}[kind]}


def build():
    """Derive benchmark and provenance manifest from the unchanged V1 corpus."""
    data = read_rows(SOURCE)
    w = {r["worklog_id"]: r for r in data["tb_worklog"]}
    users = {r["user_id"]: r["user_name"] for r in data["tb_user"]}
    teams = {r["team_id"]: r["team_name"] for r in data["tb_team"]}
    edges = {(r["worklog_id"], r["depends_on_worklog_id"]) for r in data["tb_worklog_dependency"]}
    adjacency, components = topology(w, edges)
    old = [json.loads(line) for line in V1.read_text(encoding="utf-8").splitlines()]
    old_ids = {wid for c in old for wid in c["expected_worklog_ids"]}
    cases = []
    def label(wid):
        row = w[wid]
        return f"「{row['title']}」(업무 ID: {wid})"
    def add(category, subtype, root, targets, relations, paths, question, reference,
            hops, fact=None, reused=None, negative=False):
        evidence_ids = sorted({root, *targets, *(r["source_worklog_id"] for r in relations)})
        component = components[root]
        require(all(components[i] == component for i in evidence_ids), "cross-component case")
        if category != "simple_fact" and not negative:
            question += " 답변에 업무 ID를 함께 적고, 작성자나 팀을 답할 때는 해당 ID도 함께 적어 주세요."
            identities = [f"{w[t]['title']} (업무 ID: {t})" for t in sorted(set(targets))]
            for r in relations:
                if r["relation"] == "AUTHORED_BY":
                    identities.append(f"업무 ID {r['source_worklog_id']}의 작성자: {users[r['target_id']]} (작성자 ID: {r['target_id']})")
                elif r["relation"] == "BELONGS_TO":
                    identities.append(f"업무 ID {r['source_worklog_id']}의 소속 팀: {teams[r['target_id']]} (팀 ID: {r['target_id']})")
            reference += "\n식별 정보: " + "; ".join(identities)
        gold = {"worklog_ids": sorted(set(targets)), "author_ids": [], "team_ids": [],
                "relations": relations}
        gold["author_ids"] = sorted({r["target_id"] for r in relations if r["relation"] == "AUTHORED_BY"})
        gold["team_ids"] = sorted({r["target_id"] for r in relations if r["relation"] == "BELONGS_TO"})
        evidence = [{"worklog_id": i, "source_field": "title", "quote": w[i]["title"]} for i in evidence_ids]
        if fact:
            evidence.append({"worklog_id": root, "source_field": "work_content", "quote": fact})
        cases.append({"id": f"v2-{len(cases)+1:03}", "category": category, "subtype": subtype,
          "question": question, "reference": reference, "root_worklog_id": root,
          "split": "evaluation" if int(hashlib.sha256(f'v2-component:{component}'.encode()).hexdigest()[:8],16)%5<2 else "development",
          "component_id": component, "information_scope": "shared_text" if category=="simple_fact" else "registered_relations",
          "answerable": True, "answer_kind": "verified_absence" if negative else "positive",
          "hop_count": hops, "dependency_paths": paths, "structured_gold": gold,
          "expected_worklog_ids": evidence_ids, "evidence": evidence, "relation_evidence": relations,
          "provenance": {"source": "../evaluation-corpus-v1.sql", "reused_v1_question_id": reused,
                         "v1_exposed_worklog_ids": sorted(set(evidence_ids)&old_ids)},
          "evaluation_scope": "fixed_200_worklog_induced_subgraph",
          "absence_check": {"kind": "zero_out_degree", "root_worklog_id": root} if negative else None})
    for case in old:
        if case["category"] == "single_fact":
            root = case["expected_worklog_ids"][0]
            add("simple_fact","body_fact",root,[root],[],[],case["question"],case["reference"],0,
                fact=case["evidence"][0]["quote"],reused=case["id"])
    for root, question, answer in FACTS:
        require(answer in w[root]["work_content"], f"fact quote missing: {root}")
        add("simple_fact","body_fact",root,[root],[],[],f"{label(root)} 업무에서 {question}",answer,0,fact=answer)
    ranked = sorted(w, key=lambda i: hashlib.sha256(f'v2-target:{i}'.encode()).hexdigest())
    with_dep = [i for i in ranked if adjacency[i]]
    with_two = [i for i in ranked if any(len(p)>=3 for p in paths_from(i,adjacency))]
    for subtype in ("author", "team", "dependency"):
        pool = with_dep if subtype=="dependency" else ranked
        for root in pool[:(6 if subtype=="dependency" else 7)]:
            if subtype=="dependency":
                targets=adjacency[root]; rels=[relation(root,"DEPENDS_ON",t) for t in targets]
                answer="직접 선행업무는 " + ", ".join(w[t]["title"] for t in targets)+"이다."
                question=f"{label(root)} 업무가 직접 의존하는 선행업무를 모두 알려 주세요."
                paths=[[root,t] for t in targets]
            else:
                targets=[root]; field,kind,names = ("author_id","AUTHORED_BY",users) if subtype=="author" else ("team_id","BELONGS_TO",teams)
                rels=[relation(root,kind,w[root][field])];answer=names[w[root][field]];paths=[]
                question=f"{label(root)} 업무의 작성자는 누구인가요?" if subtype=='author' else f"{label(root)} 업무의 소속 팀은 어디인가요?"
            add("direct_relation",subtype,root,targets,rels,paths,question,answer,1)
    for subtype in ("dependency_author","dependency_team","dependency_dependency"):
        for root in (with_two if subtype=="dependency_dependency" else with_dep)[:(6 if subtype=="dependency_dependency" else 7)]:
            parents=adjacency[root];rels=[relation(root,"DEPENDS_ON",t) for t in parents]
            if subtype=="dependency_dependency":
                paths=[p for p in paths_from(root,adjacency) if len(p)==3]
                targets=sorted({p[-1] for p in paths})
                rels += [relation(p[1],"DEPENDS_ON",p[2]) for p in paths]
                answer="; ".join(f"{w[p[1]]['title']}의 직접 선행업무: {w[p[2]]['title']}" for p in paths)
                question=f"{label(root)} 업무에서 선행관계를 정확히 두 단계 따라가면 어떤 업무에 도달하나요?"
            else:
                targets=parents;paths=[[root,t] for t in parents]
                field,kind,names=("author_id","AUTHORED_BY",users) if subtype.endswith("author") else ("team_id","BELONGS_TO",teams)
                rels += [relation(t,kind,w[t][field]) for t in targets]
                answer="; ".join(f"{w[t]['title']}: {names[w[t][field]]}" for t in targets)
                question=f"{label(root)} 업무의 직접 선행업무 제목과 각 선행업무의 {'작성자를' if subtype.endswith('author') else '소속 팀을'} 알려 주세요."
            add("multi_relation",subtype,root,targets,rels,paths,question,answer,2)
    for root in sorted(with_two, key=lambda i: -max(len(p) for p in paths_from(i,adjacency)))[:12]:
        paths=paths_from(root,adjacency);targets=sorted({p[-1] for p in paths})
        rels=[relation(s,"DEPENDS_ON",t) for s,t in sorted({(p[-2],p[-1]) for p in paths})]
        question=f"{label(root)} 업무의 모든 직·간접 선행업무를 빠짐없이 알려 주세요. 현재 200건의 평가 코퍼스에 등록된 관계만 기준으로 답하세요."
        add("dependency_closure","all_ancestors",root,targets,rels,paths,question,"; ".join(w[t]["title"] for t in targets),max(map(len,paths))-1)
    absent = [i for i in ranked if not adjacency[i]]
    is_eval = lambda i: int(hashlib.sha256(f"v2-component:{components[i]}".encode()).hexdigest()[:8],16)%5<2
    absence_targets = [i for i in absent if is_eval(i)][:4] + [i for i in absent if not is_eval(i)][:4]
    for root in absence_targets:
        question=f"{label(root)} 업무에 등록된 직접 선행업무가 있나요? 현재 200건의 평가 코퍼스에 등록된 관계만 기준으로 답하세요."
        add("relation_absence","no_registered_dependency",root,[],[],[],question,"현재 200건의 평가 코퍼스에 등록된 직접 선행업무가 없습니다.",1,negative=True)
    depths=Counter(max([0]+[len(p)-1 for p in paths_from(i,adjacency)]) for i in w)
    manifest={"schema_version":"2.2","sources":{"../evaluation-corpus-v1.sql":digest(SOURCE),"../benchmark-v1.jsonl":digest(V1)},
      "counts":{"worklogs":len(w),"dependencies":len(edges),"questions":len(cases)},
      "categories":dict(Counter(c["category"] for c in cases)),"split_counts":dict(Counter(c["split"] for c in cases)),
      "split_by_category":{s:dict(Counter(c["category"] for c in cases if c["split"]==s)) for s in ("development","evaluation")},
      "topology":{"max_dependency_depth":max(depths),"depth_distribution":dict(sorted(depths.items())),"branching_nodes":sum(len(v)>1 for v in adjacency.values()),"cycles":0,"weak_components":len(set(components.values()))},
      "limitations":["Same V1 synthetic corpus; not a fresh unseen-corpus holdout.","15 simple-fact questions reused verbatim from V1.","Evaluation split grouped by complete weak dependency component; author/team entities may overlap.","All absence and closure gold is relative to the fixed 200-node induced subgraph, not the full source database.","No real branching dependencies in this source; branch behavior only covered by synthetic algorithm tests.","No runtime ACL evaluation; no independent human adjudication yet.","V2 requires a dedicated runner adapter; do not pass directly to the V1 runner."],
      "index_policy":"Reuse existing V1 indexes; no new corpus or indexing artifacts","never_index":["benchmark-v2.jsonl","validation-details.jsonl","manifest.json"],
      "comparison_arms":["Naive existing V1 body index","LightRAG Mix existing V1 body and graph indexes"],
      "comparison_claim":"System-level comparison; graph metadata availability differs, not equal-information algorithm isolation",
      "metrics":{"all":"RAGAS faithfulness/relevance plus subtype macro averages, latency and tokens","relations":"entity-ID exact match, directed edge precision/recall/F1","closure":"exact-set accuracy plus worklog-ID precision/recall/F1","absence":"false-positive relation rate; verified absence is not unsupported-topic refusal"}}
    return cases,manifest


def split_cases(cases):
    """Keep evaluation input minimal; join validation-only metadata by case ID."""
    fields = ("id", "category", "question", "reference")
    public = [{key: case[key] for key in fields} for case in cases]
    details = [{key: value for key, value in case.items() if key not in fields or key == "id"} for case in cases]
    return public, details


def serialize(rows):
    return "".join(json.dumps(r,ensure_ascii=False,sort_keys=True)+"\n" for r in rows)


def validate(cases):
    """Recompute every field from frozen facts and enforce split isolation."""
    expected,_=build()
    require(cases==expected,"benchmark differs from deterministic source-derived gold")
    require(len(cases)==80,"expected 80 cases")
    require(len({c['question'] for c in cases})==len(cases),"duplicate question")
    groups={s:{i for c in cases if c['split']==s for i in c['expected_worklog_ids']} for s in ('development','evaluation')}
    require(not groups['development']&groups['evaluation'],"cross-split evidence IDs")


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--check',action='store_true');args=parser.parse_args()
    cases,manifest=build();validate(cases)
    public, details = split_cases(cases)
    artifacts={'benchmark-v2.jsonl':serialize(public),'validation-details.jsonl':serialize(details)}
    manifest['artifacts']={name:hashlib.sha256(text.encode()).hexdigest() for name,text in artifacts.items()}
    artifacts['manifest.json']=json.dumps(manifest,ensure_ascii=False,indent=2)+"\n"
    for name,text in artifacts.items():
        path=HERE/name
        if args.check:
            require(path.read_text(encoding='utf-8')==text,f'stale artifact: {name}')
        else:
            path.write_text(text,encoding='utf-8',newline='\n')
    print(json.dumps({'status':'PASS','counts':manifest['counts'],'splits':manifest['split_by_category'],'topology':manifest['topology']},ensure_ascii=False))


if __name__=='__main__':
    main()
