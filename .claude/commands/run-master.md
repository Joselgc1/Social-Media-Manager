# Run Master Command

Start the master control plane locally for development.

1. Activate the virtual environment if not already active
2. Run from the project root:

```bash
cd /home/joselgc/projects/Social-Media-Manager/master && uvicorn app.main:app --reload --port 9000
```

After it starts, remind me of the test URLs:

The `/test/*` routes require `ENABLE_TEST_ENDPOINTS=true` and a loopback `APP_BASE_URL` in `master/.env`.

- Test UI: `http://localhost:9000/test/ui`
- DB check: `http://localhost:9000/test/db-check`
- Dashboard login: `http://localhost:9000/login` (sign in with `MASTER_SECRET_KEY`)
