"""
Analytics module.
Provides conversion metrics, response time tracking, product popularity,
and per-provider performance comparison.
"""

import json
import logging
from datetime import date, timedelta

from app import db

logger = logging.getLogger(__name__)


# ── Response time tracking ───────────────────────────────────

async def log_response(
    provider: str,
    model: str,
    usage: dict,
    customer_id: str,
    response_time_ms: int,
    was_fallback: bool = False,
    had_tool_calls: bool = False,
    channel: str = "whatsapp",
):
    """Enhanced usage logging with response time and metadata."""
    try:
        await db.execute(
            """
            INSERT INTO usage_log
                (provider, model, input_tokens, output_tokens, customer_id,
                 response_time_ms, was_fallback, had_tool_calls, channel)
            VALUES
                (:provider, :model, :input, :output, :cid,
                 :rt, :fb, :tc, :channel)
            """,
            {
                "provider": provider,
                "model": model,
                "input": usage.get("input_tokens", 0),
                "output": usage.get("output_tokens", 0),
                "cid": customer_id,
                "rt": response_time_ms,
                "fb": was_fallback,
                "tc": had_tool_calls,
                "channel": channel,
            },
        )
    except Exception as e:
        logger.error(f"Failed to log response: {e}")


async def log_ai_run(
    *,
    customer_id: str,
    channel: str,
    orchestration_mode: str,
    selected_agent: str,
    route_intent: str | None = None,
    route_source: str | None = None,
    route_confidence: float | None = None,
    provider: str | None = None,
    model: str | None = None,
    usage: dict | None = None,
    response_time_ms: int | None = None,
    tool_names: list[str] | tuple[str, ...] | None = None,
    tool_rounds: int = 0,
    handoff_occurred: bool = False,
    fallback_occurred: bool = False,
    escalation_occurred: bool = False,
    shadow_evaluation: bool = False,
    legacy_fallback: bool = False,
) -> None:
    """Log non-sensitive AI orchestration metadata for evaluation and rollout."""
    safe_tool_names = [str(name)[:80] for name in (tool_names or []) if name]
    token_usage = usage or {}
    try:
        await db.execute(
            """
            INSERT INTO ai_run_logs
                (customer_id, channel, orchestration_mode, selected_agent,
                 route_intent, route_source, route_confidence, provider, model,
                 input_tokens, output_tokens, response_time_ms, tool_names, tool_rounds,
                 handoff_occurred, fallback_occurred, escalation_occurred,
                 shadow_evaluation, legacy_fallback)
            VALUES
                (:customer_id, :channel, :orchestration_mode, :selected_agent,
                 :route_intent, :route_source, :route_confidence, :provider, :model,
                 :input_tokens, :output_tokens, :response_time_ms, CAST(:tool_names AS jsonb), :tool_rounds,
                 :handoff_occurred, :fallback_occurred, :escalation_occurred,
                 :shadow_evaluation, :legacy_fallback)
            """,
            {
                "customer_id": customer_id,
                "channel": channel,
                "orchestration_mode": orchestration_mode,
                "selected_agent": selected_agent,
                "route_intent": route_intent,
                "route_source": route_source,
                "route_confidence": route_confidence,
                "provider": provider,
                "model": model,
                "input_tokens": int(token_usage.get("input_tokens", 0) or 0),
                "output_tokens": int(token_usage.get("output_tokens", 0) or 0),
                "response_time_ms": response_time_ms,
                "tool_names": json.dumps(safe_tool_names),
                "tool_rounds": int(tool_rounds or 0),
                "handoff_occurred": handoff_occurred,
                "fallback_occurred": fallback_occurred,
                "escalation_occurred": escalation_occurred,
                "shadow_evaluation": shadow_evaluation,
                "legacy_fallback": legacy_fallback,
            },
        )
    except Exception as e:
        logger.error(f"Failed to log AI run metadata: {e}")


# ── Conversion analytics ─────────────────────────────────────

async def get_conversion_funnel(days: int = 7) -> dict:
    """
    Calculate the conversion funnel for the last N days.
    Stages: message received -> product inquiry -> order created -> payment confirmed
    """
    since = date.today() - timedelta(days=days)

    # Total unique customers who messaged
    messaged = await db.fetch_one(
        """
        SELECT COUNT(DISTINCT customer_id) as cnt
        FROM conversations
        WHERE role = 'user' AND created_at >= :since
        """,
        {"since": since},
    )

    # Customers who triggered check_inventory (showed purchase intent)
    inquired = await db.fetch_one(
        """
        SELECT COUNT(DISTINCT customer_id) as cnt
        FROM conversations
        WHERE role = 'assistant'
          AND function_calls IS NOT NULL
          AND function_calls::text LIKE '%check_inventory%'
          AND created_at >= :since
        """,
        {"since": since},
    )

    # Orders created
    ordered = await db.fetch_one(
        "SELECT COUNT(DISTINCT customer_id) as cnt FROM orders WHERE created_at >= :since",
        {"since": since},
    )

    # Orders with payment confirmed
    paid = await db.fetch_one(
        """
        SELECT COUNT(DISTINCT customer_id) as cnt FROM orders
        WHERE payment_status IN ('proof_received', 'confirmed')
          AND created_at >= :since
        """,
        {"since": since},
    )

    m = messaged["cnt"] if messaged else 0
    i = inquired["cnt"] if inquired else 0
    o = ordered["cnt"] if ordered else 0
    p = paid["cnt"] if paid else 0

    return {
        "period_days": days,
        "funnel": {
            "messaged": m,
            "inquired_products": i,
            "placed_order": o,
            "paid": p,
        },
        "rates": {
            "inquiry_rate": f"{(i/m*100):.1f}%" if m > 0 else "0%",
            "order_rate": f"{(o/m*100):.1f}%" if m > 0 else "0%",
            "payment_rate": f"{(p/o*100):.1f}%" if o > 0 else "0%",
            "overall_conversion": f"{(p/m*100):.1f}%" if m > 0 else "0%",
        },
    }


# ── Response time analytics ──────────────────────────────────

async def get_response_time_stats(days: int = 7) -> dict:
    """Average and percentile response times by provider."""
    since = date.today() - timedelta(days=days)

    rows = await db.fetch_all(
        """
        SELECT provider, model,
               COUNT(*) as calls,
               AVG(response_time_ms)::int as avg_ms,
               PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY response_time_ms)::int as p50_ms,
               PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY response_time_ms)::int as p95_ms,
               MIN(response_time_ms) as min_ms,
               MAX(response_time_ms) as max_ms
        FROM usage_log
        WHERE response_time_ms IS NOT NULL AND created_at >= :since
        GROUP BY provider, model
        ORDER BY provider, model
        """,
        {"since": since},
    )

    return {
        "period_days": days,
        "providers": [dict(r) for r in rows],
    }


# ── Product popularity ───────────────────────────────────────

async def record_product_inquiries(products: list[dict]) -> None:
    """Increment daily counters for resolved products without storing customer queries."""
    safe_by_sku = {}
    for product in products:
        sku = str(product.get("parent_sku") or product.get("sku") or "").strip()[:120]
        if sku:
            safe_by_sku[sku] = {
                "sku": sku,
                "product_name": str(product.get("product_name") or "").strip()[:300],
            }
    safe_products = list(safe_by_sku.values())
    if not safe_products:
        return
    try:
        await db.execute(
            """
            INSERT INTO product_analytics (date, sku, product_name, times_asked)
            SELECT CURRENT_DATE, item->>'sku', item->>'product_name', 1
            FROM jsonb_array_elements(CAST(:products AS jsonb)) AS item
            ON CONFLICT (date, sku) DO UPDATE
            SET product_name = EXCLUDED.product_name,
                times_asked = product_analytics.times_asked + 1
            """,
            {"products": json.dumps(safe_products, ensure_ascii=False)},
        )
    except Exception as e:
        logger.error("Failed to record product inquiry analytics: %s", e)


async def get_popular_products(days: int = 30) -> list[dict]:
    """
    Find the most asked-about resolved catalog products.
    """
    since = date.today() - timedelta(days=days)

    rows = await db.fetch_all(
        """
        SELECT sku,
               MAX(product_name) AS product_name,
               SUM(times_asked) AS times_asked,
               SUM(times_ordered) AS times_ordered,
               SUM(revenue) AS revenue
        FROM product_analytics
        WHERE date >= :since
        GROUP BY sku
        ORDER BY times_asked DESC, product_name
        LIMIT 20
        """,
        {"since": since},
    )

    return [
        {
            "sku": row["sku"],
            "product_name": row["product_name"] or row["sku"],
            "query": row["product_name"] or row["sku"],
            "size_filter": None,
            "times_asked": int(row["times_asked"] or 0),
            "times_ordered": int(row["times_ordered"] or 0),
            "revenue": float(row["revenue"] or 0),
        }
        for row in rows
    ]


# ── Daily aggregate builder ──────────────────────────────────

async def build_daily_aggregate(target_date: date | None = None):
    """
    Compute daily analytics aggregates and store in daily_analytics table.
    Called by the scheduler at end of day or on demand.
    """
    if target_date is None:
        target_date = date.today() - timedelta(days=1)  # Yesterday

    for channel in ["whatsapp", "instagram"]:
        try:
            messages = await db.fetch_one(
                """
                SELECT
                    COUNT(*) FILTER (WHERE role = 'user') AS msgs_in,
                    COUNT(*) FILTER (WHERE role = 'assistant') AS msgs_out,
                    COUNT(DISTINCT customer_id) AS uniq
                FROM conversations
                WHERE created_at::date = :d AND channel = :ch
                """,
                {"d": target_date, "ch": channel},
            )
            await _upsert_daily_analytics(
                target_date,
                channel,
                "all",
                messages_in=messages["msgs_in"] if messages else 0,
                messages_out=messages["msgs_out"] if messages else 0,
                unique_customers=messages["uniq"] if messages else 0,
            )

            usage_rows = await db.fetch_all(
                """
                SELECT provider,
                       AVG(response_time_ms)::int AS avg_rt,
                       PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY response_time_ms)::int AS p95_rt,
                       COALESCE(SUM(input_tokens), 0) AS inp,
                       COALESCE(SUM(output_tokens), 0) AS out
                FROM usage_log
                WHERE created_at::date = :d AND channel = :ch
                  AND provider IN ('openai', 'anthropic')
                GROUP BY provider
                """,
                {"d": target_date, "ch": channel},
            )
            usage_by_provider = {row["provider"]: row for row in usage_rows}
            for provider in ["openai", "anthropic"]:
                usage = usage_by_provider.get(provider)
                await _upsert_daily_analytics(
                    target_date,
                    channel,
                    provider,
                    avg_response_ms=usage["avg_rt"] if usage else 0,
                    p95_response_ms=usage["p95_rt"] if usage else 0,
                    input_tokens=usage["inp"] if usage else 0,
                    output_tokens=usage["out"] if usage else 0,
                )
        except Exception as e:
            logger.error("Daily aggregate failed for %s/%s: %s", target_date, channel, e)

    logger.info(f"Daily analytics aggregated for {target_date}")


async def _upsert_daily_analytics(
    target_date: date,
    channel: str,
    provider: str,
    *,
    messages_in: int = 0,
    messages_out: int = 0,
    unique_customers: int = 0,
    avg_response_ms: int = 0,
    p95_response_ms: int = 0,
    input_tokens: int = 0,
    output_tokens: int = 0,
) -> None:
    await db.execute(
        """
        INSERT INTO daily_analytics
            (date, channel, provider, total_messages_in, total_messages_out,
             unique_customers, avg_response_ms, p95_response_ms,
             total_input_tokens, total_output_tokens)
        VALUES (:d, :ch, :prov, :mi, :mo, :uniq, :avg, :p95, :inp, :out)
        ON CONFLICT (date, channel, provider)
        DO UPDATE SET
            total_messages_in = EXCLUDED.total_messages_in,
            total_messages_out = EXCLUDED.total_messages_out,
            unique_customers = EXCLUDED.unique_customers,
            avg_response_ms = EXCLUDED.avg_response_ms,
            p95_response_ms = EXCLUDED.p95_response_ms,
            total_input_tokens = EXCLUDED.total_input_tokens,
            total_output_tokens = EXCLUDED.total_output_tokens
        """,
        {
            "d": target_date,
            "ch": channel,
            "prov": provider,
            "mi": int(messages_in or 0),
            "mo": int(messages_out or 0),
            "uniq": int(unique_customers or 0),
            "avg": int(avg_response_ms or 0),
            "p95": int(p95_response_ms or 0),
            "inp": int(input_tokens or 0),
            "out": int(output_tokens or 0),
        },
    )
