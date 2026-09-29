"""Build the ID-free V3 sample from frozen V2 cases; never index gold files."""

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import sys

HERE = Path(__file__).resolve().parent
V2 = HERE.parent / "v2"
sys.path.insert(0, str(HERE.parent / 'v1'))
from generate_corpus import read_rows
SELECTED = {
    "simple_fact": (1, 2, 3, 4, 5, 6, 10, 11, 16, 19),
    "direct_relation": (21, 23, 24, 26, 29, 30, 31, 33, 35, 37, 38, 39),
    "multi_relation": (41, 42, 43, 45, 47, 48, 49, 51, 52, 54, 56, 57, 58, 59),
    "dependency_closure": (61, 62, 63, 64, 65, 66, 70, 71),
    "relation_absence": (73, 74, 75, 77, 78, 80),
}

# Semantic anchors are authored separately from the gold; none names a target ID.
PARAPHRASES = {
    21: "권한 매트릭스 보정 뒤 후속 조치를 검토한 업무일지는 누가 썼나요?",
    24: "에러 토스트가 노출되는 원인을 분석한 업무일지의 작성자는 누구인가요?",
    29: "P0 회귀 현상을 재현해 달라고 요청한 업무는 어느 팀에서 기록했나요?",
    31: "에러 토스트 노출 원인을 분석한 업무는 어느 팀 소속인가요?",
    35: "권한 매트릭스 보정 후속 조치를 검토한 업무가 바로 의존한 선행업무는 무엇인가요?",
    38: "릴리즈 노트 누락 건을 승인 전에 검토한 업무는 무엇을 직접 선행업무로 삼았나요?",
    42: "에러 토스트 노출 문제를 이전 대응과 현재 흔적을 같은 축에서 비교해 원인을 분석한 기록의 직접 선행업무와 그 작성자는 누구인가요?",
    47: "보고용 필터 조정의 검증 결과를 정리한 기록보다 앞선 업무와 그 작성자는 누구인가요?",
    48: "권한 매트릭스 보정에서 남은 리스크와 결정 포인트를 짚은 후속 검토 업무의 바로 전 단계와 그 소속 팀은 어디인가요?",
    51: "릴리즈 노트 누락 건을 승인 전에 검토한 업무의 바로 앞 단계와 그 단계의 소속 팀은 어디인가요?",
    54: "필터 조정 결과를 넘기며 적용 조건값과 필터 조합을 먼저 남긴 검증 정리 업무의 직접 선행업무와 그 소속 팀은 어디인가요?",
    56: "릴리즈 노트 누락 승인 전 검토 기록에서 선행업무를 두 차례 따라가면 어디에 도착하나요?",
    59: "보정 승인 지연에서 직접 닿는 부분을 반영하고 변경 이유를 남긴 수정 업무의 선행관계를 두 단계 따라가면 어디에 도달하나요?",
    62: "보정 승인 지연의 보고 내용을 정리한 업무보다 앞서 등록된 직·간접 선행업무를 모두 알려 주세요. 현재 200건의 평가 코퍼스에 등록된 관계만 기준으로 답하세요.",
    64: "보고용 필터 조정 승인 전 검토에 이르기까지 앞선 업무들을 빠짐없이 알려 주세요. 현재 200건의 평가 코퍼스에 등록된 관계만 기준으로 답하세요.",
    66: "창고별 수량 편차를 수정 적용한 업무의 모든 선행업무를 직접·간접 구분 없이 알려 주세요. 현재 200건의 평가 코퍼스에 등록된 관계만 기준으로 답하세요.",
    71: "비슷한 시기의 기록, 로그 변화와 팀 메모를 맞춰 매장 문의 반복 원인을 분석한 업무의 모든 선행업무는 무엇인가요? 현재 200건의 평가 코퍼스에 등록된 관계만 기준으로 답하세요.",
    74: "운영 이관 체크리스트 누락에 직접 닿는 부분을 반영하고 후속 수정의 맥락을 남긴 업무에는 등록된 직접 선행업무가 있나요? 현재 200건의 평가 코퍼스에 등록된 관계만 기준으로 답하세요.",
    77: "P0 회귀 현상을 처음 재현해 달라고 요청한 기록에는 등록된 직접 선행업무가 있나요? 현재 200건의 평가 코퍼스에 등록된 관계만 기준으로 답하세요.",
    80: "마감 임박 업무 집계의 추가 검증을 기다리는 기록에 직접 선행업무가 등록돼 있나요? 현재 200건의 평가 코퍼스에 등록된 관계만 기준으로 답하세요.",
}
CONTENT_ANCHORS = {42, 48, 54, 59, 71, 74}
CONTENT_EVIDENCE = {
    42: "이전 대응분과 현재 흔적을 같은 축에서 비교",
    48: "남은 리스크와 결정 포인트를 같이 짚는",
    54: "적용한 조건값을 앞쪽에 적어 두었다",
    59: "변경 이유를 같이 적는 방식으로 정리했다",
    71: "비슷한 시기 기록, 로그 변화, 팀 내 메모",
    74: "후속 수정이 겹쳐도 맥락이 남도록 변경 이유",
}


def _read_jsonl(path: Path) -> dict[str, dict]:
    """Load keyed, unique V2 rows without changing their order or contents."""
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8-sig").splitlines()]
    result = {row["id"]: row for row in rows}
    if len(result) != len(rows):
        raise ValueError(f"Duplicate IDs in {path.name}")
    return result


def _sha(data: bytes) -> str:
    """Return a reproducible digest for a source or generated artifact."""
    return hashlib.sha256(data).hexdigest()


def _question(source: dict, number: int) -> tuple[str, str]:
    """Remove direct identifiers and response-format instructions."""
    if number in PARAPHRASES:
        return PARAPHRASES[number], ("content_paraphrase" if number in CONTENT_ANCHORS
                                      else "title_paraphrase")
    question = re.sub(r"\s*\(업무 ID:\s*\d+\)", "", source["question"])
    question = question.split(" 답변에 업무 ID를 함께 적고", 1)[0]
    return question, "title_or_existing_fact"


def build() -> tuple[bytes, bytes, bytes]:
    """Produce 50 public questions, private gold rows, and a source manifest."""
    if CONTENT_ANCHORS != CONTENT_EVIDENCE.keys():
        raise ValueError("Content anchors require source evidence")
    source_paths = [V2 / "benchmark-v2.jsonl", V2 / "validation-details.jsonl"]
    questions = _read_jsonl(source_paths[0])
    details = _read_jsonl(source_paths[1])
    corpus = HERE.parent / "v1" / "evaluation-corpus-v1.sql"
    worklogs = {row["worklog_id"]: row for row in read_rows(corpus)["tb_worklog"]}
    public, private = [], []
    for category, numbers in SELECTED.items():
        for number in numbers:
            source_id = f"v2-{number:03}"
            case, gold = questions[source_id], details[source_id]
            if case["category"] != category or gold["id"] != source_id:
                raise ValueError(f"V2 category/gold mismatch: {source_id}")
            question, anchor_style = _question(case, number)
            if (re.search(r"(?:업무|작성자|팀)\s*ID|(?:Worklog|User|Team):\d+|업무\s+\d+|\d+\s*번\s*업무", question, re.I)
                    or any(digit != "200" for digit in re.findall(
                        r"(?<![A-Za-z가-힣0-9])\d{2,}(?![A-Za-z가-힣0-9])", question))):
                raise ValueError(f"Direct identifier leaked: {source_id}")
            new_id = f"v3-{len(public) + 1:03}"
            public.append({"id": new_id, "category": category, "question": question,
                           "reference": case["reference"].split("\n식별 정보:", 1)[0]})
            anchor_evidence = None
            if number in CONTENT_ANCHORS:
                quote = CONTENT_EVIDENCE[number]
                if quote not in worklogs[gold["root_worklog_id"]]["work_content"]:
                    raise ValueError(f"Content anchor missing from source: {source_id}")
                anchor_evidence = {"worklog_id": gold["root_worklog_id"],
                                   "source_field": "work_content", "quote": quote}
            private.append({**gold, "id": new_id, "source_v2_id": source_id,
                            "anchor_style": anchor_style, "anchor_evidence": anchor_evidence})
    if len(public) != 50 or len({row["question"] for row in public}) != 50:
        raise ValueError("V3 must contain 50 unique questions")
    if Counter(row["split"] for row in private) != {"development": 25, "evaluation": 25}:
        raise ValueError("V3 split must remain balanced")
    output = ["".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows).encode("utf-8")
              for rows in (public, private)]
    manifest = {
        "schema_version": "3.0", "questions": 50,
        "categories": dict(Counter(row["category"] for row in public)),
        "split_counts": dict(Counter(row["split"] for row in private)),
        "anchor_styles": dict(Counter(row["anchor_style"] for row in private)),
        "source_v2_sha256": {path.name: _sha(path.read_bytes()) for path in source_paths},
        "source_corpus_sha256": _sha(corpus.read_bytes()),
        "artifact_sha256": {name: _sha(data) for name, data in zip(
            ("benchmark-v3.jsonl", "validation-details.jsonl"), output)},
        "evaluation_scope": "fixed_200_worklog_induced_subgraph",
        "index_policy": "Reuse V2 indexes; never index benchmark or gold artifacts",
    }
    return *output, (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def main() -> None:
    """Write artifacts or compare them byte-for-byte with the frozen build."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    for name, data in zip(("benchmark-v3.jsonl", "validation-details.jsonl", "manifest.json"), build()):
        path = HERE / name
        if args.check:
            if not path.exists() or path.read_bytes() != data:
                raise ValueError(f"V3 artifact drift: {name}")
        else:
            path.write_bytes(data)
    print("V3 50 cases verified" if args.check else "V3 50 cases generated")


if __name__ == "__main__":
    main()
