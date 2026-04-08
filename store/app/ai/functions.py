"""
Tool definitions for the AI sales assistant.
Written in a provider-agnostic format (JSON Schema).
Each provider's _convert_tools() translates these into its native format.
"""

TOOLS = [
    {
        "name": "check_inventory",
        "description": (
            "Check if a specific product is available and in what sizes. "
            "Call this BEFORE confirming any product's availability to the customer. "
            "Search by product name, category, or keyword."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "product_query": {
                    "type": "string",
                    "description": "Product name, keyword, or category the customer is asking about",
                },
                "size": {
                    "type": "string",
                    "description": "Specific size to check, if the customer mentioned one",
                    "enum": ["XS", "S", "M", "L", "XL"],
                },
            },
            "required": ["product_query"],
        },
    },
    {
        "name": "tag_customer",
        "description": (
            "Add one or more tags to the current customer's profile for segmentation. "
            "Call this whenever you learn something useful about the customer: "
            "their interests, size, city, or buying behavior. "
            "This is a silent action; do NOT mention tagging to the customer."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Tags to add. Formats: "
                        "'interested:pajamas', 'interested:underwear', 'interested:sets', "
                        "'size:S' through 'size:XL', "
                        "'city:caracas', 'city:maracaibo', etc., "
                        "'payment:zelle', 'payment:binance', 'payment:zinli', 'payment:bolivares', "
                        "'repeat_buyer', 'vip', 'new_lead'"
                    ),
                },
            },
            "required": ["tags"],
        },
    },
    {
        "name": "create_order",
        "description": (
            "Create a new order when the customer CONFIRMS they want to purchase. "
            "Only call this AFTER you have collected and confirmed ALL of these: "
            "the specific product(s), size(s), quantity, shipping method, "
            "full shipping address, and payment method."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "product_name": {"type": "string"},
                            "sku": {"type": "string"},
                            "size": {"type": "string"},
                            "quantity": {"type": "integer", "minimum": 1},
                            "unit_price": {"type": "number"},
                        },
                        "required": ["product_name", "sku", "size", "quantity", "unit_price"],
                    },
                    "description": "List of items the customer wants to buy",
                },
                "payment_method": {
                    "type": "string",
                    "enum": ["zelle", "binance", "zinli", "bolivares"],
                    "description": "The customer's chosen payment method",
                },
                "shipping_city": {
                    "type": "string",
                    "description": "City for delivery",
                },
                "shipping_address": {
                    "type": "string",
                    "description": "Full delivery address (street, city, state, ZIP/postal code)",
                },
                "shipping_method": {
                    "type": "string",
                    "enum": ["mrw", "zoom"],
                    "description": "Preferred shipping courier",
                },
            },
            "required": ["items", "payment_method", "shipping_method", "shipping_address"],
        },
    },
    {
        "name": "update_payment_status",
        "description": (
            "Call when the customer sends a payment screenshot or confirms payment. "
            "Marks the most recent pending order as 'proof_received'."
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
    {
        "name": "escalate_to_human",
        "description": (
            "Transfer the conversation to the store owner. Call this for: "
            "complaints, refund requests, questions you cannot answer, "
            "angry or insulting customers, threats, payment disputes, or when the customer "
            "explicitly asks to speak with a human."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "Brief summary of why escalation is needed",
                },
                "urgency": {
                    "type": "string",
                    "enum": ["low", "medium", "high"],
                    "description": "How urgently the owner should respond",
                },
            },
            "required": ["reason"],
        },
    },
    {
        "name": "send_catalog_pdf",
        "description": (
            "Send the product catalog as a PDF document to the customer on WhatsApp. "
            "Use this when the customer asks to see all products, wants a catalog, "
            "or asks 'que tienen?' / 'muestrame todo' / 'catalogo'. "
            "Only works on WhatsApp."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "caption": {
                    "type": "string",
                    "description": "Short caption to send with the PDF (1 sentence max)",
                },
            },
            "required": ["caption"],
        },
    },
    {
        "name": "send_interactive_buttons",
        "description": (
            "Send a message with clickable reply buttons to the customer. "
            "Only works on WhatsApp. Use for presenting 2-3 clear choices "
            "like payment methods or shipping options ONLY when the customer "
            "has not already answered in plain text. Do NOT use buttons to "
            "re-confirm a choice the customer already made."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "body_text": {
                    "type": "string",
                    "description": "The message text shown above the buttons",
                },
                "buttons": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": 3,
                    "description": "Button labels (max 20 chars each)",
                },
            },
            "required": ["body_text", "buttons"],
        },
    },
]
