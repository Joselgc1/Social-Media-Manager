"""
Anthropic provider implementation.
Translates the unified interface into anthropic SDK calls.

Key differences from OpenAI:
- System prompt is a separate `system` parameter, not a message
- Tool definitions use `input_schema` instead of `parameters`
- Response content is a list of blocks (text, tool_use) instead of a single message
- Tool results use role "user" with a tool_result content block
"""

import json
import logging
from anthropic import AsyncAnthropic
from app.ai.providers.base import LLMProvider, LLMResponse

logger = logging.getLogger(__name__)


class AnthropicProvider(LLMProvider):
    def __init__(self, api_key: str):
        self.client = AsyncAnthropic(api_key=api_key)

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

        kwargs = {
            "model": model,
            "system": system_prompt,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }

        if tools:
            kwargs["tools"] = self._convert_tools(tools)
            kwargs["tool_choice"] = {"type": "auto"}

        response = await self.client.messages.create(**kwargs)
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
        tools: list[dict] | None = None,
        temperature: float = 0.7,
        max_tokens: int = 500,
    ) -> LLMResponse:

        # Anthropic expects the assistant's tool_use block followed by
        # a user message containing the tool_result block
        extended_messages = messages + [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": tool_call_id,
                        "name": tool_name,
                        "input": {},
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_call_id,
                        "content": tool_result,
                    }
                ],
            },
        ]

        kwargs = {
            "model": model,
            "system": system_prompt,
            "messages": extended_messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }

        if tools:
            kwargs["tools"] = self._convert_tools(tools)
            kwargs["tool_choice"] = {"type": "auto"}

        response = await self.client.messages.create(**kwargs)
        return self._normalize(response)

    # ── Vision ───────────────────────────────────────────────

    async def analyze_image(
        self,
        model: str,
        image_base64: str,
        media_type: str,
        prompt: str,
        max_tokens: int = 300,
    ) -> LLMResponse:
        """Analyze an image using Anthropic's vision capability."""
        response = await self.client.messages.create(
            model=model,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": media_type,
                                "data": image_base64,
                            },
                        },
                        {"type": "text", "text": prompt},
                    ],
                }
            ],
            max_tokens=max_tokens,
        )
        return self._normalize(response)

    # ── Internal helpers ─────────────────────────────────────

    def _convert_tools(self, tools: list[dict]) -> list[dict]:
        """
        Convert our universal tool format to Anthropic's format.
        Universal:  {"name", "description", "parameters"}
        Anthropic:  {"name", "description", "input_schema"}
        """
        return [
            {
                "name": t["name"],
                "description": t["description"],
                "input_schema": t["parameters"],
            }
            for t in tools
        ]

    def _normalize(self, response) -> LLMResponse:
        """Convert Anthropic's response into our unified LLMResponse."""
        text_parts = []
        tool_calls = []

        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_calls.append({
                    "id": block.id,
                    "name": block.name,
                    "arguments": block.input,
                })

        return LLMResponse(
            text="\n".join(text_parts) if text_parts else None,
            tool_calls=tool_calls if tool_calls else None,
            usage={
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
            },
            raw_response=response,
        )
