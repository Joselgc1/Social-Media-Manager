"""Bounded cleanup of expired sensitive operational data."""

from __future__ import annotations

import logging

from app import db

logger = logging.getLogger(__name__)

CONVERSATION_MEDIA_DAYS = 30
CONVERSATION_TEXT_DAYS = 180
PAYMENT_URL_DAYS = 90
ORDER_ADDRESS_DAYS = 365
CUSTOMER_ADDRESS_DAYS = 365
KOMMO_PAYLOAD_DAYS = 7
KOMMO_UNKNOWN_PAYLOAD_DAYS = 90
KOMMO_TERMINAL_JOB_DAYS = 30
KOMMO_RECEIPT_DAYS = 30
BROADCAST_DELIVERY_DAYS = 90
META_CONTEXT_SENSITIVE_DAYS = 0
DEFAULT_BATCH_SIZE = 500
DEFAULT_MAX_BATCHES = 10

_POLICIES = (
    (
        "conversation_media",
        CONVERSATION_MEDIA_DAYS,
        """
        WITH candidates AS (
            SELECT id FROM conversations
            WHERE media_url IS NOT NULL
              AND created_at < NOW() - (:days * INTERVAL '1 day')
            ORDER BY created_at LIMIT :batch_size
            FOR UPDATE SKIP LOCKED
        ), changed AS (
            UPDATE conversations SET media_url = NULL
            WHERE id IN (SELECT id FROM candidates)
            RETURNING 1
        ) SELECT COUNT(*) AS count FROM changed
        """,
    ),
    (
        "conversation_text",
        CONVERSATION_TEXT_DAYS,
        """
        WITH candidates AS (
            SELECT id FROM conversations
            WHERE created_at < NOW() - (:days * INTERVAL '1 day')
            ORDER BY created_at LIMIT :batch_size
            FOR UPDATE SKIP LOCKED
        ), changed AS (
            DELETE FROM conversations WHERE id IN (SELECT id FROM candidates)
            RETURNING 1
        ) SELECT COUNT(*) AS count FROM changed
        """,
    ),
    (
        "payment_details",
        PAYMENT_URL_DAYS,
        """
        WITH candidates AS (
            SELECT id FROM orders
            WHERE (
                  payment_proof IS NOT NULL OR payment_reference IS NOT NULL
                  OR payment_currency IS NOT NULL OR payment_amount IS NOT NULL
                  OR payment_transaction_at IS NOT NULL OR payment_verified_at IS NOT NULL
              )
              AND payment_status IN ('proof_received', 'confirmed', 'failed', 'rejected')
              AND updated_at < NOW() - (:days * INTERVAL '1 day')
            ORDER BY updated_at LIMIT :batch_size
            FOR UPDATE SKIP LOCKED
        ), changed AS (
            UPDATE orders
            SET payment_proof = NULL, payment_reference = NULL,
                payment_currency = NULL, payment_amount = NULL,
                payment_transaction_at = NULL, payment_verified_at = NULL,
                updated_at = NOW()
            WHERE id IN (SELECT id FROM candidates)
            RETURNING 1
        ) SELECT COUNT(*) AS count FROM changed
        """,
    ),
    (
        "order_addresses",
        ORDER_ADDRESS_DAYS,
        """
        WITH candidates AS (
            SELECT id FROM orders
            WHERE shipping_address IS NOT NULL
              AND payment_status IN ('proof_received', 'confirmed', 'failed', 'rejected')
              AND created_at < NOW() - (:days * INTERVAL '1 day')
            ORDER BY created_at LIMIT :batch_size
            FOR UPDATE SKIP LOCKED
        ), changed AS (
            UPDATE orders
            SET shipping_address = NULL, shipping_city = NULL, updated_at = NOW()
            WHERE id IN (SELECT id FROM candidates)
            RETURNING 1
        ) SELECT COUNT(*) AS count FROM changed
        """,
    ),
    (
        "customer_addresses",
        CUSTOMER_ADDRESS_DAYS,
        """
        WITH candidates AS (
            SELECT c.id FROM customers c
            WHERE c.last_shipping_address IS NOT NULL
              AND c.last_active < NOW() - (:days * INTERVAL '1 day')
              AND NOT EXISTS (
                  SELECT 1 FROM orders o
                  WHERE o.customer_id = c.id AND o.payment_status = 'pending'
              )
            ORDER BY c.last_active LIMIT :batch_size
            FOR UPDATE SKIP LOCKED
        ), changed AS (
            UPDATE customers
            SET last_shipping_address = NULL, last_shipping_city = NULL,
                last_shipping_method = NULL
            WHERE id IN (SELECT id FROM candidates)
            RETURNING 1
        ) SELECT COUNT(*) AS count FROM changed
        """,
    ),
    (
        "meta_instagram_context_sensitive",
        META_CONTEXT_SENSITIVE_DAYS,
        """
        WITH candidates AS (
            SELECT id FROM meta_instagram_context_events
            WHERE expires_at < NOW() - (:days * INTERVAL '1 day')
              AND (message_text IS NOT NULL OR sender_id IS NOT NULL OR sender_username IS NOT NULL)
            ORDER BY expires_at LIMIT :batch_size
            FOR UPDATE SKIP LOCKED
        ), changed AS (
            UPDATE meta_instagram_context_events
            SET correlation_status = CASE
                    WHEN correlation_status = 'matched' THEN 'matched'
                    ELSE 'expired'
                END,
                message_text = NULL, sender_id = NULL, sender_username = NULL,
                updated_at = NOW()
            WHERE id IN (SELECT id FROM candidates)
            RETURNING 1
        ) SELECT COUNT(*) AS count FROM changed
        """,
    ),
    (
        "kommo_payloads",
        KOMMO_PAYLOAD_DAYS,
        """
        WITH candidates AS (
            SELECT id FROM kommo_message_jobs
            WHERE status IN ('sent', 'discarded', 'failed')
              AND completed_at < NOW() - (:days * INTERVAL '1 day')
              AND (
                  combined_message != '[redacted]' OR media_url IS NOT NULL
                  OR return_url IS NOT NULL OR callback_claims IS NOT NULL
                   OR public_comment_context IS NOT NULL OR continuation_payload IS NOT NULL
                   OR instagram_content_context <> '{}'::jsonb
                  OR continuation_response IS NOT NULL
              )
            ORDER BY completed_at LIMIT :batch_size
            FOR UPDATE SKIP LOCKED
        ), changed AS (
            UPDATE kommo_message_jobs
            SET combined_message = '[redacted]', media_url = NULL, return_url = NULL,
                callback_claims = NULL, public_comment_context = NULL,
                instagram_content_context = '{}'::jsonb,
                continuation_payload = NULL, continuation_response = NULL,
                author_profile_url = NULL, sender_profile_url = NULL,
                author_name = NULL, author_username = NULL, sender_username = NULL,
                updated_at = NOW()
            WHERE id IN (SELECT id FROM candidates)
            RETURNING 1
        ) SELECT COUNT(*) AS count FROM changed
        """,
    ),
    (
        "kommo_terminal_jobs",
        KOMMO_TERMINAL_JOB_DAYS,
        """
        WITH candidates AS (
            SELECT id FROM kommo_message_jobs
            WHERE status IN ('sent', 'discarded', 'failed')
              AND completed_at < NOW() - (:days * INTERVAL '1 day')
            ORDER BY completed_at LIMIT :batch_size
            FOR UPDATE SKIP LOCKED
        ), changed AS (
            DELETE FROM kommo_message_jobs WHERE id IN (SELECT id FROM candidates)
            RETURNING 1
        ) SELECT COUNT(*) AS count FROM changed
        """,
    ),
    (
        "kommo_unknown_payloads",
        KOMMO_UNKNOWN_PAYLOAD_DAYS,
        """
        WITH candidates AS (
            SELECT id FROM kommo_message_jobs
            WHERE status = 'delivery_unknown'
              AND completed_at < NOW() - (:days * INTERVAL '1 day')
              AND (
                  combined_message != '[redacted]' OR media_url IS NOT NULL
                  OR return_url IS NOT NULL OR callback_claims IS NOT NULL
                   OR public_comment_context IS NOT NULL OR continuation_payload IS NOT NULL
                   OR instagram_content_context <> '{}'::jsonb
                  OR continuation_response IS NOT NULL
              )
            ORDER BY completed_at LIMIT :batch_size
            FOR UPDATE SKIP LOCKED
        ), changed AS (
            UPDATE kommo_message_jobs
            SET combined_message = '[redacted]', media_url = NULL, return_url = NULL,
                callback_claims = NULL, public_comment_context = NULL,
                instagram_content_context = '{}'::jsonb,
                continuation_payload = NULL, continuation_response = NULL,
                author_profile_url = NULL, sender_profile_url = NULL,
                author_name = NULL, author_username = NULL, sender_username = NULL,
                updated_at = NOW()
            WHERE id IN (SELECT id FROM candidates)
            RETURNING 1
        ) SELECT COUNT(*) AS count FROM changed
        """,
    ),
    (
        "kommo_receipts",
        KOMMO_RECEIPT_DAYS,
        """
        WITH candidates AS (
            SELECT id FROM kommo_message_receipts
            WHERE created_at < NOW() - (:days * INTERVAL '1 day')
            ORDER BY created_at LIMIT :batch_size
            FOR UPDATE SKIP LOCKED
        ), changed AS (
            DELETE FROM kommo_message_receipts WHERE id IN (SELECT id FROM candidates)
            RETURNING 1
        ) SELECT COUNT(*) AS count FROM changed
        """,
    ),
    (
        "broadcast_deliveries",
        BROADCAST_DELIVERY_DAYS,
        """
        WITH candidates AS (
            SELECT id FROM broadcast_deliveries
            WHERE status IN ('sent', 'failed')
              AND updated_at < NOW() - (:days * INTERVAL '1 day')
            ORDER BY updated_at LIMIT :batch_size
            FOR UPDATE SKIP LOCKED
        ), changed AS (
            DELETE FROM broadcast_deliveries WHERE id IN (SELECT id FROM candidates)
            RETURNING 1
        ) SELECT COUNT(*) AS count FROM changed
        """,
    ),
)


async def run_data_retention(
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    max_batches: int = DEFAULT_MAX_BATCHES,
) -> dict[str, int]:
    """Apply each retention policy in bounded batches and return affected counts."""
    batch_size = max(1, min(int(batch_size), 5000))
    max_batches = max(1, min(int(max_batches), 100))
    totals = {}
    for name, days, query in _POLICIES:
        total = 0
        for _ in range(max_batches):
            row = await db.fetch_one(query, {"days": days, "batch_size": batch_size})
            changed = int(row["count"] or 0) if row else 0
            total += changed
            if changed < batch_size:
                break
        totals[name] = total
    logger.info("Sensitive data retention completed: %s", totals)
    return totals
