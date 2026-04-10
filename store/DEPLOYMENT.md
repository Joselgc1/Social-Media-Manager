# VS Chatbot: Complete Deployment and Testing Guide

Everything you need to go from a fresh laptop to a fully operational AI chatbot handling real customer messages on WhatsApp and Instagram, with broadcast campaigns, an admin dashboard, payment screenshot recognition, and analytics.

Important business behavior baked into the current system:

- shipping is offered through `MRW` or `Zoom` with `cobro a destino`
- payment methods are store-defined and managed only from the store dashboard
- the owner can update a store-only daily exchange-rate field used for `¿a qué tasa recibes?`
- the customer PDF catalog hides internal SKU and stock columns

The guide has 10 parts:

- Parts 1 through 4: Set up external services.
- Part 5: Deploys the code.
- Part 6: Connects the webhooks (WhatsApp, Instagram, Telegram).
- Part 7: Sets up broadcasts and the admin dashboard.
- Part 8: Activates analytics.
- Part 9: The full testing checklist.
- Part 10: A quick reference of all endpoints and commands.

**Estimated total time:** 3–5 hours for the core system (same day), plus 1–4 weeks for Instagram (waiting on Meta App Review).

> **Running multiple stores?** This guide covers deploying a single store. For managing multiple stores from a centralized dashboard, see [master/DEPLOYMENT.md](master/DEPLOYMENT.md).

---

## Part 1: External Accounts and API Keys

You need accounts on six services before touching the code. Most are free.

### 1.1 Supabase (Database)

Go to [supabase.com](https://supabase.com) and create a free account. Click **New Project**, pick a region close to your deployment server (US East if deploying on Railway), and set a strong database password. **Save this password!**

Once the project is created, go to **Settings > Database**. You'll find your connection string under **Connection string > URI**. It looks like:

```text
postgresql://postgres:[YOUR-PASSWORD]@db.xyzabc.supabase.co:5432/postgres
```

Copy this. It goes in your `.env` as `DATABASE_URL`.

Run the migrations. Go to the SQL Editor in Supabase's dashboard:

1. Paste the entire contents of `store/migrations/001_schema.sql` and click **Run**.

This creates all tables and seeds the runtime settings used by the store dashboard and the master control plane.

Verify by going to Table Editor. You should see the `settings` table pre-populated with the AI defaults, the scheduler defaults (`catalog_refresh_minutes`, `broadcast_check_interval_minutes`, `catalog_pdf_interval_hours`, `token_reminder_*`, `daily_analytics_*`), an empty `payment_methods` row, and an empty `accepted_exchange_rate` row.

### 1.2 OpenAI API Key

Go to [platform.openai.com](https://platform.openai.com), sign in, and navigate to **API Keys**. Click **Create new secret key**, name it "vs-chatbot," and copy it. It starts with `sk-`.

Add $10 of credit under Billing. At your message volume, this lasts 1–2 months with GPT-4o-mini.

### 1.3 Anthropic API Key

Go to [console.anthropic.com](https://console.anthropic.com), sign in, and navigate to **API Keys**. Click **Create Key**, name it "vs-chatbot," and copy it. It starts with `sk-ant-`.

Add $10 of credit under Plans & Billing.

### 1.4 Google Sheets (Product Catalog)

You need a Google Cloud service account so the bot can read the product catalog.

Go to [console.cloud.google.com](https://console.cloud.google.com). Create a new project called "vs-chatbot." Then:

1. Go to **APIs & Services > Library**. Search for "Google Sheets API" and enable it.
2. Go to **APIs & Services > Credentials**. Click **Create Credentials > Service Account**.
3. Name it "vs-chatbot-reader," skip the optional steps, and click **Done**.
4. Click on the service account. Go to the **Keys** tab, click **Add Key > Create new key > JSON**. A file downloads.
5. Base64-encode this file:
   - Mac: `base64 -i your-downloaded-file.json`
   - Linux: `base64 -w 0 your-downloaded-file.json`
   - Copy the output. This goes in `.env` as `GOOGLE_SHEETS_CREDENTIALS_B64`.

Now create the product catalog spreadsheet. Open Google Sheets, create a new sheet, and set up these **exact column headers in row 1**:

```text
SKU | Product name | Category | Description | Sizes | Price USD | Stock | Active | Image URL
```

If you want the bot to be able to send product photos, use the `Image URL` column. Supported formats:

- a plain public image URL
- a Google Drive share link for a publicly accessible image
- a Google Sheets formula like `=IMAGE("https://...")`

If the customer asks to see a specific product and that row has a usable image, the bot can send it directly in chat.

Add a few test products:

```text
PJ-001 | Pijama rayas rosa | Pajamas | Pijama de algodón con rayas rosas | S,M,L | 28.00 | 5 | Yes |
UN-001 | Conjunto encaje negro | Underwear | Conjunto de encaje negro Victoria's Secret | S,M | 22.00 | 3 | Yes |
PJ-002 | Pijama satén azul | Pajamas | Pijama de satén azul marino | M,L,XL | 32.00 | 2 | Yes |
SET-001 | Set completo rojo | Sets | Set de ropa interior completo rojo | S,M,L | 35.00 | 4 | Yes |
```

Share the sheet with the service account email (looks like `vs-chatbot-reader@your-project.iam.gserviceaccount.com`). Give it **Viewer** access.

Copy the sheet ID from the URL (the long string between `/d/` and `/edit`). This goes in `.env` as `PRODUCT_SHEET_ID`.

### 1.5 Telegram Bot (Admin Notifications + Management)

The Telegram bot serves two purposes: it receives escalation/order notifications, and it lets you manage the entire system with commands from your phone.

Open Telegram, search for **@BotFather**, and send `/newbot`. Follow the prompts:

- Bot name: "VS Chatbot Admin"
- Username: something like `vs_chatbot_admin_bot`

BotFather gives you a token like `123456789:ABCdefGHIjklMNOpqrSTUvwxYZ`. This goes in `.env` as `TELEGRAM_BOT_TOKEN`.

Now get the admin's chat ID. Search for **@userinfobot** on Telegram and send it any message. It replies with your numeric user ID. This goes in `.env` as `TELEGRAM_ADMIN_CHAT_ID`.

Send a message to your bot (search for it by username, click **Start**). This is necessary so the bot has permission to message you.

### 1.6 Meta Developer App (WhatsApp + Instagram APIs)

Go to [developers.facebook.com](https://developers.facebook.com) and log in with the Facebook account that manages the business.

Click **My Apps > Create App**. Choose **Business** type. Name it "VS Chatbot" and connect it to the existing Meta Business Manager account.

#### A) Add the WhatsApp Product

Click **Add Product > WhatsApp > Set Up**.

Add the business phone number:

1. Go to **WhatsApp > Getting Started > Add phone number**.
2. Enter a phone number NOT currently registered on regular WhatsApp. Your friend may need a new SIM or to unregister the current number.
3. Verify via SMS or voice call.
4. Under **WhatsApp > Configuration**, note the **Phone number ID**. This goes in `.env` as `WHATSAPP_PHONE_NUMBER_ID`.

Generate a permanent access token: Create a System User in Business Settings and generate a token. This goes in `.env` as `WHATSAPP_ACCESS_TOKEN`.

#### B) Add the Webhooks Product

Click **Add Product > Webhooks**. You'll configure the URL later in Part 6.

#### C) Get the App Secret

Go to **App Settings > Basic**. Click **Show** next to App Secret. This goes in `.env` as `META_APP_SECRET`.

#### D) Choose Verify Tokens

Make up any random string (e.g., `my_secret_verify_2026`). This goes in `.env` as both `WHATSAPP_VERIFY_TOKEN` and `INSTAGRAM_VERIFY_TOKEN`.

---

## Part 2: Local Development Setup

### 2.1 Extract and Install

```bash
tar -xzf vs-chatbot.tar.gz
cd vs-chatbot

python3 -m venv .venv
source .venv/bin/activate    # Windows: .venv\Scripts\activate

pip install -r store/requirements.txt
```

### 2.2 Configure Environment Variables

```bash
cp store/.env.example store/.env
```

Open `.env` and fill in every value from Part 1:

```bash
META_APP_SECRET=abc123...
WHATSAPP_ACCESS_TOKEN=EAAG...
WHATSAPP_PHONE_NUMBER_ID=123456789
WHATSAPP_VERIFY_TOKEN=my_secret_verify_2026
INSTAGRAM_ACCESS_TOKEN=          # Leave empty until App Review approval
INSTAGRAM_VERIFY_TOKEN=my_secret_verify_2026
OPENAI_API_KEY=sk-...
ANTHROPIC_API_KEY=sk-ant-...
DATABASE_URL=postgresql://postgres:yourpass@db.xyz.supabase.co:5432/postgres
GOOGLE_SHEETS_CREDENTIALS_B64=eyJ0eXBlIjoi...
PRODUCT_SHEET_ID=1abc2def3ghi...
TELEGRAM_BOT_TOKEN=123456789:ABC...
TELEGRAM_ADMIN_CHAT_ID=987654321
STORE_NAME="Zona Pink"
OWNER_NAME=Carlos
APP_BASE_URL=https://your-app.railway.app
DEBUG=true
```

### 2.3 Test Locally

```bash
cd store && uvicorn app.main:app --reload --port 8000
```

You should see:

```text
[INFO] Starting Zona Pink chatbot...
[INFO] Database connected.
[INFO] LLM providers initialized (OpenAI + Anthropic).
[INFO] Catalog refreshed: 4 active products loaded.
[INFO] Active LLM: openai/gpt-5.4-nano
[INFO] Background scheduler started with 4 jobs.
[INFO] Chatbot is ready! Waiting for messages...
```

Test these endpoints:

```bash
# Health check
curl http://localhost:8000/health

# Settings (requires auth when ADMIN_PASSWORD is set; skipped in DEBUG mode without password)
curl -H "Authorization: Bearer YOUR_ADMIN_PASSWORD" http://localhost:8000/admin/settings/

# Provider switching
curl -X POST "http://localhost:8000/admin/settings/switch-provider?provider=anthropic" \
  -H "Authorization: Bearer YOUR_ADMIN_PASSWORD"
curl -X POST "http://localhost:8000/admin/settings/switch-provider?provider=openai" \
  -H "Authorization: Bearer YOUR_ADMIN_PASSWORD"

# Admin dashboard login
open http://localhost:8000/admin/login
```

> **Note:** With `DEBUG=true` and no `ADMIN_PASSWORD` set, admin routes are accessible without auth for local development. In production, startup validation requires `ADMIN_PASSWORD` and the full WhatsApp config (`META_APP_SECRET`, `WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_VERIFY_TOKEN`). Instagram and Telegram are optional, but if you enable either one, you must provide its full variable set.

---

## Part 3: Expose Your Local Server (for Development)

Meta requires a publicly accessible HTTPS URL for webhooks. Use ngrok during development.

```bash
# Install (Mac)
brew install ngrok

# Install (Linux)
curl -sSL https://ngrok-agent.s3.amazonaws.com/ngrok-v3-stable-linux-amd64.tgz | tar xz
sudo mv ngrok /usr/local/bin/

# Authenticate (once)
ngrok config add-authtoken YOUR_NGROK_AUTH_TOKEN

# Start tunnel (with FastAPI running on port 8000)
ngrok http 8000
```

Copy the `https://abc123.ngrok-free.app` URL. Verify: `https://abc123.ngrok-free.app/health`.

---

## Part 4: Connect WhatsApp Webhooks

In your Meta Developer App, go to **WhatsApp > Configuration > Webhook**. Click **Edit**:

- **Callback URL:** `https://abc123.ngrok-free.app/webhooks/whatsapp`
- **Verify token:** Your `WHATSAPP_VERIFY_TOKEN`

Click **Verify and Save**. Subscribe to the `messages` field.

Test: Send "Hola, tienen pijamas?" from WhatsApp. The bot should respond within seconds.

---

## Part 5: Deploy to Production (Railway)

### 5.1 Push to GitHub

```bash
git init
git add .
git commit -m "VS Chatbot: all 4 phases complete"
git remote add origin https://github.com/youruser/vs-chatbot.git
git push -u origin main
```

### 5.2 Deploy on Railway

Go to [railway.app](https://railway.app). **New Project > Deploy from GitHub Repo** > select your repo. Add all `.env` variables in the Variables tab. Set `DEBUG=false`. **Set `ADMIN_PASSWORD` to a strong random string** — this protects the admin dashboard and all admin API endpoints in production.

Deploy this store as a **single instance / single worker**. The scheduler runs in-process, so multiple app instances would duplicate scheduled jobs and broadcast checks.

After deploy, get your URL (e.g., `https://vs-chatbot-production.up.railway.app`). Update `APP_BASE_URL` in Railway variables.

Before you go live, open the store dashboard login at:

```text
https://vs-chatbot-production.up.railway.app/admin/login
```

After logging in, go to **Configuración** and add the payment methods you want this store to offer. Each method needs a **Nombre** and **Información**. These are stored in the store DB, not in `.env`, and the bot will list only the configured method names during checkout.

### 5.3 Switch WhatsApp Webhook to Railway

Update the Meta Developer App webhook URL:

```text
https://vs-chatbot-production.up.railway.app/webhooks/whatsapp
```

---

## Part 6: Connect Remaining Webhooks

### 6.1 Telegram Admin Bot

Register the Telegram webhook (run once, requires admin auth):

```bash
curl -X POST "https://vs-chatbot-production.up.railway.app/admin/settings/telegram/setup-webhook" \
  -H "Authorization: Bearer YOUR_ADMIN_PASSWORD"
```

Open Telegram, send `/start` to your bot. You should get the full command menu.

Test:

```text
/stats        -> Today's stats
/settings     -> All current settings
/usage        -> Token usage
```

### 6.2 Instagram (After App Review)

Submit for App Review requesting `instagram_business_basic`, `instagram_business_manage_messages`, and `human_agent`. Include a screencast, privacy policy, and app icon. This takes 1–4 weeks.

After approval:

1. Configure webhook: **Meta Developer App > Webhooks > Instagram > subscribe to `messages`:**
    - **URL:** `https://vs-chatbot-production.up.railway.app/webhooks/instagram`
    - **Verify token:** Your `INSTAGRAM_VERIFY_TOKEN`
2. Update `INSTAGRAM_ACCESS_TOKEN` in Railway.
3. Subscribe the page and set up Ice Breakers:

    ```bash
    curl -X POST "https://your-app.railway.app/admin/settings/instagram/subscribe-page?page_id=YOUR_PAGE_ID" \
      -H "Authorization: Bearer YOUR_ADMIN_PASSWORD"
    curl -X POST "https://your-app.railway.app/admin/settings/instagram/setup-ice-breakers?ig_user_id=YOUR_IG_USER_ID" \
      -H "Authorization: Bearer YOUR_ADMIN_PASSWORD"
    ```

4. Set the Meta app to **Live** mode.

---

## Part 7: Set Up Broadcasts and the Admin Dashboard

### 7.1 Create WhatsApp Message Templates

In Meta Business Manager > WhatsApp > Message Templates, create:

- **`new_arrivals`** (Marketing):
  - Body: "Hola {{1}}, tenemos {{2}} nuevos que te van a encantar! Escríbenos para ver el catálogo."
- **`payment_reminder`** (Utility):
  - Body: "Hola {{1}}, tu pedido aún está pendiente de pago. Escríbenos si necesitas ayuda."
- **`vip_exclusive`** (Marketing):
  - Body: "Hola {{1}}, como cliente VIP tienes acceso exclusivo a {{2}}. Escríbenos!"

Submit for approval (usually takes hours).

### 7.2 Create and Send a Broadcast

**Via Telegram:**

```text
/preview vip,interested:pajamas     -> See matching customer count and cost
```

**Via API:**

```bash
# Create a draft
curl -X POST "https://your-app.railway.app/admin/broadcasts/create" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_ADMIN_PASSWORD" \
  -d '{"name": "New pajamas March", "template_name": "new_arrivals", "target_tags": ["interested:pajamas"], "template_params": ["{first_name}", "pijamas de primavera"]}'

# Send it
curl -X POST "https://your-app.railway.app/admin/broadcasts/[BROADCAST_ID]/send" \
  -H "Authorization: Bearer YOUR_ADMIN_PASSWORD"
```

**Via Telegram:**

```text
/broadcast       -> Shows broadcasts with IDs
/send abc123     -> Sends the broadcast
```

**Via the web dashboard:** Open `/admin/login`, sign in, then use the Broadcasts tab. Fill the form, preview, and send.

### 7.3 Schedule Broadcasts

Add `scheduled_at` when creating:

```bash
curl -X POST "https://your-app.railway.app/admin/broadcasts/create" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_ADMIN_PASSWORD" \
  -d '{"name": "Weekend promo", "template_name": "vip_exclusive", "target_tags": ["vip"], "template_params": ["{first_name}", "descuento de fin de semana"], "scheduled_at": "2026-03-28T10:00:00Z"}'
```

The scheduler checks every minute and sends automatically.

### 7.4 Admin Dashboard

Open `https://your-app.railway.app/admin/login` in any browser, sign in, and you'll be redirected to `/admin/dashboard`.

**Five tabs:**

- **Resumen:** Today's stats, per-channel breakdown, token usage by provider, AI on/off toggle
- **Clientes:** Sortable customer table, inline tag management (add/remove), resolve escalations individually or all at once
- **Pedidos:** Sortable order table with status badges
- **Broadcasts:** Sortable broadcast table, create/preview/send broadcasts, inspect `partial` sends, reset failed broadcasts
- **Configuracion:** Switch LLM provider/model, adjust temperature/max tokens/conversation history, configure fallback, manage store-only payment methods, and generate/download the catalog PDF. Scheduled-job timings are shown read-only here and are managed from `master/`.

All tables in Clientes, Pedidos, and Broadcasts are sortable by clicking column headers. Click once for ascending, again for descending.

Dark mode toggle in the header (🌙/☀️). Persists via localStorage and auto-detects OS preference on first visit.

**Dashboard and API protection:** `ADMIN_PASSWORD` is **required** in production. Browser access goes through `/admin/login`, which sets an HTTP-only session cookie. All `/admin/settings/`, `/admin/broadcasts/`, and `/admin/analytics/` API endpoints accept either that cookie or `Authorization: Bearer YOUR_PASSWORD`. Without `ADMIN_PASSWORD` set in production (`DEBUG=false`), startup fails fast.

**Custom AI persona:** Set `SYSTEM_PROMPT_OVERRIDE` to replace the default `store/prompts/system_prompt.md` template for a specific store deployment. Must use the same `{store_name}`, `{product_catalog}`, etc. placeholders.

**Shared runtime settings:** Dashboard-managed AI settings live in the store DB `settings` table. AI config and `ai_enabled` apply immediately without redeploy. If you manage the store from `master/`, the master dashboard also controls the scheduler timings (`catalog_refresh_minutes`, `broadcast_check_interval_minutes`, `catalog_pdf_interval_hours`, `token_reminder_*`, `daily_analytics_*`), and the store app applies those changes automatically within about a minute. Payment methods are store-only and are edited only from the store dashboard.

**Centralized LLM control:** Set `LLM_MANAGED_EXTERNALLY=true` to lock the store's LLM provider/model/temperature controls. When enabled, the store dashboard hides LLM-only settings, the settings API rejects LLM changes (403), and the Telegram `/provider` command is disabled. Payment methods and other non-LLM store settings remain editable locally.

---

## Part 8: Activate Analytics

### 8.1 Analytics (Available Immediately)

Analytics track everything automatically as conversations happen.

**Via Telegram:**

```text
/conversion          -> Sales funnel (messaged -> inquired -> ordered -> paid)
/conversion 30       -> Same for last 30 days
/performance         -> Response time stats per provider
/products            -> Most-asked-about products
```

**Via API (requires admin auth):**

```bash
curl -H "Authorization: Bearer YOUR_ADMIN_PASSWORD" \
  "https://your-app.railway.app/admin/analytics/conversion?days=7"
curl -H "Authorization: Bearer YOUR_ADMIN_PASSWORD" \
  "https://your-app.railway.app/admin/analytics/response-times?days=7"
curl -H "Authorization: Bearer YOUR_ADMIN_PASSWORD" \
  "https://your-app.railway.app/admin/analytics/popular-products?days=30"
curl -H "Authorization: Bearer YOUR_ADMIN_PASSWORD" \
  "https://your-app.railway.app/admin/analytics/daily?days=14"
```

### 8.2 Daily Aggregation

Runs automatically at 1:00 AM for yesterday's data. To backfill:

```bash
curl -X POST "https://your-app.railway.app/admin/analytics/build-daily" \
  -H "Authorization: Bearer YOUR_ADMIN_PASSWORD"
curl -X POST "https://your-app.railway.app/admin/analytics/build-daily?target_date=2026-03-20" \
  -H "Authorization: Bearer YOUR_ADMIN_PASSWORD"
```

### 8.4 Payment Screenshot Recognition

Works automatically. When a customer sends an image, the system downloads it, runs it through the LLM's vision capability, extracts payment details (method, amount, reference, status), and injects the analysis into the conversation. No setup needed.

---

## Part 9: Complete Testing Checklist

### 9.1 Infrastructure

```text
[ ] GET /health -> status=healthy, scheduler=running, both providers, catalog > 0
[ ] GET /admin/settings/ without auth -> 401 (when ADMIN_PASSWORD is set)
[ ] GET /admin/settings/ with Bearer header -> Settings including llm_max_tokens
[ ] POST /admin/settings/switch-provider?provider=anthropic with auth -> switches cleanly
[ ] GET /admin/settings/usage-summary with auth -> JSON response
[ ] GET /admin/settings/stats/conversations with auth -> channel breakdown
[ ] Open /admin/login -> successful login sets cookie and redirects to /admin/dashboard
[ ] Subsequent visits to /admin/dashboard -> works via cookie (no password in URL)
[ ] Toggle dark mode -> UI switches, persists on refresh
[ ] Configuracion tab -> AI settings and payment methods visible; PDF auto-refresh shown read-only
[ ] Send /start to Telegram bot -> 18-command menu appears
[ ] GET /test/ui with DEBUG=false -> 404 (test endpoints disabled in production)
[ ] GET /test/ui with DEBUG=true -> test page loads
```

### 9.2 WhatsApp Conversations

```text
[ ] "Hola" -> Warm greeting, customer tagged "new_lead"
[ ] "Tienen pijamas?" -> Product list, tagged "interested:pajamas"
[ ] "Me interesa el pijama de rayas en talla M" -> Availability confirmed, tagged "size:M"
[ ] "Quiero pagar por Binance" -> Order created, payment details shown, Telegram notification
[ ] Send a payment screenshot -> Bot reads amount/method/reference from image
[ ] "Quiero hablar con una persona" -> Escalation, Telegram alert (once), AI stops responding
[ ] Subsequent messages from escalated customer -> Stored but no repeat Telegram notification
[ ] /resolve [ID] via Telegram -> AI resumes for that customer
[ ] /ai off -> AI paused globally, incoming messages forwarded to Telegram (once per customer)
[ ] /ai on -> AI resumes for all customers
[ ] "Muestrame el catalogo" -> PDF catalog sent (WhatsApp)
[ ] Repeat order -> AI offers saved address: "¿misma dirección de la última vez?"
[ ] Set product Stock=0 in Sheets, ask for it -> "Out of stock" + alternatives
[ ] "Tienen zapatos?" -> Politely declines, only sells underwear/pajamas
[ ] Payment flow -> Interactive buttons appear for payment method choice
```

### 9.3 Instagram Conversations (After App Review)

```text
[ ] Ice Breakers appear on first DM open
[ ] Tapping an Ice Breaker triggers appropriate response
[ ] Regular DM gets same quality as WhatsApp
[ ] Quick Replies appear for choices (IG equivalent of WA buttons)
[ ] Story reply received and answered
[ ] Long messages split correctly at sentence boundaries
```

### 9.4 Telegram Admin Bot

```text
[ ] /stats -> Today's numbers
[ ] /customers -> Customer list (alphabetical with IDs)
[ ] /customers vip -> Filtered by tag
[ ] /tags [ID] -> Shows all tags for a customer
[ ] /tag [ID] add vip -> Adds tag
[ ] /tag [ID] del new_lead -> Removes tag
[ ] /orders -> Order list (alphabetical by customer)
[ ] /order [ID] confirmed -> Status updates
[ ] /resolve -> Lists escalated customers
[ ] /resolve [ID] -> Resolves one
[ ] /resolve all -> Resolves all
[ ] /ai off -> Pauses AI responses
[ ] /ai on -> Resumes AI responses
[ ] /provider anthropic -> Switches provider
[ ] /usage -> Token costs
[ ] /settings -> All settings
[ ] /catalogpdf -> Generates PDF catalog
[ ] /conversion -> Sales funnel
[ ] /performance -> Response times
[ ] /products -> Popular products
[ ] /preview vip -> Broadcast preview with cost
[ ] /broadcast -> Lists broadcasts
[ ] /send [ID] -> Executes broadcast
```

### 9.5 Broadcasts

```text
[ ] Create broadcast (API or dashboard) -> Appears as "draft"
[ ] /preview [tags] -> Shows customer count and estimated cost
[ ] Send broadcast -> Messages delivered, Telegram notification received
[ ] Scheduled broadcast -> Auto-sends at scheduled time
[ ] Reset stuck broadcast (dashboard "Resetear" button or POST /{id}/reset) -> Returns to "draft"
```

### 9.6 Analytics

```text
[ ] /conversion -> Funnel with rates (after at least one purchase flow)
[ ] /performance -> avg/p50/p95 per provider (after some messages)
[ ] /products -> Popularity ranking (after inventory checks)
```

### 9.7 Resilience

```text
[ ] Break primary API key -> Bot falls back to secondary provider
[ ] Unshare Google Sheet -> Bot uses cached catalog
[ ] Send 5 rapid messages -> All get responses without errors
[ ] /health -> Scheduler shows "running"
```

### 9.8 First-Week Monitoring (Daily Checks)

```text
[ ] /stats -> Message volumes per channel
[ ] /usage -> API costs (~$0.15-0.30/day at normal volume)
[ ] /conversion -> Customers moving through funnel
[ ] /performance -> No response times above 5000ms
[ ] Check Supabase conversations for incorrect AI responses
[ ] Check orders table for stuck orders (pending >24h)
[ ] Verify Telegram escalation and order notifications arrive
[ ] /products -> Address frequently asked-for products you don't carry
```

---

## Part 10: Quick Reference

### All Endpoints

```text
Webhooks (Meta/Telegram call these — no auth, verified by signature):
  GET/POST  /webhooks/whatsapp
  GET/POST  /webhooks/instagram
  POST      /webhooks/telegram

Health (no auth):
  GET  /
  GET  /health

Settings (require ADMIN_PASSWORD via Bearer header or session cookie):
  GET  /admin/settings/
  GET  /admin/settings/providers
  PUT  /admin/settings/{key}
  POST /admin/settings/switch-provider
  GET  /admin/settings/usage-summary
  GET  /admin/settings/stats/conversations
  POST /admin/settings/telegram/setup-webhook
  POST /admin/settings/instagram/setup-ice-breakers
  POST /admin/settings/instagram/subscribe-page

Customers (require admin auth):
  GET  /admin/settings/customers
  PUT  /admin/settings/customers/{id}
  DELETE /admin/settings/customers/{id}
  POST /admin/settings/customers/{id}/resolve
  POST /admin/settings/customers/resolve-all
  GET  /admin/settings/customers/{id}/tags
  POST /admin/settings/customers/{id}/tags
  DELETE /admin/settings/customers/{id}/tags/{tag}
  PUT  /admin/settings/orders/{id}
  DELETE /admin/settings/orders/{id}

Catalog PDF (require admin auth):
  POST /admin/settings/catalog/generate-pdf
  GET  /admin/settings/catalog/pdf-status
  GET  /admin/settings/catalog/download-pdf

Dashboard:
  GET  /admin/login
  POST /admin/login
  POST /admin/logout
  GET  /admin/dashboard

Orders (require admin auth):
  GET  /admin/settings/orders

Broadcasts (require admin auth):
  POST /admin/broadcasts/create
  POST /admin/broadcasts/preview
  GET  /admin/broadcasts/list
  POST /admin/broadcasts/{id}/send
  POST /admin/broadcasts/{id}/reset

Analytics (require admin auth):
  GET  /admin/analytics/conversion
  GET  /admin/analytics/response-times
  GET  /admin/analytics/popular-products
  GET  /admin/analytics/daily
  POST /admin/analytics/build-daily

Testing (DEBUG=true only — disabled in production):
  GET  /test/ui
  POST /test/chat
  GET  /test/catalog
  GET  /test/history
  DELETE /test/reset
```

### All Telegram Commands

```text
/start /stats /customers /orders /order /resolve /provider
/broadcast /send /preview /settings /usage /conversion
/performance /products /catalogpdf
/ai /tags /tag
```

---

## Common Issues and Fixes

- **"WhatsApp webhook verification failed"**  
  Verify token in Meta dashboard doesn't match `.env`.
- **"Invalid signature" errors**  
  `META_APP_SECRET` is wrong. Re-copy from App Settings > Basic.
- **Bot responds slowly (>5s)**  
  Check `/performance`. Switch providers or check Railway region.
- **Bot gives wrong product info**  
  Google Sheets stale. Restart server to force refresh. Verify column headers match exactly.
- **Bot responds in English**  
  Check `store/prompts/system_prompt.md` hasn't been modified.
- **Bot replies with raw JSON**  
  The model narrated internal tool results instead of generating a real response. Check that system prompt rule 13 is intact ("NEVER output raw JSON..."). Also check `MAX_TOOL_ROUNDS` in `engine.py` — if it's set below 6, the model may hit the limit before generating text; the final round must pass `tools=None` to force a text response.
- **Bot reveals stock numbers ("stock: 4")**  
  `_tool_check_inventory` in `engine.py` should not include a `"stock"` key in its return dict — only `"in_stock": true/false`. Check that key isn't present. Also verify system prompt rule 2 contains the "NEVER reveal stock quantities" clause.
- **"Could not load catalog"**  
  Service account email needs Viewer access to the sheet.
- **Telegram notifications not arriving**  
  Must have sent bot `/start` first. Check `TELEGRAM_ADMIN_CHAT_ID` is your ID, not the bot's.
- **Telegram bot not responding**  
  Re-run: `POST /admin/settings/telegram/setup-webhook`
- **Instagram messages not arriving**  
  App must be Live mode. IG account must be Professional. Page subscription must be active.
- **WhatsApp "not registered"**  
  Number must be registered with Cloud API, not regular WhatsApp.
- **Broadcasts send 0 messages**  
  Tags don't match any customers. Use `/preview` first. Verify template name matches Meta Business Manager exactly.
- **Broadcast stuck in "sending"**  
  The send crashed mid-execution. Use the "Resetear" button in the dashboard or `POST /admin/broadcasts/{id}/reset` to return it to draft. Crash recovery now auto-sets failed broadcasts to "failed" status.
- **Payment screenshots not recognized**  
  Check active model supports vision (GPT-4o-mini and Claude Haiku 4.5 do). Check logs for errors.
- **Daily analytics empty**  
  Runs at 1 AM for yesterday. Manually trigger: `POST /admin/analytics/build-daily`.
- **Scheduler not running**  
  Check `/health`. Restart Railway deployment if stopped.

---

## Next: Multi-Store Deployment

To manage multiple stores (e.g., for family members or additional businesses), see the **[Master Control Plane Deployment Guide](master/DEPLOYMENT.md)**. It covers:

- Setting up the centralized master dashboard
- Registering stores and managing encrypted credentials
- Pushing env vars to Railway and triggering redeploys from one place
- Local testing with both services running side by side
