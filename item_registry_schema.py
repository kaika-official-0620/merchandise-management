"""Explicit, additive stage-2 registry schema; import never connects or migrates.

SQLite execution is confined to memory or an explicitly named temporary test
database. PostgreSQL statements are review material, not an execution helper.
Existing stage-1 billing/contract tables and all legacy rows are left untouched.
"""
import os
from pathlib import Path
import re
import sqlite3
import tempfile

from business_schema import _TABLES

SCHEMA_VERSION = 'item-registry-stage2-2026-10-22-v1'
_CORE_NAMES = ('business_parties', 'business_items', 'business_item_links',
               'business_item_assignments', 'business_work_records', 'business_item_events')

_ADDITIONS = (
    ('business_registry_schema', 'version TEXT PRIMARY KEY NOT NULL'),
    ('business_item_profiles', '''
        physical_item_id TEXT PRIMARY KEY REFERENCES business_items(physical_item_id) ON DELETE RESTRICT,
        title TEXT NOT NULL, origin TEXT NOT NULL CHECK(origin IN ('manual','legacy_import','legacy_link')),
        created_at TEXT NOT NULL, initial_customer_id INTEGER NOT NULL REFERENCES users(id),
        external_label TEXT, imported_fields_json TEXT NOT NULL DEFAULT '{}',
        physical_state TEXT NOT NULL DEFAULT 'unknown', sales_state TEXT NOT NULL DEFAULT 'unlisted',
        workflow_state TEXT NOT NULL DEFAULT 'unreviewed', held INTEGER NOT NULL DEFAULT 0 CHECK(held IN (0,1)),
        received_at TEXT, registration_completed_at TEXT, sold_at TEXT, shipped_at TEXT,
        returned_at TEXT, return_shipped_at TEXT, last_event_at TEXT,
        deadline_at TEXT, deadline_status TEXT NOT NULL DEFAULT 'unknown' CHECK(deadline_status IN ('unknown','confirmed')),
        deadline_evidence TEXT, deadline_customer_id INTEGER REFERENCES users(id),
        CHECK(deadline_status != 'confirmed' OR (deadline_at IS NOT NULL AND deadline_evidence IS NOT NULL))
    '''),
    ('business_registry_operations', '''
        source_key TEXT PRIMARY KEY NOT NULL, actor_user_id INTEGER NOT NULL REFERENCES users(id),
        request_hash TEXT NOT NULL, result_json TEXT NOT NULL
    '''),
    ('business_item_observations', '''
        observation_id TEXT PRIMARY KEY NOT NULL,
        physical_item_id TEXT NOT NULL REFERENCES business_items(physical_item_id),
        kind TEXT NOT NULL, occurred_at TEXT NOT NULL, recorded_at TEXT NOT NULL,
        actor_user_id INTEGER NOT NULL REFERENCES users(id), customer_id INTEGER NOT NULL REFERENCES users(id),
        evidence_reference TEXT NOT NULL, details_json TEXT NOT NULL,
        base_event_id TEXT REFERENCES business_item_events(event_id), source_key TEXT NOT NULL UNIQUE
    '''),
    ('business_work_context', '''
        work_id TEXT PRIMARY KEY REFERENCES business_work_records(work_id),
        customer_id INTEGER NOT NULL REFERENCES users(id), recorded_at TEXT NOT NULL
    '''),
    ('business_legacy_snapshots', '''
        snapshot_id TEXT PRIMARY KEY NOT NULL, merchandise_id INTEGER NOT NULL REFERENCES merchandise(id),
        physical_item_id TEXT NOT NULL REFERENCES business_items(physical_item_id),
        customer_id INTEGER NOT NULL REFERENCES users(id), captured_at TEXT NOT NULL,
        snapshot_json TEXT NOT NULL, source_key TEXT NOT NULL UNIQUE
    '''),
    ('business_item_reviews', '''
        review_id TEXT PRIMARY KEY NOT NULL,
        physical_item_id TEXT NOT NULL REFERENCES business_items(physical_item_id),
        customer_id INTEGER NOT NULL REFERENCES users(id), actor_user_id INTEGER NOT NULL REFERENCES users(id),
        kind TEXT NOT NULL CHECK(kind IN ('identity','deadline','title')),
        recorded_at TEXT NOT NULL, evidence_reference TEXT NOT NULL,
        before_json TEXT NOT NULL, after_json TEXT NOT NULL, source_key TEXT NOT NULL UNIQUE
    '''),
)


def _postgres_columns(columns):
    # Only the six shared identity/log tables are emitted. Replace SQLite-only
    # timestamp/type checks with equivalent PostgreSQL casts/type guarantees.
    columns = re.sub(r"typeof\((\w+)\)='integer' AND ", '', columns)
    columns = re.sub(r"datetime\((\w+)\) IS NOT NULL", r"\1::timestamptz IS NOT NULL", columns)
    columns = re.sub(r"julianday\((\w+)\)", r"\1::timestamptz", columns)
    columns = re.sub(r"substr\((\w+),-1\)", r"right(\1,1)", columns)
    columns = re.sub(r"substr\((\w+),-6,1\)", r"substring(\1 from length(\1)-5 for 1)", columns)
    return columns


def migration_statements(dialect='sqlite'):
    """Return SQL for review. No discovery, connection, backfill or commit."""
    if dialect not in ('sqlite', 'postgres'):
        raise ValueError('Unsupported database dialect')
    tables = tuple((name, cols) for name, cols in _TABLES if name in _CORE_NAMES) + _ADDITIONS
    statements = ['CREATE TABLE IF NOT EXISTS ' + name + ' (' +
                  (_postgres_columns(cols) if dialect == 'postgres' else cols) + ')' for name, cols in tables]
    statements.append('CREATE UNIQUE INDEX IF NOT EXISTS business_current_item_role ON business_item_assignments(physical_item_id,role) WHERE ended_at IS NULL')
    statements.append('CREATE INDEX IF NOT EXISTS business_registry_owner ON business_item_assignments(party_id,role,ended_at)')
    immutable = ('business_work_records', 'business_item_events', 'business_item_observations',
                 'business_work_context', 'business_legacy_snapshots', 'business_registry_operations', 'business_item_reviews')
    if dialect == 'sqlite':
        for table in immutable:
            for op in ('UPDATE', 'DELETE'):
                statements.append("CREATE TRIGGER IF NOT EXISTS " + table + '_registry_no_' + op.lower() +
                    ' BEFORE ' + op + ' ON ' + table + " BEGIN SELECT RAISE(ABORT,'append-only registry record'); END")
        statements.extend([
            '''CREATE TRIGGER IF NOT EXISTS business_registry_assignment_immutable BEFORE UPDATE ON business_item_assignments WHEN
               NEW.assignment_id IS NOT OLD.assignment_id OR NEW.physical_item_id IS NOT OLD.physical_item_id OR
               NEW.role IS NOT OLD.role OR NEW.party_id IS NOT OLD.party_id OR NEW.started_at IS NOT OLD.started_at OR
               NEW.evidence_reference IS NOT OLD.evidence_reference OR NEW.source_event_id IS NOT OLD.source_event_id OR
               (OLD.ended_at IS NOT NULL AND NEW.ended_at IS NOT OLD.ended_at)
               BEGIN SELECT RAISE(ABORT,'close assignment; do not rewrite history'); END''',
            "CREATE TRIGGER IF NOT EXISTS business_registry_assignment_no_delete BEFORE DELETE ON business_item_assignments BEGIN SELECT RAISE(ABORT,'retain assignment history'); END",
        ])
    else:
        statements.append("""CREATE OR REPLACE FUNCTION business_registry_append_only() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'append-only registry record'; END $$""")
        for table in immutable:
            statements.append('DROP TRIGGER IF EXISTS ' + table + '_registry_immutable ON ' + table)
            statements.append('CREATE TRIGGER ' + table + '_registry_immutable BEFORE UPDATE OR DELETE ON ' + table + ' FOR EACH ROW EXECUTE FUNCTION business_registry_append_only()')
        statements.append("""CREATE OR REPLACE FUNCTION business_registry_assignment_guard() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
            IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'retain assignment history'; END IF;
            IF ROW(NEW.assignment_id,NEW.physical_item_id,NEW.role,NEW.party_id,NEW.started_at,NEW.evidence_reference,NEW.source_event_id)
              IS DISTINCT FROM ROW(OLD.assignment_id,OLD.physical_item_id,OLD.role,OLD.party_id,OLD.started_at,OLD.evidence_reference,OLD.source_event_id)
              OR (OLD.ended_at IS NOT NULL AND NEW.ended_at IS DISTINCT FROM OLD.ended_at)
            THEN RAISE EXCEPTION 'close assignment; do not rewrite history'; END IF; RETURN NEW; END $$""")
        statements.append('DROP TRIGGER IF EXISTS business_registry_assignment_guard ON business_item_assignments')
        statements.append('CREATE TRIGGER business_registry_assignment_guard BEFORE UPDATE OR DELETE ON business_item_assignments FOR EACH ROW EXECUTE FUNCTION business_registry_assignment_guard()')
    statements.append("INSERT INTO business_registry_schema(version) VALUES ('" + SCHEMA_VERSION + "') ON CONFLICT(version) DO NOTHING")
    return tuple(statements)


def migrate_registry(connection, dialect='sqlite', mode='test', *, allowed_test_path=None):
    """Explicit local fixture migration only. PostgreSQL is SQL-review-only.

    File fixtures must exactly match allowed_test_path and reside beneath the
    OS temporary directory, excluding any attached real database.
    """
    if dialect != 'sqlite' or not isinstance(connection, sqlite3.Connection) or mode != 'test':
        raise ValueError('Only explicitly isolated SQLite fixtures can be migrated here')
    databases = connection.execute('PRAGMA database_list').fetchall()
    for _, _, file in databases:
        if not file:
            continue
        path = Path(file).resolve()
        temp = Path(tempfile.gettempdir()).resolve()
        if allowed_test_path is None or path != Path(allowed_test_path).resolve() or temp not in path.parents:
            raise ValueError('File-backed database is not the explicitly approved temporary fixture')
    if connection.execute('PRAGMA foreign_keys').fetchone()[0] != 1:
        raise ValueError('foreign_keys=ON is required')
    names = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {'users', 'merchandise'}.issubset(names):
        raise ValueError('Fixture users and merchandise tables are required')
    connection.execute('SAVEPOINT registry_schema')
    try:
        for statement in migration_statements('sqlite'):
            connection.execute(statement)
        connection.execute('RELEASE SAVEPOINT registry_schema')
    except Exception:
        connection.execute('ROLLBACK TO SAVEPOINT registry_schema')
        connection.execute('RELEASE SAVEPOINT registry_schema')
        raise
    return SCHEMA_VERSION
