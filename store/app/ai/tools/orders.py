"""
Order-related tool handlers.
"""

from app import db
from app.admin.notify import notify_new_order
from app.ai.tools.context import ToolExecutionContext
from app.crm import customers, orders
from app.payment_methods import payment_method_tag_value


async def create_order(args: dict, context: ToolExecutionContext) -> dict:
    """Create or reuse a pending customer order and apply related side effects."""
    customer = context.customer
    customer_id = customer["id"]
    try:
        order = await orders.create_order(
            customer_id=customer_id,
            items=args.get("items", []),
            payment_method=args.get("payment_method", ""),
            shipping_city=args.get("shipping_city"),
            shipping_address=args.get("shipping_address"),
            shipping_method=args.get("shipping_method"),
        )
    except ValueError as exc:
        return {"status": "error", "message": str(exc)}
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

    payment_method = args.get("payment_method", "")
    await customers.add_tags(customer_id, [f"payment:{payment_method_tag_value(payment_method)}"])

    return order
