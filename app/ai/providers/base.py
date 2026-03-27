"""
Abstract base class for LLM providers.
Every provider (OpenAI, Anthropic, etc.) implements this interface
so the engine never touches SDK-specific code.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class LLMResponse:
    """Unified response format regardless of which provider generated it."""

    text: str | None = None
    tool_calls: list[dict] | None = None  # [{"id": "...", "name": "...", "arguments": {...}}]
    usage: dict = field(default_factory=lambda: {"input_tokens": 0, "output_tokens": 0})
    raw_response: object = None


class LLMProvider(ABC):
    """
    Every provider must implement chat(), continue_after_tool(), and analyze_image().
    The engine calls these and nothing else.
    """

    @abstractmethod
    async def chat(
        self,
        model: str,
        system_prompt: str,
        messages: list[dict],
        tools: list[dict] | None = None,
        temperature: float = 0.7,
        max_tokens: int = 500,
    ) -> LLMResponse:
        """Send a conversation to the LLM and return a unified response."""
        ...

    @abstractmethod
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
        """
        Continue the conversation after the engine executed a tool call.
        Appends the assistant's tool_call and the tool result to messages,
        then asks the LLM for its final reply.
        """
        ...

    @abstractmethod
    async def analyze_image(
        self,
        model: str,
        image_base64: str,
        media_type: str,
        prompt: str,
        max_tokens: int = 300,
    ) -> LLMResponse:
        """
        Send an image to the LLM for visual analysis.
        Used for payment screenshot verification.

        Parameters
        ----------
        image_base64 : Base64-encoded image data.
        media_type : MIME type (e.g., "image/jpeg", "image/png").
        prompt : What to analyze in the image.
        """
        ...
