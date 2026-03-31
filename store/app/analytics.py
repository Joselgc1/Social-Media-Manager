"""
Analytics module.
Provides conversion metrics, response time tracking, product popularity,
per-provider performance comparison, and A/B test analysis.
"""

import json
import logging
import random
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

async def get_popular_products(days: int = 30) -> list[dict]:
    """
    Find the most asked-about products based on check_inventory calls
    logged in conversation function_calls.
    """
    since = date.today() - timedelta(days=days)

    # Parse function_calls JSONB to extract product queries
    rows = await db.fetch_all(
        """
        SELECT
            fc_elem->>'args' as args_json,
            COUNT(*) as times_asked
        FROM conversations,
             jsonb_array_elements(function_calls::jsonb) as fc_elem
        WHERE function_calls IS NOT NULL
          AND fc_elem->>'name' = 'check_inventory'
          AND created_at >= :since
        GROUP BY fc_elem->>'args'
        ORDER BY times_asked DESC
        LIMIT 20
        """,
        {"since": since},
    )

    products = []
    for r in rows:
        try:
            args = json.loads(r["args_json"]) if r["args_json"] else {}
            products.append({
                "query": args.get("product_query", "unknown"),
                "size_filter": args.get("size"),
                "times_asked": r["times_asked"],
            })
        except (json.JSONDecodeError, TypeError):
            pass

    return products


# ── A/B test analysis ────────────────────────────────────────

async def get_ab_test_results(days: int = 14) -> dict:
    """
    Compare performance between providers for A/B-tested customers.
    Only includes customers with an ab_provider assignment.
    """
    since = date.today() - timedelta(days=days)

    rows = await db.fetch_all(
        """
        SELECT
            c.ab_provider,
            COUNT(DISTINCT c.id) as customers,
            COUNT(DISTINCT o.id) as orders,
            COALESCE(SUM(o.total), 0) as revenue,
            COUNT(DISTINCT o.id) FILTER (WHERE o.payment_status IN ('proof_received', 'confirmed')) as paid_orders
        FROM customers c
        LEFT JOIN orders o ON o.customer_id = c.id AND o.created_at >= :since
        WHERE c.ab_provider IS NOT NULL
          AND c.first_contact >= :since
        GROUP BY c.ab_provider
        """,
        {"since": since},
    )

    results = {}
    for r in rows:
        prov = r["ab_provider"]
        custs = r["customers"]
        results[prov] = {
            "customers": custs,
            "orders": r["orders"],
            "paid_orders": r["paid_orders"],
            "revenue": float(r["revenue"]),
            "conversion_rate": f"{(r['paid_orders']/custs*100):.1f}%" if custs > 0 else "0%",
            "avg_order_value": f"${(float(r['revenue'])/r['orders']):.2f}" if r["orders"] > 0 else "$0",
        }

    # Response time comparison for A/B groups
    rt_rows = await db.fetch_all(
        """
        SELECT
            u.provider,
            AVG(u.response_time_ms)::int as avg_ms,
            COUNT(*) as calls
        FROM usage_log u
        JOIN customers c ON u.customer_id = c.id
        WHERE c.ab_provider IS NOT NULL
          AND u.response_time_ms IS NOT NULL
          AND u.created_at >= :since
        GROUP BY u.provider
        """,
        {"since": since},
    )

    response_times = {r["provider"]: {"avg_ms": r["avg_ms"], "calls": r["calls"]} for r in rt_rows}

    return {
        "period_days": days,
        "groups": results,
        "response_times": response_times,
        "note": "Customers are randomly assigned to a provider on first contact when A/B mode is enabled.",
    }


async def assign_ab_group(customer_id: str) -> str:
    """
    Randomly assign a new customer to an A/B test group.
    Returns the assigned provider name.
    """
    provider = random.choice(["openai", "anthropic"])

    await db.execute(
        "UPDATE customers SET ab_provider = :provider WHERE id = :id",
        {"provider": provider, "id": customer_id},
    )

    return provider


# ── Daily aggregate builder ──────────────────────────────────

async def build_daily_aggregate(target_date: date | None = None):
    """
    Compute daily analytics aggregates and store in daily_analytics table.
    Called by the scheduler at end of day or on demand.
    """
    if target_date is None:
        target_date = date.today() - timedelta(days=1)  # Yesterday

    for channel in ["whatsapp", "instagram"]:
        for provider in ["openai", "anthropic"]:
            try:
                # Message counts
                msgs = await db.fetch_one(
                    """
                    SELECT
                        COUNT(*) FILTER (WHERE c.role = 'user') as msgs_in,
                        COUNT(*) FILTER (WHERE c.role = 'assistant') as msgs_out,
                        COUNT(DISTINCT c.customer_id) as uniq
                    FROM conversations c
                    JOIN usage_log u ON u.customer_id = c.customer_id
                        AND u.created_at::date = :d AND u.provider = :prov
                    WHERE c.created_at::date = :d AND c.channel = :ch
                    """,
                    {"d": target_date, "ch": channel, "prov": provider},
                )

                # Response times
                rt = await db.fetch_one(
                    """
                    SELECT
                        AVG(response_time_ms)::int as avg_rt,
                        PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY response_time_ms)::int as p95_rt
                    FROM usage_log
                    WHERE created_at::date = :d AND channel = :ch AND provider = :prov
                      AND response_time_ms IS NOT NULL
                    """,
                    {"d": target_date, "ch": channel, "prov": provider},
                )

                # Token usage
                tokens = await db.fetch_one(
                    """
                    SELECT COALESCE(SUM(input_tokens), 0) as inp,
                           COALESCE(SUM(output_tokens), 0) as out
                    FROM usage_log
                    WHERE created_at::date = :d AND channel = :ch AND provider = :prov
                    """,
                    {"d": target_date, "ch": channel, "prov": provider},
                )

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
                        "d": target_date, "ch": channel, "prov": provider,
                        "mi": msgs["msgs_in"] if msgs else 0,
                        "mo": msgs["msgs_out"] if msgs else 0,
                        "uniq": msgs["uniq"] if msgs else 0,
                        "avg": rt["avg_rt"] if rt and rt["avg_rt"] else 0,
                        "p95": rt["p95_rt"] if rt and rt["p95_rt"] else 0,
                        "inp": tokens["inp"] if tokens else 0,
                        "out": tokens["out"] if tokens else 0,
                    },
                )

            except Exception as e:
                logger.error(f"Daily aggregate failed for {target_date}/{channel}/{provider}: {e}")

    logger.info(f"Daily analytics aggregated for {target_date}")
