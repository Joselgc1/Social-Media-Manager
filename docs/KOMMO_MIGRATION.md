# Kommo Migration

## Architecture Overview

`CHANNEL_BACKEND=meta` keeps the current direct Meta webhook and sender flow. `CHANNEL_BACKEND=kommo` uses Kommo official WhatsApp and Instagram integrations as the channel provider and shared inbox, while this app remains the AI, CRM, catalog, order, analytics, escalation, Telegram, and multi-store backend.

```text
WhatsApp text
-> existing Salesbot

WhatsApp product image
-> Kommo Files API/cache
-> Chats API, text + attachment
-> Salesbot media branch finishes silently

WhatsApp catalog PDF
-> generated PDF
-> Kommo Files API/cache
-> Chats API, text + attachment
-> Salesbot media branch finishes silently

Instagram DM
-> durable job with required talk_id
-> optional Story/context correlation
-> AI response
-> Talks/Chats API text or enabled product image

Instagram public comment
-> existing native comment Salesbot
```

Kommo owns the WhatsApp and Instagram channel connection. The app does not create a custom Kommo Chats API channel and does not call Meta sender modules in Kommo mode.

## Why Kommo

Kommo removes the need to manage Meta Developers app review, long-lived Meta access tokens, WhatsApp Cloud API setup, Instagram Messaging API permissions, and webhook subscriptions directly. WhatsApp text replies use Salesbot. Instagram DM replies use the originating Kommo `talk_id` and Talks/Chats API. Enabled product images use the Files API/cache plus Chats API on both channels; catalog PDFs remain WhatsApp-only.

## Backend Modes

`CHANNEL_BACKEND=meta`:

- Registers `/webhooks/whatsapp` and `/webhooks/instagram`.
- Requires production Meta WhatsApp credentials.
- Sends replies and broadcasts through Meta Graph APIs.

`CHANNEL_BACKEND=kommo`:

- Registers `/webhooks/kommo/events/{webhook_secret}` and `/webhooks/kommo/salesbot`.
- Does not require Meta credentials.
- Launches and resumes the configured Salesbot for WhatsApp private messages.
- Requires `talk_id` for Instagram private messages and delivers them directly through Talks/Chats API, with no Salesbot fallback.
- Uses Files API/cache plus Chats API for enabled WhatsApp/Instagram product images; catalog PDFs remain WhatsApp-only.
- Rejects direct WhatsApp broadcast delivery.

## Kommo Plan Prerequisites

Kommo private widgets and WebSDK/Salesbot widget usage require a Kommo plan that supports custom widgets and webhooks. Confirm the current plan in Kommo before production setup.

## WhatsApp Coexistence Setup

Connect WhatsApp Business inside Kommo using Kommo's official WhatsApp integration. If WhatsApp Coexistence is available for the account, configure it in Kommo according to Kommo's current UI. Do not connect this backend directly to WhatsApp Cloud API in Kommo mode.

## Instagram Business Setup

Connect the Instagram Business account inside Kommo using Kommo's official Instagram integration. Confirm DMs arrive in the Kommo inbox before enabling this app's Kommo mode.

## Instagram Comment Setup

Public Instagram comments use the same durable job system as private messages:

```text
Kommo native comment trigger -> widget callback -> create ready or context-waiting kommo_message_job -> AI response -> continue Salesbot with json.message
```

Private messages use channel-specific durable paths:

```text
Instagram DM webhook -> create kommo_message_job with talk_id -> optional context wait -> AI response -> direct Talks/Chats API send
WhatsApp webhook -> create kommo_message_job -> launch KOMMO_WHATSAPP_SALESBOT_ID -> widget callback -> AI response -> continue Salesbot with json.message
```

Create two Salesbot flows that use the installed Social Media Manager widget:

1. WhatsApp Salesbot: add `Ask Eva AI for WhatsApp`, followed by a Kommo Message step using `{{json.message}}` restricted to the WhatsApp channel. Do not add a native incoming-message trigger. Set its ID as `KOMMO_WHATSAPP_SALESBOT_ID`.
2. Instagram comment Salesbot: keep Kommo's native `When a comment is received` trigger, add `Ask Eva AI for Instagram comments`, followed by a Kommo Comment step using `{{json.message}}`. Do not configure this Salesbot's ID in the backend.

The backend never launches the comment Salesbot through `/api/v4/bots/{id}/run`. Authenticated Instagram-comment widget callbacks create durable `ready` jobs, or `waiting_for_context` jobs when supplemental Meta context is enabled. WhatsApp private-message callbacks must match an existing `waiting_for_salesbot` job. Instagram private messages never call the Salesbot callback endpoint.

Kommo may also mirror a native Instagram comment through the general webhook as `origin=instagram_business` with `message_type=text`. That event is intentionally treated as a normal Instagram private-message job first. The authenticated native comment-triggered Salesbot callback creates the durable `instagram_comment` job, then reconciliation discards any recent matching private-message mirror before direct Instagram processing.

Public-comment replies are deterministic. Eva answers only price or availability. A single mapped product can be answered directly; with multiple mapped products, a generic question requests clarification and a confident explicit product reference can select one product. Greetings, sizing, recommendations, payment, delivery, ordering, comparisons, complaints, unknown products, and unresolved context return `Para más información escríbenos al DM o por WhatsApp al {store_phone_number}!`; if `store_phone_number` is empty, the reply is `Para más información escríbenos al DM!`.

## Private Integration Creation

Create a private integration in Kommo under Settings -> Integrations. Leave OAuth redirect fields empty if using a long-lived token. Save the Integration ID and Secret Key.

## Permissions

Grant only the scopes needed for this integration:

- Leads read/write.
- Contacts read.
- Users read when `KOMMO_DEFAULT_RESPONSIBLE_USER_ID` is used.
- Notes write for escalation notes.
- Salesbot/bot execution access according to Kommo permissions.
- `Sending to external chats` for every direct Instagram private-message reply and for opted-in Chats API media delivery.
- `Access to files` for image and PDF uploads to Kommo Drive.

`Sending to external chats` is mandatory even when Instagram product images are disabled because every Instagram DM text reply uses `POST /api/v4/talks/{talk_id}/send_message`. After adding this scope, an existing private integration authorization may need to be granted access again in Kommo. Verify the grant manually before production rollout: the read-only `/admin/settings/kommo/test` endpoint cannot prove send permission without sending a real message. A `403` from Kommo is terminal for that attempt and must not fall back to Salesbot; direct Instagram intentionally has no Salesbot fallback.

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

`KOMMO_WHATSAPP_SALESBOT_ID` selects the WhatsApp Salesbot. The backend temporarily falls back to `KOMMO_SALESBOT_ID` only for WhatsApp. Instagram DMs intentionally have no Salesbot ID or fallback. `KOMMO_DEFAULT_RESPONSIBLE_USER_ID` is optional. Do not add `KOMMO_ACCOUNT_ID`, `KOMMO_RETURN_URL_ALLOWLIST`, `KOMMO_AUTO_TAKEOVER_ON_HUMAN_REPLY`, or `KOMMO_REQUEST_TIMEOUT_SECONDS`.

`KOMMO_CHATS_MEDIA_ENABLED` is the global kill switch. Product images on WhatsApp or Instagram additionally require `KOMMO_CHATS_PRODUCT_IMAGES_ENABLED=true`. PDFs are selected only for WhatsApp and additionally require `KOMMO_CHATS_CATALOG_PDF_ENABLED=true` plus `KOMMO_CHATS_PDF_ATTACHMENT_TYPE=file`. Instagram catalog requests are redirected toward WhatsApp and never upload or send a PDF. All media flags default to `false`. `KOMMO_CHATS_API_MONTHLY_LIMIT` is an optional positive integer used only for local monitoring; Kommo remains authoritative for billing and quota.

## Database Migration

Run `python3 store/scripts/migrate.py` through Railway pre-deploy or against the intended database. The current Store schema version is `16`. The runner applies the fresh baseline and every normal numbered migration through `016_kommo_waiting_for_delivery.sql`; `002_consolidated_upgrade.sql` is a manual pre-consolidation recovery migration and is not part of the normal runner. Migrations 009-012 add outbound media history, uniqueness, cache, and content hashes; 013-014 add inbound voice type and ordered inbound attachment metadata; 015 isolates private-message and Instagram-comment history; 016 adds `waiting_for_delivery`, delivery reconciliation scheduling, and pending assistant-history persistence for direct Instagram sends. Let `store/app/db.py` validate the complete migration set rather than checking only `MAX(version)`.

## Widget Build

Create or open the private Kommo integration and copy the Widget code first.

```bash
cd store/kommo-widget
python3 build_widget.py --widget-code YOUR_WIDGET_CODE
```

Use the real widget code shown by the private Kommo integration. The source `manifest.json` keeps `__WIDGET_CODE__`; the builder substitutes the real value only inside the ZIP manifest and validates the installable manifest, i18n keys, PNG assets, widget version, and obvious secret markers. The build creates `store/kommo-widget/social-media-manager-kommo-widget.zip` with `manifest.json` at the archive root.

The widget version must be incremented on every upload. [`store/kommo-widget/manifest.json`](../store/kommo-widget/manifest.json) is authoritative and currently specifies `1.2.15`.

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

Create the WhatsApp private-message Salesbot containing `Ask Eva AI for WhatsApp`. It should not have a native incoming-message trigger because the backend launches it after durable webhook processing. The integration settings `backend_url` is used automatically, so do not enter the same URL twice. If needed for a per-block override, set the block URL as:

```text
https://<store-domain>/webhooks/kommo/salesbot
```

The WhatsApp block sends `{{message_text}}`, `{{lead.id}}`, `{{contact.id}}`, `{{origin}}`, `interaction_type`, and `expected_channel=whatsapp`. Its saved source must use `widget_request` followed by `goto` question step `1`, so the bot waits for this backend to call the validated continuation URL. If the block URL is empty, the widget uses the installed account-level `backend_url`. Route `success` to the WhatsApp Message step, `media` to a silent end because Chats API already delivered the response, and `fail` to a silent end or explicit human handling without a customer-visible `{{json.message}}` step.

For public comments, create a separate Kommo Salesbot using the native `When a comment is received` trigger and the installed `Ask Eva AI for Instagram comments` widget block. End that flow with a Kommo Comment step using `{{json.message}}`. The backend validates the widget JWT and creates the durable comment job from the callback, so no comment Salesbot ID is configured in this app.

## Salesbot ID Retrieval

Retrieve the WhatsApp Salesbot ID from Kommo's Salesbot UI or API and set it as `KOMMO_WHATSAPP_SALESBOT_ID`. During migration, `KOMMO_SALESBOT_ID` may remain as a WhatsApp-only fallback. Do not configure backend Salesbot IDs for Instagram DMs or public comments.

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

1. Run `python3 store/scripts/migrate.py`. Use `002_consolidated_upgrade.sql` only when a documented pre-consolidation schema error requires recovery, then run the normal migration runner again.
2. Upload the widget.
3. Create and test the WhatsApp Salesbot and native comment-triggered Salesbot; verify direct Instagram DM delivery by `talk_id`.
4. Register the general webhook.
5. Set all Kommo env vars.
6. Set `CHANNEL_BACKEND=kommo`.
7. Redeploy manually when ready.

## Production Go-Live Checklist

```text
[ ] Store Railway root directory is store/
[ ] python3 store/scripts/migrate.py completed and store/app/db.py accepts the complete migration set through version 16
[ ] CHANNEL_BACKEND=kommo is set in the store environment
[ ] All required KOMMO_* variables are set
[ ] KOMMO_SUBDOMAIN is only the subdomain, not a full URL
[ ] Widget ZIP uploaded to the private integration
[ ] Widget installed from Settings -> Integrations with backend_url=https://<store-domain>/webhooks/kommo/salesbot
[ ] Social Media Manager AI appears in Salesbot as an installed widget
[ ] WhatsApp Salesbot has no native trigger and ends with a WhatsApp-only Message step using {{json.message}}
[ ] WhatsApp Salesbot routes the widget media exit to a silent end
[ ] Instagram DMs contain a valid talk_id and receive direct Chats API replies without Salesbot
[ ] Comment Salesbot ends with a Comment step using {{json.message}}
[ ] General webhook points to https://<store-domain>/webhooks/kommo/events/<secret>
[ ] /admin/settings/kommo/test returns ok=true for passing automatic checks and readiness_status=manual_verification_required while the mandatory sending scope remains manual/unverified
[ ] A real WhatsApp or Instagram DM produces one customer reply through Kommo
[ ] A real Instagram comment creates the authoritative native-comment job; any general-webhook private-message mirror is discarded as superseded before direct Instagram processing
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
3. Confirm the durable job persists the originating `talk_id`.
4. Confirm AI text or an enabled product image is sent through `/api/v4/talks/{talk_id}/send_message`, without `run_salesbot()` or `continue_salesbot()`.
5. Confirm catalog requests redirect toward WhatsApp and no PDF is uploaded or sent.

## Inbound Voice Notes

Kommo private WhatsApp and Instagram DM `voice`/`audio` attachments are persisted in arrival order, safely downloaded, and transcribed before the AI turn. Multiple debounced notes are transcribed in order. Mixed image/audio batches keep the latest image for vision while preserving the ordered audio transcriptions.

Transcription always uses OpenAI `gpt-4o-mini-transcribe`, so `OPENAI_API_KEY` is required even if Anthropic is the active chat provider. There is no separate voice feature flag. Downloads are HTTPS-only, DNS/IP validated, limited to three redirects and 20 MiB, and accept supported OGG/Opus, MP3, MP4/M4A, WAV, and WebM audio. Retryable failures remain in the durable job retry path; terminal failures continue through the widget `fail` exit without an AI reply. Direct Meta audio is not transcribed.

## Instagram Comment Test Procedure

1. Create a public Instagram comment.
2. Confirm the native comment-triggered Salesbot calls the widget and the public reply comes from the Kommo Comment step using `{{json.message}}`.
3. Confirm the general webhook may create a short-lived Instagram `private_message` mirror when Kommo sends `origin=instagram_business`, `message_type=text`.
4. Confirm the authenticated native comment callback creates the `instagram_comment` job and the mirrored private-message job is discarded with `superseded_by_instagram_comment` before direct Instagram processing.

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

## Hybrid Media Delivery

Text-only WhatsApp responses remain on Salesbot. Instagram private-message text always uses `POST /api/v4/talks/{talk_id}/send_message`; enabled Instagram product images use the same request with text plus one picture attachment. A WhatsApp response containing an opted-in product image or catalog PDF uses Chats API for the whole response, including accompanying text. A WhatsApp image plus PDF produces two requests because Kommo accepts one attachment per request; customer text is included only on the first request. PDF delivery remains strictly WhatsApp-only. Unsupported buttons degrade to numbered text choices.

The Files API upload/cache lifecycle is separate from outgoing Chats API message usage. The metered operation is `POST /api/v4/talks/{talk_id}/send_message`; ordinary Salesbot text replies do not consume that outgoing Chats API pool.

Only incoming external Kommo messages create AI jobs. Outgoing `add_outgoing_message` webhooks only reconcile an existing outbound delivery when the provider message ID matches; they never trigger Eva or create another `kommo_message_job`.

### Direct Instagram Delivery Reconciliation

An HTTP `202 Accepted` response for a direct Instagram send is not treated as final customer delivery. The durable job enters `waiting_for_delivery`, stores the current provider message IDs and pending assistant-history payload, and becomes eligible for reconciliation immediately. The scheduler polls Kommo every 15 seconds; a matching outgoing-message webhook only advances the next reconciliation time and never confirms delivery by itself.

- `delivered` or `seen` confirms the outbound record, persists the assistant turn once, and finalizes the job.
- `sent`, a missing status, or a temporary lookup failure leaves the job pending without resending.
- `error` is definitive. A failed requested product image may queue one direct text-only Instagram fallback that explains the issue and links to WhatsApp; this is not a Salesbot fallback.
- `delivery_unknown` means Kommo may have accepted a send whose outcome cannot be established. Inspect the Kommo conversation before any manual replacement and never retry it automatically.

Queue monitoring must include `waiting_for_delivery`. The pending assistant payload is intentionally retained until confirmation so conversation history never claims an undelivered image was sent.

Conversation history stores one logical assistant turn regardless of transport. `conversations.attachments` contains only semantic product-image or catalog-PDF context. Kommo Drive UUIDs, provider message IDs, request fingerprints, and raw provider payloads stay in Kommo delivery/cache records and are never supplied to the LLM. Signed inbound attachment URLs are stored temporarily in the durable job's `inbound_attachments` metadata so audio/images survive debounce and retries.

Kommo Salesbot continuations are data-only payloads. WhatsApp Salesbot delivery uses `{"data":{"status":"success","delivery_mode":"salesbot","message":"..."}}`; after WhatsApp media is delivered through Chats API, its Salesbot receives `{"data":{"status":"success","delivery_mode":"chats_api","message":""}}`. Failures use `{"data":{"status":"fail","message":""}}`. Direct Instagram jobs never create a continuation or require `return_url`.

Emoji and markdown formatting are normalized before Kommo delivery. Per-channel settings `kommo_emoji_mode_whatsapp` and `kommo_emoji_mode_instagram` accept `preserve`, `safe`, or `strip`; the default is `safe`. The legacy `kommo_strip_emoji=true` setting still forces stripping.

For `interaction_type=instagram_comment`, final text is additionally collapsed to one short public-safe message before continuation.

## Kommo Media Transport

The Files and Chats transport supports enabled product images for WhatsApp and Instagram private messages. WhatsApp text-only and feature-disabled media responses remain on Salesbot. Instagram always remains on direct Chats API text delivery when images are disabled. Catalog PDFs are selected only for WhatsApp.

The Kommo private integration requires these additional scopes before the transport can be exercised:

- `Access to files` for the Files API.
- `Sending to external chats` for all direct Instagram DM text and enabled Chats API media sends.

Adding a scope to an existing integration may require granting access again in Kommo. This authorization must be verified manually with a development conversation before production. If Kommo returns `403`, restore the missing scope; do not add or expect an Instagram Salesbot fallback.

Enable the global flag and each media-specific flag only after development-account validation. `KOMMO_CHATS_PDF_ATTACHMENT_TYPE` defaults to empty; PDF sending remains blocked until it is explicitly configured as `file`. The code and automated tests accept only `file`; verify a real PDF in the development account before production. Do not introduce undocumented attachment types.

The base flow follows Kommo's Files and Chats APIs:

1. Request `GET /api/v4/account?with=drive_url` and cache the returned Drive URL in memory.
2. Create a session with `POST {drive_url}/v1.0/sessions`, including file name, byte size, and MIME type.
3. Respect the returned `max_file_size` and `max_part_size`, then upload each chunk through the exact `upload_url` or `next_url` returned by Kommo.
4. Resolve the parent file UUID as Chats `drive_uuid` and the uploaded version UUID as `drive_version_uuid`. They must be valid and distinct. Depending on the response shape, the implementation resolves them from `file_uuid`/`uuid`, `_links.self`, and validated file metadata rather than assuming one fixed field layout.
5. Send an attachment, optionally with text, through `POST /api/v4/talks/{talk_id}/send_message`; `202 Accepted` is success.

### Live-Verified Contract (2026-08-04)

The following details were learned from successful manual tests against the Kommo development account, not solely from Kommo's published reference:

- Session creation returned `200 OK` with `max_file_size`, `max_part_size`, `session_id`, and an upload URL shaped as `https://drive-c.kommo.com/upload/<signed-token>`.
- File chunks were accepted as the raw binary request body with bearer authorization, `Accept: application/json`, and the original file MIME type as `Content-Type`. Multipart form data was not used.
- Every non-final chunk returned `202 Accepted` with `session_id` and a signed `next_url`.
- The final chunk returned `200 OK` with file metadata and distinct parent-file and uploaded-version identifiers. Field names vary across response shapes, so the implementation resolves their semantic roles before building the Chats attachment.
- Chats image attachment type `picture` delivered a native image successfully.
- One `POST /api/v4/talks/{talk_id}/send_message` request successfully carried both text and the image attachment and returned `202 Accepted` with a message `id`.

The implementation validates intermediate and final chunk statuses separately. It does not retry upload-session creation or chunk POSTs because an ambiguous retry could create a duplicate file or corrupt an upload session.

Inventory image downloads allow at most three manual redirects. Every initial and redirected hostname is resolved asynchronously and rejected if any resolved address is private, loopback, link-local, multicast, reserved, or unspecified. Each request is then pinned to the validated address while preserving the original HTTP Host and TLS SNI hostname, preventing a second DNS lookup from bypassing validation. URL schemes, ports, userinfo, response size, MIME type, and binary signatures are also validated at every applicable step.

Image attachment type `picture` is live-verified. WhatsApp PDF sending remains disabled unless the global flag, PDF-specific flag, and `KOMMO_CHATS_PDF_ATTACHMENT_TYPE=file` are all configured. Instagram never selects a PDF attachment. Files may still be uploaded independently for diagnostics without consuming an outgoing Chats API send.

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

For `POST /admin/settings/kommo/test`, `ok` and `automatic_checks_ok` describe only the checks the read-only endpoint can perform. `readiness_status=automatic_checks_failed` means one of those checks failed; `readiness_status=manual_verification_required` means they passed but `Sending to external chats` must still be verified manually. The endpoint never sends a customer message and never claims that scope was verified.

`GET /admin/settings/kommo/status` also reports current-calendar-month Chats API attempts, including separate text, product-image, and catalog-PDF request counts, final-state accepted/confirmed, failed, and `delivery_unknown` delivery counts, the configured monitoring allowance, estimated remaining requests, utilization percentage, and warning level. `attempted_requests` is the authoritative local total and includes text plus media attempts. Each claimed send increments transport-only attempt metadata; `sending` and `delivery_unknown` attempts count conservatively because the request may already have reached Kommo. Final-state delivery counts describe current durable records rather than every historical retry outcome. These values are local estimates from `kommo_outbound_deliveries`, not Kommo billing records, and do not enforce a hard quota. Direct manual calls such as `verify_kommo_media.py --send` do not have a durable job row and are not included in this local estimate; account for them separately and use Kommo as the billing source of truth.

Telegram `/kommo` exposes a concise read-only subset of these local diagnostics for phone-based operations. It does not send test messages, verify the external-chat permission automatically, or retry jobs.

## Staged Media Rollout

1. Run `python3 store/scripts/migrate.py` and confirm Store schema version 16 is accepted.
2. Deploy with `KOMMO_CHATS_MEDIA_ENABLED=false`, `KOMMO_CHATS_PRODUCT_IMAGES_ENABLED=false`, and `KOMMO_CHATS_CATALOG_PDF_ENABLED=false`.
3. Verify `POST /admin/settings/kommo/test` returns `ok=true`, `automatic_checks_ok=true`, and `readiness_status=manual_verification_required`; then complete its separate `manual_unverified` `Sending to external chats` check.
4. Run `KOMMO_CHATS_MEDIA_ENABLED=true python3 store/scripts/verify_kommo_media.py --talk-id DEVELOPMENT_TALK_ID --send` against a development talk. The command-scoped global override enables the low-level diagnostic while the deployed media-specific flags remain false; `--send` makes one real metered Chats API request.
5. Enable `KOMMO_CHATS_MEDIA_ENABLED=true` and `KOMMO_CHATS_PRODUCT_IMAGES_ENABLED=true`; leave PDF disabled.
6. Test an Instagram text-only reply and a real Instagram product-image reply end to end. Confirm both use the originating `talk_id` through Chats API and neither uses Salesbot. Recheck the private integration authorization if either returns `403`.
7. Verify conversation history, `kommo_outbound_deliveries`, and that outgoing webhook events created no duplicate AI jobs.
8. Set `KOMMO_CHATS_PDF_ATTACHMENT_TYPE=file` and enable `KOMMO_CHATS_CATALOG_PDF_ENABLED=true`.
9. Test a real PDF plus text delivery and confirm it uses one Chats API request.
10. Check `/admin/settings/kommo/status` quota diagnostics and manually reconcile any `delivery_unknown` rows before retrying or sending replacements.
11. Only after these checks pass, apply the same flags and validated attachment type in production.

## Delivery-Unknown Reconciliation

Jobs or outbound deliveries marked `delivery_unknown` mean the backend started a Salesbot continuation or Chats API send but could not confirm whether Kommo accepted it, usually because of a timeout, transient 5xx/429 response, persistence failure, or process interruption. Do not blindly retry these records. First check the Kommo lead/chat to see whether the customer already received the message, then either leave the record as an audit trail or reconcile manually with a one-off human reply.

## Media Rollback

Set `KOMMO_CHATS_MEDIA_ENABLED=false` and redeploy. Ordinary Salesbot text processing continues without reverting migrations. Leave all migrations through 16 and existing delivery/cache/job records in place for auditability and safe future re-enablement.

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
