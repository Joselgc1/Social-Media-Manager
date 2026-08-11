# Railway PostgreSQL Deployment

This project uses four independent Railway services in one project and environment:

```text
Railway project
├── Store
├── StorePostgres
├── Master
└── MasterPostgres
```

`Store` and `Master` must never share a database. The Master service connects directly to each registered Store database for statistics and runtime-setting updates.

## Create The Services

1. Create or select a Railway project and production environment.
2. Add two Railway PostgreSQL services named `StorePostgres` and `MasterPostgres`.
3. Add two GitHub services from this repository.
4. Set the Store service root directory to `store/` and the Master service root directory to `master/`.
5. Keep the Store service at one replica. Its scheduler and broadcast processing run in-process.

The `railway.toml` files live inside `store/` and `master/` because Railway resolves config-as-code from each service root.

## Required Variables

Set these Railway reference variables in the service Variables UI:

```text
Store
DATABASE_URL=${{StorePostgres.DATABASE_URL}}

Master
DATABASE_URL=${{MasterPostgres.DATABASE_URL}}
```

Then set each application's existing secrets and configuration variables. The minimum additional Store production configuration remains its channel credentials, one LLM key, Google Sheets credentials, `APP_BASE_URL`, and a strong `ADMIN_PASSWORD`. Master requires `MASTER_SECRET_KEY`, the existing Fernet `ENCRYPTION_KEY`, `APP_BASE_URL`, and optionally `RAILWAY_API_TOKEN`.

Do not hardcode database hosts, users, passwords, or ports in code or committed files. For local development use normal PostgreSQL URLs such as `postgresql://postgres:password@localhost:5432/store_db`.

## Migrations And Deploys

Each service has an explicit Railway pre-deploy migration command:

```text
Store pre-deploy:  python scripts/migrate.py
Store start:       uvicorn app.main:app --host 0.0.0.0 --port $PORT

Master pre-deploy: python scripts/migrate.py
Master start:      uvicorn app.main:app --host 0.0.0.0 --port $PORT
```

The runners use `asyncpg`, take a PostgreSQL advisory lock, and exit non-zero on failure without printing database URLs or credentials. The Store runner applies its normal sequence through schema version `16`; the Master runner applies its `001_master_schema.sql` baseline. Railway does not activate a deployment when its pre-deploy command fails.

Run the same command locally after exporting the relevant `DATABASE_URL`:

```bash
python store/scripts/migrate.py
python master/scripts/migrate.py
```

For a legacy pre-consolidation database, take a backup and use the appropriate `002_consolidated_upgrade.sql` recovery migration only when required by the application's schema-version error, then run the normal service migration runner again. Do not use the obsolete `002_existing_database_upgrade.sql` filename. For normal fresh installs and upgrades, run only the service migration runner rather than individual SQL files.

## Register StorePostgres In Master

The Store service itself should always use its private Railway reference variable. The Master registry needs a resolved, usable Store PostgreSQL connection URL because Railway reference interpolation occurs only in service environment variables.

1. Obtain the resolved private StorePostgres PostgreSQL URL from Railway.
2. Open Master Dashboard, select the Store, and use **Edit**.
3. Enter the URL in **Sensitive Database Connection URL** and save.
4. Leave that field blank on future metadata edits to retain the existing URL.
5. Deploy Store credentials from Master only if the Store service's `DATABASE_URL` must also be changed.

Never save the literal `${{StorePostgres.DATABASE_URL}}` in the Master database. If Master cannot reach the Railway private network, register the StorePostgres public URL instead. Keep the Store service on its private `DATABASE_URL` in either case.

Changing the URL through `PUT /api/stores/{id}` with an optional `db_url` encrypts it with the existing Fernet key, updates the authoritative `stores.db_url_encrypted` and `DATABASE_URL` credential in one transaction, and disconnects the old cached Store pool. API responses expose only a masked URL.

## Verify And Back Up

After both deployments complete:

```bash
curl https://YOUR_STORE_URL/health
curl https://YOUR_MASTER_URL/health
```

Confirm Store health reports its database and scheduler status, and Master health loads with the expected Store count. Enable Railway database backups for both `StorePostgres` and `MasterPostgres` in the Railway dashboard, then perform a restore drill before relying on production backups.

## Migrate Existing PostgreSQL Data

Supabase remains PostgreSQL-compatible during the transition. Keep it available until the Railway applications, row counts, and health checks are verified. Back up both source databases before switching and do not commit URLs or dump files.

Store migration:

```bash
pg_dump \
  --dbname="$OLD_STORE_DATABASE_URL" \
  --format=custom \
  --no-owner \
  --no-acl \
  --file=store.dump

pg_restore \
  --dbname="$NEW_STORE_DATABASE_PUBLIC_URL" \
  --clean \
  --if-exists \
  --no-owner \
  --no-acl \
  store.dump
```

Master migration:

```bash
pg_dump \
  --dbname="$OLD_MASTER_DATABASE_URL" \
  --format=custom \
  --no-owner \
  --no-acl \
  --file=master.dump

pg_restore \
  --dbname="$NEW_MASTER_DATABASE_PUBLIC_URL" \
  --clean \
  --if-exists \
  --no-owner \
  --no-acl \
  master.dump
```

After each restore, run the appropriate migration runner, compare source and target row counts, let the application validate the complete `schema_migrations` set, and check `/health`. Preserve the existing Master `ENCRYPTION_KEY`; changing it makes encrypted Store URLs and credentials unrecoverable. Update the registered Store URL in Master before retiring the prior database.
