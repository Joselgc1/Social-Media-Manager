---
name: add-tool
description: Add a new AI tool/function to the chatbot engine. Use when the user wants to add a tool, create a function call, or extend the AI's capabilities.
argument-hint: "[tool description]"
---

# Tool description

$ARGUMENTS$

## Current tool definitions

!`cd /home/joselgc/projects/Social-Media-Manager && rg -n "\"name\":" store/app/ai/tools/definitions.py 2>/dev/null || echo "Could not read tool definitions"`

## Current tool handlers

!`cd /home/joselgc/projects/Social-Media-Manager && rg -n "async def|def " store/app/ai/tools 2>/dev/null || echo "Could not read tool handlers"`

## Current agent allowlists

!`cd /home/joselgc/projects/Social-Media-Manager && rg -n "allowed_tools" store/app/ai/agents 2>/dev/null || echo "Could not read agent allowlists"`

## Implementation steps

1. **Define the tool** in `store/app/ai/tools/definitions.py`:
   - Add a new dict to the tool definitions following the JSON Schema format
   - Include `name`, `description`, and `parameters` (with `type`, `properties`, `required`)
   - The definition is provider-agnostic — each provider converts it automatically

2. **Add the handler** under `store/app/ai/tools/`:
   - Put the behavior in the most relevant handler module, or add a new module if needed
   - Follow the pattern of existing handlers and keep sensitive values out of tool results
   - Return a string result that gets sent back to the LLM as tool output
   - The LLM then formulates the customer-facing response

3. **Wire the handler** in `store/app/ai/tools/executor.py`:
   - Add a dispatch case that matches the tool name and calls your handler

4. **Grant permissions** in `store/app/ai/agents/`:
   - Add the tool only to agents that should be allowed to call it
   - Add tests proving unauthorized agents cannot call the tool

## Important rules

- **Privacy:** Never return raw data counts or internal IDs to the LLM. Use booleans or summaries (e.g., `in_stock: true` instead of `stock: 47`)
- **One tool per iteration:** The engine processes one tool call per loop iteration (max 6 rounds). Don't assume multiple tools run in one pass.
- **Provider-agnostic:** Define tools once in `tools/definitions.py`. OpenAI and Anthropic providers convert them automatically.
- **Agent permissions:** Tool allowlists are enforced in `store/app/ai/runner.py`; never bypass them in a handler.
- **No direct customer output:** Tool results go to the LLM, which decides how to present them. The tool itself should never format a customer-facing message.

Read the relevant definition, handler, executor, and agent files fully before implementing to match the exact patterns used.
