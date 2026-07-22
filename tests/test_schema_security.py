import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "migration",
    [
        ROOT / "store/migrations/001_schema.sql",
        ROOT / "store/migrations/002_existing_database_upgrade.sql",
        ROOT / "master/migrations/001_master_schema.sql",
        ROOT / "master/migrations/002_existing_database_upgrade.sql",
    ],
)
def test_every_application_table_has_rls_and_data_api_roles_are_revoked(migration):
    sql = migration.read_text(encoding="utf-8")
    tables = set(re.findall(r"^CREATE TABLE IF NOT EXISTS ([a-z_]+)", sql, re.MULTILINE))

    assert tables
    for table in tables:
        assert f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;" in sql

    assert "REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM anon, authenticated;" in sql
    assert "REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM anon, authenticated;" in sql
    assert "REVOKE ALL PRIVILEGES ON ALL FUNCTIONS IN SCHEMA public FROM anon, authenticated;" in sql
    assert "REVOKE ALL PRIVILEGES ON SCHEMA public FROM anon, authenticated;" in sql
    assert "REVOKE CREATE ON SCHEMA public FROM PUBLIC;" in sql
    assert "REVOKE ALL PRIVILEGES ON TABLES FROM anon, authenticated;" in sql
    assert "REVOKE ALL PRIVILEGES ON SEQUENCES FROM anon, authenticated;" in sql
    assert "REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC, anon, authenticated;" in sql


@pytest.mark.parametrize(
    "migration",
    [
        ROOT / "store/migrations/001_schema.sql",
        ROOT / "store/migrations/002_existing_database_upgrade.sql",
        ROOT / "master/migrations/001_master_schema.sql",
        ROOT / "master/migrations/002_existing_database_upgrade.sql",
    ],
)
def test_migrations_record_current_schema_version(migration):
    sql = migration.read_text(encoding="utf-8")

    assert "CREATE TABLE IF NOT EXISTS schema_migrations" in sql
    assert "(2, 'versioned_schema_and_security_hardening')" in sql
    assert sql.rfind("INSERT INTO schema_migrations") > sql.rfind("ALTER TABLE")


@pytest.mark.parametrize(
    "migration",
    [
        ROOT / "store/migrations/001_schema.sql",
        ROOT / "store/migrations/002_existing_database_upgrade.sql",
        ROOT / "master/migrations/001_master_schema.sql",
        ROOT / "master/migrations/002_existing_database_upgrade.sql",
    ],
)
def test_migrations_are_transactional(migration):
    sql = migration.read_text(encoding="utf-8").strip()

    assert sql.index("BEGIN;") < sql.index("CREATE TABLE")
    assert sql.endswith("COMMIT;")


def test_store_upgrade_adds_columns_before_dependent_constraints_and_indexes():
    sql = (ROOT / "store/migrations/002_existing_database_upgrade.sql").read_text(encoding="utf-8")

    escalation_column = sql.index("ADD COLUMN IF NOT EXISTS escalation_source")
    escalation_constraint = sql.index("ADD CONSTRAINT customers_escalation_source_check")
    payment_hash_column = sql.index("ADD COLUMN IF NOT EXISTS payment_proof_hash")
    payment_hash_index = sql.index("idx_orders_payment_proof_hash_unique")
    assert escalation_column < escalation_constraint
    assert payment_hash_column < payment_hash_index
