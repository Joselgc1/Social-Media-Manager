"""
Provider-neutral tool definitions and metadata.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ToolSpec:
    """Provider-neutral description of an LLM-callable tool."""

    name: str
    schema: dict[str, Any]
    category: str
    creates_side_effects: bool
    safe_for_shadow: bool
    description: str | None = None


TOOL_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="check_inventory",
        category="catalog",
        creates_side_effects=False,
        safe_for_shadow=True,
        description="Searches the cached product catalog for availability.",
        schema={
            "name": "check_inventory",
            "description": (
                "Check if a specific product is available and in what presentations or options. "
                "Call this BEFORE confirming any product's availability to the customer. "
                "Search by product name, brand, category, or keyword."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "product_query": {
                        "type": "string",
                        "description": "Product name, brand, keyword, or category the customer is asking about",
                    },
                    "size": {
                        "type": "string",
                        "pattern": "\\S",
                        "description": (
                            "Specific sellable presentation/option if the customer mentioned one, "
                            "for example M, 100 ml, 38, or 256 GB"
                        ),
                    },
                },
                "required": ["product_query"],
            },
        },
    ),
    ToolSpec(
        name="tag_customer",
        category="customers",
        creates_side_effects=True,
        safe_for_shadow=False,
        description="Adds segmentation tags to the current customer.",
        schema={
            "name": "tag_customer",
            "description": (
                "Add one or more tags to the current customer's profile for segmentation. "
                "Call this whenever you learn something useful about the customer: "
                "their interests, product preferences, city, or buying behavior. "
                "This is a silent action; do NOT mention tagging to the customer."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Tags to add. Formats include 'interested:<category-or-product>', "
                            "'size:S' when a clothing size is useful, 'city:caracas', "
                            "'payment:<payment_method_name>', 'repeat_buyer', 'vip', or 'new_lead'."
                        ),
                    },
                },
                "required": ["tags"],
            },
        },
    ),
    ToolSpec(
        name="create_order",
        category="orders",
        creates_side_effects=True,
        safe_for_shadow=False,
        description="Creates or reuses a pending customer order.",
        schema={
            "name": "create_order",
            "description": (
                "Create a new order when the customer CONFIRMS they want to purchase. "
                "Only call this AFTER you have collected and confirmed the specific product(s), "
                "the presentation/option when the product has more than one, quantity, city, and either the "
                "home-delivery zone/address or the MRW/Zoom pickup agency, plus payment method. "
                "The chosen payment method is the final checkout step. As soon as the customer chooses it "
                "and the other checkout information is already complete, create the order immediately BEFORE "
                "sending the payment details. The new order should remain pending until a payment screenshot is validated. "
                "If the order qualifies for the store's configured automatic discount, the backend applies it automatically. "
                "Do NOT modify item unit prices to simulate that discount. Do NOT call this from a payment-proof "
                "message or screenshot. The order must already exist before payment is validated."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "items": {
                        "type": "array",
                        "minItems": 1,
                        "items": {
                            "type": "object",
                            "properties": {
                                "product_name": {"type": "string", "pattern": "\\S"},
                                "sku": {"type": "string", "pattern": "\\S"},
                                "size": {
                                    "type": "string",
                                    "pattern": "\\S",
                                    "description": (
                                        "Selected presentation/option when applicable, such as M, 100 ml, 38, or 256 GB"
                                    ),
                                },
                                "quantity": {"type": "integer", "minimum": 1},
                                "unit_price": {"type": "number", "minimum": 0},
                            },
                            "required": ["product_name", "sku", "quantity", "unit_price"],
                        },
                        "description": "List of items the customer wants to buy",
                    },
                    "payment_method": {
                        "type": "string",
                        "pattern": "\\S",
                        "description": "The customer's chosen payment method name, exactly as configured by the store",
                    },
                    "shipping_city": {
                        "type": "string",
                        "description": "Customer city. The backend decides whether it is home delivery or agency pickup.",
                    },
                    "shipping_address": {
                        "type": "string",
                        "description": "Exact home-delivery address. Required only for a configured Metro Valencia home-delivery zone.",
                    },
                    "shipping_method": {
                        "type": "string",
                        "enum": ["mrw", "zoom"],
                        "description": "Preferred courier for agency pickup outside Metro Valencia",
                    },
                    "shipping_zone": {
                        "type": "string",
                        "description": "Configured zone for Metro Valencia home delivery",
                    },
                    "pickup_agency": {
                        "type": "string",
                        "description": "MRW or Zoom agency where the customer will pick up the order outside Metro Valencia",
                    },
                },
                "required": ["items", "payment_method", "shipping_city"],
            },
        },
    ),
    ToolSpec(
        name="update_payment_status",
        category="payments",
        creates_side_effects=True,
        safe_for_shadow=False,
        description="Updates an existing order after payment proof validation.",
        schema={
            "name": "update_payment_status",
            "description": (
                "Call when the customer sends a payment screenshot for an EXISTING order. "
                "Use this only after the order was already created and only when the proof "
                "matches the expected amount and destination details."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "confirmation_note": {
                        "type": "string",
                        "description": "Brief note about the payment (e.g., 'Zelle screenshot received')",
                    },
                },
                "required": ["confirmation_note"],
            },
        },
    ),
    ToolSpec(
        name="escalate_to_human",
        category="messaging",
        creates_side_effects=True,
        safe_for_shadow=False,
        description="Escalates the conversation to the store owner.",
        schema={
            "name": "escalate_to_human",
            "description": (
                "Transfer the conversation to the store owner. Call this for: complaints, refund requests, "
                "questions you cannot answer, angry or insulting customers, threats, payment disputes, "
                "or when the customer explicitly asks to speak with a human."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {"type": "string", "description": "Brief summary of why escalation is needed"},
                    "urgency": {
                        "type": "string",
                        "enum": ["low", "medium", "high"],
                        "description": "How urgently the owner should respond",
                    },
                },
                "required": ["reason"],
            },
        },
    ),
    ToolSpec(
        name="send_catalog_pdf",
        category="messaging",
        creates_side_effects=True,
        safe_for_shadow=False,
        description="Signals that the catalog PDF should be sent to the customer.",
        schema={
            "name": "send_catalog_pdf",
            "description": (
                "Send the product catalog as a PDF document to the customer on WhatsApp. "
                "Use this when the customer asks to see all products, wants a catalog, "
                "or asks 'que tienen?' / 'muestrame todo' / 'catalogo'. Only works on WhatsApp."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "caption": {"type": "string", "description": "Short caption to send with the PDF (1 sentence max)"},
                },
                "required": ["caption"],
            },
        },
    ),
    ToolSpec(
        name="send_whatsapp_handoff",
        category="messaging",
        creates_side_effects=False,
        safe_for_shadow=True,
        description="Builds a trusted WhatsApp handoff for transactional Instagram requests.",
        schema={
            "name": "send_whatsapp_handoff",
            "description": (
                "Create the WhatsApp handoff when an Instagram customer clearly wants to buy, place or confirm "
                "an order, pay, provide delivery details, continue checkout, or receive the PDF catalog. "
                "Do not use for browsing, prices, presentations/options, availability, recommendations, comparisons, "
                "or photos. Pass only customer-visible product context already established in the conversation; "
                "never pass SKUs, addresses, payment credentials, or technical metadata."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "handoff_reason": {
                        "type": "string",
                        "enum": ["purchase", "payment", "delivery", "checkout", "catalog_pdf"],
                        "description": "Why the customer needs to continue through WhatsApp.",
                    },
                    "product_name": {
                        "type": "string",
                        "description": "Customer-visible product name already identified in the conversation.",
                    },
                    "size": {
                        "type": "string",
                        "pattern": "\\S",
                        "description": "Confirmed product presentation/option, if already known.",
                    },
                    "quantity": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 99,
                        "description": "Confirmed quantity, if already known.",
                    },
                },
                "required": ["handoff_reason"],
            },
        },
    ),
    ToolSpec(
        name="send_product_image",
        category="messaging",
        creates_side_effects=True,
        safe_for_shadow=False,
        description="Signals that a matched product image should be sent.",
        schema={
            "name": "send_product_image",
            "description": (
                "Send the image of a specific product to the customer when they ask to see it. "
                "Use this only for a specific product, not for the whole catalog. "
                "Works on WhatsApp and Instagram only if the matched product has a usable image."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "product_query": {
                        "type": "string",
                        "description": "Specific product name, SKU, brand, or clear keyword for the product image to send",
                    },
                    "caption": {"type": "string", "description": "Optional short caption for the image"},
                },
                "required": ["product_query"],
            },
        },
    ),
    ToolSpec(
        name="send_interactive_buttons",
        category="messaging",
        creates_side_effects=True,
        safe_for_shadow=False,
        description="Signals that WhatsApp buttons or Instagram DM Quick Replies should be sent.",
        schema={
            "name": "send_interactive_buttons",
            "description": (
                "Send 1-3 clickable choices as WhatsApp buttons or Instagram DM Quick Replies. "
                "Never use this for public Instagram comments. Use it ONLY when the customer has not already "
                "answered in plain text. Do NOT use it to re-confirm a choice the customer already made."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "body_text": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 1000,
                        "description": "The message text shown above the choices (max 1000 UTF-8 bytes)",
                    },
                    "buttons": {
                        "type": "array",
                        "items": {"type": "string", "minLength": 1, "maxLength": 20},
                        "minItems": 1,
                        "maxItems": 3,
                        "uniqueItems": True,
                        "description": "One to three unique choice labels (max 20 characters each)",
                    },
                },
                "required": ["body_text", "buttons"],
            },
        },
    ),
    ToolSpec(
        name="request_agent_handoff",
        category="routing",
        creates_side_effects=False,
        safe_for_shadow=True,
        description="Requests a structured internal handoff to another specialist agent.",
        schema={
            "name": "request_agent_handoff",
            "description": (
                "Request an internal handoff to another agent. Use this when the customer clearly wants to buy "
                "and the checkout agent should collect the remaining checkout fields. This is internal only; "
                "do not show raw handoff metadata to the customer."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "target_agent": {
                        "type": "string",
                        "enum": ["checkout"],
                        "description": "Specialist agent that should continue the workflow",
                    },
                    "intent": {"type": "string", "description": "Short intent label such as purchase_intent"},
                    "reason": {"type": "string", "description": "Brief internal reason for the handoff"},
                },
                "required": ["target_agent", "intent"],
            },
        },
    ),
    ToolSpec(
        name="update_checkout_draft",
        category="checkout",
        creates_side_effects=True,
        safe_for_shadow=False,
        description="Validates and persists partial checkout fields without creating an order.",
        schema={
            "name": "update_checkout_draft",
            "description": (
                "Update the server-side checkout draft with only fields the customer has provided. "
                "Do not include prices. This validates products and any required presentation/option against "
                "the catalog and returns missing fields."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "items": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "product_query": {"type": "string"},
                                "sku": {"type": "string"},
                                "size": {
                                    "type": "string",
                                    "description": "Presentation/option when the selected product has more than one",
                                },
                                "quantity": {"type": "integer", "minimum": 1},
                            },
                        },
                        "description": "Products the customer selected. Never include unit_price.",
                    },
                    "product_query": {"type": "string"},
                    "sku": {"type": "string"},
                    "size": {
                        "type": "string",
                        "description": "Presentation/option when the selected product has more than one",
                    },
                    "quantity": {"type": "integer", "minimum": 1},
                    "shipping_method": {"type": "string", "enum": ["mrw", "zoom"]},
                    "shipping_city": {"type": "string"},
                    "shipping_address": {"type": "string"},
                    "shipping_zone": {"type": "string"},
                    "pickup_agency": {"type": "string"},
                    "payment_method": {"type": "string"},
                    "use_saved_address": {
                        "type": "boolean",
                        "description": "Set true only if the customer explicitly confirmed using the saved address",
                    },
                },
            },
        },
    ),
    ToolSpec(
        name="finalize_checkout",
        category="checkout",
        creates_side_effects=True,
        safe_for_shadow=False,
        description="Creates an order from the validated server-side checkout draft.",
        schema={
            "name": "finalize_checkout",
            "description": (
                "Finalize checkout only after the server-side draft has all required fields. The backend resolves prices, "
                "rechecks stock, calculates discounts, creates/reuses the order, and returns payment instructions."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "start_new_order": {
                        "type": "boolean",
                        "description": (
                            "True only after the customer explicitly confirms starting a separate purchase despite unpaid orders. "
                            "The backend allows up to three unpaid orders and rejects a fourth."
                        ),
                    },
                },
            },
        },
    ),
    ToolSpec(
        name="cancel_checkout",
        category="checkout",
        creates_side_effects=True,
        safe_for_shadow=False,
        description="Clears the active checkout draft and returns the session to idle.",
        schema={
            "name": "cancel_checkout",
            "description": "Cancel the active checkout flow when the customer clearly asks to cancel or stop the purchase.",
            "parameters": {"type": "object", "properties": {"reason": {"type": "string"}}},
        },
    ),
    ToolSpec(
        name="get_customer_profile",
        category="support",
        creates_side_effects=False,
        safe_for_shadow=True,
        description="Reads safe profile context for the current customer only.",
        schema={
            "name": "get_customer_profile",
            "description": (
                "Read safe profile context for the current customer only. "
                "Do not pass or request a customer_id; the backend scopes this automatically."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    ),
    ToolSpec(
        name="get_customer_order_status",
        category="support",
        creates_side_effects=False,
        safe_for_shadow=True,
        description="Reads sanitized recent-order status for the current customer only.",
        schema={
            "name": "get_customer_order_status",
            "description": (
                "Read sanitized recent-order status for the current customer only. "
                "Use for order status, shipping, delivery, tracking, and payment-state questions. "
                "Do not pass or request a customer_id; the backend scopes this automatically."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 5,
                        "description": "Number of recent orders to read, capped by the backend.",
                    },
                },
            },
        },
    ),
)
