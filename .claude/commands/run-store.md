# Run Store Command

Start the store app locally for development.

1. Activate the virtual environment if not already active
2. Run from the project root:

```bash
cd /home/joselgc/projects/Social-Media-Manager/store && DEBUG=true uvicorn app.main:app --reload --port 8000
```

After it starts, remind me of the test URLs:

- Chat UI: `http://localhost:8000/test/ui`
- Catalog: `http://localhost:8000/test/catalog`
- Health: `http://localhost:8000/health`
- Dashboard: `http://localhost:8000/admin/dashboard` (requires ADMIN_PASSWORD)
