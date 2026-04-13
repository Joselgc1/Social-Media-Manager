"""
Safety guards for customer-facing AI output.
"""

from __future__ import annotations

import re

SAFE_FALLBACK_REPLY = "Déjame confirmarte eso bien y te respondo enseguida."

_SUSPICIOUS_MARKERS = (
    '{"',
    "[{",
    '"found":',
    '"count":',
    '"products":',
    '"sku":',
    '"price_usd":',
    '"in_stock":',
    '"has_image":',
    '"status":',
    '"message":',
    "tool_call",
    "tool_result",
    "raw_response",
    "function_call",
)


def sanitize_customer_facing_text(text: str, fallback_text: str = SAFE_FALLBACK_REPLY) -> str:
    cleaned = (text or "").strip()
    if not cleaned:
        return ""

    for _ in range(4):
        previous = cleaned
        cleaned = _remove_suspicious_code_fence(cleaned)
        cleaned = _remove_leading_structured_blob(cleaned)
        cleaned = _remove_leading_technical_line(cleaned)
        cleaned = cleaned.strip()
        if cleaned == previous:
            break

    if _contains_suspicious_marker(cleaned):
        customer_facing_tail = _extract_customer_facing_tail(cleaned)
        if customer_facing_tail and not _contains_suspicious_marker(customer_facing_tail):
            cleaned = customer_facing_tail
        else:
            return fallback_text

    return cleaned.strip()


def _remove_suspicious_code_fence(text: str) -> str:
    cleaned = text.lstrip()
    if not cleaned.startswith("```"):
        return text

    fence_match = re.match(r"^```[a-zA-Z0-9_-]*\n", cleaned)
    if not fence_match:
        return text

    end_index = cleaned.find("\n```", fence_match.end())
    if end_index == -1:
        return text

    inner_content = cleaned[fence_match.end():end_index]
    if _contains_suspicious_marker(inner_content):
        return cleaned[end_index + 4:].lstrip()
    return text


def _remove_leading_structured_blob(text: str) -> str:
    stripped = text.lstrip()
    if not stripped or stripped[0] not in "{[":
        return text

    closing_char = "}" if stripped[0] == "{" else "]"
    depth = 0
    in_string = False
    escape = False

    for index, char in enumerate(stripped):
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
        elif char == stripped[0]:
            depth += 1
        elif char == closing_char:
            depth -= 1
            if depth == 0:
                remaining = stripped[index + 1:].lstrip()
                return remaining

    return text


def _remove_leading_technical_line(text: str) -> str:
    lines = text.splitlines()
    if not lines:
        return text

    first_line = lines[0].strip().lower()
    if first_line in {"json", "tool_result", "tool call", "resultado interno"}:
        return "\n".join(lines[1:]).lstrip()
    return text


def _contains_suspicious_marker(text: str) -> bool:
    lowered = (text or "").lower()
    return any(marker in lowered for marker in _SUSPICIOUS_MARKERS)


def _extract_customer_facing_tail(text: str) -> str:
    normalized = text.strip()
    sentence_start = re.search(r"(?m)(?:^|\n)([¡¿A-ZÁÉÍÓÚÜÑ])", normalized)
    if not sentence_start:
        return ""

    candidate = normalized[sentence_start.start(1):].strip()
    if candidate == normalized:
        return ""
    return candidate
