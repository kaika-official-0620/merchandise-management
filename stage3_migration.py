"""Explicit isolated-fixture migration; production SQL is review material only."""
from pathlib import Path
import tempfile

from stage3_common import migration_statements as common_statements


def statements(dialect='sqlite'):
    from stage3_billing_schema import migration_statements as billing
    from stage3_funds_schema import migration_statements as funds
    from stage3_profit_schema import migration_statements as profit
    return common_statements(dialect) + list(profit(dialect)) + list(billing(dialect)) + list(funds(dialect))


def migrate_test_connection(connection, *, allowed_test_path=None):
    """Caller owns commit. Memory or exact explicitly named OS-temp DB only."""
    from item_registry import Registry
    rows = connection.execute('PRAGMA database_list').fetchall()
    for _, alias, filename in rows:
        if alias not in ('main', 'temp'):
            raise ValueError('Attached databases are not permitted')
        if not filename:
            continue
        intended = Path(allowed_test_path).resolve() if allowed_test_path else None
        if (intended is None or Path(filename).resolve() != intended
                or Path(tempfile.gettempdir()).resolve() not in intended.parents):
            raise ValueError('Only an explicitly named disposable database is allowed')
    registry = Registry(connection)
    registry._require_ready()
    with registry.atomic():
        for sql in statements():
            connection.execute(sql)


def prepare_stage3_fixture(runtime, *, allowed_test_path):
    if getattr(runtime, 'DATABASE_URL', None):
        raise ValueError('PostgreSQL is SQL-review-only')
    intended = Path(allowed_test_path).resolve()
    actual = Path(getattr(runtime, 'DATABASE', '')).resolve()
    if (intended != actual or not intended.is_file()
            or Path(tempfile.gettempdir()).resolve() not in intended.parents):
        raise ValueError('Only the explicitly named existing disposable fixture is allowed')
    conn = runtime.get_db()
    try:
        conn.execute('PRAGMA foreign_keys=ON')
        conn.execute('BEGIN IMMEDIATE')
        migrate_test_connection(conn, allowed_test_path=intended)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def postgres_review_sql():
    return ('-- Stage 3 review candidate only. NOT APPLIED. Stage 2 migration required.\n'
            '-- No backfill, fees, funds or flags are activated by this candidate.\n'
            'BEGIN;\nSELECT pg_advisory_xact_lock(574920263);\n' +
            ';\n\n'.join(statements('postgres')) + ';\nCOMMIT;\n')
