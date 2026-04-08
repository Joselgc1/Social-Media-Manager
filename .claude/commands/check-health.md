# Check Health Command

Check the health of both services. Try local first, then production.

1. Check local store: `curl -s http://localhost:8000/health`
2. Check local master: `curl -s http://localhost:9000/health`

For each service, if localhost fails, check if there's a production URL in the corresponding .env file (APP_BASE_URL in store/.env, APP_BASE_URL in master/.env) and try that instead.

Display results in a summary showing:

- Service status (up/down)
- Database connectivity
- LLM providers available
- Active provider/model
- Catalog product count (store only)
- Any warnings or errors
