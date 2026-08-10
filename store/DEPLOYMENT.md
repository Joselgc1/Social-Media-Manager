# VS Chatbot: Complete Deployment and Testing Guide

Everything you need to go from a fresh laptop to a fully operational AI chatbot handling real customer messages on WhatsApp and Instagram, with broadcast campaigns, an admin dashboard, payment screenshot recognition, and analytics. Instagram always uses native Meta; WhatsApp can use direct Meta or Kommo.

Important business behavior baked into the current system:

- Instagram is informational for product discovery, prices, presentations/options (including clothing sizes), availability, recommendations, comparisons, general shipping/payment-option questions, read-only support, and product images; buying, payment, checkout-specific delivery details, checkout continuation, and PDF delivery hand off to WhatsApp through a backend-generated `wa.me` link
- shipping is offered through `MRW` or `Zoom` with `cobro a destino`
- payment methods are store-defined and managed only from the store dashboard
- the owner or Master can update the selected/manual exchange-rate settings used for `¿a qué tasa recibes?`
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

**Estimated total time:** 3-5 hours for the core store system. Native Meta Instagram can add 1-4 weeks for Meta App Review. Kommo WhatsApp requires a Kommo channel, private widget, Salesbot, and webhook setup but does not replace Instagram's Meta setup.

> **Running multiple stores?** This guide covers deploying a single store. For managing multiple stores from a centralized dashboard, see [master/DEPLOYMENT.md](../master/DEPLOYMENT.md).

---



## Part 1: External Accounts and API Keys

You need the core service accounts before touching the code. Then choose a WhatsApp backend: direct Meta or Kommo. Instagram remains native Meta in both cases.

### 1.1 Railway PostgreSQL Database

In the Railway project, add a PostgreSQL service named `StorePostgres`. Deploy the Store service from this repository with root directory `store/`, then set:

```text
DATABASE_URL=${{StorePostgres.DATABASE_URL}}
```

`store/railway.toml` runs `python scripts/migrate.py` before every deployment and starts the Store with `uvicorn app.main:app --host 0.0.0.0 --port $PORT`. The runner applies the normal migration sequence through Store schema version `16`; a failure blocks the deployment. Migration 016 completes Meta-native Instagram support, deprecates but does not drop the legacy Instagram/Kommo correlation schema, and adds durable echo reconciliation and comment thread scope. It also blocks deployment while launched or uncertain legacy Instagram Kommo jobs remain; disable legacy Instagram Kommo ingress, let the old deployment drain those jobs, and retry. For local development use a normal URL such as `postgresql://postgres:password@localhost:5432/store_db` and run `cd store && python scripts/migrate.py`. Do not run individual SQL files for a normal install or upgrade.

Back up before changing an existing database. See [Railway PostgreSQL Deployment](../docs/RAILWAY_POSTGRES.md) for Store/Master setup, legacy recovery migrations, backup, and cutover instructions.

### 1.2 OpenAI API Key

Go to [platform.openai.com](https://platform.openai.com), sign in, and navigate to **API Keys**. Click **Create new secret key**, name it "vs-chatbot," and copy it. It starts with `sk-`.

Add $10 of credit under Billing. At your message volume, this lasts 1–2 months with the default GPT-5.4 Nano model.

### 1.3 Anthropic API Key

Go to [console.anthropic.com](https://console.anthropic.com), sign in, and navigate to **API Keys**. Click **Create Key**, name it "vs-chatbot," and copy it. It starts with `sk-ant-`.

Add $10 of credit under Plans & Billing.

### 1.4 Google Sheets (Product Catalog)

You need a Google Cloud service account so the bot can read the product catalog and update inventory after checkout.

Go to [console.cloud.google.com](https://console.cloud.google.com). Create a new project called "vs-chatbot." Then:

1. Go to **APIs & Services > Library**. Search for "Google Sheets API" and enable it.
2. Go to **APIs & Services > Credentials**. Click **Create Credentials > Service Account**.
3. Name it "vs-chatbot-reader," skip the optional steps, and click **Done**.
4. Click on the service account. Go to the **Keys** tab, click **Add Key > Create new key > JSON**. A file downloads.
5. Base64-encode this file:
   - Mac: `base64 -i your-downloaded-file.json`
   - Linux: `base64 -w 0 your-downloaded-file.json`
   - Copy the output. This goes in `.env` as `GOOGLE_SHEETS_CREDENTIALS_B64`.

Now create the product catalog spreadsheet. Open Google Sheets, create a new sheet, and use these headers:

```text
SKU | Parent SKU | Product Name | Brand | Category | Description | Size | Price USD | Stock | Active | Image URL
```

`Brand` is optional for backward compatibility. Existing sheets without the column still load correctly.

Use **one row per sellable SKU/variant**. `Parent SKU` groups variants that belong to the same customer-facing product. The historical column name `Size` is intentionally kept, but it now means the product's sellable **presentation/option**, not only a clothing size.

Examples of valid `Size` values include:

- clothing: `S`, `M`, `L`
- perfume: `30 ml`, `50 ml`, `100 ml`
- shoes: `38`, `39`, `40`
- electronics/storage: `128 GB`, `256 GB`
- standalone products with no meaningful variant: leave `Size` blank

Rules:

- `SKU` must be unique for each sellable row, for example `PJ-001-M` or `DIOR-SAV-100`
- `Parent SKU` should be shared by all variants of the same product, for example `PJ-001` or `DIOR-SAV`
- `Brand` is optional and participates in catalog search
- `Size` may contain any single presentation/option value; matching is case-insensitive
- `Price USD` and `Stock` belong to that exact SKU/variant, so variants may have different prices
- if a product has no meaningful presentation and only one sellable SKU, `Size` may be blank and checkout will not ask for it
- if a product has multiple sellable variants, the customer must select the desired presentation before checkout can finalize
- use the same `Product Name`, `Brand`, `Category`, `Description`, and `Image URL` across related rows unless they intentionally differ

Clothing example:

```text
PJ-001-S | PJ-001 | Pijama rayas rosa | Victoria's Secret | Pajamas | Pijama de algodón con rayas rosas | S | 28.00 | 5 | Yes |
PJ-001-M | PJ-001 | Pijama rayas rosa | Victoria's Secret | Pajamas | Pijama de algodón con rayas rosas | M | 28.00 | 8 | Yes |
PJ-001-L | PJ-001 | Pijama rayas rosa | Victoria's Secret | Pajamas | Pijama de algodón con rayas rosas | L | 28.00 | 3 | Yes |
```

Perfume example with variant-specific prices:

```text
DIOR-SAV-50 | DIOR-SAV | Sauvage EDT | Dior | Perfumes | Fragancia fresca amaderada | 50 ml | 85.00 | 3 | Yes |
DIOR-SAV-100 | DIOR-SAV | Sauvage EDT | Dior | Perfumes | Fragancia fresca amaderada | 100 ml | 125.00 | 2 | Yes |
```

If you want the bot to be able to send product photos, use the `Image URL` column. Supported formats:

- a plain public image URL
- a Google Drive share link for a publicly accessible image
- a Google Sheets formula like `=IMAGE("https://...")`

If the customer asks to see a specific product and that row has a usable image, the bot can send it directly in chat.

For the complete field rules and additional examples, see [Product Catalog Schema](../docs/CATALOG_SCHEMA.md).

Share the sheet with the service account email (looks like `vs-chatbot-reader@your-project.iam.gserviceaccount.com`). Give it **Editor** access (write permission). Read-only Viewer access is not enough: checkout decrements the `Stock` column and writes the hidden `Inventory Movements` worksheet, so the service account must be able to write to the sheet.

Copy the sheet ID from the URL (the long string between `/d/` and `/edit`). This goes in `.env` as `PRODUCT_SHEET_ID`.

### 1.5 Telegram Bot (Admin Notifications + Management)

The Telegram bot serves two purposes: it receives escalation/order notifications, and it lets you manage the entire system with commands from your phone.

Open Telegram, search for **@BotFather**, and send `/newbot`. Follow the prompts:

- Bot name: "VS Chatbot Admin"
- Username: something like `vs_chatbot_admin_bot`

BotFather gives you a token like `123456789:ABCdefGHIjklMNOpqrSTUvwxYZ`. This goes in `.env` as `TELEGRAM_BOT_TOKEN`.

Now get the admin's chat ID. Search for **@userinfobot** on Telegram and send it any message. It replies with your numeric user ID. This goes in `.env` as `TELEGRAM_ADMIN_CHAT_ID`.

Send a message to your bot (search for it by username, click **Start**). This is necessary so the bot has permission to message you.

### 1.6 Choose a WhatsApp Backend

Set exactly one WhatsApp backend per store:


| Backend | Use When                                                   | WhatsApp Webhooks                                             | Sends WhatsApp Replies Through |
| ------- | ---------------------------------------------------------- | ------------------------------------------------------------- | ------------------------------ |
| `meta`  | You want direct WhatsApp Cloud API control.                 | `/webhooks/whatsapp`                                          | Meta Graph API                 |
| `kommo` | You want Kommo to own WhatsApp and its shared inbox.        | `/webhooks/kommo/events/{secret}`, `/webhooks/kommo/salesbot` | Kommo Salesbot/Chats API       |


All stores need the Instagram portion of section 1.7 when Instagram is enabled. Meta WhatsApp stores also configure the WhatsApp product. Kommo WhatsApp stores follow [docs/KOMMO_MIGRATION.md](../docs/KOMMO_MIGRATION.md) for WhatsApp after the core setup, while following [`INSTAGRAM DEPLOY.md`](INSTAGRAM%20DEPLOY.md) separately for Instagram.

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
WHATSAPP_BACKEND=meta
META_APP_SECRET=abc123...
WHATSAPP_ACCESS_TOKEN=EAAG...
WHATSAPP_PHONE_NUMBER_ID=123456789
WHATSAPP_VERIFY_TOKEN=my_secret_verify_2026
INSTAGRAM_ACCESS_TOKEN=          # Leave empty until App Review approval
INSTAGRAM_VERIFY_TOKEN=my_secret_verify_2026
OPENAI_API_KEY=sk-...
ANTHROPIC_API_KEY=sk-ant-...
DATABASE_URL=postgresql://postgres:password@localhost:5432/store_db
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

For Kommo WhatsApp local testing, replace the Meta WhatsApp variables with the following while retaining native Meta Instagram variables when Instagram is enabled:

```bash
WHATSAPP_BACKEND=kommo
KOMMO_SUBDOMAIN=your-account-subdomain
KOMMO_ACCESS_TOKEN=...
KOMMO_INTEGRATION_ID=...
KOMMO_INTEGRATION_SECRET=...
KOMMO_WHATSAPP_SALESBOT_ID=234567
KOMMO_SALESBOT_ID=                 # Legacy WhatsApp fallback
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
[INFO] Active LLM: openai/gpt-5.6-luna
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

> **Note:** With `DEBUG=true` and no `ADMIN_PASSWORD` set, admin routes are accessible without auth for local development. In production, startup validation requires a non-placeholder `ADMIN_PASSWORD` of at least 12 characters and at least one real LLM API key. Documented sample credentials are rejected. `WHATSAPP_BACKEND=meta` requires the full WhatsApp config (`META_APP_SECRET`, `WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_VERIFY_TOKEN`). `WHATSAPP_BACKEND=kommo` requires the Kommo private integration, WhatsApp Salesbot, webhook secret, and AI Mode field/enum variables. Telegram is optional, but if you enable it, provide `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ADMIN_CHAT_ID`, and `TELEGRAM_WEBHOOK_SECRET` together.

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

### 4.2 Kommo WhatsApp: General Webhook and Salesbot

Only do this when `WHATSAPP_BACKEND=kommo`.

1. Run `python scripts/migrate.py` and confirm `store/app/db.py` accepts the complete migration set through version 16.
2. Build and upload the private widget from `store/kommo-widget/` with `python3 build_widget.py --widget-code <kommo-widget-code>`.
3. Create one WhatsApp Salesbot with the matching widget block. Route `success` to a WhatsApp-restricted Message step using `{{json.message}}`, `media` to a silent end, and `fail` to a silent end or human fallback.
4. Register a Kommo general webhook at `https://abc123.ngrok-free.app/webhooks/kommo/events/<KOMMO_WEBHOOK_SECRET>`.
5. Subscribe to incoming message, outgoing message, lead edited, talk added, and talk edited events.
6. Confirm `GET /admin/settings/kommo/status` and `POST /admin/settings/kommo/test` work with admin auth.

Do not connect Instagram to Kommo. Instagram DMs and comments must continue through `/webhooks/instagram`, durable `meta_inbound_jobs`, and Meta Graph API delivery.

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
| `WHATSAPP_BACKEND`                                  | `meta` or `kommo`                                                     |
| `ADMIN_PASSWORD`                                    | Required when `DEBUG=false`                                           |
| `OPENAI_API_KEY` or `ANTHROPIC_API_KEY`             | At least one is required                                              |
| `DATABASE_URL`                                      | `${{StorePostgres.DATABASE_URL}}` Railway reference variable          |
| `GOOGLE_SHEETS_CREDENTIALS_B64`                     | Base64 service-account JSON                                           |
| `PRODUCT_SHEET_ID`                                  | Google Sheets catalog ID                                              |
| `STORE_NAME`, `OWNER_NAME`, `APP_BASE_URL`, `DEBUG` | Store metadata/runtime                                                |


Additional variables for native Meta Instagram when Instagram is enabled:


| Variable                    | Notes                                    |
| --------------------------- | ---------------------------------------- |
| `META_APP_SECRET`           | Webhook signature verification           |
| `INSTAGRAM_ACCESS_TOKEN`    | Native Instagram Graph API token         |
| `INSTAGRAM_VERIFY_TOKEN`    | Instagram webhook verification token     |
| `INSTAGRAM_ACCOUNT_ID`      | Managed Instagram Professional account   |
| `META_GRAPH_API_VERSION`    | Validated Graph API version (`v26.0`)      |

Additional variables for `WHATSAPP_BACKEND=meta`:

| Variable                     | Notes                            |
| ---------------------------- | -------------------------------- |
| `WHATSAPP_ACCESS_TOKEN`      | WhatsApp Cloud API token         |
| `WHATSAPP_PHONE_NUMBER_ID`   | WhatsApp sender phone number ID  |
| `WHATSAPP_VERIFY_TOKEN`      | WhatsApp webhook verify token    |


Additional variables for `WHATSAPP_BACKEND=kommo`:


| Variable                            | Notes                                      |
| ----------------------------------- | ------------------------------------------ |
| `KOMMO_SUBDOMAIN`                   | Account subdomain only, for example `acme` |
| `KOMMO_ACCESS_TOKEN`                | Long-lived private integration token       |
| `KOMMO_INTEGRATION_ID`              | Private integration ID/client UUID         |
| `KOMMO_INTEGRATION_SECRET`          | JWT validation secret                      |
| `KOMMO_WHATSAPP_SALESBOT_ID`        | Preferred WhatsApp Salesbot                |
| `KOMMO_SALESBOT_ID`                 | Legacy fallback for the WhatsApp Salesbot  |
| `KOMMO_WEBHOOK_SECRET`              | Random path secret for general webhook URL |
| `KOMMO_AI_MODE_FIELD_ID`            | Lead field ID for AI Mode                  |
| `KOMMO_AI_ACTIVE_ENUM_ID`           | Enum ID for AI Active                      |
| `KOMMO_AI_HUMAN_ENUM_ID`            | Enum ID for Human                          |
| `KOMMO_AI_PAUSED_ENUM_ID`           | Enum ID for Paused                         |
| `KOMMO_DEFAULT_RESPONSIBLE_USER_ID` | Optional assignment target on escalation   |
| `KOMMO_CHATS_MEDIA_ENABLED`         | Global WhatsApp Chats API media kill switch; default `false` |
| `KOMMO_CHATS_PRODUCT_IMAGES_ENABLED` | Independent product-image rollout flag     |
| `KOMMO_CHATS_CATALOG_PDF_ENABLED`   | Independent WhatsApp PDF rollout flag      |
| `KOMMO_CHATS_API_MONTHLY_LIMIT`     | Optional local monitoring value; not enforcement |
| `KOMMO_CHATS_PDF_ATTACHMENT_TYPE`   | Must be `file` before PDF delivery is enabled |

Meta calls `GET/POST /webhooks/instagram` independently of `WHATSAPP_BACKEND`; see [`INSTAGRAM DEPLOY.md`](INSTAGRAM%20DEPLOY.md).


Deploy this store as a **single instance / single worker**. The scheduler runs in-process, so multiple app instances would duplicate scheduled jobs and broadcast checks.

After deploy, get your URL (e.g., `https://vs-chatbot-production.up.railway.app`). Update `APP_BASE_URL` in Railway variables.

Before you go live, open the store dashboard login at:

```text
https://vs-chatbot-production.up.railway.app/admin/login
```

After logging in, go to **Configuración** and add the payment methods you want this store to offer. Each method needs a **Nombre** and **Información**. These are stored in the store DB, not in `.env`, and the bot will list only the configured method names during checkout. Also set `store_phone_number` to the public WhatsApp number with its full international country code, for example `+58 412 1234567`. The backend strips formatting but never guesses a country code; a missing, local-only, or invalid number disables the clickable Instagram handoff URL and uses a profile/store-contact fallback instead. Keep `ai_orchestration_mode` on `legacy` until you complete the rollout checklist in Part 9.

### 5.3 Switch Channel Webhooks to Railway

Always update the enabled native Instagram webhook to:

```text
https://vs-chatbot-production.up.railway.app/webhooks/instagram
```

For Meta WhatsApp, update the WhatsApp webhook URL:

```text
https://vs-chatbot-production.up.railway.app/webhooks/whatsapp
```

For Kommo WhatsApp, update:

- WhatsApp Salesbot widget URL: `https://vs-chatbot-production.up.railway.app/webhooks/kommo/salesbot`
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



### 6.2 Native Meta Instagram (After App Review)

This section applies whenever Instagram is enabled, regardless of the WhatsApp backend.

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

**Six tabs:**

- **Resumen:** Today's stats, per-channel breakdown, token usage by provider, AI on/off toggle
- **Clientes:** Sortable customer table, inline tag management (add/remove), inline state/channel editing, and resolve escalations individually or all at once. In Kommo mode, dashboard reactivation verifies the lead's `AI Mode=AI Active` before local history is cleared.
- **Pedidos:** Sortable order table with status badges
- **Broadcasts:** Sortable broadcast table, create/preview/send broadcasts, inspect `partial` sends, reset failed broadcasts
- **Instagram:** Map posts, Reels, carousels, and discovered Stories to one or more catalog products
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

When a customer sends a downloadable image, the system runs it through the LLM's vision capability, extracts payment details, then deterministically verifies open order, amount, method, recipient, and completed status before changing payment state. The LLM does not decide whether to mark a payment as paid. In Kommo mode this requires the inbound event to expose a direct HTTPS image URL accepted by the trusted-host downloader; see [Kommo payment-image limitations](../docs/KOMMO_MIGRATION.md#payment-image-limitations).

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



### 10.1 Infrastructure

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
[ ] WHATSAPP_BACKEND=meta -> /webhooks/whatsapp is registered
[ ] WHATSAPP_BACKEND=kommo -> Kommo webhooks are registered and /webhooks/whatsapp is absent
[ ] Instagram enabled -> /webhooks/instagram is registered regardless of WHATSAPP_BACKEND
[ ] GET /test/ui with DEBUG=false -> 404 (test endpoints disabled in production)
[ ] GET /test/ui with DEBUG=true from direct loopback and no forwarding headers -> test page loads
```



### 10.2 WhatsApp Conversations

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
[ ] Same request in opted-in Kommo WhatsApp with all PDF flags and type=file -> PDF sent through Chats API
[ ] Same request in Instagram DM -> WhatsApp handoff; no Instagram PDF attachment
[ ] Kommo WhatsApp voice note -> transcribed before the AI turn when OPENAI_API_KEY is configured
[ ] Native Meta Instagram voice note -> transcribed before the AI turn when OPENAI_API_KEY is configured
[ ] Repeat order -> AI offers saved address: "¿misma dirección de la última vez?"
[ ] Set product Stock=0 in Sheets, ask for it -> "Out of stock" + alternatives
[ ] Ask for a product or category not present in the Sheet -> Politely declines and suggests the closest available catalog alternatives
[ ] Payment flow -> Interactive buttons appear for payment method choice
[ ] Set ai_orchestration_mode=shadow -> Legacy response is served and route metadata is logged
[ ] Set ai_orchestration_mode=multi_agent -> Sales, checkout, and support routes respond correctly
[ ] Set ai_orchestration_mode=legacy -> Immediate rollback to legacy behavior
```



### 10.3 Instagram Conversations (After App Review)

Test native Instagram Messaging API behavior regardless of the WhatsApp backend. Instagram must never enter Kommo.

```text
[ ] Ice Breakers appear on first DM open
[ ] Tapping an Ice Breaker triggers appropriate response
[ ] Regular DM gets same quality as WhatsApp
[ ] Quick Replies appear for choices (IG equivalent of WA buttons)
[ ] Native Meta Story reply is received when the required field and permissions are active
[ ] Instagram DMs/comments create meta_inbound_jobs and no kommo_message_jobs
[ ] Long messages split correctly at sentence boundaries
```



### 10.4 Telegram Admin Bot

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



### 10.5 Broadcasts

```text
[ ] Create broadcast (API or dashboard) -> Appears as "draft"
[ ] /preview [tags] -> Shows customer count and estimated cost
[ ] Meta mode send broadcast -> Messages delivered, Telegram notification received
[ ] Meta mode scheduled broadcast -> Auto-sends at scheduled time
[ ] Kommo mode send broadcast -> Rejected with a clear Kommo-mode message
[ ] Kommo mode scheduled broadcast -> Marked failed instead of retrying forever
[ ] Reset stuck broadcast (dashboard "Resetear" button or POST /{id}/reset) -> Returns to "draft"
```



### 10.6 Kommo WhatsApp Mode

```text
[ ] python scripts/migrate.py completes and store/app/db.py accepts the full migration set through version 16
[ ] Widget ZIP uploaded to private Kommo integration
[ ] WhatsApp Salesbot contains its matching widget step and success/media/fail exits
[ ] General webhook points to /webhooks/kommo/events/<KOMMO_WEBHOOK_SECRET>
[ ] POST /admin/settings/kommo/test with auth -> read-only checks pass
[ ] WhatsApp message appears in Kommo inbox and creates a Kommo job
[ ] Customer receives AI response through Kommo Salesbot
[ ] Lead AI Mode=Human -> local customer becomes escalated and AI stops
[ ] Lead AI Mode=AI Active -> AI can answer the next inbound message
[ ] Store dashboard resolves a Kommo escalation -> Kommo AI Mode is confirmed active before local state/history changes
```



### 10.7 Analytics

```text
[ ] /conversion -> Funnel with rates (after at least one purchase flow)
[ ] /performance -> avg/p50/p95 per provider (after some messages)
[ ] /products -> Popularity ranking (after inventory checks)
[ ] ai_run_logs -> Contains route metadata without message text, addresses, payment credentials, or raw tool arguments
```



### 10.8 Resilience

```text
[ ] Break primary API key -> Bot falls back to secondary provider
[ ] Unshare Google Sheet -> Bot uses cached catalog
[ ] Send 5 rapid messages -> All get responses without errors
[ ] /health -> Scheduler shows "running"
```



### 10.9 First-Week Monitoring (Daily Checks)

```text
[ ] /stats -> Message volumes per channel
[ ] /usage -> API costs (~$0.15-0.30/day at normal volume)
[ ] /conversion -> Customers moving through funnel
[ ] /performance -> No response times above 5000ms
[ ] Check stored conversations for incorrect AI responses
[ ] Check orders table for stuck orders (pending >24h)
[ ] Verify Telegram escalation and order notifications arrive
[ ] /products -> Address frequently asked-for products you don't carry
```

---



## Part 11: Quick Reference



### All Endpoints

```text
Webhooks (no admin auth, verified by channel-specific secret/signature):
  Instagram always when enabled:
    GET/POST  /webhooks/instagram
  Meta WhatsApp mode:
    GET/POST  /webhooks/whatsapp
  Kommo WhatsApp mode:
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
  GET  /admin/settings/shipping-policy
  PUT  /admin/settings/shipping-policy
  PUT  /admin/settings/batch
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

Instagram content mappings (require admin auth):
  GET  /admin/instagram-content/products
  GET  /admin/instagram-content
  POST /admin/instagram-content
  PUT  /admin/instagram-content/{id}
  DELETE /admin/instagram-content/{id}

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
  GET  /admin/broadcasts/{id}/deliveries
  POST /admin/broadcasts/{id}/deliveries/{delivery_id}/retry

Analytics (require admin auth):
  GET  /admin/analytics/conversion
  GET  /admin/analytics/response-times
  GET  /admin/analytics/popular-products
  GET  /admin/analytics/daily
  POST /admin/analytics/build-daily

Testing (DEBUG=true, direct loopback only, forwarding headers rejected):
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
  Service account email needs **Editor** access to the sheet because checkout and the inventory ledger write to it.
- **Telegram notifications not arriving**  
  Must have sent bot `/start` first. Check `TELEGRAM_ADMIN_CHAT_ID` is your ID, not the bot's.
- **Telegram bot not responding**  
  Re-run: `POST /admin/settings/telegram/setup-webhook`
- **Instagram messages not arriving**  
  App must be Live mode. IG account must be Professional. Page subscription must be active.
- **Kommo Salesbot callbacks return 401**
  Verify `KOMMO_INTEGRATION_SECRET`, `KOMMO_INTEGRATION_ID`, `KOMMO_SUBDOMAIN`, and the widget request JWT. Confirm the Salesbot widget URL points to `/webhooks/kommo/salesbot`.
- **Kommo jobs stuck in `waiting_for_salesbot`**
  The backend marks stale waits as failed after about 3 minutes so new inbound messages can retry. If this repeats, verify the WhatsApp widget block, HTTPS callback, and `KOMMO_WHATSAPP_SALESBOT_ID` (with `KOMMO_SALESBOT_ID` only as a legacy WhatsApp fallback). Instagram does not use Kommo jobs.
- **Kommo image payment screenshots are ignored**
  Direct media downloads are intentionally limited to trusted Meta/Instagram/Kommo hosts over HTTPS, with redirects disabled and a 5 MB size limit. Some Kommo media payloads may need manual production validation.
- **Kommo WhatsApp catalog requests do not send PDFs**
  Confirm `KOMMO_CHATS_MEDIA_ENABLED=true`, `KOMMO_CHATS_CATALOG_PDF_ENABLED=true`, and `KOMMO_CHATS_PDF_ATTACHMENT_TYPE=file`, plus a valid WhatsApp `talk_id`. Instagram never receives a PDF and instead gets the WhatsApp handoff.
- **WhatsApp "not registered"**  
  Number must be registered with Cloud API, not regular WhatsApp.
- **Broadcasts send 0 messages**  
  Tags don't match any customers. Use `/preview` first. Verify template name matches Meta Business Manager exactly.
- **Broadcasts fail immediately in Kommo mode**
  This is expected. This backend does not send direct WhatsApp Cloud API broadcasts when `WHATSAPP_BACKEND=kommo`; use Kommo broadcasts or approved Kommo WhatsApp template flows.
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
