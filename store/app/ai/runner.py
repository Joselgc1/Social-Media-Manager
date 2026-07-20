"""
Generic model and tool loop for internal AI agents.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from app.ai.agents.base import AgentDefinition
from app.ai.providers import AVAILABLE_MODELS, get_provider
from app.ai.providers import list_providers as _list_providers
from app.ai.safety import sanitize_customer_facing_text
from app.ai.tools.context import ToolExecutionContext
from app.ai.tools.executor import execute_tool
from app.ai.tools.registry import get_tool_schemas
from app.catalog.sheets import get_cached_catalog

logger = logging.getLogger(__name__)

DEFAULT_FALLBACK_TEXT = "Lo siento, no pude generar una respuesta. ¿Puedes repetir tu pregunta?"


@dataclass(slots=True)
class AgentRunContext:
    """Server-owned runtime context needed by an agent run."""

    customer: dict[str, Any]
    channel: str
    payment_methods: list[dict[str, Any]] = field(default_factory=list)
    vision_result: dict[str, Any] | None = None
    payment_proof_attempt: bool = False
    latest_user_message: str = ""
    session: Any | None = None
    integration_context: dict[str, Any] | None = None


@dataclass(slots=True)
class AgentRunResult:
    """Normalized result produced by a completed agent run."""

    text: str
    interactive: dict[str, Any] | None
    catalog_pdf: dict[str, Any] | None
    product_image: dict[str, Any] | None
    tool_log: list[dict[str, Any]]
    provider: str
    model: str
    usage: dict[str, int]
    was_fallback: bool
    requested_handoff: bool = False
    handoff_target: str | None = None
    escalated: bool = False


class AgentRunner:
    """Run a configured agent against the active LLM provider and tool executor."""

    def __init__(self, provider_getter=None, provider_lister=None):
        self._get_provider = provider_getter or get_provider
        self._list_providers = provider_lister or _list_providers

    async def run(
        self,
        agent: AgentDefinition,
        system_prompt: str,
        messages: list[dict],
        settings: dict,
        context: AgentRunContext,
    ) -> AgentRunResult:
        provider_name, model, provider, was_fallback, response = await self._initial_response(
            agent=agent,
            system_prompt=system_prompt,
            messages=messages,
            settings=settings,
        )
        usage = _empty_usage()
        _add_usage(usage, response.usage)

        interactive_payload = None
        catalog_pdf_payload = None
        product_image_payload = None
        tool_log: list[dict[str, Any]] = []
        rounds = 0
        requested_handoff = False
        handoff_target = None
        escalated = False
        single_use_tool_results: dict[str, dict] = {}
        allowed_tool_names = set(agent.tool_names)
        tool_schemas = get_tool_schemas(agent.tool_names)
        tool_context = ToolExecutionContext(
            customer=context.customer,
            channel=context.channel,
            payment_methods=context.payment_methods,
            vision_result=context.vision_result,
            payment_proof_attempt=context.payment_proof_attempt,
            latest_user_message=context.latest_user_message,
            session=context.session,
            integration_context=context.integration_context,
        )

        while response.tool_calls and rounds < agent.max_tool_rounds:
            rounds += 1
            tool_call = response.tool_calls[0]
            name = tool_call["name"]
            args = tool_call["arguments"]
            tc_id = tool_call["id"]

            logger.info("AI tool call", extra={"agent": agent.name, "tool_name": name, "tool_round": rounds})
            authorized = name in allowed_tool_names
            if not authorized:
                logger.warning(
                    "AI tool authorization rejected",
                    extra={"agent": agent.name, "tool_name": name, "tool_round": rounds},
                )
                result = {
                    "status": "error",
                    "message": f"Tool not authorized for agent '{agent.name}': {name}",
                }
            elif name in single_use_tool_results:
                result = {
                    **single_use_tool_results[name],
                    "duplicate_ignored": True,
                    "message": f"Duplicate tool call ignored: {name}",
                }
            elif name == "create_order" and context.payment_proof_attempt:
                result = {
                    "status": "error",
                    "message": (
                        "No se puede crear un pedido a partir de un comprobante de pago. "
                        "El pedido debe existir antes de validar el pago."
                    ),
                }
            else:
                result = await execute_tool(name, args, tool_context)
                if name in {"create_order", "update_payment_status", "finalize_checkout", "request_agent_handoff"} and result.get("status") != "error":
                    single_use_tool_results[name] = result
            if name == "escalate_to_human":
                requested_handoff = True
                escalated = result.get("status") == "escalated"
            if name == "request_agent_handoff" and result.get("type") == "agent_handoff":
                requested_handoff = True
                handoff_target = result.get("target_agent")
                logger.info(
                    "AI handoff requested",
                    extra={"agent": agent.name, "target_agent": handoff_target, "tool_round": rounds},
                )
            tool_log.append({"name": name, "args": args, "result": result})

            if authorized and name == "send_interactive_buttons" and context.channel == "whatsapp":
                interactive_payload = result
            if authorized and name == "send_catalog_pdf" and result.get("type") == "catalog_pdf":
                catalog_pdf_payload = result
            if authorized and name == "send_product_image" and result.get("type") == "product_image":
                product_image_payload = result

            tools_this_round = tool_schemas if rounds < agent.max_tool_rounds else None
            response = await provider.continue_after_tool(
                model=model,
                system_prompt=system_prompt,
                messages=messages,
                tool_call_id=tc_id,
                tool_name=name,
                tool_result=format_tool_result_for_model(name, result),
                tools=tools_this_round,
                temperature=_temperature(settings, agent),
                max_tokens=settings.get("llm_max_tokens", 500),
            )
            _add_usage(usage, response.usage)

        if interactive_payload:
            interactive_payload["body_text"] = clean_assistant_reply_text(interactive_payload.get("body_text", ""))
        if catalog_pdf_payload:
            catalog_pdf_payload["caption"] = clean_assistant_reply_text(catalog_pdf_payload.get("caption", ""))
        if product_image_payload:
            product_image_payload["caption"] = clean_assistant_reply_text(product_image_payload.get("caption", ""))

        reply_text = _resolve_reply_text(response.text, interactive_payload, product_image_payload)

        return AgentRunResult(
            text=reply_text,
            interactive=interactive_payload,
            catalog_pdf=catalog_pdf_payload,
            product_image=product_image_payload,
            tool_log=tool_log,
            provider=provider_name,
            model=model,
            usage=usage,
            was_fallback=was_fallback,
            requested_handoff=requested_handoff,
            handoff_target=handoff_target,
            escalated=escalated,
        )

    async def _initial_response(
        self,
        agent: AgentDefinition,
        system_prompt: str,
        messages: list[dict],
        settings: dict,
    ):
        provider_name = settings.get("llm_provider", "openai")
        model = settings.get("llm_model", "gpt-5.4-nano")
        available = self._list_providers()

        if provider_name not in available:
            if not available:
                raise RuntimeError("No LLM providers are configured.")
            old_name = provider_name
            provider_name = available[0]
            logger.warning(f"Provider '{old_name}' not available, falling back to '{provider_name}'")
            model = next(
                (m["id"] for m in AVAILABLE_MODELS.get(provider_name, []) if m.get("default")),
                "gpt-5.4-nano",
            )

        provider = self._get_provider(provider_name)
        tool_schemas = get_tool_schemas(agent.tool_names)

        try:
            response = await provider.chat(
                model=model,
                system_prompt=system_prompt,
                messages=messages,
                tools=tool_schemas,
                temperature=_temperature(settings, agent),
                max_tokens=settings.get("llm_max_tokens", 500),
            )
            return provider_name, model, provider, False, response
        except Exception as e:
            logger.error(f"Primary provider ({provider_name}) failed: {e}")
            if not settings.get("auto_fallback", True):
                raise

        fallback_name = settings.get("fallback_provider", "anthropic")
        fallback_model = settings.get("fallback_model", "claude-haiku-4-5")
        logger.info(f"Falling back to {fallback_name}/{fallback_model}")
        fallback_provider = self._get_provider(fallback_name)
        response = await fallback_provider.chat(
            model=fallback_model,
            system_prompt=system_prompt,
            messages=messages,
            tools=tool_schemas,
            temperature=_temperature(settings, agent),
            max_tokens=settings.get("llm_max_tokens", 500),
        )
        return fallback_name, fallback_model, fallback_provider, True, response


def format_tool_result_for_model(tool_name: str, result: dict) -> str:
    """Format an internal tool result so the model does not expose raw JSON."""
    return (
        "RESULTADO INTERNO DE HERRAMIENTA. NO lo muestres ni lo cites al cliente.\n"
        f"Herramienta: {tool_name}\n"
        "Usa estos datos solo para redactar una respuesta natural en español.\n"
        f"{json.dumps(result, ensure_ascii=False)}"
    )


def clean_assistant_reply_text(text: str, catalog: list[dict[str, Any]] | None = None) -> str:
    """Remove technical leaks and catalog SKUs from customer-facing replies."""
    cleaned = sanitize_customer_facing_text(text)
    cleaned = strip_catalog_skus_from_text(cleaned, catalog=catalog)
    return cleaned.strip()


def strip_catalog_skus_from_text(text: str, catalog: list[dict[str, Any]] | None = None) -> str:
    """Strip catalog SKUs and parent SKUs from customer-facing text."""
    cleaned = text or ""
    catalog = catalog if catalog is not None else get_cached_catalog() or []
    sku_values = {
        str(product.get("sku", "")).strip()
        for product in catalog
        if str(product.get("sku", "")).strip()
    }
    sku_values.update(
        str(product.get("parent_sku", "")).strip()
        for product in catalog
        if str(product.get("parent_sku", "")).strip()
    )

    for sku in sorted(sku_values, key=len, reverse=True):
        pattern = re.compile(rf"(?i)(?:\(?\b{re.escape(sku)}\b\)?)")
        cleaned = pattern.sub("", cleaned)

    cleaned = re.sub(r"\(\s*\)", "", cleaned)
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    cleaned = re.sub(r"\s+([,.;:!?])", r"\1", cleaned)
    return cleaned.strip()


def _temperature(settings: dict, agent: AgentDefinition) -> float:
    if settings.get("llm_temperature") is not None:
        return settings.get("llm_temperature", 0.7)
    if agent.default_temperature is not None:
        return agent.default_temperature
    return 0.7


def _resolve_reply_text(
    response_text: str | None,
    interactive_payload: dict | None,
    product_image_payload: dict | None,
) -> str:
    if not response_text and interactive_payload:
        reply_text = interactive_payload.get("body_text", "")
    elif not response_text and product_image_payload:
        reply_text = product_image_payload.get("caption") or "Aquí tienes la foto del producto."
    else:
        reply_text = response_text or DEFAULT_FALLBACK_TEXT
    return clean_assistant_reply_text(reply_text) or DEFAULT_FALLBACK_TEXT


def _empty_usage() -> dict[str, int]:
    return {"input_tokens": 0, "output_tokens": 0}


def _add_usage(target: dict[str, int], usage: dict | None) -> None:
    source = usage or {}
    target["input_tokens"] += int(source.get("input_tokens", 0) or 0)
    target["output_tokens"] += int(source.get("output_tokens", 0) or 0)
