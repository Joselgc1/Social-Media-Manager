"""
Analytics API endpoints.
Conversion funnels, response times, product popularity, and A/B test results.
"""

from datetime import date, timedelta
from fastapi import APIRouter, Depends
from app import analytics
from app import db
from app.admin.auth import require_admin

router = APIRouter(prefix="/admin/analytics", tags=["analytics"], dependencies=[Depends(require_admin)])


@router.get("/conversion")
async def conversion_funnel(days: int = 7):
    """
    Conversion funnel: messaged -> inquired products -> placed order -> paid.
    Shows rates at each stage.
    """
    return await analytics.get_conversion_funnel(days=days)


@router.get("/response-times")
async def response_times(days: int = 7):
    """
    LLM response time stats by provider/model.
    Shows average, p50, p95, min, and max.
    """
    return await analytics.get_response_time_stats(days=days)


@router.get("/popular-products")
async def popular_products(days: int = 30):
    """
    Most frequently asked-about products based on check_inventory calls.
    """
    return await analytics.get_popular_products(days=days)


@router.get("/ab-test")
async def ab_test_results(days: int = 14):
    """
    A/B test comparison between LLM providers.
    Shows conversion rates, revenue, and response times per group.
    """
    return await analytics.get_ab_test_results(days=days)


@router.post("/build-daily")
async def build_daily(target_date: date | None = None):
    """
    Manually trigger daily aggregate computation.
    Defaults to yesterday. Useful for backfilling.
    """
    await analytics.build_daily_aggregate(target_date)
    return {"status": "ok", "date": str(target_date or "yesterday")}


@router.get("/daily")
async def daily_trends(days: int = 14):
    """
    Daily aggregated trends over the last N days.
    Returns one row per day with totals across channels and providers.
    """
    since = date.today() - timedelta(days=days)
    rows = await db.fetch_all(
        """
        SELECT date,
               SUM(total_messages_in) as messages_in,
               SUM(total_messages_out) as messages_out,
               SUM(unique_customers) as unique_customers,
               SUM(total_orders) as orders,
               SUM(total_revenue) as revenue,
               AVG(avg_response_ms)::int as avg_response_ms,
               SUM(estimated_cost_usd) as llm_cost
        FROM daily_analytics
        WHERE date >= :since
        GROUP BY date
        ORDER BY date ASC
        """,
        {"since": since},
    )

    return {
        "period_days": days,
        "data": [
            {
                "date": str(r["date"]),
                "messages_in": r["messages_in"] or 0,
                "messages_out": r["messages_out"] or 0,
                "unique_customers": r["unique_customers"] or 0,
                "orders": r["orders"] or 0,
                "revenue": float(r["revenue"] or 0),
                "avg_response_ms": r["avg_response_ms"] or 0,
                "llm_cost": float(r["llm_cost"] or 0),
            }
            for r in rows
        ],
    }
