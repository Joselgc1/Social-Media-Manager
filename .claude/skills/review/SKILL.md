---
name: review
description: Review code changes for quality, security, and project-specific patterns. Use when the user asks to review code, check changes, look for issues, or prepare for a commit.
allowed-tools: Bash, Read, Grep, Glob
---

# Current changes

## Unstaged changes

!`cd /home/joselgc/projects/Social-Media-Manager && git diff --stat 2>/dev/null || echo "No unstaged changes"`

## Staged changes

!`cd /home/joselgc/projects/Social-Media-Manager && git diff --cached --stat 2>/dev/null || echo "No staged changes"`

## Full diff

!`cd /home/joselgc/projects/Social-Media-Manager && git diff 2>/dev/null; git diff --cached 2>/dev/null`

## Review checklist

Review every changed file against this project-specific checklist. For each issue found, cite the exact file and line.

### Database patterns

- Use `record["key"]` — NEVER `record.get("key")` (the `databases` library returns record objects without `.get()`)
- JSONB queries MUST use `CAST(:param AS jsonb)` — NEVER `:param::jsonb` (conflicts with SQLAlchemy bind syntax)
- After writing to the `settings` table, ensure `db.invalidate_settings_cache()` is called

### Security

- New `/admin/` routes in the store must use the `require_admin` dependency from `store/app/admin/auth.py`
- New `/api/` routes in the master must use the `require_auth` dependency from `master/app/auth.py`
- No secrets, API keys, or raw stock counts exposed to the LLM or customers
- No SQL injection via dynamic column names (use whitelists)

### Architecture

- New routers must be registered in the appropriate `main.py` (`store/app/main.py` or `master/app/main.py`)
- New endpoints should have rate limiting via the existing `slowapi` limiter
- Tool definitions in `store/app/ai/tools/definitions.py` must be provider-agnostic JSON Schema
- Tool handlers belong under `store/app/ai/tools/` and are dispatched from `store/app/ai/tools/executor.py`
- New tools must be granted only to the intended agents in `store/app/ai/agents/`
- AI run observability must not log customer message text, full addresses, payment credentials, raw image contents, tool arguments, or raw tool results

### Consistency

- Error handling follows existing patterns (try/except with logging, generic 500 in production)
- Response format matches existing endpoints in the same router
- If LLM model costs were changed, verify BOTH `store/app/ai/providers/__init__.py` AND `master/app/stores/api.py` are updated

Provide a summary of findings organized by severity: blockers, warnings, and suggestions.
