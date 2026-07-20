"""Channel-aware customer message formatting."""

from __future__ import annotations

import re

_WHATSAPP_MARKDOWN_BOLD_RE = re.compile(r"\*\*(.+?)\*\*", flags=re.DOTALL)
_WHATSAPP_MARKDOWN_UNDERSCORE_BOLD_RE = re.compile(r"__(.+?)__", flags=re.DOTALL)
_WHATSAPP_MARKDOWN_STRIKETHROUGH_RE = re.compile(r"~~(.+?)~~", flags=re.DOTALL)
_WHATSAPP_MARKDOWN_LINK_RE = re.compile(r"!?\[([^\]\n]+)\]\((https?://[^)\s]+)\)")
_WHATSAPP_MARKDOWN_HEADING_RE = re.compile(r"^#{1,6}[ \t]+(.+?)\s*#*\s*$", flags=re.MULTILINE)
_WHATSAPP_FENCED_CODE_RE = re.compile(r"```(?:[a-zA-Z0-9_-]+\n)?([\s\S]*?)```")
_WHATSAPP_INLINE_CODE_RE = re.compile(r"(?<!`)`([^`\n]+?)`(?!`)")
_WHATSAPP_HTML_CODE_RE = re.compile(r"<\s*code(?:\s+[^>]*)?\s*>(.*?)<\s*/\s*code\s*>", flags=re.IGNORECASE | re.DOTALL)
_WHATSAPP_HTML_BR_RE = re.compile(r"<\s*br\s*/?\s*>", flags=re.IGNORECASE)
_WHATSAPP_HTML_LIST_ITEM_RE = re.compile(r"<\s*li(?:\s+[^>]*)?\s*>(.*?)<\s*/\s*li\s*>", flags=re.IGNORECASE | re.DOTALL)
_WHATSAPP_HTML_BLOCK_CLOSE_RE = re.compile(r"<\s*/\s*(?:p|div|section|article|ul|ol)\s*>", flags=re.IGNORECASE)
_WHATSAPP_HTML_TAG_RE = re.compile(r"<[^>\n]+>")
_WHATSAPP_LIST_SPACING_RE = re.compile(r"\n{2,}(?=(?:[-*•]|\d+[.)])\s+)")
_WHATSAPP_MULTIPLE_BLANK_LINES_RE = re.compile(r"\n{3,}")
_WHATSAPP_HTML_FORMATS = (
    (re.compile(r"<\s*(?:strong|b)(?:\s+[^>]*)?\s*>(.*?)<\s*/\s*(?:strong|b)\s*>", flags=re.IGNORECASE | re.DOTALL), "*"),
    (re.compile(r"<\s*(?:em|i)(?:\s+[^>]*)?\s*>(.*?)<\s*/\s*(?:em|i)\s*>", flags=re.IGNORECASE | re.DOTALL), "_"),
    (re.compile(r"<\s*(?:s|del|strike)(?:\s+[^>]*)?\s*>(.*?)<\s*/\s*(?:s|del|strike)\s*>", flags=re.IGNORECASE | re.DOTALL), "~"),
)
_INSTAGRAM_BOLD_RE = re.compile(r"\*\*(.+?)\*\*", flags=re.DOTALL)
_INSTAGRAM_LIST_MARKER_RE = re.compile(r"^(\s*)(?:[-*•]|\d+[.)])\s+")
_WHATSAPP_INLINE_LIST_START_RE = re.compile(r":([ \t]+)((?:[-*•]|\d+[.)])[ \t]+)")
_WHATSAPP_INLINE_LIST_CONTINUATION_RE = re.compile(r"(?<=\S)[ \t]+((?:[-*•]|\d+[.)])[ \t]+)")
_WHATSAPP_LIST_LINE_RE = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+")
_WHATSAPP_LIST_FOLLOWUP_QUESTION_RE = re.compile(r"^(\s*(?:[-*•]|\d+[.)])\s+.+?)\s+(¿.+)$")
_URL_RE = re.compile(r"https?://\S+")


def format_customer_text(text: str | None, channel: str | None) -> str:
    """Apply only the formatting rules supported by the destination channel."""
    formatted = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if channel == "whatsapp":
        return _format_whatsapp_text(formatted)
    if channel == "instagram":
        return _format_instagram_text(formatted)
    return formatted


def _format_whatsapp_text(text: str) -> str:
    formatted, code_placeholders = _protect_whatsapp_code(text)
    formatted = _convert_markdown_links(formatted)
    formatted, url_placeholders = _protect_urls(formatted)
    formatted = _apply_whatsapp_markup(formatted)
    formatted = _break_inline_whatsapp_lists(formatted)
    formatted = _restore_placeholders(formatted, url_placeholders)
    formatted = _restore_placeholders(formatted, code_placeholders)
    return formatted.strip()


def _protect_whatsapp_code(text: str) -> tuple[str, dict[str, str]]:
    placeholders: dict[str, str] = {}

    def store_code(value: str) -> str:
        code = value.strip("\n").strip()
        return _store_placeholder(placeholders, f"```{code}```" if code else "", "WACODE")

    formatted = _WHATSAPP_HTML_CODE_RE.sub(lambda match: store_code(match.group(1)), text)
    formatted = _WHATSAPP_FENCED_CODE_RE.sub(lambda match: store_code(match.group(1)), formatted)
    formatted = _WHATSAPP_INLINE_CODE_RE.sub(lambda match: store_code(match.group(1)), formatted)
    return formatted, placeholders


def _convert_markdown_links(text: str) -> str:
    def replace_link(match: re.Match) -> str:
        label = match.group(1).strip()
        url = match.group(2).strip()
        return f"{label}: {url}" if label else url

    return _WHATSAPP_MARKDOWN_LINK_RE.sub(replace_link, text)


def _protect_urls(text: str) -> tuple[str, dict[str, str]]:
    placeholders: dict[str, str] = {}
    return _URL_RE.sub(lambda match: _store_placeholder(placeholders, match.group(0), "WAURL"), text), placeholders


def _store_placeholder(placeholders: dict[str, str], value: str, prefix: str) -> str:
    token = f"@@{prefix}{len(placeholders)}@@"
    placeholders[token] = value
    return token


def _restore_placeholders(text: str, placeholders: dict[str, str]) -> str:
    restored = text
    for token, value in placeholders.items():
        restored = restored.replace(token, value)
    return restored


def _apply_whatsapp_markup(text: str) -> str:
    formatted = _WHATSAPP_HTML_BR_RE.sub("\n", text)
    for pattern, marker in _WHATSAPP_HTML_FORMATS:
        formatted = pattern.sub(lambda match, marker=marker: _wrap_whatsapp_markup(match.group(1), marker), formatted)
    formatted = _WHATSAPP_HTML_LIST_ITEM_RE.sub(lambda match: f"\n- {match.group(1).strip()}", formatted)
    formatted = _WHATSAPP_HTML_BLOCK_CLOSE_RE.sub("\n", formatted)
    formatted = _WHATSAPP_HTML_TAG_RE.sub("", formatted)
    formatted = _WHATSAPP_LIST_SPACING_RE.sub("\n", formatted)
    formatted = _WHATSAPP_MULTIPLE_BLANK_LINES_RE.sub("\n\n", formatted)
    formatted = _WHATSAPP_MARKDOWN_HEADING_RE.sub(lambda match: _wrap_whatsapp_markup(match.group(1), "*"), formatted)
    formatted = _WHATSAPP_MARKDOWN_BOLD_RE.sub(lambda match: _wrap_whatsapp_markup(match.group(1), "*"), formatted)
    formatted = _WHATSAPP_MARKDOWN_UNDERSCORE_BOLD_RE.sub(lambda match: _wrap_whatsapp_markup(match.group(1), "*"), formatted)
    return _WHATSAPP_MARKDOWN_STRIKETHROUGH_RE.sub(lambda match: _wrap_whatsapp_markup(match.group(1), "~"), formatted)


def _wrap_whatsapp_markup(value: str, marker: str) -> str:
    text = str(value or "").strip()
    return f"{marker}{text}{marker}" if text else ""


def _break_inline_whatsapp_lists(text: str) -> str:
    lines: list[str] = []
    for line in text.split("\n"):
        lines.extend(_break_inline_whatsapp_list_line(line))
    return "\n".join(lines)


def _break_inline_whatsapp_list_line(line: str) -> list[str]:
    starts_inline_list = bool(_WHATSAPP_INLINE_LIST_START_RE.search(line))
    is_list_line = bool(_WHATSAPP_LIST_LINE_RE.match(line))
    if not starts_inline_list and not is_list_line:
        return [line]

    normalized = _WHATSAPP_INLINE_LIST_START_RE.sub(r":\n\2", line, count=1)
    segments: list[str] = []
    for segment in normalized.split("\n"):
        if _WHATSAPP_LIST_LINE_RE.match(segment):
            segment = _WHATSAPP_INLINE_LIST_CONTINUATION_RE.sub(r"\n\1", segment)
        segments.extend(segment.split("\n"))
    return _separate_whatsapp_list_followup_questions(segments)


def _separate_whatsapp_list_followup_questions(lines: list[str]) -> list[str]:
    separated: list[str] = []
    for line in lines:
        match = _WHATSAPP_LIST_FOLLOWUP_QUESTION_RE.match(line)
        if not match:
            separated.append(line)
            continue
        separated.append(match.group(1).rstrip())
        if separated[-1] and match.group(2).strip():
            separated.append("")
        separated.append(match.group(2).strip())
    return separated


def _format_instagram_text(text: str) -> str:
    formatted = _INSTAGRAM_BOLD_RE.sub(r"\1", text)
    lines = [_INSTAGRAM_LIST_MARKER_RE.sub(r"\1• ", line) for line in formatted.split("\n")]
    return _remove_asterisks_outside_urls("\n".join(lines)).strip()


def _remove_asterisks_outside_urls(text: str) -> str:
    parts: list[str] = []
    last_end = 0
    for match in _URL_RE.finditer(text):
        parts.append(text[last_end:match.start()].replace("*", ""))
        parts.append(match.group(0))
        last_end = match.end()
    parts.append(text[last_end:].replace("*", ""))
    return "".join(parts)
