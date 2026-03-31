"""
AI Engine: the central orchestrator.

Receives an incoming message + customer context, builds the prompt,
calls the active LLM provider, executes any tool calls, and returns
the final text response.
"""

import json
import time
import logging
from app import db
from app import analytics
from app.ai.providers import get_provider, AVAILABLE_MODELS, list_providers as _list_providers
from app.ai.providers.base import LLMResponse
from app.ai.functions import TOOLS
from app.ai.prompts import build_system_prompt, format_catalog_as_markdown
from app.ai.vision import analyze_payment_screenshot
from app.crm import customers, conversations, orders
from app.admin.notify import notify_escalation, notify_incoming_message, notify_new_order
from app.catalog.sheets import get_cached_catalog
from app.catalog.pdf_generator import PDF_PATH, generate_catalog_pdf

logger = logging.getLogger(__name__)

# Maximum number of tool-call rounds per message (prevent infinite loops)
MAX_TOOL_ROUNDS = 6


async def generate_response(
    channel: str,
    sender_id: str,
    message_text: str,
    media_url: str | None = None,
) -> dict:
    """
    Full pipeline: message in -> AI response out.

    Returns
    -------
    dict with keys:
        "text": str             - The reply text to send back
        "interactive": dict|None - If the AI wants to send buttons (WhatsApp only)
        "customer_id": str      - For reference
    """
    # ── 1. Load settings ─────────────────────────────────────
    settings = await db.get_settings()

    # ── 2. Get or create customer ────────────────────────────
    customer = await customers.get_or_create_customer(
        channel=channel,
        platform_id=sender_id,
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
            "customer_id": customer["id"],
            "escalated": is_escalated,
            "paused": not ai_enabled,
        }

    # ── 3. Load conversation history ─────────────────────────
    max_history = settings.get("max_conversation_history", 20)
    history = await conversations.get_history(customer["id"], limit=max_history)

    # Append the new message
    history.append({"role": "user", "content": message_text})

    # ── 4. Build system prompt with live catalog ─────────────
    catalog = get_cached_catalog()
    catalog_md = format_catalog_as_markdown(catalog)
    system_prompt = build_system_prompt(
        catalog_markdown=catalog_md,
        store_name=settings.get("store_name", "Tu Tienda VS"),
        channel=channel,
        customer=customer,
    )

    # ── 5. Call the LLM ──────────────────────────────────────
    ab_mode = settings.get("ab_test_enabled", False)
    was_fallback = False

    if ab_mode and customer.get("ab_provider"):
        # A/B test: use the customer's assigned provider
        provider_name = customer["ab_provider"]
        # Find the default model for this provider
        model = next(
            (m["id"] for m in AVAILABLE_MODELS.get(provider_name, []) if m.get("default")),
            settings.get("llm_model", "gpt-5.4-nano"),
        )
    elif ab_mode and not customer.get("ab_provider"):
        # New customer in A/B mode: assign a group
        provider_name = await analytics.assign_ab_group(customer["id"])
        model = next(
            (m["id"] for m in AVAILABLE_MODELS.get(provider_name, []) if m.get("default")),
            settings.get("llm_model", "gpt-5.4-nano"),
        )
    else:
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
    if media_url:
        vision_result = await analyze_payment_screenshot(
            media_id=media_url if channel == "whatsapp" else None,
            media_url=media_url if channel == "instagram" else None,
            channel=channel,
        )
        if vision_result.get("analyzed"):
            # Inject vision analysis into the message text so the AI has context
            summary = vision_result.get("summary", "Imagen analizada")
            message_text = f"{message_text}\n\n[Análisis de imagen: {summary}]"
            # Update the last message in history
            history[-1] = {"role": "user", "content": message_text}

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
    tool_log = []
    rounds = 0

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

        logger.info(f"Tool call: {name}({json.dumps(args, ensure_ascii=False)})")
        result = await _execute_tool(name, args, customer, channel)
        tool_log.append({"name": name, "args": args, "result": result})

        # Check if the tool wants to send interactive buttons
        if name == "send_interactive_buttons" and channel == "whatsapp":
            interactive_payload = result

        # Check if the tool wants to send the catalog PDF
        if name == "send_catalog_pdf" and channel == "whatsapp":
            catalog_pdf_payload = result

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
            tool_result=json.dumps(result, ensure_ascii=False),
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

    # When send_interactive_buttons was used, the model sometimes returns
    # text=None (treating buttons as the full response). Use body_text in that
    # case so the customer still receives a readable message.
    if not response.text and interactive_payload:
        reply_text = interactive_payload.get("body_text", "")
    else:
        reply_text = response.text or "Lo siento, no pude generar una respuesta. ¿Puedes repetir tu pregunta?"

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
        "customer_id": customer["id"],
        "escalated": False,
    }


# ── Tool execution ───────────────────────────────────────────

async def _execute_tool(name: str, args: dict, customer: dict, channel: str) -> dict:
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
        # Notify the owner
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
        await customers.add_tags(customer_id, [f"payment:{args.get('payment_method', '')}"])
        if customer.get("total_orders", 0) >= 2:
            await customers.add_tags(customer_id, ["repeat_buyer"])
        if customer.get("total_orders", 0) >= 3 or customer.get("total_spent", 0) >= 100:
            await customers.add_tags(customer_id, ["vip"])

        return order

    elif name == "update_payment_status":
        result = await orders.update_payment_status(
            customer_id=customer_id,
            status="proof_received",
            note=args.get("confirmation_note"),
        )
        return result or {"status": "error", "message": "No pending order found for this customer."}

    elif name == "escalate_to_human":
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

    else:
        logger.warning(f"Unknown tool: {name}")
        return {"status": "error", "message": f"Unknown tool: {name}"}


async def _tool_check_inventory(args: dict) -> dict:
    """Search the cached product catalog for matching products."""
    query = args.get("product_query", "").lower()
    size_filter = args.get("size")
    catalog = get_cached_catalog()

    matches = []
    for product in catalog:
        name = product.get("product_name", "").lower()
        category = product.get("category", "").lower()
        description = product.get("description", "").lower()

        # Simple keyword match (good enough for <200 products)
        if query in name or query in category or query in description:
            if size_filter:
                available_sizes = [s.strip().upper() for s in product.get("sizes", "").split(",")]
                if size_filter.upper() not in available_sizes:
                    continue
            matches.append({
                "sku": product.get("sku"),
                "product_name": product.get("product_name"),
                "category": product.get("category"),
                "sizes": product.get("sizes"),
                "price_usd": product.get("price_usd"),
                "in_stock": int(product.get("stock", 0)) > 0,
            })

    if not matches:
        return {
            "found": False,
            "message": f"No products found matching '{args.get('product_query')}'.",
            "suggestion": "Try a broader search term.",
        }

    return {
        "found": True,
        "count": len(matches),
        "products": matches[:5],  # Limit to top 5 matches
    }


# Usage logging is now handled by app.analytics.log_response()
