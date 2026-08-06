"""
AI Engine: the central orchestrator.

Receives an incoming message + customer context, builds the prompt,
calls the active LLM provider, executes any tool calls, and returns
the final text response.
"""

import json
import logging
import re
import time
from dataclasses import replace

from app import analytics, db
from app.admin.notify import notify_escalation, notify_incoming_message
from app.ai import runner as agent_runner_module
from app.ai.agents.legacy import LEGACY_TOOL_NAMES
from app.ai.orchestrator import decide_orchestration_with_router, resolve_effective_orchestration_mode
from app.ai.payment.responder import render_payment_response
from app.ai.payment.verifier import verify_payment_proof
from app.ai.policies import guards
from app.ai.policies.channel_capabilities import (
    can_process_payment_proof,
    filter_tool_names,
    is_agent_route_allowed,
)
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
from app.ai.tools.registry import get_tool_schemas
from app.ai.vision import analyze_payment_screenshot
from app.catalog.pdf_generator import ensure_catalog_pdf
from app.catalog.sheets import (
    ensure_fresh_catalog,
    get_cached_catalog,
    get_cached_reference_catalog,
    group_catalog_products,
)
from app.config import get_config
from app.crm import conversations, customers, escalations, orders, sessions
from app.exchange_rates import build_customer_exchange_rate_reply

logger = logging.getLogger(__name__)

# Maximum number of tool-call rounds per message (legacy compatibility constant)
MAX_TOOL_ROUNDS = 6

_INITIAL_GREETING_RE = re.compile(
    r"^\s*(?:¡?\s*)?(?:hola|buenas|buenos dias|buenos días|buenas tardes|buenas noches)"
    r"(?:\s+(?:bella|mi amor|hermosa|linda|corazon|corazón|\w+))?\s*[!¡.,:;-]*\s*",
    re.IGNORECASE,
)
_PUBLIC_COMMENT_PRIVATE_DETAIL_RE = re.compile(
    r"\b(talla|tallas|medida|medidas|size|envio|envios|delivery|shipping|domicilio|"
    r"pago|pagos|pagar|transferencia|zelle|binance|zinli|compr\w*|pedido|orden|"
    r"apart\w*|reserv\w*|recomienda|recomiendas|recomendacion|recomendaciones|asesor|humano|dm|"
    r"whatsapp|wsp|telefono|direccion|ubicacion)\b"
)
_PUBLIC_COMMENT_PRICE_RE = re.compile(r"\b(precio|precios|costo|costos|cuesta|vale|valor|sale|cuanto|cuanta|cuantos|cuantas)\b")
_PUBLIC_COMMENT_STOCK_RE = re.compile(r"\b(disponible|disponibles|disponibilidad|stock|hay|tienen|queda|quedan|agotado|agotada)\b")
_PUBLIC_COMMENT_CONTEXT_KEYS = {
    "product_sku",
    "product_skus",
    "parent_sku",
    "sku",
    "product_name",
    "post_product_name",
    "post_caption",
    "post_text",
    "caption",
    "media_caption",
    "image_url",
    "post_image_url",
    "post_media_url",
    "media_url",
    "permalink",
    "post_id",
    "comment_id",
    "parent_comment_id",
    "media_id",
    "post_url",
    "comment_url",
    "media_type",
    "media_product_type",
    "content_type",
    "mapping_status",
}
_PUBLIC_COMMENT_CLARIFICATION = (
    "¿Cuál producto de la publicación te interesa? "
    "Dinos el nombre o escríbenos al DM y te ayudamos 😊"
)
_PUBLIC_COMMENT_STOPWORDS = {
    "con",
    "del",
    "para",
    "por",
    "una",
    "uno",
    "las",
    "los",
    "que",
    "new",
    "nueva",
    "nuevo",
    "disponible",
    "disponibles",
}
_PUBLIC_COMMENT_REFERENCE_STOPWORDS = _PUBLIC_COMMENT_STOPWORDS | {
    "cuanto",
    "cuanta",
    "cuantos",
    "cuantas",
    "cual",
    "cuales",
    "precio",
    "precios",
    "costo",
    "costos",
    "cuesta",
    "vale",
    "valor",
    "sale",
    "esta",
    "estan",
    "hay",
    "tienen",
    "queda",
    "quedan",
    "agotado",
    "agotada",
    "que",
    "tiene",
    "el",
    "ella",
    "la",
    "las",
    "lo",
    "los",
    "de",
    "es",
    "bonita",
    "bonito",
}


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
    persist_user_before_response: bool = False,
    message_source_id: str | None = None,
) -> dict:
    """
    Full pipeline: message in -> AI response out.

    Returns
    -------
    dict with keys:
        "text": str             - The reply text to send back
        "interactive": dict|None - If the AI wants to send buttons (WhatsApp only)
        "product_image": dict|None - If the AI wants to send a product image
        "whatsapp_handoff": dict|None - Trusted Instagram-to-WhatsApp handoff payload
        "customer_id": str      - For reference
    """
    # ── 1. Load settings ─────────────────────────────────────
    settings = await db.get_settings()
    config = get_config()
    payment_methods = settings.get("payment_methods", [])
    is_public_comment = _is_public_instagram_comment(integration_context)
    interaction_type = (
        conversations.INSTAGRAM_COMMENT_SCOPE
        if is_public_comment
        else conversations.PRIVATE_MESSAGE_SCOPE
    )
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

    if is_escalated and not is_blocked:
        expiry_result = await escalations.reactivate_if_expired(customer)
        if expiry_result.status == "reactivated":
            customer = expiry_result.customer or {**customer, "conversation_state": "active"}
            is_escalated = False

    if not ai_enabled or is_escalated or is_blocked:
        await conversations.store_message(
            customer_id=customer["id"],
            role="user",
            content=message_text,
            channel=channel,
            media_url=media_url,
            source_id=message_source_id,
            interaction_type=interaction_type,
        )
        # Only notify the owner on the first unanswered message.
        last_msg = await db.fetch_one(
            """
            SELECT role FROM conversations
            WHERE customer_id = :cid AND interaction_type = :interaction_type
            ORDER BY created_at DESC OFFSET 1 LIMIT 1
            """,
            {"cid": customer["id"], "interaction_type": interaction_type},
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
            "whatsapp_handoff": None,
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
        if is_public_comment:
            return await _handle_public_comment_private_invite(
                customer=customer,
                channel=channel,
                media_url=media_url,
                message_text=message_text,
                settings=settings,
                orchestration_mode=orchestration_mode,
                route_intent="hostile_message",
                persist_assistant_message=persist_assistant_message,
                message_source_id=message_source_id,
            )
        t_start = time.monotonic()
        await conversations.store_message(
            customer_id=customer["id"],
            role="user",
            content=message_text,
            channel=channel,
            media_url=media_url,
            source_id=message_source_id,
            interaction_type=interaction_type,
        )
        await escalations.escalate_customer_automatically(customer["id"], settings=settings)
        summary = await conversations.get_recent_summary(
            customer["id"],
            limit=5,
            interaction_type=interaction_type,
        )
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
                    source_id=message_source_id,
                    interaction_type=interaction_type,
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
            "whatsapp_handoff": None,
            "customer_id": customer["id"],
            "escalated": True,
            "paused": False,
        }

    # ── 2d. Explicit human requests bypass router/agents ─────
    human_request_reason = guards.detect_human_request(message_text)
    if human_request_reason:
        if is_public_comment:
            return await _handle_public_comment_private_invite(
                customer=customer,
                channel=channel,
                media_url=media_url,
                message_text=message_text,
                settings=settings,
                orchestration_mode=orchestration_mode,
                route_intent="human_request",
                persist_assistant_message=persist_assistant_message,
                message_source_id=message_source_id,
            )
        t_start = time.monotonic()
        await conversations.store_message(
            customer_id=customer["id"],
            role="user",
            content=message_text,
            channel=channel,
            media_url=media_url,
            source_id=message_source_id,
            interaction_type=interaction_type,
        )
        await escalations.escalate_customer_automatically(customer["id"], settings=settings)
        summary = await conversations.get_recent_summary(
            customer["id"],
            limit=5,
            interaction_type=interaction_type,
        )
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
                    source_id=message_source_id,
                    interaction_type=interaction_type,
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
            "whatsapp_handoff": None,
            "customer_id": customer["id"],
            "escalated": True,
            "paused": False,
        }

    if is_public_comment:
        return await _handle_public_instagram_comment(
            customer=customer,
            channel=channel,
            media_url=media_url,
            message_text=message_text,
            settings=settings,
            integration_context=integration_context,
            orchestration_mode=orchestration_mode,
            persist_assistant_message=persist_assistant_message,
            message_source_id=message_source_id,
        )

    # ── 3. Load conversation history ─────────────────────────
    original_message_text = message_text
    if persist_user_before_response:
        await conversations.store_message(
            customer_id=customer["id"],
            role="user",
            content=original_message_text,
            channel=channel,
            media_url=media_url,
            source_id=message_source_id,
            interaction_type=interaction_type,
        )
    max_history = settings.get("max_conversation_history", 20)
    stored_history = await conversations.get_history(
        customer["id"],
        limit=max_history,
        interaction_type=interaction_type,
    )
    has_previous_context = bool(stored_history)
    history = conversations.prepare_history_for_generation(stored_history, latest_user_message=message_text)

    open_order = None if is_public_comment else await orders.get_latest_open_order(customer["id"])

    # ── 4. If image attached, analyze it first ───────────────
    vision_result = None
    payment_proof_attempt = False
    if media_url and not is_public_comment:
        if can_process_payment_proof(channel):
            direct_media_url = bool((integration_context or {}).get("media_url_is_direct"))
            vision_channel = "instagram" if direct_media_url else channel
            vision_result = await analyze_payment_screenshot(
                media_id=media_url if vision_channel == "whatsapp" else None,
                media_url=media_url if vision_channel != "whatsapp" else None,
                channel=vision_channel,
            )
            payment_proof_attempt = guards.looks_like_payment_proof_message(message_text, vision_result or {})
            if not payment_proof_attempt and _is_kommo_payment_screenshot(
                integration_context,
                open_order,
            ):
                # Kommo sends image-only messages with a generic placeholder. Once an
                # order is awaiting payment, route that screenshot to verification
                # even when vision cannot extract fields from it.
                payment_proof_attempt = True
            if (vision_result or {}).get("analyzed") and not payment_proof_attempt:
                summary = vision_result.get("summary", "Imagen analizada")
                message_text = f"{message_text}\n\n[Análisis de imagen: {summary}]"
                history = conversations.prepare_history_for_generation(stored_history, latest_user_message=message_text)
        else:
            payment_proof_attempt = guards.looks_like_payment_proof_message(message_text, {})

    if payment_proof_attempt and can_process_payment_proof(channel):
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
            persist_user_message=not persist_user_before_response,
            message_source_id=message_source_id,
        )

    if _detect_exchange_rate_question(message_text, stored_history) and _is_exchange_rate_only_message(
        message_text
    ):
        logger.info(
            "AI route decision",
            extra={
                "orchestration_mode": orchestration_mode,
                "selected_agent": "direct",
                "route_intent": "exchange_rate",
                "route_source": "deterministic_guard",
                "route_confidence": 1.0,
            },
        )
        return await _handle_exchange_rate_question(
            customer=customer,
            channel=channel,
            media_url=media_url,
            message_text=original_message_text,
            settings=settings,
            orchestration_mode=orchestration_mode,
            persist_assistant_message=persist_assistant_message,
            persist_user_message=not persist_user_before_response,
            message_source_id=message_source_id,
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
        channel=channel,
        settings=settings,
        history=history,
        payment_proof_attempt=payment_proof_attempt,
        vision_result=vision_result,
        session_state=session,
    )
    if orchestration.mode == "multi_agent" and orchestration.agent.name != "legacy":
        session = await _record_active_route(customer["id"], orchestration)
    _log_route_decision(orchestration)

    try:
        catalog = await ensure_fresh_catalog()
    except Exception:
        logger.exception("Catalog is stale and could not be refreshed before prompt generation")
        catalog = []
    instagram_content_context = _resolve_private_instagram_content_context(
        channel=channel,
        integration_context=integration_context,
        message_text=original_message_text,
        products=group_catalog_products(get_cached_reference_catalog()),
    )
    clear_story_context = bool(instagram_content_context.pop("_clear_story_context", False))
    selected_story_sku = instagram_content_context.get("selected_product_sku")
    incoming_story_context = (integration_context or {}).get("incoming_instagram_context") or {}
    if clear_story_context:
        await sessions.clear_instagram_content_context(str(customer["id"]))
        instagram_content_context = {}
    elif (
        selected_story_sku
        and selected_story_sku != incoming_story_context.get("selected_product_sku")
    ):
        persisted_context = await sessions.update_instagram_selected_product(
            str(customer["id"]),
            selected_story_sku,
        )
        if not persisted_context:
            instagram_content_context["selected_product_sku"] = None
    catalog_md = format_catalog_as_markdown(catalog)
    catalog_pdf_supported = _catalog_pdf_supported(channel, integration_context, config)
    system_prompt = build_agent_prompt(
        orchestration.agent.prompt_name,
        PromptContext(
            catalog_markdown=catalog_md,
            store_name=settings.get("store_name", config.store_name),
            channel=channel,
            customer=None if is_public_comment else customer,
            open_order=open_order,
            payment_methods=[] if is_public_comment else payment_methods,
            exchange_rate_settings=settings,
            order_discount_percent=settings.get("order_discount_percent"),
            order_discount_threshold_usd=settings.get("order_discount_threshold_usd"),
            catalog_pdf_supported=catalog_pdf_supported,
            workflow_state=session.workflow_context() if session and orchestration.agent.name != "legacy" else None,
            instagram_content_context=instagram_content_context,
        ),
    )
    system_prompt = _with_conversation_continuity_guidance(system_prompt, has_previous_context)
    if is_public_comment:
        system_prompt = _with_public_comment_guidance(system_prompt)

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
    required_whatsapp_handoff_reason = _required_whatsapp_handoff_reason(orchestration.route_decision.intent)
    agent_result = await AgentRunner(provider_getter=get_provider, provider_lister=_list_providers).run(
        agent=agent_for_delivery,
        system_prompt=system_prompt,
        messages=history,
        settings=settings,
        context=AgentRunContext(
            customer=customer,
            channel=channel,
            payment_methods=[] if is_public_comment else payment_methods or [],
            vision_result=vision_result,
            payment_proof_attempt=payment_proof_attempt,
            latest_user_message=message_text,
            store_phone_number=str(settings.get("store_phone_number") or ""),
            required_whatsapp_handoff_reason=required_whatsapp_handoff_reason,
            session=session,
            integration_context=integration_context,
        ),
    )
    _apply_conversation_continuity_to_agent_result(agent_result, has_previous_context)
    if is_public_comment:
        agent_result.text = _sanitize_public_comment_reply(agent_result.text)
        agent_result.interactive = None
        agent_result.catalog_pdf = None
        agent_result.product_image = None
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

    if (
        orchestration.mode == "multi_agent"
        and agent_result.handoff_target == "checkout"
        and is_agent_route_allowed(channel, "checkout")
    ):
        await sessions.set_active_agent(
            str(customer["id"]),
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
    if not persist_user_before_response:
        await conversations.store_message(
            customer_id=customer["id"],
            role="user",
            content=message_text,
            channel=channel,
            media_url=media_url,
            source_id=message_source_id,
            interaction_type=interaction_type,
        )

    safe_tool_log = _safe_tool_log_for_persistence(agent_result.tool_log)
    if persist_assistant_message:
        await conversations.store_message(
            customer_id=customer["id"],
            role="assistant",
            content=agent_result.text,
            channel=channel,
            function_calls=safe_tool_log,
            source_id=message_source_id,
            interaction_type=interaction_type,
        )

    response = {
        "text": agent_result.text,
        "interactive": agent_result.interactive,
        "catalog_pdf": agent_result.catalog_pdf,
        "product_image": agent_result.product_image,
        "whatsapp_handoff": agent_result.whatsapp_handoff,
        "customer_id": customer["id"],
        "escalated": agent_result.escalated,
    }
    if not persist_assistant_message and safe_tool_log:
        response["function_calls"] = safe_tool_log
    return response


def _is_kommo_payment_screenshot(integration_context: dict | None, open_order: dict | None) -> bool:
    return (
        str((integration_context or {}).get("provider") or "").strip().lower() == "kommo"
        and bool((integration_context or {}).get("media_url_is_direct"))
        and str((open_order or {}).get("payment_status") or "").strip().lower() == "pending"
    )


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
    persist_user_message: bool = True,
    message_source_id: str | None = None,
) -> dict:
    """Validate payment screenshots deterministically before any LLM sees them."""
    t_start = time.monotonic()
    if persist_user_message:
        await conversations.store_message(
            customer_id=customer["id"],
            role="user",
            content=message_text,
            channel=channel,
            media_url=media_url,
            source_id=message_source_id,
            interaction_type=conversations.PRIVATE_MESSAGE_SCOPE,
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
            source_id=message_source_id,
            interaction_type=conversations.PRIVATE_MESSAGE_SCOPE,
        )
    response = {
        "text": reply_text,
        "interactive": None,
        "catalog_pdf": None,
        "product_image": None,
        "whatsapp_handoff": None,
        "customer_id": customer["id"],
        "escalated": False,
    }
    if not persist_assistant_message:
        response["function_calls"] = function_calls
    return response


async def _handle_exchange_rate_question(
    *,
    customer: dict,
    channel: str,
    media_url: str | None,
    message_text: str,
    settings: dict,
    orchestration_mode: str,
    persist_assistant_message: bool = True,
    persist_user_message: bool = True,
    message_source_id: str | None = None,
) -> dict:
    """Answer exchange-rate questions without routing through catalog-focused agents."""
    t_start = time.monotonic()
    if persist_user_message:
        await conversations.store_message(
            customer_id=customer["id"],
            role="user",
            content=message_text,
            channel=channel,
            media_url=media_url,
            source_id=message_source_id,
            interaction_type=conversations.PRIVATE_MESSAGE_SCOPE,
        )
    reply_text = _exchange_rate_reply(settings, message_text)
    response_time_ms = int((time.monotonic() - t_start) * 1000)
    await analytics.log_ai_run(
        customer_id=customer["id"],
        channel=channel,
        orchestration_mode=orchestration_mode,
        selected_agent="direct",
        route_intent="exchange_rate",
        route_source="deterministic_guard",
        route_confidence=1.0,
        provider=None,
        model=None,
        usage={},
        response_time_ms=response_time_ms,
        tool_names=[],
        tool_rounds=0,
        handoff_occurred=False,
        fallback_occurred=False,
        escalation_occurred=False,
        shadow_evaluation=False,
        legacy_fallback=False,
    )
    if persist_assistant_message:
        await conversations.store_message(
            customer_id=customer["id"],
            role="assistant",
            content=reply_text,
            channel=channel,
            source_id=message_source_id,
            interaction_type=conversations.PRIVATE_MESSAGE_SCOPE,
        )
    return {
        "text": reply_text,
        "interactive": None,
        "catalog_pdf": None,
        "product_image": None,
        "whatsapp_handoff": None,
        "customer_id": customer["id"],
        "escalated": False,
    }


async def _handle_public_comment_private_invite(
    *,
    customer: dict,
    channel: str,
    media_url: str | None,
    message_text: str,
    settings: dict,
    orchestration_mode: str,
    route_intent: str,
    persist_assistant_message: bool = True,
    message_source_id: str | None = None,
) -> dict:
    """Answer public comments without changing private customer or escalation state."""
    t_start = time.monotonic()
    await conversations.store_message(
        customer_id=customer["id"],
        role="user",
        content=message_text,
        channel=channel,
        media_url=media_url,
        source_id=message_source_id,
        interaction_type=conversations.INSTAGRAM_COMMENT_SCOPE,
    )
    reply_text = _public_comment_private_invite_text(settings)
    response_time_ms = int((time.monotonic() - t_start) * 1000)
    await analytics.log_ai_run(
        customer_id=customer["id"],
        channel=channel,
        orchestration_mode=orchestration_mode,
        selected_agent="direct",
        route_intent=route_intent,
        route_source="public_comment_guard",
        route_confidence=1.0,
        provider=None,
        model=None,
        usage={},
        response_time_ms=response_time_ms,
        tool_names=[],
        tool_rounds=0,
        handoff_occurred=False,
        fallback_occurred=False,
        escalation_occurred=False,
        shadow_evaluation=False,
        legacy_fallback=False,
    )
    if persist_assistant_message:
        await conversations.store_message(
            customer_id=customer["id"],
            role="assistant",
            content=reply_text,
            channel=channel,
            source_id=message_source_id,
            interaction_type=conversations.INSTAGRAM_COMMENT_SCOPE,
        )
    return {
        "text": reply_text,
        "interactive": None,
        "catalog_pdf": None,
        "product_image": None,
        "whatsapp_handoff": None,
        "customer_id": customer["id"],
        "escalated": False,
    }


async def _handle_public_instagram_comment(
    *,
    customer: dict,
    channel: str,
    media_url: str | None,
    message_text: str,
    settings: dict,
    integration_context: dict | None,
    orchestration_mode: str,
    persist_assistant_message: bool = True,
    message_source_id: str | None = None,
) -> dict:
    """Answer safe public Instagram comment intents using mapped catalog context."""
    t_start = time.monotonic()
    recent_history = await conversations.get_history(
        customer["id"],
        limit=2,
        interaction_type=conversations.INSTAGRAM_COMMENT_SCOPE,
    )
    await conversations.store_message(
        customer_id=customer["id"],
        role="user",
        content=message_text,
        channel=channel,
        media_url=media_url,
        source_id=message_source_id,
        interaction_type=conversations.INSTAGRAM_COMMENT_SCOPE,
    )

    request_kind = _classify_public_comment_request(message_text)
    if request_kind == "other":
        request_kind = _public_comment_follow_up_request_kind(recent_history) or request_kind
    reply_text = None
    resolution = None
    route_intent = f"public_comment_{request_kind}"
    if request_kind in {"price", "stock"}:
        product = None
        should_clarify = False
        try:
            await ensure_fresh_catalog()
        except Exception:
            logger.warning("Public comment catalog lookup skipped because the catalog is stale")
        else:
            product, should_clarify, resolution = await _resolve_public_comment_product(
                integration_context,
                message_text,
                settings,
            )
        if product:
            reply_text = (
                _public_comment_price_reply(product)
                if request_kind == "price"
                else _public_comment_stock_reply(product)
            )
        elif should_clarify:
            route_intent = "public_comment_clarification"
            reply_text = _PUBLIC_COMMENT_CLARIFICATION

    if not reply_text:
        route_intent = "public_comment_private_invite"
        reply_text = _public_comment_private_invite_text(settings)

    context = _public_comment_context(integration_context)
    logger.info(
        "public_instagram_comment_route_selected route_intent=%s mapping_status=%s product_resolved=%s",
        route_intent,
        context.get("mapping_status") or "missing",
        route_intent in {"public_comment_price", "public_comment_stock"},
    )

    response_time_ms = int((time.monotonic() - t_start) * 1000)
    await analytics.log_ai_run(
        customer_id=customer["id"],
        channel=channel,
        orchestration_mode=orchestration_mode,
        selected_agent="direct",
        route_intent=route_intent,
        route_source="public_comment_llm_matcher" if resolution else "public_comment_guard",
        route_confidence=1.0,
        provider=resolution["provider"] if resolution else None,
        model=resolution["model"] if resolution else None,
        usage=resolution["usage"] if resolution else {},
        response_time_ms=response_time_ms,
        tool_names=[],
        tool_rounds=0,
        handoff_occurred=False,
        fallback_occurred=False,
        escalation_occurred=False,
        shadow_evaluation=False,
        legacy_fallback=False,
    )
    if persist_assistant_message:
        await conversations.store_message(
            customer_id=customer["id"],
            role="assistant",
            content=reply_text,
            channel=channel,
            source_id=message_source_id,
            interaction_type=conversations.INSTAGRAM_COMMENT_SCOPE,
        )
    return {
        "text": reply_text,
        "interactive": None,
        "catalog_pdf": None,
        "product_image": None,
        "whatsapp_handoff": None,
        "customer_id": customer["id"],
        "escalated": False,
    }


def _classify_public_comment_request(message_text: str) -> str:
    normalized = guards.normalize_text_for_moderation(_INITIAL_GREETING_RE.sub("", message_text or ""))
    if not normalized:
        return "other"
    if _PUBLIC_COMMENT_PRIVATE_DETAIL_RE.search(normalized):
        return "other"
    has_price = bool(_PUBLIC_COMMENT_PRICE_RE.search(normalized))
    has_stock = bool(_PUBLIC_COMMENT_STOCK_RE.search(normalized))
    if has_price:
        return "price"
    if has_stock:
        return "stock"
    return "other"


def _public_comment_follow_up_request_kind(history: list[dict]) -> str | None:
    """Keep the prior price/stock intent when a commenter names the product next."""
    if len(history) != 2:
        return None
    question, clarification = history
    if (
        question.get("role") != "user"
        or clarification.get("role") != "assistant"
        or str(clarification.get("content") or "").strip() != _PUBLIC_COMMENT_CLARIFICATION
    ):
        return None
    request_kind = _classify_public_comment_request(str(question.get("content") or ""))
    return request_kind if request_kind in {"price", "stock"} else None


def _public_comment_private_invite_text(settings: dict) -> str:
    phone = _normalize_public_store_phone(settings.get("store_phone_number"))
    if phone:
        return f"Hola! Para más info escríbenos al DM o por WhatsApp al {phone}! :)"
    return "Hola! Para más info escríbenos al DM! :)"


def _normalize_public_store_phone(value) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


async def _resolve_public_comment_product(
    integration_context: dict | None,
    message_text: str,
    settings: dict,
) -> tuple[dict | None, bool, dict | None]:
    context = _public_comment_context(integration_context)
    if not context:
        return None, False, None

    if context.get("mapping_status") != "resolved":
        return None, False, None

    mapped_skus = context.get("product_skus")
    if not isinstance(mapped_skus, list):
        mapped_skus = [context.get("product_sku")] if context.get("product_sku") else []
    mapped_skus = list(dict.fromkeys(mapped_skus))
    if not mapped_skus:
        return None, False, None

    grouped_products = group_catalog_products(get_cached_reference_catalog())
    if not grouped_products:
        return None, False, None

    mapped_products = []
    for mapped_sku in mapped_skus:
        product = _single_public_comment_match(
            _match_public_comment_product_by_sku(mapped_sku, grouped_products)
        )
        if not product:
            return None, False, None
        mapped_products.append(product)

    unique_mapped_products = {
        _public_comment_product_identity(product): product
        for product in mapped_products
        if _public_comment_product_identity(product)
    }
    if _public_comment_mentions_mapped_variant_sku(
        message_text,
        list(unique_mapped_products.values()),
    ):
        return None, True, None
    if len(unique_mapped_products) == 1:
        return next(iter(unique_mapped_products.values())), False, None

    matched_product = _single_public_comment_match(
        _match_public_comment_product_by_text(
            message_text,
            list(unique_mapped_products.values()),
        )
    )
    if matched_product:
        return matched_product, False, None

    mapped_products = list(unique_mapped_products.values())
    if not _public_comment_has_product_reference(message_text):
        return None, True, None
    return await _resolve_public_comment_product_with_llm(message_text, mapped_products, settings)


async def _resolve_public_comment_product_with_llm(
    message_text: str,
    mapped_products: list[dict],
    settings: dict,
) -> tuple[dict | None, bool, dict | None]:
    """Select one mapped product from semantic catalog context, or fail closed."""
    provider_name = str(settings.get("llm_provider") or "openai")
    if provider_name not in _list_providers():
        return None, True, None

    model = str(settings.get("llm_model") or "gpt-5.6-luna")
    allowed_products = [
        {
            "sku": product.get("parent_sku") or product.get("sku"),
            "name": str(product.get("product_name") or "")[:300],
            "category": str(product.get("category") or "")[:200],
            "description": str(product.get("description") or "")[:500],
        }
        for product in mapped_products
    ]
    try:
        response = await get_provider(provider_name).chat(
            model=model,
            system_prompt=(
                "Selecciona el producto de Instagram al que se refiere el comentario. "
                "Usa nombre, categoria y descripcion. Solo puedes seleccionar un SKU de la lista. "
                "Si no hay una referencia clara o hay empate, responde exactamente JSON con sku null. "
                'Responde solo JSON: {"sku":"SKU permitido o null","confidence":0.0 a 1.0}.'
            ),
            messages=[{
                "role": "user",
                "content": "Comentario:\n"
                f"{message_text[:1000]}\n\nProductos mapeados:\n"
                f"{json.dumps(allowed_products, ensure_ascii=False)}",
            }],
            tools=None,
            temperature=0,
            max_tokens=100,
        )
    except Exception as exc:
        logger.warning("Public comment product matcher failed: %s", exc)
        return None, True, None

    selected_sku = _parse_public_comment_matcher_response(response.text or "", allowed_products)
    resolution = {
        "provider": provider_name,
        "model": model,
        "usage": response.usage,
    }
    if not selected_sku:
        return None, True, resolution
    product = _single_public_comment_match(
        _match_public_comment_product_by_sku(selected_sku, mapped_products)
    )
    return (product, False, resolution) if product else (None, True, resolution)


def _parse_public_comment_matcher_response(text: str, allowed_products: list[dict]) -> str | None:
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if not match:
        return None
    try:
        result = json.loads(match.group(0))
        confidence = float(result.get("confidence", 0))
    except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
        return None
    sku = _normalize_catalog_text(result.get("sku") or "")
    allowed_skus = {
        _normalize_catalog_text(product.get("sku") or "")
        for product in allowed_products
    }
    return sku if confidence >= 0.7 and sku in allowed_skus else None


def _public_comment_has_product_reference(message_text: str) -> bool:
    return any(
        len(term) > 2 and term not in _PUBLIC_COMMENT_REFERENCE_STOPWORDS
        for term in re.findall(r"\w+", _normalize_catalog_text(message_text))
    )


def _public_comment_context(integration_context: dict | None) -> dict:
    raw_context = {}
    source = integration_context or {}
    nested = source.get("public_comment_context")
    if isinstance(nested, dict):
        raw_context.update(nested)
    raw_context.update({key: source.get(key) for key in _PUBLIC_COMMENT_CONTEXT_KEYS if key in source})
    cleaned_context = {}
    for key, value in raw_context.items():
        if key not in _PUBLIC_COMMENT_CONTEXT_KEYS:
            continue
        if key == "product_skus":
            if not isinstance(value, list):
                continue
            product_skus = [
                cleaned
                for sku in value[:20]
                if (cleaned := _clean_public_comment_context_value(sku))
            ]
            if product_skus:
                cleaned_context[key] = list(dict.fromkeys(product_skus))
        elif cleaned := _clean_public_comment_context_value(value):
            cleaned_context[key] = cleaned
    return cleaned_context


def _clean_public_comment_context_value(value) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if not text or (text.startswith("{{") and text.endswith("}}")):
        return ""
    return text[:1000]


def _match_public_comment_product_by_sku(value: str | None, grouped_products: list[dict]) -> list[dict]:
    target = _normalize_catalog_text(value or "")
    if not target:
        return []
    matches = []
    for product in grouped_products:
        skus = {
            _normalize_catalog_text(product.get("sku", "")),
            _normalize_catalog_text(product.get("parent_sku", "")),
        }
        if target in {sku for sku in skus if sku}:
            matches.append(product)
    return matches


def _public_comment_mentions_mapped_variant_sku(
    value: str | None,
    grouped_products: list[dict],
) -> bool:
    text = _normalize_catalog_text(value or "")
    if not text:
        return False
    for product in grouped_products:
        grouped_skus = {
            _normalize_catalog_text(product.get("sku", "")),
            _normalize_catalog_text(product.get("parent_sku", "")),
        }
        for variant in product.get("variants", []) or []:
            variant_sku = _normalize_catalog_text(variant.get("sku", ""))
            if (
                variant_sku
                and variant_sku not in grouped_skus
                and re.search(rf"(?<![\w-]){re.escape(variant_sku)}(?![\w-])", text)
            ):
                return True
    return False


def _match_public_comment_product_by_image(value: str | None, grouped_products: list[dict]) -> list[dict]:
    target = str(value or "").strip()
    if not target.startswith(("http://", "https://")):
        return []
    matches = []
    for product in grouped_products:
        urls = {str(product.get("image_url") or "").strip()}
        urls.update(str(variant.get("image_url") or "").strip() for variant in product.get("variants", []) or [])
        if target in {url for url in urls if url}:
            matches.append(product)
    return matches


def _match_public_comment_product_by_text(value: str | None, grouped_products: list[dict]) -> list[dict]:
    text = _normalize_catalog_text(value or "")
    if not text:
        return []
    product_terms = {
        _public_comment_product_identity(product): _public_comment_product_name_terms(product)
        for product in grouped_products
    }
    term_product_counts: dict[str, int] = {}
    for terms in product_terms.values():
        for term in terms:
            term_product_counts[term] = term_product_counts.get(term, 0) + 1

    matches = []
    for product in grouped_products:
        product_name = _normalize_catalog_text(product.get("product_name", ""))
        if not product_name:
            continue
        terms = product_terms.get(_public_comment_product_identity(product), set())
        product_skus = {
            _normalize_catalog_text(product.get("sku", "")),
            _normalize_catalog_text(product.get("parent_sku", "")),
        }
        sku_matches = any(
            sku and re.search(rf"(?<![\w-]){re.escape(sku)}(?![\w-])", text)
            for sku in product_skus
        )
        matched_terms = {term for term in terms if re.search(rf"(?<!\w){re.escape(term)}(?!\w)", text)}
        name_matches = (
            product_name == text
            or product_name in text
            or (len(terms) >= 2 and terms.issubset(matched_terms))
            # A comment often omits a material or style word (for example,
            # "pijama azul" for "Pijama satén azul"). Two matching product
            # terms, or one term unique to this mapped content, is specific
            # enough without considering products outside the post.
            or len(matched_terms) >= 2
            or any(len(term) >= 4 and term_product_counts[term] == 1 for term in matched_terms)
        )
        if sku_matches or name_matches:
            matches.append(product)
    return matches


def _public_comment_product_name_terms(product: dict) -> set[str]:
    return {
        term
        for term in _normalize_catalog_text(product.get("product_name", "")).split()
        if len(term) > 2 and term not in _PUBLIC_COMMENT_STOPWORDS
    }


def _single_public_comment_match(matches: list[dict]) -> dict | None:
    unique = {}
    for product in matches:
        identity = _public_comment_product_identity(product)
        if identity:
            unique[identity] = product
    return next(iter(unique.values())) if len(unique) == 1 else None


def _public_comment_product_identity(product: dict) -> str:
    return _normalize_catalog_text(
        product.get("parent_sku")
        or product.get("sku")
        or f"{product.get('product_name', '')}|{product.get('category', '')}"
    )


def _public_comment_price_reply(product: dict) -> str | None:
    price = _single_public_comment_price(product)
    name = str(product.get("product_name") or "").strip()
    if price is None or not name:
        return None
    return _sanitize_public_comment_reply(
        f"¡Hola! El precio de {name} es {_format_usd_price(price)}."
    )


def _public_comment_stock_reply(product: dict) -> str | None:
    name = str(product.get("product_name") or "").strip()
    if not name:
        return None
    stock = _safe_public_comment_stock(product)
    if stock is None:
        return None
    if stock > 0:
        return _sanitize_public_comment_reply(f"¡Hola! Sí, {name} está disponible.")
    return _sanitize_public_comment_reply(f"¡Hola! Por ahora {name} no está disponible.")


def _single_public_comment_price(product: dict) -> float | None:
    prices = set()
    for variant in product.get("variants", []) or []:
        price = _safe_public_comment_float(variant.get("price_usd"))
        if price and price > 0:
            prices.add(round(price, 2))
    if not prices:
        price = _safe_public_comment_float(product.get("price_usd"))
        if price and price > 0:
            prices.add(round(price, 2))
    return next(iter(prices)) if len(prices) == 1 else None


def _safe_public_comment_stock(product: dict) -> int | None:
    try:
        return int(product.get("stock", 0) or 0)
    except (TypeError, ValueError):
        return None


def _safe_public_comment_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _format_usd_price(value: float) -> str:
    if float(value).is_integer():
        return f"${int(value)}"
    return f"${value:.2f}".rstrip("0").rstrip(".")


def _detect_exchange_rate_question(message_text: str, stored_history: list[dict] | None = None) -> bool:
    normalized = guards.normalize_text_for_moderation(message_text)
    if _looks_like_exchange_rate_question(normalized):
        return True
    if normalized not in {"?", "??", "???", "aja", "aja?", "ajá", "aja y la tasa?"} and "tasa" not in normalized:
        return False
    for message in reversed(stored_history or []):
        role = str(message.get("role") or "")
        content = guards.normalize_text_for_moderation(str(message.get("content") or ""))
        if role == "assistant":
            return False
        if role == "user" and _looks_like_exchange_rate_question(content):
            return True
    return False


def _is_exchange_rate_only_message(message_text: str) -> bool:
    lines = [line.strip() for line in message_text.splitlines() if line.strip()]
    if len(lines) != 1:
        return False
    normalized = guards.normalize_text_for_moderation(lines[0])
    discount_markers = (
        "descuento",
        "descuentos",
        "rebaja",
        "rebajas",
        "oferta",
        "ofertas",
        "promocion",
        "promociones",
    )
    return not any(marker in normalized for marker in discount_markers)


def _looks_like_exchange_rate_question(normalized: str) -> bool:
    if not normalized:
        return False
    if "tasa" in normalized:
        return any(
            marker in normalized
            for marker in (
                "que",
                "cual",
                "cuanto",
                "manejan",
                "usan",
                "reciben",
                "tienen",
                "hoy",
                "dia",
                "binance",
                "?",
            )
        )
    currency_markers = ("dolar", "dolares", "usd", "binance")
    if not any(marker in normalized for marker in currency_markers):
        return False
    return any(
        marker in normalized
        for marker in (
            "a cuanto",
            "a que",
            "cuanto esta",
            "cuanto tienen",
            "cuanto manejan",
            "cambio manejan",
            "cambio usan",
        )
    )


def _exchange_rate_reply(settings: dict, message_text: str = "") -> str:
    return build_customer_exchange_rate_reply(settings, message_text)


def _with_conversation_continuity_guidance(system_prompt: str, has_previous_context: bool) -> str:
    if has_previous_context:
        guidance = (
            "Esta conversacion ya tiene contexto previo almacenado. "
            "No abras con un saludo inicial. Usa el historial para entender a que producto, "
            "preferencia o paso de compra se refiere el ultimo mensaje y responde con continuidad natural. "
            "No repitas preguntas ni respuestas que ya quedaron claras, ni reinicies el flujo de venta."
        )
    else:
        guidance = "No hay contexto previo almacenado. Puedes saludar brevemente al iniciar la conversacion."
    return f"{system_prompt}\n\n# Continuidad de conversacion\n\n{guidance}"


def _with_public_comment_guidance(system_prompt: str) -> str:
    guidance = (
        "Esta respuesta se publicara como comentario de Instagram, visible para otras personas. "
        "Responde en una sola frase corta, sin Markdown, sin listas y sin datos privados. "
        "No menciones pedidos, pagos, direcciones, telefonos, envios ni informacion personal del cliente. "
        "No hagas checkout ni pidas datos de compra; cuando haga falta informacion privada, invita a escribir por DM."
    )
    return f"{system_prompt}\n\n# Reglas para comentario publico de Instagram\n\n{guidance}"


def _is_public_instagram_comment(integration_context: dict | None) -> bool:
    return (integration_context or {}).get("interaction_type") == "instagram_comment"


def _resolve_private_instagram_content_context(
    *,
    channel: str,
    integration_context: dict | None,
    message_text: str,
    products: list[dict],
) -> dict:
    context = (integration_context or {}).get("incoming_instagram_context")
    if (
        channel != "instagram"
        or (integration_context or {}).get("provider") != "kommo"
        or (integration_context or {}).get("interaction_type") != "private_message"
        or not isinstance(context, dict)
        or context.get("source") != "story_reply"
        or context.get("mapping_status") != "resolved"
    ):
        return {}

    mapped_skus = list(dict.fromkeys(
        str(sku).strip() for sku in context.get("product_skus") or [] if str(sku).strip()
    ))
    products_by_sku = {
        str(product.get("sku") or "").strip(): product
        for product in products
        if str(product.get("sku") or "").strip()
    }
    if not mapped_skus or any(sku not in products_by_sku for sku in mapped_skus):
        return {}

    selected_sku = str(context.get("selected_product_sku") or "").strip() or None
    normalized_message = _normalize_catalog_text(message_text)
    explicit_matches = []
    for sku, product in products_by_sku.items():
        normalized_name = _normalize_catalog_text(product.get("product_name") or "")
        normalized_sku = _normalize_catalog_text(sku)
        if (
            normalized_name
            and len(normalized_name) >= 3
            and normalized_name in normalized_message
        ) or (normalized_sku and normalized_sku in normalized_message):
            explicit_matches.append(sku)
    explicit_matches = list(dict.fromkeys(explicit_matches))
    if len(explicit_matches) == 1:
        if explicit_matches[0] not in mapped_skus:
            return {"_clear_story_context": True}
        selected_sku = explicit_matches[0]

    resolved_products = []
    for sku in mapped_skus:
        product = products_by_sku[sku]
        price = _single_public_comment_price(product)
        try:
            in_stock = float(product.get("stock", 0) or 0) > 0
        except (TypeError, ValueError):
            in_stock = False
        resolved_products.append({
            "sku": sku,
            "name": product.get("product_name") or "Producto",
            "price_text": _format_usd_price(price) if price is not None else "variable; confirmar",
            "sizes": product.get("sizes") or "",
            "availability": "disponible" if in_stock else "agotado",
        })
    return {
        "source": "story_reply",
        "story_id": context.get("story_id"),
        "product_skus": mapped_skus,
        "selected_product_sku": selected_sku if selected_sku in mapped_skus else None,
        "products": resolved_products,
    }


def _sanitize_public_comment_reply(text: str | None) -> str:
    cleaned = _runner_clean_assistant_reply_text(text or "")
    cleaned = re.sub(r"```[\s\S]*?```", " ", cleaned)
    cleaned = re.sub(r"`([^`\n]+?)`", r"\1", cleaned)
    cleaned = re.sub(r"\*\*(.+?)\*\*", r"\1", cleaned, flags=re.DOTALL)
    cleaned = re.sub(r"__(.+?)__", r"\1", cleaned, flags=re.DOTALL)
    cleaned = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s+", "", cleaned, flags=re.MULTILINE)
    cleaned = cleaned.replace("*", "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned:
        cleaned = "Escríbenos por DM y con gusto te ayudamos."
    return cleaned[:297].rstrip() + "..." if len(cleaned) > 300 else cleaned


def _catalog_pdf_supported(channel: str, integration_context: dict | None, config=None) -> bool:
    config = config or get_config()
    delivery_provider = (integration_context or {}).get("provider")
    if not delivery_provider:
        delivery_provider = getattr(config, "channel_backend", "meta")
    if not isinstance(delivery_provider, str) or delivery_provider not in {"meta", "kommo"}:
        delivery_provider = "meta"
    if channel != "whatsapp":
        return False
    if delivery_provider == "meta":
        return True
    return (
        (integration_context or {}).get("interaction_type", "private_message")
        == "private_message"
        and bool(getattr(config, "kommo_chats_media_enabled", False))
        and bool(getattr(config, "kommo_chats_catalog_pdf_enabled", False))
        and getattr(config, "kommo_chats_pdf_attachment_type", None) == "file"
    )


def _required_whatsapp_handoff_reason(route_intent: str) -> str | None:
    if route_intent == "instagram_catalog_pdf_handoff":
        return "catalog_pdf"
    if route_intent == "instagram_whatsapp_handoff":
        return "purchase"
    return None


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
    tool_names = filter_tool_names(channel, tool_names)
    if _is_public_instagram_comment(integration_context):
        return tuple(name for name in tool_names if name == "check_inventory")
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
        str(customer_id),
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
    catalog = get_cached_catalog()
    if not catalog:
        return {"status": "error", "message": "Catalog is empty, cannot generate PDF."}
    try:
        ensure_catalog_pdf(catalog)
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
