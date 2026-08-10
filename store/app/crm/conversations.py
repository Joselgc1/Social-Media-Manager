"""
Conversation history storage and retrieval.
Stores every message (user + assistant) and retrieves the last N
for context in the LLM prompt.
"""

import json
import re

from app import db

PRIVATE_MESSAGE_SCOPE = "private_message"
INSTAGRAM_COMMENT_SCOPE = "instagram_comment"
_INTERACTION_SCOPES = {PRIVATE_MESSAGE_SCOPE, INSTAGRAM_COMMENT_SCOPE}


async def store_message(
    customer_id: str,
    role: str,
    content: str,
    channel: str,
    media_url: str | None = None,
    function_calls: list[dict] | None = None,
    source_id: str | None = None,
    attachments: list[dict] | None = None,
    interaction_type: str = PRIVATE_MESSAGE_SCOPE,
    author_type: str | None = None,
    provider_message_id: str | None = None,
):
    """
    Store a single message in the conversation history.

    Parameters
    ----------
    customer_id : str (UUID)
    role : "user" or "assistant"
    content : The message text
    channel : "instagram" or "whatsapp"
    media_url : URL to any attached media (images, etc.)
    function_calls : List of tool calls the AI made (for logging)
    attachments : Transport-independent semantic attachments for future model context
    """
    semantic_attachments = _normalize_semantic_attachments(attachments)
    interaction_scope = _normalize_interaction_type(interaction_type)
    message_author = author_type or ("customer" if role == "user" else "ai")
    if message_author not in {"customer", "ai", "human"}:
        raise ValueError("Unsupported conversation author type")
    await db.execute(
        """
        INSERT INTO conversations (
            customer_id, role, content, channel, interaction_type, media_url,
            attachments, function_calls, source_id, author_type, provider_message_id
        )
        VALUES (
            :cid, :role, :content, :channel, :interaction_type, :media,
            CAST(:attachments AS jsonb), :fc, :source_id, :author_type, :provider_message_id
        )
        ON CONFLICT (channel, role, source_id) WHERE source_id IS NOT NULL DO NOTHING
        """,
        {
            "cid": customer_id,
            "role": role,
            "content": content,
            "channel": channel,
            "interaction_type": interaction_scope,
            "media": media_url,
            "attachments": (
                json.dumps(semantic_attachments, ensure_ascii=False)
                if semantic_attachments is not None
                else None
            ),
            "fc": json.dumps(function_calls) if function_calls else None,
            "source_id": source_id,
            "author_type": message_author,
            "provider_message_id": provider_message_id,
        },
    )


async def get_history(
    customer_id: str,
    limit: int = 20,
    interaction_type: str = PRIVATE_MESSAGE_SCOPE,
) -> list[dict]:
    """
    Retrieve the last `limit` messages for a customer,
    formatted as the messages array expected by OpenAI/Anthropic.

    Returns
    -------
    list[dict]
        [{"role": "user", "content": "..."}, {"role": "assistant", "content": "..."}, ...]
        Ordered oldest-first (chronological).
    """
    interaction_scope = _normalize_interaction_type(interaction_type)
    rows = await db.fetch_all(
        """
        SELECT role, content, attachments, author_type
        FROM conversations
        WHERE customer_id = :cid
          AND interaction_type = :interaction_type
        ORDER BY created_at DESC
        LIMIT :limit
        """,
        {"cid": customer_id, "limit": limit, "interaction_type": interaction_scope},
    )

    # Rows come newest-first from DB; reverse to chronological order
    messages = []
    for row in reversed(rows):
        role = row["role"]
        content = row["content"]
        if role == "assistant":
            content = _content_with_delivery_context(content, _row_attachments(row))
        messages.append({"role": role, "content": content})
    return messages


def _normalize_semantic_attachments(attachments: list[dict] | None) -> list[dict] | None:
    if attachments is None:
        return None
    if not isinstance(attachments, list):
        raise ValueError("Conversation attachments must be a list")

    allowed_fields = {
        "product_image": ("product_name", "sku"),
        "catalog_pdf": ("filename", "catalog_fingerprint"),
    }
    normalized = []
    for attachment in attachments:
        if not isinstance(attachment, dict):
            raise ValueError("Conversation attachments must contain objects")
        attachment_type = attachment.get("type")
        if attachment_type not in allowed_fields:
            raise ValueError("Unsupported semantic conversation attachment type")
        clean = {"type": attachment_type}
        for field in allowed_fields[attachment_type]:
            value = attachment.get(field)
            if isinstance(value, str) and value.strip():
                clean[field] = value.strip()
        normalized.append(clean)
    return normalized


def _row_attachments(row) -> list[dict]:
    try:
        value = row["attachments"]
    except (KeyError, IndexError):
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return []
    return value if isinstance(value, list) else []


def _content_with_delivery_context(content: str, attachments: list[dict]) -> str:
    annotations = []
    for attachment in attachments:
        if not isinstance(attachment, dict):
            continue
        attachment_type = attachment.get("type")
        if attachment_type == "product_image":
            product_name = _safe_context_value(attachment.get("product_name"))
            sku = _safe_context_value(attachment.get("sku"))
            if product_name and sku:
                detail = f' del producto "{product_name}", SKU {sku}'
            elif product_name:
                detail = f' del producto "{product_name}"'
            else:
                detail = " del producto"
            annotations.append(
                f"[Contexto de entrega: Eva también envió una imagen{detail}.]"
            )
        elif attachment_type == "catalog_pdf":
            annotations.append("[Contexto de entrega: Eva envió el catálogo PDF actualizado.]")
    if not annotations:
        return content
    annotation_text = "\n".join(annotations)
    return f"{content}\n\n{annotation_text}" if content else annotation_text


def _safe_context_value(value) -> str:
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", " ", value).strip().replace('"', "'")[:160]


def prepare_history_for_generation(history: list[dict], latest_user_message: str | None = None) -> list[dict]:
    """Return model-ready history that alternates user/assistant turns."""
    prepared: list[dict] = []
    for message in history:
        role = message.get("role")
        content = message.get("content")
        if role not in {"user", "assistant"} or not content:
            continue
        normalized = {"role": role, "content": content}
        if not prepared:
            if role == "user":
                prepared.append(normalized)
            continue
        if prepared[-1]["role"] == role:
            prepared[-1] = normalized
        else:
            prepared.append(normalized)

    if latest_user_message is not None:
        while prepared and prepared[-1]["role"] == "user":
            prepared.pop()
        prepared.append({"role": "user", "content": latest_user_message})

    return prepared


async def get_recent_summary(
    customer_id: str,
    limit: int = 5,
    interaction_type: str = PRIVATE_MESSAGE_SCOPE,
) -> str:
    """
    Get a plain-text summary of the last few messages.
    Used for escalation notifications to the store owner.
    """
    interaction_scope = _normalize_interaction_type(interaction_type)
    rows = await db.fetch_all(
        """
        SELECT role, content, author_type, created_at
        FROM conversations
        WHERE customer_id = :cid
          AND interaction_type = :interaction_type
        ORDER BY created_at DESC
        LIMIT :limit
        """,
        {"cid": customer_id, "limit": limit, "interaction_type": interaction_scope},
    )

    lines = []
    for row in reversed(rows):
        if row["role"] == "user":
            prefix = "Cliente"
        else:
            try:
                prefix = "Equipo" if row["author_type"] == "human" else "Eva"
            except (KeyError, IndexError):
                prefix = "Eva"
        lines.append(f"[{prefix}]: {row['content']}")

    return "\n".join(lines) if lines else "(Sin mensajes previos)"


async def clear_history(customer_id: str):
    """Delete all stored conversation messages for a customer."""
    await db.execute(
        "DELETE FROM conversations WHERE customer_id = :cid",
        {"cid": customer_id},
    )


async def clear_history_for_customers(customer_ids: list[str]):
    """Delete all stored conversation messages for a list of customers."""
    unique_ids = [cid for cid in dict.fromkeys(customer_ids) if cid]
    if not unique_ids:
        return

    async with db.get_db().transaction():
        for customer_id in unique_ids:
            await db.execute(
                "DELETE FROM conversations WHERE customer_id = :cid",
                {"cid": customer_id},
            )


def _normalize_interaction_type(value: str | None) -> str:
    normalized = str(value or PRIVATE_MESSAGE_SCOPE).strip().lower()
    if normalized not in _INTERACTION_SCOPES:
        raise ValueError("Unsupported conversation interaction type")
    return normalized
