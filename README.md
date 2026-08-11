# VS Chatbot - AI Sales Assistant

AI-powered sales chatbot for Instagram DMs and WhatsApp, built for a Venezuelan Victoria's Secret resale business. Supports both OpenAI and Anthropic as LLM providers, with hot-swapping from the admin panel. Features a PDF product catalog, global AI pause/resume, per-customer escalation, customer address memory, admin tag management, dynamic store-defined payment methods, database-backed exchange-rate settings, sortable dashboard tables, and a dark mode admin dashboard. Channel transport can run in direct Meta mode or Kommo mode.

The sales flow is tuned for Venezuelan operations: shipments are offered through `MRW` or `Zoom` with `cobro a destino`, owners can update the daily accepted exchange rate from the store dashboard, and the customer-facing catalog PDF does not expose internal SKU or stock columns.

Dashboard-managed runtime settings live in each store's `settings` table. That includes shared AI configuration, `ai_orchestration_mode`, and master-managed scheduler timings such as `catalog_refresh_minutes`, `broadcast_check_interval_minutes`, `catalog_pdf_interval_hours`, and the daily cron times. Store payment methods are persisted separately in the same table under `payment_methods` and are managed only from the store dashboard. When a store is connected to `master/`, both dashboards read and write the shared AI rows, and the master dashboard manages the scheduler rows.

**Multi-store support:** A Master Control Plane (`master/`) lets you manage multiple independent store deployments from a single dashboard — each with its own database, API keys, channel backend, and Telegram bot. See [master/DEPLOYMENT.md](master/DEPLOYMENT.md) for the multi-store setup guide.

## Documentation Index

| Topic | Authoritative guide |
| :--- | :--- |
| Quick start and project overview | [Quick Start](#quick-start) and [Architecture](#architecture) below |
| Store deployment | [`store/DEPLOYMENT.md`](store/DEPLOYMENT.md) |
| Kommo setup and migration | [`docs/KOMMO_MIGRATION.md`](docs/KOMMO_MIGRATION.md) |
| Instagram comments, Stories, and content mappings | [`store/INSTAGRAM DEPLOY.md`](store/INSTAGRAM%20DEPLOY.md) |
| Product images, media, and WhatsApp PDF delivery | [Message Types Handled](#message-types-handled) and [Kommo media guide](docs/KOMMO_MIGRATION.md#hybrid-media-delivery) |
| Voice notes | [Message Types Handled](#message-types-handled) and [Kommo voice notes](docs/KOMMO_MIGRATION.md#inbound-voice-notes) |
| Railway and PostgreSQL | [`docs/RAILWAY_POSTGRES.md`](docs/RAILWAY_POSTGRES.md) |
| Production backup, restore, and rollback | [`docs/PRODUCTION_OPERATIONS.md`](docs/PRODUCTION_OPERATIONS.md) |
| Multi-store Master deployment | [`master/DEPLOYMENT.md`](master/DEPLOYMENT.md) |
| Kommo widget | [`store/kommo-widget/README.md`](store/kommo-widget/README.md) |
| AI and multi-agent architecture | [Multi-Agent Rollout](#multi-agent-rollout) |
| Security | [Security](#security) |
| Testing | [`store/DEPLOYMENT.md`](store/DEPLOYMENT.md#part-10-complete-testing-checklist) and [`PRESENTATION_RUNBOOK.md`](PRESENTATION_RUNBOOK.md) |

## Table of Contents

- [Quick Start](#quick-start)
- [Deployment Guides](#deployment-guides)
- [Railway PostgreSQL](#railway-postgresql)
- [Architecture](#architecture)
- [Webhook Endpoints](#webhook-endpoints)
- [Admin Endpoints](#admin-endpoints)
- [Instagram Setup](#instagram-setup-after-meta-app-review-approval)
- [Message Types Handled](#message-types-handled)
- [Admin Dashboard Features](#admin-dashboard-features)
- [Shared Runtime Settings](#shared-runtime-settings)
- [Multi-Agent Rollout](#multi-agent-rollout)
- [Security](#security)
- [Multi-store Architecture](#multi-store-architecture)

## Quick Start

```bash
# 1. Clone and install
git clone <your-repo-url>
cd Social-Media-Manager
python -m venv .venv
source .venv/bin/activate
pip install -r store/requirements.txt

# 2. Configure
cp store/.env.example store/.env
# Edit store/.env with your actual API keys and credentials

# 3. Set up the database and apply the idempotent migration
python store/scripts/migrate.py

# 4. Run locally
cd store
uvicorn app.main:app --reload --port 8000

# 5. Expose for webhook testing (in another terminal)
# Use ngrok or similar to get a public HTTPS URL
ngrok http 8000
```

## Deployment Guides

| Goal | Guide |
| :--- | :---- |
| Deploy one store in direct Meta mode | [`store/DEPLOYMENT.md`](store/DEPLOYMENT.md) |
| Deploy one store in Kommo mode | [`store/DEPLOYMENT.md`](store/DEPLOYMENT.md) plus [`docs/KOMMO_MIGRATION.md`](docs/KOMMO_MIGRATION.md) |
| Manage multiple stores from one dashboard | [`master/DEPLOYMENT.md`](master/DEPLOYMENT.md) |
| Build/upload the Kommo Salesbot widget | [`store/kommo-widget/README.md`](store/kommo-widget/README.md) |
| Back up, restore, migrate, roll back, and review retention | [`docs/PRODUCTION_OPERATIONS.md`](docs/PRODUCTION_OPERATIONS.md) |

Use `CHANNEL_BACKEND=meta` for direct Meta WhatsApp/Instagram webhooks. Use `CHANNEL_BACKEND=kommo` when Kommo owns the official channel integrations: WhatsApp uses Salesbot callbacks, while Instagram DMs use direct Talks/Chats API delivery by persisted `talk_id`.

## Railway PostgreSQL

Production runs on four Railway services in one project:

```text
Railway project
├── Store
├── StorePostgres
├── Master
└── MasterPostgres
```

Set `Store.DATABASE_URL=${{StorePostgres.DATABASE_URL}}` and `Master.DATABASE_URL=${{MasterPostgres.DATABASE_URL}}`. Each service runs its migration in Railway pre-deploy using the service-local `railway.toml`. See [Railway PostgreSQL Deployment](docs/RAILWAY_POSTGRES.md) for provisioning, Store registration, backups, and existing-data migration.

## Architecture

```txt
Customer (WhatsApp or Instagram DM)
    -> Meta webhook or Kommo official channel integration
        -> Normalize message (text, buttons, images, ice breakers, postbacks)
        -> Inbound buffer and deterministic guards
        -> AI Engine
            -> Orchestration resolver (legacy, shadow, multi_agent)
            -> Deterministic route guards, then ambiguity-only LLM router
            -> Selected agent prompt (legacy, sales, checkout, support)
            -> Direct SDK provider call (OpenAI or Anthropic)
            -> Tool executor with per-agent allowlists
            -> Response
        -> Send reply via Meta API or Kommo Salesbot
            -> WhatsApp: text, interactive buttons, templates
            -> Instagram DM: text, quick replies, and a product image when available

Store DB settings (source of truth for runtime settings)
    -> Store dashboard reads/writes locally
    -> Master dashboard reads/writes remotely through the store DB
    -> Changes become visible on the next refresh/save without redeploy
```

## Webhook Endpoints

Registered channel endpoints depend on `CHANNEL_BACKEND`.

Meta mode:

| Method | Path                   | Purpose                        |
| :----- | :--------------------- | :----------------------------- |
| GET    | `/webhooks/whatsapp`   | WhatsApp webhook verification  |
| POST   | `/webhooks/whatsapp`   | Receive WhatsApp messages      |
| GET    | `/webhooks/instagram`  | Instagram webhook verification |
| POST   | `/webhooks/instagram`  | Receive Instagram DMs          |

Kommo mode:

| Method | Path                                      | Purpose                                  |
| :----- | :---------------------------------------- | :--------------------------------------- |
| POST   | `/webhooks/kommo/events/{webhook_secret}` | Receive Kommo general webhook events     |
| POST   | `/webhooks/kommo/salesbot`                | Receive Salesbot `widget_request` calls  |

Optional signed Meta context alongside Kommo:

| Method   | Path                                  | Purpose                                      |
| :------- | :------------------------------------ | :------------------------------------------- |
| GET/POST | `/webhooks/meta/instagram-context`    | Verify/receive comment or Story context only |

This supplemental route is registered only in Kommo mode when `META_INSTAGRAM_CONTEXT_ENABLED=true` or `META_STORY_CONTEXT_ENABLED=true`. Kommo still sends every reply.

## Admin Endpoints

> Underlying `/admin/` APIs require authentication via `Authorization: Bearer ADMIN_PASSWORD` or the login-created session cookie. Dashboard pages use the session cookie. See **Security** below.

| Method | Path                                              | Purpose                                         |
| :----- | :------------------------------------------------ | :-----------------------------------------------|
| GET    | `/`                                               | Health check (no auth)                          |
| GET    | `/health`                                         | Detailed health status (no auth)                |
| GET    | `/admin/login`                                    | Store dashboard login page                      |
| POST   | `/admin/login`                                    | Create store admin session cookie               |
| POST   | `/admin/logout`                                   | Clear store admin session cookie                |
| GET    | `/admin/settings/`                                | View all settings                               |
| GET    | `/admin/settings/payment-methods`                  | View store payment methods                      |
| PUT    | `/admin/settings/payment-methods`                  | Update store payment methods                    |
| GET    | `/admin/settings/providers`                       | List available LLM providers and models         |
| GET    | `/admin/settings/kommo/status`                    | Safe Kommo configuration/job diagnostics         |
| POST   | `/admin/settings/kommo/test`                      | Safe read-only Kommo API checks                  |
| PUT    | `/admin/settings/{key}`                           | Update a setting                                |
| POST   | `/admin/settings/switch-provider`                 | Quick provider switch                           |
| GET    | `/admin/settings/usage-summary`                   | Today's token usage and cost estimate           |
| GET    | `/admin/settings/stats/conversations`             | Today's conversation and order stats            |
| POST   | `/admin/settings/telegram/setup-webhook`           | Register Telegram webhook                       |
| POST   | `/admin/settings/instagram/setup-ice-breakers`    | Configure Instagram Ice Breakers                |
| POST   | `/admin/settings/instagram/subscribe-page`        | Subscribe FB Page to webhooks                   |
| PUT    | `/admin/settings/ai_enabled`                      | Toggle AI on/off globally                       |
| POST   | `/admin/settings/catalog/generate-pdf`            | Generate product catalog PDF                    |
| GET    | `/admin/settings/catalog/pdf-status`              | Check PDF status                                |
| GET    | `/admin/settings/catalog/download-pdf`            | Download catalog PDF                            |
| GET    | `/admin/settings/orders`                          | List recent orders                              |
| GET    | `/admin/settings/orders/{id}`                     | Get order detail                                |
| PUT    | `/admin/settings/orders/{id}`                     | Update order payment/shipping state             |
| DELETE | `/admin/settings/orders/{id}`                     | Delete order                                    |
| GET    | `/admin/settings/customers`                       | List customers                                  |
| PUT    | `/admin/settings/customers/{id}`                  | Update customer state or channel                |
| DELETE | `/admin/settings/customers/{id}`                  | Delete customer                                 |
| POST   | `/admin/settings/customers/{id}/resolve`          | Resolve escalated customer                      |
| POST   | `/admin/settings/customers/resolve-all`           | Resolve all escalated customers                 |
| GET    | `/admin/settings/customers/{id}/tags`             | Get customer tags                               |
| POST   | `/admin/settings/customers/{id}/tags`             | Add tags to customer                            |
| DELETE | `/admin/settings/customers/{id}/tags/{tag}`       | Remove tag from customer                        |

## Switching LLM Providers

```bash
# Switch to Anthropic Claude Haiku (requires admin auth in production)
curl -X POST "http://localhost:8000/admin/settings/switch-provider?provider=anthropic" \
  -H "Authorization: Bearer YOUR_ADMIN_PASSWORD"

# Switch to OpenAI GPT-5.4 Nano
curl -X POST "http://localhost:8000/admin/settings/switch-provider?provider=openai" \
  -H "Authorization: Bearer YOUR_ADMIN_PASSWORD"

# Switch to a specific model
curl -X POST "http://localhost:8000/admin/settings/switch-provider?provider=openai&model=gpt-5.6-terra" \
  -H "Authorization: Bearer YOUR_ADMIN_PASSWORD"
```

> With `DEBUG=true` and no `ADMIN_PASSWORD` set, auth is skipped for local development.

## Instagram Setup (after Meta App Review approval)

This section applies to `CHANNEL_BACKEND=meta`. For Kommo mode, connect Instagram inside Kommo and see [docs/KOMMO_MIGRATION.md](docs/KOMMO_MIGRATION.md).

```bash
# 1. Subscribe your Facebook Page to messaging webhooks (once)
curl -X POST "http://localhost:8000/admin/settings/instagram/subscribe-page?page_id=YOUR_PAGE_ID"

# 2. Configure Ice Breakers (once, or whenever you want to update them)
curl -X POST "http://localhost:8000/admin/settings/instagram/setup-ice-breakers?ig_user_id=YOUR_IG_USER_ID"
```

## Message Types Handled

### WhatsApp

- Text messages
- Interactive button replies
- List replies
- Images (with captions)
- Direct Meta audio, video, documents, and stickers (represented as attachment placeholders; no transcription)
- Reactions (ignored)

### Instagram

- Text DMs
- Quick Reply callbacks
- Ice Breaker taps (converted to natural language for AI)
- Story replies and mentions
- Ad referrals (warm welcome acknowledging the ad)
- Image/media attachments
- Post/Reel shares
- Message deletions (acknowledged)
- Postbacks from buttons

### Kommo Mode

- WhatsApp text handling through the configured Kommo Salesbot.
- Instagram DM text and enabled product images are delivered directly through Kommo Talks/Chats API using the originating `talk_id`; there is intentionally no Instagram DM Salesbot fallback.
- The Kommo private integration's `Sending to external chats` permission is mandatory for every Instagram DM text reply as well as opted-in Chats API media. Existing authorizations may need access granted again after adding the scope; verify it manually before production because the read-only diagnostic cannot test sends. A missing-scope `403` never falls back to Salesbot.
- Public Instagram comment replies use Kommo's native comment-triggered Salesbot flow; the authenticated widget callback creates a durable `instagram_comment` job directly.
- Interactive choices become numbered text; URL actions become text plus URLs.
- Opted-in WhatsApp product images and catalog PDFs use Kommo Files API/cache plus Chats API; disabled WhatsApp media safely falls back to Salesbot behavior.
- Product images can be enabled for WhatsApp and Instagram under the global media kill switch. Catalog PDF delivery remains strictly WhatsApp-only.
- Incoming Kommo `voice` and `audio` attachments in private WhatsApp and Instagram DMs are downloaded safely and transcribed before the AI turn. This path requires `OPENAI_API_KEY` even when Anthropic is the active chat provider. Direct Meta audio is not transcribed.
- Durable jobs and outbound records track `delivery_unknown` when Salesbot or Chats API acceptance cannot be confirmed; inspect Kommo before manual retry.
- Accepted direct Instagram sends remain `waiting_for_delivery` until Kommo reports `delivered` or `seen`. The scheduler polls every 15 seconds and matching outgoing webhooks accelerate the next check; reconciliation never resends an accepted message, and assistant history is persisted only after confirmation.
- If Kommo definitively reports `error` for a requested product image, the backend sends one text-only Instagram fallback explaining the delivery issue and linking to WhatsApp. A valid `store_phone_number` creates the clickable link, with the product name in the prefilled message when it can be resolved safely. Unknown delivery outcomes never trigger this replacement.
- Chats API diagnostics count direct Instagram text under `text_requests`; `attempted_requests` remains the authoritative total across text and media and is monitoring-only.
- Kommo may mirror native Instagram comments through the general webhook as `origin=instagram_business`, `message_type=text`, which looks like a private Instagram message. The native comment Salesbot callback is the source of truth; durable reconciliation discards the mirrored private-message job before direct Instagram processing.

## Telegram Admin Commands

```text
/start          - Welcome + command list
/stats          - Today's conversation and order stats
/customers      - Recent customers by activity with IDs and tags
/customers vip  - Filter by tag
/orders         - Recent orders (alphabetical by customer)
/order ID STATUS - Update order status
/pause ID       - Pause AI responses for one customer
/resolve        - List escalated customers
/resolve ID     - Resolve one escalation
/resolve all    - Resolve all escalations
/tags ID        - View all tags for a customer
/tag ID add t1  - Add tags (comma-separated)
/tag ID del t1  - Remove a tag
/provider NAME  - Switch LLM provider (openai/anthropic)
/ai on          - Resume AI auto-responses
/ai off         - Pause AI (manual responses)
/broadcast      - List broadcasts
/send ID        - Send a broadcast
/preview TAGS   - Preview broadcast reach
/settings       - View current settings
/payments       - Registered payment methods and details
/shipping       - Registered delivery methods, zones, and rates
/instagram      - Instagram product and URL mappings
/kommo          - Kommo transport, queue, and Chats API status
/usage          - Token usage and costs
/conversion     - Sales funnel stats
/performance    - Response time stats
/products       - Popular products
/catalogpdf     - Generate PDF catalog
```

Broadcast commands are omitted from `/start` when `CHANNEL_BACKEND=kommo`, where direct broadcasts are unavailable.

## Admin Dashboard Features

Open `/admin/login` in a browser, sign in with `ADMIN_PASSWORD`, and the app will set an HTTP-only session cookie before redirecting to `/admin/dashboard`. Six tabs:

- **Resumen**: Stats cards, per-channel breakdown, LLM usage by provider, AI on/off toggle
- **Clientes**: Sortable customer table, retractable filters, tag management, inline channel/state editing with auto-save, delete customer, resolve escalations individually or all at once. In Kommo mode, manual reactivation first sets the Kommo lead `AI Mode` to `AI Active` and verifies it before clearing local history.
- **Pedidos**: Sortable order table with status badges
- **Broadcasts**: Sortable broadcast table, create/preview/send broadcasts, inspect `partial` sends, reset stuck broadcasts
- **Instagram**: Search, filter, and sort mappings for posts, Reels, carousels, and current Stories; map one or more brand-labelled catalog products and archive/restore records. Current Stories are synchronized from Meta when the list loads, and a current `/stories/{username}/{story-id}/` URL can be verified and mapped manually.
- **Configuracion**: LLM provider/model/temperature/max tokens/conversation history, orchestration mode, fallback settings, daily exchange rate, dynamic payment methods, and catalog PDF generation/download

Dark mode toggle in the header (persists via localStorage, auto-detects OS preference).

## Shared Runtime Settings

These values are stored in the store database and can be changed without redeploying:

- `llm_provider`, `llm_model`, `llm_temperature`, `llm_max_tokens`
- `fallback_provider`, `fallback_model`, `auto_fallback`
- `max_conversation_history`, `ai_enabled`, `ai_orchestration_mode`
- `catalog_refresh_minutes`, `broadcast_check_interval_minutes`, `catalog_pdf_interval_hours`
- `token_reminder_hour`, `token_reminder_minute`, `daily_analytics_hour`, `daily_analytics_minute`
- `kommo_emoji_mode_whatsapp`, `kommo_emoji_mode_instagram`, `kommo_strip_emoji`
- `store_phone_number`

Store-only payment methods are persisted separately under `payment_methods` in the same `settings` table. They are edited only from the store dashboard through `GET/PUT /admin/settings/payment-methods`, and the bot uses the configured method names plus their stored instructions at checkout. Scheduler timings are edited only from the master dashboard.

Exchange-rate settings are stored in the same `settings` table and are used when customers ask things like `¿a qué tasa recibes?`. Instagram is informational: Eva can answer product, general shipping/payment-option, and read-only support questions and can send product images there. Buying, payment, checkout-specific delivery details, checkout continuation, and PDF delivery hand off to WhatsApp. Trusted backend code builds a clickable `wa.me` handoff from the database-backed `store_phone_number`; configure that setting with the full international country code. It also controls public Instagram comment fallback replies; when empty, those comments invite only to DM and private handoffs use the profile/store-contact fallback without generating a broken URL.

Kommo Instagram customers resolve first through persisted provider identifiers. If those identifiers are new but the webhook provides an exact case-insensitive Instagram handle already stored on a customer, the existing customer is reused under a PostgreSQL advisory lock. This is exact identity reuse, not fuzzy customer merging.

The generated customer PDF catalog intentionally omits the internal `SKU` and `Stock` columns. It only shows customer-facing product information. The `send_catalog_pdf` AI tool remains available only on supported WhatsApp delivery; Instagram requests for the PDF receive the WhatsApp handoff instead. The Google Sheets catalog can be modeled as one row per size variant with `SKU`, `Parent SKU`, and a singular `Size` column; see [store/DEPLOYMENT.md](store/DEPLOYMENT.md) for the exact sheet format.

If `LLM_MANAGED_EXTERNALLY=true` is enabled for a store, the store dashboard/API/Telegram commands stop allowing LLM-setting writes locally, but payment methods and other non-LLM store settings remain editable in the store dashboard.

## Multi-Agent Rollout

The store supports three orchestration modes:

- `legacy`: existing single-agent behavior. This is the default and rollback mode.
- `shadow`: legacy serves customer responses while specialist routing is logged for evaluation. Specialist side-effect tools do not run.
- `multi_agent`: deterministic guards and the router select `sales`, `checkout`, `support`, or legacy fallback. Payment proofs are handled deterministically before agent execution.

Mode precedence is: valid `ai_orchestration_mode` row in the store DB, then valid `AI_ORCHESTRATION_MODE` env default, then hard-coded `legacy`. Invalid values are ignored safely.

Recommended rollout:

1. Keep stores on `legacy` after deployment and run the transcript regression suite locally.
2. Move one low-risk store to `shadow` from the store or master dashboard and inspect logs plus `ai_run_logs` for route accuracy.
3. Move to `multi_agent` only after route quality is acceptable.
4. Roll back immediately by setting `ai_orchestration_mode=legacy` from either dashboard.

Routing precedence is fixed: global AI pause and escalated customers suppress replies, hostile messages escalate before LLMs, payment-proof images go through deterministic verification, active checkout sessions remain sticky, clear purchase/support/sales keywords route deterministically, and the LLM router runs only for ambiguous messages.

Agent responsibilities:

- `legacy`: existing broad sales assistant and rollback path.
- `sales`: greetings, product discovery, catalog, photos, availability, recommendations, and checkout handoff.
- `checkout`: checkout draft collection, backend-validated finalization, cancellation, and saved-address reuse.
- `support`: read-only customer/order status support and escalation.

Tool permissions are enforced in `store/app/ai/runner.py` from agent definitions in `store/app/ai/agents/`. Tool schemas live in `store/app/ai/tools/definitions.py`; handlers live under `store/app/ai/tools/`; dispatch is in `store/app/ai/tools/executor.py`. Adding a tool requires schema, handler, allowlist membership, and tests proving unauthorized agents cannot call it.

Prompts are composed from `store/prompts/shared/` plus `store/prompts/agents/{legacy,sales,checkout,support}.md`. The ambiguity router prompt is `store/prompts/agents/router.md`.

Payment verification boundary: payment screenshots are analyzed by vision, then `store/app/ai/payment/verifier.py` deterministically checks open order, amount, method, recipient, and completed status. LLMs cannot create orders from proofs or mark payment state directly.

Observability: `usage_log` remains token/cost-oriented. `ai_run_logs` stores non-sensitive routing and agent metadata: mode, selected agent, route intent/source/confidence, provider/model, token usage, response time, tool names, tool rounds, handoff/fallback/escalation flags, shadow flag, and legacy fallback flag. It does not store payment credentials, image contents, full addresses, message text, tool arguments, or raw tool results.

Transcript regression tests live in `tests/store/test_ai_transcript_regressions.py` and cover complete customer journeys without external services.

## Security

- **Admin authentication:** Protected `/admin/` APIs (settings, broadcasts, analytics, customers, orders, and Instagram mappings) require `ADMIN_PASSWORD` via `Authorization: Bearer <password>` or an HTTP-only session cookie. Without a valid production password, Store startup fails before serving requests.
- **Dashboard sessions:** Both the store and master dashboards use dedicated login forms (`/admin/login` and `/login`), then set HTTP-only session cookies after successful authentication.
- **Webhook verification:** WhatsApp and Instagram webhooks verify `X-Hub-Signature-256` using HMAC-SHA256 with timing-safe comparison.
- **Kommo webhook verification:** General Kommo webhooks use a path secret with timing-safe comparison. Salesbot callbacks validate Kommo JWTs using HS256 or HS512, the private integration secret, expiration, issuer/subdomain, Integration ID when present, and a strict `return_url` host check.
- **Direct media download safety:** Payment-image downloads from direct URLs are limited to HTTPS URLs on trusted Meta/Instagram/Kommo host suffixes, with userinfo/custom ports rejected, redirects disabled, content-type checks, and a 5 MB size limit.
- **Master auth:** All `/api/stores/` endpoints require Bearer token (`MASTER_SECRET_KEY`) or the `master_session` cookie. All token comparisons use `hmac.compare_digest`.
- **Credentials at rest:** Store credentials in the master DB are Fernet-encrypted. API responses only return masked values.
- **Test endpoints:** Store `/test/` routes require `DEBUG=true` and a direct loopback request with no forwarding headers. Master `/test/` routes require `ENABLE_TEST_ENDPOINTS=true` and an exact loopback `APP_BASE_URL`; production leaves them disabled.
- **Rate limiting:** Both apps use `slowapi` (store: 60 req/min, master: 30 req/min per IP).
- **CORS:** Restricted to the app's own origin (`APP_BASE_URL`).
- **Security headers:** `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy`, `Strict-Transport-Security` (production), `Content-Security-Policy` (production).
- **Error sanitization:** Unhandled exceptions return a generic 500 in production; full errors only shown in debug mode.
- **Production startup validation:** In `store/`, production boot fails fast if `ADMIN_PASSWORD` or all LLM keys are missing. Meta mode requires WhatsApp Meta credentials. Kommo mode requires the Kommo private integration, WhatsApp Salesbot, webhook secret, and AI Mode field/enum variables. Instagram and Telegram remain optional, but if either Meta Instagram context or Telegram is enabled it must be fully configured.
- **Log redaction:** Normal webhook logging uses masked sender IDs and avoids logging raw customer message text or tool arguments at `INFO`.
- **Inbound debounce:** Rapid consecutive inbound messages from the same customer are buffered briefly and grouped into a single AI turn, so the bot does not answer twice when the user is still typing follow-up context.

## Multi-store Architecture

The system supports running multiple independent stores from a single codebase. Each store is a separate Railway deployment with its own `.env`, database, and channel accounts.

A **Master Control Plane** (`master/`) sits on top:

```txt
Master Control Plane (1 deployment, port 9000)
  ├── Master PostgreSQL database (store registry, encrypted credentials, audit log)
  ├── Dashboard: monitor all stores, manage runtime settings, view costs, deploy changes
  ├── Runtime settings: set provider/model/fallback/orchestration per store
  ├── Railway API integration: push env vars + trigger redeploys
  └── Health checker: pings each store every 5 minutes

Store A (port 8000)          Store B (port 8001)          Store C ...
  ├── Own PostgreSQL database  ├── Own PostgreSQL database
  ├── Own channel backend      ├── Own channel backend
  │   (Meta or Kommo)          │   (Meta or Kommo)
  ├── Own Telegram bot         ├── Own Telegram bot
  ├── Own Google Sheet         ├── Own Google Sheet
  └── Own admin dashboard      └── Own admin dashboard
      (ADMIN_PASSWORD + cookie)    (ADMIN_PASSWORD + cookie)
```

### Master Dashboard Features

Open `http://localhost:9000/login`, sign in with `MASTER_SECRET_KEY`, and the app will set an HTTP-only session cookie before redirecting to `/dashboard`. Three tabs:

- **Stores**: Overview cards with live stats, health status dots, platform-wide LLM cost summary with time-range toggles (Today/7d/30d)
- **Store Detail**: Drill into one store — stats, dedicated API Keys panel (OpenAI/Anthropic with active/configured/not-set status), shared AI settings including orchestration mode, a separate Scheduled Jobs panel, LLM usage & costs with time-range toggles, grouped environment variables (Channels, Infrastructure, Customization), Railway deployment status, and "Deploy to Railway"
- **Audit Log**: Full history of all actions (store created, credential updated, runtime settings changed, deploy triggered)

Runtime settings apply immediately from the store database. Credentials and deploy-time env vars remain master-managed and may still require a redeploy.

### Quick Start (Master)

```bash
cd /path/to/Social-Media-Manager
source .venv/bin/activate
pip install -r master/requirements.txt

cd master
cp .env.example .env
# Edit .env: DATABASE_URL, MASTER_SECRET_KEY, ENCRYPTION_KEY
# For /test/* locally also set ENABLE_TEST_ENDPOINTS=true
uvicorn app.main:app --reload --port 9000

# Open test UI
open http://localhost:9000/test/ui
```

See [master/DEPLOYMENT.md](master/DEPLOYMENT.md) for the full setup guide.

### Multi-store Env Vars (per store)

| Variable                 | Purpose                                                                                              |
| :----------------------- | :--------------------------------------------------------------------------------------------------  |
| `ADMIN_PASSWORD`         | **Required in production.** Protects dashboard and all admin API endpoints                           |
| `AI_ORCHESTRATION_MODE`  | Optional env default for rollout mode. DB runtime setting takes precedence. Defaults to `legacy`      |
| `SYSTEM_PROMPT_OVERRIDE` | If set, replaces `prompts/system_prompt.md` content for this store                                   |
| `LLM_MANAGED_EXTERNALLY` | If `true`, locks LLM controls in store dashboard/API/Telegram so those keys are managed from master  |

Channel backend variables managed per store can include:

```env
CHANNEL_BACKEND=meta|kommo
KOMMO_SUBDOMAIN=
KOMMO_ACCESS_TOKEN=
KOMMO_INTEGRATION_ID=
KOMMO_INTEGRATION_SECRET=
KOMMO_WHATSAPP_SALESBOT_ID=
KOMMO_SALESBOT_ID= # Temporary WhatsApp-only fallback
KOMMO_WEBHOOK_SECRET=
KOMMO_AI_MODE_FIELD_ID=
KOMMO_AI_ACTIVE_ENUM_ID=
KOMMO_AI_HUMAN_ENUM_ID=
KOMMO_AI_PAUSED_ENUM_ID=
KOMMO_DEFAULT_RESPONSIBLE_USER_ID=
KOMMO_CHATS_MEDIA_ENABLED=false
KOMMO_CHATS_PRODUCT_IMAGES_ENABLED=false
KOMMO_CHATS_CATALOG_PDF_ENABLED=false
KOMMO_CHATS_API_MONTHLY_LIMIT=
KOMMO_CHATS_PDF_ATTACHMENT_TYPE=
```

See [docs/KOMMO_MIGRATION.md](docs/KOMMO_MIGRATION.md) for the full Kommo setup, widget build, Salesbot configuration, diagnostics, limitations, and rollback procedure.
