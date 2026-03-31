# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

AI-powered sales chatbot ("VS Chatbot" / "Eva") for a Venezuelan Victoria's Secret resale business. Handles customer conversations on WhatsApp and Instagram DMs in Spanish, with a Telegram admin bot, broadcast campaigns, analytics/A/B testing, and a web admin dashboard. Supports hot-swapping between OpenAI and Anthropic as LLM providers via direct SDK calls (no LangChain).

**Multi-store platform:** The system supports multiple independent store deployments managed from a centralized Master Control Plane (`master/`). Each store runs this same app with its own database, API keys, and channels. The master service monitors all stores, manages encrypted credentials, controls LLM provider/model selection per store, tracks platform-wide AI costs, and can push env vars to Railway + trigger redeploys.

## Commands

```bash
# Run store app locally
cd store && uvicorn app.main:app --reload --port 8000

# Run master control plane locally
cd master && uvicorn app.main:app --reload --port 9000

# Production (via Procfile in each service)
uvicorn app.main:app --host 0.0.0.0 --port $PORT
```

No tests or linting configured. Python 3.12.

### Local testing endpoints (store app — port 8000)

- `http://localhost:8000/test/ui` — Browser-based chat UI simulating WhatsApp conversations through the full AI pipeline (no Meta APIs needed)
- `http://localhost:8000/test/catalog` — View loaded products from Google Sheets
- `http://localhost:8000/health` — System health status
- `http://localhost:8000/admin/dashboard` — Web admin panel with dark mode (5 tabs: Resumen, Clientes, Pedidos, Broadcasts, Configuracion). Requires `ADMIN_PASSWORD` (via `?password=` on first visit — sets HTTP-only cookie and redirects to clean URL). All `/admin/` API routes also require auth via Bearer header or session cookie.

### Local testing endpoints (master — port 9000)

- `http://localhost:9000/test/ui` — Browser-based test UI with quick checks, seed data, API tester
- `http://localhost:9000/dashboard?token=SECRET` — Master dashboard (store overview with platform costs, per-store API key management, LLM config, usage/costs with time-range toggles, grouped credentials, Railway deploy, audit log). On first visit with `?token=`, sets HTTP-only cookie and redirects to clean URL.
- `http://localhost:9000/test/seed` — Create 3 sample stores with fake credentials (localhost only)
- `http://localhost:9000/test/db-check` — Verify master DB connectivity (localhost only)
- `http://localhost:9000/test/crypto?value=hello` — Test encryption round-trip (localhost only)

```bash
# Test chat via curl
curl -X POST http://localhost:8000/test/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "Hola, tienen pijamas?"}'
```

### Minimal .env for local testing

Only `OPENAI_API_KEY`, `DATABASE_URL`, `GOOGLE_SHEETS_CREDENTIALS_B64`, `PRODUCT_SHEET_ID`, `TELEGRAM_BOT_TOKEN`, and `TELEGRAM_ADMIN_CHAT_ID` are required. All Meta/Instagram/Anthropic fields default to empty strings and the app still starts.

## Architecture

**Entry point:** `store/app/main.py` — FastAPI app with lifespan that connects DB, initializes LLM providers, loads product catalog from Google Sheets, and starts the APScheduler background scheduler.

**Webhook channels:** WhatsApp (`store/app/webhooks/whatsapp.py`), Instagram (`store/app/webhooks/instagram.py`), Telegram admin bot (`store/app/admin/telegram_bot.py` at `/webhooks/telegram`).

**Request flow (WhatsApp/Instagram):**
1. Meta sends webhook → `store/app/webhooks/{whatsapp,instagram}.py` normalizes the message
2. Normalized message → `store/app/ai/engine.py` (central orchestrator)
3. Engine builds prompt (`store/app/ai/prompts.py` + `store/prompts/system_prompt.md`), calls active LLM provider, executes tool calls in a loop (max 6 rounds)
4. Response sent back via `store/app/channels/{whatsapp,instagram}_sender.py`

**Key modules:**
- `store/app/ai/providers/` — Provider abstraction with `base.py` (LLMResponse dataclass + abstract base), `openai_provider.py`, `anthropic_provider.py`. Provider-agnostic tool definitions live in `store/app/ai/functions.py` (JSON Schema format); each provider converts them to its native format.
- `store/app/ai/vision.py` — Payment screenshot analysis via LLM vision API
- `store/app/crm/` — Customer, conversation, and order management (all DB-backed via Supabase PostgreSQL)
- `store/app/catalog/sheets.py` — Google Sheets product catalog with in-memory cache and periodic refresh
- `store/app/broadcast/` — Campaign system: `api.py` (CRUD + reset endpoints), `sender.py` (WhatsApp template messages with crash recovery), `scheduler.py` (APScheduler jobs for scheduled sends + catalog refresh)
- `store/app/catalog/pdf_generator.py` — Generates a branded PDF product catalog from the Google Sheets data using fpdf2
- `store/app/admin/` — Settings API, web dashboard with dark mode (HTML served from `dashboard.py`, CSS in `store/app/static/css/dashboard.css`, JS in `store/app/static/js/dashboard.js`), Telegram bot (`telegram_bot.py` with 22 commands), notification helpers (`notify.py`), analytics API
- `store/app/analytics.py` — Tracks response times, fallback usage, conversion funnels, product popularity
- `store/app/db.py` — Async DB wrapper using `databases` library with a 60-second settings cache
- `store/app/test_endpoint.py` — `/test/ui` chat UI + `/test/chat` API for local testing without Meta APIs

**Background scheduler** (`store/app/broadcast/scheduler.py`): 5 APScheduler jobs — catalog refresh, broadcast execution, daily analytics aggregation (1 AM), token usage reminders, catalog PDF auto-refresh.

**Database:** Supabase PostgreSQL. Schema in `store/migrations/001_schema.sql` (run manually via Supabase SQL Editor). Tables: customers, conversations, orders, broadcasts, settings, usage_log, daily_analytics, product_analytics.

**Config:** `store/app/config.py` uses pydantic-settings to load from `store/.env`. All secrets are env vars. Multi-store fields: `admin_password` (protects store dashboard), `system_prompt_override` (replaces prompt template file), and `llm_managed_externally` (when True, locks LLM controls in store dashboard/Telegram/API — managed from master instead).

### Master Control Plane (`master/`)

Separate FastAPI service for managing multiple store deployments. Has its own database, requirements, and Procfile. Both services live as subdirectories of the same repo — Railway root directory is set to `store/` for the store service and `master/` for the master service.

**Entry point:** `master/app/main.py` — FastAPI app with lifespan that connects to the master DB and starts a background health check loop (asyncio task, not APScheduler).

**Key modules:**
- `master/app/stores/api.py` — CRUD for stores + credentials + stats + Railway deploy + audit log. All endpoints require Bearer token auth.
- `master/app/stores/crypto.py` — Fernet symmetric encryption for credentials at rest. Uses `ENCRYPTION_KEY` from `master/.env`.
- `master/app/stores/railway.py` — Railway GraphQL API client: upsert env vars, trigger redeploys, get service/deployment/environment info.
- `master/app/stores/health.py` — Periodic health checker that pings each store's `/health` endpoint.
- `master/app/stores/models.py` — Pydantic request models (StoreCreate, StoreUpdate, CredentialSet).
- `master/app/auth.py` — Bearer token auth. Checks `Authorization` header or `?token=` query param against `MASTER_SECRET_KEY`.
- `master/app/dashboard/router.py` — Serves master dashboard HTML (auth-protected).
- `master/app/test_endpoint.py` — `/test/ui`, `/test/seed`, `/test/reset`, `/test/db-check`, `/test/crypto`, `/test/railway-check`.

**Stats aggregation:** The `GET /api/stores/{id}/stats` endpoint connects directly to each store's Supabase DB (decrypting the URL from the master DB) and queries `conversations`, `orders`, `customers`, and `settings` tables. Concurrency is capped by `STORE_STATS_MAX_CONCURRENT` to avoid exhausting Supabase session pooler slots.

**Database:** Separate Supabase project. Migration: `master/migrations/001_master_schema.sql`. Tables: `stores`, `store_credentials`, `master_audit_log`.

**Config:** `master/app/config.py` uses pydantic-settings. Key vars: `DATABASE_URL`, `MASTER_SECRET_KEY`, `ENCRYPTION_KEY`, `RAILWAY_API_TOKEN` (optional), `HEALTH_CHECK_INTERVAL_SECONDS`, `STORE_STATS_MAX_CONCURRENT`.

## Security

- **Store admin auth:** All `/admin/settings/`, `/admin/broadcasts/`, and `/admin/analytics/` API routes require authentication via `ADMIN_PASSWORD` (Bearer header or `admin_session` HTTP-only cookie). The `require_admin` dependency in `store/app/admin/auth.py` enforces this. Without `ADMIN_PASSWORD` set, admin routes are blocked unless `DEBUG=true`.
- **Master auth:** All `/api/stores/` routes require Bearer token (`MASTER_SECRET_KEY`). Token comparisons use `hmac.compare_digest` (timing-safe). The master dashboard also accepts a `master_session` HTTP-only cookie.
- **Cookie-based sessions:** Both dashboards accept a password/token via query param on first visit, set an HTTP-only secure cookie, and redirect to the clean URL (stripping the secret from the URL bar, browser history, and logs).
- **Test endpoints:** Store test routes (`/test/`) are only available when `DEBUG=true`. Master test routes are only available when `APP_BASE_URL` contains `localhost` or `127.0.0.1`.
- **Rate limiting:** Both apps use `slowapi` — store app: 60 req/min, master: 30 req/min per IP.
- **CORS:** Restricted to the app's own origin (`APP_BASE_URL`). Only `GET/POST/PUT/DELETE` with `Authorization` and `Content-Type` headers.
- **Security headers:** Both apps set `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `X-XSS-Protection`, `Referrer-Policy`. In production, also `Strict-Transport-Security` and `Content-Security-Policy`.
- **Error sanitization:** Unhandled exceptions return a generic 500 in production (details are logged server-side). In development/localhost, the full error propagates for debugging.
- **SQL column whitelisting:** The master `PUT /api/stores/{id}` endpoint explicitly whitelists allowed column names to prevent SQL injection through dynamic field names.

## Key design decisions

- The AI system prompt is in `store/prompts/system_prompt.md` (Spanish-language, sales-focused). The Python code in `store/app/ai/prompts.py` injects dynamic context (catalog, customer history, order status) into it.
- With <200 products, the entire catalog is stuffed into the system prompt — no RAG or vector database needed.
- Settings (provider, model, temperature, max_tokens, max_conversation_history, fallback, A/B test, ai_enabled, catalog_pdf_interval_hours) are stored in the DB `settings` table and cached for 60s. Use `db.invalidate_settings_cache()` after writes. All settings are configurable from the dashboard, Telegram bot, and REST API.
- Tool calls are provider-agnostic: defined once in `store/app/ai/functions.py`, converted per-provider. Adding a new tool means adding it there and handling it in `store/app/ai/engine.py`. Current tools: `check_inventory`, `tag_customer`, `create_order`, `update_payment_status`, `escalate_to_human`, `send_interactive_buttons`, `send_catalog_pdf`.
- **Tool call loop** (`store/app/ai/engine.py`): `MAX_TOOL_ROUNDS = 6`. The loop processes **one tool call per iteration** — if the LLM returns multiple tool calls in one response, only the first is executed and the while loop re-evaluates afterward. This prevents stale history from being passed to subsequent `continue_after_tool` calls. On the final round (`rounds == MAX_TOOL_ROUNDS`), tools are withheld (`tools=None`) so the model is forced to produce a text response instead of another tool call. If `send_interactive_buttons` was called and the model returned no text, the `body_text` of the interactive payload is used as the reply.
- **Inventory privacy:** `_tool_check_inventory` returns `in_stock` (boolean) only — never the raw `stock` count. This prevents the LLM from revealing exact inventory levels to customers. System prompt rule 13 also explicitly prohibits outputting raw JSON, tool results, or technical metadata.
- WhatsApp supports interactive buttons; Instagram uses quick replies. The engine returns an `interactive` dict that the channel sender interprets.
- A/B testing assigns new customers randomly to a provider; existing customers keep their assignment.
- Global AI pause (`ai_enabled` setting) and per-customer escalation (`conversation_state = 'escalated'`) both suppress auto-replies. Messages are stored and the owner is notified via Telegram only once (first unanswered message), not on every subsequent message.
- Customer shipping addresses are saved on the customer record after order creation (`last_shipping_address`, `last_shipping_city`, `last_shipping_method`). The AI offers to reuse the saved address for returning customers.
- OpenAI newer models require `max_completion_tokens` instead of `max_tokens` (changed in `openai_provider.py`).
- Dashboard dark mode uses Tailwind CDN with `darkMode: 'class'` config. The `tailwind.config` must be set after the CDN `<script>` loads (not before, or `tailwind` is undefined). Custom component dark styles (`.dark .card`, etc.) live in `store/app/static/css/dashboard.css`. The `dark` class is toggled on `<html>` via `toggleDarkMode()` in `store/app/static/js/dashboard.js`.
- Dashboard settings tab exposes all configurable settings: LLM provider/model/temperature/max_tokens/conversation_history, fallback provider/model/auto-enable, A/B testing toggle, catalog PDF interval, and AI pause. These map to `PUT /admin/settings/{key}` calls.
- Broadcast execution wraps the send loop in try/except — if it crashes after setting status to `'sending'`, it auto-sets status to `'failed'`. A `POST /{id}/reset` endpoint resets stuck broadcasts back to `'draft'`. The dashboard shows a "Resetear" button for broadcasts in `sending` or `failed` status.
- The `databases` library returns record objects that support `[]` bracket access but not `.get()`. Use `record["key"]` with a conditional fallback, not `record.get("key", default)`.
- JSONB queries with the `databases` library must use `CAST(:param AS jsonb)` instead of `:param::jsonb` because the `::` cast syntax conflicts with SQLAlchemy's `:param` bind parameter syntax.
- **Multi-store: separate deployments, not multi-tenant.** Each store is a full independent deployment of this app with its own `.env` and database. The master service is a separate FastAPI app (not a router on the store app). This gives true data isolation and means a bug in one store doesn't affect others.
- **System prompt override:** If the `SYSTEM_PROMPT_OVERRIDE` env var is set, `store/app/ai/prompts.py` uses its value instead of reading `store/prompts/system_prompt.md`. The override must use the same `{store_name}`, `{product_catalog}`, etc. placeholders.
- **Centralized LLM control:** When `LLM_MANAGED_EXTERNALLY=true` is set on a store, the store dashboard hides the LLM provider/model/temperature/fallback/A/B controls, the settings API rejects writes to those keys (403), and the Telegram `/provider` and `/abmode` commands respond with "managed by admin". The Master Control Plane reads/writes LLM settings directly to each store's DB via `GET/PUT /api/stores/{id}/llm-settings`. This gives the platform operator full control over which provider and model each store uses, and lets them see aggregated costs via `GET /api/stores/llm-costs/aggregate`.

## API endpoints

### Store app (port 8000)

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

### Master control plane (port 9000)

```
Health:         GET /, GET /health
Dashboard:      GET /dashboard?token=SECRET
Stores:         GET/POST /api/stores/, GET/PUT/DELETE /api/stores/{id}
Credentials:    GET/POST /api/stores/{id}/credentials, DELETE /api/stores/{id}/credentials/{key}
Stats:          GET /api/stores/{id}/stats
LLM Control:    GET/PUT /api/stores/{id}/llm-settings, GET /api/stores/{id}/llm-usage?days=N
                GET /api/stores/llm-costs/aggregate?days=N
Railway:        GET /api/stores/{id}/railway/status, POST /api/stores/{id}/deploy
Audit:          GET /api/stores/audit/log
Testing:        GET /test/ui, GET /test/db-check, GET /test/crypto, POST /test/seed
                DELETE /test/reset, GET /test/railway-check
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
- **Migrations are manual:** Run SQL files from `store/migrations/` (001 through 004) directly in the Supabase SQL Editor. There is no migration framework.
- **Database connection:** Direct Supabase connection (port 5432) may be unreachable from some networks. Use the Session Pooler URL (port 6543) instead.
- **OpenAI max_tokens:** Newer OpenAI models (gpt-5.x) require `max_completion_tokens` instead of `max_tokens`. This is already handled in `openai_provider.py`.
- **Master migrations are manual too:** Run `master/migrations/001_master_schema.sql` in the master Supabase project's SQL Editor. Separate project from any store.
- **Master health checker uses asyncio, not APScheduler.** The master's background health loop is a simple `asyncio.create_task` with `asyncio.sleep`, unlike the store app which uses APScheduler.
- **Supabase session pooler concurrency:** The master opens a short-lived DB connection to each store when fetching stats. With the store app also holding pool slots, this can exceed Supabase's session pooler limits. `STORE_STATS_MAX_CONCURRENT` (default 1) caps parallel stats queries.
- **Master credentials are Fernet-encrypted.** All values in `store_credentials` are encrypted at rest. The `ENCRYPTION_KEY` must not change after stores are registered, or credentials become unrecoverable.
- **Railway API uses GraphQL.** The `master/app/stores/railway.py` client talks to `backboard.railway.com/graphql/v2`. The deploy flow: upsert variables → redeploy service. Both require a `RAILWAY_API_TOKEN` (account-level, from railway.app > Settings > Tokens).
- **LLM model costs are duplicated.** The `_MODEL_COSTS` dict in `master/app/stores/api.py` must match the `cost_per_m_tokens` values in the store app's `store/app/ai/providers/__init__.py`. When adding new models, update both.
- **LLM_MANAGED_EXTERNALLY must be deployed as an env var.** It's read from the store's `.env`, not from the DB. Set it via the master's credential management + Railway deploy flow (`LLM_MANAGED_EXTERNALLY=true`).
- **Cost endpoints support `?days=N`.** Both `GET /api/stores/{id}/llm-usage` and `GET /api/stores/llm-costs/aggregate` accept `?days=1` (today, default), `?days=7`, or `?days=30`. Max 90 days. The cutoff is computed in Python and passed as a query parameter to avoid SQL dialect issues across Supabase instances.
- **Master dashboard credential grouping.** API keys (OPENAI/ANTHROPIC) have a dedicated panel with status badges. Other credentials are grouped by category: Channels, Infrastructure, Customization, Other. The grouping is purely frontend — the backend stores all credentials the same way.
