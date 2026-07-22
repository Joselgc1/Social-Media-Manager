"""
OpenAI provider implementation.
Translates the unified interface into openai SDK calls.
"""

import json
import logging

from openai import AsyncOpenAI

from app.ai.providers.base import LLMProvider, LLMResponse

logger = logging.getLogger(__name__)


class OpenAIProvider(LLMProvider):
    def __init__(self, api_key: str):
        self.client = AsyncOpenAI(api_key=api_key)

    # ── Main chat call ───────────────────────────────────────

    async def chat(
        self,
        model: str,
        system_prompt: str,
        messages: list[dict],
        tools: list[dict] | None = None,
        temperature: float = 0.7,
        max_tokens: int = 500,
    ) -> LLMResponse:

        # OpenAI expects the system prompt as the first message
        full_messages = [{"role": "system", "content": system_prompt}] + messages

        kwargs = {
            "model": model,
            "messages": full_messages,
            "temperature": temperature,
            "max_completion_tokens": max_tokens,
        }

        if tools:
            kwargs["tools"] = self._convert_tools(tools)
            kwargs["tool_choice"] = "auto"

        response = await self.client.chat.completions.create(**kwargs)
        return self._normalize(response)

    # ── Continue after tool execution ────────────────────────

    async def continue_after_tool(
        self,
        model: str,
        system_prompt: str,
        messages: list[dict],
        tool_call_id: str,
        tool_name: str,
        tool_result: str,
        tool_history: list[dict] | None = None,
        tools: list[dict] | None = None,
        temperature: float = 0.7,
        max_tokens: int = 500,
    ) -> LLMResponse:

        full_messages = [{"role": "system", "content": system_prompt}] + messages

        history = tool_history or [{
            "id": tool_call_id,
            "name": tool_name,
            "arguments": {},
            "result": tool_result,
        }]
        for entry in history:
            full_messages.append({
                "role": "assistant",
                "tool_calls": [{
                    "id": entry["id"],
                    "type": "function",
                    "function": {
                        "name": entry["name"],
                        "arguments": json.dumps(entry.get("arguments") or {}, ensure_ascii=False),
                    },
                }],
            })
            full_messages.append({
                "role": "tool",
                "tool_call_id": entry["id"],
                "content": entry["result"],
            })

        kwargs = {
            "model": model,
            "messages": full_messages,
            "temperature": temperature,
            "max_completion_tokens": max_tokens,
        }

        if tools:
            kwargs["tools"] = self._convert_tools(tools)
            kwargs["tool_choice"] = "auto"

        response = await self.client.chat.completions.create(**kwargs)
        return self._normalize(response)

    # ── Internal helpers ─────────────────────────────────────

    def _convert_tools(self, tools: list[dict]) -> list[dict]:
        """
        Convert our universal tool format to OpenAI's format.
        Universal: {"name", "description", "parameters"}
        OpenAI:    {"type": "function", "function": {"name", "description", "parameters"}}
        """
        return [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t["description"],
                    "parameters": t["parameters"],
                },
            }
            for t in tools
        ]

    def _normalize(self, response) -> LLMResponse:
        """Convert OpenAI's response into our unified LLMResponse."""
        choice = response.choices[0]
        tool_calls = None

        if choice.message.tool_calls:
            tool_calls = []
            for tc in choice.message.tool_calls:
                try:
                    args = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    args = {}
                tool_calls.append({
                    "id": tc.id,
                    "name": tc.function.name,
                    "arguments": args,
                })

        return LLMResponse(
            text=choice.message.content,
            tool_calls=tool_calls,
            usage={
                "input_tokens": response.usage.prompt_tokens,
                "output_tokens": response.usage.completion_tokens,
            },
            raw_response=response,
        )

    # ── Vision ───────────────────────────────────────────────

    async def analyze_image(
        self,
        model: str,
        image_base64: str,
        media_type: str,
        prompt: str,
        max_tokens: int = 300,
    ) -> LLMResponse:
        """Analyze an image using OpenAI's vision capability."""
        response = await self.client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{media_type};base64,{image_base64}",
                                "detail": "low",  # Save tokens; payment screenshots don't need high detail
                            },
                        },
                    ],
                }
            ],
            max_completion_tokens=max_tokens,
        )
        return self._normalize(response)
