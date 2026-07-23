# Kommo Migration

## Architecture Overview

`CHANNEL_BACKEND=meta` keeps the current direct Meta webhook and sender flow. `CHANNEL_BACKEND=kommo` uses Kommo official WhatsApp and Instagram integrations as the channel provider and shared inbox, while this app remains the AI, CRM, catalog, order, analytics, escalation, Telegram, and multi-store backend.

```text
WhatsApp / Instagram -> Kommo inbox -> Kommo webhook -> Social-Media-Manager
Social-Media-Manager -> selected Kommo Salesbot widget continuation -> Customer
```

Kommo owns the WhatsApp and Instagram channel connection. The app does not create a custom Kommo Chats API channel and does not call Meta sender modules in Kommo mode.

## Why Kommo

Kommo removes the need to manage Meta Developers app review, long-lived Meta access tokens, WhatsApp Cloud API setup, Instagram Messaging API permissions, and webhook subscriptions directly. The tradeoff is that outbound AI replies must be sent by Salesbot through Kommo's connected channels.

## Backend Modes

`CHANNEL_BACKEND=meta`:

- Registers `/webhooks/whatsapp` and `/webhooks/instagram`.
- Requires production Meta WhatsApp credentials.
- Sends replies and broadcasts through Meta Graph APIs.

`CHANNEL_BACKEND=kommo`:

- Registers `/webhooks/kommo/events/{webhook_secret}` and `/webhooks/kommo/salesbot`.
- Does not require Meta credentials.
- Launches and resumes the configured Kommo Salesbot for replies.
- Rejects direct WhatsApp broadcast delivery.

## Kommo Plan Prerequisites

Kommo private widgets and WebSDK/Salesbot widget usage require a Kommo plan that supports custom widgets and webhooks. Confirm the current plan in Kommo before production setup.

## WhatsApp Coexistence Setup

Connect WhatsApp Business inside Kommo using Kommo's official WhatsApp integration. If WhatsApp Coexistence is available for the account, configure it in Kommo according to Kommo's current UI. Do not connect this backend directly to WhatsApp Cloud API in Kommo mode.

## Instagram Business Setup

Connect the Instagram Business account inside Kommo using Kommo's official Instagram integration. Confirm DMs arrive in the Kommo inbox before enabling this app's Kommo mode.

## Instagram Comment Setup

Public Instagram comments use the same durable path as private messages:

```text
Kommo native comment trigger -> widget callback -> create ready kommo_message_job -> AI response -> continue Salesbot with json.message
```

Private messages still use the backend-created job path:

```text
Kommo webhook -> normalize event -> create kommo_message_job -> launch KOMMO_SALESBOT_ID -> widget callback -> AI response -> continue Salesbot with json.message
```

Create two Salesbot flows that both use the installed Social Media Manager widget:

1. Private-message Salesbot: add the `Ask Eva AI for DMs` widget block, followed by a Kommo Message step using `{{json.message}}`. Set this Salesbot ID as `KOMMO_SALESBOT_ID`.
2. Comment Salesbot: configure Kommo's native `When a comment is received` trigger, add the `Ask Eva AI for Instagram comments` widget block, followed by a Kommo Comment step using `{{json.message}}`. Do not set a backend Salesbot ID for this flow.

The backend never launches the comment Salesbot through `/api/v4/bots/{id}/run`. Authenticated Instagram-comment widget callbacks create durable `ready` jobs directly. Private-message callbacks must still match an existing `waiting_for_salesbot` job.

Kommo may also mirror a native Instagram comment through the general webhook as `origin=instagram_business` with `message_type=text`. That event is intentionally treated as a normal Instagram private-message job first. The authenticated native comment-triggered Salesbot callback creates the durable `instagram_comment` job, then reconciliation discards any recent matching private-message mirror before `KOMMO_SALESBOT_ID` can launch.

Public-comment replies are deterministic. Eva answers only price or availability, and only when the widget callback provides post/product context that maps confidently to one catalog product. Greetings, sizing, recommendations, payment, delivery, ordering, comparisons, complaints, unknown products, and ambiguous post context return `Para más información escríbenos al DM o por WhatsApp al {store_phone_number}!`; if `store_phone_number` is empty, the reply is `Para más información escríbenos al DM!`.

## Private Integration Creation

Create a private integration in Kommo under Settings -> Integrations. Leave OAuth redirect fields empty if using a long-lived token. Save the Integration ID and Secret Key.

## Permissions

Grant only the scopes needed for this integration:

- Leads read/write.
- Contacts read.
- Users read when `KOMMO_DEFAULT_RESPONSIBLE_USER_ID` is used.
- Notes write for escalation notes.
- Salesbot/bot execution access according to Kommo permissions.

## Long-Lived Token Creation

Generate a long-lived token from the private integration's Keys and scopes tab. Store it as `KOMMO_ACCESS_TOKEN`. Kommo shows the token once; do not commit it.

## AI Mode Field Creation

Create a lead select/radio field named `AI Mode` with values:

- `AI Active`
- `Human`
- `Paused`

The app maps these to local states:

- `AI Active` -> `active`
- `Human` -> `escalated`
- `Paused` -> `escalated`

## Retrieving Field And Enum IDs

Use Kommo's lead custom fields API or the Kommo UI/network inspector to retrieve:

- `KOMMO_AI_MODE_FIELD_ID`
- `KOMMO_AI_ACTIVE_ENUM_ID`
- `KOMMO_AI_HUMAN_ENUM_ID`
- `KOMMO_AI_PAUSED_ENUM_ID`

The diagnostics test endpoint verifies the configured field and enum IDs with read-only API calls.

## Responsible User ID

Retrieve a Kommo user ID from Kommo's users API or UI. Set `KOMMO_DEFAULT_RESPONSIBLE_USER_ID` only if AI-initiated escalations should assign that user.

## Environment Variables

Final Kommo-specific contract:

```env
CHANNEL_BACKEND=meta|kommo
KOMMO_SUBDOMAIN=
KOMMO_ACCESS_TOKEN=
KOMMO_INTEGRATION_ID=
KOMMO_INTEGRATION_SECRET=
KOMMO_SALESBOT_ID=
KOMMO_WEBHOOK_SECRET=
KOMMO_AI_MODE_FIELD_ID=
KOMMO_AI_ACTIVE_ENUM_ID=
KOMMO_AI_HUMAN_ENUM_ID=
KOMMO_AI_PAUSED_ENUM_ID=
KOMMO_DEFAULT_RESPONSIBLE_USER_ID=
```

`KOMMO_DEFAULT_RESPONSIBLE_USER_ID` is optional. Do not add `KOMMO_ACCOUNT_ID`, `KOMMO_RETURN_URL_ALLOWLIST`, `KOMMO_AUTO_TAKEOVER_ON_HUMAN_REPLY`, or `KOMMO_REQUEST_TIMEOUT_SECONDS`.

## Database Migration

Run `store/migrations/001_schema.sql` manually in Supabase SQL Editor. The consolidated schema includes `customer_channel_mappings`, `kommo_message_jobs`, `kommo_message_receipts`, `interaction_type` persistence for private messages versus public comments, callback claim storage, continuation tracking, `delivery_unknown`, assistant-history idempotency, and lead-safe contact mappings.

## Widget Build

Create or open the private Kommo integration and copy the Widget code first.

```bash
cd store/kommo-widget
python3 build_widget.py --widget-code YOUR_WIDGET_CODE
```

Use the real widget code shown by the private Kommo integration. The source `manifest.json` keeps `__WIDGET_CODE__`; the builder substitutes the real value only inside the ZIP manifest and validates the installable manifest, i18n keys, PNG assets, widget version, and obvious secret markers. The build creates `store/kommo-widget/social-media-manager-kommo-widget.zip` with `manifest.json` at the archive root.

The widget version must be incremented on every upload. Current version: `1.2.7`.

## Widget Installation

1. Create or open the private Kommo integration.
2. Obtain the Widget code.
3. Build with `python3 build_widget.py --widget-code YOUR_WIDGET_CODE`.
4. Upload `social-media-manager-kommo-widget.zip` to the private integration.
5. Save the integration.
6. Return to Settings -> Integrations.
7. Open the Social Media Manager widget.
8. Enter `https://YOUR-STORE-DOMAIN/webhooks/kommo/salesbot` in `backend_url`.
9. Enable/install it and save the settings.
10. Disable and re-enable the integration, or refresh Kommo after uploading a new widget version.
11. Hard refresh the browser if Salesbot still shows stale widget fields.
12. Open Salesbot.
13. Add a Widget step.
14. Select Social Media Manager AI from the installed widget list.
15. Leave the block-level `Salesbot callback URL override` empty unless this block must call a different store backend.

The widget must be installed from Settings -> Integrations before it is expected to appear as an installed widget in Salesbot. The manifest intentionally uses `installation=true`, top-level `settings.backend_url`, and both `settings` and `salesbot_designer` locations. The top-level `backend_url` is required and is configured once in the integration settings. The block-level `webhook_url` is optional and only overrides the global URL when it is valid.

If invalid manifests were previously uploaded first and Kommo continues using stale metadata, create a fresh private integration or regenerate the Widget code/key before uploading the corrected archive, following Kommo's widget update behavior.

## Salesbot Creation

Create a private-message Salesbot that contains the installed `Ask Eva AI for DMs` widget step. The integration settings `backend_url` is used automatically, so do not enter the same URL twice. If needed for a per-block override, set the block URL as:

```text
https://<store-domain>/webhooks/kommo/salesbot
```

The widget sends `{{message_text}}`, `{{lead.id}}`, `{{contact.id}}`, `{{origin}}`, and `interaction_type`. Its saved Salesbot source must use `widget_request` followed by `goto` question step `1`, so the bot waits for this backend to call the validated continuation URL. If the block URL is empty, the widget uses the installed account-level `backend_url`. The widget exposes `success` and `fail` branches; use `success` for normal AI completion and `fail` for fallback/human handling. The continuation response remains `{"data":{"status":"success","message":"..."}}`.

For public comments, create a separate Kommo Salesbot using the native `When a comment is received` trigger and the installed `Ask Eva AI for Instagram comments` widget block. End that flow with a Kommo Comment step using `{{json.message}}`. The backend validates the widget JWT and creates the durable comment job from the callback, so no comment Salesbot ID is configured in this app.

## Salesbot ID Retrieval

Retrieve the private-message Salesbot ID from Kommo's Salesbot UI or API and set it as `KOMMO_SALESBOT_ID`. Do not configure a public-comment Salesbot ID in the backend.

## General Webhook Registration

Register a Kommo general webhook URL:

```text
https://<store-domain>/webhooks/kommo/events/<KOMMO_WEBHOOK_SECRET>
```

Subscribe at least to:

- Incoming message received.
- Outgoing message sent.
- Lead edited.
- Talk added.
- Talk edited.

The path secret is compared with constant-time comparison. Do not reuse production secrets in screenshots or docs.

## Railway Configuration

Set the store Railway service root directory to `store/`. Set store service variables in Railway directly or through the master dashboard credential manager. Do not deploy automatically from this guide. Redeploy only after verifying all variables and migration state.

When using the master dashboard to deploy credentials, store all Kommo variables as normal encrypted store credentials. The master does not interpret the values; it pushes them to Railway and triggers the store redeploy.

## Kommo Mode Activation

1. Run the consolidated `store/migrations/001_schema.sql`.
2. Upload the widget.
3. Create and test the private-message Salesbot and native comment-triggered Salesbot.
4. Register the general webhook.
5. Set all Kommo env vars.
6. Set `CHANNEL_BACKEND=kommo`.
7. Redeploy manually when ready.

## Production Go-Live Checklist

```text
[ ] Store Railway root directory is store/
[ ] 001_schema.sql has already been run
[ ] CHANNEL_BACKEND=kommo is set in the store environment
[ ] All required KOMMO_* variables are set
[ ] KOMMO_SUBDOMAIN is only the subdomain, not a full URL
[ ] Widget ZIP uploaded to the private integration
[ ] Widget installed from Settings -> Integrations with backend_url=https://<store-domain>/webhooks/kommo/salesbot
[ ] Social Media Manager AI appears in Salesbot as an installed widget
[ ] Private-message Salesbot ends with a Message step using {{json.message}}
[ ] Comment Salesbot ends with a Comment step using {{json.message}}
[ ] General webhook points to https://<store-domain>/webhooks/kommo/events/<secret>
[ ] /admin/settings/kommo/test passes with admin auth
[ ] A real WhatsApp or Instagram DM produces one customer reply through Kommo
[ ] A real Instagram comment triggers only the native comment Salesbot flow; the general webhook logs it as ignored
[ ] AI Mode=Human suppresses future AI replies
```

## WhatsApp Test Procedure

1. Send a WhatsApp message to the Kommo-connected number.
2. Confirm it appears in Kommo inbox.
3. Confirm `/admin/settings/kommo/status` shows a recent incoming webhook and pending/processed job movement.
4. Confirm a Salesbot run is launched and the customer receives the AI text response.

## Instagram DM Test Procedure

1. Send an Instagram DM to the connected account.
2. Confirm origin maps to `instagram`.
3. Confirm AI response appears through Kommo, not Meta sender modules.

## Instagram Comment Test Procedure

1. Create a public Instagram comment.
2. Confirm the native comment-triggered Salesbot calls the widget and the public reply comes from the Kommo Comment step using `{{json.message}}`.
3. Confirm the general webhook may create a short-lived Instagram `private_message` mirror when Kommo sends `origin=instagram_business`, `message_type=text`.
4. Confirm the authenticated native comment callback creates the `instagram_comment` job and the mirrored private-message job is discarded with `superseded_by_instagram_comment` before `KOMMO_SALESBOT_ID` launches.

## Human Takeover Procedure

Set the lead `AI Mode` field to `Human` before replying manually. The app syncs local `conversation_state` to `escalated` and suppresses future AI replies.

## AI Resume Procedure

Set `AI Mode` back to `AI Active`. The next inbound customer message may be handled by AI. The app does not automatically send a reply merely because the field changed.

If you reactivate from the store dashboard instead, the backend first sets the Kommo lead `AI Mode` to `AI Active`, re-reads the lead, verifies the enum, and only then sets local `conversation_state` to `active` and clears local conversation history. If Kommo cannot confirm the update, single-customer reactivation returns `502` and the customer stays locally escalated. Bulk reactivation reports each customer independently as `activated`, `local_only`, or `failed`.

## Race-Condition Test

During a slow AI generation, switch `AI Mode` to `Human`. The job rechecks state before delivery and discards non-escalation AI output if the lead is no longer active.

## AI-Initiated Escalation

The existing `escalate_to_human` tool now also attempts to:

- Set local state to `escalated`.
- Set Kommo `AI Mode` to `Human`.
- Assign `KOMMO_DEFAULT_RESPONSIBLE_USER_ID` when configured.
- Add an internal Kommo note with reason, urgency, and summary.
- Preserve Telegram notification.
- Send one final customer handoff response.

## Rich-Media Limitations

The first release is text-first. Product images become caption plus public image URL when the Kommo channel cannot carry native rich media. Catalog PDF delivery is intentionally not offered through Kommo: `send_catalog_pdf` is excluded from Kommo tool availability, and catalog requests are answered as normal text from the loaded catalog. Unsupported buttons degrade to numbered text choices.

Kommo Salesbot continuations are data-only payloads shaped as `{"data":{"status":"success","message":"..."}}` or `{"data":{"status":"fail","message":""}}`. They do not include `execute_handlers`, `attachment_type`, or public catalog PDF URLs.

Emoji and markdown formatting are normalized before Kommo continuation. Per-channel settings `kommo_emoji_mode_whatsapp` and `kommo_emoji_mode_instagram` accept `preserve`, `safe`, or `strip`; the default is `safe`. The legacy `kommo_strip_emoji=true` setting still forces stripping.

For `interaction_type=instagram_comment`, final text is additionally collapsed to one short public-safe message before continuation.

## Payment-Image Limitations

If Kommo provides a direct HTTPS image URL, the existing vision flow can attempt a safe download only when the URL host is a trusted Meta/Instagram/Kommo host suffix. The downloader rejects userinfo, custom ports, non-HTTPS URLs, IP literals, localhost, redirects, non-image content types, and files over 5 MB. If Kommo provides only an inaccessible media identifier or an untrusted URL, the app stores the message and does not treat it as downloadable media. Real Kommo image payloads still require production validation.

Current trusted direct-media host suffixes are:

- `amocrm.com`
- `cdninstagram.com`
- `facebook.com`
- `fbcdn.net`
- `fbsbx.com`
- `instagram.com`
- `kommo.com`

## Broadcast Limitation

Direct WhatsApp broadcast delivery is rejected when `CHANNEL_BACKEND=kommo`:

```text
WhatsApp broadcast delivery is unavailable while CHANNEL_BACKEND=kommo. Use Kommo broadcasts or an approved Kommo WhatsApp template flow.
```

Existing broadcast records, previews, history, and Meta-mode delivery behavior remain.

## Diagnostics

Authenticated endpoints:

- `GET /admin/settings/kommo/status`
- `POST /admin/settings/kommo/test`

They return booleans, timestamps, counts, and sanitized errors only. They do not return tokens, secrets, JWTs, phone numbers, full messages, or raw payloads.

## Delivery-Unknown Reconciliation

Jobs marked `delivery_unknown` mean the backend started a Salesbot continuation but could not confirm whether Kommo accepted it, usually because of a timeout, transient 5xx/429 response, or process interruption while status was `continuing`. Do not blindly retry these jobs. First check the Kommo lead/chat to see whether the customer already received the message, then either leave the job as an audit record or reconcile manually with a one-off human reply.

## Troubleshooting

- `401` on Salesbot callback: verify JWT secret, subdomain, Integration ID, and expiration.
- `return_url` rejected: ensure it is `https://{KOMMO_SUBDOMAIN}.kommo.com/...` with no userinfo or custom port.
- Jobs stuck in `waiting_for_salesbot`: the backend marks stale waits as failed after about 3 minutes so new inbound messages can retry. Verify Salesbot widget URL and webhook reachability if this repeats.
- Jobs in `delivery_unknown`: manually inspect the Kommo conversation before retrying or sending a replacement reply.
- Jobs failed after AI Mode initialization: verify field and enum IDs.
- No automatic reply: check global `ai_enabled`, local customer state, Kommo `AI Mode`, and job diagnostics.

## Rollback To Meta Mode

1. Set `CHANNEL_BACKEND=meta`.
2. Restore valid Meta WhatsApp credentials.
3. Ensure Meta webhook routes are registered in Meta Developers.
4. Redeploy manually.
5. Kommo tables can remain in the database; they are non-destructive and ignored in Meta mode.

Rollback does not delete Kommo jobs, mappings, customers, orders, conversations, analytics, or broadcasts.
