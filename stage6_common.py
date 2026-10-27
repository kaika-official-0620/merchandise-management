"""Opt-in home and notification records. No I/O or configuration on import."""
from item_registry import RegistryError
from stage5_common import Stage5Store

SCHEMA_VERSION = 'stage6-home-notifications-2026-10-25-v2'


def immutable_statements(table, dialect='sqlite'):
    if not table.startswith('stage6_') or not table.replace('_', '').isalnum():
        raise ValueError('Invalid table')
    if dialect == 'sqlite':
        return [f"CREATE TRIGGER IF NOT EXISTS {table}_no_{op.lower()} BEFORE {op} ON {table} BEGIN SELECT RAISE(ABORT,'append_only'); END" for op in ('UPDATE', 'DELETE')]
    if dialect != 'postgres':
        raise ValueError('Unsupported dialect')
    return ["CREATE OR REPLACE FUNCTION stage6_reject_change() RETURNS trigger AS $$ BEGIN RAISE EXCEPTION 'append_only'; END; $$ LANGUAGE plpgsql",
            f'DROP TRIGGER IF EXISTS {table}_immutable ON {table}',
            f'CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION stage6_reject_change()']


def migration_statements(dialect='sqlite'):
    if dialect not in ('sqlite', 'postgres'):
        raise ValueError('Unsupported dialect')
    return ['CREATE TABLE IF NOT EXISTS stage6_schema(version TEXT PRIMARY KEY NOT NULL)',
            '''CREATE TABLE IF NOT EXISTS stage6_home_preferences(
                customer_id INTEGER PRIMARY KEY REFERENCES users(id),
                sections_json TEXT NOT NULL, updated_at TEXT NOT NULL)''',
            "INSERT INTO stage6_schema(version) VALUES ('" + SCHEMA_VERSION + "') ON CONFLICT(version) DO NOTHING"]


class Stage6Store(Stage5Store):
    def require_ready(self):
        super().require_ready()
        try:
            ready = self.query('SELECT version FROM stage6_schema WHERE version=?', (SCHEMA_VERSION,))
        except Exception:
            ready = False
        if not ready:
            raise RegistryError('stage6_migration_required', 'ホーム・通知の検証用データ構造が未準備です。', 409)

    def operation(self, actor, source_key, payload, callback, customer_id=None, staff_required=False, finance=False):
        return super().operation(actor, 'stage6:' + source_key, payload, callback,
                                 customer_id=customer_id, staff_required=staff_required, finance=finance)
