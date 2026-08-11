# Meta Instagram Comment/Story Context + Kommo

## Complete Deployment and Testing Guide

## 1. What this integration does

The application continues using **Kommo as the communication backend**. Meta is an optional source of reliable public-comment/post context and private Story-reply context. Kommo remains responsible for every customer-visible response.

```text
Customer comments on Instagram
        │
        ├── Meta webhook
        │     └── Comment ID, username, text and Instagram media ID
        │
        └── Kommo
              └── Comment event and Salesbot callback
                         │
                         ▼
              Application correlates both events
                         │
                         ▼
              Instagram post → catalog product mapping
                         │
                         ▼
              Current product price and availability
                         │
                         ▼
                  Reply sent through Kommo
```

Meta must **not** send the final reply. The production configuration remains:

```env
CHANNEL_BACKEND=kommo
```

The Meta endpoint is specifically a signed, context-only webhook. It stores comment and Story-reply context, enriches media information, and correlates events with waiting Kommo jobs. Enable comment and Story context independently with `META_INSTAGRAM_CONTEXT_ENABLED` and `META_STORY_CONTEXT_ENABLED`.

This implementation uses:

```text
Instagram API with Facebook Login
API host: graph.facebook.com
Credential: Facebook Page access token
Webhook fields: comments, plus messages when Story context is enabled
```

It does not use the newer Instagram Login integration based on `graph.instagram.com` and `instagram_business_*` permissions. Meta officially treats those as separate login and credential models.

---

# 2. Prepare a staging environment

Do not enable the new Meta listener directly in the existing production service before testing.

## 2.1 Recommended Railway structure

Create a separate Railway project or staging environment containing:

```text
Application service
PostgreSQL service
```

Configure the application service with:

```text
Repository: Joselgc1/Social-Media-Manager
Branch: dev
Root directory: /store
Replicas: 1
```

Railway supports deploying an isolated application from a monorepo by configuring the service’s **Root Directory**.

Use one application replica because your scheduler runs inside the application process.

## 2.2 Add PostgreSQL

In the Railway project canvas:

1. Click **Create** or press `Ctrl/Cmd + K`.
2. Choose **Database**.
3. Choose **PostgreSQL**.
4. Name the service something recognizable, such as:

```text
StorePostgres
```

Railway’s PostgreSQL service exposes a `DATABASE_URL` variable for applications in the same project.

In the application service, add:

```env
DATABASE_URL=${{StorePostgres.DATABASE_URL}}
```

Railway reference variables resolve values from another service by using the service name as the namespace.

## 2.3 Automatic migrations and startup

The repository’s Railway configuration runs:

```bash
python scripts/migrate.py
```

before starting:

```bash
uvicorn app.main:app --host 0.0.0.0 --port $PORT
```

Copy your existing working Kommo, catalog, LLM, Telegram and store variables into staging, but connect the staging service to the staging database.

Keep:

```env
DEBUG=false
OUTBOUND_PROCESSING_ENABLED=true
AI_ORCHESTRATION_MODE=legacy
```

Railway variable changes remain staged until you review and deploy them.

---

# 3. Verify the Meta business assets

## 3.1 Instagram must be professional

The Zona Pink Instagram account must be:

```text
Business
or
Creator
```

A consumer/personal account is not supported by Instagram API with Facebook Login. Meta also requires this integration type to use an Instagram professional account linked to a Facebook Page.

On Instagram, the profile should show something similar to:

```text
Professional dashboard
Panel para profesionales
```

## 3.2 Connect Instagram to the Facebook Page

The current Facebook desktop route is:

1. Log in to Facebook.
2. Click your profile picture.
3. Click **See all profiles**.
4. Switch to the Zona Pink Page.
5. Click the Page profile picture.
6. Open **Settings & privacy**.
7. Open **Settings**.
8. Under **Permissions**, open **Linked accounts**.
9. Next to Instagram, click **View**.
10. Click **Connect account**.
11. Log into the Zona Pink Instagram account.

Meta documents that route for connecting a Facebook Page and Instagram account.

Alternatively, from Instagram:

1. Open the Instagram profile.
2. Tap **Edit profile**.
3. Under **Public business information**, tap **Page**.
4. Select or connect the Facebook Page.

Do not disconnect Instagram from Kommo.

## 3.3 Confirm Page access

Your Facebook profile should have access to the Zona Pink Facebook Page.

Facebook distinguishes:

```text
Facebook access
Task access
```

Facebook access allows a person to switch into the Page and manage it directly. Task access is primarily for management through Business Suite, Ads Manager and related business tools.

To inspect it:

1. Switch into the Facebook Page.
2. Open **Settings & privacy → Settings**.
3. Open **Page setup → Page access**.

Full control is preferable for initial API setup.

## 3.4 Business Portfolio

Confirm that all of these belong to the same Business Portfolio:

```text
[ ] Facebook Page
[ ] Instagram professional account
[ ] Meta developer app
[ ] Your Facebook administrator profile
```

In the Meta app, inspect:

```text
App Settings → Basic
Configuración de la app → Información básica
```

Confirm the correct Business Portfolio is selected.

## 3.5 Business verification

Open Meta Business Suite and inspect:

```text
Settings → Security Center → Business verification
Configuración → Centro de seguridad → Verificación del negocio
```

Start or complete verification using the legal business information and supporting documents Meta requests.

Submitted documents should be clear, in colour, show the complete document and must not be expired or altered.

### Verification and access requirements depend on the deployment

You may continue callback verification and read-only Graph API staging while verification is pending. The requirements for Live mode depend on the assets the app serves and the access requirements shown in the Meta dashboard.

Business verification, App Review or Advanced Access may be required for:

```text
Serving Instagram accounts owned by other businesses
Permissions for which Meta requires Advanced Access
Production permission approval shown in the Meta dashboard
System-user or durable business credentials
Requirements displayed before switching the app to Live
```

For an Instagram account owned or managed by the same business, Standard Access may be sufficient for some permissions. Successful account and media queries prove the token and asset relationship, but the Meta dashboard remains authoritative for the app's Live-mode, verification, App Review and access-level requirements.

---

# 4. Meta developer app configuration

You have already completed the corrected app-creation process.

## 4.1 Correct app structure

The app should be:

```text
App type: Business
```

The products added should be:

```text
Instagram Graph API
Facebook Login for Business
Webhooks
```

Do not return to the earlier login-only app that offered only the Facebook authentication and ads-management use cases.

## 4.2 Record the App ID and App Secret

Open:

```text
App Settings → Basic
Configuración de la app → Información básica
```

Record:

```text
App ID
App Secret
```

Click **Show** beside App Secret and complete Meta’s security prompt.

The App Secret becomes:

```env
META_APP_SECRET=YOUR_META_APP_SECRET
```

The application uses it to validate the `X-Hub-Signature-256` signature on incoming Meta POST requests. Requests with invalid signatures are rejected with HTTP 403.

Never put the App Secret in:

```text
Git
Frontend JavaScript
Screenshots
Application logs
Public documentation
```

## 4.3 App role

Open:

```text
App Roles → Roles
Roles de la app → Roles
```

Confirm that the Facebook profile used during setup appears as an administrator or developer.

## 4.4 Basic compliance information

Complete the available fields under **App Settings → Basic**, including:

```text
Contact email
Privacy Policy URL
User data deletion URL or instructions
App category
App icon
```

These fields become important when preparing the app for Live mode or App Review.

---

# 5. Generate the Meta credentials

## 5.1 Token chain

The required token flow is:

```text
Facebook User access token
        │
        ▼
GET /me/accounts
        │
        ▼
Facebook Page access token
        │
        ▼
Stored as INSTAGRAM_ACCESS_TOKEN
```

The Page access token acts on behalf of the Facebook Page linked to the Instagram professional account.

## 5.2 Open Graph API Explorer

In Meta for Developers:

```text
Tools → Graph API Explorer
Herramientas → Explorador de la API Graph
```

The page should contain:

```text
App selector
Access Token field
Get Token button
API version selector
HTTP method selector
Request field
Submit button
Response panel
```

## 5.3 Select the correct app and version

In the application selector, choose the new Business app.

Do not leave Meta’s default Graph API Explorer app selected.

In the version selector, choose the newest stable Graph API version available for your app.

Record the exact value, for example:

```text
v24.0
```

Do not blindly copy the example. Use the version currently shown in your Meta dashboard.

The application validates a version in the form `v<number>.<number>` and builds requests against that configured Graph API version.

## 5.4 Generate a User access token

Click:

```text
Get Token → Get User Access Token
Obtener token → Obtener token de acceso de usuario
```

Request:

```text
pages_show_list
pages_read_engagement
pages_manage_metadata
instagram_basic
instagram_manage_comments
business_management
```

Meta’s documented Facebook Login permissions for this Instagram comment use case include:

```text
pages_show_list
instagram_basic
pages_read_engagement
pages_manage_metadata
instagram_manage_comments
```

For your Business Portfolio, also include:

```text
business_management
```

This was required in your actual setup: without it, `/me/accounts` returned an empty list; after adding it, Meta returned the managed Page. Treat it as a practical requirement for this portfolio.

For comment-only context you do not need:

```text
instagram_content_publish
instagram_manage_messages
pages_messaging
```

for the comment-only integration. Story context is a separate private-message subscription: the application parses Story replies from Meta's `messaging` payload, so verify the current Meta `messages` subscription and permission requirements before enabling `META_STORY_CONTEXT_ENABLED=true`.

Generate a completely new token after selecting the permissions. During the Facebook authorization flow:

1. Continue with the Facebook profile that manages Zona Pink.
2. Grant access to the correct Page and business assets.
3. Accept all selected permissions.
4. Return to Graph API Explorer.

## 5.5 Verify the generated token

Run:

```text
me?fields=id,name
```

Confirm the returned user is the Facebook profile that manages the Page.

Then run:

```text
me/permissions
```

Confirm the required permissions have:

```json
{
  "status": "granted"
}
```

## 5.6 Retrieve the Page token and Instagram account ID

In Graph API Explorer:

```text
Method: GET
```

Request:

```text
me/accounts?fields=id,name,access_token,tasks,instagram_business_account
```

A successful response should resemble:

```json
{
  "data": [
    {
      "id": "FACEBOOK_PAGE_ID",
      "name": "Zona Pink",
      "access_token": "PAGE_ACCESS_TOKEN",
      "tasks": [
        "ANALYZE",
        "MESSAGING",
        "MODERATE",
        "CREATE_CONTENT",
        "MANAGE"
      ],
      "instagram_business_account": {
        "id": "INSTAGRAM_ACCOUNT_ID"
      }
    }
  ]
}
```

That response structure is normal. `instagram_business_account` returning only an `id` is expected; you query that Instagram object separately afterward.

Record these application settings, keeping `FACEBOOK_PAGE_ID` only as a shell/Graph API diagnostic placeholder:

```env
INSTAGRAM_ACCESS_TOKEN=PAGE_ACCESS_TOKEN
INSTAGRAM_ACCOUNT_ID=INSTAGRAM_BUSINESS_ACCOUNT_ID
```

`FACEBOOK_PAGE_ID` is useful in the Graph API commands in this guide but is not an application config variable; do not add it to `store/.env`.

The application variable named `INSTAGRAM_ACCESS_TOKEN` must contain the **Facebook Page access token**, not the temporary User token.

## 5.7 Test the Instagram account

Replace the token in Graph API Explorer with the Page access token.

Run:

```text
INSTAGRAM_ACCOUNT_ID?fields=id,username,account_type
```

Expected:

```json
{
  "id": "1784...",
  "username": "zonapink...",
  "account_type": "BUSINESS"
}
```

## 5.8 Test recent media

Run:

```text
INSTAGRAM_ACCOUNT_ID/media?fields=id,permalink,caption,media_type,media_product_type,timestamp&limit=5
```

Choose one returned media ID and run:

```text
MEDIA_ID?fields=id,permalink,caption,media_type,media_product_type,timestamp,thumbnail_url
```

The application requests those exact media fields when enriching a comment webhook event.

Do not proceed until both account and media queries work.

## 5.9 When `/me/accounts` returns an empty list

Run through these checks:

```text
[ ] Correct Business app selected in Graph API Explorer
[ ] Token belongs to the correct Facebook profile
[ ] pages_show_list is granted
[ ] business_management is granted
[ ] Facebook profile manages the Page
[ ] Page and app belong to the same Business Portfolio
[ ] Instagram is linked to the Page
```

Regenerate the User token after changing permissions.

---

# 6. Create a webhook verification token

This token is created by you. Meta does not provide it.

Because the previous token was exposed during testing, generate a new one before continuing:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Save the result as:

```env
INSTAGRAM_VERIFY_TOKEN=YOUR_NEW_RANDOM_VALUE
```

The three Meta secrets serve different purposes:

```text
META_APP_SECRET
    Validates POST webhook signatures.

INSTAGRAM_ACCESS_TOKEN
    Reads Instagram media through graph.facebook.com.
    Must contain the Facebook Page access token.

INSTAGRAM_VERIFY_TOKEN
    Validates Meta's initial GET callback challenge.
    Created by you.
```

The GET verification endpoint responds only when:

```text
META_INSTAGRAM_CONTEXT_ENABLED=true
hub.mode=subscribe
hub.verify_token matches INSTAGRAM_VERIFY_TOKEN
hub.challenge is present
```

---

# 7. Configure the application

## 7.1 Required Meta context variables

The current application defines:

```env
META_APP_SECRET=
INSTAGRAM_ACCESS_TOKEN=
INSTAGRAM_VERIFY_TOKEN=
INSTAGRAM_ACCOUNT_ID=
META_GRAPH_API_VERSION=
META_INSTAGRAM_CONTEXT_ENABLED=
META_CONTEXT_WAIT_SECONDS=
META_CONTEXT_MATCH_WINDOW_SECONDS=
META_CONTEXT_EVENT_RETENTION_HOURS=
META_STORY_CONTEXT_ENABLED=
META_STORY_CONTEXT_WAIT_SECONDS=
META_STORY_CONTEXT_MATCH_WINDOW_SECONDS=
INSTAGRAM_STORY_MAPPING_TTL_HOURS=
INSTAGRAM_STORY_CONTEXT_TTL_HOURS=
```

Use:

```env
CHANNEL_BACKEND=kommo

META_INSTAGRAM_CONTEXT_ENABLED=true
META_APP_SECRET=YOUR_META_APP_SECRET
INSTAGRAM_ACCESS_TOKEN=YOUR_PAGE_ACCESS_TOKEN
INSTAGRAM_VERIFY_TOKEN=YOUR_NEW_VERIFY_TOKEN
INSTAGRAM_ACCOUNT_ID=YOUR_INSTAGRAM_ACCOUNT_ID
META_GRAPH_API_VERSION=vXX.X

META_CONTEXT_WAIT_SECONDS=10
META_CONTEXT_MATCH_WINDOW_SECONDS=45
META_CONTEXT_EVENT_RETENTION_HOURS=24
META_STORY_CONTEXT_ENABLED=false
META_STORY_CONTEXT_WAIT_SECONDS=3
META_STORY_CONTEXT_MATCH_WINDOW_SECONDS=45
INSTAGRAM_STORY_MAPPING_TTL_HOURS=24
INSTAGRAM_STORY_CONTEXT_TTL_HOURS=24

APP_BASE_URL=https://YOUR-PUBLIC-DOMAIN
DEBUG=false
OUTBOUND_PROCESSING_ENABLED=true
```

Keep all existing Kommo variables, including:

```env
KOMMO_SUBDOMAIN=
KOMMO_ACCESS_TOKEN=
KOMMO_INTEGRATION_ID=
KOMMO_INTEGRATION_SECRET=
KOMMO_WEBHOOK_SECRET=

KOMMO_WHATSAPP_SALESBOT_ID=
KOMMO_SALESBOT_ID= # Temporary WhatsApp-only fallback

KOMMO_AI_MODE_FIELD_ID=
KOMMO_AI_ACTIVE_ENUM_ID=
KOMMO_AI_HUMAN_ENUM_ID=
KOMMO_AI_PAUSED_ENUM_ID=
KOMMO_CHATS_MEDIA_ENABLED=false
KOMMO_CHATS_PRODUCT_IMAGES_ENABLED=false
KOMMO_CHATS_CATALOG_PDF_ENABLED=false
KOMMO_CHATS_API_MONTHLY_LIMIT=
KOMMO_CHATS_PDF_ATTACHMENT_TYPE=
```

When Meta context is enabled, application startup validates that:

```text
CHANNEL_BACKEND is kommo
META_APP_SECRET is present
INSTAGRAM_ACCESS_TOKEN is present
INSTAGRAM_VERIFY_TOKEN is present
INSTAGRAM_ACCOUNT_ID is present
META_GRAPH_API_VERSION is present
```

The Meta route is registered when Kommo mode is active and at least one context listener is enabled:

```env
CHANNEL_BACKEND=kommo
META_INSTAGRAM_CONTEXT_ENABLED=true  # comments
# or META_STORY_CONTEXT_ENABLED=true # Story replies
```

---

# 8. Run locally through ngrok

This is useful for initial webhook verification and controlled development testing.

## 8.1 Start the application

From the repository:

```bash
source .venv/bin/activate
cd store
uvicorn app.main:app --reload --port 8000
```

Make sure your local `.env` contains the Meta and Kommo configuration.

## 8.2 Start ngrok

In another terminal:

```bash
ngrok http 8000
```

Copy the HTTPS forwarding domain, for example:

```text
https://your-random-domain.ngrok-free.dev
```

Set:

```env
APP_BASE_URL=https://your-random-domain.ngrok-free.dev
```

Restart the application after changing local environment variables.

## 8.3 Test the webhook handshake

Use this exact Bash formatting:

```bash
curl -i -G \
  "https://YOUR-NGROK-DOMAIN/webhooks/meta/instagram-context" \
  --data-urlencode "hub.mode=subscribe" \
  --data-urlencode "hub.verify_token=YOUR_NEW_VERIFY_TOKEN" \
  --data-urlencode "hub.challenge=123456"
```

The backslash must be the final character on each continued line. Do not place spaces after it.

Expected:

```text
HTTP/2 200

123456
```

A single-line equivalent is:

```bash
curl -i -G "https://YOUR-NGROK-DOMAIN/webhooks/meta/instagram-context" --data-urlencode "hub.mode=subscribe" --data-urlencode "hub.verify_token=YOUR_NEW_VERIFY_TOKEN" --data-urlencode "hub.challenge=123456"
```

## 8.4 Important ngrok limitation

An ngrok domain may change when the tunnel restarts.

Whenever it changes, update:

```text
Meta webhook Callback URL
APP_BASE_URL
Any Kommo staging callback that must reach the same local application
```

Full end-to-end testing requires both the Meta comment event and the Kommo Salesbot callback to reach the same application and database.

---

# 9. Deploy the Railway staging service

Add the variables from Part 7 to the Railway application service.

Set:

```env
APP_BASE_URL=https://YOUR-STAGING-DOMAIN
DEBUG=false
```

Deploy the staged changes.

## 9.1 Verify health

Run:

```bash
curl -sS "https://YOUR-STAGING-DOMAIN/health"
```

Expected relevant section:

```json
{
  "status": "healthy",
  "channels": {
    "backend": "kommo",
    "kommo": true,
    "instagram_via_kommo": true,
    "meta_instagram_context": true
  }
}
```

The health endpoint reports the backend and whether the supplemental Meta listener is active.

## 9.2 Verify the callback before opening Meta

```bash
curl -i -G \
  "https://YOUR-STAGING-DOMAIN/webhooks/meta/instagram-context" \
  --data-urlencode "hub.mode=subscribe" \
  --data-urlencode "hub.verify_token=YOUR_NEW_VERIFY_TOKEN" \
  --data-urlencode "hub.challenge=123456"
```

Do not proceed until it returns HTTP 200 and `123456`.

---

# 10. Configure the Meta Webhooks product

## 10.1 Open the Webhooks product

In the Meta App Dashboard:

1. Select your Business app.
2. Open **Webhooks** in the left sidebar.
3. Find the object selector:

```text
Select an object
Seleccionar un objeto
```

4. Select:

```text
Instagram
```

Do not select:

```text
Page
User
Application
WhatsApp Business Account
```

## 10.2 Configure the callback

Click the option shown by your dashboard:

```text
Subscribe to this object
Suscribirse a este objeto
```

or:

```text
Edit subscription
Editar suscripción
```

Enter:

```text
Callback URL:
https://YOUR-STAGING-DOMAIN/webhooks/meta/instagram-context

Verify Token:
The exact INSTAGRAM_VERIFY_TOKEN value
```

Click:

```text
Verify and Save
Verificar y guardar
```

Meta sends the GET challenge to your callback. The application compares the verify token and returns the challenge.

## 10.3 Subscribe to `comments`

After verification, Meta should display Instagram webhook fields.

Find:

```text
comments
```

Click:

```text
Subscribe
Suscribirse
```

For comment-only context, subscribe to:

```text
comments
```

Do not enable unrelated fields for a comment-only rollout:

```text
messaging_postbacks
messaging_seen
live_comments
mentions
story_insights
```

Meta’s documented comment payload contains:

```text
Instagram professional-account ID
Comment ID
Commenter Instagram-scoped ID
Commenter username
Comment text
Instagram media ID
Media product type/location
```

The application parser supports Instagram `comments` events and Story replies from the `messaging` payload. If `META_STORY_CONTEXT_ENABLED=true`, also subscribe to the appropriate `messages` field and satisfy Meta's current permission/access requirements; do not enable Story context with a comment-only subscription.

## 10.4 Enable webhook notifications for the Instagram account

After subscribing the app to the Instagram `comments` field in the dashboard, enable webhook notifications for the managed Instagram professional account. Use the Instagram account ID and Page access token returned through `/me/accounts`:

```bash
curl -i -X POST \
  "https://graph.facebook.com/vXX.X/INSTAGRAM_ACCOUNT_ID/subscribed_apps" \
  --data-urlencode "subscribed_fields=comments" \
  --data-urlencode "access_token=PAGE_ACCESS_TOKEN"
```

This uses `graph.facebook.com` because this deployment uses Instagram API with Facebook Login. It does not subscribe to the Facebook Page `feed` field.

Expected response:

```json
{
  "success": true
}
```

Verify the Instagram account subscription:

```bash
curl -G \
  "https://graph.facebook.com/vXX.X/INSTAGRAM_ACCOUNT_ID/subscribed_apps" \
  --data-urlencode "access_token=PAGE_ACCESS_TOKEN"
```

The response must list the Meta app and its subscribed Instagram fields. Use the permissions and access level required by the Meta dashboard for the selected Graph API version.

---

# 11. Steps that are not required

## 11.1 Do not subscribe the Page to Messenger fields

Do not run:

```text
FACEBOOK_PAGE_ID/subscribed_apps?subscribed_fields=messages,messaging_postbacks
```

That belongs to Messenger/Page messaging integrations. Meta’s Messenger Platform requires Page messaging permissions and is a separate product flow.

The repository contains an older helper called `subscribe_page_to_webhooks()`, but that helper explicitly subscribes to:

```text
messages,messaging_postbacks
```

and is intended for direct Meta DM delivery—not the new context-only Kommo integration.

Because:

```env
CHANNEL_BACKEND=kommo
```

Kommo continues receiving and sending Instagram DMs.

## 11.2 Do not use `graph.instagram.com/subscribed_apps`

Do not run:

```text
https://graph.instagram.com/INSTAGRAM_ACCOUNT_ID/subscribed_apps
```

That endpoint belongs to Instagram API with Instagram Login and expects an Instagram User token and `instagram_business_*` permissions.

Your implementation uses:

```text
graph.facebook.com
Facebook Page access token
INSTAGRAM_ACCOUNT_ID/subscribed_apps
instagram_basic
instagram_manage_comments
pages_read_engagement
pages_manage_metadata
```

---

# 12. Understand Meta’s Test button

The `comments` field may show a **Test** button.

It can demonstrate that Meta can reach the callback, but Meta may send a synthetic payload using a sample Instagram account ID.

Your endpoint checks:

```text
event.instagram_account_id == INSTAGRAM_ACCOUNT_ID
```

and ignores events for another account.

Therefore:

```text
Test button reaches endpoint
≠
Complete integration is working
```

The definitive test is a real comment on a Zona Pink post.

---

# 13. Configure an Instagram product mapping

Open:

```text
https://YOUR-STAGING-DOMAIN/admin/login
```

Log in with the staging `ADMIN_PASSWORD`.

Open the Instagram content mapping section.

For the first, simplest test:

1. Choose a normal Zona Pink post, Reel, or current Story. Manual mappings accept public `/p/`, `/reel/`, and `/stories/{username}/{story-id}/` URLs. Story URLs are verified against the connected account's current Stories response and are rejected after expiry; Highlights are not supported.
2. Use a post advertising exactly one product.
3. Copy its public Instagram URL.
4. Paste the URL into the mapping form.
5. Select one catalog product for this first test.
6. Save the mapping.
7. Confirm the mapping is active.

The mapping API normalizes post/Reel/Story URLs, validates catalog SKUs, preserves display order, and stores one or more product relations. Loading the mapping list fetches up to 50 current Stories from Meta and upserts their rows. Story URL creation requires `INSTAGRAM_ACCESS_TOKEN` and `INSTAGRAM_ACCOUNT_ID`, and the Story must still appear in that current response. A `422` means the Story URL is unsupported, expired, or did not match the connected account; a `502` means Meta verification failed. Story mappings expire according to `INSTAGRAM_STORY_MAPPING_TTL_HOURS` and remain visible as historical records but are excluded from message matching. Multiple-product public comments request clarification for generic price/availability questions and can answer when an explicit reference resolves one mapped product.

Start with:

```text
One post
One advertised product
One mapped product
Known current price
Known current stock
```

The mapping may initially have no Meta media ID. The first matching Meta event can enrich it using the media ID and permalink.

---

# 14. Confirm the Kommo staging path

The complete test requires the Instagram comment to trigger the correct Kommo Salesbot.

Confirm that the comment-specific Salesbot or widget callback reaches:

```text
POST https://YOUR-STAGING-DOMAIN/webhooks/kommo/salesbot
```

The Kommo general webhook should reach:

```text
POST https://YOUR-STAGING-DOMAIN/webhooks/kommo/events/YOUR_KOMMO_WEBHOOK_SECRET
```

Do not accidentally direct Meta to staging while Kommo continues sending the corresponding comment event only to production. Both sides of the event correlation must reach the same database.

When using local ngrok, the same rule applies.

---

# 15. Run the first end-to-end test

## 15.1 Use another Instagram account

Do not use the Zona Pink business account as the commenter.

From another Instagram account, comment on the mapped post. While the Meta app is in Development mode, that account must belong to a Meta user who has accepted an administrator, developer, or tester role for the app. Otherwise, switch the app to Live after satisfying Meta's production requirements before testing with an ordinary account.

Comment:

```text
Precio prueba 7421
```

The unique number makes the same event easy to find in:

```text
Instagram
Meta webhook logs
Kommo
Railway logs
PostgreSQL
```

## 15.2 Expected processing order

Meta and Kommo may arrive in either order.

Expected flow:

```text
1. Customer comments on Instagram.

2. Meta sends:
   POST /webhooks/meta/instagram-context

3. Kommo receives the comment.

4. The comment-triggered Salesbot sends:
   POST /webhooks/kommo/salesbot

5. The application stores both events.

6. Correlation identifies one matching Meta event and Kommo job.

7. The application reads the Meta media ID.

8. The Graph API returns the post permalink and media metadata.

9. The content mapping resolves the mapped SKU set and, when necessary, the referenced product.

10. The product SKU is added to the waiting Kommo job.

11. The job becomes ready.

12. The product price or availability is resolved from the current catalog.

13. Kommo sends the public response.
```

The Meta webhook itself only stores context and schedules processing; it never sends the final customer response.

Expected response pattern:

```text
PRODUCT_NAME cuesta $PRICE.
```

---

# 16. Inspect application logs

Search the staging logs around the comment timestamp.

Useful success signals include events showing:

```text
Meta Instagram event received
Meta/Kommo correlation matched
Meta media enrichment succeeded
Product mapping resolved
Kommo job processed/sent
```

Investigate messages such as:

```text
Rejected Meta Instagram context webhook signature
Ignored Meta Instagram context event for a different account
Correlation ambiguous
Correlation timed out
Meta Graph API request failed
Mapping not found
```

## Signature rejection

This usually means:

```text
META_APP_SECRET is incorrect
The webhook is coming from a different Meta app
The raw request body was modified by a proxy
The wrong environment was deployed
```

## Different account warning

This means the webhook’s `entry.id` does not equal:

```env
INSTAGRAM_ACCOUNT_ID=
```

The application intentionally ignores events for other Instagram accounts.

---

# 17. Inspect PostgreSQL

## 17.1 Meta event

Run:

```sql
SELECT
    id,
    external_event_id,
    event_type,
    comment_id,
    media_id,
    media_permalink,
    sender_username,
    message_text,
    correlation_status,
    matched_kommo_job_id,
    correlation_details,
    event_timestamp,
    created_at
FROM meta_instagram_context_events
ORDER BY created_at DESC
LIMIT 10;
```

Expected:

```text
event_type = comment
media_id is populated
media_permalink is populated
correlation_status = matched
matched_kommo_job_id is populated
```

## 17.2 Kommo job

```sql
SELECT
    id,
    external_message_id,
    interaction_type,
    channel,
    status,
    context_status,
    meta_context_event_id,
    context_correlation_score,
    public_comment_context,
    last_error,
    created_at,
    completed_at
FROM kommo_message_jobs
WHERE interaction_type = 'instagram_comment'
ORDER BY created_at DESC
LIMIT 10;
```

Expected final state:

```text
interaction_type = instagram_comment
channel = instagram
context_status = matched
status = sent
```

The context should resemble:

```json
{
  "comment_id": "...",
  "media_id": "...",
  "post_url": "...",
  "context_provider": "meta",
  "correlation_status": "matched",
  "mapping_status": "resolved",
  "product_sku": "YOUR-SKU"
}
```

## 17.3 Mapping backfill

```sql
SELECT
    id,
    content_type,
    permalink,
    normalized_permalink,
    shortcode,
    media_id,
    caption_snapshot,
    status,
    updated_at
FROM instagram_content
ORDER BY updated_at DESC;
```

The URL-based mapping should eventually contain the Meta media ID.

---

# 18. Required staging tests

## Test A: Correct single-product mapping

1. Map one post to one product.
2. Comment:

```text
Precio prueba 7421
```

3. Confirm the exact product price is returned.
4. Comment:

```text
Disponible prueba 7422
```

5. Confirm availability uses current catalog stock.

## Test B: Unmapped post

Comment on a post with no dashboard mapping.

Expected:

```text
No product is guessed
Job waits briefly
Safe DM/WhatsApp fallback is used
Meta event diagnostics record mapping_status = not_found while waiting
The released Kommo job receives mapping_status = timed_out after its deadline
No tight 15-second retry loop
```

## Test C: Similar comments close together

From two Instagram accounts, post:

```text
Precio?
```

within a few seconds.

Expected:

```text
Username and timestamps separate the events when possible
An unresolved collision becomes ambiguous
The application never selects the first candidate arbitrarily
```

## Test D: Arrival order

Repeat the test several times.

Confirm both cases succeed:

```text
Meta arrives before Kommo
Kommo arrives before Meta
```

## Test E: Invalid Meta access token

Temporarily set an invalid staging:

```env
INSTAGRAM_ACCESS_TOKEN=invalid
```

Redeploy and leave a test comment.

Expected:

```text
Webhook is received
Correlation may still occur
Media enrichment fails safely
No product is guessed
If the mapping already has the matching media ID, it may still resolve safely
If enrichment is required to find the mapping, Kommo eventually uses the safe fallback
```

Restore the token afterward.

## Test F: Incorrect Instagram account ID

Temporarily use another value for:

```env
INSTAGRAM_ACCOUNT_ID=
```

Expected log:

```text
Ignored Meta Instagram context event for a different account
```

Restore the correct value afterward.

## Test G: Invalid webhook signature

Send an unsigned POST request manually.

Expected:

```text
HTTP 403
Invalid signature
```

---

# 19. App mode and access level

## 19.1 Development mode

Keep the app in Development mode during the initial setup and controlled technical tests.

Use:

```text
Your own Business Portfolio
Your own Instagram professional account
Facebook users with app roles
```

This restriction also applies to the Instagram account used to create a test comment. An arbitrary customer account does not generate production webhook delivery while the app remains in Development mode.

The dashboard Test button can validate callback delivery but does not prove that real comments will be delivered in the app's current mode. Check the access level and Live-mode requirements shown for the app and permissions in the Meta dashboard.

## 19.2 Live mode

Before using this with normal production traffic:

```text
[ ] Complete App Settings → Basic
[ ] Complete Business Portfolio verification if Meta requires it
[ ] Obtain Advanced Access for permissions or third-party assets that require it
[ ] Complete App Review if shown as required
[ ] Replace temporary staging credentials
[ ] Switch the app to Live
[ ] Test with an ordinary customer account
```

Do not assume that a successful Graph API Explorer test means every production-access requirement is complete. The Meta dashboard is the authoritative place for the app’s current access and review requirements.

---

# 20. Production token strategy

The token generated in Graph API Explorer is acceptable for staging but should not be treated as an unmanaged permanent production credential.

Meta’s Facebook Login integration supports User and system-user access tokens, and Page access tokens are derived from a user or business identity that can manage the Page.

Before production:

1. Use Meta’s durable token flow available for your verified Business Portfolio.
2. Obtain or exchange the appropriate long-lived User or system-user token.
3. Retrieve the Page access token again.
4. Store only the Page token in:

```env
INSTAGRAM_ACCESS_TOKEN=
```

5. Inspect it using:

```text
Tools → Access Token Debugger
Herramientas → Depurador de tokens de acceso
```

Confirm:

```text
Correct App ID
Token is valid
Correct permissions
Expected expiration
Correct Page/business association
```

The current application does not automatically renew Meta tokens. Token validity and expiration need operational monitoring.

---

# 21. Production cutover

After all staging tests pass:

1. Back up the production PostgreSQL database.
2. Merge `dev` into `master`.
3. Deploy `master` to the production Railway service.
4. Confirm migrations succeed.
5. Add the production Meta variables.
6. Set:

```env
META_INSTAGRAM_CONTEXT_ENABLED=true
```

7. Deploy.
8. Confirm `/health` reports Meta context as enabled.
9. Change the Meta callback to:

```text
https://YOUR-PRODUCTION-DOMAIN/webhooks/meta/instagram-context
```

10. Verify and save the callback.
11. Confirm `comments` remains subscribed.
12. Point the Kommo comment Salesbot to the production backend.
13. Create or verify production post mappings.
14. Run one controlled comment.
15. Inspect logs and database records.
16. Monitor the first live comments closely.

---

# 22. Troubleshooting reference

## `/me/accounts` returns `{"data":[]}`

Generate a new User token containing:

```text
pages_show_list
pages_read_engagement
instagram_basic
instagram_manage_comments
business_management
```

Also confirm:

```text
Correct app selected
Correct Facebook user
Correct Business Portfolio
Correct Page access
Instagram linked to Page
```

## `instagram_business_account` contains only `id`

That is correct.

Use the returned ID in:

```text
INSTAGRAM_ACCOUNT_ID?fields=id,username,account_type
```

## Graph API media request fails

Confirm:

```text
Using Page access token, not User token
Using graph.facebook.com
Correct Instagram account ID
instagram_basic granted
pages_read_engagement granted
Valid Graph API version
Media belongs to the managed account
```

## Callback verification returns HTTP 403

Confirm:

```text
META_INSTAGRAM_CONTEXT_ENABLED=true
CHANNEL_BACKEND=kommo
Verify token matches exactly
Correct environment has been restarted/redeployed
Correct /webhooks/meta/instagram-context path
```

## Meta POST returns HTTP 403

Confirm:

```text
META_APP_SECRET belongs to the same app
Meta is calling the expected environment
No proxy modifies the raw body
```

## Callback verifies but real comments do not arrive

Confirm:

```text
Webhooks object is Instagram
comments says Subscribed
Comment is on the configured account's media
INSTAGRAM_ACCOUNT_ID is correct
Instagram account's subscribed_apps response lists the app and comments field
Required permissions are granted at the access level shown by Meta
Business verification and App Review are complete if Meta requires them
App mode permits the test
Callback is still publicly reachable
```

## Meta event exists but Kommo does not match

Confirm:

```text
Comment-specific Salesbot triggered
Salesbot callback reached the same backend
interaction_type is instagram_comment
Comment text was preserved
Both events arrived inside the match window
Username values do not conflict
```

## Mapping is not found

Confirm:

```text
Exact public post or Reel URL was entered, or the Story row was discovered
Mapping status is active
Selected SKU exists in the catalog
Meta permalink normalizes to the same URL
At least one valid product is mapped; use exactly one for the first test
```

---

# 23. Final credential reference

| Variable                             | Correct value                                     |
| ------------------------------------ | ------------------------------------------------- |
| `CHANNEL_BACKEND`                    | `kommo`                                           |
| `META_APP_SECRET`                    | App Secret from App Settings → Basic              |
| `INSTAGRAM_ACCESS_TOKEN`             | Facebook Page access token from `/me/accounts`    |
| `INSTAGRAM_ACCOUNT_ID`               | `instagram_business_account.id`                   |
| `INSTAGRAM_VERIFY_TOKEN`             | New random value created by you                   |
| `META_GRAPH_API_VERSION`             | Version selected and tested in Graph API Explorer |
| `META_INSTAGRAM_CONTEXT_ENABLED`     | `true`                                            |
| `META_CONTEXT_WAIT_SECONDS`          | `10`                                              |
| `META_CONTEXT_MATCH_WINDOW_SECONDS`  | `45`                                              |
| `META_CONTEXT_EVENT_RETENTION_HOURS` | `24`                                              |
| `META_STORY_CONTEXT_ENABLED`         | `false` for comments-only; `true` for Story context |
| `META_STORY_CONTEXT_WAIT_SECONDS`    | `3`                                               |
| `META_STORY_CONTEXT_MATCH_WINDOW_SECONDS` | `45`                                        |
| `INSTAGRAM_STORY_MAPPING_TTL_HOURS`  | `24`                                              |
| `INSTAGRAM_STORY_CONTEXT_TTL_HOURS`  | `24`                                              |

## Final Meta checklist

```text
[ ] Business app exists
[ ] Instagram Graph API product added
[ ] Facebook Login for Business product added
[ ] Webhooks product added
[ ] Correct Business Portfolio selected
[ ] Business verification submitted or completed if Meta requires it
[ ] Instagram is professional
[ ] Instagram is linked to the Facebook Page
[ ] User token includes business_management
[ ] /me/accounts returns the Page
[ ] Page access token copied
[ ] Instagram account ID copied
[ ] Instagram account query succeeds
[ ] Media query succeeds
[ ] New webhook verify token generated
[ ] Application variables deployed
[ ] Verification curl returns 200
[ ] Webhooks object is Instagram
[ ] comments field is subscribed for public-comment context
[ ] messages field and current Meta permissions are enabled if Story context is active
[ ] Instagram account subscription lists every enabled context field
[ ] Required permissions have the access level shown by Meta
[ ] Business verification and App Review are complete if required
[ ] No Facebook Messenger subscription was added
[ ] At least one post, Reel, or discovered Story is mapped to a catalog product
[ ] Meta and Kommo point to the same backend
[ ] Real-comment test succeeds
```
