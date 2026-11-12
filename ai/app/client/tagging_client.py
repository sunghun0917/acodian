from app.client.base_api_client import BaseApiClient
from app.model.tagging_model import MetaTag


class TaggingClient(BaseApiClient):
    async def list_tags(self) -> list[MetaTag]:
        response = await self.client.get(self.api_path("/api/internal/tags"))
        response.raise_for_status()

        data = response.json().get("data", {})
        items = data.get(
            "tags",
            data.get("items", data if isinstance(data, list) else []),
        )
        return [
            MetaTag.model_validate(
                {
                    "tagId": item.get("tagId", item.get("id")),
                    "tagName": item.get("tagName"),
                    "description": item.get("description"),
                    "usageCount": item.get("usageCount", item.get("usage_count", 0)),
                }
            )
            for item in items
        ]
