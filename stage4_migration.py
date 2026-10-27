"""Stage 4 DDL review and disposable-fixture execution, never startup DDL."""
from pathlib import Path
import tempfile
from stage3_common import FinanceStore
from stage4_common import migration_statements as common


def statements(dialect='sqlite'):
    from stage4_catalog_schema import migration_statements as catalog
    from stage4_listings_schema import migration_statements as listings
    from stage4_channels_schema import migration_statements as channels
    return list(common(dialect))+list(catalog(dialect))+list(listings(dialect))+list(channels(dialect))


def migrate_test_connection(conn,*,allowed_test_path=None):
    from item_registry import Registry
    if conn.execute('PRAGMA foreign_keys').fetchone()[0]!=1:
        raise ValueError('foreign_keys=ON required')
    for _,alias,filename in conn.execute('PRAGMA database_list'):
        if alias not in ('main','temp'):
            raise ValueError('Attached databases prohibited')
        if filename:
            intended=Path(allowed_test_path).resolve() if allowed_test_path else None
            if intended is None or Path(filename).resolve()!=intended or Path(tempfile.gettempdir()).resolve() not in intended.parents:
                raise ValueError('Explicit disposable database required')
    registry=Registry(conn)
    FinanceStore(registry).require_ready()
    with registry.atomic():
        for sql in statements():
            conn.execute(sql)


def prepare_stage4_fixture(runtime,*,allowed_test_path):
    if getattr(runtime,'DATABASE_URL',None):
        raise ValueError('PostgreSQL SQL-review-only')
    intended=Path(allowed_test_path).resolve()
    if Path(getattr(runtime,'DATABASE','')).resolve()!=intended or not intended.is_file() or Path(tempfile.gettempdir()).resolve() not in intended.parents:
        raise ValueError('Explicit existing disposable fixture required')
    conn=runtime.get_db()
    try:
        conn.execute('PRAGMA foreign_keys=ON')
        conn.execute('BEGIN IMMEDIATE')
        migrate_test_connection(conn,allowed_test_path=intended)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def postgres_review_sql():
    return '-- Stage 4 candidate only, NOT APPLIED. Requires Stage2/3. No backfill or activation.\nBEGIN;\nSELECT pg_advisory_xact_lock(574920264);\n'+';\n\n'.join(statements('postgres'))+';\nCOMMIT;\n'
