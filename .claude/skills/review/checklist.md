# Full Review Checklist

## Database Patterns

- [ ] `record["key"]` used instead of `record.get("key")`
- [ ] JSONB queries use `CAST(:param AS jsonb)` not `::jsonb`
- [ ] `db.invalidate_settings_cache()` called after settings table writes
- [ ] Database queries use parameterized values (no f-strings with user input)

## Security

- [ ] Store admin routes use `require_admin` dependency
- [ ] Master API routes use `require_auth` dependency
- [ ] No secrets or API keys in code (all via env vars)
- [ ] No raw stock counts exposed (use `in_stock` boolean only)
- [ ] SQL column names whitelisted (no dynamic column injection)
- [ ] No raw JSON/tool results exposed to customers (system prompt rule 13)

## Architecture

- [ ] New routers registered in appropriate `main.py`
- [ ] Rate limiting applied to new endpoints
- [ ] Tool definitions are provider-agnostic JSON Schema in `tools/definitions.py`
- [ ] Tool handlers live under `store/app/ai/tools/` and dispatch through `tools/executor.py`
- [ ] Tool allowlists in `store/app/ai/agents/` grant only intended permissions
- [ ] One tool call processed per engine iteration (not multiple)

## Consistency

- [ ] Error handling matches existing patterns
- [ ] Response format matches sibling endpoints
- [ ] LLM model costs synced between store and master
- [ ] Interactive buttons/quick replies handled per-channel (WhatsApp vs Instagram)

## Multi-Store

- [ ] Changes don't break multi-store isolation
- [ ] `LLM_MANAGED_EXTERNALLY` respected (403 on locked settings)
- [ ] Master credential encryption not broken
