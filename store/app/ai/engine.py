"""
AI Engine: the central orchestrator.

Receives an incoming message + customer context, builds the prompt,
calls the active LLM provider, executes any tool calls, and returns
the final text response.
"""

import logging
import re
import time
from dataclasses import replace

from app.ai import runner as agent_runner_module
from app import analytics, db
from app.admin.notify import notify_escalation, notify_incoming_message
from app.ai.agents.legacy import LEGACY_TOOL_NAMES
from app.ai.orchestrator import decide_orchestration_with_router, resolve_effective_orchestration_mode
from app.ai.payment.responder import render_payment_response
from app.ai.payment.verifier import verify_payment_proof
from app.ai.policies import guards
from app.ai.prompts import PromptContext, build_agent_prompt, format_catalog_as_markdown
from app.ai.runner import (
    AgentRunContext,
    AgentRunner,
    clean_assistant_reply_text as _runner_clean_assistant_reply_text,
    strip_catalog_skus_from_text,
)
from app.ai.tools import catalog as catalog_tools
from app.ai.tools.context import ToolExecutionContext
from app.ai.tools.executor import execute_tool
from app.ai.tools.registry import get_tool_schemas
from app.ai.vision import analyze_payment_screenshot
from app.catalog.pdf_generator import PDF_PATH, generate_catalog_pdf
from app.catalog.sheets import get_cached_catalog
from app.config import get_config
from app.crm import conversations, customers, orders, sessions

logger = logging.getLogger(__name__)

# Maximum number of tool-call rounds per message (legacy compatibility constant)
MAX_TOOL_ROUNDS = 6

_INITIAL_GREETING_RE = re.compile(
    r"^\s*(?:¡?\s*)?(?:hola|buenas|buenos dias|buenos días|buenas tardes|buenas noches)"
    r"(?:\s+(?:bella|mi amor|hermosa|linda|corazon|corazón|\w+))?\s*[!¡.,:;-]*\s*",
    re.IGNORECASE,
)


def _list_providers():
    return agent_runner_module._list_providers()


def get_provider(provider_name: str):
    return agent_runner_module.get_provider(provider_name)


async def generate_response(
    channel: str,
    sender_id: str,
    message_text: str,
    media_url: str | None = None,
    customer_profile: dict | None = None,
    customer_id: str | None = None,
    integration_context: dict | None = None,
    persist_assistant_message: bool = True,
) -> dict:
    """
    Full pipeline: message in -> AI response out.

    Returns
    -------
    dict with keys:
        "text": str             - The reply text to send back
        "interactive": dict|None - If the AI wants to send buttons (WhatsApp only)
        "product_image": dict|None - If the AI wants to send a product image
        "customer_id": str      - For reference
    """
    # ── 1. Load settings ─────────────────────────────────────
    settings = await db.get_settings()
    config = get_config()
    payment_methods = settings.get("payment_methods", [])
    orchestration_mode = resolve_effective_orchestration_mode(
        settings,
        getattr(config, "ai_orchestration_mode", "legacy"),
    )

    # ── 2. Get or create customer ────────────────────────────
    if customer_id:
        customer_row = await db.fetch_one("SELECT * FROM customers WHERE id = :id", {"id": customer_id})
        customer = dict(customer_row) if customer_row else None
    else:
        customer = None
    if not customer:
        customer = await customers.get_or_create_customer(
            channel=channel,
            platform_id=sender_id,
            display_name=(customer_profile or {}).get("display_name"),
            phone=(customer_profile or {}).get("phone"),
            instagram_handle=(customer_profile or {}).get("instagram_handle"),
        )

    # ── 2b. Check if AI is paused (globally or per-customer) ─
    ai_enabled = not guards.is_ai_paused(settings)
    is_escalated = guards.is_customer_escalated(customer)
    is_blocked = guards.is_customer_blocked(customer)

    if not ai_enabled or is_escalated or is_blocked:
        await conversations.store_message(
            customer_id=customer["id"],
            role="user",
            content=message_text,
            channel=channel,
            media_url=media_url,
        )
        # Only notify the owner on the first unanswered message.
        last_msg = await db.fetch_one(
            "SELECT role FROM conversations WHERE customer_id = :cid ORDER BY created_at DESC OFFSET 1 LIMIT 1",
            {"cid": customer["id"]},
        )
        already_notified = last_msg and last_msg["role"] == "user"

        if not already_notified and not is_blocked:
            await notify_incoming_message(
                customer_name=customer.get("display_name"),
                customer_channel=channel,
                customer_platform_id=sender_id,
                message_text=message_text,
            )
        suppressed_response = {
            "text": None,
            "interactive": None,
            "catalog_pdf": None,
            "product_image": None,
            "customer_id": customer["id"],
            "escalated": is_escalated,
            "paused": not ai_enabled,
        }
        if is_blocked:
            suppressed_response["blocked"] = True
        return suppressed_response

    # ── 2c. Auto-escalate abusive customers ─────────────────
    hostility_reason = _detect_hostile_customer_message(message_text)
    if hostility_reason:
        t_start = time.monotonic()
        await conversations.store_message(
            customer_id=customer["id"],
            role="user",
            content=message_text,
            channel=channel,
            media_url=media_url,
        )
        await customers.set_conversation_state(customer["id"], "escalated")
        summary = await conversations.get_recent_summary(customer["id"], limit=5)
        await _sync_kommo_escalation_if_needed(
            customer_id=customer["id"],
            reason=hostility_reason,
            urgency="high",
            conversation_summary=summary,
            lead_id=(integration_context or {}).get("lead_id"),
        )
        await notify_escalation(
            customer_name=customer.get("display_name"),
            customer_channel=channel,
            customer_platform_id=sender_id,
            reason=hostility_reason,
            urgency="high",
            conversation_summary=summary,
        )
        handoff_text = (
            "Voy a dejar esta conversación en manos de una persona del equipo "
            "para que te atienda directamente."
        )
        if persist_assistant_message:
            await conversations.store_message(
                customer_id=customer["id"],
                role="assistant",
                content=handoff_text,
                channel=channel,
            )
        response_time_ms = int((time.monotonic() - t_start) * 1000)
        logger.info(
            "AI route decision",
            extra={
                "orchestration_mode": orchestration_mode,
                "selected_agent": "support",
                "route_intent": "hostile_message",
                "route_source": "deterministic_guard",
                "route_confidence": 1.0,
            },
        )
        await analytics.log_ai_run(
            customer_id=customer["id"],
            channel=channel,
            orchestration_mode=orchestration_mode,
            selected_agent="support",
            route_intent="hostile_message",
            route_source="deterministic_guard",
            route_confidence=1.0,
            provider=None,
            model=None,
            usage={},
            response_time_ms=response_time_ms,
            tool_names=["escalate_to_human"],
            tool_rounds=0,
            handoff_occurred=True,
            fallback_occurred=False,
            escalation_occurred=True,
            shadow_evaluation=False,
            legacy_fallback=False,
        )
        return {
            "text": handoff_text,
            "interactive": None,
            "catalog_pdf": None,
            "product_image": None,
            "customer_id": customer["id"],
            "escalated": True,
            "paused": False,
        }

    # ── 2d. Explicit human requests bypass router/agents ─────
    human_request_reason = guards.detect_human_request(message_text)
    if human_request_reason:
        t_start = time.monotonic()
        await conversations.store_message(
            customer_id=customer["id"],
            role="user",
            content=message_text,
            channel=channel,
            media_url=media_url,
        )
        await customers.set_conversation_state(customer["id"], "escalated")
        summary = await conversations.get_recent_summary(customer["id"], limit=5)
        await _sync_kommo_escalation_if_needed(
            customer_id=customer["id"],
            reason=human_request_reason,
            urgency="medium",
            conversation_summary=summary,
            lead_id=(integration_context or {}).get("lead_id"),
        )
        await notify_escalation(
            customer_name=customer.get("display_name"),
            customer_channel=channel,
            customer_platform_id=sender_id,
            reason=human_request_reason,
            urgency="medium",
            conversation_summary=summary,
        )
        handoff_text = "Claro, te paso con una persona del equipo para que te atienda directamente."
        if persist_assistant_message:
            await conversations.store_message(
                customer_id=customer["id"],
                role="assistant",
                content=handoff_text,
                channel=channel,
            )
        response_time_ms = int((time.monotonic() - t_start) * 1000)
        await analytics.log_ai_run(
            customer_id=customer["id"],
            channel=channel,
            orchestration_mode=orchestration_mode,
            selected_agent="support",
            route_intent="human_request",
            route_source="deterministic_guard",
            route_confidence=0.95,
            provider=None,
            model=None,
            usage={},
            response_time_ms=response_time_ms,
            tool_names=["escalate_to_human"],
            tool_rounds=0,
            handoff_occurred=True,
            fallback_occurred=False,
            escalation_occurred=True,
            shadow_evaluation=False,
            legacy_fallback=False,
        )
        return {
            "text": handoff_text,
            "interactive": None,
            "catalog_pdf": None,
            "product_image": None,
            "customer_id": customer["id"],
            "escalated": True,
            "paused": False,
        }

    # ── 3. Load conversation history ─────────────────────────
    max_history = settings.get("max_conversation_history", 20)
    stored_history = await conversations.get_history(customer["id"], limit=max_history)
    has_previous_context = bool(stored_history)
    history = conversations.prepare_history_for_generation(stored_history, latest_user_message=message_text)
    original_message_text = message_text

    open_order = await orders.get_latest_open_order(customer["id"])

    # ── 4. If image attached, analyze it first ───────────────
    vision_result = None
    payment_proof_attempt = False
    if media_url:
        direct_media_url = bool((integration_context or {}).get("media_url_is_direct"))
        vision_channel = "instagram" if direct_media_url else channel
        vision_result = await analyze_payment_screenshot(
            media_id=media_url if vision_channel == "whatsapp" else None,
            media_url=media_url if vision_channel != "whatsapp" else None,
            channel=vision_channel,
        )
        payment_proof_attempt = guards.looks_like_payment_proof_message(message_text, vision_result or {})
        if (vision_result or {}).get("analyzed") and not payment_proof_attempt:
            summary = vision_result.get("summary", "Imagen analizada")
            message_text = f"{message_text}\n\n[Análisis de imagen: {summary}]"
            history = conversations.prepare_history_for_generation(stored_history, latest_user_message=message_text)

    if payment_proof_attempt:
        logger.info(
            "AI route decision",
            extra={
                "orchestration_mode": orchestration_mode,
                "selected_agent": "payment",
                "route_intent": "payment_proof",
                "route_source": "deterministic_guard",
                "route_confidence": 0.95,
            },
        )
        return await _handle_payment_proof_attempt(
            customer=customer,
            channel=channel,
            media_url=media_url,
            message_text=original_message_text,
            payment_methods=payment_methods or [],
            vision_result=vision_result or {},
            orchestration_mode=orchestration_mode,
            persist_assistant_message=persist_assistant_message,
        )

    # ── 5. Resolve orchestration route and build prompt ──────
    session = None
    if orchestration_mode == "multi_agent":
        session = await sessions.get_or_create_session(customer["id"])
    elif orchestration_mode == "shadow":
        session = await sessions.get_session(customer["id"])
    orchestration = await decide_orchestration_with_router(
        mode=orchestration_mode,
        message_text=message_text,
        settings=settings,
        history=history,
        payment_proof_attempt=payment_proof_attempt,
        vision_result=vision_result,
        session_state=session,
    )
    if orchestration.mode == "multi_agent" and orchestration.agent.name != "legacy":
        session = await _record_active_route(customer["id"], orchestration)
    _log_route_decision(orchestration)

    catalog = get_cached_catalog()
    catalog_md = format_catalog_as_markdown(catalog)
    catalog_pdf_supported = _catalog_pdf_supported(channel, integration_context, config)
    system_prompt = build_agent_prompt(
        orchestration.agent.prompt_name,
        PromptContext(
            catalog_markdown=catalog_md,
            store_name=settings.get("store_name", config.store_name),
            channel=channel,
            customer=customer,
            open_order=open_order,
            payment_methods=payment_methods,
            accepted_exchange_rate=str(settings.get("accepted_exchange_rate", "") or ""),
            order_discount_percent=settings.get("order_discount_percent"),
            order_discount_threshold_usd=settings.get("order_discount_threshold_usd"),
            catalog_pdf_supported=catalog_pdf_supported,
            workflow_state=session.workflow_context() if session and orchestration.agent.name != "legacy" else None,
        ),
    )
    system_prompt = _with_conversation_continuity_guidance(system_prompt, has_previous_context)

    # ── 6. Run the selected agent with timing ────────────────
    t_start = time.monotonic()
    logger.info(
        "AI agent execution started",
        extra={
            "orchestration_mode": orchestration.mode,
            "selected_agent": orchestration.agent.name,
            "route_intent": orchestration.route_decision.intent,
            "route_source": orchestration.route_decision.source,
        },
    )
    agent_for_delivery = _agent_with_delivery_tools(orchestration.agent, channel, integration_context, config)
    agent_result = await AgentRunner(provider_getter=get_provider, provider_lister=_list_providers).run(
        agent=agent_for_delivery,
        system_prompt=system_prompt,
        messages=history,
        settings=settings,
        context=AgentRunContext(
            customer=customer,
            channel=channel,
            payment_methods=payment_methods or [],
            vision_result=vision_result,
            payment_proof_attempt=payment_proof_attempt,
            latest_user_message=message_text,
            session=session,
            integration_context=integration_context,
        ),
    )
    _apply_conversation_continuity_to_agent_result(agent_result, has_previous_context)
    t_end = time.monotonic()
    response_time_ms = int((t_end - t_start) * 1000)

    await analytics.log_response(
        provider=agent_result.provider,
        model=agent_result.model,
        usage=agent_result.usage,
        customer_id=customer["id"],
        response_time_ms=response_time_ms,
        was_fallback=agent_result.was_fallback,
        had_tool_calls=bool(agent_result.tool_log),
        channel=channel,
    )

    if orchestration.mode == "multi_agent" and agent_result.handoff_target == "checkout":
        await sessions.set_active_agent(
            customer["id"],
            "checkout",
            active_intent="checkout_or_order",
            workflow_stage="checkout_collecting",
            last_route_confidence=orchestration.route_decision.confidence,
        )
        logger.info(
            "AI handoff accepted",
            extra={
                "from_agent": orchestration.agent.name,
                "to_agent": "checkout",
                "route_intent": orchestration.route_decision.intent,
            },
        )

    await analytics.log_ai_run(
        customer_id=customer["id"],
        channel=channel,
        orchestration_mode=orchestration.mode,
        selected_agent=orchestration.agent.name,
        route_intent=orchestration.route_decision.intent,
        route_source=orchestration.route_decision.source,
        route_confidence=orchestration.route_decision.confidence,
        provider=agent_result.provider,
        model=agent_result.model,
        usage=agent_result.usage,
        response_time_ms=response_time_ms,
        tool_names=[entry["name"] for entry in agent_result.tool_log],
        tool_rounds=len(agent_result.tool_log),
        handoff_occurred=agent_result.requested_handoff,
        fallback_occurred=agent_result.was_fallback,
        escalation_occurred=agent_result.escalated,
        shadow_evaluation=orchestration.mode == "shadow",
        legacy_fallback=orchestration.fallback_to_legacy,
    )

    logger.info(
        f"Response generated in {response_time_ms}ms "
        f"({agent_result.provider}/{agent_result.model}, "
        f"{agent_result.usage.get('input_tokens', 0)}+{agent_result.usage.get('output_tokens', 0)} tokens"
        f"{', FALLBACK' if agent_result.was_fallback else ''})"
    )

    # ── 7. Store messages ────────────────────────────────────
    await conversations.store_message(
        customer_id=customer["id"],
        role="user",
        content=message_text,
        channel=channel,
        media_url=media_url,
    )

    safe_tool_log = _safe_tool_log_for_persistence(agent_result.tool_log)
    if persist_assistant_message:
        await conversations.store_message(
            customer_id=customer["id"],
            role="assistant",
            content=agent_result.text,
            channel=channel,
            function_calls=safe_tool_log,
        )

    response = {
        "text": agent_result.text,
        "interactive": agent_result.interactive,
        "catalog_pdf": agent_result.catalog_pdf,
        "product_image": agent_result.product_image,
        "customer_id": customer["id"],
        "escalated": agent_result.escalated,
    }
    if not persist_assistant_message and safe_tool_log:
        response["function_calls"] = safe_tool_log
    return response


def _detect_hostile_customer_message(message_text: str) -> str | None:
    """
    Detect clear insults, accusations, or threats from the customer.
    High-confidence matches are escalated immediately to a human.
    """
    return guards.detect_hostile_customer_message(message_text)


async def _handle_payment_proof_attempt(
    *,
    customer: dict,
    channel: str,
    media_url: str | None,
    message_text: str,
    payment_methods: list[dict],
    vision_result: dict,
    orchestration_mode: str,
    persist_assistant_message: bool = True,
) -> dict:
    """Validate payment screenshots deterministically before any LLM sees them."""
    t_start = time.monotonic()
    await conversations.store_message(
        customer_id=customer["id"],
        role="user",
        content=message_text,
        channel=channel,
        media_url=media_url,
    )
    result = await verify_payment_proof(
        customer_id=customer["id"],
        payment_methods=payment_methods,
        vision_result=vision_result,
        confirmation_note=vision_result.get("summary") or "Comprobante recibido por imagen",
        update_order=True,
    )
    reply_text = render_payment_response(result, customer=customer)
    response_time_ms = int((time.monotonic() - t_start) * 1000)
    await analytics.log_ai_run(
        customer_id=customer["id"],
        channel=channel,
        orchestration_mode=orchestration_mode,
        selected_agent="payment",
        route_intent="payment_proof",
        route_source="deterministic_guard",
        route_confidence=0.95,
        provider=None,
        model=None,
        usage={},
        response_time_ms=response_time_ms,
        tool_names=["verify_payment_proof"],
        tool_rounds=1,
        handoff_occurred=False,
        fallback_occurred=False,
        escalation_occurred=False,
        shadow_evaluation=False,
        legacy_fallback=False,
    )
    function_calls = [{"name": "verify_payment_proof", "status": result.status}]
    if persist_assistant_message:
        await conversations.store_message(
            customer_id=customer["id"],
            role="assistant",
            content=reply_text,
            channel=channel,
            function_calls=function_calls,
        )
    response = {
        "text": reply_text,
        "interactive": None,
        "catalog_pdf": None,
        "product_image": None,
        "customer_id": customer["id"],
        "escalated": False,
    }
    if not persist_assistant_message:
        response["function_calls"] = function_calls
    return response


def _with_conversation_continuity_guidance(system_prompt: str, has_previous_context: bool) -> str:
    if has_previous_context:
        guidance = (
            "Esta conversacion ya tiene contexto previo almacenado. "
            "No abras con un saludo inicial y responde solo el ultimo mensaje del cliente, "
            "sin volver a contestar preguntas anteriores ya atendidas."
        )
    else:
        guidance = "No hay contexto previo almacenado. Puedes saludar brevemente al iniciar la conversacion."
    return f"{system_prompt}\n\n# Continuidad de conversacion\n\n{guidance}"


def _catalog_pdf_supported(channel: str, integration_context: dict | None, config=None) -> bool:
    delivery_provider = (integration_context or {}).get("provider")
    if not delivery_provider:
        delivery_provider = getattr(config or get_config(), "channel_backend", "meta")
    if not isinstance(delivery_provider, str) or delivery_provider not in {"meta", "kommo"}:
        delivery_provider = "meta"
    return channel == "whatsapp" and delivery_provider == "meta"


def _tools_for_delivery(channel: str, integration_context: dict | None, config=None) -> list[dict]:
    tool_names = _tool_names_for_delivery(LEGACY_TOOL_NAMES, channel, integration_context, config)
    return get_tool_schemas(tool_names)


def _agent_with_delivery_tools(agent, channel: str, integration_context: dict | None, config=None):
    tool_names = _tool_names_for_delivery(agent.tool_names, channel, integration_context, config)
    if tool_names == agent.tool_names:
        return agent
    return replace(agent, tool_names=tool_names)


def _tool_names_for_delivery(
    tool_names: tuple[str, ...],
    channel: str,
    integration_context: dict | None,
    config=None,
) -> tuple[str, ...]:
    if _catalog_pdf_supported(channel, integration_context, config):
        return tuple(tool_names)
    return tuple(name for name in tool_names if name != "send_catalog_pdf")


def _apply_conversation_continuity_to_agent_result(agent_result, has_previous_context: bool) -> None:
    if not has_previous_context:
        return
    agent_result.text = _strip_initial_greeting_if_needed(agent_result.text, has_previous_context) or agent_result.text
    if agent_result.interactive:
        agent_result.interactive["body_text"] = _strip_initial_greeting_if_needed(
            agent_result.interactive.get("body_text", ""),
            has_previous_context,
        )
    if agent_result.catalog_pdf:
        agent_result.catalog_pdf["caption"] = _strip_initial_greeting_if_needed(
            agent_result.catalog_pdf.get("caption", ""),
            has_previous_context,
        )
    if agent_result.product_image:
        agent_result.product_image["caption"] = _strip_initial_greeting_if_needed(
            agent_result.product_image.get("caption", ""),
            has_previous_context,
        )


def _strip_initial_greeting_if_needed(text: str | None, has_previous_context: bool) -> str:
    if not has_previous_context or not text:
        return (text or "").strip()
    stripped = _INITIAL_GREETING_RE.sub("", text, count=1).lstrip()
    return stripped or text.strip()


def _log_route_decision(orchestration) -> None:
    decision = orchestration.route_decision
    logger.info(
        "AI route decision",
        extra={
            "orchestration_mode": orchestration.mode,
            "selected_agent": orchestration.agent.name,
            "route_agent": orchestration.route_agent.name,
            "route_intent": decision.intent,
            "route_source": decision.source,
            "route_confidence": decision.confidence,
            "legacy_fallback": orchestration.fallback_to_legacy,
            "shadow_evaluation": orchestration.mode == "shadow",
        },
    )
    if orchestration.mode == "shadow":
        logger.info(
            "AI shadow comparison",
            extra={
                "served_agent": orchestration.agent.name,
                "shadow_agent": orchestration.route_agent.name,
                "route_intent": decision.intent,
                "route_source": decision.source,
            },
        )
    if orchestration.fallback_to_legacy:
        logger.info(
            "AI legacy fallback",
            extra={"requested_route": decision.route, "selected_agent": orchestration.agent.name},
        )


async def _record_active_route(customer_id: str, orchestration):
    """Persist the executable specialist route for multi-agent mode."""
    return await sessions.set_active_agent(
        customer_id,
        orchestration.agent.name,
        active_intent=orchestration.route_decision.intent,
        workflow_stage=_workflow_stage_for_route(orchestration.agent.name, orchestration.route_decision.intent),
        last_route_confidence=orchestration.route_decision.confidence,
    )


def _workflow_stage_for_route(agent_name: str, intent: str) -> str:
    if agent_name == "checkout":
        if intent == "cancel_checkout":
            return "checkout_collecting"
        return "checkout_collecting"
    if agent_name == "sales":
        return "sales"
    if agent_name == "support":
        return "support"
    return "idle"


def _safe_tool_log_for_persistence(tool_log: list[dict] | None) -> list[dict] | None:
    """Persist tool metadata without model-supplied args or raw tool results."""
    safe_entries = []
    for entry in tool_log or []:
        result = entry.get("result") if isinstance(entry.get("result"), dict) else {}
        safe_entry = {"name": entry.get("name")}
        status = result.get("status") or result.get("type")
        if status:
            safe_entry["status"] = status
        if result.get("duplicate_ignored"):
            safe_entry["duplicate_ignored"] = True
        safe_entries.append(safe_entry)
    return safe_entries or None


# ── Tool execution compatibility wrappers ─────────────────────

async def _execute_tool(
    name: str,
    args: dict,
    customer: dict,
    channel: str,
    payment_methods: list[dict] | None = None,
    vision_result: dict | None = None,
    payment_proof_attempt: bool = False,
    latest_user_message: str = "",
    integration_context: dict | None = None,
) -> dict:
    """Compatibility wrapper around the extracted tool executor."""
    if name == "send_catalog_pdf":
        return await _tool_send_catalog_pdf(args, channel, integration_context)
    return await execute_tool(
        name=name,
        arguments=args,
        context=ToolExecutionContext(
            customer=customer,
            channel=channel,
            payment_methods=payment_methods or [],
            vision_result=vision_result,
            payment_proof_attempt=payment_proof_attempt,
            latest_user_message=latest_user_message,
            integration_context=integration_context,
        ),
    )


async def _tool_check_inventory(args: dict) -> dict:
    """Compatibility wrapper for the catalog inventory handler."""
    return await catalog_tools.check_inventory(args)


async def _tool_send_product_image(args: dict) -> dict:
    """Compatibility wrapper for the product-image handler."""
    return await catalog_tools.send_product_image(args)


async def _tool_send_catalog_pdf(args: dict, channel: str, integration_context: dict | None = None) -> dict:
    """Compatibility wrapper for catalog PDF delivery checks."""
    if not _catalog_pdf_supported(channel, integration_context):
        return {
            "status": "error",
            "message": "Catalog PDF delivery is unavailable here. Describe catalog categories in text instead.",
        }
    if not PDF_PATH.exists():
        catalog = get_cached_catalog()
        if not catalog:
            return {"status": "error", "message": "Catalog is empty, cannot generate PDF."}
        try:
            generate_catalog_pdf(catalog)
        except Exception as e:
            logger.error(f"Auto-generate catalog PDF failed: {e}")
            return {"status": "error", "message": "Could not generate catalog PDF."}
    return {
        "type": "catalog_pdf",
        "caption": args.get("caption", "Aqui tienes nuestro catalogo de productos 📖"),
    }


def _find_catalog_matches(product_query: str, size_filter: str | None = None) -> list[dict]:
    return catalog_tools.find_catalog_matches(product_query, size_filter=size_filter)


def _normalize_catalog_text(text: str) -> str:
    return catalog_tools.normalize_catalog_text(text)


def _clean_assistant_reply_text(text: str) -> str:
    return _runner_clean_assistant_reply_text(text, catalog=get_cached_catalog() or [])


def _strip_catalog_skus_from_text(text: str) -> str:
    return strip_catalog_skus_from_text(text, catalog=get_cached_catalog() or [])


async def _sync_kommo_escalation_if_needed(
    *,
    customer_id: str,
    reason: str,
    urgency: str,
    conversation_summary: str,
    lead_id: str | None = None,
) -> None:
    if getattr(get_config(), "channel_backend", "meta") != "kommo":
        return
    from app.integrations.kommo.state import sync_escalation_to_kommo

    await sync_escalation_to_kommo(
        customer_id=customer_id,
        reason=reason,
        urgency=urgency,
        conversation_summary=conversation_summary,
        lead_id=lead_id,
    )


# Usage logging is now handled by app.analytics.log_response()
