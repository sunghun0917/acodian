from app.pr_review.models import ReviewResult


def test_review_schema_requires_all_fields_and_forbids_extra_properties() -> None:
    schema = ReviewResult.model_json_schema()
    finding_schema = schema["$defs"]["ReviewFinding"]

    assert schema["required"] == ["summary", "findings"]
    assert schema["additionalProperties"] is False
    assert finding_schema["required"] == ["severity", "title", "body", "evidence"]
    assert finding_schema["additionalProperties"] is False
