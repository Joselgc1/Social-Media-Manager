from app.ai.checkout import service
from app.crm import orders, sessions
from app.shipping import normalize_shipping_policy, resolve_delivery_quote


def _policy() -> dict:
    return {
        "currency": "USD",
        "home_delivery_cities": [
            {"name": "Valencia", "aliases": ["Valencia, Carabobo"]},
            {"name": "Naguanagua", "aliases": []},
        ],
        "home_delivery_zones": [
            {"city": "Valencia", "name": "El Viñedo", "aliases": ["El Vinedo"], "fee_usd": 4.5},
        ],
        "courier_destination_rates": [
            {"city": "Caracas", "aliases": ["Distrito Capital"], "mrw_fee_usd": 6.0, "zoom_fee_usd": 7.25},
        ],
    }


def test_home_delivery_quote_uses_configured_zone_and_alias():
    quote = resolve_delivery_quote(_policy(), city="Valencia, Carabobo", delivery_zone="El Vinedo")

    assert quote == {
        "status": "quoted",
        "fulfillment_type": "home_delivery",
        "shipping_city": "Valencia",
        "shipping_zone": "El Viñedo",
        "shipping_method": None,
        "shipping_fee": 4.5,
        "shipping_currency": "USD",
        "message": "Tarifa de entrega a domicilio: $4.50 USD.",
    }


def test_courier_pickup_quote_uses_city_and_courier_rate():
    quote = resolve_delivery_quote(_policy(), city="Distrito Capital", shipping_method="zoom")

    assert quote["status"] == "quoted"
    assert quote["fulfillment_type"] == "courier_agency_pickup"
    assert quote["shipping_city"] == "Caracas"
    assert quote["shipping_method"] == "zoom"
    assert quote["shipping_fee"] == 7.25


def test_unconfigured_city_cannot_receive_a_quote():
    quote = resolve_delivery_quote(_policy(), city="Barquisimeto", shipping_method="mrw")

    assert quote["status"] == "rate_unavailable"


def test_shipping_policy_rejects_zone_outside_home_delivery_city():
    policy = _policy()
    policy["home_delivery_zones"][0]["city"] = "Caracas"

    try:
        normalize_shipping_policy(policy)
    except ValueError as error:
        assert "debe pertenecer" in str(error)
    else:
        raise AssertionError("Expected invalid home-delivery zone to be rejected")


def test_home_delivery_requires_zone_and_address_but_not_courier():
    draft = sessions.CheckoutDraft.model_validate({
        "items": [{"product_query": "Pijama", "size": "M", "quantity": 1}],
        "shipping_city": "Valencia",
        "fulfillment_type": "home_delivery",
        "payment_method": "Zelle",
    })
    quote = resolve_delivery_quote(_policy(), city=draft.shipping_city)

    assert service._missing_fields(draft, quote) == ["shipping_zone", "shipping_address"]


def test_order_pricing_keeps_delivery_separate_from_product_discount():
    hydrated = orders._hydrate_order_pricing({
        "items": [{"unit_price": 50, "quantity": 2}],
        "total": 94,
        "merchandise_total": 90,
        "shipping_fee": 4,
        "shipping_currency": "USD",
    })

    assert hydrated["subtotal"] == 100
    assert hydrated["discount_amount"] == 10
    assert hydrated["merchandise_total"] == 90
    assert hydrated["shipping_fee"] == 4
    assert hydrated["total"] == 94
