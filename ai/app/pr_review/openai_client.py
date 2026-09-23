"""OpenAI Responses API의 제한된 function calling 클라이언트다."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx


class OpenAIResponsesClient:
    """PR 리뷰 에이전트에 필요한 Responses API 요청만 제공한다."""

    def __init__(self, *, api_key: str, model: str, api_url: str = "https://api.openai.com/v1") -> None:
        self._api_key = api_key
        self._model = model
        self._api_url = api_url.rstrip("/")

    def review_with_tools(
        self,
        *,
        context: str,
        instructions: str,
        tools: list[dict[str, object]],
        execute_tool: Callable[[str, dict[str, object]], str],
        max_tool_calls: int = 4,
    ) -> dict[str, Any]:
        """모델 function call을 최대 횟수까지 실행하고 마지막 응답을 반환한다."""
        response = self._create(input_items=context, instructions=instructions, tools=tools)
        calls_used = 0
        while True:
            calls = [item for item in response.get("output", []) if item.get("type") == "function_call"]
            if not calls:
                return response
            outputs: list[dict[str, str]] = []
            for call in calls:
                if calls_used >= max_tool_calls:
                    outputs.append(
                        {
                            "type": "function_call_output",
                            "call_id": str(call.get("call_id", "")),
                            "output": "Tool call limit reached. Complete the review using existing evidence.",
                        }
                    )
                    continue
                try:
                    arguments = json.loads(call.get("arguments", "{}"))
                    if not isinstance(arguments, dict):
                        raise ValueError("arguments must be an object")
                    result = execute_tool(str(call.get("name", "")), arguments)
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    result = f"Rejected tool input: {exc}"
                outputs.append(
                    {
                        "type": "function_call_output",
                        "call_id": str(call.get("call_id", "")),
                        "output": result,
                    }
                )
                calls_used += 1
            response = self._create(
                input_items=outputs,
                instructions=instructions,
                tools=tools if calls_used < max_tool_calls else None,
                previous_response_id=str(response["id"]),
            )

    def create_structured_review(
        self,
        *,
        previous_response_id: str,
        instructions: str,
        schema: dict[str, object],
    ) -> str:
        """도구 호출 뒤 최종 리뷰를 JSON schema 형식으로 요청한다."""
        response = self._create(
            input_items="Now produce the final pull request review.",
            instructions=instructions,
            previous_response_id=previous_response_id,
            text_format={
                "type": "json_schema",
                "name": "pull_request_review",
                "strict": True,
                "schema": schema,
            },
        )
        output_text = response.get("output_text")
        if not isinstance(output_text, str) or not output_text.strip():
            raise ValueError("OpenAI returned no structured review output")
        return output_text

    def _create(
        self,
        *,
        input_items: str | list[dict[str, str]],
        instructions: str,
        tools: list[dict[str, object]] | None = None,
        previous_response_id: str | None = None,
        text_format: dict[str, object] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, object] = {
            "model": self._model,
            "instructions": instructions,
            "input": input_items,
        }
        if tools:
            payload["tools"] = tools
        if previous_response_id:
            payload["previous_response_id"] = previous_response_id
        if text_format:
            payload["text"] = {"format": text_format}
        with httpx.Client(timeout=90) as client:
            response = client.post(
                f"{self._api_url}/responses",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json=payload,
            )
            response.raise_for_status()
            return response.json()
