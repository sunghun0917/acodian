import asyncio

from app.model.tagging_model import MetaTag, NewTag, TaggingResult
from app.task import tagging_tasks


class FakeTaggingClient:
    def __init__(self) -> None:
        self.created_request: tuple[int, list[int], list[NewTag]] | None = None

    async def __aenter__(self) -> "FakeTaggingClient":
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None

    async def list_tags(self) -> list[MetaTag]:
        return [
            MetaTag(tagId=1, tagName="재고", description="재고 동기화 업무", usageCount=3),
            MetaTag(tagId=2, tagName="MCP 개발", description="MCP 기능 개발 업무", usageCount=2),
        ]


class FakeTaggingService:
    async def generate_tags(
        self,
        work_content: str,
        existing_tags: list[MetaTag],
    ) -> TaggingResult:
        assert work_content == "재고 배치 자동화 작업"
        assert [tag.tag_name for tag in existing_tags] == ["재고", "MCP 개발"]
        return TaggingResult(
            existing_tags=["재고"],
            new_tags=[
                NewTag(
                    tagName="배치자동화",
                    description="정기 배치 자동화 개선 업무에 사용하는 태그",
                )
            ],
        )


def test_generate_worklog_tags_returns_ids_without_legacy_callback(monkeypatch) -> None:
    fake_client = FakeTaggingClient()
    monkeypatch.setattr(tagging_tasks, "TaggingClient", lambda: fake_client)
    monkeypatch.setattr(tagging_tasks, "TaggingService", FakeTaggingService)

    result = asyncio.run(
        tagging_tasks._generate_identified_tags(work_content="재고 배치 자동화 작업")
    )

    assert fake_client.created_request is None
    assert result == {
        "existingTagIds": [1],
        "newTags": [
            {
                "tagName": "배치자동화",
                "description": "정기 배치 자동화 개선 업무에 사용하는 태그",
            }
        ],
    }
def test_new_tag_rejects_blank_fields():
    import pytest
    from pydantic import ValidationError
    from app.model.tagging_model import NewTag

    with pytest.raises(ValidationError):
        NewTag(tagName="  ", description="valid")
    with pytest.raises(ValidationError):
        NewTag(tagName="valid", description="  ")
