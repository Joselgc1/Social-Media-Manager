# Kommo WhatsApp Migration

## Scope And Final Architecture

Kommo is a WhatsApp transport only. Instagram is always native Meta and is not connected to Kommo.

```text
WhatsApp customer
  -> Kommo official WhatsApp integration
  -> POST /webhooks/kommo/events/{KOMMO_WEBHOOK_SECRET}
  -> durable kommo_message_jobs row
  -> launch WhatsApp Salesbot
  -> POST /webhooks/kommo/salesbot (signed widget_request)
  -> AI engine
  -> Salesbot continuation for text, or Files API + Chats API for enabled media

Instagram customer or commenter
  -> Meta webhook
  -> durable meta_inbound_jobs row
  -> AI or deterministic comment handler
  -> Meta Graph API
```

`WHATSAPP_BACKEND=meta` keeps direct Meta WhatsApp delivery. `WHATSAPP_BACKEND=kommo` moves only WhatsApp to Kommo. There is no Instagram backend selector, Instagram Salesbot, comment mirror, Meta/Kommo event correlation, or context-only Meta listener in the final architecture.

Migration `018_retire_kommo_instagram.sql` retires live Instagram/Kommo processing and expires pending legacy records. It deliberately does not drop the old correlation tables or columns, preserving historical data and forward-only migration safety.

## Prerequisites

- A Kommo plan that supports private widgets, Salesbot, and webhooks.
- WhatsApp Business connected through Kommo's official integration. Configure WhatsApp Coexistence in Kommo if the account uses it.
- A Kommo private integration with a long-lived access token.
- A WhatsApp Salesbot containing the Social Media Manager widget.
- The production store remains single-instance because queue processing and scheduled work run in-process.

Do not connect Instagram to this Kommo integration. Follow [`store/INSTAGRAM DEPLOY.md`](../store/INSTAGRAM%20DEPLOY.md) for native Meta Instagram.

## Private Integration

Create a private integration under Kommo **Settings -> Integrations**. Save:

- Account subdomain, without scheme or `.kommo.com`.
- Long-lived access token.
- Integration ID/client UUID.
- Integration secret used to validate Salesbot JWTs.

Grant only the required permissions:

- Leads read/write.
- Contacts read.
- Salesbot/bot execution.
- Notes write for escalation notes.
- Users read only if `KOMMO_DEFAULT_RESPONSIBLE_USER_ID` is configured.
- `Access to files` and `Sending to external chats` only when Chats API media is enabled.

## AI Mode Field

Create a lead select/radio field named `AI Mode` with these values:

- `AI Active` maps to local `active`.
- `Human` maps to local `escalated`.
- `Paused` maps to local `escalated`.

Retrieve the field ID and enum IDs for the environment variables below. An empty AI Mode must be initialized to `AI Active` successfully before an automatic response is sent. The application never overwrites `Human` or `Paused` automatically.

## Environment Contract

```env
WHATSAPP_BACKEND=kommo
KOMMO_SUBDOMAIN=
KOMMO_ACCESS_TOKEN=
KOMMO_INTEGRATION_ID=
KOMMO_INTEGRATION_SECRET=
KOMMO_WHATSAPP_SALESBOT_ID=
KOMMO_SALESBOT_ID=
KOMMO_WEBHOOK_SECRET=
KOMMO_AI_MODE_FIELD_ID=
KOMMO_AI_ACTIVE_ENUM_ID=
KOMMO_AI_HUMAN_ENUM_ID=
KOMMO_AI_PAUSED_ENUM_ID=
KOMMO_DEFAULT_RESPONSIBLE_USER_ID=
KOMMO_CHATS_MEDIA_ENABLED=false
KOMMO_CHATS_PRODUCT_IMAGES_ENABLED=false
KOMMO_CHATS_CATALOG_PDF_ENABLED=false
KOMMO_CHATS_API_MONTHLY_LIMIT=
KOMMO_CHATS_PDF_ATTACHMENT_TYPE=
```

`KOMMO_WHATSAPP_SALESBOT_ID` is the preferred Salesbot ID. `KOMMO_SALESBOT_ID` is the legacy WhatsApp fallback for an existing deployment and should not be needed by a new installation. `KOMMO_DEFAULT_RESPONSIBLE_USER_ID` is optional.

Do not configure obsolete Instagram Kommo variables or use Kommo Instagram Salesbots. Do not add undocumented `KOMMO_ACCOUNT_ID`, `KOMMO_RETURN_URL_ALLOWLIST`, `KOMMO_AUTO_TAKEOVER_ON_HUMAN_REPLY`, or `KOMMO_REQUEST_TIMEOUT_SECONDS` variables.

All media flags default to `false`. Product images require the global and product-image flags. PDFs require the global and PDF flags plus `KOMMO_CHATS_PDF_ATTACHMENT_TYPE=file`. `KOMMO_CHATS_API_MONTHLY_LIMIT` is monitoring-only; Kommo remains authoritative for quota and billing.

## Database Migration

Run the normal migration runner through Railway pre-deploy or against the intended database:

```bash
python3 store/scripts/migrate.py
```

The current Store schema version is `18`, through `018_retire_kommo_instagram.sql`. Use `002_consolidated_upgrade.sql` only for a documented pre-consolidation recovery, then rerun the normal migration runner. Migration 018 deprecates but does not drop the legacy Instagram/Kommo correlation schema.

## Widget Build And Installation

Copy the Widget code from the private integration, then build the package:

```bash
cd store/kommo-widget
python3 build_widget.py --widget-code YOUR_WIDGET_CODE
```

Upload `social-media-manager-kommo-widget.zip` to the private integration. Install the widget and set its account-level `backend_url` to:

```text
https://<store-domain>/webhooks/kommo/salesbot
```

Leave the block-level URL empty unless that block intentionally targets another store backend. Increment `widget.version` for every upload. If Kommo shows stale fields, disable and re-enable the integration, refresh Kommo, and hard-refresh the browser. A bad initial upload can require a fresh widget code/private integration under Kommo's update behavior.

## WhatsApp Salesbot

Create one WhatsApp private-message Salesbot:

1. Add the `Ask Eva AI for WhatsApp` widget block.
2. Do not add a native incoming-message trigger; the backend launches one flow after durably committing the webhook event.
3. Route `success` to a WhatsApp-restricted Message step using `{{json.message}}`.
4. Route `media` to a silent end because Chats API already delivered the response.
5. Route `fail` to a silent end or explicit human handling without a customer-visible `{{json.message}}` step.

The widget source uses `widget_request` followed by `goto` question step `1`, allowing the backend to post a data-only continuation to the validated `return_url`. The backend checks that callbacks match a waiting WhatsApp job and rejects non-WhatsApp callbacks.

## General Webhook

Register:

```text
https://<store-domain>/webhooks/kommo/events/<KOMMO_WEBHOOK_SECRET>
```

Subscribe to:

- Incoming message received.
- Outgoing message sent.
- Lead edited.
- Talk added.
- Talk edited.

The path secret is compared in constant time. Only incoming external WhatsApp messages create AI jobs. Outgoing events reconcile known deliveries and never create another AI turn.

## Durable Processing And Delivery

PostgreSQL `kommo_message_jobs` is the source of truth. In-process tasks only accelerate processing. Jobs persist through restarts, use leases and stale-job recovery, and recheck AI Mode before delivery to avoid replying after human takeover.

Salesbot continuations are data-only:

```json
{"data":{"status":"success","delivery_mode":"salesbot","message":"..."}}
```

Chats API delivery returns an empty continuation message because the response was already sent. Failure continuations use `status=fail`. The callback JWT is validated with HS256 or HS512, expiration, issuer/subdomain, integration identity, and a strict `https://{KOMMO_SUBDOMAIN}.kommo.com` return URL policy.

`delivery_unknown` means a continuation or Chats API request may have reached Kommo but acceptance was not confirmed. Inspect the Kommo conversation before retrying or sending a replacement; never retry blindly.

## Voice Notes And Payment Images

WhatsApp `voice` and `audio` attachments are stored in arrival order and transcribed before the AI turn with OpenAI `gpt-4o-mini-transcribe`. This requires `OPENAI_API_KEY` even when Anthropic handles chat. Downloads are HTTPS-only, DNS/IP validated, redirect-limited, size-limited, and restricted to supported audio formats.

Payment-image analysis requires a direct trusted HTTPS image URL. The downloader rejects userinfo, custom ports, IP literals, localhost, redirects, invalid content types, and files over 5 MB. Some provider payloads expose only inaccessible media identifiers and therefore cannot be analyzed automatically; validate real payloads before production.

## Hybrid WhatsApp Media

Text-only replies use Salesbot. When enabled, a response containing a product image or catalog PDF uses Kommo Files API/cache plus Chats API for the entire response, including text. One attachment creates one `POST /api/v4/talks/{talk_id}/send_message`; image plus PDF requires two requests, with text only on the first. Unsupported interactive buttons degrade to numbered text choices.

The verified upload flow is:

1. Read and cache `drive_url` from `GET /api/v4/account?with=drive_url`.
2. Create an upload session with file name, byte size, and MIME type.
3. Respect `max_file_size` and `max_part_size`, posting raw binary chunks to each returned signed URL.
4. Resolve distinct parent-file and version UUIDs from the final metadata.
5. Send the attachment through the talk endpoint; `202 Accepted` is success.

Image attachment type `picture` is live-verified. PDF delivery remains blocked until all PDF flags are enabled with attachment type `file` and a real development-account test succeeds.

Conversation history stores one logical assistant turn. Semantic product-image/PDF context may enter `conversations.attachments`; provider IDs, Drive UUIDs, request fingerprints, and raw payloads remain transport metadata and are not supplied to the LLM.

## Media Rollout

1. Deploy with every media flag false.
2. Confirm `POST /admin/settings/kommo/test` passes.
3. Use `store/scripts/verify_kommo_media.py` against a development talk for one controlled real send.
4. Enable the global and product-image flags; test text and a real image end to end.
5. Inspect conversation history and `kommo_outbound_deliveries`; confirm outgoing webhooks created no AI job.
6. Set PDF attachment type to `file`, enable the PDF flag, and test one real PDF.
7. Reconcile every `delivery_unknown` row before production rollout.

To roll back media only, set `KOMMO_CHATS_MEDIA_ENABLED=false` and redeploy. Text continues through Salesbot. Do not revert migrations or delete delivery/cache/job records.

## Human Takeover And Resume

Set the lead `AI Mode` to `Human` before replying manually. The app syncs local state to `escalated` and suppresses future replies. Set it back to `AI Active` to permit the next inbound message; changing the field does not itself send a message.

Dashboard reactivation updates Kommo first, reads the lead back, verifies `AI Active`, and only then activates local state and clears WhatsApp history. If verification fails, the customer remains locally escalated. AI-initiated escalation also attempts to update AI Mode, assign the optional responsible user, add an internal note, and preserve Telegram notification.

## Broadcast Limitation

Direct WhatsApp broadcasts are rejected under `WHATSAPP_BACKEND=kommo`. Use Kommo broadcasts or an approved Kommo WhatsApp template flow. Existing draft, preview, and history records remain available.

## Diagnostics And Go-Live

Authenticated diagnostics:

- `GET /admin/settings/kommo/status`
- `POST /admin/settings/kommo/test`

They expose sanitized booleans, timestamps, counts, and errors, never secrets or raw customer payloads. Status also reports local Chats API attempt estimates; Kommo billing remains authoritative.

```text
[ ] Store migration runner accepts schema version 18
[ ] WHATSAPP_BACKEND=kommo
[ ] Required Kommo credentials and AI Mode IDs are set
[ ] KOMMO_SUBDOMAIN contains only the subdomain
[ ] Widget is installed with the correct backend_url
[ ] WhatsApp Salesbot has success/media/fail exits and no native trigger
[ ] General webhook points to /webhooks/kommo/events/<secret>
[ ] /admin/settings/kommo/test passes
[ ] A real WhatsApp message creates a durable Kommo job and one customer reply
[ ] AI Mode=Human suppresses AI
[ ] Instagram remains connected only to native Meta webhooks
```

## Troubleshooting

- `401` on Salesbot callback: verify integration secret, Integration ID, subdomain, JWT expiration, and widget URL.
- Rejected `return_url`: require the exact configured Kommo HTTPS origin with no userinfo or custom port.
- Job stuck in `waiting_for_salesbot`: stale waits fail after about three minutes; verify the WhatsApp Salesbot ID, widget installation, and callback reachability.
- `delivery_unknown`: inspect the Kommo conversation before any retry.
- AI Mode initialization failure: verify the field and enum IDs.
- No automatic reply: inspect global `ai_enabled`, local customer state, Kommo AI Mode, and sanitized job diagnostics.

## Rollback To Meta WhatsApp

1. Set `WHATSAPP_BACKEND=meta`.
2. Restore valid Meta WhatsApp credentials.
3. Register the native WhatsApp webhook in Meta Developers.
4. Redeploy manually.
5. Leave Kommo tables and historical jobs in place.

This rollback changes WhatsApp transport only. Instagram remains native Meta throughout.
