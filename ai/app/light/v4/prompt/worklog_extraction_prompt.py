"""Native KG 추출 역할 식별과 형식 재시도 지시만 정의한다."""

NATIVE_EXTRACTION_ROLE = (
    "---Role---\nYou are a Knowledge Graph Specialist responsible for extracting "
    "entities and relationships from the input text."
)

EXTRACTION_FORMAT_RETRY = """
The previous response failed format validation. Repeat this extraction task using the
native format only: each entity has exactly 4 fields and each relation exactly 5 fields,
separated by <|#|>. Keep every record on one line. All fields must be nonempty;
relations require meaningful comma-separated keywords. End with one <|COMPLETE|>.
Do not return JSON or invent facts to fill missing fields. If nothing is extractable,
return only <|COMPLETE|>. Preserve the task's correction/addition scope.
"""
