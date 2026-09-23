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
                    {
                        "type": "function_call",
                        "call_id": "call-1",
                        "name": "read_match_context",
                        "arguments": '{"path":"a.py","query":"target"}',
                    },
                ],
            },
            {"id": "response-2", "output": []},
        ]
    )

    result = client.review_with_tools(
        context="diff",
        instructions="review",
        tools=[{"type": "function"}],
        execute_tool=lambda name, arguments: f"{name}:{arguments['path']}:{arguments['query']}",
    )

    assert result["id"] == "response-2"
    assert client.requests[1]["input_items"] == [
        {"type": "function_call_output", "call_id": "call-1", "output": "read_match_context:a.py:target"},
    ]


def test_create_structured_review_reads_nested_output_text() -> None:
    client = FakeResponsesClient(
        [
            {
                "output": [
                    {"type": "reasoning", "summary": []},
                    {
                        "type": "message",
                        "content": [
                            {"type": "output_text", "text": '{"summary":"ok","findings":[]}'},
                        ],
                    },
                ],
            }
        ]
    )

    result = client.create_structured_review(
        previous_response_id="response-1",
        instructions="review",
        schema={"type": "object"},
    )

    assert result == '{"summary":"ok","findings":[]}'
