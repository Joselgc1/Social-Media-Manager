"""
Registry for provider-neutral LLM tool specifications.
"""

from __future__ import annotations

from collections.abc import Collection
from copy import deepcopy

from app.ai.tools.definitions import TOOL_SPECS, ToolSpec


def _build_registry(specs: tuple[ToolSpec, ...]) -> dict[str, ToolSpec]:
    registry: dict[str, ToolSpec] = {}
    for spec in specs:
        if spec.name in registry:
            raise ValueError(f"Duplicate tool registration: {spec.name}")
        if spec.schema.get("name") != spec.name:
            raise ValueError(f"Tool schema name mismatch for: {spec.name}")
        registry[spec.name] = spec
    return registry


_REGISTRY = _build_registry(TOOL_SPECS)


def _copy_spec(spec: ToolSpec) -> ToolSpec:
    return ToolSpec(
        name=spec.name,
        schema=deepcopy(spec.schema),
        category=spec.category,
        creates_side_effects=spec.creates_side_effects,
        safe_for_shadow=spec.safe_for_shadow,
        description=spec.description,
    )


def get_tool_spec(name: str) -> ToolSpec:
    """Return a defensive copy of a registered tool spec by name."""
    try:
        return _copy_spec(_REGISTRY[name])
    except KeyError as exc:
        raise KeyError(f"Unknown tool: {name}") from exc


def get_tool_specs(names: Collection[str] | None = None) -> list[ToolSpec]:
    """Return registered tool specs in default order or in the requested order."""
    if names is None:
        return [_copy_spec(spec) for spec in TOOL_SPECS]
    return [get_tool_spec(name) for name in names]


def get_tool_schemas(names: Collection[str] | None = None) -> list[dict]:
    """Return provider-neutral tool schemas in default order or in the requested order."""
    return [spec.schema for spec in get_tool_specs(names)]


def is_registered_tool(name: str) -> bool:
    """Return whether a tool name exists in the registry."""
    return name in _REGISTRY
