"""Explicit disposable-fixture setup and reviewable PostgreSQL SQL only.

Never called at application startup. No production runner or rollback deletion.
"""
from pathlib import Path
import tempfile

from item_registry_schema import migrate_registry, migration_statements
from inventory_reconciliation import import_schema_statements
from stage2_self_management import PHOTO_RECORDS_DDL, SELF_INVENTORY_DDL, custody_ddl


CUSTODY_COLUMNS = (
    ('custody_location', "VARCHAR(16) NOT NULL DEFAULT 'unknown'"),
    ('custody_version', 'INTEGER NOT NULL DEFAULT 1'),
    ('custody_received_at', 'TIMESTAMP'),
)


def postgres_review_sql():
    """Return a candidate script; callers must not execute it against live data."""
    statements = [f'ALTER TABLE merchandise ADD COLUMN IF NOT EXISTS {name} {definition}'
                  for name, definition in CUSTODY_COLUMNS]
    statements += list(migration_statements('postgres'))
    statements += list(import_schema_statements()) + list(SELF_INVENTORY_DDL)
    statements += list(custody_ddl('postgres')) + [PHOTO_RECORDS_DDL]
    return ('-- Stage 2 review candidate. NOT APPLIED. Back up and verify target/schema first.\n'
            '-- Existing contracts, owners, photos, prices and work dates are not backfilled.\n'
            'BEGIN;\nSELECT pg_advisory_xact_lock(574920262);\n' +
            ';\n\n'.join(statements) + ';\nCOMMIT;\n')


def prepare_stage2_fixture(runtime, *, allowed_test_path):
    """Create additive tables only in an exactly named OS-temp fixture DB.

    Validate configuration before opening a connection. Enables neither flags
    nor billing. The caller must already have created fictional base tables.
    """
    if getattr(runtime, 'DATABASE_URL', None):
        raise ValueError('PostgreSQL is SQL-review-only in this migration helper')
    intended = Path(allowed_test_path).resolve()
    temp = Path(tempfile.gettempdir()).resolve()
    actual = Path(getattr(runtime, 'DATABASE', '')).resolve()
    if temp not in intended.parents or actual != intended or not intended.is_file():
        raise ValueError('Only the explicitly named existing disposable fixture is allowed')
    conn = runtime.get_db()
    try:
        conn.execute('PRAGMA foreign_keys=ON')
        for _, _, filename in conn.execute('PRAGMA database_list'):
            if filename and Path(filename).resolve() != intended:
                raise ValueError('An unexpected database was attached')
        conn.execute('BEGIN IMMEDIATE')
        columns = {row[1] for row in conn.execute('PRAGMA table_info(merchandise)')}
        if not columns:
            raise ValueError('Fictional base tables must be prepared first')
        for name, definition in CUSTODY_COLUMNS:
            if name not in columns:
                conn.execute(f'ALTER TABLE merchandise ADD COLUMN {name} {definition}')
        migrate_registry(conn, allowed_test_path=intended)
        for statement in list(import_schema_statements()) + list(SELF_INVENTORY_DDL) + list(custody_ddl()) + [PHOTO_RECORDS_DDL]:
            conn.execute(statement)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

