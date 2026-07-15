"""
AI Engine: the central orchestrator.

Receives an incoming message + customer context, builds the prompt,
calls the active LLM provider, executes any tool calls, and returns
the final text response.
"""

import logging
import time

from app import analytics, db
from app.admin.notify import notify_escalation, notify_incoming_message
from app.ai.orchestrator import decide_orchestration_with_router, resolve_effective_orchestration_mode
from app.ai.payment.responder import render_payment_response
from app.ai.payment.verifier import verify_payment_proof
from app.ai.policies import guards
from app.ai.prompts import PromptContext, build_agent_prompt, format_catalog_as_markdown
from app.ai.runner import (
    AgentRunContext,
    AgentRunner,
    strip_catalog_skus_from_text,
)
from app.ai.runner import (
    clean_assistant_reply_text as _runner_clean_assistant_reply_text,
)
from app.ai.tools import catalog as catalog_tools
from app.ai.tools.context import ToolExecutionContext
from app.ai.tools.executor import execute_tool
from app.ai.vision import analyze_payment_screenshot
from app.catalog.sheets import get_cached_catalog
from app.config import get_config
from app.crm import conversations, customers, orders, sessions

logger = logging.getLogger(__name__)

# Maximum number of tool-call rounds per message (legacy compatibility constant)
MAX_TOOL_ROUNDS = 6


async def generate_response(
    channel: str,
    sender_id: str,
    message_text: str,
    media_url: str | None = None,
    customer_profile: dict | None = None,
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
        # Store the message so conversation history is preserved
        await conversations.store_message(
            customer_id=customer["id"],
            role="user",
            content=message_text,
            channel=channel,
            media_url=media_url,
        )
        # Only notify the owner on the first unanswered message.
        # If the last message was already from the user, the owner
        # was already notified — no need to spam.
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
        await notify_escalation(
            customer_name=customer.get("display_name"),
            customer_channel=channel,
            customer_platform_id=sender_id,
            reason=human_request_reason,
            urgency="medium",
            conversation_summary=summary,
        )
        handoff_text = "Claro, te paso con una persona del equipo para que te atienda directamente."
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
    history = await conversations.get_history(customer["id"], limit=max_history)
    original_message_text = message_text

    # Append the new message
    history.append({"role": "user", "content": message_text})

    open_order = await orders.get_latest_open_order(customer["id"])

    # ── 4. If image attached, analyze it first ───────────────
    vision_result = None
    payment_proof_attempt = False
    if media_url:
        vision_result = await analyze_payment_screenshot(
            media_id=media_url if channel == "whatsapp" else None,
            media_url=media_url if channel == "instagram" else None,
            channel=channel,
        )
        payment_proof_attempt = guards.looks_like_payment_proof_message(message_text, vision_result or {})
        if vision_result.get("analyzed") and not payment_proof_attempt:
            # Inject vision analysis into the message text so the AI has context
            summary = vision_result.get("summary", "Imagen analizada")
            message_text = f"{message_text}\n\n[Análisis de imagen: {summary}]"
            # Update the last message in history
            history[-1] = {"role": "user", "content": message_text}

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
        )

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

    # ── 5. Build selected agent prompt with live catalog ─────
    catalog = get_cached_catalog()
    catalog_md = format_catalog_as_markdown(catalog)
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
            workflow_state=session.workflow_context() if session and orchestration.agent.name != "legacy" else None,
        ),
    )

    # ── 6. Run the legacy agent with timing ──────────────────
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
    agent_result = await AgentRunner().run(
        agent=orchestration.agent,
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
        ),
    )
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
            extra={"from_agent": orchestration.agent.name, "to_agent": "checkout", "route_intent": orchestration.route_decision.intent},
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

    await conversations.store_message(
        customer_id=customer["id"],
        role="assistant",
        content=agent_result.text,
        channel=channel,
        function_calls=_safe_tool_log_for_persistence(agent_result.tool_log),
    )

    return {
        "text": agent_result.text,
        "interactive": agent_result.interactive,
        "catalog_pdf": agent_result.catalog_pdf,
        "product_image": agent_result.product_image,
        "customer_id": customer["id"],
        "escalated": agent_result.escalated,
    }


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
    await conversations.store_message(
        customer_id=customer["id"],
        role="assistant",
        content=reply_text,
        channel=channel,
        function_calls=[{"name": "verify_payment_proof", "status": result.status}],
    )
    return {
        "text": reply_text,
        "interactive": None,
        "catalog_pdf": None,
        "product_image": None,
        "customer_id": customer["id"],
        "escalated": False,
    }


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
) -> dict:
    """Compatibility wrapper around the extracted tool executor."""
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
        ),
    )


async def _tool_check_inventory(args: dict) -> dict:
    """Compatibility wrapper for the catalog inventory handler."""
    return await catalog_tools.check_inventory(args)


async def _tool_send_product_image(args: dict) -> dict:
    """Compatibility wrapper for the product-image handler."""
    return await catalog_tools.send_product_image(args)


def _find_catalog_matches(product_query: str, size_filter: str | None = None) -> list[dict]:
    return catalog_tools.find_catalog_matches(product_query, size_filter=size_filter)


def _normalize_catalog_text(text: str) -> str:
    return catalog_tools.normalize_catalog_text(text)


def _clean_assistant_reply_text(text: str) -> str:
    return _runner_clean_assistant_reply_text(text, catalog=get_cached_catalog() or [])


def _strip_catalog_skus_from_text(text: str) -> str:
    return strip_catalog_skus_from_text(text, catalog=get_cached_catalog() or [])


# Usage logging is now handled by app.analytics.log_response()
