# VS Chatbot - AI Sales Assistant

AI-powered sales chatbot for Instagram DMs and WhatsApp, built for a Venezuelan
Victoria's Secret resale business. Supports both OpenAI and Anthropic as LLM
providers, with hot-swapping from the admin panel. Features a PDF product catalog,
global AI pause/resume, per-customer escalation, customer address memory,
admin tag management, sortable dashboard tables, and a dark mode admin dashboard.

**Multi-store support:** A Master Control Plane (`master/`) lets you manage
multiple independent store deployments from a single dashboard — each with its
own database, API keys, WhatsApp number, and Telegram bot. See
[master/DEPLOYMENT.md](master/DEPLOYMENT.md) for the multi-store setup guide.

## Quick start

```bash
# 1. Clone and install
git clone <your-repo-url>
cd vs-chatbot
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 2. Configure
cp .env.example .env
# Edit .env with your actual API keys and credentials

# 3. Set up the database
# Run all migrations (001 through 004) against your Supabase PostgreSQL instance

# 4. Run locally
uvicorn app.main:app --reload --port 8000

# 5. Expose for webhook testing (in another terminal)
# Use ngrok or similar to get a public HTTPS URL
ngrok http 8000
```

## Architecture

```
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
```

## Webhook endpoints

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/webhooks/whatsapp` | WhatsApp webhook verification |
| POST | `/webhooks/whatsapp` | Receive WhatsApp messages |
| GET | `/webhooks/instagram` | Instagram webhook verification |
| POST | `/webhooks/instagram` | Receive Instagram DMs |

## Admin endpoints

> All `/admin/` endpoints require authentication via `Authorization: Bearer ADMIN_PASSWORD` header or session cookie. See **Security** section below.

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/` | Health check (no auth) |
| GET | `/health` | Detailed health status (no auth) |
| GET | `/admin/settings/` | View all settings |
| GET | `/admin/settings/providers` | List available LLM providers and models |
| PUT | `/admin/settings/{key}` | Update a setting |
| POST | `/admin/settings/switch-provider` | Quick provider switch |
| GET | `/admin/settings/usage-summary` | Today's token usage and cost estimate |
| GET | `/admin/settings/stats/conversations` | Today's conversation and order stats |
| POST | `/admin/settings/instagram/setup-ice-breakers` | Configure Instagram Ice Breakers |
| POST | `/admin/settings/instagram/subscribe-page` | Subscribe FB Page to webhooks |
| PUT | `/admin/settings/ai_enabled` | Toggle AI on/off globally |
| POST | `/admin/settings/catalog/generate-pdf` | Generate product catalog PDF |
| GET | `/admin/settings/catalog/pdf-status` | Check PDF status |
| GET | `/admin/settings/catalog/download-pdf` | Download catalog PDF |
| GET | `/admin/settings/orders` | List recent orders |
| GET | `/admin/settings/customers` | List customers (optional `?tag=` filter) |
| POST | `/admin/settings/customers/{id}/resolve` | Resolve escalated customer |
| POST | `/admin/settings/customers/resolve-all` | Resolve all escalated customers |
| GET | `/admin/settings/customers/{id}/tags` | Get customer tags |
| POST | `/admin/settings/customers/{id}/tags` | Add tags to customer |
| DELETE | `/admin/settings/customers/{id}/tags/{tag}` | Remove tag from customer |

## Switching LLM providers

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

## Instagram setup (after Meta App Review approval)

```bash
# 1. Subscribe your Facebook Page to messaging webhooks (once)
curl -X POST "http://localhost:8000/admin/settings/instagram/subscribe-page?page_id=YOUR_PAGE_ID"

# 2. Configure Ice Breakers (once, or whenever you want to update them)
curl -X POST "http://localhost:8000/admin/settings/instagram/setup-ice-breakers?ig_user_id=YOUR_IG_USER_ID"
```

## Message types handled

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

## Telegram admin commands

```
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
/abtest         - A/B test results
/abmode on/off  - Toggle A/B testing
/catalogpdf     - Generate PDF catalog
```

## Admin dashboard features

Open `/admin/dashboard?password=YOUR_ADMIN_PASSWORD` in a browser (sets a session cookie and redirects to the clean URL). Five tabs:

- **Resumen**: Stats cards, per-channel breakdown, LLM usage by provider, AI on/off toggle
- **Clientes**: Sortable customer table, tag management (add/remove per customer), resolve escalations individually or all at once
- **Pedidos**: Sortable order table with status badges
- **Broadcasts**: Sortable broadcast table, create/preview/send broadcasts, reset stuck broadcasts
- **Configuracion**: LLM provider/model/temperature/max tokens/conversation history, fallback settings, A/B testing toggle, catalog PDF generation/download/auto-refresh interval

Dark mode toggle in the header (persists via localStorage, auto-detects OS preference).

## Security

- **Admin authentication:** All `/admin/` API endpoints (settings, broadcasts, analytics, customers, orders) require `ADMIN_PASSWORD` via `Authorization: Bearer <password>` header or HTTP-only session cookie. Without `ADMIN_PASSWORD` set in production (`DEBUG=false`), all admin routes return 403.
- **Dashboard sessions:** Both the store and master dashboards accept a password/token via query param on first visit, set an HTTP-only secure cookie, and redirect to the clean URL — keeping secrets out of browser history, server logs, and referrer headers.
- **Webhook verification:** WhatsApp and Instagram webhooks verify `X-Hub-Signature-256` using HMAC-SHA256 with timing-safe comparison.
- **Master auth:** All `/api/stores/` endpoints require Bearer token (`MASTER_SECRET_KEY`). All token comparisons use `hmac.compare_digest`.
- **Credentials at rest:** Store credentials in the master DB are Fernet-encrypted. API responses only return masked values.
- **Test endpoints:** `/test/` routes are disabled in production (`DEBUG=false` for store, non-localhost for master).
- **Rate limiting:** Both apps use `slowapi` (store: 60 req/min, master: 30 req/min per IP).
- **CORS:** Restricted to the app's own origin (`APP_BASE_URL`).
- **Security headers:** `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy`, `Strict-Transport-Security` (production), `Content-Security-Policy` (production).
- **Error sanitization:** Unhandled exceptions return a generic 500 in production; full errors only shown in debug mode.

## Multi-store architecture

The system supports running multiple independent stores from a single codebase. Each store is a separate Railway deployment with its own `.env`, database, and channel accounts.

A **Master Control Plane** (`master/`) sits on top:

```
Master Control Plane (1 deployment, port 9000)
  ├── Master Supabase DB (store registry, encrypted credentials, audit log)
  ├── Dashboard: monitor all stores, manage AI settings, view costs, deploy changes
  ├── LLM control: set provider/model/temperature per store, see platform-wide costs
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

### Master dashboard features

Open `http://localhost:9000/dashboard?token=YOUR_SECRET` (sets a session cookie and redirects to clean URL). Three tabs:

- **Stores**: Overview cards with live stats, health status dots, platform-wide LLM cost summary with time-range toggles (Today/7d/30d)
- **Store Detail**: Drill into one store — stats, dedicated API Keys panel (OpenAI/Anthropic with active/configured/not-set status), AI provider configuration, LLM usage & costs with time-range toggles, grouped environment variables (Channels, Infrastructure, Customization), Railway deployment status, "Deploy to Railway" button
- **Audit Log**: Full history of all actions (store created, credential updated, LLM settings changed, deploy triggered)

### Quick start (master)

```bash
cd master
cp .env.example .env
# Edit .env: DATABASE_URL, MASTER_SECRET_KEY, ENCRYPTION_KEY
pip install -r requirements.txt
uvicorn app.main:app --reload --port 9000

# Open test UI
open http://localhost:9000/test/ui
```

See [master/DEPLOYMENT.md](master/DEPLOYMENT.md) for the full setup guide.

### Multi-store env vars (per store)

| Variable | Purpose |
|----------|---------|
| `ADMIN_PASSWORD` | **Required in production.** Protects dashboard and all admin API endpoints |
| `SYSTEM_PROMPT_OVERRIDE` | If set, replaces `prompts/system_prompt.md` content for this store |
| `LLM_MANAGED_EXTERNALLY` | If `true`, locks LLM controls in store dashboard/API/Telegram — managed from master |
