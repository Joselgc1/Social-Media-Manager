---
name: add-tool
description: Add a new AI tool/function to the chatbot engine. Use when the user wants to add a tool, create a function call, or extend the AI's capabilities.
argument-hint: "[tool description]"
---

# Tool description

$ARGUMENTS$

## Current tool definitions

!`cd /home/joselgc/projects/Social-Media-Manager && grep -n "\"name\":" store/app/ai/functions.py 2>/dev/null || echo "Could not read functions.py"`

## Current tool handlers

!`cd /home/joselgc/projects/Social-Media-Manager && grep -n "async def _tool_\|def _tool_" store/app/ai/engine.py 2>/dev/null || echo "Could not read engine.py"`

## Implementation steps

1. **Define the tool** in `store/app/ai/functions.py`:
   - Add a new dict to the tools list following the JSON Schema format
   - Include `name`, `description`, and `parameters` (with `type`, `properties`, `required`)
   - The definition is provider-agnostic — each provider converts it automatically

2. **Add the handler** in `store/app/ai/engine.py`:
   - Create an `async def _tool_<name>(args, customer_id, db)` function
   - Follow the pattern of existing handlers (check the ones listed above)
   - Return a string result that gets sent back to the LLM as tool output
   - The LLM then formulates the customer-facing response

3. **Wire the handler** in the tool dispatch section of `engine.py`:
   - Add a case in the if/elif chain that matches the tool name and calls your handler

## Important rules

- **Privacy:** Never return raw data counts or internal IDs to the LLM. Use booleans or summaries (e.g., `in_stock: true` instead of `stock: 47`)
- **One tool per iteration:** The engine processes one tool call per loop iteration (max 6 rounds). Don't assume multiple tools run in one pass.
- **Provider-agnostic:** Define tools once in `functions.py`. OpenAI and Anthropic providers convert them automatically.
- **No direct customer output:** Tool results go to the LLM, which decides how to present them. The tool itself should never format a customer-facing message.

Read both files fully before implementing to match the exact patterns used.
