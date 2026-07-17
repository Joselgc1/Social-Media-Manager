# Kommo Migration

## Architecture Overview

`CHANNEL_BACKEND=meta` keeps the current direct Meta webhook and sender flow. `CHANNEL_BACKEND=kommo` uses Kommo official WhatsApp and Instagram integrations as the channel provider and shared inbox, while this app remains the AI, CRM, catalog, order, analytics, escalation, Telegram, and multi-store backend.

```text
WhatsApp / Instagram -> Kommo inbox -> Kommo webhook -> Social-Media-Manager
Social-Media-Manager -> Kommo Salesbot widget continuation -> Customer
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
- Launches and resumes Kommo Salesbot for replies.
- Rejects direct WhatsApp broadcast delivery.

## Kommo Plan Prerequisites

Kommo private widgets and WebSDK/Salesbot widget usage require a Kommo plan that supports custom widgets and webhooks. Confirm the current plan in Kommo before production setup.

## WhatsApp Coexistence Setup

Connect WhatsApp Business inside Kommo using Kommo's official WhatsApp integration. If WhatsApp Coexistence is available for the account, configure it in Kommo according to Kommo's current UI. Do not connect this backend directly to WhatsApp Cloud API in Kommo mode.

## Instagram Business Setup

Connect the Instagram Business account inside Kommo using Kommo's official Instagram integration. Confirm DMs arrive in the Kommo inbox before enabling this app's Kommo mode.

## Instagram Comment Setup

Do not route public Instagram comments directly to this app in the first release. Configure native Kommo comment triggers manually:

1. Create a Kommo automation for new Instagram comments.
2. Send a controlled public template reply from Kommo.
3. Open or invite the user into a private Instagram DM through Kommo-supported behavior.
4. Let this app handle the resulting Instagram DM only.

Dynamic LLM-generated public comment replies are deliberately unsupported.

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

Run migrations manually in Supabase SQL Editor:

1. `store/migrations/001_schema.sql` for fresh databases.
2. `store/migrations/002_kommo_integration.sql` for Kommo tables and indexes.

`002_kommo_integration.sql` adds `customer_channel_mappings` and `kommo_message_jobs` without changing existing customer, order, conversation, usage, or broadcast rows.

## Widget Build

```bash
cd store/kommo-widget
python3 build_widget.py
```

The build creates `store/kommo-widget/social-media-manager-kommo-widget.zip` with `manifest.json` at the archive root.

## Widget Upload

1. Open the private Kommo integration.
2. Upload `social-media-manager-kommo-widget.zip`.
3. Confirm the widget is available in `salesbot_designer`.
4. Verify the widget placeholders in the real account before production.

## Salesbot Creation

Create a Salesbot that contains the uploaded widget step. Configure the widget URL as:

```text
https://<store-domain>/webhooks/kommo/salesbot
```

The widget sends `{{message_text}}`, `{{lead.id}}`, `{{contact.id}}`, `{{origin}}`, and `{{lead.responsible.id}}` where supported.

## Salesbot ID Retrieval

Retrieve the Salesbot ID from Kommo's Salesbot UI or API and set `KOMMO_SALESBOT_ID`.

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

Set store service variables in Railway directly or through the master dashboard credential manager. Do not deploy automatically from this guide. Redeploy only after verifying all variables and migration state.

## Kommo Mode Activation

1. Run `002_kommo_integration.sql`.
2. Upload the widget.
3. Create and test the Salesbot.
4. Register the general webhook.
5. Set all Kommo env vars.
6. Set `CHANNEL_BACKEND=kommo`.
7. Redeploy manually when ready.

## WhatsApp Test Procedure

1. Send a WhatsApp message to the Kommo-connected number.
2. Confirm it appears in Kommo inbox.
3. Confirm `/admin/settings/kommo/status` shows a recent incoming webhook and pending/processed job movement.
4. Confirm a Salesbot run is launched and the customer receives the AI text response.

## Instagram DM Test Procedure

1. Send an Instagram DM to the connected account.
2. Confirm origin maps to `instagram`.
3. Confirm AI response appears through Kommo, not Meta sender modules.

## Human Takeover Procedure

Set the lead `AI Mode` field to `Human` before replying manually. The app syncs local `conversation_state` to `escalated` and suppresses future AI replies.

## AI Resume Procedure

Set `AI Mode` back to `AI Active`. The next inbound customer message may be handled by AI. The app does not automatically send a reply merely because the field changed.

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

The first release is text-first. Product images become caption plus public image URL. Catalog PDFs become text plus the public `/static/catalog/catalog.pdf` URL. Unsupported buttons degrade to numbered text choices.

## Payment-Image Limitations

If Kommo provides a direct HTTPS image URL, the existing vision flow can attempt a safe download with timeout, content-type, redirect, and size limits. If Kommo provides only an inaccessible media identifier, the app stores the message and does not treat it as a URL. Real Kommo image payloads require production validation.

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

## Troubleshooting

- `401` on Salesbot callback: verify JWT secret, subdomain, Integration ID, and expiration.
- `return_url` rejected: ensure it is `https://{KOMMO_SUBDOMAIN}.kommo.com/...` with no userinfo or custom port.
- Jobs stuck in `waiting_for_salesbot`: verify Salesbot widget URL and webhook reachability.
- Jobs failed after AI Mode initialization: verify field and enum IDs.
- No automatic reply: check global `ai_enabled`, local customer state, Kommo `AI Mode`, and job diagnostics.

## Rollback To Meta Mode

1. Set `CHANNEL_BACKEND=meta`.
2. Restore valid Meta WhatsApp credentials.
3. Ensure Meta webhook routes are registered in Meta Developers.
4. Redeploy manually.
5. Kommo tables can remain in the database; they are non-destructive and ignored in Meta mode.

Rollback does not delete Kommo jobs, mappings, customers, orders, conversations, analytics, or broadcasts.
