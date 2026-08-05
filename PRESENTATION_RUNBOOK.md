# Presentation Runbook

This file is a detailed, manual test and demo guide for tomorrow's presentation.

It is written for the safest demo setup:

1. Run `store/` locally on `http://localhost:8000`
2. Run `master/` locally on `http://localhost:9000`
3. Use the built-in test endpoints instead of relying on live Meta webhooks
4. Keep the demo focused on the core value:
   - the AI can answer customers
   - the store dashboard works
   - master can manage the store
   - settings stay in sync between both dashboards

If you follow this runbook in order, you should be able to test the important flows with minimal surprises.

---

## 1. What You Should Demo Tomorrow

The most convincing demo is this:

1. Show the store login and dashboard
2. Show the AI replying to a fake customer from the test chat UI
3. Show that customer and order data appear in the store dashboard
4. Show store-only payment methods and the daily exchange rate being changed
5. Show the same store from `master/`
6. Change a shared setting in one dashboard and show it reflected in the other
7. Optionally show AI pause/resume and catalog PDF generation

Do not try to demo every advanced integration unless you have already tested it tonight.

For tomorrow, the best demo is a clean, stable one.

---

## 2. Demo Strategy

Use this rule:

- Use `store/test/ui` for the customer conversation demo
- Use the real store dashboard for operations
- Use the real master dashboard for cross-store management
- Only use WhatsApp, Instagram, Telegram, or Railway live features if you already know they work in your environment
- Keep `CHANNEL_BACKEND=meta` for the local test UI demo unless you are explicitly testing a real Kommo account

Important:

- `store/test/*` endpoints require `DEBUG=true`, a direct loopback request, and no forwarding headers
- `master/test/*` endpoints require `ENABLE_TEST_ENDPOINTS=true` and a loopback `APP_BASE_URL`
- For the local demo, keep `store` in `DEBUG=true`
- Still set `ADMIN_PASSWORD` and `MASTER_SECRET_KEY` so you can test the login flows

---

## 3. Before You Start

Prepare these things before doing any manual test:

### 3.1. Store `.env`

Open `store/.env` and verify at least these values:

```env
DATABASE_URL=...
GOOGLE_SHEETS_CREDENTIALS_B64=...
PRODUCT_SHEET_ID=...
OPENAI_API_KEY=...

STORE_NAME=Demo Store
OWNER_NAME=Your Name
APP_BASE_URL=http://localhost:8000
DEBUG=true

ADMIN_PASSWORD=demo-store-password
CHANNEL_BACKEND=meta
```

Notes:

- `DEBUG` must be `true` or `false`. Do not use values like `release`.
- For the local demo, `DEBUG=true` is recommended.
- At least one LLM key is required. `OPENAI_API_KEY` alone is enough.
- Google Sheets credentials and sheet ID must be valid if you want the catalog to load correctly.
- Meta, Instagram, and Telegram values can stay empty for the local demo.
- Kommo values can stay empty for the local test UI demo. If you set `CHANNEL_BACKEND=kommo`, you need real Kommo credentials, the current Store migrations from `python store/scripts/migrate.py`, channel-specific Salesbots, the widget, and public HTTPS webhooks.

Optional store env vars you may add manually if needed:

```env
SYSTEM_PROMPT_OVERRIDE=
LLM_MANAGED_EXTERNALLY=false
```

### 3.2. Master `.env`

Open `master/.env` and verify at least these values:

```env
DATABASE_URL=...
MASTER_SECRET_KEY=replace-with-a-generated-32-character-or-longer-secret
ENCRYPTION_KEY=your-generated-fernet-key
APP_BASE_URL=http://localhost:9000
ENABLE_TEST_ENDPOINTS=true
STORE_STATS_MAX_CONCURRENT=1
```

Notes:

- `MASTER_SECRET_KEY` is required for login and must be at least 32 characters; generate it with `python -c "import secrets; print(secrets.token_urlsafe(32))"`
- `ENCRYPTION_KEY` is required because master stores credentials encrypted
- `RAILWAY_API_TOKEN` can stay empty unless you want to demo Railway integration

### 3.3. Boolean Sanity Check

Before the demo, scan both `.env` files and make sure booleans are real booleans:

- `true`
- `false`

Do not use:

- `release`
- `yes`
- `on`
- `1`

unless you are absolutely sure Pydantic will parse them the way you expect.

---

## 4. Database Preparation

### 4.1. Store database

For a presentation database, run `cd store && python scripts/migrate.py`. For a pre-consolidation recovery case, back up first and apply `store/migrations/002_consolidated_upgrade.sql` as directed by the schema-version error. Confirm `schema_migrations` is supported by `store/app/db.py`.

After that, verify the `settings` table contains at least:

- `llm_provider`
- `llm_model`
- `llm_temperature`
- `llm_max_tokens`
- `fallback_provider`
- `fallback_model`
- `auto_fallback`
- `max_conversation_history`
- `ai_enabled`
- `catalog_pdf_interval_hours`
- `kommo_emoji_mode_whatsapp`
- `kommo_emoji_mode_instagram`
- `payment_methods`
- `exchange_rate_reference`
- `manual_exchange_rate`

### 4.2. Master database

Make sure the master schema is already installed and the `stores`, `store_credentials`, and `master_audit_log` tables exist.

If your master DB is not ready, do not leave this until tomorrow morning.

---

## 5. Start Both Apps

Open two terminals.

### 5.1. Terminal A: Start store

```bash
cd /home/joselgc/projects/Social-Media-Manager
source .venv/bin/activate
cd store
uvicorn app.main:app --reload --port 8000
```

Expected result:

- The app starts without crashing
- It logs startup successfully
- It does not fail on missing Meta config because `DEBUG=true`

### 5.2. Terminal B: Start master

```bash
cd /home/joselgc/projects/Social-Media-Manager
source .venv/bin/activate
cd master
uvicorn app.main:app --reload --port 9000
```

Expected result:

- The app starts without crashing
- Background health check task starts

---

## 6. Quick Smoke Checks

Do these immediately after both apps are up.

### 6.1. Store health

Open:

- `http://localhost:8000/`
- `http://localhost:8000/health`

Expected result:

- `/` returns `status=running`
- `/health` returns `status=healthy`

### 6.2. Master health

Open:

- `http://localhost:9000/`
- `http://localhost:9000/health`

Expected result:

- `/` returns `status=running`
- `/health` returns `status=healthy`

If either app fails here, stop and fix it now before doing anything else.

---

## 7. Store Login Flow Test

### 7.1. Open store login

Open:

- `http://localhost:8000/admin/login`

Expected result:

- You see the login page
- You are not dropped straight into the dashboard unless you already have a valid cookie

### 7.2. Wrong password test

Enter the wrong password first.

Expected result:

- You stay on the login page
- You see an error message
- You do not enter the dashboard

### 7.3. Correct password test

Enter the real `ADMIN_PASSWORD`.

Expected result:

- You are redirected to `/admin/dashboard`
- The dashboard loads

### 7.4. Logout test

Use the sign-out/logout button if visible.

Expected result:

- You return to `/admin/login`
- Going back to `/admin/dashboard` requires login again

If this fails, do not continue until login/logout works.

---

## 8. Master Login Flow Test

Repeat the same process for master.

### 8.1. Open master login

Open:

- `http://localhost:9000/login`

### 8.2. Wrong secret test

Enter a wrong value first.

Expected result:

- You stay on login
- You see an error

### 8.3. Correct secret test

Enter `MASTER_SECRET_KEY`.

Expected result:

- You enter `/dashboard`

### 8.4. Logout test

Use the sign-out button.

Expected result:

- Session is cleared
- Login is required again

---

## 9. Check That the Store Catalog Is Real

Before doing the customer demo, check the catalog that the AI will use.

Open:

- `http://localhost:8000/test/catalog`

Expected result:

- You get a JSON response with `products` and `count`
- `count` is greater than `0`

If `count=0`, the AI demo will be weaker and order creation may fail.

If the catalog is empty:

1. Fix the Google Sheet
2. Fix the credentials
3. Restart the store if needed
4. Recheck `/test/catalog`

---

## 10. Configure Store Payment Methods Before the Demo

You want the AI to answer payment questions with real-looking store data.

### 10.1. Open store dashboard settings

In the store dashboard, go to the configuration/settings section.

Create at least one or two payment methods with obvious demo values, for example:

- `Zelle` -> `Zelle a demo@correo.com, titular Carlos Demo`
- `Pago Móvil` -> `Pago Móvil Banco Demo, teléfono 0412..., cédula V-...`
- `Binance Pay` -> `Binance Pay ID: 123456789`

Save the payment methods.

Optional but recommended:

- set `Tasa del día` to something realistic like `BCV del día: 128 Bs/USD`

### 10.2. Verify they persist

Refresh the page.

Expected result:

- The payment methods still contain the values you saved
- The daily exchange-rate value also persists if you set one

This matters because later the AI should mention these methods and can answer `¿a qué tasa recibes?` using the configured value.

---

## 11. Customer Conversation Demo via Test UI

This is the most important functional demo.

### 11.1. Open the test chat UI

Open:

- `http://localhost:8000/test/ui`

Use:

- Channel: `whatsapp`
- Sender: `test_demo_customer_1`

### 11.2. Start with a natural greeting

Send:

```text
Hola, qué pijamas tienes disponibles?
```

Expected result:

- The AI responds naturally
- It should sound like a sales assistant
- It should mention available products or ask follow-up questions

### 11.3. Use a real product from the catalog

Go to `http://localhost:8000/test/catalog`, copy a real product name, then continue the chat.

Send something like:

```text
Me interesa la pijama satén azul. Qué tallas tienen y cuánto cuesta?
```

Expected result:

- The AI references real catalog information

### 11.4. Ask for payment methods

Send:

```text
Perfecto. Cómo puedo pagar?
```

Expected result:

- The AI mentions the payment methods you configured
- It should not use placeholder text

### 11.5. Force order creation clearly

Now give all information needed in one message.

Use a real product name from the catalog and send something like:

```text
Quiero comprar 1 pijama satén azul en talla M. Pago por Zelle. Envío por MRW. Dirección: Avenida Principal, edificio Demo, piso 2, Caracas.
```

Expected result:

- The AI should confirm the order or at least move the conversation into order-taking properly
- In the best case, it creates the order directly

If the AI asks follow-up questions instead of creating the order, answer them until the order is confirmed.

### 11.6. Simulate payment proof

Use the same sender and send a message that simulates a payment screenshot.

If using `curl`:

```bash
curl -X POST http://localhost:8000/test/chat \
  -H "Content-Type: application/json" \
  -d '{"message":"Ya pagué, te envié el comprobante","sender":"test_demo_customer_1","channel":"whatsapp","has_image":true}'
```

Expected result:

- The AI should handle this as a payment-proof style message
- The order may move to `proof_received`

### 11.7. Check conversation history

Open:

- `http://localhost:8000/test/history?sender=test_demo_customer_1&channel=whatsapp`

Expected result:

- You see the stored message history
- You can confirm the conversation was persisted

---

## 12. Verify Store Dashboard Data After the Chat

Return to the store dashboard.

### 12.1. Customers tab

Expected result:

- `test_demo_customer_1` appears as a customer
- Tags may exist depending on the conversation
- Conversation state should make sense

### 12.2. Orders tab

Expected result:

- The order appears
- Payment status reflects the current stage

If you successfully simulated payment proof:

- The order should be `proof_received` or later

### 12.3. Customer totals

Check whether the customer now shows:

- `total_orders`
- `total_spent`

Expected result:

- Totals should only increase once the order reaches a paid state

This is one of the important fixes that was implemented.

---

## 13. AI Pause and Resume Test

This is a good, simple operational feature to demo.

### 13.1. Pause AI from the store dashboard

Use the AI toggle button in the dashboard.

Expected result:

- The dashboard clearly shows AI is paused

### 13.2. Test in chat while paused

In `/test/ui`, send:

```text
Hola de nuevo, tienes otra opción?
```

Expected result:

- The response object should show `paused=true`
- The AI should not behave like it is actively responding

### 13.3. Resume AI

Turn AI back on.

Send another message.

Expected result:

- Normal replies resume

This is a strong “owner control” demo point.

---

## 14. Catalog PDF Test

### 14.1. Generate the PDF

In the store dashboard settings, trigger catalog PDF generation.

Expected result:

- The action completes successfully
- The dashboard reports that the PDF exists

### 14.2. Download the PDF

Open:

- `http://localhost:8000/admin/settings/catalog/download-pdf`

Expected result:

- A PDF downloads

This is optional in the presentation, but useful if you want to show a non-chat feature.

---

## 15. Register the Real Store in Master

This is the correct way to test real store <-> master sync.

Do not use `/test/seed` for this sync test. Store and Master must use separate databases; register the real Store database URL in Master instead.

### 15.1. Preferred method: Add the store from the master dashboard

Open the master dashboard and click `+ Add Store`.

Enter:

- Name: any clear demo name
- Owner Name: your friend's name or a demo name
- Owner Contact: phone or email
- App URL: `http://localhost:8000`
- DB URL: the exact same `DATABASE_URL` used by `store/.env`
- Railway Service ID: leave blank unless needed
- Railway Project ID: leave blank unless needed

Save it.

Expected result:

- The store appears in the master overview

### 15.2. Confirm health and stats

Refresh the master overview or open the store detail.

Expected result:

- The store shows up
- Health should become available
- Basic stats should load

If health/stats do not load:

1. Check that the store app is running on port `8000`
2. Check that the `app_url` is correct
3. Check that the `db_url` matches the store database, not the master database

---

## 16. Shared Runtime Settings Sync Test

This is one of the most important demo points.

You need to prove that store and master are looking at the same runtime settings.

### 16.1. Store -> Master sync

From the store dashboard:

1. Change `exchange_rate_reference` or `manual_exchange_rate`
2. Change a shared AI setting like `llm_temperature`
3. Save

Then go to the same store in the master dashboard and refresh the detail view.

Expected result:

- The shared AI setting appears updated in master
- The selected/manual exchange-rate value appears updated because Master can manage those shared rows

### 16.2. Master -> Store sync

From the master dashboard:

1. Toggle `ai_enabled`
2. Change another shared AI setting such as `llm_max_tokens`
3. Save runtime settings

Then reload the store dashboard settings page.

Expected result:

- The changed shared AI values appear in the store dashboard
- If you changed `ai_enabled`, the store dashboard should reflect the new AI state
- Payment methods remain Store-only; selected/manual exchange-rate settings can be managed from either dashboard

### 16.3. Prove store-only payment settings affect the AI immediately

After changing payment methods from the store dashboard, go back to the store test chat and ask:

```text
Cuáles son los métodos de pago?
```

Expected result:

- The answer reflects the latest payment methods
- If you also changed `Tasa del día`, asking `¿a qué tasa recibes?` should use the new value
- You do not need a redeploy

This is probably the strongest “multi-store control plane” proof in the whole demo.

---

## 17. Broadcasts Test

Use this section only if you have time.

### 17.1. Safe demo version

Use the store dashboard to:

1. Create a broadcast
2. Preview the audience by tags
3. Show the matching-customer count

This is enough if you do not want to risk real message delivery in the presentation.

### 17.2. Real send version

Only do this if all of the following are true:

1. WhatsApp credentials are real
2. You have a valid approved template
3. The recipient has opted in
4. You already tested it tonight

Then:

1. Create a broadcast for a known tag
2. Send it
3. Show the resulting status

Expected result:

- `sent`, `partial`, or `failed` should be truthful

If you have not tested real send already, do not use this in tomorrow's demo.

---

## 18. Optional Master Demo Helpers

### 18.1. Master test UI

Open:

- `http://localhost:9000/test/ui`

This can be useful if you want to inspect master APIs quickly.

### 18.2. Seed fake stores

Endpoint:

- `POST http://localhost:9000/test/seed`

Use this only if you want the master dashboard to look populated for a visual demo.

Important warning:

- This is useful for showing the master overview
- It does not prove sync with the live `store/` app; never point Store and Master at the same database

---

## 19. Reset Steps Between Demo Attempts

If the conversation history gets messy, reset only the test customer.

### 19.1. Reset the fake customer

```bash
curl -X DELETE "http://localhost:8000/test/reset?sender=test_demo_customer_1&channel=whatsapp"
```

Expected result:

- The customer, conversation history, orders, and usage log for that sender are deleted

### 19.2. Start fresh

Return to:

- `http://localhost:8000/test/ui`

Use the same sender again.

### 19.3. If master fake data is cluttered

If you used `master/test/seed` and want to clean it:

```bash
curl -X DELETE http://localhost:9000/test/reset
```

Be careful:

- This deletes all stores, credentials, and audit log entries from the master database

---

## 20. Recommended Presentation Flow

Use this exact order.

### 20.1. Opening

Say:

1. This is a lightweight but functional sales assistant for a store
2. It has a store-side dashboard for operations
3. It also has a master control plane for multi-store management

### 20.2. Show store login

Open:

- `http://localhost:8000/admin/login`

Log in.

### 20.3. Show customer conversation

Open:

- `http://localhost:8000/test/ui`

Have a short but clear conversation:

1. Ask for products
2. Ask for price
3. Ask for payment methods
4. Create an order with a real product name
5. Simulate sending payment proof

### 20.4. Show store dashboard data

Return to the store dashboard:

1. Customers tab
2. Orders tab
3. Settings tab

Show that the data persisted.

### 20.5. Show master login

Open:

- `http://localhost:9000/login`

Log in.

### 20.6. Show the same store from master

Open the store detail view.

Show:

1. Store stats
2. Runtime settings
3. Scheduled jobs and environment variables

### 20.7. Prove settings sync

Change a shared AI setting in master.

Then go back to the store dashboard and refresh.

Show:

- The value changed there too

If you want an even stronger close:

Ask the AI again something affected by that runtime setting and show that it reflects the change.

### 20.8. Ending

Summarize:

1. The AI can talk to customers
2. The store owner has operational control
3. The master dashboard can manage shared AI/store runtime settings centrally
4. No redeploy is needed for runtime setting changes

---

## 21. If Something Breaks Right Before the Demo

Use this order.

### 21.1. If store will not start

Check:

1. `DEBUG=true`
2. `DATABASE_URL`
3. `GOOGLE_SHEETS_CREDENTIALS_B64`
4. `PRODUCT_SHEET_ID`
5. `OPENAI_API_KEY`
6. `ADMIN_PASSWORD`

### 21.2. If master will not start

Check:

1. `DATABASE_URL`
2. `MASTER_SECRET_KEY`
3. `ENCRYPTION_KEY`
4. `APP_BASE_URL=http://localhost:9000`

### 21.3. If login fails

Check:

1. You are using the right password/secret
2. Cookies are not blocked in the browser
3. You did not leave an old bad tab open in another browser profile

### 21.4. If the AI answers strangely

Check:

1. The catalog is loaded at `/test/catalog`
2. Payment settings are filled
3. The message uses a real product from the catalog
4. AI is not paused

### 21.5. If the store does not appear healthy in master

Check:

1. The store is running on `http://localhost:8000`
2. `app_url` in master is exactly `http://localhost:8000`
3. The DB URL in master points to the real store DB

### 21.6. If settings do not appear synced

Check:

1. You saved the setting successfully
2. You refreshed the other dashboard
3. The master store record uses the correct store DB

---

## 22. Final Night-Before Checklist

Do these tonight, not tomorrow:

- Start `store/`
- Start `master/`
- Verify both login flows
- Verify `/test/catalog` returns products
- Verify one full conversation works in `/test/ui`
- Verify one order appears in the dashboard
- Verify payment methods and exchange rate can be saved
- Verify a real store record exists in master
- Verify store -> master sync
- Verify master -> store sync
- Decide whether you will demo broadcasts or skip them
- Keep one clean test sender ready for the actual presentation, for example `test_demo_customer_1`

If all of the above works tonight, tomorrow's demo should be straightforward.
