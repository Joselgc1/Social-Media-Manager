"""
Order-related tool handlers.
"""

from app import db
from app.admin.notify import notify_new_order
from app.ai.checkout import service as checkout_service
from app.ai.tools.context import ToolExecutionContext
from app.crm import customers, orders
from app.payment_methods import payment_method_tag_value


async def create_order(args: dict, context: ToolExecutionContext) -> dict:
    """Create or reuse a pending customer order and apply related side effects."""
    customer = context.customer
    customer_id = customer["id"]
    delivery = await checkout_service.validate_legacy_delivery(args)
    if delivery["status"] != "ok":
        return delivery
    quote = delivery["quote"]
    try:
        order = await orders.create_order(
            customer_id=customer_id,
            items=args.get("items", []),
            payment_method=args.get("payment_method", ""),
            shipping_city=quote.get("shipping_city") or args.get("shipping_city"),
            shipping_address=args.get("shipping_address") if quote.get("fulfillment_type") == "home_delivery" else None,
            shipping_method=quote.get("shipping_method"),
            fulfillment_type=quote.get("fulfillment_type"),
            shipping_zone=quote.get("shipping_zone"),
            pickup_agency=args.get("pickup_agency") if quote.get("fulfillment_type") == "courier_agency_pickup" else None,
            shipping_fee=quote.get("shipping_fee", 0),
            shipping_currency=quote.get("shipping_currency", "USD"),
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
            delivery_summary=checkout_service._delivery_summary(order),
        )

    await db.execute(
        """UPDATE customers
           SET last_shipping_address = :addr,
               last_shipping_city = :city,
               last_shipping_method = :method,
               last_fulfillment_type = :fulfillment_type,
               last_shipping_zone = :zone,
               last_pickup_agency = :pickup_agency
           WHERE id = :id""",
        {
            "addr": args.get("shipping_address") if quote.get("fulfillment_type") == "home_delivery" else None,
            "city": quote.get("shipping_city") or args.get("shipping_city", ""),
            "method": quote.get("shipping_method") or "",
            "fulfillment_type": quote.get("fulfillment_type"),
            "zone": quote.get("shipping_zone"),
            "pickup_agency": args.get("pickup_agency") if quote.get("fulfillment_type") == "courier_agency_pickup" else None,
            "id": customer_id,
        },
    )

    payment_method = args.get("payment_method", "")
    await customers.add_tags(customer_id, [f"payment:{payment_method_tag_value(payment_method)}"])

    return order
