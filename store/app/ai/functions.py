"""
Compatibility export for provider-neutral AI tool schemas.
"""

from app.ai.tools.registry import get_tool_schemas

TOOLS = get_tool_schemas()
