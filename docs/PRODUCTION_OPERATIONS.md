# Production Operations Runbook

## Ownership And Schedule

- Assign one operator for production database changes and one reviewer.
- Enable automated Railway PostgreSQL backups for both StorePostgres and MasterPostgres.
- Take an operator-controlled backup before every schema migration.
- Perform and document a restore drill at least quarterly.
- Keep store and master backups separate and label them with service, UTC timestamp, schema version, and commit SHA.
- Store backups and encryption keys in separate access-controlled locations.

## Sensitive Data Retention

The store scheduler runs `sensitive_data_retention` daily at 03:30 UTC. Cleanup statements use `FOR UPDATE SKIP LOCKED` and bounded batches so they do not monopolize the database.

| Data | Retention | Action |
| --- | ---: | --- |
| Conversation media URLs | 30 days | Set `media_url` to null |
| Conversation text and tool metadata | 180 days | Delete conversation rows |
| Order payment proof and raw transaction details | 90 days after the order's last update when payment is terminal | Clear URL, raw reference, amount, currency, and timestamps; retain anti-replay hashes |
| Terminal order shipping addresses | 365 days after order creation | Clear address and city |
| Inactive customer saved addresses | 365 days | Clear unless the customer has a pending order |
| Meta completed/failed jobs and receipts | 7 days | Delete through the existing Meta queue cleanup; this includes native Instagram jobs |
| Legacy Instagram/Kommo correlation records | Historical only | Migration 018 deprecates but does not drop this schema; live processing must not create or depend on these records |
| Kommo WhatsApp sent/discarded/failed payloads and callback claims | 7 days | Redact message, primary media URL, return URL, claims, and continuation payloads; ordered `inbound_attachments` remain until job deletion |
| Kommo sent/discarded/failed jobs | 30 days | Delete |
| Kommo receipts | 30 days | Delete |
| Kommo `delivery_unknown` payloads | 90 days | Redact the implemented payload fields and retain minimal job status for manual reconciliation; ordered `inbound_attachments` are not currently cleared |
| Broadcast recipient delivery rows | 90 days | Delete sent/failed rows; retain ambiguous `sending` rows for reconciliation |

Retention is irreversible in the live database. Do not restore expired personal data into production except for a documented incident response with an approved deletion plan.

## Backup Before Migration

Use PostgreSQL URLs supplied through a secure environment, not command history.

```bash
export STORE_DATABASE_URL='postgresql://...'
export MASTER_DATABASE_URL='postgresql://...'
mkdir -p /secure/backups/$(date -u +%Y%m%dT%H%M%SZ)
BACKUP_DIR="/secure/backups/$(date -u +%Y%m%dT%H%M%SZ)"

pg_dump "$STORE_DATABASE_URL" --format=custom --no-owner --no-acl \
  --file "$BACKUP_DIR/store.dump"
pg_dump "$MASTER_DATABASE_URL" --format=custom --no-owner --no-acl \
  --file "$BACKUP_DIR/master.dump"
sha256sum "$BACKUP_DIR/store.dump" "$BACKUP_DIR/master.dump" \
  > "$BACKUP_DIR/SHA256SUMS"
```

Record schema versions and application revision beside the dumps:

```sql
SELECT version, name, applied_at FROM schema_migrations ORDER BY version;
```

Back up these secrets separately:

- Master `ENCRYPTION_KEY`. Without it, restored `store_credentials` and store database URLs are unrecoverable.
- Master and store session/authentication secrets.
- Railway project, service, and production environment identifiers.
- Google/Meta/Kommo credentials using the approved secret manager, not inside the database dump directory.

## Migration Procedure

1. Confirm the backup checksums and record the current commit SHA.
2. Confirm no broadcast is `sending` and reconcile Kommo `delivery_unknown` jobs.
3. Pause deploys and scheduled traffic. Keep the store single-instance.
4. Run the service migration runner for the normal deployment path. The Store runner applies its sequence through version 16 (`016_meta_native_instagram.sql`); the Master runner applies its baseline. Migration 016 deprecates but does not drop legacy Instagram/Kommo correlation schema. It fails closed while launched or uncertain legacy Instagram Kommo jobs remain, so disable legacy Instagram Kommo ingress and let the old deployment drain them before retrying. Use `002_consolidated_upgrade.sql` only for a documented pre-consolidation recovery case, then rerun the normal runner.
5. Query `schema_migrations` and let the matching application revision validate the complete accepted version set; do not rely only on `MAX(version)`.
6. Deploy the matching application revision.
7. Verify `/health`, login, a read-only dashboard query, catalog loading, and one test conversation.
8. Re-enable traffic and monitor errors, job queues, database connections, and delivery reconciliation rows.

Current store upgrade sequence for an existing pre-consolidation database:

```bash
psql "$STORE_DATABASE_URL" -v ON_ERROR_STOP=1 \
   -f store/migrations/002_consolidated_upgrade.sql
DATABASE_URL="$STORE_DATABASE_URL" python store/scripts/migrate.py
```

## Restore Procedure

Restore into a new empty database first. Do not overwrite the only production copy during diagnosis.

```bash
export RESTORE_DATABASE_URL='postgresql://...'
pg_restore --dbname "$RESTORE_DATABASE_URL" --no-owner --no-acl \
  --exit-on-error /secure/backups/<timestamp>/store.dump
```

For master, restore `master.dump` to a separate master database using the same command. Configure the original `ENCRYPTION_KEY` before starting master and verify that one credential can be decrypted without displaying its value.

After restore:

1. Verify checksums and `schema_migrations`.
2. Run row-count and foreign-key sanity checks for customers, orders, settings, jobs, and stores.
3. Start one application instance against the restored database with `OUTBOUND_PROCESSING_ENABLED=false`. This disables scheduled and manually triggered broadcasts, native Meta and Kommo WhatsApp accelerators/job processing, Salesbot callback continuations, and inventory reservation cleanup. Inbound webhooks may be recorded but will not send replies.
4. Run health and read-only smoke tests.
5. Point Railway `DATABASE_URL` to the restored database only after approval.
6. Use the master credential deploy flow so master and Railway retain the same authoritative store URL.
7. Re-enable outbound processing after checking pending jobs for duplicate-delivery risk.

## Rollback

Application rollback is allowed only when the previous revision supports the current schema. Check its `EXPECTED_SCHEMA_VERSION` before deploying it.

Schema migrations are forward-only. Do not improvise destructive down migrations in production. If a migration must be rolled back:

1. Stop store/master processes that write to the affected database.
2. Preserve a dump of the failed post-migration state for investigation.
3. Restore the pre-migration backup into a new database.
4. Deploy the matching pre-migration application revision.
5. Update Railway and master database URLs together.
6. Run the restore verification steps before resuming traffic.

If writes occurred after the migration, decide explicitly whether to replay them. Orders, payments, inventory reservations, broadcasts, and external-message jobs require manual reconciliation; never blindly replay webhook or send-job rows.

## Incident Evidence

For every backup, restore, migration, or rollback, record:

- UTC start/end time, operator, reviewer, commit SHA, and schema version.
- Backup location and SHA-256 checksums.
- Railway deployment IDs and database project identifiers.
- Smoke-test results and any manually reconciled orders/jobs.
- Actual recovery point and recovery time.

## Instagram Customer Continuity Audit

Run the privacy-safe, read-only audit before native Instagram cutover reviews or continuity investigations:

```bash
DATABASE_URL="$STORE_DATABASE_URL" python store/scripts/audit_instagram_customer_continuity.py
```

The script uses a read-only asyncpg transaction and a five-second statement timeout. It reports only aggregate counts and internal customer UUID samples. It does not merge customers, print external identifiers, PII, messages, or the database URL, or call Meta or Kommo APIs.

## Native Instagram Delivery Safety

- Every native Instagram outbound part rechecks the customer's current automation state immediately before sending. A manual/external takeover, block, or global AI pause suppresses the remaining AI output. A final handoff generated by the same automatic-escalation turn remains deliverable.
- Unknown outgoing echoes are deferred durably only when a recent send for that recipient is in flight. The scheduler reconciles them against accepted Meta message IDs before classifying a genuine administrator reply and pausing AI.
- Comment and Story enrichment is supplemental. Graph media or product-mapping failures use an unavailable status and continue to a generic public-comment fallback or a normal private Story reply.
- Public comment history is scoped by Meta media and root thread identifiers. Legacy unscoped comment rows are not used for follow-up inference.
- `META_GRAPH_API_VERSION` is validated and defaults to the tested `v26.0`; keep it explicit in production configuration and update it deliberately after staging verification.
