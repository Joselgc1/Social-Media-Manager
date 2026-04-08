---
name: tune-prompt
description: Review and improve the AI chatbot system prompt. Use when the user discusses prompt improvements, chatbot behavior, AI personality, or response quality.
---

# Current system prompt

!`cat /home/joselgc/projects/Social-Media-Manager/store/prompts/system_prompt.md 2>/dev/null || echo "System prompt file not found"`

## Current prompt builder

!`cat /home/joselgc/projects/Social-Media-Manager/store/app/ai/prompts.py 2>/dev/null || echo "Prompt builder not found"`

## Context

The system prompt is the core personality and behavior definition for "Eva", the AI sales assistant for a Venezuelan Victoria's Secret resale business. It's written in Spanish and handles WhatsApp/Instagram customer conversations.

### How the prompt system works

1. The base template lives in `store/prompts/system_prompt.md`
2. `store/app/ai/prompts.py` injects dynamic context: product catalog, customer history, order status, store name
3. Available placeholders: `{store_name}`, `{product_catalog}`, `{customer_context}`, `{order_context}`, etc.
4. The `SYSTEM_PROMPT_OVERRIDE` env var can replace the template entirely (must use same placeholders)

### Key constraints

- Must be in Spanish (Venezuelan dialect preferred)
- Sales-focused: guide conversations toward purchases
- Rule 13: Never output raw JSON, tool results, or technical metadata to customers
- Inventory privacy: Never reveal exact stock counts
- Must handle payment methods: Zelle, Binance, Zinli, Bolivares (tasa Binance)
- Must handle shipping: MRW or Zoom (Venezuelan couriers)

## How to help

When reviewing or improving the prompt:

1. Read the full current prompt carefully
2. Identify areas that could be clearer, more consistent, or more effective
3. Suggest specific edits with before/after examples
4. Consider how changes affect tool usage (the AI calls tools based on prompt instructions)
5. Test changes with `/test-chat` after applying them
