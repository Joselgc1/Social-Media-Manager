"""
Conversation history storage and retrieval.
Stores every message (user + assistant) and retrieves the last N
for context in the LLM prompt.
"""

import json
from app import db


async def store_message(
    customer_id: str,
    role: str,
    content: str,
    channel: str,
    media_url: str | None = None,
    function_calls: list[dict] | None = None,
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
    """
    await db.execute(
        """
        INSERT INTO conversations (customer_id, role, content, channel, media_url, function_calls)
        VALUES (:cid, :role, :content, :channel, :media, :fc)
        """,
        {
            "cid": customer_id,
            "role": role,
            "content": content,
            "channel": channel,
            "media": media_url,
            "fc": json.dumps(function_calls) if function_calls else None,
        },
    )


async def get_history(customer_id: str, limit: int = 20) -> list[dict]:
    """
    Retrieve the last `limit` messages for a customer,
    formatted as the messages array expected by OpenAI/Anthropic.

    Returns
    -------
    list[dict]
        [{"role": "user", "content": "..."}, {"role": "assistant", "content": "..."}, ...]
        Ordered oldest-first (chronological).
    """
    rows = await db.fetch_all(
        """
        SELECT role, content
        FROM conversations
        WHERE customer_id = :cid
        ORDER BY created_at DESC
        LIMIT :limit
        """,
        {"cid": customer_id, "limit": limit},
    )

    # Rows come newest-first from DB; reverse to chronological order
    messages = [{"role": row["role"], "content": row["content"]} for row in reversed(rows)]
    return messages


async def get_recent_summary(customer_id: str, limit: int = 5) -> str:
    """
    Get a plain-text summary of the last few messages.
    Used for escalation notifications to the store owner.
    """
    rows = await db.fetch_all(
        """
        SELECT role, content, created_at
        FROM conversations
        WHERE customer_id = :cid
        ORDER BY created_at DESC
        LIMIT :limit
        """,
        {"cid": customer_id, "limit": limit},
    )

    lines = []
    for row in reversed(rows):
        prefix = "Cliente" if row["role"] == "user" else "Eva"
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
