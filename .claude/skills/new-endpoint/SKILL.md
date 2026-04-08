---
name: new-endpoint
description: Scaffold a new API endpoint for the store or master service. Use when the user wants to add an endpoint, create a route, or add an API.
argument-hint: "[endpoint description]"
---

# Endpoint description

$ARGUMENTS$

## Current route registrations

### Store app (store/app/main.py)

!`cd /home/joselgc/projects/Social-Media-Manager && grep -n "include_router\|app.add\|app.get\|app.post" store/app/main.py 2>/dev/null || echo "Could not read"`

### Master app (master/app/main.py)

!`cd /home/joselgc/projects/Social-Media-Manager && grep -n "include_router\|app.add\|app.get\|app.post" master/app/main.py 2>/dev/null || echo "Could not read"`

## Implementation steps

1. **Determine which service** this endpoint belongs to (store or master) based on the description

2. **Create or modify a router file**:
   - **Store admin endpoints:** Follow patterns in `store/app/admin/settings.py`
   - **Store broadcast endpoints:** Follow patterns in `store/app/broadcast/api.py`
   - **Store test/debug endpoints:** Add to `store/app/test_endpoint.py`
   - **Master store API endpoints:** Follow patterns in `master/app/stores/api.py`

3. **Add authentication**:
   - Store admin routes: `require_admin = Depends(require_admin)` from `store/app/admin/auth.py`
   - Master API routes: `auth = Depends(require_auth)` from `master/app/auth.py`
   - Test routes: Guard with `DEBUG=true` (store) or localhost check (master)

4. **Add rate limiting**: Use the existing `limiter` instance with `@limiter.limit("60/minute")` (store) or `"30/minute"` (master)

5. **Register the router** in the appropriate `main.py` if it's a new file

## Database patterns to follow

- Use `await db.execute(query, values)` or `await db.fetch_one/fetch_all`
- Access results with `record["column"]` — NEVER `record.get("column")`
- JSONB: `CAST(:param AS jsonb)` — NEVER `::jsonb`
- After settings writes: call `db.invalidate_settings_cache()`

Read the existing router file for the target area before implementing to match exact patterns.
