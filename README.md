# VS Chatbot - AI Sales Assistant

AI-powered sales chatbot for Instagram DMs and WhatsApp, built for a Venezuelan Victoria's Secret resale business. Supports both OpenAI and Anthropic as LLM providers, with hot-swapping from the admin panel. Features a PDF product catalog, global AI pause/resume, per-customer escalation, customer address memory, admin tag management, per-store payment instructions, sortable dashboard tables, and a dark mode admin dashboard.

Dashboard-managed runtime settings live in each store's `settings` table. That includes AI configuration, `ai_enabled`, `catalog_pdf_interval_hours`, and the four payment instruction fields. When a store is connected to `master/`, both dashboards read and write those same rows, so changes stay in sync after refresh.

**Multi-store support:** A Master Control Plane (`master/`) lets you manage multiple independent store deployments from a single dashboard — each with its own database, API keys, WhatsApp number, and Telegram bot. See [master/DEPLOYMENT.md](master/DEPLOYMENT.md) for the multi-store setup guide.

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

# 3. Set up the database
# Run this SQL file manually against your Supabase PostgreSQL instance:
#   store/migrations/001_schema.sql

# 4. Run locally
cd store
uvicorn app.main:app --reload --port 8000

# 5. Expose for webhook testing (in another terminal)
# Use ngrok or similar to get a public HTTPS URL
ngrok http 8000
```

## Architecture

```txt
Customer (WhatsApp or Instagram DM)
    -> Webhook (FastAPI)
        -> Normalize message (text, buttons, images, ice breakers, postbacks)
        -> Conversation Router
            -> LLM Engine (OpenAI or Anthropic, admin-selectable)
                -> Tool Calls (inventory, tags, orders, escalation)
            -> Response
        -> Send reply via Meta API
            -> WhatsApp: text, interactive buttons, templates
            -> Instagram: text, quick replies, images, carousels

Store DB settings (source of truth for runtime settings)
    -> Store dashboard reads/writes locally
    -> Master dashboard reads/writes remotely through the store DB
    -> Changes become visible on the next refresh/save without redeploy
```

## Webhook Endpoints

| Method | Path                   | Purpose                        |
| :----- | :--------------------- | :----------------------------- |
| GET    | `/webhooks/whatsapp`   | WhatsApp webhook verification  |
| POST   | `/webhooks/whatsapp`   | Receive WhatsApp messages      |
| GET    | `/webhooks/instagram`  | Instagram webhook verification |
| POST   | `/webhooks/instagram`  | Receive Instagram DMs          |

## Admin Endpoints

> All `/admin/` endpoints require authentication via `Authorization: Bearer ADMIN_PASSWORD` header or session cookie. See **Security** section below.

| Method | Path                                              | Purpose                                         |
| :----- | :------------------------------------------------ | :-----------------------------------------------|
| GET    | `/`                                               | Health check (no auth)                          |
| GET    | `/health`                                         | Detailed health status (no auth)                |
| GET    | `/admin/login`                                    | Store dashboard login page                      |
| POST   | `/admin/login`                                    | Create store admin session cookie               |
| POST   | `/admin/logout`                                   | Clear store admin session cookie                |
| GET    | `/admin/settings/`                                | View all settings                               |
| GET    | `/admin/settings/providers`                       | List available LLM providers and models         |
| PUT    | `/admin/settings/{key}`                           | Update a setting                                |
| POST   | `/admin/settings/switch-provider`                 | Quick provider switch                           |
| GET    | `/admin/settings/usage-summary`                   | Today's token usage and cost estimate           |
| GET    | `/admin/settings/stats/conversations`             | Today's conversation and order stats            |
| POST   | `/admin/settings/instagram/setup-ice-breakers`    | Configure Instagram Ice Breakers                |
| POST   | `/admin/settings/instagram/subscribe-page`        | Subscribe FB Page to webhooks                   |
| PUT    | `/admin/settings/ai_enabled`                      | Toggle AI on/off globally                       |
| POST   | `/admin/settings/catalog/generate-pdf`            | Generate product catalog PDF                    |
| GET    | `/admin/settings/catalog/pdf-status`              | Check PDF status                                |
| GET    | `/admin/settings/catalog/download-pdf`            | Download catalog PDF                            |
| GET    | `/admin/settings/orders`                          | List recent orders                              |
| GET    | `/admin/settings/customers`                       | List customers (optional `?tag=` filter)        |
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

# Switch to OpenAI GPT-4o-mini
curl -X POST "http://localhost:8000/admin/settings/switch-provider?provider=openai" \
  -H "Authorization: Bearer YOUR_ADMIN_PASSWORD"

# Switch to a specific model
curl -X POST "http://localhost:8000/admin/settings/switch-provider?provider=openai&model=gpt-5.4-mini" \
  -H "Authorization: Bearer YOUR_ADMIN_PASSWORD"
```

> With `DEBUG=true` and no `ADMIN_PASSWORD` set, auth is skipped for local development.

## Instagram Setup (after Meta App Review approval)

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
- Audio, video, documents, stickers (acknowledged)
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

## Telegram Admin Commands

```text
/start          - Welcome + command list
/stats          - Today's conversation and order stats
/customers      - Recent customers (alphabetical) with IDs and tags
/customers vip  - Filter by tag
/orders         - Recent orders (alphabetical by customer)
/order ID STATUS - Update order status
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
/usage          - Token usage and costs
/conversion     - Sales funnel stats
/performance    - Response time stats
/products       - Popular products
/catalogpdf     - Generate PDF catalog
```

## Admin Dashboard Features

Open `/admin/login` in a browser, sign in with `ADMIN_PASSWORD`, and the app will set an HTTP-only session cookie before redirecting to `/admin/dashboard`. Five tabs:

- **Resumen**: Stats cards, per-channel breakdown, LLM usage by provider, AI on/off toggle
- **Clientes**: Sortable customer table, tag management (add/remove per customer), resolve escalations individually or all at once
- **Pedidos**: Sortable order table with status badges
- **Broadcasts**: Sortable broadcast table, create/preview/send broadcasts, inspect `partial` sends, reset stuck broadcasts
- **Configuracion**: LLM provider/model/temperature/max tokens/conversation history, fallback settings, payment instructions, catalog PDF generation/download, and auto-refresh interval

Dark mode toggle in the header (persists via localStorage, auto-detects OS preference).

## Shared Runtime Settings

These values are stored in the store database and can be changed without redeploying:

- `llm_provider`, `llm_model`, `llm_temperature`, `llm_max_tokens`
- `fallback_provider`, `fallback_model`, `auto_fallback`
- `max_conversation_history`, `ai_enabled`, `catalog_pdf_interval_hours`
- `payment_zelle_details`, `payment_binance_details`, `payment_zinli_details`, `payment_bolivares_details`

If `LLM_MANAGED_EXTERNALLY=true` is enabled for a store, the store dashboard/API/Telegram commands stop allowing LLM-setting writes locally, but the payment settings and other non-LLM runtime settings remain editable.

## Security

- **Admin authentication:** All `/admin/` API endpoints (settings, broadcasts, analytics, customers, orders) require `ADMIN_PASSWORD` via `Authorization: Bearer <password>` header or HTTP-only session cookie. Without `ADMIN_PASSWORD` set in production (`DEBUG=false`), all admin routes return 403.
- **Dashboard sessions:** Both the store and master dashboards use dedicated login forms (`/admin/login` and `/login`), then set HTTP-only session cookies after successful authentication.
- **Webhook verification:** WhatsApp and Instagram webhooks verify `X-Hub-Signature-256` using HMAC-SHA256 with timing-safe comparison.
- **Master auth:** All `/api/stores/` endpoints require Bearer token (`MASTER_SECRET_KEY`) or the `master_session` cookie. All token comparisons use `hmac.compare_digest`.
- **Credentials at rest:** Store credentials in the master DB are Fernet-encrypted. API responses only return masked values.
- **Test endpoints:** `/test/` routes are disabled in production (`DEBUG=false` for store, non-localhost for master).
- **Rate limiting:** Both apps use `slowapi` (store: 60 req/min, master: 30 req/min per IP).
- **CORS:** Restricted to the app's own origin (`APP_BASE_URL`).
- **Security headers:** `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy`, `Strict-Transport-Security` (production), `Content-Security-Policy` (production).
- **Error sanitization:** Unhandled exceptions return a generic 500 in production; full errors only shown in debug mode.
- **Production startup validation:** In `store/`, production boot now fails fast if `ADMIN_PASSWORD`, the WhatsApp credentials, or all LLM keys are missing. Instagram and Telegram remain optional, but if either integration is enabled it must be fully configured.
- **Log redaction:** Normal webhook logging uses masked sender IDs and avoids logging raw customer message text or tool arguments at `INFO`.

## Multi-store Architecture

The system supports running multiple independent stores from a single codebase. Each store is a separate Railway deployment with its own `.env`, database, and channel accounts.

A **Master Control Plane** (`master/`) sits on top:

```txt
Master Control Plane (1 deployment, port 9000)
  ├── Master Supabase DB (store registry, encrypted credentials, audit log)
  ├── Dashboard: monitor all stores, manage runtime settings, view costs, deploy changes
  ├── Runtime settings: set provider/model/fallback/payment instructions per store
  ├── Railway API integration: push env vars + trigger redeploys
  └── Health checker: pings each store every 5 minutes

Store A (port 8000)          Store B (port 8001)          Store C ...
  ├── Own Supabase DB          ├── Own Supabase DB
  ├── Own WhatsApp number      ├── Own WhatsApp number
  ├── Own Instagram account    ├── Own Instagram account
  ├── Own Telegram bot         ├── Own Telegram bot
  ├── Own Google Sheet         ├── Own Google Sheet
  └── Own admin dashboard      └── Own admin dashboard
      (ADMIN_PASSWORD + cookie)    (ADMIN_PASSWORD + cookie)
```

### Master Dashboard Features

Open `http://localhost:9000/login`, sign in with `MASTER_SECRET_KEY`, and the app will set an HTTP-only session cookie before redirecting to `/dashboard`. Three tabs:

- **Stores**: Overview cards with live stats, health status dots, platform-wide LLM cost summary with time-range toggles (Today/7d/30d)
- **Store Detail**: Drill into one store — stats, dedicated API Keys panel (OpenAI/Anthropic with active/configured/not-set status), shared runtime settings (AI config, payment instructions, AI toggle, catalog PDF interval), LLM usage & costs with time-range toggles, grouped environment variables (Channels, Infrastructure, Customization), Railway deployment status, and "Deploy to Railway"
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
uvicorn app.main:app --reload --port 9000

# Open test UI
open http://localhost:9000/test/ui
```

See [master/DEPLOYMENT.md](master/DEPLOYMENT.md) for the full setup guide.

### Multi-store Env Vars (per store)

| Variable                 | Purpose                                                                                              |
| :----------------------- | :--------------------------------------------------------------------------------------------------  |
| `ADMIN_PASSWORD`         | **Required in production.** Protects dashboard and all admin API endpoints                           |
| `SYSTEM_PROMPT_OVERRIDE` | If set, replaces `prompts/system_prompt.md` content for this store                                   |
| `LLM_MANAGED_EXTERNALLY` | If `true`, locks LLM controls in store dashboard/API/Telegram so those keys are managed from master  |
