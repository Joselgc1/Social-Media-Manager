# VS Chatbot - AI Sales Assistant

AI-powered sales chatbot for Instagram DMs and WhatsApp, built for a Venezuelan
Victoria's Secret resale business. Supports both OpenAI and Anthropic as LLM
providers, with hot-swapping from the admin panel. Features a PDF product catalog,
global AI pause/resume, per-customer escalation, customer address memory,
admin tag management, and sortable dashboard tables.

## Quick start

```bash
# 1. Clone and install
git clone <your-repo-url>
cd vs-chatbot
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 2. Configure
cp .env.example .env
# Edit .env with your actual API keys and credentials

# 3. Set up the database
# Run all migrations (001 through 004) against your Supabase PostgreSQL instance

# 4. Run locally
uvicorn app.main:app --reload --port 8000

# 5. Expose for webhook testing (in another terminal)
# Use ngrok or similar to get a public HTTPS URL
ngrok http 8000
```

## Architecture

```
Customer (WhatsApp or Instagram DM)
    -> Webhook (FastAPI)
        -> Normalize message (text, buttons, images, ice breakers, postbacks)
        -> Conversation Router
            -> LLM Engine (OpenAI or Anthropic, admin-selectable)
                -> Tool Calls (inventory, tags, orders, escalation)
            -> Response
        -> Send reply via Meta API
            -> WhatsApp: text, interactive buttons, templates
            -> Instagram: text, quick replies, images, carousels
```

## Webhook endpoints

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/webhooks/whatsapp` | WhatsApp webhook verification |
| POST | `/webhooks/whatsapp` | Receive WhatsApp messages |
| GET | `/webhooks/instagram` | Instagram webhook verification |
| POST | `/webhooks/instagram` | Receive Instagram DMs |

## Admin endpoints

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/` | Health check |
| GET | `/health` | Detailed health status (both channels) |
| GET | `/admin/settings/` | View all settings |
| GET | `/admin/settings/providers` | List available LLM providers and models |
| PUT | `/admin/settings/{key}` | Update a setting |
| POST | `/admin/settings/switch-provider` | Quick provider switch |
| GET | `/admin/settings/usage-summary` | Today's token usage and cost estimate |
| GET | `/admin/settings/stats/conversations` | Today's conversation and order stats |
| POST | `/admin/settings/instagram/setup-ice-breakers` | Configure Instagram Ice Breakers |
| POST | `/admin/settings/instagram/subscribe-page` | Subscribe FB Page to webhooks |
| PUT | `/admin/settings/ai_enabled` | Toggle AI on/off globally |
| POST | `/admin/settings/catalog/generate-pdf` | Generate product catalog PDF |
| GET | `/admin/settings/catalog/pdf-status` | Check PDF status |
| GET | `/admin/settings/catalog/download-pdf` | Download catalog PDF |
| GET | `/admin/settings/customers` | List customers (optional `?tag=` filter) |
| POST | `/admin/settings/customers/{id}/resolve` | Resolve escalated customer |
| POST | `/admin/settings/customers/resolve-all` | Resolve all escalated customers |
| GET | `/admin/settings/customers/{id}/tags` | Get customer tags |
| POST | `/admin/settings/customers/{id}/tags` | Add tags to customer |
| DELETE | `/admin/settings/customers/{id}/tags/{tag}` | Remove tag from customer |

## Switching LLM providers

```bash
# Switch to Anthropic Claude Haiku
curl -X POST "http://localhost:8000/admin/settings/switch-provider?provider=anthropic"

# Switch to OpenAI GPT-4o-mini
curl -X POST "http://localhost:8000/admin/settings/switch-provider?provider=openai"

# Switch to a specific model
curl -X POST "http://localhost:8000/admin/settings/switch-provider?provider=openai&model=gpt-5.4-mini"
```

## Instagram setup (after Meta App Review approval)

```bash
# 1. Subscribe your Facebook Page to messaging webhooks (once)
curl -X POST "http://localhost:8000/admin/settings/instagram/subscribe-page?page_id=YOUR_PAGE_ID"

# 2. Configure Ice Breakers (once, or whenever you want to update them)
curl -X POST "http://localhost:8000/admin/settings/instagram/setup-ice-breakers?ig_user_id=YOUR_IG_USER_ID"
```

## Message types handled

### WhatsApp
- Text messages
- Interactive button replies
- List replies
- Images (with captions)
- Audio, video, documents, stickers (acknowledged)
- Reactions (ignored)

### Instagram
- Text DMs
- Quick Reply callbacks
- Ice Breaker taps (converted to natural language for AI)
- Story replies and mentions
- Ad referrals (warm welcome acknowledging the ad)
- Image/media attachments
- Post/Reel shares
- Message deletions (acknowledged)
- Postbacks from buttons

## Telegram admin commands

```
/start          - Welcome + command list
/stats          - Today's conversation and order stats
/customers      - Recent customers (alphabetical) with IDs and tags
/customers vip  - Filter by tag
/orders         - Recent orders (alphabetical by customer)
/order ID STATUS - Update order status
/resolve        - List escalated customers
/resolve ID     - Resolve one escalation
/resolve all    - Resolve all escalations
/tags ID        - View all tags for a customer
/tag ID add t1  - Add tags (comma-separated)
/tag ID del t1  - Remove a tag
/provider NAME  - Switch LLM provider (openai/anthropic)
/ai on          - Resume AI auto-responses
/ai off         - Pause AI (manual responses)
/broadcast      - List broadcasts
/send ID        - Send a broadcast
/preview TAGS   - Preview broadcast reach
/settings       - View current settings
/usage          - Token usage and costs
/conversion     - Sales funnel stats
/performance    - Response time stats
/products       - Popular products
/abtest         - A/B test results
/abmode on/off  - Toggle A/B testing
/catalogpdf     - Generate PDF catalog
```

## Admin dashboard features

Open `/admin/dashboard` in a browser. Five tabs:

- **Resumen**: Stats cards, per-channel breakdown, LLM usage by provider, AI on/off toggle
- **Clientes**: Sortable customer table, tag management (add/remove per customer), resolve escalations individually or all at once
- **Pedidos**: Sortable order table with status badges
- **Broadcasts**: Sortable broadcast table, create/preview/send broadcasts
- **Configuracion**: LLM provider/model/temperature, fallback settings, catalog PDF generation and download
