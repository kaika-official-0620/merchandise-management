"""Explicit additive sandbox schema; nothing runs on import."""
from pathlib import Path
import re
import tempfile

VERSION = 'stage9-stripe-sandbox-2026-10-25-v1'


def statements(dialect='sqlite'):
    if dialect not in ('sqlite', 'postgres'):
        raise ValueError('Unsupported dialect')
    amount_check=" AND typeof(amount_yen)='integer'" if dialect=='sqlite' else ''
    return [
        "CREATE TABLE IF NOT EXISTS stage9_payment_scope(sandbox_id TEXT PRIMARY KEY, schema_version TEXT NOT NULL, environment TEXT NOT NULL CHECK(environment='test'))",
        '''CREATE TABLE IF NOT EXISTS stage9_payment_customers(customer_id INTEGER PRIMARY KEY REFERENCES users(id),
            sandbox_id TEXT NOT NULL REFERENCES stage9_payment_scope(sandbox_id), enabled INTEGER NOT NULL CHECK(enabled IN (0,1)))''',
        '''CREATE TABLE IF NOT EXISTS stage9_payment_consents(consent_id TEXT PRIMARY KEY, customer_id INTEGER NOT NULL REFERENCES users(id),
            terms_reference TEXT NOT NULL, actor_id INTEGER NOT NULL REFERENCES users(id), created_at BIGINT NOT NULL,
            revoked_at BIGINT, setup_job_id TEXT UNIQUE, payment_method_id TEXT,
            environment TEXT NOT NULL DEFAULT 'test' CHECK(environment='test'))''',
        f'''CREATE TABLE IF NOT EXISTS stage9_payment_jobs(job_id TEXT PRIMARY KEY, sandbox_id TEXT NOT NULL,
            customer_id INTEGER NOT NULL REFERENCES users(id), kind TEXT NOT NULL CHECK(kind IN ('checkout','setup','off_session','refund')),
            bill_id TEXT REFERENCES stage5_physical_bills(bill_id), parent_job_id TEXT REFERENCES stage9_payment_jobs(job_id),
            consent_id TEXT REFERENCES stage9_payment_consents(consent_id), amount_yen BIGINT NOT NULL CHECK(amount_yen>=0{amount_check}),
            provider_customer_id TEXT NOT NULL, payload_json TEXT NOT NULL, payload_hash TEXT NOT NULL,
            idempotency_key TEXT NOT NULL UNIQUE, source_key TEXT NOT NULL UNIQUE, source_hash TEXT NOT NULL,
            actor_id INTEGER NOT NULL REFERENCES users(id), created_at BIGINT NOT NULL, state TEXT NOT NULL,
            provider_id TEXT UNIQUE, payment_id TEXT, payment_method_id TEXT, first_sent_at BIGINT,
            lease_token TEXT, lease_until BIGINT, hint_id TEXT, hint_kind TEXT, hint_version BIGINT NOT NULL DEFAULT 0,
            needs_reconcile INTEGER NOT NULL DEFAULT 0 CHECK(needs_reconcile IN (0,1)),
            environment TEXT NOT NULL DEFAULT 'test' CHECK(environment='test'))''',
        "CREATE UNIQUE INDEX IF NOT EXISTS stage9_one_payment_per_bill ON stage9_payment_jobs(bill_id) WHERE kind IN ('checkout','off_session')",
        "CREATE UNIQUE INDEX IF NOT EXISTS stage9_one_verified_payment ON stage9_payment_jobs(payment_id) WHERE kind IN ('checkout','off_session') AND payment_id IS NOT NULL",
        '''CREATE TABLE IF NOT EXISTS stage9_payment_events(event_id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES stage9_payment_jobs(job_id),
            kind TEXT NOT NULL, state TEXT NOT NULL, detail_json TEXT NOT NULL, recorded_at BIGINT NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS stage9_payment_webhooks(event_id TEXT PRIMARY KEY, body_hash TEXT NOT NULL,
            job_id TEXT, object_id TEXT NOT NULL, object_kind TEXT NOT NULL, received_at BIGINT NOT NULL)''',
        'CREATE INDEX IF NOT EXISTS stage9_jobs_customer ON stage9_payment_jobs(customer_id,created_at)',
    ] + _immutable(dialect) + _request_immutable(dialect) + _consent_immutable(dialect)


def _consent_immutable(dialect):
    columns=('consent_id','customer_id','terms_reference','actor_id','created_at','setup_job_id','environment')
    if dialect=='sqlite':
        condition=' OR '.join(f'NEW.{name} IS NOT OLD.{name}' for name in columns)
        condition+=" OR OLD.revoked_at IS NOT NULL AND NEW.revoked_at IS NOT OLD.revoked_at OR OLD.payment_method_id IS NOT NULL AND NEW.payment_method_id IS NOT OLD.payment_method_id"
        return [f"CREATE TRIGGER IF NOT EXISTS stage9_consent_immutable BEFORE UPDATE ON stage9_payment_consents WHEN {condition} BEGIN SELECT RAISE(ABORT,'immutable_consent'); END",
            "CREATE TRIGGER IF NOT EXISTS stage9_consent_no_delete BEFORE DELETE ON stage9_payment_consents BEGIN SELECT RAISE(ABORT,'append_only'); END"]
    condition=' OR '.join(f'NEW.{name} IS DISTINCT FROM OLD.{name}' for name in columns)
    condition+=' OR OLD.revoked_at IS NOT NULL AND NEW.revoked_at IS DISTINCT FROM OLD.revoked_at OR OLD.payment_method_id IS NOT NULL AND NEW.payment_method_id IS DISTINCT FROM OLD.payment_method_id'
    return [f"CREATE OR REPLACE FUNCTION stage9_consent_guard() RETURNS trigger AS $$ BEGIN IF TG_OP='DELETE' THEN RAISE EXCEPTION 'append_only'; END IF; IF {condition} THEN RAISE EXCEPTION 'immutable_consent'; END IF; RETURN NEW; END; $$ LANGUAGE plpgsql",
        'DROP TRIGGER IF EXISTS stage9_consent_immutable ON stage9_payment_consents',
        'CREATE TRIGGER stage9_consent_immutable BEFORE UPDATE OR DELETE ON stage9_payment_consents FOR EACH ROW EXECUTE FUNCTION stage9_consent_guard()']


def _request_immutable(dialect):
    # An ambiguous provider result can be retried only with the exact original
    # operation. Enforce request identity in the DB, independently of Python.
    columns=('job_id','sandbox_id','customer_id','kind','bill_id','parent_job_id','consent_id','amount_yen',
        'provider_customer_id','payload_json','payload_hash','idempotency_key','source_key','source_hash','actor_id','created_at','environment')
    if dialect=='sqlite':
        condition=' OR '.join(f'NEW.{name} IS NOT OLD.{name}' for name in columns)
        return [f"CREATE TRIGGER IF NOT EXISTS stage9_request_immutable BEFORE UPDATE ON stage9_payment_jobs WHEN {condition} BEGIN SELECT RAISE(ABORT,'immutable_request'); END",
            "CREATE TRIGGER IF NOT EXISTS stage9_request_no_delete BEFORE DELETE ON stage9_payment_jobs BEGIN SELECT RAISE(ABORT,'append_only'); END"]
    condition=' OR '.join(f'NEW.{name} IS DISTINCT FROM OLD.{name}' for name in columns)
    return [f"CREATE OR REPLACE FUNCTION stage9_request_guard() RETURNS trigger AS $$ BEGIN IF TG_OP='DELETE' THEN RAISE EXCEPTION 'append_only'; END IF; IF {condition} THEN RAISE EXCEPTION 'immutable_request'; END IF; RETURN NEW; END; $$ LANGUAGE plpgsql",
        'DROP TRIGGER IF EXISTS stage9_request_immutable ON stage9_payment_jobs',
        'CREATE TRIGGER stage9_request_immutable BEFORE UPDATE OR DELETE ON stage9_payment_jobs FOR EACH ROW EXECUTE FUNCTION stage9_request_guard()']


def _immutable(dialect):
    tables = ('stage9_payment_events', 'stage9_payment_webhooks', 'stage9_payment_scope')
    if dialect == 'sqlite':
        return [f"CREATE TRIGGER IF NOT EXISTS {table}_no_{op.lower()} BEFORE {op} ON {table} BEGIN SELECT RAISE(ABORT,'append_only'); END"
                for table in tables for op in ('UPDATE','DELETE')]
    return ["CREATE OR REPLACE FUNCTION stage9_payment_reject_change() RETURNS trigger AS $$ BEGIN RAISE EXCEPTION 'append_only'; END; $$ LANGUAGE plpgsql"] + [
        part for table in tables for part in (f'DROP TRIGGER IF EXISTS {table}_immutable ON {table}',
        f'CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION stage9_payment_reject_change()')]


def migrate_test_connection(conn, *, sandbox_id, customer_ids=(), allowed_test_path=None):
    from item_registry import Registry
    from stage5_common import Stage5Store
    if not isinstance(sandbox_id,str) or not re.fullmatch(r'[A-Za-z0-9_-]{16,100}',sandbox_id):
        raise ValueError('Explicit sandbox fixture identifier required')
    if conn.execute('PRAGMA foreign_keys').fetchone()[0] != 1:
        raise ValueError('foreign_keys=ON required')
    for _,alias,filename in conn.execute('PRAGMA database_list'):
        if alias not in ('main','temp'):
            raise ValueError('Attached databases prohibited')
        if filename:
            path=Path(filename).resolve()
            if not allowed_test_path or path!=Path(allowed_test_path).resolve() or Path(tempfile.gettempdir()).resolve() not in path.parents:
                raise ValueError('Explicit disposable database required')
    registry=Registry(conn); Stage5Store(registry).require_ready()
    with registry.atomic():
        for statement in statements(): conn.execute(statement)
        scopes=conn.execute('SELECT sandbox_id FROM stage9_payment_scope').fetchall()
        if scopes and any(row[0]!=sandbox_id for row in scopes):
            raise ValueError('Sandbox scope cannot be changed')
        conn.execute("INSERT INTO stage9_payment_scope VALUES (?,?,'test') ON CONFLICT(sandbox_id) DO NOTHING",(sandbox_id,VERSION))
        for customer in customer_ids:
            if type(customer) is not int or customer<1: raise ValueError('Invalid fixture customer')
            registry._customer_exists(customer)
            conn.execute('INSERT INTO stage9_payment_customers VALUES (?,?,1) ON CONFLICT(customer_id) DO NOTHING',(customer,sandbox_id))


def prepare_stage9_payments_fixture(runtime, *, allowed_test_path, sandbox_id, customer_ids=()):
    path=Path(allowed_test_path).resolve()
    if getattr(runtime,'DATABASE_URL',None) or Path(getattr(runtime,'DATABASE','')).resolve()!=path or not path.is_file():
        raise ValueError('Explicit existing disposable SQLite fixture required')
    conn=runtime.get_db()
    try:
        conn.execute('PRAGMA foreign_keys=ON'); conn.execute('BEGIN IMMEDIATE')
        migrate_test_connection(conn,sandbox_id=sandbox_id,customer_ids=customer_ids,allowed_test_path=path)
        conn.commit()
    except Exception:
        conn.rollback(); raise
    finally: conn.close()
