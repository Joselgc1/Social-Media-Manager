# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

AI-powered sales chatbot ("VS Chatbot" / "Eva") for a Venezuelan Victoria's Secret resale business. Handles customer conversations on WhatsApp and Instagram DMs in Spanish, with a Telegram admin bot, broadcast campaigns, analytics/A/B testing, and a web admin dashboard. Supports hot-swapping between OpenAI and Anthropic as LLM providers via direct SDK calls (no LangChain).

## Commands

```bash
# Run locally
uvicorn app.main:app --reload --port 8000

# Production (via Procfile)
uvicorn app.main:app --host 0.0.0.0 --port $PORT
```

No tests or linting configured. Python 3.12.

### Local testing endpoints

- `http://localhost:8000/test/ui` — Browser-based chat UI simulating WhatsApp conversations through the full AI pipeline (no Meta APIs needed)
- `http://localhost:8000/test/catalog` — View loaded products from Google Sheets
- `http://localhost:8000/health` — System health status
- `http://localhost:8000/admin/dashboard` — Web admin panel with dark mode (5 tabs: Resumen, Clientes, Pedidos, Broadcasts, Configuracion). Dark mode persists via localStorage and auto-detects OS preference.

```bash
# Test chat via curl
curl -X POST http://localhost:8000/test/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "Hola, tienen pijamas?"}'
```

### Minimal .env for local testing

Only `OPENAI_API_KEY`, `DATABASE_URL`, `GOOGLE_SHEETS_CREDENTIALS_B64`, `PRODUCT_SHEET_ID`, `TELEGRAM_BOT_TOKEN`, and `TELEGRAM_ADMIN_CHAT_ID` are required. All Meta/Instagram/Anthropic fields default to empty strings and the app still starts.

## Architecture

**Entry point:** `app/main.py` — FastAPI app with lifespan that connects DB, initializes LLM providers, loads product catalog from Google Sheets, and starts the APScheduler background scheduler.

**Webhook channels:** WhatsApp (`app/webhooks/whatsapp.py`), Instagram (`app/webhooks/instagram.py`), Telegram admin bot (`app/admin/telegram_bot.py` at `/webhooks/telegram`).

**Request flow (WhatsApp/Instagram):**
1. Meta sends webhook → `app/webhooks/{whatsapp,instagram}.py` normalizes the message
2. Normalized message → `app/ai/engine.py` (central orchestrator)
3. Engine builds prompt (`app/ai/prompts.py` + `prompts/system_prompt.md`), calls active LLM provider, executes tool calls in a loop (max 3 rounds)
4. Response sent back via `app/channels/{whatsapp,instagram}_sender.py`

**Key modules:**
- `app/ai/providers/` — Provider abstraction with `base.py` (LLMResponse dataclass + abstract base), `openai_provider.py`, `anthropic_provider.py`. Provider-agnostic tool definitions live in `app/ai/functions.py` (JSON Schema format); each provider converts them to its native format.
- `app/ai/vision.py` — Payment screenshot analysis via LLM vision API
- `app/crm/` — Customer, conversation, and order management (all DB-backed via Supabase PostgreSQL)
- `app/catalog/sheets.py` — Google Sheets product catalog with in-memory cache and periodic refresh
- `app/broadcast/` — Campaign system: `api.py` (CRUD + reset endpoints), `sender.py` (WhatsApp template messages with crash recovery), `scheduler.py` (APScheduler jobs for scheduled sends + catalog refresh)
- `app/catalog/pdf_generator.py` — Generates a branded PDF product catalog from the Google Sheets data using fpdf2
- `app/admin/` — Settings API, web dashboard with dark mode (HTML served from `dashboard.py`, CSS in `app/static/css/dashboard.css`, JS in `app/static/js/dashboard.js`), Telegram bot (`telegram_bot.py` with 22 commands), notification helpers (`notify.py`), analytics API
- `app/analytics.py` — Tracks response times, fallback usage, conversion funnels, product popularity
- `app/db.py` — Async DB wrapper using `databases` library with a 60-second settings cache
- `app/test_endpoint.py` — `/test/ui` chat UI + `/test/chat` API for local testing without Meta APIs

**Background scheduler** (`app/broadcast/scheduler.py`): 5 APScheduler jobs — catalog refresh, broadcast execution, daily analytics aggregation (1 AM), token usage reminders, catalog PDF auto-refresh.

**Database:** Supabase PostgreSQL. Migrations in `migrations/` (run manually via Supabase SQL Editor). Tables: customers, conversations, orders, broadcasts, settings, usage_log, daily_analytics, product_analytics.

**Config:** `app/config.py` uses pydantic-settings to load from `.env`. All secrets are env vars.

## Key design decisions

- The AI system prompt is in `prompts/system_prompt.md` (Spanish-language, sales-focused). The Python code in `app/ai/prompts.py` injects dynamic context (catalog, customer history, order status) into it.
- With <200 products, the entire catalog is stuffed into the system prompt — no RAG or vector database needed.
- Settings (provider, model, temperature, max_tokens, max_conversation_history, fallback, A/B test, ai_enabled, catalog_pdf_interval_hours) are stored in the DB `settings` table and cached for 60s. Use `db.invalidate_settings_cache()` after writes. All settings are configurable from the dashboard, Telegram bot, and REST API.
- Tool calls are provider-agnostic: defined once in `app/ai/functions.py`, converted per-provider. Adding a new tool means adding it there and handling it in `engine.py`. Current tools: `check_inventory`, `tag_customer`, `create_order`, `update_payment_status`, `escalate_to_human`, `send_interactive_buttons`, `send_catalog_pdf`.
- WhatsApp supports interactive buttons; Instagram uses quick replies. The engine returns an `interactive` dict that the channel sender interprets.
- A/B testing assigns new customers randomly to a provider; existing customers keep their assignment.
- Global AI pause (`ai_enabled` setting) and per-customer escalation (`conversation_state = 'escalated'`) both suppress auto-replies. Messages are stored and the owner is notified via Telegram only once (first unanswered message), not on every subsequent message.
- Customer shipping addresses are saved on the customer record after order creation (`last_shipping_address`, `last_shipping_city`, `last_shipping_method`). The AI offers to reuse the saved address for returning customers.
- OpenAI newer models require `max_completion_tokens` instead of `max_tokens` (changed in `openai_provider.py`).
- Dashboard dark mode uses Tailwind CDN with `darkMode: 'class'` config. The `tailwind.config` must be set after the CDN `<script>` loads (not before, or `tailwind` is undefined). Custom component dark styles (`.dark .card`, etc.) live in `dashboard.css`. The `dark` class is toggled on `<html>` via `toggleDarkMode()` in `dashboard.js`.
- Dashboard settings tab exposes all configurable settings: LLM provider/model/temperature/max_tokens/conversation_history, fallback provider/model/auto-enable, A/B testing toggle, catalog PDF interval, and AI pause. These map to `PUT /admin/settings/{key}` calls.
- Broadcast execution wraps the send loop in try/except — if it crashes after setting status to `'sending'`, it auto-sets status to `'failed'`. A `POST /{id}/reset` endpoint resets stuck broadcasts back to `'draft'`. The dashboard shows a "Resetear" button for broadcasts in `sending` or `failed` status.
- The `databases` library returns record objects that support `[]` bracket access but not `.get()`. Use `record["key"]` with a conditional fallback, not `record.get("key", default)`.
- JSONB queries with the `databases` library must use `CAST(:param AS jsonb)` instead of `:param::jsonb` because the `::` cast syntax conflicts with SQLAlchemy's `:param` bind parameter syntax.

## API endpoints

```
Webhooks:       GET/POST /webhooks/whatsapp, /webhooks/instagram, POST /webhooks/telegram
Health:         GET /, GET /health
Settings:       GET /admin/settings/, GET /admin/settings/providers, PUT /admin/settings/{key}
                POST /admin/settings/switch-provider, GET /admin/settings/usage-summary
                GET /admin/settings/stats/conversations, POST /admin/settings/telegram/setup-webhook
                POST /admin/settings/instagram/setup-ice-breakers, POST /admin/settings/instagram/subscribe-page
                POST /admin/settings/catalog/generate-pdf, GET /admin/settings/catalog/pdf-status
                GET /admin/settings/catalog/download-pdf
Customers:      GET /admin/settings/customers, GET /admin/settings/orders
                POST /admin/settings/customers/{id}/resolve, POST /admin/settings/customers/resolve-all
                GET /admin/settings/customers/{id}/tags, POST /admin/settings/customers/{id}/tags
                DELETE /admin/settings/customers/{id}/tags/{tag}
Dashboard:      GET /admin/dashboard
Broadcasts:     POST /admin/broadcasts/create, /preview, GET /list, POST /{id}/send, POST /{id}/reset
Analytics:      GET /admin/analytics/conversion, /response-times, /popular-products, /ab-test, /daily
                POST /admin/analytics/build-daily
Testing:        GET /test/ui, POST /test/chat, GET /test/catalog
```

## Telegram admin commands

```
/start /stats /customers /orders /order /resolve /provider
/broadcast /send /preview /settings /usage /conversion
/performance /products /abtest /abmode /catalogpdf
/ai /tags /tag
```

## Business context

- Venezuelan resale business: ~$5k/month revenue, 6-60 messages/day
- Accepted payments: Zelle, Binance, Zinli, Bolívares (tasa Binance)
- Shipping: MRW or Zoom (Venezuelan couriers), delivery included in prices
- Product catalog managed in Google Sheets so the store owner can update from his phone
- Target hosting budget: $50-150/month total (Railway $5-10, LLM APIs ~$0.15-0.30/day)

## Gotchas

- **Settings cache:** DB settings are cached for 60s. After writing to the `settings` table, call `db.invalidate_settings_cache()` or changes won't be visible until the cache expires.
- **Telegram webhook:** Must be registered once via `POST /admin/settings/telegram/setup-webhook` before the Telegram bot responds. This sets the webhook URL using `APP_BASE_URL`.
- **Catalog refresh:** The Google Sheets catalog is loaded into memory at startup and refreshed periodically by APScheduler. A stale catalog won't update until the next refresh cycle or a server restart.
- **Migrations are manual:** Run SQL files from `migrations/` (001 through 004) directly in the Supabase SQL Editor. There is no migration framework.
- **Database connection:** Direct Supabase connection (port 5432) may be unreachable from some networks. Use the Session Pooler URL (port 6543) instead.
- **OpenAI max_tokens:** Newer OpenAI models (gpt-5.x) require `max_completion_tokens` instead of `max_tokens`. This is already handled in `openai_provider.py`.
