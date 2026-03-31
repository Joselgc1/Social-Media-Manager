# Multi-Store Platform: Deployment Guide

How to deploy and manage multiple stores from a single Master Control Plane. Each store runs the existing VS Chatbot app independently (its own Railway deployment, database, API keys, and channels). The Master Control Plane sits on top, giving you a centralized dashboard to monitor, configure, and manage all stores.

This guide has 7 parts. Part 1 sets up the master service infrastructure. Part 2 covers local testing. Part 3 deploys the master service. Part 4 shows how to add your first store. Part 5 covers adding subsequent stores and Railway credential deployment. Part 6 is the testing checklist. Part 7 is a quick reference.

**Prerequisites:** You should already be familiar with deploying a single store. See the main `DEPLOYMENT.md` in the project root for the per-store deployment process (database, API keys, webhooks, etc.).

---

## Part 1: Master Control Plane infrastructure

The master service needs its own database, encryption key, and authentication secret. It does NOT share a database with any store.

### 1.1 Master database (Supabase)

Go to supabase.com and create a **new project** (separate from any store project). Name it something like "master-control-plane." Pick the same region as your store deployments.

Once created, go to Settings > Database and copy the connection string. Use the **Session Pooler** URL (port 6543) to avoid network issues:

```
postgresql://postgres.xxxx:[YOUR-PASSWORD]@aws-0-us-east-1.pooler.supabase.com:6543/postgres
```

This goes in the master `.env` as `DATABASE_URL`.

Now run the master schema migration. Go to the SQL Editor in Supabase and paste the entire contents of `master/migrations/001_master_schema.sql`. Click "Run." This creates three tables: `stores`, `store_credentials`, and `master_audit_log`.

### 1.2 Generate the encryption key

The encryption key protects all store credentials (API keys, database URLs) at rest using Fernet symmetric encryption. Generate one:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Copy the output. This goes in `.env` as `ENCRYPTION_KEY`.

**Keep this key safe.** If you lose it, all stored credentials become unrecoverable and you'll need to re-enter them. Back it up somewhere secure (password manager, not git).

### 1.3 Generate the master secret key

This is the Bearer token that protects the master dashboard. Only you should know it.

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Copy the output. This goes in `.env` as `MASTER_SECRET_KEY`.

### 1.4 Configure master environment

```bash
cd master
cp .env.example .env
```

Fill in the values:

```bash
DATABASE_URL=postgresql://postgres.xxxx:password@aws-0-us-east-1.pooler.supabase.com:6543/postgres
MASTER_SECRET_KEY=your-generated-secret-key
ENCRYPTION_KEY=your-generated-fernet-key
RAILWAY_API_TOKEN=             # Leave empty for now (Phase 2)
APP_BASE_URL=http://localhost:9000
HEALTH_CHECK_INTERVAL_SECONDS=300
```

### 1.5 Test locally

```bash
cd master
python -m venv .venv
source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt

uvicorn app.main:app --reload --port 9000
```

You should see:

```
[INFO] Starting Master Control Plane...
[INFO] Master database connected.
[INFO] Health check loop started.
[INFO] Master Control Plane is ready.
```

Open in browser:

```
http://localhost:9000/dashboard?token=YOUR_MASTER_SECRET_KEY
```

On first visit, the token is validated, an HTTP-only session cookie is set, and you're redirected to the clean URL (`/dashboard` — no token in the URL bar, browser history, or server logs). Subsequent visits use the cookie automatically.

You should see the master dashboard with an empty "All Stores" grid and a button to add your first store.

---

## Part 2: Local testing

The master service includes a test UI and test endpoints so you can verify everything works before deploying to production — just like the store app's `/test/ui`.

### 2.1 Test UI

Open in your browser:

```
http://localhost:9000/test/ui
```

The test UI provides:

- **Quick Checks**: One-click verification of database, encryption, health, and Railway connectivity
- **Test Data**: Seed 3 sample stores with fake credentials, or reset everything
- **Dashboard Access**: Enter your `MASTER_SECRET_KEY` to generate a clickable dashboard link
- **API Tester**: Send requests to any master API endpoint and see the response

### 2.2 Seed test stores

Click "Seed Test Stores" in the test UI, or via curl:

```bash
curl -X POST http://localhost:9000/test/seed
```

This creates 3 sample stores ("Eva - Tienda de Carlos", "Bella - Tienda de Maria", "Chic - Tienda de Ana") with fake credentials. The stores use the master's own DB URL as a placeholder — stats won't work since the master DB doesn't have store tables, but all CRUD operations, credential encryption, and audit logging work.

### 2.3 Test the dashboard

Open the dashboard with your token:

```
http://localhost:9000/dashboard?token=YOUR_MASTER_SECRET_KEY
```

Verify:

- Store cards appear with name, owner, and status
- Click a store card to see the detail view
- Add/edit/delete credentials (they're encrypted in the DB)
- Check the Audit Log tab for all actions
- Dark mode toggle works

### 2.4 Test endpoints reference

> **Note:** Test endpoints (`/test/`) are only available when `APP_BASE_URL` contains `localhost` or `127.0.0.1`. They return 404 in production.

```bash
# Quick checks (no auth required — localhost only)
curl http://localhost:9000/health
curl http://localhost:9000/test/db-check
curl http://localhost:9000/test/crypto?value=my-secret

# Seed and reset (no auth required — localhost only)
curl -X POST http://localhost:9000/test/seed
curl -X DELETE http://localhost:9000/test/reset

# Railway check (requires auth — localhost only)
curl -H "Authorization: Bearer YOUR_TOKEN" http://localhost:9000/test/railway-check

# Store API (requires auth)
curl -H "Authorization: Bearer YOUR_TOKEN" http://localhost:9000/api/stores/
curl -H "Authorization: Bearer YOUR_TOKEN" http://localhost:9000/api/stores/audit/log
```

### 2.5 Running both services locally

To test the master alongside a store, run them on different ports:

```bash
# Terminal 1: Store app on port 8000
cd /path/to/Social-Media-Manager
source .venv/bin/activate
uvicorn app.main:app --reload --port 8000

# Terminal 2: Master on port 9000
cd /path/to/Social-Media-Manager/master
source .venv/bin/activate
uvicorn app.main:app --reload --port 9000
```

Register the local store in the master dashboard:

- **App URL**: `http://localhost:8000`
- **Database URL**: your store's `DATABASE_URL` from `.env`

The master's health checker will ping `http://localhost:8000/health` and the stats view will query the store's database directly. This lets you test the full flow end-to-end before deploying.

---

## Part 3: Deploy the master service (Railway)

### 3.1 Push to GitHub

The master service lives inside the same repo under `master/`. Railway can deploy a subdirectory.

If you haven't already pushed the repo:

```bash
cd /path/to/Social-Media-Manager
git add master/
git commit -m "Add master control plane for multi-store management"
git push origin main
```

### 3.2 Create a Railway project for the master

Go to railway.app. Create a **new project** (separate from any store project).

Click "Deploy from GitHub Repo" and select your repo. Under **Settings > General**, set the **Root Directory** to `master/`.

Add environment variables in the Variables tab:


| Variable                        | Value                                              |
| ------------------------------- | -------------------------------------------------- |
| `DATABASE_URL`                  | Your master Supabase connection string (port 6543) |
| `MASTER_SECRET_KEY`             | Your generated secret key                          |
| `ENCRYPTION_KEY`                | Your generated Fernet key                          |
| `RAILWAY_API_TOKEN`             | Your Railway API token (see Part 5.6)              |
| `APP_BASE_URL`                  | Will be set after first deploy (Railway URL)       |
| `HEALTH_CHECK_INTERVAL_SECONDS` | `300`                                              |


Deploy. Once live, get the Railway URL (e.g., `https://master-control-plane-production.up.railway.app`).

Update `APP_BASE_URL` in Railway variables to this URL.

### 3.3 Verify deployment

```bash
# Health check
curl https://your-master-url.railway.app/health

# Should return:
# {"status": "healthy", "service": "master-control-plane", "total_stores": 0, "active_stores": 0}
```

Open the dashboard:

```
https://your-master-url.railway.app/dashboard?token=YOUR_MASTER_SECRET_KEY
```

On first visit, the token is validated and a session cookie is set. You're redirected to the clean URL (`/dashboard`). Bookmark this clean URL — the cookie handles authentication on subsequent visits (24-hour expiry).

This is your central management interface.

---

## Part 4: Add your first store (existing deployment)

If you already have a store running (your friend's current deployment), register it in the master dashboard.

### 4.1 Gather store information

You need:

- **Store name**: e.g., "Eva - Tienda de Carlos"
- **Owner name**: e.g., "Carlos"
- **Owner contact**: Phone number or email
- **App URL**: The Railway URL of the store (e.g., `https://vs-chatbot-production.up.railway.app`)
- **Database URL**: The store's Supabase connection string (from the store's `.env`)

### 4.2 Register via the dashboard

1. Open the master dashboard
2. Click **"+ Add Store"**
3. Fill in the form:
  - Store Name: `Eva - Tienda de Carlos`
  - Owner Name: `Carlos`
  - Owner Contact: `+58 412 123 4567`
  - App URL: `https://vs-chatbot-production.up.railway.app`
  - Database URL: `postgresql://postgres:...@db.xyz.supabase.co:6543/postgres`
4. Click **"Add Store"**

The store card should appear on the overview with live stats (today's chats, orders, customers, AI status).

### 4.3 Store credentials (optional but recommended)

Click on the store card to open the detail view. Under **Credentials**, add the store's environment variables for safekeeping. This is useful for when you need to update an API key or redeploy.

Click **"+ Add Credential"** and add each one:


| Key                        | Value                |
| -------------------------- | -------------------- |
| `OPENAI_API_KEY`           | sk-...               |
| `ANTHROPIC_API_KEY`        | sk-ant-...           |
| `WHATSAPP_ACCESS_TOKEN`    | EAAG...              |
| `WHATSAPP_PHONE_NUMBER_ID` | 123...               |
| `TELEGRAM_BOT_TOKEN`       | 123:ABC...           |
| ...                        | (all other env vars) |


All values are encrypted with Fernet before storage. The dashboard only shows masked values (e.g., `sk-p**`**).

### 4.4 Add admin password to the store (required)

`ADMIN_PASSWORD` is **required** in production. Without it, all admin routes return 403. Add it to the store's Railway deployment:

```
ADMIN_PASSWORD=some-strong-password-for-carlos
```

After redeploy, the store dashboard and all admin API endpoints are protected. The dashboard at `/admin/dashboard?password=...` validates the password, sets an HTTP-only session cookie, and redirects to the clean URL. API calls require `Authorization: Bearer <password>`.

Give this password to the store owner so they can access their own dashboard.

---

## Part 5: Add a new store (from scratch)

When Carlos's mom or sister wants their own store, follow these steps.

### 5.1 Set up external services for the new store

Follow **Parts 1.1 through 1.6** of the main `DEPLOYMENT.md` (in the project root), but for the new store's accounts:

1. **New Supabase project** (e.g., "store-maria"). Run all 4 migrations (`001` through `004`).
2. **New Google Sheets** catalog with their products. Share with the same service account, or create a new one.
3. **New Telegram bot** via @BotFather for their admin notifications.
4. **Same Meta Developer App** (or a new one): Add their WhatsApp phone number and generate an access token.
5. **Same or new LLM API keys** (they can share API keys or have their own).

### 5.2 Deploy a new Railway service

Option A: **Same repo, new Railway service.** In Railway, add a new service to the store's Railway project, deploy from the same GitHub repo, but with different environment variables.

Option B: **Fork the repo.** Create a separate GitHub repo for the new store and deploy from there.

Either way, set the root directory to `/` (the store app, not master), and add all environment variables for the new store. Key variables to customize:

```bash
DATABASE_URL=postgresql://...          # NEW Supabase project
WHATSAPP_ACCESS_TOKEN=...              # NEW phone number token
WHATSAPP_PHONE_NUMBER_ID=...           # NEW phone number ID
TELEGRAM_BOT_TOKEN=...                 # NEW Telegram bot
TELEGRAM_ADMIN_CHAT_ID=...             # Store owner's Telegram ID
GOOGLE_SHEETS_CREDENTIALS_B64=...      # Same or new service account
PRODUCT_SHEET_ID=...                   # NEW Google Sheet
STORE_NAME=Tienda de Maria             # Customized per store
OWNER_NAME=Maria
ADMIN_PASSWORD=marias-secret-password  # Protects the store dashboard
APP_BASE_URL=https://store-maria.railway.app
```

Optionally, set `SYSTEM_PROMPT_OVERRIDE` to customize the AI persona for this store. If not set, it uses the default `prompts/system_prompt.md` file.

### 5.3 Connect webhooks for the new store

Follow **Parts 4 and 6** of the main `DEPLOYMENT.md`, using the new store's Railway URL:

1. **WhatsApp webhook**: `https://store-maria.railway.app/webhooks/whatsapp`
2. **Telegram webhook**: `POST https://store-maria.railway.app/admin/settings/telegram/setup-webhook`
3. **Instagram webhook** (after App Review): `https://store-maria.railway.app/webhooks/instagram`

### 5.4 Register in the master dashboard

Go to the master dashboard and click **"+ Add Store"**. Fill in the new store's details (name, owner, app URL, database URL). Add its credentials for safekeeping.

### 5.5 Customize the AI persona (optional)

If the new store sells different products or has a different brand voice, you can override the system prompt without modifying code.

Set the `SYSTEM_PROMPT_OVERRIDE` environment variable in Railway to the full text of the custom system prompt. The prompt should use the same `{store_name}`, `{product_catalog}`, `{zelle_details}`, `{binance_details}`, `{zinli_details}`, `{bolivares_details}` placeholders as the original `prompts/system_prompt.md`.

If you don't set this variable, the store uses the default prompt template from the file.

### 5.6 Enable centralized LLM control (recommended)

To take full control of which AI provider and model each store uses:

1. In the master dashboard, go to the store's credentials and add `LLM_MANAGED_EXTERNALLY` with value `true`
2. Deploy the credential to Railway (see 5.7 below)
3. Once deployed, the store's dashboard will hide all LLM provider/model controls, and the Telegram bot `/provider` and `/abmode` commands will be disabled

Now you can manage the store's AI settings from the master dashboard:

- Open a store's detail view → **AI Provider Configuration** panel
- Set the provider (OpenAI/Anthropic), model, temperature, max tokens, fallback, and A/B testing
- Click "Save AI Settings" — changes take effect within 60 seconds (the store's settings cache interval)

The **LLM Usage (Today)** panel shows the store's API call count and estimated cost. The overview tab shows **Platform LLM Costs** aggregated across all stores.

### 5.7 Deploy credentials from the master dashboard (Railway integration)

Instead of manually editing environment variables in each store's Railway dashboard, you can manage all credentials centrally in the master dashboard and push them to Railway with one click.

**Set up the Railway API token:**

1. Go to railway.app > Account Settings > Tokens
2. Click "Create Token," name it "master-control-plane"
3. Copy the token and add it to the master `.env` (or Railway variables) as `RAILWAY_API_TOKEN`

**Link a store to Railway:**

When adding or editing a store in the master dashboard, fill in:

- **Railway Service ID**: Found in Railway dashboard > your store service > Settings > Service ID
- **Railway Project ID**: Found in Railway dashboard > your project > Settings > Project ID

**Push credentials:**

1. Open the store detail in the master dashboard
2. Add all environment variables under **Credentials** (they're encrypted at rest)
3. Click the **"Deploy Changes"** button
4. The master decrypts all credentials, pushes them to Railway, and triggers a redeploy
5. The Railway Deployment section shows the current deploy status

The "Deploy Changes" button only appears when a store has both credentials and a Railway Service ID configured.

**What happens during deploy:**

```
Master dashboard "Deploy Changes" clicked
  -> Decrypts all store credentials from master DB
  -> Calls Railway API: upsert environment variables on the service
  -> Calls Railway API: trigger service redeploy
  -> Audit log records the deploy with credential count and deployment ID
  -> Railway section updates with latest deployment status
```

**Via API (for automation):**

```bash
# Push credentials and redeploy
curl -X POST "https://your-master-url/api/stores/STORE_ID/deploy" \
  -H "Authorization: Bearer YOUR_MASTER_SECRET"

# Check Railway status
curl "https://your-master-url/api/stores/STORE_ID/railway/status" \
  -H "Authorization: Bearer YOUR_MASTER_SECRET"
```

---

## Part 6: Testing checklist

### 6.1 Master Control Plane

```
[ ] GET /health -> status=healthy, total_stores and active_stores correct
[ ] Dashboard loads with ?token= (no token -> 401)
[ ] Store cards show live stats (chats, orders, customers, AI status)
[ ] Status dots update: green (active), red (error), yellow (paused)
[ ] "Add Store" creates a store, card appears on overview
[ ] Click store card -> detail view with stats + credentials + info
[ ] Add credential -> appears masked, stored encrypted in DB
[ ] Edit credential -> value updates, audit log entry created
[ ] Delete credential -> removed, audit log entry created
[ ] Edit store metadata (name, owner, status) -> updates correctly
[ ] Delete store -> removed with all credentials (after confirmation)
[ ] Audit Log tab -> shows all actions with timestamps and store names
[ ] Dark mode toggle works and persists on refresh
[ ] Store's "Open Store Dashboard" link works (opens in new tab)
```

### 6.2 Store-level additions

```
[ ] Store with ADMIN_PASSWORD set -> /admin/dashboard without ?password= returns 401
[ ] Store with ADMIN_PASSWORD set -> /admin/dashboard?password=correct sets cookie, redirects to clean URL
[ ] Store dashboard -> subsequent visits work via cookie (no password in URL)
[ ] Store with ADMIN_PASSWORD -> GET /admin/settings/ without auth returns 401
[ ] Store with ADMIN_PASSWORD -> GET /admin/settings/ with Bearer header works
[ ] Store without ADMIN_PASSWORD + DEBUG=false -> /admin/* routes return 403
[ ] Store without ADMIN_PASSWORD + DEBUG=true -> /admin/* routes work (dev mode)
[ ] Store with DEBUG=false -> /test/* routes return 404
[ ] Store with SYSTEM_PROMPT_OVERRIDE -> AI uses custom prompt
[ ] Store without SYSTEM_PROMPT_OVERRIDE -> AI uses default file template
[ ] Store with LLM_MANAGED_EXTERNALLY=true -> Configuracion tab hides LLM controls
[ ] Store with LLM_MANAGED_EXTERNALLY=true -> PUT /admin/settings/llm_provider returns 403
[ ] Store with LLM_MANAGED_EXTERNALLY=true -> Telegram /provider returns managed message
[ ] Store without LLM_MANAGED_EXTERNALLY -> LLM controls work as normal
```

### 6.2b LLM Management from Master

```
[ ] Master store detail shows "AI Provider Configuration" panel
[ ] Changing provider updates model dropdown to matching models
[ ] "Save AI Settings" writes to store's DB (verify via store dashboard /admin/settings/)
[ ] "LLM Usage (Today)" panel shows call counts and costs
[ ] Overview tab shows "Platform LLM Costs" table with per-store costs and total
[ ] Costs aggregate endpoint returns correct totals across all stores
```

### 6.3 Multi-store isolation

```
[ ] Send message to Store A's WhatsApp -> only Store A processes it
[ ] Send message to Store B's WhatsApp -> only Store B processes it
[ ] Each store shows only its own customers, orders, and conversations
[ ] Crashing one store does not affect the other stores
[ ] Master health check detects a downed store and marks it as "error"
```

### 6.4 Railway Deployment

```
[ ] Railway status shows "linked" for a store with service + project IDs
[ ] Railway status shows "not_configured" when RAILWAY_API_TOKEN is empty
[ ] Railway status shows "not_linked" for a store without service ID
[ ] Deploy Changes pushes credentials and triggers redeploy
[ ] Audit log records deploy with credential count
[ ] Deploy fails gracefully when Railway API token is invalid (502 error)
[ ] Deploy fails gracefully when store has no credentials (400 error)
```

### 6.5 Local Testing

```
[ ] GET /test/ui (localhost) -> test page loads
[ ] GET /test/ui (production URL) -> 404 (test endpoints disabled)
[ ] GET /test/db-check -> tables exist with correct counts
[ ] GET /test/crypto?value=hello -> round_trip_ok: true
[ ] POST /test/seed -> 3 sample stores created with credentials
[ ] DELETE /test/reset -> all data cleared
[ ] GET /test/railway-check -> shows connected or not_configured
[ ] API tester in test UI sends requests and shows responses
[ ] Seed stores appear in dashboard after seeding
```

### 6.6 Security

```
[ ] Master dashboard without token or cookie -> 401
[ ] Master dashboard with ?token= -> sets cookie, redirects to clean URL
[ ] Master dashboard with valid cookie -> loads (no token in URL)
[ ] Master API without Bearer header -> 401
[ ] Credentials in master DB are encrypted (check directly in Supabase)
[ ] Store dashboards with ADMIN_PASSWORD are protected (cookie-based after first login)
[ ] Store admin API endpoints require Bearer header or session cookie
[ ] Credential values are never returned in plaintext via API (only masked)
[ ] Error responses do not leak internal details (DB URLs, stack traces) in production
[ ] Test endpoints return 404 in production (both store and master)
[ ] Rate limiting active (60/min store, 30/min master) — returns 429 on excess
[ ] Security headers present: X-Content-Type-Options, X-Frame-Options, HSTS (production)
```

---

## Part 7: Quick reference

### Master Control Plane endpoints

```
Health:
  GET  /               -> Basic status
  GET  /health          -> Detailed status with store counts

Dashboard:
  GET  /dashboard?token=SECRET  -> Master dashboard UI

Store CRUD (all require Bearer token):
  GET    /api/stores/                          -> List all stores
  GET    /api/stores/{id}                      -> Store detail
  POST   /api/stores/                          -> Create store
  PUT    /api/stores/{id}                      -> Update store metadata
  DELETE /api/stores/{id}                      -> Delete store + credentials

Store Credentials:
  GET    /api/stores/{id}/credentials          -> List credentials (masked)
  POST   /api/stores/{id}/credentials          -> Set/update a credential
  DELETE /api/stores/{id}/credentials/{key}    -> Delete a credential

Store Stats:
  GET    /api/stores/{id}/stats                -> Live stats from store's DB

LLM Control:
  GET    /api/stores/{id}/llm-settings         -> Read LLM settings from store's DB
  PUT    /api/stores/{id}/llm-settings         -> Write LLM settings to store's DB
  GET    /api/stores/{id}/llm-usage?days=N     -> Token usage + costs (default: today)
  GET    /api/stores/llm-costs/aggregate?days=N -> Platform-wide costs (default: today)

Railway Deployment:
  GET    /api/stores/{id}/railway/status       -> Railway service + deploy status
  POST   /api/stores/{id}/deploy               -> Push credentials + redeploy

Audit Log:
  GET    /api/stores/audit/log?limit=50        -> Recent audit entries

Testing (localhost only — returns 404 in production):
  GET    /test/ui                              -> Browser-based test UI
  GET    /test/db-check                        -> Database connectivity check
  GET    /test/crypto?value=hello              -> Encryption round-trip test
  POST   /test/seed                            -> Create sample stores + credentials
  DELETE /test/reset                           -> Delete all test data
  GET    /test/railway-check                   -> Railway API connectivity (needs auth)
```

### Architecture overview

```
Master Control Plane (1 deployment)
  ├── Master Supabase DB (stores registry, encrypted credentials, audit log)
  ├── Dashboard: monitor all stores, manage AI settings, view costs, deploy changes
  ├── LLM control: set provider/model per store, track platform-wide costs
  └── Health checker: pings each store every 5 minutes

Store A (1 deployment)                   Store B (1 deployment)
  ├── Own Supabase DB                      ├── Own Supabase DB
  ├── Own WhatsApp number                  ├── Own WhatsApp number
  ├── Own Instagram account                ├── Own Instagram account
  ├── Own Telegram bot                     ├── Own Telegram bot
  ├── Own Google Sheet catalog             ├── Own Google Sheet catalog
  ├── Own LLM API keys                     ├── Own LLM API keys
  └── Own admin dashboard                  └── Own admin dashboard
      (protected by ADMIN_PASSWORD)            (protected by ADMIN_PASSWORD)
```

### Cost estimate per store


| Service                        | Monthly cost |
| ------------------------------ | ------------ |
| Railway deployment             | $5-10        |
| Supabase (free tier)           | $0           |
| LLM APIs (6-60 msgs/day)       | $5-10        |
| **Total per store**            | **$10-20**   |
| Master Control Plane (Railway) | $5           |
| Master Supabase (free tier)    | $0           |


---

## Common issues and fixes

**"401 Invalid or missing authentication token"** on master dashboard - On first visit, use `?token=YOUR_KEY` — this sets a session cookie and redirects. If the cookie has expired (24 hours), revisit with `?token=`. Check that the token matches `MASTER_SECRET_KEY` exactly.

**Store card shows "Stats unavailable"** - The master can't connect to the store's database. Verify the DB URL is correct. Use the Session Pooler URL (port 6543). Check that the store's Supabase project allows connections from the master's IP/network.

**"MaxClientsInSessionMode" / "max clients reached" when loading the dashboard** - The dashboard requests stats for every store at once, and each request opens a short-lived connection to that store's database. Supabase's **Session** pooler only allows a small number of concurrent clients per pool. If the store app is also running (it holds its own pool slots), parallel stats calls can exceed the limit. The master caps concurrent stats queries and uses a single connection per request; if you still hit the limit, set `STORE_STATS_MAX_CONCURRENT=1` or `2` in the master's `.env`, or register fewer simultaneous stores during local testing.

**Store status shows red (error)** - The store's `/health` endpoint is unreachable. Check that the store's Railway deployment is running. Verify the `app_url` is correct in the master dashboard.

**"Cannot decrypt store database URL"** - The `ENCRYPTION_KEY` in the master `.env` has changed since the store was registered. If you rotated the key, you need to re-register all stores with the new key.

**Store dashboard returns 401** - `ADMIN_PASSWORD` is set but you don't have a valid session cookie. Visit `/admin/dashboard?password=...` to set one. The cookie lasts 24 hours. For API calls, use `Authorization: Bearer YOUR_PASSWORD`.

**Store admin API returns 403** - `ADMIN_PASSWORD` is not set and `DEBUG=false`. Set `ADMIN_PASSWORD` in the store's Railway variables and redeploy.

**Test endpoints return 404** - This is expected in production. Test endpoints are only available when `DEBUG=true` (store app) or when `APP_BASE_URL` contains `localhost` (master).

**AI responds with wrong persona** - Check if `SYSTEM_PROMPT_OVERRIDE` is set for that store. If it is, verify the content is correct and uses the right placeholders.

**Health checks not updating** - The background task runs every `HEALTH_CHECK_INTERVAL_SECONDS` (default 300 = 5 minutes). Wait for the next cycle or restart the master service. Stores with status "paused" are skipped.

**"Deploy Changes" button not visible** - The button only appears when a store has both credentials and a Railway Service ID configured. Add the service ID via the Edit button on the store detail page.

**"Railway API error" on deploy** - Check that `RAILWAY_API_TOKEN` is valid and has access to the store's Railway project. Regenerate the token at railway.app > Account Settings > Tokens if needed.

**Deploy succeeds but store doesn't restart** - Railway redeploys are asynchronous. Check the Railway Deployment section in the store detail for the latest deployment status. If it shows "FAILED", check Railway logs for build errors.

**Test seed stores show "Stats unavailable"** - This is expected. Seeded stores use the master DB URL as a placeholder, and the master DB doesn't have store tables (customers, conversations, etc.). CRUD, credentials, and audit logging still work correctly for testing.