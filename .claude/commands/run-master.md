# Run Master Command

Start the master control plane locally for development.

1. Activate the virtual environment if not already active
2. Run from the project root:

```bash
cd /home/joselgc/projects/Social-Media-Manager/master && uvicorn app.main:app --reload --port 9000
```

After it starts, remind me of the test URLs:

- Test UI: `http://localhost:9000/test/ui`
- DB check: `http://localhost:9000/test/db-check`
- Dashboard: `http://localhost:9000/dashboard?token=SECRET` (replace SECRET with MASTER_SECRET_KEY)
