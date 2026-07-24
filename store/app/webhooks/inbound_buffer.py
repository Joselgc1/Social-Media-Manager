"""
Small in-memory debounce buffer for inbound user messages.

Used to group rapid consecutive messages from the same customer into a
single AI turn, so the assistant responds once with fuller context.

This is intentionally in-memory because the store app currently runs as a
single instance / single worker.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

MESSAGE_DEBOUNCE_SECONDS = 10


Processor = Callable[[str, str, str | None, dict], Awaitable[None]]


@dataclass
class _PendingInbound:
    sender_id: str
    channel: str
    parts: list[str] = field(default_factory=list)
    media_url: str | None = None
    customer_profile: dict = field(default_factory=dict)
    processor: Processor | None = None
    sequence: int = 0
    task: asyncio.Task | None = None


_PENDING: dict[tuple[str, str], _PendingInbound] = {}
_LOCK = asyncio.Lock()


def _merge_profile(current: dict, new_values: dict | None) -> dict:
    merged = dict(current or {})
    for key, value in (new_values or {}).items():
        if value:
            merged[key] = value
    return merged


def _combine_parts(parts: list[str]) -> str:
    cleaned = [part.strip() for part in parts if (part or "").strip()]
    return "\n".join(cleaned)


async def enqueue_inbound_message(
    *,
    channel: str,
    sender_id: str,
    text: str,
    processor: Processor,
    media_url: str | None = None,
    customer_profile: dict | None = None,
) -> None:
    """
    Buffer an inbound message and trigger the processor only after a brief
    quiet period from the same sender/channel.
    """
    key = (channel, sender_id)

    async with _LOCK:
        pending = _PENDING.get(key)
        if pending is None:
            pending = _PendingInbound(sender_id=sender_id, channel=channel)
            _PENDING[key] = pending

        pending.parts.append(text)
        if media_url:
            pending.media_url = media_url
        pending.customer_profile = _merge_profile(pending.customer_profile, customer_profile)
        pending.processor = processor
        pending.sequence += 1

        if pending.task and not pending.task.done():
            pending.task.cancel()

        sequence = pending.sequence
        pending.task = asyncio.create_task(_flush_after_delay(key, sequence))


async def _flush_after_delay(key: tuple[str, str], sequence: int) -> None:
    try:
        await asyncio.sleep(MESSAGE_DEBOUNCE_SECONDS)

        async with _LOCK:
            pending = _PENDING.get(key)
            if pending is None or pending.sequence != sequence or pending.processor is None:
                return

            text = _combine_parts(pending.parts)
            media_url = pending.media_url
            customer_profile = dict(pending.customer_profile)
            processor = pending.processor
            sender_id = pending.sender_id
            _PENDING.pop(key, None)

        if not text:
            return

        await processor(sender_id, text, media_url, customer_profile)
    except asyncio.CancelledError:
        return
    except Exception:
        logger.exception("Inbound message flush failed for %s/%s", key[0], key[1])

