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
        self.client = AsyncOpenAI(api_key=api_key, timeout=90.0)

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
        if _uses_fixed_reasoning(model):
            return await self._responses_chat(model, system_prompt, messages, tools, max_tokens)

        # OpenAI expects the system prompt as the first message
        full_messages = [{"role": "system", "content": system_prompt}] + messages

        kwargs = {
            "model": model,
            "messages": full_messages,
            "max_completion_tokens": max_tokens,
        }
        if _uses_fixed_reasoning(model):
            kwargs["reasoning_effort"] = "none"
        else:
            kwargs["temperature"] = temperature

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
        if _uses_fixed_reasoning(model):
            history = tool_history or [{
                "id": tool_call_id,
                "name": tool_name,
                "arguments": {},
                "result": tool_result,
            }]
            return await self._responses_chat(
                model,
                system_prompt,
                messages,
                tools,
                max_tokens,
                tool_history=history,
            )

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
            "max_completion_tokens": max_tokens,
        }
        if _uses_fixed_reasoning(model):
            kwargs["reasoning_effort"] = "none"
        else:
            kwargs["temperature"] = temperature

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

    async def _responses_chat(
        self,
        model: str,
        system_prompt: str,
        messages: list[dict],
        tools: list[dict] | None,
        max_tokens: int,
        *,
        tool_history: list[dict] | None = None,
    ) -> LLMResponse:
        input_items = [{"role": "system", "content": system_prompt}, *messages]
        for entry in tool_history or []:
            input_items.extend([
                {
                    "type": "function_call",
                    "call_id": entry["id"],
                    "name": entry["name"],
                    "arguments": json.dumps(entry.get("arguments") or {}, ensure_ascii=False),
                },
                {"type": "function_call_output", "call_id": entry["id"], "output": entry["result"]},
            ])
        response = await self.client.responses.create(
            model=model,
            input=input_items,
            tools=[
                {"type": "function", "name": tool["name"], "description": tool["description"], "parameters": tool["parameters"]}
                for tool in tools or []
            ] or None,
            reasoning={"effort": "low"},
            max_output_tokens=max_tokens,
        )
        tool_calls = [
            {"id": item.call_id, "name": item.name, "arguments": json.loads(item.arguments)}
            for item in response.output
            if item.type == "function_call"
        ] or None
        return LLMResponse(
            text=response.output_text or "",
            tool_calls=tool_calls,
            usage={"input_tokens": response.usage.input_tokens, "output_tokens": response.usage.output_tokens},
            raw_response=response,
        )

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
                                "detail": "high",
                            },
                        },
                    ],
                }
            ],
            max_completion_tokens=max_tokens,
        )
        return self._normalize(response)


def _uses_fixed_reasoning(model: str) -> bool:
    return model.startswith("gpt-5.6-")
