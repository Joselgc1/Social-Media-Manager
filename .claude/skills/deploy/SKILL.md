---
name: deploy
description: Deploy store or master service to Railway
disable-model-invocation: true
argument-hint: "[store|master|both]"
---

# Service to deploy

$ARGUMENTS$

## Current git status

### Working tree

!`cd /home/joselgc/projects/Social-Media-Manager && git status --short 2>/dev/null || echo "Not a git repo"`

### Unpushed commits

!`cd /home/joselgc/projects/Social-Media-Manager && git log origin/main..HEAD --oneline 2>/dev/null || echo "No remote tracking or no unpushed commits"`

### Last 3 commits

!`cd /home/joselgc/projects/Social-Media-Manager && git log --oneline -3 2>/dev/null`

## Deployment steps

1. **Check for uncommitted changes** — if there are changes, ask if I want to commit first
2. **Check for unpushed commits** — show what will be deployed
3. **Push to origin main:** `git push origin main`
4. Railway auto-deploys from the main branch

## Reminders

- Store service root directory on Railway: `store/`
- Master service root directory on Railway: `master/`
- Each service has its own Procfile: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
- Check deployment status at https://railway.app
- After deploy, verify health endpoints are responding
