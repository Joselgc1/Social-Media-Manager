# VS Chatbot: Complete Deployment and Testing Guide

Everything you need to go from a fresh laptop to a fully operational AI chatbot handling real customer messages on WhatsApp and Instagram, with broadcast campaigns, an admin dashboard, payment screenshot recognition, and analytics. The store can run in direct Meta mode or Kommo mode.

Important business behavior baked into the current system:

- shipping is offered through `MRW` or `Zoom` with `cobro a destino`
- payment methods are store-defined and managed only from the store dashboard
- the owner can update a store-only daily exchange-rate field used for `¿a qué tasa recibes?`
- the customer PDF catalog hides internal SKU and stock columns

The guide has 11 parts:

- Parts 1 through 4: Set up external services and channel webhooks.
- Part 5: Deploys the code.
- Part 6: Connects the webhooks (Meta or Kommo, plus Telegram).
- Part 7: Sets up broadcasts and the admin dashboard.
- Part 8: Activates analytics.
- Part 9: Multi-agent rollout and rollback.
- Part 10: The full testing checklist.
- Part 11: A quick reference of all endpoints and commands.

**Estimated total time:** 3-5 hours for the core store system. Direct Meta Instagram can add 1-4 weeks for Meta App Review. Kommo mode avoids direct Meta app review but requires Kommo channel, private widget, Salesbot, and webhook setup.

> **Running multiple stores?** This guide covers deploying a single store. For managing multiple stores from a centralized dashboard, see [master/DEPLOYMENT.md](../master/DEPLOYMENT.md).

---



## Part 1: External Accounts and API Keys

You need the core service accounts before touching the code. Then choose one customer-channel backend: direct Meta or Kommo.

### 1.1 Supabase (Database)

Go to [supabase.com](https://supabase.com) and create a free account. Click **New Project**, pick a region close to your deployment server (US East if deploying on Railway), and set a strong database password. **Save this password!**

Once the project is created, go to **Settings > Database**. You'll find your connection string under **Connection string > URI**. It looks like:

```text
postgresql://postgres:[YOUR-PASSWORD]@db.xyzabc.supabase.co:5432/postgres
```

Copy this. It goes in your `.env` as `DATABASE_URL`.

Run migrations from the Supabase SQL Editor. Choose exactly one path:

**Fresh database:** paste and run `store/migrations/001_schema.sql` once. It creates the current schema, seeds settings, and records schema versions through the current application version.

**Existing database created before version tracking:** take a database backup, enter a maintenance window, then run `store/migrations/002_existing_database_upgrade.sql`, `store/migrations/003_broadcast_delivery_safety.sql`, and `store/migrations/004_meta_inbound_lease_fencing.sql` in that order. Do not rerun `001_schema.sql`; `CREATE TABLE IF NOT EXISTS` cannot upgrade an existing table safely.

For future releases, run only new numbered migrations in ascending order. A migration records its version only at the end of its transaction. The store refuses to start when `schema_migrations` is absent, behind, or ahead of the version supported by the deployed code.

Verify with `SELECT version, name, applied_at FROM schema_migrations ORDER BY version;`. The latest version must match `EXPECTED_SCHEMA_VERSION` in `store/app/db.py` for the deployed code. Then confirm the `settings` table contains the runtime defaults used by the dashboard and scheduler.

### 1.2 OpenAI API Key

Go to [platform.openai.com](https://platform.openai.com), sign in, and navigate to **API Keys**. Click **Create new secret key**, name it "vs-chatbot," and copy it. It starts with `sk-`.

Add $10 of credit under Billing. At your message volume, this lasts 1–2 months with the default GPT-5.4 Nano model.

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
SKU | Parent SKU | Product name | Category | Description | Size | Price USD | Stock | Active | Image URL
```

Use **one row per size variant**. That means every sellable `product + size` combination gets its own row and its own unique `SKU`.

Rules:

- `SKU` must be unique for each row, for example `SET-001-S` and `SET-001-M`
- `Parent SKU` must be shared by all size variants of the same product, for example `SET-001`
- `Size` must contain a single size only, such as `S`, `M`, `L`, `XL`, or `XXL`
- `Stock` must be the stock for that exact size row only
- Use the same `Product name`, `Category`, `Description`, and `Image URL` across rows that belong to the same product unless you intentionally want them to differ

If you want the bot to be able to send product photos, use the `Image URL` column. Supported formats:

- a plain public image URL
- a Google Drive share link for a publicly accessible image
- a Google Sheets formula like `=IMAGE("https://...")`

If the customer asks to see a specific product and that row has a usable image, the bot can send it directly in chat.

Add a few test products:

```text
PJ-001-S | PJ-001 | Pijama rayas rosa | Pajamas | Pijama de algodón con rayas rosas | S | 28.00 | 5 | Yes |
PJ-001-M | PJ-001 | Pijama rayas rosa | Pajamas | Pijama de algodón con rayas rosas | M | 28.00 | 8 | Yes |
PJ-001-L | PJ-001 | Pijama rayas rosa | Pajamas | Pijama de algodón con rayas rosas | L | 28.00 | 3 | Yes |
UN-001-S | UN-001 | Conjunto encaje negro | Underwear | Conjunto de encaje negro Victoria's Secret | S | 22.00 | 2 | Yes |
UN-001-M | UN-001 | Conjunto encaje negro | Underwear | Conjunto de encaje negro Victoria's Secret | M | 22.00 | 1 | Yes |
PJ-002-M | PJ-002 | Pijama satén azul | Pajamas | Pijama de satén azul marino | M | 32.00 | 2 | Yes |
PJ-002-L | PJ-002 | Pijama satén azul | Pajamas | Pijama de satén azul marino | L | 32.00 | 2 | Yes |
PJ-002-XL | PJ-002 | Pijama satén azul | Pajamas | Pijama de satén azul marino | XL | 32.00 | 1 | Yes |
SET-001-S | SET-001 | Set completo rojo | Sets | Set de ropa interior completo rojo | S | 35.00 | 4 | Yes |
SET-001-M | SET-001 | Set completo rojo | Sets | Set de ropa interior completo rojo | M | 35.00 | 7 | Yes |
SET-001-L | SET-001 | Set completo rojo | Sets | Set de ropa interior completo rojo | L | 35.00 | 2 | Yes |
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

### 1.6 Choose a Channel Backend

Set exactly one channel backend per store:


| Backend | Use When                                                                       | Webhooks                                                      | Sends Replies Through |
| ------- | ------------------------------------------------------------------------------ | ------------------------------------------------------------- | --------------------- |
| `meta`  | You want direct WhatsApp Cloud API and Instagram Messaging API control.        | `/webhooks/whatsapp`, `/webhooks/instagram`                   | Meta Graph API        |
| `kommo` | You want Kommo to own WhatsApp/Instagram channel connections and shared inbox. | `/webhooks/kommo/events/{secret}`, `/webhooks/kommo/salesbot` | Kommo Salesbot        |


For new direct-Meta stores, continue with section 1.7. For Kommo stores, skip direct Meta setup and follow [docs/KOMMO_MIGRATION.md](../docs/KOMMO_MIGRATION.md) after the core Supabase, LLM, Google Sheets, and Telegram setup is complete.

### 1.7 Meta Developer App (WhatsApp + Instagram APIs)

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
CHANNEL_BACKEND=meta
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
TELEGRAM_WEBHOOK_SECRET=
STORE_NAME="Zona Pink"
OWNER_NAME=Carlos
APP_BASE_URL=https://your-app.railway.app
DEBUG=true
AI_ORCHESTRATION_MODE=legacy
```

For Kommo local testing, replace the Meta channel variables with:

```bash
CHANNEL_BACKEND=kommo
KOMMO_SUBDOMAIN=your-account-subdomain
KOMMO_ACCESS_TOKEN=...
KOMMO_INTEGRATION_ID=...
KOMMO_INTEGRATION_SECRET=...
KOMMO_SALESBOT_ID=123456
KOMMO_WEBHOOK_SECRET=your-random-path-secret
KOMMO_AI_MODE_FIELD_ID=111
KOMMO_AI_ACTIVE_ENUM_ID=222
KOMMO_AI_HUMAN_ENUM_ID=333
KOMMO_AI_PAUSED_ENUM_ID=444
KOMMO_DEFAULT_RESPONSIBLE_USER_ID=
```

`KOMMO_SUBDOMAIN` is the account subdomain only, for example `acme`, not `https://acme.kommo.com`.

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
[INFO] Background scheduler started.
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

> **Note:** With `DEBUG=true` and no `ADMIN_PASSWORD` set, admin routes are accessible without auth for local development. In production, startup validation requires a non-placeholder `ADMIN_PASSWORD` of at least 12 characters and at least one real LLM API key. Documented sample credentials are rejected. `CHANNEL_BACKEND=meta` requires the full WhatsApp config (`META_APP_SECRET`, `WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_VERIFY_TOKEN`). `CHANNEL_BACKEND=kommo` requires the Kommo private integration, private-message Salesbot, webhook secret, and AI Mode field/enum variables. Telegram is optional, but if you enable it, provide `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ADMIN_CHAT_ID`, and `TELEGRAM_WEBHOOK_SECRET` together.

---



## Part 3: Expose Your Local Server (for Development)

Meta and Kommo both require a publicly accessible HTTPS URL for webhooks. Use ngrok during development.

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



## Part 4: Connect Channel Webhooks



### 4.1 Meta Mode: WhatsApp Webhook

In your Meta Developer App, go to **WhatsApp > Configuration > Webhook**. Click **Edit**:

- **Callback URL:** `https://abc123.ngrok-free.app/webhooks/whatsapp`
- **Verify token:** Your `WHATSAPP_VERIFY_TOKEN`

Click **Verify and Save**. Subscribe to the `messages` field.

Test: Send "Hola, tienen pijamas?" from WhatsApp. The bot should respond within seconds.

### 4.2 Kommo Mode: General Webhook and Salesbot

Only do this when `CHANNEL_BACKEND=kommo`.

1. Confirm the correct numbered migration path has been completed and the latest `schema_migrations` version matches `EXPECTED_SCHEMA_VERSION` in `store/app/db.py`.
2. Build and upload the private widget from `store/kommo-widget/` with `python3 build_widget.py --widget-code <kommo-widget-code>`.
3. Create the private-message Kommo Salesbot with the `Ask Eva AI for DMs` widget step pointing to `https://abc123.ngrok-free.app/webhooks/kommo/salesbot`, ending in a Message step with `{{json.message}}`.
4. Create the public-comment Kommo Salesbot with Kommo's native `When a comment is received` trigger, the `Ask Eva AI for Instagram comments` widget step, and a Comment step with `{{json.message}}`.
5. Register a Kommo general webhook at `https://abc123.ngrok-free.app/webhooks/kommo/events/<KOMMO_WEBHOOK_SECRET>`.
6. Subscribe to incoming message, outgoing message, lead edited, talk added, and talk edited events.
7. Confirm `GET /admin/settings/kommo/status` and `POST /admin/settings/kommo/test` work with admin auth.

Instagram public comments should use the native comment-triggered Salesbot. Kommo can also mirror those comments through the general webhook as `origin=instagram_business`, `message_type=text`; the backend reconciles the authenticated comment callback against any recent matching private-message mirror and discards the mirror before the private-message Salesbot is launched.

The complete Kommo setup is documented in [docs/KOMMO_MIGRATION.md](../docs/KOMMO_MIGRATION.md).

---



## Part 5: Deploy to Production (Railway)



### 5.1 Push to GitHub

```bash
git init
git add .
git commit -m "Deploy VS Chatbot"
git remote add origin https://github.com/youruser/vs-chatbot.git
git push -u origin main
```



### 5.2 Deploy on Railway

Go to [railway.app](https://railway.app). **New Project > Deploy from GitHub Repo** > select your repo. Set the Railway service **Root Directory** to `store/`. Add all `.env` variables in the Variables tab. Set `DEBUG=false`. **Set** `ADMIN_PASSWORD` **to a strong random string** — this protects the admin dashboard and all admin API endpoints in production.

Minimum production variables shared by both backends:


| Variable                                            | Notes                                                                 |
| --------------------------------------------------- | --------------------------------------------------------------------- |
| `CHANNEL_BACKEND`                                   | `meta` or `kommo`                                                     |
| `ADMIN_PASSWORD`                                    | Required when `DEBUG=false`                                           |
| `OPENAI_API_KEY` or `ANTHROPIC_API_KEY`             | At least one is required                                              |
| `DATABASE_URL`                                      | Store Supabase URL, preferably session pooler if direct DB is blocked |
| `GOOGLE_SHEETS_CREDENTIALS_B64`                     | Base64 service-account JSON                                           |
| `PRODUCT_SHEET_ID`                                  | Google Sheets catalog ID                                              |
| `STORE_NAME`, `OWNER_NAME`, `APP_BASE_URL`, `DEBUG` | Store metadata/runtime                                                |


Additional variables for `CHANNEL_BACKEND=meta`:


| Variable                                           | Notes                                                |
| -------------------------------------------------- | ---------------------------------------------------- |
| `META_APP_SECRET`                                  | Used for webhook signature verification              |
| `WHATSAPP_ACCESS_TOKEN`                            | WhatsApp Cloud API token                             |
| `WHATSAPP_PHONE_NUMBER_ID`                         | WhatsApp sender phone number ID                      |
| `WHATSAPP_VERIFY_TOKEN`                            | Meta webhook verification token                      |
| `INSTAGRAM_ACCESS_TOKEN`, `INSTAGRAM_VERIFY_TOKEN` | Optional, but all-or-nothing if Instagram is enabled |


Additional variables for `CHANNEL_BACKEND=kommo`:


| Variable                            | Notes                                      |
| ----------------------------------- | ------------------------------------------ |
| `KOMMO_SUBDOMAIN`                   | Account subdomain only, for example `acme` |
| `KOMMO_ACCESS_TOKEN`                | Long-lived private integration token       |
| `KOMMO_INTEGRATION_ID`              | Private integration ID/client UUID         |
| `KOMMO_INTEGRATION_SECRET`          | JWT validation secret                      |
| `KOMMO_SALESBOT_ID`                 | Private-message Salesbot with the widget   |
| `KOMMO_WEBHOOK_SECRET`              | Random path secret for general webhook URL |
| `KOMMO_AI_MODE_FIELD_ID`            | Lead field ID for AI Mode                  |
| `KOMMO_AI_ACTIVE_ENUM_ID`           | Enum ID for AI Active                      |
| `KOMMO_AI_HUMAN_ENUM_ID`            | Enum ID for Human                          |
| `KOMMO_AI_PAUSED_ENUM_ID`           | Enum ID for Paused                         |
| `KOMMO_DEFAULT_RESPONSIBLE_USER_ID` | Optional assignment target on escalation   |


Deploy this store as a **single instance / single worker**. The scheduler runs in-process, so multiple app instances would duplicate scheduled jobs and broadcast checks.

After deploy, get your URL (e.g., `https://vs-chatbot-production.up.railway.app`). Update `APP_BASE_URL` in Railway variables.

Before you go live, open the store dashboard login at:

```text
https://vs-chatbot-production.up.railway.app/admin/login
```

After logging in, go to **Configuración** and add the payment methods you want this store to offer. Each method needs a **Nombre** and **Información**. These are stored in the store DB, not in `.env`, and the bot will list only the configured method names during checkout. Keep `ai_orchestration_mode` on `legacy` until you complete the rollout checklist in Part 9.

### 5.3 Switch Channel Webhooks to Railway

For Meta mode, update the Meta Developer App webhook URL:

```text
https://vs-chatbot-production.up.railway.app/webhooks/whatsapp
```

For Kommo mode, update:

- Salesbot widget URL in both the private-message Salesbot and native comment-triggered Salesbot: `https://vs-chatbot-production.up.railway.app/webhooks/kommo/salesbot`
- General webhook URL: `https://vs-chatbot-production.up.railway.app/webhooks/kommo/events/<KOMMO_WEBHOOK_SECRET>`

---



## Part 6: Connect Remaining Webhooks



### 6.1 Telegram Admin Bot

Configure `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ADMIN_CHAT_ID`, and a random
`TELEGRAM_WEBHOOK_SECRET` first. Generate the secret with
`python -c "import secrets; print(secrets.token_urlsafe(32))"`.

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



### 6.2 Instagram in Meta Mode (After App Review)

This section applies only to `CHANNEL_BACKEND=meta`. In Kommo mode, connect Instagram inside Kommo and test that Instagram DMs appear in the Kommo inbox before enabling the store app's Kommo webhooks.

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

Broadcast delivery depends on the channel backend. In Meta mode, this app can send approved WhatsApp Cloud API templates. In Kommo mode, direct WhatsApp broadcast delivery is rejected before sending; use Kommo broadcasts or approved Kommo WhatsApp template flows instead. Existing broadcast records, previews, and history still work in the dashboard.

### 7.1 Create WhatsApp Message Templates

In Meta Business Manager > WhatsApp > Message Templates, create:

- `new_arrivals` (Marketing):
  - Body: "Hola {{1}}, tenemos {{2}} nuevos que te van a encantar! Escríbenos para ver el catálogo."
- `payment_reminder` (Utility):
  - Body: "Hola {{1}}, tu pedido aún está pendiente de pago. Escríbenos si necesitas ayuda."
- `vip_exclusive` (Marketing):
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

In Kommo mode, a due scheduled broadcast is marked `failed` with an explanatory error instead of retrying forever.

### 7.4 Admin Dashboard

Open `https://your-app.railway.app/admin/login` in any browser, sign in, and you'll be redirected to `/admin/dashboard`.

**Five tabs:**

- **Resumen:** Today's stats, per-channel breakdown, token usage by provider, AI on/off toggle
- **Clientes:** Sortable customer table, inline tag management (add/remove), inline state/channel editing, and resolve escalations individually or all at once. In Kommo mode, dashboard reactivation verifies the lead's `AI Mode=AI Active` before local history is cleared.
- **Pedidos:** Sortable order table with status badges
- **Broadcasts:** Sortable broadcast table, create/preview/send broadcasts, inspect `partial` sends, reset failed broadcasts
- **Configuracion:** Switch LLM provider/model, adjust temperature/max tokens/conversation history, choose orchestration mode, configure fallback, manage store-only payment methods, and generate/download the catalog PDF. Scheduled-job timings are shown read-only here and are managed from `master/`.

All tables in Clientes, Pedidos, and Broadcasts are sortable by clicking column headers. Click once for ascending, again for descending.

Dark mode toggle in the header (🌙/☀️). Persists via localStorage and auto-detects OS preference on first visit.

**Dashboard and API protection:** A non-placeholder `ADMIN_PASSWORD` of at least 12 characters is **required** in production. Browser access goes through `/admin/login`, which sets an HTTP-only session cookie. All `/admin/settings/`, `/admin/broadcasts/`, and `/admin/analytics/` API endpoints accept either that cookie or `Authorization: Bearer YOUR_PASSWORD`. Without a valid `ADMIN_PASSWORD` in production (`DEBUG=false`), startup fails fast.

**Custom AI persona:** Set `SYSTEM_PROMPT_OVERRIDE` to replace the default `store/prompts/system_prompt.md` template for a specific store deployment. Must use the same `{store_name}`, `{product_catalog}`, etc. placeholders.

**Shared runtime settings:** Dashboard-managed AI settings live in the store DB `settings` table. AI config, `ai_enabled`, and `ai_orchestration_mode` apply immediately without redeploy. If you manage the store from `master/`, the master dashboard also controls the scheduler timings (`catalog_refresh_minutes`, `broadcast_check_interval_minutes`, `catalog_pdf_interval_hours`, `token_reminder_*`, `daily_analytics_*`), and the store app applies those changes automatically within about a minute. Payment methods are store-only and are edited only from the store dashboard.

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

Works automatically. When a customer sends an image, the system downloads it, runs it through the LLM's vision capability, extracts payment details (method, amount, reference, status), then deterministically verifies open order, amount, method, recipient, and completed status before changing payment state. The LLM does not decide whether to mark a payment as paid. No setup needed.

### 8.5 AI Run Observability

`usage_log` keeps token and cost tracking. `ai_run_logs` records safe routing metadata for rollout monitoring: orchestration mode, selected agent, route intent/source/confidence, provider/model, token counts, response time, tool names, tool rounds, and handoff/fallback/escalation/shadow/legacy fallback flags.

Do not add payment credentials, raw image contents, full addresses, customer message text, tool arguments, or raw tool results to this table.

---



## Part 9: Multi-Agent Rollout and Rollback

The default mode is `legacy`. Keep it that way until you are ready to evaluate the new specialist agents.

Modes:

- `legacy`: existing single-agent behavior and rollback mode.
- `shadow`: legacy serves customer responses while specialist routing is logged for evaluation.
- `multi_agent`: deterministic guards and the router select `sales`, `checkout`, `support`, or legacy fallback.

Precedence is: valid `ai_orchestration_mode` row in the store DB, then valid `AI_ORCHESTRATION_MODE` env default, then hard-coded `legacy`.

Rollout checklist:

1. Deploy with `AI_ORCHESTRATION_MODE=legacy` and confirm the store DB setting is `legacy`.
2. Run `python3 -m pytest tests/store/test_ai_transcript_regressions.py -q` locally.
3. Switch one low-risk store to `shadow` from the store dashboard or master dashboard.
4. Inspect app logs and `ai_run_logs` for unexpected route decisions or legacy fallbacks.
5. Switch to `multi_agent` only after shadow routing is acceptable.
6. Roll back by setting `ai_orchestration_mode=legacy` from either dashboard.

Agent and tool locations:

- Agent definitions and allowlists: `store/app/ai/agents/`
- Tool schemas: `store/app/ai/tools/definitions.py`
- Tool handlers: `store/app/ai/tools/`
- Tool dispatch: `store/app/ai/tools/executor.py`
- Prompt fragments: `store/prompts/shared/` and `store/prompts/agents/`
- Deterministic payment verification: `store/app/ai/payment/verifier.py`

---



## Part 10: Complete Testing Checklist



### 9.1 Infrastructure

```text
[ ] GET /health -> status=healthy, scheduler=running, both providers, catalog > 0
[ ] GET /admin/settings/ without auth -> 401 (when ADMIN_PASSWORD is set)
[ ] GET /admin/settings/ with Bearer header -> Settings including llm_max_tokens and ai_orchestration_mode
[ ] POST /admin/settings/switch-provider?provider=anthropic with auth -> switches cleanly
[ ] GET /admin/settings/usage-summary with auth -> JSON response
[ ] GET /admin/settings/stats/conversations with auth -> channel breakdown
[ ] Open /admin/login -> successful login sets cookie and redirects to /admin/dashboard
[ ] Subsequent visits to /admin/dashboard -> works via cookie (no password in URL)
[ ] Toggle dark mode -> UI switches, persists on refresh
[ ] Configuracion tab -> AI settings, orchestration mode, payment methods, and store-only settings visible; PDF auto-refresh shown read-only
[ ] Send /start to Telegram bot -> command menu appears
[ ] CHANNEL_BACKEND=meta -> /webhooks/whatsapp and /webhooks/instagram are registered
[ ] CHANNEL_BACKEND=kommo -> /webhooks/kommo/events/{secret} and /webhooks/kommo/salesbot are registered
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
[ ] "Muestrame el catalogo" in Meta WhatsApp -> PDF catalog sent
[ ] "Muestrame el catalogo" in Kommo or Instagram -> normal text catalog guidance, no PDF attachment
[ ] Repeat order -> AI offers saved address: "¿misma dirección de la última vez?"
[ ] Set product Stock=0 in Sheets, ask for it -> "Out of stock" + alternatives
[ ] "Tienen zapatos?" -> Politely declines, only sells underwear/pajamas
[ ] Payment flow -> Interactive buttons appear for payment method choice
[ ] Set ai_orchestration_mode=shadow -> Legacy response is served and route metadata is logged
[ ] Set ai_orchestration_mode=multi_agent -> Sales, checkout, and support routes respond correctly
[ ] Set ai_orchestration_mode=legacy -> Immediate rollback to legacy behavior
```



### 9.3 Instagram Conversations (After App Review)

For Meta mode, test direct Instagram Messaging API behavior. For Kommo mode, test that Instagram DMs enter Kommo and are answered through the Salesbot flow.

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
[ ] Meta mode send broadcast -> Messages delivered, Telegram notification received
[ ] Meta mode scheduled broadcast -> Auto-sends at scheduled time
[ ] Kommo mode send broadcast -> Rejected with a clear Kommo-mode message
[ ] Kommo mode scheduled broadcast -> Marked failed instead of retrying forever
[ ] Reset stuck broadcast (dashboard "Resetear" button or POST /{id}/reset) -> Returns to "draft"
```



### 9.5b Kommo Mode

```text
[ ] Store schema_migrations reports version 2 (001 for fresh DB, 002 for existing DB)
[ ] Widget ZIP uploaded to private Kommo integration
[ ] Salesbot contains widget step pointing to /webhooks/kommo/salesbot
[ ] General webhook points to /webhooks/kommo/events/<KOMMO_WEBHOOK_SECRET>
[ ] POST /admin/settings/kommo/test with auth -> read-only checks pass
[ ] WhatsApp message appears in Kommo inbox and creates a Kommo job
[ ] Customer receives AI response through Kommo Salesbot
[ ] Lead AI Mode=Human -> local customer becomes escalated and AI stops
[ ] Lead AI Mode=AI Active -> AI can answer the next inbound message
[ ] Store dashboard resolves a Kommo escalation -> Kommo AI Mode is confirmed active before local state/history changes
```



### 9.6 Analytics

```text
[ ] /conversion -> Funnel with rates (after at least one purchase flow)
[ ] /performance -> avg/p50/p95 per provider (after some messages)
[ ] /products -> Popularity ranking (after inventory checks)
[ ] ai_run_logs -> Contains route metadata without message text, addresses, payment credentials, or raw tool arguments
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



## Part 11: Quick Reference



### All Endpoints

```text
Webhooks (no admin auth, verified by channel-specific secret/signature):
  Meta mode:
    GET/POST  /webhooks/whatsapp
    GET/POST  /webhooks/instagram
  Kommo mode:
    POST      /webhooks/kommo/events/{webhook_secret}
    POST      /webhooks/kommo/salesbot
  Always available:
    POST      /webhooks/telegram

Health (no auth):
  GET  /
  GET  /health

Settings (require ADMIN_PASSWORD via Bearer header or session cookie):
  GET  /admin/settings/
  GET  /admin/settings/payment-methods
  PUT  /admin/settings/payment-methods
  GET  /admin/settings/providers
  PUT  /admin/settings/{key}
  POST /admin/settings/switch-provider
  GET  /admin/settings/usage-summary
  GET  /admin/settings/stats/conversations
  GET  /admin/settings/kommo/status
  POST /admin/settings/kommo/test
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

Orders (require admin auth):
  GET  /admin/settings/orders
  GET  /admin/settings/orders/{id}
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
  GET  /admin/orders/{order_id}

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
- **Kommo Salesbot callbacks return 401**
  Verify `KOMMO_INTEGRATION_SECRET`, `KOMMO_INTEGRATION_ID`, `KOMMO_SUBDOMAIN`, and the widget request JWT. Confirm the Salesbot widget URL points to `/webhooks/kommo/salesbot`.
- **Kommo jobs stuck in `waiting_for_salesbot`**
  The backend marks stale waits as failed after about 3 minutes so new inbound messages can retry. If this repeats for DMs, verify the uploaded widget is present in the private-message Salesbot, the widget URL is reachable over HTTPS, and the Salesbot ID matches `KOMMO_SALESBOT_ID`. Public Instagram comments should use Kommo's native comment trigger and should not create backend-launched waits.
- **Kommo image payment screenshots are ignored**
  Direct media downloads are intentionally limited to trusted Meta/Instagram/Kommo hosts over HTTPS, with redirects disabled and a 5 MB size limit. Some Kommo media payloads may need manual production validation.
- **Kommo catalog requests do not send PDFs**
  This is expected. The catalog PDF is generated/downloaded from the admin dashboard and can be sent only by the direct Meta WhatsApp `send_catalog_pdf` tool. Kommo and Instagram catalog requests should produce normal text replies.
- **WhatsApp "not registered"**  
  Number must be registered with Cloud API, not regular WhatsApp.
- **Broadcasts send 0 messages**  
  Tags don't match any customers. Use `/preview` first. Verify template name matches Meta Business Manager exactly.
- **Broadcasts fail immediately in Kommo mode**
  This is expected. This backend does not send direct WhatsApp Cloud API broadcasts when `CHANNEL_BACKEND=kommo`; use Kommo broadcasts or approved Kommo WhatsApp template flows.
- **Broadcast stuck in "sending"**  
  The send crashed mid-execution. Use the "Resetear" button in the dashboard or `POST /admin/broadcasts/{id}/reset` to return it to draft. Crash recovery now auto-sets failed broadcasts to "failed" status.
- **Payment screenshots not recognized**  
  Check the active model/provider supports vision (current OpenAI and Anthropic defaults do). Check logs for extraction and deterministic verification errors.
- **Multi-agent rollout behaves unexpectedly**
  Set `ai_orchestration_mode=legacy` in the store settings table from the store or master dashboard. Then inspect `ai_run_logs` and rerun `tests/store/test_ai_transcript_regressions.py` locally before enabling `shadow` or `multi_agent` again.
- **Daily analytics empty**  
  Runs at 1 AM for yesterday. Manually trigger: `POST /admin/analytics/build-daily`.
- **Scheduler not running**  
  Check `/health`. Restart Railway deployment if stopped.

---



## Next: Multi-Store Deployment

To manage multiple stores (e.g., for family members or additional businesses), see the **[Master Control Plane Deployment Guide](../master/DEPLOYMENT.md)**. It covers:

- Setting up the centralized master dashboard
- Registering stores and managing encrypted credentials
- Pushing env vars to Railway and triggering redeploys from one place
- Local testing with both services running side by side
