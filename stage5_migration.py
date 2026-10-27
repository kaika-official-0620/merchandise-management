"""SQL review plus explicit disposable SQLite fixture migration, no startup DDL."""
from pathlib import Path
import tempfile
from stage4_common import Stage4Store
from stage5_common import migration_statements as common


def statements(dialect='sqlite'):
    from stage5_payments_schema import migration_statements as payments
    return list(common(dialect))+list(payments(dialect))


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
    Stage4Store(registry).require_ready()
    with registry.atomic():
        for sql in statements():
            conn.execute(sql)


def prepare_stage5_fixture(runtime,*,allowed_test_path):
    if getattr(runtime,'DATABASE_URL',None):
        raise ValueError('PostgreSQL SQL-review-only')
    intended=Path(allowed_test_path).resolve()
    if Path(getattr(runtime,'DATABASE','')).resolve()!=intended or not intended.is_file() or Path(tempfile.gettempdir()).resolve() not in intended.parents:
        raise ValueError('Explicit existing disposable fixture required')
    conn=runtime.get_db()
    try:
        conn.execute('PRAGMA foreign_keys=ON'); conn.execute('BEGIN IMMEDIATE')
        migrate_test_connection(conn,allowed_test_path=intended)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def postgres_review_sql():
    return '-- Stage 5 candidate only. NOT APPLIED. Requires Stage2/3/4. No live activation.\nBEGIN;\nSELECT pg_advisory_xact_lock(574920265);\n'+';\n\n'.join(statements('postgres'))+';\nCOMMIT;\n'
