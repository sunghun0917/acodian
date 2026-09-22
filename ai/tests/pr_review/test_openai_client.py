from typing import Any

from app.pr_review.openai_client import OpenAIResponsesClient


class FakeResponsesClient(OpenAIResponsesClient):
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        super().__init__(api_key="test", model="gpt-5.6-terra")
        self.responses = responses
        self.requests: list[dict[str, Any]] = []

    def _create(self, **kwargs: Any) -> dict[str, Any]:
        self.requests.append(kwargs)
        return self.responses.pop(0)


def test_review_with_tools_returns_tool_outputs_to_responses_api() -> None:
    client = FakeResponsesClient(
        [
            {
                "id": "response-1",
                "output": [
                    {"type": "function_call", "call_id": "call-1", "name": "read_file", "arguments": '{"path":"a.py"}'},
                ],
            },
            {"id": "response-2", "output": []},
        ]
    )

    result = client.review_with_tools(
        context="diff",
        instructions="review",
        tools=[{"type": "function"}],
        execute_tool=lambda name, arguments: f"{name}:{arguments['path']}",
    )

    assert result["id"] == "response-2"
    assert client.requests[1]["input_items"] == [
        {"type": "function_call_output", "call_id": "call-1", "output": "read_file:a.py"},
    ]
