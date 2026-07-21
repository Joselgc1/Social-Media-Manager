# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

AI-powered sales chatbot ("VS Chatbot" / "Eva") for a Venezuelan Victoria's Secret resale business. Handles customer conversations on WhatsApp and Instagram DMs in Spanish, with a Telegram admin bot, broadcast campaigns, analytics, and a web admin dashboard. Supports hot-swapping between OpenAI and Anthropic as LLM providers via direct SDK calls (no LangChain).

**Multi-store platform:** The system supports multiple independent store deployments managed from a centralized Master Control Plane (`master/`). Each store runs this same app with its own database, API keys, and channels. The master service monitors all stores, manages encrypted credentials, reads/writes each store's runtime settings directly in the store DB, tracks platform-wide AI costs, and can push env vars to Railway + trigger redeploys.

## Commands

```bash
# Run store app locally
cd store && uvicorn app.main:app --reload --port 8000

# Run master control plane locally
cd master && uvicorn app.main:app --reload --port 9000

# Production (via Procfile in each service)
uvicorn app.main:app --host 0.0.0.0 --port $PORT
```

Python 3.12. Tests use pytest + pytest-asyncio (see `tests/`). Linting uses ruff.

### Local testing endpoints (store app — port 8000)

- `http://localhost:8000/test/ui` — Browser-based chat UI simulating WhatsApp conversations through the full AI pipeline (no Meta APIs needed)
- `http://localhost:8000/test/history?sender=test_user_1` — View conversation history for a simulated customer
- `DELETE http://localhost:8000/test/reset?sender=test_user_1` — Reset a simulated customer and all related test data
- `http://localhost:8000/test/catalog` — View loaded products from Google Sheets
- `http://localhost:8000/health` — System health status
- `http://localhost:8000/admin/login` — Store admin login page. Successful login sets the `admin_session` HTTP-only cookie and redirects to `/admin/dashboard`.
- `http://localhost:8000/admin/dashboard` — Web admin panel with dark mode (5 tabs: Resumen, Clientes, Pedidos, Broadcasts, Configuracion). Requires a valid session cookie or Bearer auth on the underlying `/admin/` APIs.

### Local testing endpoints (master — port 9000)

- `http://localhost:9000/test/ui` — Browser-based test UI with quick checks, seed data, API tester
- `http://localhost:9000/login` — Master dashboard login page. Successful login sets the `master_session` HTTP-only cookie and redirects to `/dashboard`.
- `http://localhost:9000/dashboard` — Master dashboard (store overview with platform costs, per-store API key management, shared AI runtime settings, scheduled-job settings, usage/costs with time-range toggles, conversation viewer, grouped credentials, Railway deploy, audit log).
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

For local debug, either `OPENAI_API_KEY` or `ANTHROPIC_API_KEY` plus `DATABASE_URL`, `GOOGLE_SHEETS_CREDENTIALS_B64`, and `PRODUCT_SHEET_ID` are the core minimum. Meta, Kommo, and Telegram fields may stay empty when `DEBUG=true` unless you are testing that specific channel backend. In production (`DEBUG=false`), startup validation requires `ADMIN_PASSWORD`, at least one LLM key, and the complete credential set for the selected `CHANNEL_BACKEND`: Meta mode requires the full WhatsApp config (`META_APP_SECRET`, `WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_VERIFY_TOKEN`); Kommo mode requires the Kommo private integration, private-message Salesbot, webhook secret, and AI Mode field/enum vars. Instagram and Telegram remain optional but must be all-or-nothing if enabled.

## Development workflow

### Running locally

- Store: `/run-store` or `cd store && DEBUG=true uvicorn app.main:app --reload --port 8000`
- Master: `/run-master` or `cd master && uvicorn app.main:app --reload --port 9000`

### Testing

- Run all tests: `python3 -m pytest tests/ -v`
- Generate tests for a module: `/write-tests <module path>`
- Test the chatbot: `/test-chat <message>`
- Install dev deps: `pip install -r requirements-dev.txt`

### Code quality

- Lint: `ruff check store/ master/`
- Review changes: `/review` (checks project-specific patterns)

### Deploying

- `/deploy store`, `/deploy master`, or `/deploy both`
- Push to main triggers Railway auto-deploy

### Available skills and commands

- `/review` — code review with project-specific checklist (auto-invoked)
- `/add-tool` — scaffold a new AI tool/function (auto-invoked)
- `/new-endpoint` — scaffold a new API endpoint (auto-invoked)
- `/write-tests` — generate pytest tests (auto-invoked)
- `/tune-prompt` — iterate on the system prompt (auto-invoked)
- `/deploy` — deploy to Railway (manual)
- `/db-query` — read-only SQL against store DB (manual)
- `/analyze-conversations` — chatbot performance analysis (manual, runs in forked subagent)
- `/run-store`, `/run-master` — start services locally
- `/test-chat` — test the chatbot via curl
- `/check-health` — check service health

## Common tasks

### Add a new AI tool

Use `/add-tool <description>` or modify manually:

1. `store/app/ai/tools/definitions.py` — add the provider-agnostic JSON Schema definition
2. `store/app/ai/tools/` — add or update the handler module
3. `store/app/ai/tools/executor.py` — wire the handler in dispatch
4. `store/app/ai/agents/` — add the tool to only the agent allowlists that should use it
5. Add tests proving unauthorized agents cannot call the tool

### Add a new setting

1. Add the default in `store/app/runtime_settings.py` and seed it in `store/migrations/001_schema.sql` for fresh databases; `store/app/db.py::ensure_default_settings()` backfills missing runtime defaults at startup for existing databases
2. If the setting should sync with `master/`, add it to `master/app/stores/runtime_settings.py` and `RuntimeSettingsUpdate`
3. Handle validation in `store/app/admin/settings.py` and `master/app/stores/api.py`
4. Expose it in the relevant dashboard JS (`store/app/static/js/dashboard.js` and/or `master/app/static/js/master_dashboard.js`)
5. Use it in the relevant module via `await db.get_settings()`

### Add a new endpoint

Use `/new-endpoint <description>` or follow the patterns in:

- `store/app/admin/settings.py` for store admin endpoints
- `master/app/stores/api.py` for master API endpoints
- Always add auth, rate limiting, and register the router in `main.py`

### Update LLM model costs

When adding new models, update BOTH files:

- `store/app/ai/providers/__init__.py` (`cost_per_m_tokens`)
- `master/app/stores/api.py` (`_MODEL_COSTS` dict)

## Architecture

**Entry point:** `store/app/main.py` — FastAPI app with lifespan that connects DB, initializes LLM providers, loads product catalog from Google Sheets, and starts the APScheduler background scheduler.

**Webhook channels:** WhatsApp (`store/app/webhooks/whatsapp.py`), Instagram (`store/app/webhooks/instagram.py`), Kommo (`store/app/webhooks/kommo.py` when `CHANNEL_BACKEND=kommo`), Telegram admin bot (`store/app/admin/telegram_bot.py` at `/webhooks/telegram`).

**Channel backend switch:** `CHANNEL_BACKEND=meta` registers direct Meta WhatsApp/Instagram webhooks and uses Meta sender modules. `CHANNEL_BACKEND=kommo` registers `/webhooks/kommo/events/{webhook_secret}` and `/webhooks/kommo/salesbot`, does not require Meta credentials, does not register Meta webhooks, and sends customer replies by launching/resuming Kommo Salesbot. Keep Meta modules for rollback.

**Request flow (WhatsApp/Instagram):**

1. Meta sends webhook → `store/app/webhooks/{whatsapp,instagram}.py` normalizes the message
2. Normalized message → `store/app/ai/engine.py` (central orchestrator)
3. Engine applies pause/escalation/hostility/payment-proof guards, resolves `ai_orchestration_mode`, and either uses legacy, shadow, or multi-agent orchestration
4. In multi-agent mode, deterministic route guards run first; `store/app/ai/llm_router.py` is used only for ambiguous messages
5. The selected agent prompt is composed from `store/prompts/shared/` plus `store/prompts/agents/`, direct SDK providers are called, and `store/app/ai/runner.py` enforces per-agent tool allowlists
6. Response sent back via `store/app/channels/{whatsapp,instagram}_sender.py`

In Kommo mode, Kommo sends general DM webhooks to `store/app/webhooks/kommo.py`; the app persists durable jobs, launches the private-message Salesbot, receives the Salesbot `widget_request` callback, runs the existing AI engine, then posts a data-only Salesbot continuation (`data.status`, `data.message`) to the validated Kommo `return_url`. Public Instagram comments use Kommo's native comment-triggered Salesbot; its authenticated widget callback creates the durable `instagram_comment` job directly, and the backend never launches that Salesbot.

**Key modules:**

- `store/app/ai/orchestrator.py` — Resolves orchestration mode and coordinates legacy, shadow, and multi-agent runs.
- `store/app/ai/agents/` — Agent definitions for `legacy`, `sales`, `checkout`, and `support`, including allowed tools.
- `store/app/ai/runner.py` — Runs an agent, enforces tool allowlists, logs tool rejections/handoffs, and executes tool loops.
- `store/app/ai/tools/` — Provider-agnostic tool schemas, handlers, registry, and executor. `store/app/ai/functions.py` remains as compatibility surface for provider tool conversion.
- `store/app/ai/providers/` — Provider abstraction with `base.py` (LLMResponse dataclass + abstract base), `openai_provider.py`, `anthropic_provider.py`. Each provider converts shared tool definitions to its native format.
- `store/app/ai/payment/` — Deterministic payment proof verification and response formatting after LLM vision extraction.
- `store/app/integrations/kommo/` — Kommo private integration client, JWT/return URL validation, webhook parser, Salesbot response mapper, AI Mode state decisions, and durable PostgreSQL-backed Kommo jobs.
- `store/app/ai/vision.py` — Payment screenshot analysis via LLM vision API
- `store/app/crm/` — Customer, conversation, and order management (all DB-backed via Supabase PostgreSQL)
- `store/app/catalog/sheets.py` — Google Sheets product catalog with in-memory cache and periodic refresh
- `store/app/broadcast/` — Campaign system: `api.py` (CRUD + reset endpoints), `sender.py` (WhatsApp template messages with crash recovery), `scheduler.py` (APScheduler jobs for scheduled sends + catalog refresh)
- `store/app/catalog/pdf_generator.py` — Generates a branded PDF product catalog from the Google Sheets data using fpdf2
- `store/app/admin/` — Settings API, web dashboard with dark mode (HTML served from `dashboard.py`, CSS in `store/app/static/css/dashboard.css`, JS in `store/app/static/js/dashboard.js`), Telegram bot (`telegram_bot.py` with 22 commands), notification helpers (`notify.py`), analytics API
- `store/app/analytics.py` — Tracks response times, fallback usage, conversion funnels, product popularity, and non-sensitive AI run metadata in `ai_run_logs`
- `store/app/db.py` — Async DB wrapper using `databases` library with version-aware settings cache invalidation
- `store/app/test_endpoint.py` — `/test/ui` chat UI + `/test/chat` API for local testing without Meta APIs

**Background scheduler** (`store/app/broadcast/scheduler.py`): 5 business jobs — catalog refresh, broadcast execution, daily analytics aggregation, token usage reminders, catalog PDF auto-refresh — plus an internal sync job that keeps APScheduler timings aligned with DB settings from `master/`. In Kommo mode it also runs durable Kommo job processing and stale-job recovery.

**Database:** Supabase PostgreSQL. Fresh installs use the consolidated schema in `store/migrations/001_schema.sql` (run manually via Supabase SQL Editor). This single store schema includes Kommo tables, conversation sessions, and AI run observability. Tables include customers, conversations, orders, broadcasts, settings, usage_log, ai_run_logs, daily_analytics, product_analytics, conversation_sessions, customer_channel_mappings, kommo_message_jobs, and kommo_message_receipts.

**Config:** `store/app/config.py` uses pydantic-settings to load from `store/.env`. All secrets are env vars. Multi-store fields: `admin_password` (protects store dashboard), `system_prompt_override` (replaces prompt template file), `llm_managed_externally` (when True, locks LLM controls in store dashboard/Telegram/API — managed from master instead), and `ai_orchestration_mode` (optional env default; DB setting wins). Kommo env vars are exactly: `CHANNEL_BACKEND`, `KOMMO_SUBDOMAIN`, `KOMMO_ACCESS_TOKEN`, `KOMMO_INTEGRATION_ID`, `KOMMO_INTEGRATION_SECRET`, `KOMMO_SALESBOT_ID`, `KOMMO_WEBHOOK_SECRET`, `KOMMO_AI_MODE_FIELD_ID`, `KOMMO_AI_ACTIVE_ENUM_ID`, `KOMMO_AI_HUMAN_ENUM_ID`, `KOMMO_AI_PAUSED_ENUM_ID`, `KOMMO_DEFAULT_RESPONSIBLE_USER_ID`.

### Master Control Plane (`master/`)

Separate FastAPI service for managing multiple store deployments. Has its own database, requirements, and Procfile. Both services live as subdirectories of the same repo — Railway root directory is set to `store/` for the store service and `master/` for the master service.

**Entry point:** `master/app/main.py` — FastAPI app with lifespan that connects to the master DB and starts asyncio background tasks for store health checks and idle store DB pool cleanup (not APScheduler).

**Key modules:**

- `master/app/stores/api.py` — CRUD for stores + credentials + stats + shared runtime settings + store conversation viewer data + Railway deploy + audit log. Browser access uses the login cookie; API clients can still use Bearer auth. Cross-store DB reads use cached per-store pools with idle cleanup.
- `master/app/stores/crypto.py` — Fernet symmetric encryption for credentials at rest. Uses `ENCRYPTION_KEY` from `master/.env`.
- `master/app/stores/railway.py` — Railway GraphQL API client: upsert env vars, trigger redeploys, get service/deployment/environment info.
- `master/app/stores/health.py` — Periodic health checker that pings each store's `/health` endpoint.
- `master/app/stores/models.py` — Pydantic request models (StoreCreate, StoreUpdate, CredentialSet).
- `master/app/auth.py` — Bearer token auth plus the master session cookie.
- `master/app/dashboard/router.py` — Serves the master login page, logout route, and dashboard HTML.
- `master/app/test_endpoint.py` — `/test/ui`, `/test/seed`, `/test/reset`, `/test/db-check`, `/test/crypto`, `/test/railway-check`.

**Stats aggregation:** The `GET /api/stores/{id}/stats` endpoint reads each store's Supabase DB directly (decrypting the URL from the master DB) and queries `conversations`, `orders`, `customers`, and `settings` tables. Store DB connections are cached in small pools (`min_size=1`, `max_size=3`) and cleaned after 2 minutes idle. Concurrency is capped by `STORE_STATS_MAX_CONCURRENT` to avoid exhausting Supabase session pooler slots.

**Database:** Separate Supabase project. Migration: `master/migrations/001_master_schema.sql`. Tables: `stores`, `store_credentials`, `master_audit_log`.

**Config:** `master/app/config.py` uses pydantic-settings. Key vars: `DATABASE_URL`, `MASTER_SECRET_KEY`, `ENCRYPTION_KEY`, `RAILWAY_API_TOKEN` (optional), `HEALTH_CHECK_INTERVAL_SECONDS`, `STORE_STATS_MAX_CONCURRENT` (default 5).

## Security

- **Store admin auth:** All `/admin/settings/`, `/admin/broadcasts/`, and `/admin/analytics/` API routes require authentication via `ADMIN_PASSWORD` (Bearer header or `admin_session` HTTP-only cookie). Browser sessions are created via `GET/POST /admin/login` and cleared via `POST /admin/logout`.
- **Master auth:** All `/api/stores/` routes require Bearer token (`MASTER_SECRET_KEY`) or the `master_session` cookie. Browser sessions are created via `GET/POST /login` and cleared via `POST /logout`.
- **Cookie-based sessions:** Both dashboards now use normal login forms. Secrets are no longer accepted in query params.
- **Test endpoints:** Store test routes (`/test/`) are only available when `DEBUG=true`. Master test routes are only available when `APP_BASE_URL` contains `localhost` or `127.0.0.1`.
- **Rate limiting:** Both apps use `slowapi` — store app: 60 req/min, master: 30 req/min per IP.
- **CORS:** Restricted to the app's own origin (`APP_BASE_URL`). Only `GET/POST/PUT/DELETE` with `Authorization` and `Content-Type` headers.
- **Security headers:** Both apps set `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `X-XSS-Protection`, `Referrer-Policy`. In production, also `Strict-Transport-Security` and `Content-Security-Policy`.
- **Error sanitization:** Unhandled exceptions return a generic 500 in production (details are logged server-side). In development/localhost, the full error propagates for debugging.
- **SQL column whitelisting:** The master `PUT /api/stores/{id}` endpoint explicitly whitelists allowed column names to prevent SQL injection through dynamic field names.

## Key design decisions

- The legacy AI system prompt is in `store/prompts/system_prompt.md`. Multi-agent prompts are composed from `store/prompts/shared/` plus `store/prompts/agents/{legacy,sales,checkout,support}.md`; the ambiguity router prompt is `store/prompts/agents/router.md`.
- With <200 products, the entire catalog is stuffed into the system prompt — no RAG or vector database needed.
- Dashboard-managed AI runtime settings (provider, model, temperature, max tokens, conversation history, fallback, ai_enabled, ai_orchestration_mode) are stored in the store DB `settings` table. Scheduler settings (`catalog_refresh_minutes`, `broadcast_check_interval_minutes`, `catalog_pdf_interval_hours`, `token_reminder_*`, `daily_analytics_*`) live there too. The store app uses version-aware cache invalidation, and `master/` reads/writes those rows through `GET/PUT /api/stores/{id}/settings`, so AI settings stay in sync after refresh or save and scheduler timings are applied automatically by the store within about a minute.
- Store payment methods are persisted separately in the same `settings` table under `payment_methods`. They are edited only from the store dashboard via `GET/PUT /admin/settings/payment-methods`, not from `master/`.
- Store contact and commerce settings also live in the store `settings` table. `store_phone_number` is used only in public Instagram comment fallback replies; exchange-rate settings answer rate questions like `¿a qué tasa recibes?`; the prompt and order backend use `order_discount_percent` and `order_discount_threshold_usd` for automatic subtotal-based discounts.
- Order creation normalizes item prices from the catalog and applies the configured discount automatically only when subtotal is strictly greater than the threshold. Do not make the LLM alter item unit prices to simulate a discount.
- Tool calls are provider-agnostic: schemas live in `store/app/ai/tools/definitions.py`, handlers live under `store/app/ai/tools/`, and the executor dispatches them with per-agent allowlists from `store/app/ai/agents/`. Adding a new tool means adding schema, handler, executor dispatch, allowlist membership, and authorization tests.
- `ai_orchestration_mode` supports `legacy`, `shadow`, and `multi_agent`. Precedence is valid DB runtime setting, valid `AI_ORCHESTRATION_MODE` env default, then hard-coded `legacy`. `legacy` is the safe rollback mode; `shadow` logs route comparisons while legacy serves the response; `multi_agent` serves via specialist agents with legacy fallback.
- Routing precedence is deliberate: global AI pause and per-customer escalation suppress replies, hostility escalates before LLM calls, payment-proof images go through deterministic verification, active checkout sessions stay sticky, clear purchase/support/sales keywords route deterministically, and the LLM router is used only for ambiguous messages.
- Payment screenshots are analyzed by LLM vision, then `store/app/ai/payment/verifier.py` deterministically validates open order, amount, method, recipient, and completed status before changing payment state. Do not let an LLM create orders from proofs or mark payment status directly.
- Rapid inbound messages are buffered briefly in `store/app/webhooks/inbound_buffer.py` and merged into a single AI turn per customer/channel. This reduces double replies when the customer sends two messages back-to-back.
- **Tool call loop:** `MAX_TOOL_ROUNDS = 6`. Tool execution remains one tool per iteration so stale history is not passed to later `continue_after_tool` calls. On the final round, tools are withheld so the model must produce text. If `send_interactive_buttons` was called and the model returned no text, the interactive payload `body_text` is used as the reply.
- **Inventory privacy:** `_tool_check_inventory` returns `in_stock` (boolean) only — never the raw `stock` count. This prevents the LLM from revealing exact inventory levels to customers. System prompt rule 13 also explicitly prohibits outputting raw JSON, tool results, or technical metadata.
- WhatsApp supports interactive buttons; Instagram uses quick replies. The engine returns an `interactive` dict that the channel sender interprets.
- Global AI pause (`ai_enabled` setting) and per-customer escalation (`conversation_state = 'escalated'`) both suppress auto-replies. Messages are stored and the owner is notified via Telegram only once (first unanswered message), not on every subsequent message.
- In Kommo mode, per-conversation automation source of truth is the Kommo lead `AI Mode` field. `AI Active` maps to local `active`; `Human` and `Paused` map to local `escalated`. Empty AI Mode must be initialized to `AI Active` successfully before automatic replies are sent. Never overwrite existing Human/Paused automatically.
- Store-dashboard manual reactivation in Kommo mode must sync Kommo first: set lead `AI Mode` to `AI Active`, re-read and verify the enum, then set local `conversation_state='active'` and clear local history. Single-customer failures return sanitized `502`; resolve-all reports per-customer `activated`, `local_only`, or `failed`.
- Kommo Salesbot callbacks include a JWT and `return_url`. Validate HS256 with `KOMMO_INTEGRATION_SECRET`, expiration, issuer/subdomain, and `client_uid`/`client_uuid` when present. Validate `return_url` strictly against `https://{KOMMO_SUBDOMAIN}.kommo.com` with no userinfo, IPs, localhost, deceptive suffixes, redirects, or unexpected ports before posting the Salesbot continuation.
- Kommo jobs are durable in `kommo_message_jobs`. Do not depend only on `BackgroundTasks`, `asyncio.create_task`, or in-memory buffers. The in-process task may accelerate handling after DB commit, but PostgreSQL is the source of truth. Ready jobs should attempt a Salesbot continuation even on discard/error paths; `delivery_unknown` means a continuation was attempted but Kommo acceptance could not be confirmed and must be manually reconciled before retrying.
- Kommo jobs carry `interaction_type`: `private_message` for WhatsApp/Instagram DMs and `instagram_comment` for public comment replies. The parser classifies the confirmed native Instagram comment general-webhook shape (`origin=instagram`, `message_type=comment`) as `instagram_comment`; `handle_kommo_events()` logs and ignores that event so it does not create a private-message job or launch `KOMMO_SALESBOT_ID`. Public comment replies are deterministic: only price/stock are answered from resolved post/product context that maps to exactly one catalog product; all other public comments return the DM/WhatsApp fallback based on `store_phone_number`.
- Customer shipping addresses are saved on the customer record after order creation (`last_shipping_address`, `last_shipping_city`, `last_shipping_method`). The AI offers to reuse the saved address for returning customers.
- OpenAI newer models require `max_completion_tokens` instead of `max_tokens` (changed in `openai_provider.py`).
- Dashboard dark mode uses Tailwind CDN with `darkMode: 'class'` config. The `tailwind.config` must be set after the CDN `<script>` loads (not before, or `tailwind` is undefined). Custom component dark styles (`.dark .card`, etc.) live in `store/app/static/css/dashboard.css`. The `dark` class is toggled on `<html>` via `toggleDarkMode()` in `store/app/static/js/dashboard.js`.
- Store dashboard settings tab exposes the locally configurable settings: LLM provider/model/temperature/max_tokens/conversation_history, orchestration mode, fallback provider/model/auto-enable, dynamic payment methods, public store phone number, exchange-rate reference, automatic order discount percent/threshold, catalog PDF generation/download, and AI pause. Most settings map to `PUT /admin/settings/{key}` calls; payment methods use `GET/PUT /admin/settings/payment-methods`. Scheduler timings are master-only.
- Broadcast execution wraps the send loop in try/except — if it crashes after setting status to `'sending'`, it auto-sets status to `'failed'`. A `POST /{id}/reset` endpoint resets stuck broadcasts back to `'draft'`. The dashboard shows a "Resetear" button for broadcasts in `sending` or `failed` status.
- In `CHANNEL_BACKEND=kommo`, direct WhatsApp broadcast delivery is rejected before marking the broadcast as sending. Use Kommo broadcasts or approved Kommo WhatsApp template flows. Meta-mode broadcast behavior is preserved.
- The `databases` library returns record objects that support `[]` bracket access but not `.get()`. Use `record["key"]` with a conditional fallback, not `record.get("key", default)`.
- JSONB queries with the `databases` library must use `CAST(:param AS jsonb)` instead of `:param::jsonb` because the `::` cast syntax conflicts with SQLAlchemy's `:param` bind parameter syntax.
- **Multi-store: separate deployments, not multi-tenant.** Each store is a full independent deployment of this app with its own `.env` and database. The master service is a separate FastAPI app (not a router on the store app). This gives true data isolation and means a bug in one store doesn't affect others.
- **System prompt override:** If the `SYSTEM_PROMPT_OVERRIDE` env var is set, `store/app/ai/prompts.py` uses its value instead of reading `store/prompts/system_prompt.md`. The override must use the same placeholders as the default template, including `{payment_method_names_text}`, `{payment_methods_block}`, `{exchange_rate_block}`, and `{order_discount_block}`.
- **Centralized runtime settings:** `master/` reads/writes shared AI settings and scheduler timings directly in each store DB via `GET/PUT /api/stores/{id}/settings`. That includes AI config plus the store's APScheduler timings, but not payment methods, accepted exchange rate, or order discount settings. When `LLM_MANAGED_EXTERNALLY=true` is set on a store, the store dashboard hides LLM-only controls and rejects writes to those keys (403), but payment methods and other non-LLM store settings remain editable locally.
- **AI run observability:** `ai_run_logs` stores non-sensitive metadata only: orchestration mode, selected agent, route intent/source/confidence, provider/model, token counts, response time, tool names, tool rounds, and boolean flags for handoff/fallback/escalation/shadow/legacy fallback. Do not log payment credentials, raw image contents, full addresses, customer message text, tool arguments, or raw tool results.
- **Deployment shape:** Keep the store service single-instance/single-worker in this phase because broadcasts and scheduled jobs run in-process.

## API endpoints

### Store app (port 8000)

```text
Webhooks:       Meta mode: GET/POST /webhooks/whatsapp, /webhooks/instagram
                Kommo mode: POST /webhooks/kommo/events/{webhook_secret}, POST /webhooks/kommo/salesbot
                Telegram: POST /webhooks/telegram
Health:         GET /, GET /health
Settings:       GET /admin/settings/, GET /admin/settings/providers, PUT /admin/settings/{key}
                GET /admin/settings/payment-methods, PUT /admin/settings/payment-methods
                GET /admin/settings/kommo/status, POST /admin/settings/kommo/test
                POST /admin/settings/switch-provider, GET /admin/settings/usage-summary
                GET /admin/settings/stats/conversations, POST /admin/settings/telegram/setup-webhook
                POST /admin/settings/instagram/setup-ice-breakers, POST /admin/settings/instagram/subscribe-page
                POST /admin/settings/catalog/generate-pdf, GET /admin/settings/catalog/pdf-status
                GET /admin/settings/catalog/download-pdf
Customers:      GET /admin/settings/customers, PUT /admin/settings/customers/{id}
                DELETE /admin/settings/customers/{id}
                POST /admin/settings/customers/{id}/resolve, POST /admin/settings/customers/resolve-all
                GET /admin/settings/customers/{id}/tags, POST /admin/settings/customers/{id}/tags
                DELETE /admin/settings/customers/{id}/tags/{tag}
Orders:         GET /admin/settings/orders, GET /admin/settings/orders/{id}
                PUT /admin/settings/orders/{id}, DELETE /admin/settings/orders/{id}
Dashboard:      GET /admin/login, POST /admin/login, POST /admin/logout, GET /admin/dashboard
                GET /admin/orders/{order_id}
Broadcasts:     POST /admin/broadcasts/create, /preview, GET /list, POST /{id}/send, POST /{id}/reset
Analytics:      GET /admin/analytics/conversion, /response-times, /popular-products, /daily
                POST /admin/analytics/build-daily
Testing:        GET /test/ui, POST /test/chat, GET /test/catalog
                GET /test/history, DELETE /test/reset
```

### Master control plane (port 9000)

```text
Health:         GET /, GET /health
Dashboard:      GET /login, POST /login, POST /logout, GET /dashboard
Stores:         GET/POST /api/stores/, GET/PUT/DELETE /api/stores/{id}
Credentials:    GET/POST /api/stores/{id}/credentials, DELETE /api/stores/{id}/credentials/{key}
Stats:          GET /api/stores/{id}/stats
Runtime:        GET/PUT /api/stores/{id}/settings, GET /api/stores/{id}/llm-usage?days=N
                GET/PUT /api/stores/{id}/llm-settings, GET /api/stores/{id}/conversations
                GET /api/stores/llm-costs/aggregate?days=N
Conversations:  GET /api/stores/{id}/conversations
Railway:        GET /api/stores/{id}/railway/status, POST /api/stores/{id}/deploy
Audit:          GET /api/stores/audit/log
Testing:        GET /test/ui, GET /test/db-check, GET /test/crypto, POST /test/seed
                DELETE /test/reset, GET /test/railway-check
```

## Telegram admin commands

```text
/start /stats /customers /orders /order /resolve /provider
/broadcast /send /preview /settings /usage /conversion
/performance /products /catalogpdf
/ai /tags /tag
```

## Business context

- Venezuelan resale business: ~$5k/month revenue, 6-60 messages/day
- Accepted payments: store-defined; the owner can add/remove methods in the dashboard
- Shipping: MRW or Zoom (Venezuelan couriers), always `cobro a destino`
- Product catalog managed in Google Sheets so the store owner can update from his phone
- Target hosting budget: $50-150/month total (Railway $5-10, LLM APIs ~$0.15-0.30/day)

## Gotchas

- **Settings cache:** The store app keeps a version-aware cache of DB settings. After writing to the `settings` table locally, still call `db.invalidate_settings_cache()` so the current process picks the change up immediately; master-side writes are detected automatically on the next read.
- **Orchestration rollout:** Keep production stores on `legacy` until transcript regressions and `shadow` logs look clean. Roll back by setting `ai_orchestration_mode=legacy` in the store DB from either dashboard.
- **Telegram webhook:** Must be registered once via `POST /admin/settings/telegram/setup-webhook` before the Telegram bot responds. This sets the webhook URL using `APP_BASE_URL`.
- **Catalog refresh:** The Google Sheets catalog is loaded into memory at startup and refreshed periodically by APScheduler. A stale catalog won't update until the next refresh cycle or a server restart.
- **Migrations are manual:** For fresh store databases, run `store/migrations/001_schema.sql` directly in the Supabase SQL Editor. There is no migration framework.
- **Database connection:** Direct Supabase connection (port 5432) may be unreachable from some networks. Use the Session Pooler URL (port 6543) instead.
- **OpenAI max_tokens:** Newer OpenAI models (gpt-5.x) require `max_completion_tokens` instead of `max_tokens`. This is already handled in `openai_provider.py`.
- **Master migrations are manual too:** Run `master/migrations/001_master_schema.sql` in the master Supabase project's SQL Editor. Separate project from any store.
- **Master background jobs use asyncio, not APScheduler.** The master runs health checks and idle store DB pool cleanup with `asyncio.create_task` and `asyncio.sleep`, unlike the store app which uses APScheduler.
- **Supabase session pooler concurrency:** The master caches per-store DB pools for stats/settings/usage/conversation reads and closes idle pools after 2 minutes. With the store app also holding pool slots, this can exceed Supabase's session pooler limits. `STORE_STATS_MAX_CONCURRENT` (default 5, minimum 1) caps parallel cross-DB queries; lower it for smaller Supabase pool limits.
- **Master credentials are Fernet-encrypted.** All values in `store_credentials` are encrypted at rest. The `ENCRYPTION_KEY` must not change after stores are registered, or credentials become unrecoverable.
- **Railway API uses GraphQL.** The `master/app/stores/railway.py` client talks to `backboard.railway.com/graphql/v2`. The deploy flow: upsert variables → redeploy service. Both require a `RAILWAY_API_TOKEN` (account-level, from railway.app > Settings > Tokens).
- **LLM model costs are duplicated.** The `_MODEL_COSTS` dict in `master/app/stores/api.py` must match the `cost_per_m_tokens` values in the store app's `store/app/ai/providers/__init__.py`. When adding new models, update both.
- **LLM_MANAGED_EXTERNALLY must be deployed as an env var.** It's read from the store's `.env`, not from the DB. Set it via the master's credential management + Railway deploy flow (`LLM_MANAGED_EXTERNALLY=true`).
- **Cost endpoints support `?days=N`.** Both `GET /api/stores/{id}/llm-usage` and `GET /api/stores/llm-costs/aggregate` accept `?days=1` (today, default), `?days=7`, or `?days=30`. Max 90 days. The cutoff is computed in Python and passed as a query parameter to avoid SQL dialect issues across Supabase instances.
- **Master dashboard credential grouping.** API keys (OPENAI/ANTHROPIC) have a dedicated panel with status badges. Other credentials are grouped by category: Channel Backend, Kommo, Meta Channels, Telegram, Infrastructure, Customization, Other. The grouping is purely frontend — the backend stores all credentials the same way.
- **Kommo widget package.** `store/kommo-widget/` contains the private Salesbot widget. Build it with `python3 build_widget.py --widget-code <kommo-widget-code>`; upload the generated ZIP manually. The widget uses `widget_request`, then `goto` question step `1`, and no secrets or production domains. Configure the callback once in integration settings as `backend_url`; the Salesbot block URL is optional and only overrides the global URL when valid. The widget exposes `success` and `fail` exits. Increment `widget.version` for every upload, then disable/re-enable the integration or refresh Kommo and hard refresh the browser if stale widget fields remain. A bad first upload may require a fresh widget code/private integration per Kommo widget update behavior.
- **Unsupported or gated in Kommo mode:** native rich-media delivery through Kommo, automatic human takeover from outgoing/native phone replies, Kommo broadcasts from this backend, and verified production Salesbot/comment delivery without testing in a real Kommo account.
