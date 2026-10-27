"""Opt-in measurement records. No I/O or configuration on import."""
from item_registry import RegistryError
from stage6_common import Stage6Store

SCHEMA_VERSION = 'stage7-measurement-operations-2026-10-22-v1'


def immutable_statements(table, dialect='sqlite'):
    if not table.startswith('stage7_') or not table.replace('_', '').isalnum():
        raise ValueError('Invalid table')
    if dialect == 'sqlite':
        return [f"CREATE TRIGGER IF NOT EXISTS {table}_no_{op.lower()} BEFORE {op} ON {table} BEGIN SELECT RAISE(ABORT,'append_only'); END" for op in ('UPDATE', 'DELETE')]
    if dialect != 'postgres':
        raise ValueError('Unsupported dialect')
    return ["CREATE OR REPLACE FUNCTION stage7_reject_change() RETURNS trigger AS $$ BEGIN RAISE EXCEPTION 'append_only'; END; $$ LANGUAGE plpgsql",
            f'DROP TRIGGER IF EXISTS {table}_immutable ON {table}',
            f'CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION stage7_reject_change()']


def migration_statements(dialect='sqlite'):
    if dialect not in ('sqlite', 'postgres'):
        raise ValueError('Unsupported dialect')
    return ['CREATE TABLE IF NOT EXISTS stage7_schema(version TEXT PRIMARY KEY NOT NULL)',
            "INSERT INTO stage7_schema(version) VALUES ('" + SCHEMA_VERSION + "') ON CONFLICT(version) DO NOTHING"]


class Stage7Store(Stage6Store):
    def require_ready(self):
        super().require_ready()
        try:
            ready = self.query('SELECT version FROM stage7_schema WHERE version=?', (SCHEMA_VERSION,))
        except Exception:
            ready = False
        if not ready:
            raise RegistryError('stage7_migration_required', '採算・実証の検証用データ構造が未準備です。', 409)

    def operation(self, actor, source_key, payload, callback, customer_id=None, staff_required=False, finance=False):
        return super().operation(actor, 'stage7:' + source_key, payload, callback,
                                 customer_id=customer_id, staff_required=staff_required, finance=finance)
