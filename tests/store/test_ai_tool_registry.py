import pytest
from app.ai.functions import TOOLS
from app.ai.tools.definitions import ToolSpec
from app.ai.tools.registry import _build_registry, get_tool_schemas, get_tool_spec, get_tool_specs

EXPECTED_TOOL_ORDER = [
    "check_inventory",
    "tag_customer",
    "create_order",
    "update_payment_status",
    "escalate_to_human",
    "send_catalog_pdf",
    "send_product_image",
    "send_interactive_buttons",
    "request_agent_handoff",
    "update_checkout_draft",
    "finalize_checkout",
    "cancel_checkout",
    "get_customer_profile",
    "get_customer_order_status",
]


def test_registry_completeness_and_unique_names():
    specs = get_tool_specs()
    names = [spec.name for spec in specs]

    assert names == EXPECTED_TOOL_ORDER
    assert len(names) == len(set(names))
    assert [tool["name"] for tool in TOOLS] == EXPECTED_TOOL_ORDER


def test_full_ordered_schema_retrieval_matches_compatibility_export():
    schemas = get_tool_schemas()

    assert [schema["name"] for schema in schemas] == EXPECTED_TOOL_ORDER
    assert schemas == TOOLS


def test_subset_retrieval_preserves_requested_order():
    names = ["create_order", "check_inventory"]

    assert [spec.name for spec in get_tool_specs(names)] == names
    assert [schema["name"] for schema in get_tool_schemas(names)] == names


def test_unknown_tool_fails_clearly():
    with pytest.raises(KeyError, match="Unknown tool: missing_tool"):
        get_tool_spec("missing_tool")


def test_tool_schemas_keep_current_contract_shape():
    for schema in get_tool_schemas():
        assert set(schema) == {"name", "description", "parameters"}
        assert schema["parameters"]["type"] == "object"
        properties = schema["parameters"].get("properties", {})
        for required_key in schema["parameters"].get("required", []):
            assert required_key in properties


def test_metadata_marks_side_effecting_tools():
    check_inventory = get_tool_spec("check_inventory")
    create_order = get_tool_spec("create_order")
    send_buttons = get_tool_spec("send_interactive_buttons")
    handoff = get_tool_spec("request_agent_handoff")
    order_status = get_tool_spec("get_customer_order_status")

    assert check_inventory.category == "catalog"
    assert check_inventory.creates_side_effects is False
    assert check_inventory.safe_for_shadow is True

    assert create_order.category == "orders"
    assert create_order.creates_side_effects is True
    assert create_order.safe_for_shadow is False

    assert send_buttons.category == "messaging"
    assert send_buttons.creates_side_effects is True

    assert handoff.category == "routing"
    assert handoff.creates_side_effects is False
    assert handoff.safe_for_shadow is True

    assert order_status.category == "support"
    assert order_status.creates_side_effects is False
    assert order_status.safe_for_shadow is True


def test_registry_returns_defensive_schema_copies():
    schemas = get_tool_schemas()
    schemas[0]["parameters"]["properties"]["product_query"]["description"] = "mutated"
    spec = get_tool_spec("check_inventory")
    spec.schema["parameters"]["properties"]["product_query"]["description"] = "mutated again"

    fresh_schema = get_tool_schemas()[0]
    assert fresh_schema["parameters"]["properties"]["product_query"]["description"] != "mutated"
    assert fresh_schema["parameters"]["properties"]["product_query"]["description"] != "mutated again"


def test_duplicate_registration_is_rejected():
    spec = get_tool_spec("check_inventory")
    duplicate = ToolSpec(
        name=spec.name,
        schema=spec.schema,
        category=spec.category,
        creates_side_effects=spec.creates_side_effects,
        safe_for_shadow=spec.safe_for_shadow,
        description=spec.description,
    )

    with pytest.raises(ValueError, match="Duplicate tool registration"):
        _build_registry((spec, duplicate))
