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
import unicodedata

from app import analytics, db
from app.admin.notify import notify_escalation, notify_incoming_message, notify_new_order
from app.ai.functions import TOOLS
from app.ai.prompts import build_system_prompt, format_catalog_as_markdown
from app.ai.providers import AVAILABLE_MODELS, get_provider
from app.ai.providers import list_providers as _list_providers
from app.ai.safety import sanitize_customer_facing_text
from app.ai.vision import analyze_payment_screenshot
from app.catalog.pdf_generator import PDF_PATH, generate_catalog_pdf
from app.catalog.sheets import get_cached_catalog, get_product_sizes, group_catalog_products
from app.config import get_config
from app.crm import conversations, customers, orders
from app.customer_identity import extract_safe_first_name
from app.payment_methods import payment_method_tag_value

logger = logging.getLogger(__name__)

# Maximum number of tool-call rounds per message (prevent infinite loops)
MAX_TOOL_ROUNDS = 6

_HOSTILE_MESSAGE_PATTERNS = [
    (
        re.compile(r"\b(estafa|estafadores?|ladrones?|fraude|timador(?:es)?|robo)\b"),
        "Cliente acusa a la tienda de estafa o robo.",
    ),
    (
        re.compile(
            r"\b(maldit[oa]s?|idiot[ae]s?|imbecil(?:es)?|estupid[oa]s?|"
            r"basura|porqueria|inutil(?:es)?|payas[oa]s?|mierda|asqueros[oa]s?)\b"
        ),
        "Cliente usa insultos o lenguaje agresivo hacia la tienda.",
    ),
    (
        re.compile(
            r"(voy a denunciar|te voy a denunciar|los voy a denunciar|"
            r"voy a demandar|te voy a demandar|los voy a demandar|"
            r"voy a quemar|te voy a quemar|los voy a quemar|"
            r"voy a funar|te voy a exponer|los voy a exponer|"
            r"me las van a pagar|les voy a caer)"
        ),
        "Cliente usa amenazas o lenguaje agresivo.",
    ),
]


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
    ai_enabled = settings.get("ai_enabled", True)
    is_escalated = customer.get("conversation_state") == "escalated"

    if not ai_enabled or is_escalated:
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

        if not already_notified:
            await notify_incoming_message(
                customer_name=customer.get("display_name"),
                customer_channel=channel,
                customer_platform_id=sender_id,
                message_text=message_text,
            )
        return {
            "text": None,
            "interactive": None,
            "catalog_pdf": None,
            "product_image": None,
            "customer_id": customer["id"],
            "escalated": is_escalated,
            "paused": not ai_enabled,
        }

    # ── 2c. Auto-escalate abusive customers ─────────────────
    hostility_reason = _detect_hostile_customer_message(message_text)
    if hostility_reason:
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

    # Append the new message
    history.append({"role": "user", "content": message_text})

    open_order = await orders.get_latest_open_order(customer["id"])

    # ── 4. Build system prompt with live catalog ─────────────
    catalog = get_cached_catalog()
    catalog_md = format_catalog_as_markdown(catalog)
    system_prompt = build_system_prompt(
        catalog_markdown=catalog_md,
        store_name=settings.get("store_name", config.store_name),
        channel=channel,
        customer=customer,
        open_order=open_order,
        payment_methods=payment_methods,
        accepted_exchange_rate=str(settings.get("accepted_exchange_rate", "") or ""),
        order_discount_percent=settings.get("order_discount_percent"),
        order_discount_threshold_usd=settings.get("order_discount_threshold_usd"),
    )

    # ── 5. Call the LLM ──────────────────────────────────────
    was_fallback = False
    provider_name = settings.get("llm_provider", "openai")
    model = settings.get("llm_model", "gpt-5.4-nano")

    temperature = settings.get("llm_temperature", 0.7)
    max_tokens = settings.get("llm_max_tokens", 500)

    # Resolve the provider (fall back to whatever is available)
    available = _list_providers()

    if provider_name not in available:
        if available:
            old_name = provider_name
            provider_name = available[0]
            logger.warning(f"Provider '{old_name}' not available, falling back to '{provider_name}'")
            model = next(
                (m["id"] for m in AVAILABLE_MODELS.get(provider_name, []) if m.get("default")),
                "gpt-5.4-nano",
            )
        else:
            raise RuntimeError("No LLM providers are configured.")

    provider = get_provider(provider_name)

    # ── 5b. If image attached, analyze it first ──────────────
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
        if vision_result.get("analyzed"):
            payment_proof_attempt = _looks_like_payment_proof_message(message_text, vision_result)
            # Inject vision analysis into the message text so the AI has context
            summary = vision_result.get("summary", "Imagen analizada")
            message_text = f"{message_text}\n\n[Análisis de imagen: {summary}]"
            # Update the last message in history
            history[-1] = {"role": "user", "content": message_text}

    if payment_proof_attempt and not open_order:
        first_name = extract_safe_first_name(customer.get("display_name"))
        greeting_name = first_name or "hola"
        await conversations.store_message(
            customer_id=customer["id"],
            role="user",
            content=message_text,
            channel=channel,
            media_url=media_url,
        )
        reply_text = (
            f"{greeting_name.capitalize()}, todavía no tengo el pedido registrado para poder validar ese comprobante. "
            "Déjame primero dejarte el pedido armado y enseguida seguimos con el pago."
        )
        if persist_assistant_message:
            await conversations.store_message(
                customer_id=customer["id"],
                role="assistant",
                content=reply_text,
                channel=channel,
            )
        return {
            "text": reply_text,
            "interactive": None,
            "catalog_pdf": None,
            "product_image": None,
            "customer_id": customer["id"],
            "escalated": False,
        }

    # ── 5c. LLM call with timing ─────────────────────────────
    t_start = time.monotonic()

    try:
        response = await provider.chat(
            model=model,
            system_prompt=system_prompt,
            messages=history,
            tools=TOOLS,
            temperature=temperature,
            max_tokens=max_tokens,
        )
    except Exception as e:
        logger.error(f"Primary provider ({provider_name}) failed: {e}")
        # Auto-fallback
        if settings.get("auto_fallback", True):
            fb_name = settings.get("fallback_provider", "anthropic")
            fb_model = settings.get("fallback_model", "claude-haiku-4-5")
            logger.info(f"Falling back to {fb_name}/{fb_model}")
            provider_name = fb_name
            model = fb_model
            was_fallback = True
            provider = get_provider(fb_name)
            response = await provider.chat(
                model=fb_model,
                system_prompt=system_prompt,
                messages=history,
                tools=TOOLS,
                temperature=temperature,
                max_tokens=max_tokens,
            )
        else:
            raise

    # ── 6. Handle tool calls ─────────────────────────────────
    interactive_payload = None
    catalog_pdf_payload = None
    product_image_payload = None
    tool_log = []
    rounds = 0
    single_use_tool_results: dict[str, dict] = {}

    while response.tool_calls and rounds < MAX_TOOL_ROUNDS:
        rounds += 1

        # Process one tool call at a time so each continue_after_tool call
        # is based on up-to-date history. If the LLM returned multiple tool
        # calls, the while loop will handle the remaining ones in subsequent
        # rounds via the updated response.
        tool_call = response.tool_calls[0]
        name = tool_call["name"]
        args = tool_call["arguments"]
        tc_id = tool_call["id"]

        logger.info(f"Tool call: {name}")
        if name in single_use_tool_results:
            result = {
                **single_use_tool_results[name],
                "duplicate_ignored": True,
                "message": f"Duplicate tool call ignored: {name}",
            }
        elif name == "create_order" and payment_proof_attempt:
            result = {
                "status": "error",
                "message": (
                    "No se puede crear un pedido a partir de un comprobante de pago. "
                    "El pedido debe existir antes de validar el pago."
                ),
            }
        else:
            result = await _execute_tool(
                name,
                args,
                customer,
                channel,
                payment_methods=payment_methods,
                vision_result=vision_result,
                payment_proof_attempt=payment_proof_attempt,
                latest_user_message=message_text,
                integration_context=integration_context,
            )
            if name in {"create_order", "update_payment_status"} and result.get("status") != "error":
                single_use_tool_results[name] = result
        tool_log.append({"name": name, "args": args, "result": result})

        # Check if the tool wants to send interactive buttons
        if name == "send_interactive_buttons" and channel == "whatsapp":
            interactive_payload = result

        # Check if the tool wants to send the catalog PDF
        if name == "send_catalog_pdf" and channel == "whatsapp":
            catalog_pdf_payload = result

        # Check if the tool wants to send a product image
        if name == "send_product_image" and result.get("type") == "product_image":
            product_image_payload = result

        # On the final allowed round, withhold tools so the model is forced
        # to generate a text response instead of calling another tool.
        tools_this_round = TOOLS if rounds < MAX_TOOL_ROUNDS else None

        # Continue the conversation with the tool result
        response = await provider.continue_after_tool(
            model=model,
            system_prompt=system_prompt,
            messages=history,
            tool_call_id=tc_id,
            tool_name=name,
            tool_result=_format_tool_result_for_model(name, result),
            tools=tools_this_round,
            temperature=temperature,
            max_tokens=max_tokens,
        )

    # ── 7. Log usage with response timing ──────────────────
    t_end = time.monotonic()
    response_time_ms = int((t_end - t_start) * 1000)

    await analytics.log_response(
        provider=provider_name,
        model=model,
        usage=response.usage,
        customer_id=customer["id"],
        response_time_ms=response_time_ms,
        was_fallback=was_fallback,
        had_tool_calls=bool(tool_log),
        channel=channel,
    )

    logger.info(
        f"Response generated in {response_time_ms}ms "
        f"({provider_name}/{model}, "
        f"{response.usage.get('input_tokens', 0)}+{response.usage.get('output_tokens', 0)} tokens"
        f"{', FALLBACK' if was_fallback else ''})"
    )

    # ── 8. Store messages ────────────────────────────────────
    await conversations.store_message(
        customer_id=customer["id"],
        role="user",
        content=message_text,
        channel=channel,
        media_url=media_url,
    )

    if interactive_payload:
        interactive_payload["body_text"] = _clean_assistant_reply_text(interactive_payload.get("body_text", ""))

    if catalog_pdf_payload:
        catalog_pdf_payload["caption"] = _clean_assistant_reply_text(catalog_pdf_payload.get("caption", ""))

    if product_image_payload:
        product_image_payload["caption"] = _clean_assistant_reply_text(product_image_payload.get("caption", ""))

    # When send_interactive_buttons was used, the model sometimes returns
    # text=None (treating buttons as the full response). Use body_text in that
    # case so the customer still receives a readable message.
    if not response.text and interactive_payload:
        reply_text = interactive_payload.get("body_text", "")
    elif not response.text and product_image_payload:
        reply_text = product_image_payload.get("caption") or "Aquí tienes la foto del producto."
    else:
        reply_text = response.text or "Lo siento, no pude generar una respuesta. ¿Puedes repetir tu pregunta?"
    reply_text = _clean_assistant_reply_text(reply_text) or "Lo siento, no pude generar una respuesta. ¿Puedes repetir tu pregunta?"

    if persist_assistant_message:
        await conversations.store_message(
            customer_id=customer["id"],
            role="assistant",
            content=reply_text,
            channel=channel,
            function_calls=tool_log if tool_log else None,
        )

    return {
        "text": reply_text,
        "interactive": interactive_payload,
        "catalog_pdf": catalog_pdf_payload,
        "product_image": product_image_payload,
        "customer_id": customer["id"],
        "escalated": False,
        "function_calls": tool_log if tool_log else None,
    }


def _detect_hostile_customer_message(message_text: str) -> str | None:
    """
    Detect clear insults, accusations, or threats from the customer.
    High-confidence matches are escalated immediately to a human.
    """
    normalized = _normalize_text_for_moderation(message_text)
    for pattern, reason in _HOSTILE_MESSAGE_PATTERNS:
        if pattern.search(normalized):
            return reason
    return None


def _normalize_text_for_moderation(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", (text or "").lower())
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"\s+", " ", ascii_text).strip()


# ── Tool execution ───────────────────────────────────────────

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
    """
    Execute a tool call and return the result as a dict.
    The result is sent back to the LLM so it can craft its final response.
    """
    customer_id = customer["id"]

    if name == "check_inventory":
        return await _tool_check_inventory(args)

    elif name == "tag_customer":
        await customers.add_tags(customer_id, args.get("tags", []))
        return {"status": "ok", "message": "Tags added silently."}

    elif name == "create_order":
        order = await orders.create_order(
            customer_id=customer_id,
            items=args.get("items", []),
            payment_method=args.get("payment_method", ""),
            shipping_city=args.get("shipping_city"),
            shipping_address=args.get("shipping_address"),
            shipping_method=args.get("shipping_method"),
        )
        if order.get("created_new", True):
            items_summary = ", ".join(
                f"{i['product_name']} ({i['size']})" for i in args.get("items", [])
            )
            await notify_new_order(
                customer_name=customer.get("display_name"),
                order_total=order["total"],
                payment_method=order["payment_method"],
                items_summary=items_summary,
            )
        # Save address on customer for future orders
        addr = args.get("shipping_address")
        if addr:
            await db.execute(
                """UPDATE customers
                   SET last_shipping_address = :addr,
                       last_shipping_city = :city,
                       last_shipping_method = :method
                   WHERE id = :id""",
                {
                    "addr": addr,
                    "city": args.get("shipping_city", ""),
                    "method": args.get("shipping_method", ""),
                    "id": customer_id,
                },
            )

        # Auto-tag
        payment_method = args.get("payment_method", "")
        await customers.add_tags(customer_id, [f"payment:{payment_method_tag_value(payment_method)}"])

        return order

    elif name == "update_payment_status":
        if payment_proof_attempt:
            validation = await _validate_payment_proof(
                customer_id=customer_id,
                payment_methods=payment_methods or [],
                vision_result=vision_result or {},
            )
            if validation.get("status") != "ok":
                return validation

            result = await orders.update_order_payment_status(
                validation["order_id"],
                status="proof_received",
                note=args.get("confirmation_note"),
            )
            if not result:
                return {"status": "error", "message": "No open order found for this customer."}

            result["validated_amount"] = validation.get("validated_amount")
            result["validated_payment_method"] = validation.get("validated_payment_method")
            return result

        result = await orders.update_payment_status(
            customer_id=customer_id,
            status="proof_received",
            note=args.get("confirmation_note"),
        )
        return result or {"status": "error", "message": "No pending order found for this customer."}

    elif name == "escalate_to_human":
        if _should_block_product_inquiry_escalation(
            reason=args.get("reason", ""),
            latest_user_message=latest_user_message,
        ):
            return {
                "status": "error",
                "message": (
                    "No escales preguntas normales de productos no disponibles o fuera del catálogo. "
                    "Explica que no lo manejan o que está agotado y ofrece alternativas del catálogo."
                ),
            }

        summary = await conversations.get_recent_summary(customer_id, limit=5)
        await notify_escalation(
            customer_name=customer.get("display_name"),
            customer_channel=customer.get("channel", "whatsapp"),
            customer_platform_id=customer.get("platform_id", ""),
            reason=args.get("reason", "Razón no especificada"),
            urgency=args.get("urgency", "medium"),
            conversation_summary=summary,
        )
        await customers.set_conversation_state(customer_id, "escalated")
        await _sync_kommo_escalation_if_needed(
            customer_id=customer_id,
            reason=args.get("reason", "Razón no especificada"),
            urgency=args.get("urgency", "medium"),
            conversation_summary=summary,
            lead_id=(integration_context or {}).get("lead_id"),
        )
        return {
            "status": "escalated",
            "message": "The store owner has been notified and will respond shortly.",
        }

    elif name == "send_interactive_buttons":
        # This doesn't call an external service; the result is passed back
        # to the webhook handler which sends the WhatsApp interactive message
        return {
            "type": "interactive_buttons",
            "body_text": args.get("body_text", ""),
            "buttons": args.get("buttons", []),
        }

    elif name == "send_catalog_pdf":
        # Auto-generate the PDF if it doesn't exist yet, then signal the webhook handler.
        generated_pdf = False
        if not PDF_PATH.exists():
            catalog = get_cached_catalog()
            if not catalog:
                return {"status": "error", "message": "Catalog is empty, cannot generate PDF."}
            try:
                generate_catalog_pdf(catalog)
                generated_pdf = True
            except Exception as e:
                logger.error(f"Auto-generate catalog PDF failed: {e}")
                return {"status": "error", "message": "Could not generate catalog PDF."}
        if generated_pdf and get_config().channel_backend == "kommo":
            from app.integrations.kommo.files import sync_catalog_pdf_to_kommo

            await sync_catalog_pdf_to_kommo(PDF_PATH)
        return {
            "type": "catalog_pdf",
            "caption": args.get("caption", "Aqui tienes nuestro catalogo de productos 📖"),
        }

    elif name == "send_product_image":
        return await _tool_send_product_image(args)

    else:
        logger.warning(f"Unknown tool: {name}")
        return {"status": "error", "message": f"Unknown tool: {name}"}


async def _tool_check_inventory(args: dict) -> dict:
    """Search the cached product catalog for matching products."""
    matches = _find_catalog_matches(
        product_query=args.get("product_query", ""),
        size_filter=args.get("size"),
    )
    grouped_matches = group_catalog_products(matches)

    result_products = []
    for product in grouped_matches:
        result_products.append({
            "sku": product.get("sku"),
            "parent_sku": product.get("parent_sku"),
            "product_name": product.get("product_name"),
            "category": product.get("category"),
            "sizes": product.get("sizes"),
            "price_usd": product.get("price_usd"),
            "in_stock": int(product.get("stock", 0)) > 0,
            "has_image": bool(product.get("image_url")),
            "size_skus": product.get("size_skus", {}),
            "variants": [
                {
                    "sku": variant.get("sku"),
                    "size": variant.get("size"),
                    "price_usd": variant.get("price_usd"),
                    "in_stock": bool(variant.get("in_stock")),
                }
                for variant in product.get("variants", [])
            ],
        })

    if not result_products:
        return {
            "found": False,
            "message": f"No products found matching '{args.get('product_query')}'.",
            "suggestion": "Try a broader search term.",
        }

    return {
        "found": True,
        "count": len(result_products),
        "products": result_products[:5],  # Limit to top 5 matches
    }


async def _tool_send_product_image(args: dict) -> dict:
    matches = _find_catalog_matches(product_query=args.get("product_query", ""))
    if not matches:
        return {
            "status": "error",
            "message": f"No product found matching '{args.get('product_query')}'.",
        }

    product_with_image = next((product for product in matches if product.get("image_url")), None)
    if not product_with_image:
        return {
            "status": "error",
            "message": "No image is available for that product in the catalog.",
        }

    return {
        "type": "product_image",
        "image_url": product_with_image["image_url"],
        "caption": args.get("caption", "").strip(),
        "product_name": product_with_image.get("product_name", ""),
        "sku": product_with_image.get("sku", ""),
    }


def _find_catalog_matches(product_query: str, size_filter: str | None = None) -> list[dict]:
    query = _normalize_catalog_text(product_query)
    if not query:
        return []

    query_terms = [
        term for term in query.split()
        if len(term) > 1 and term not in {"de", "la", "el", "los", "las", "un", "una", "del"}
    ]

    matches = []
    for product in get_cached_catalog():
        searchable = " ".join([
            str(product.get("sku", "")),
            str(product.get("parent_sku", "")),
            str(product.get("product_name", "")),
            str(product.get("category", "")),
            str(product.get("description", "")),
            str(product.get("size", "")),
        ])
        searchable = _normalize_catalog_text(searchable)

        if query not in searchable and (not query_terms or not all(term in searchable for term in query_terms)):
            continue

        if size_filter:
            available_sizes = get_product_sizes(product)
            if size_filter.upper() not in available_sizes:
                continue

        matches.append(product)

    return matches


def _normalize_catalog_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", (text or "").lower())
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"\s+", " ", ascii_text).strip()


def _clean_assistant_reply_text(text: str) -> str:
    cleaned = sanitize_customer_facing_text(text)
    cleaned = _strip_catalog_skus_from_text(cleaned)
    return cleaned.strip()


def _format_tool_result_for_model(tool_name: str, result: dict) -> str:
    return (
        "RESULTADO INTERNO DE HERRAMIENTA. NO lo muestres ni lo cites al cliente.\n"
        f"Herramienta: {tool_name}\n"
        "Usa estos datos solo para redactar una respuesta natural en español.\n"
        f"{json.dumps(result, ensure_ascii=False)}"
    )


def _strip_catalog_skus_from_text(text: str) -> str:
    cleaned = text or ""
    catalog = get_cached_catalog() or []
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


def _looks_like_payment_proof_message(message_text: str, vision_result: dict) -> bool:
    normalized_message = _normalize_text_for_moderation(message_text)
    if any(
        hint in normalized_message
        for hint in ("pago", "pague", "pagado", "comprobante", "captura", "capture", "transferencia", "zelle", "binance", "zinli")
    ):
        return True

    detected_method = str(vision_result.get("payment_method", "") or "").strip().lower()
    detected_amount = str(vision_result.get("amount", "") or "").strip()
    detected_status = str(vision_result.get("status", "") or "").strip().lower()
    recipient_identifier = str(vision_result.get("recipient_identifier", "") or "").strip()
    reference = str(vision_result.get("reference", "") or "").strip()

    return bool(
        detected_method not in {"", "unknown"}
        or detected_amount
        or detected_status in {"completed", "pending", "failed"}
        or recipient_identifier
        or reference
    )


async def _validate_payment_proof(customer_id: str, payment_methods: list[dict], vision_result: dict) -> dict:
    order = await orders.get_latest_open_order(customer_id)
    if not order:
        return {
            "status": "error",
            "message": (
                "No existe un pedido abierto para validar este comprobante. "
                "No confirmes el pago ni crees un pedido desde esta imagen."
            ),
        }

    if not vision_result.get("analyzed"):
        return {
            "status": "error",
            "message": "No se pudo analizar el comprobante de pago con suficiente claridad.",
        }

    amount = _safe_float(vision_result.get("amount"))
    expected_amount = round(float(order["total"]), 2)
    if amount is None:
        return {
            "status": "error",
            "message": "No se pudo leer el monto del comprobante. Solicita una captura más clara o revisión manual.",
        }
    if abs(amount - expected_amount) > 0.01:
        return {
            "status": "error",
            "message": (
                f"El comprobante no coincide con el monto esperado del pedido. "
                f"Esperado: ${expected_amount:.2f}. Detectado: ${amount:.2f}."
            ),
        }

    expected_method_name = str(order.get("payment_method", "") or "").strip()
    payment_method = _find_payment_method(payment_methods, expected_method_name)
    if not payment_method:
        return {
            "status": "error",
            "message": (
                "No se encontró la configuración del método de pago del pedido. "
                "Pasa este caso al encargado para revisión manual."
            ),
        }

    expected_kind = _classify_payment_method_name(expected_method_name)
    detected_kind = str(vision_result.get("payment_method", "") or "").strip().lower()
    if expected_kind and detected_kind not in {"", "unknown"} and detected_kind != expected_kind:
        return {
            "status": "error",
            "message": (
                f"El comprobante parece ser de un método distinto al pedido. "
                f"Pedido: {expected_method_name}. Detectado: {detected_kind}."
            ),
        }

    identifier_validation = _validate_payment_identifier_match(
        payment_information=payment_method.get("information", ""),
        vision_result=vision_result,
    )
    if not identifier_validation["ok"]:
        return {
            "status": "error",
            "message": identifier_validation["message"],
        }

    detected_status = str(vision_result.get("status", "") or "").strip().lower()
    if detected_status != "completed":
        return {
            "status": "error",
            "message": "El comprobante analizado no aparece como pago completado.",
        }

    return {
        "status": "ok",
        "order_id": order["id"],
        "validated_amount": expected_amount,
        "validated_payment_method": expected_method_name,
    }


def _find_payment_method(payment_methods: list[dict], method_name: str) -> dict | None:
    normalized_name = _normalize_catalog_text(method_name)
    for payment_method in payment_methods:
        if _normalize_catalog_text(payment_method.get("name", "")) == normalized_name:
            return payment_method
    return None


def _classify_payment_method_name(method_name: str) -> str | None:
    normalized = _normalize_catalog_text(method_name)
    if "zelle" in normalized:
        return "zelle"
    if "binance" in normalized:
        return "binance"
    if "zinli" in normalized:
        return "zinli"
    if any(token in normalized for token in ("bolivar", "transferencia", "banco", "pago movil")):
        return "bank_transfer"
    return None


def _validate_payment_identifier_match(payment_information: str, vision_result: dict) -> dict:
    info = (payment_information or "").strip()
    if not info:
        return {"ok": False, "message": "El método de pago no tiene datos configurados para validar el destinatario."}

    vision_text = _normalize_catalog_text(
        " ".join(
            str(vision_result.get(key, "") or "")
            for key in (
                "summary",
                "raw_response",
                "recipient_identifier",
                "recipient_name",
                "sender_name",
                "reference",
            )
        )
    )

    expected_emails = {
        email.lower()
        for email in re.findall(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", info, flags=re.IGNORECASE)
    }
    if expected_emails:
        if any(email in vision_text for email in expected_emails):
            return {"ok": True}
        return {
            "ok": False,
            "message": "El comprobante no muestra el correo o destinatario configurado para este método de pago.",
        }

    collapsed_numeric_info = re.sub(r"(?<=\d)[\s\-]+(?=\d)", "", info)
    expected_number_tokens = set(re.findall(r"\d{6,}", collapsed_numeric_info))
    if expected_number_tokens:
        vision_digits = re.sub(r"[^\d]", "", vision_text)
        if any(token in vision_digits for token in expected_number_tokens):
            return {"ok": True}
        return {
            "ok": False,
            "message": "El comprobante no coincide con el número o cuenta configurada para este método de pago.",
        }

    name_like_segments = [
        _normalize_catalog_text(segment)
        for segment in re.split(r"[\n,;|]+", info)
        if len(segment.strip()) >= 5 and not any(ch.isdigit() for ch in segment)
    ]
    name_like_segments = [
        segment
        for segment in name_like_segments
        if segment and segment not in {"nombre", "correo", "telefono", "instrucciones", "pago"}
    ]
    if name_like_segments and any(segment in vision_text for segment in name_like_segments):
        return {"ok": True}

    normalized_info = _normalize_catalog_text(info)
    if normalized_info and normalized_info in vision_text:
        return {"ok": True}

    return {
        "ok": False,
        "message": "El comprobante no coincide con los datos del método de pago configurado.",
    }


def _safe_float(value) -> float | None:
    text = str(value or "").strip().replace(",", ".")
    if not text:
        return None
    match = re.search(r"\d+(?:\.\d+)?", text)
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def _should_block_product_inquiry_escalation(reason: str, latest_user_message: str) -> bool:
    normalized_reason = _normalize_text_for_moderation(reason)
    normalized_message = _normalize_text_for_moderation(latest_user_message)

    if not normalized_reason:
        return False

    complaint_markers = (
        "reembolso", "refund", "devolucion", "demanda", "denuncia", "estafa",
        "molesto", "amenaza", "humano", "persona real", "supervisor", "gerente",
        "pago", "comprobante", "disputa", "cancelar pedido", "envio retrasado",
    )
    if any(marker in normalized_reason for marker in complaint_markers):
        return False

    product_reason_markers = (
        "catalog", "catalogo", "no aparece", "no esta", "no se encuentra",
        "no puedo confirmar", "no hay", "agotad", "sin stock", "sin inventario",
        "producto", "precio", "talla", "disponibil",
    )
    message_product_markers = (
        "tienes", "talla", "precio", "disponible", "hay", "busco", "quiero",
        "pant", "pijama", "conjunto", "set", "encaje", "splash", "victoria",
        "ropa interior", "underwear",
    )

    reason_is_product_related = any(marker in normalized_reason for marker in product_reason_markers)
    message_is_product_related = any(marker in normalized_message for marker in message_product_markers)

    return reason_is_product_related and message_is_product_related


async def _sync_kommo_escalation_if_needed(
    *,
    customer_id: str,
    reason: str,
    urgency: str,
    conversation_summary: str,
    lead_id: str | None = None,
) -> None:
    if get_config().channel_backend != "kommo":
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
