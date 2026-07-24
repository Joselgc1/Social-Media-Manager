---
name: db-query
description: Run a read-only SQL query against the store database
disable-model-invocation: true
argument-hint: "[SQL SELECT query]"
allowed-tools: Bash, Read
---

# Query

$ARGUMENTS$

## Instructions

Run a read-only SQL query against the store's PostgreSQL database.

1. Read the `DATABASE_URL` from `store/.env` (parse the file, look for `DATABASE_URL=`)
2. **REFUSE** any query that is not a SELECT — no INSERT, UPDATE, DELETE, DROP, ALTER, TRUNCATE, CREATE
3. Execute the query using python3 with asyncpg:

```python
import asyncio, asyncpg, os

async def run():
    conn = await asyncpg.connect("DATABASE_URL_HERE")
    try:
        rows = await conn.fetch("THE QUERY")
        for row in rows:
            print(dict(row))
    finally:
        await conn.close()

asyncio.run(run())
```

4. Display results in a readable table format

## Common useful queries

- Customer count: `SELECT COUNT(*) FROM customers`
- Recent conversations: `SELECT c.display_name, conv.role, conv.content, conv.created_at FROM conversations conv JOIN customers c ON c.id = conv.customer_id ORDER BY conv.created_at DESC LIMIT 10`
- Orders: `SELECT * FROM orders ORDER BY created_at DESC LIMIT 10`
- Current settings: `SELECT key, value FROM settings`
- Usage log: `SELECT * FROM usage_log ORDER BY created_at DESC LIMIT 10`
- Daily analytics: `SELECT * FROM daily_analytics ORDER BY date DESC LIMIT 7`
- Product analytics: `SELECT * FROM product_analytics ORDER BY query_count DESC LIMIT 10`
