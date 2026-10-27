"""Opt-in payment-review scope. No provider initialization or live execution."""
from item_registry import RegistryError
from stage4_common import Stage4Store

SCHEMA_VERSION='stage5-payments-2026-10-22-v1'


def immutable_statements(table,dialect='sqlite'):
    if not table.startswith('stage5_') or not table.replace('_','').isalnum():
        raise ValueError('Invalid table')
    if dialect=='sqlite':
        return [f"CREATE TRIGGER IF NOT EXISTS {table}_no_{op.lower()} BEFORE {op} ON {table} BEGIN SELECT RAISE(ABORT,'append_only'); END" for op in ('UPDATE','DELETE')]
    if dialect!='postgres':
        raise ValueError('Unsupported dialect')
    return ["CREATE OR REPLACE FUNCTION stage5_reject_change() RETURNS trigger AS $$ BEGIN RAISE EXCEPTION 'append_only'; END; $$ LANGUAGE plpgsql",
        f'DROP TRIGGER IF EXISTS {table}_immutable ON {table}',
        f'CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION stage5_reject_change()']


def migration_statements(dialect='sqlite'):
    if dialect not in ('sqlite','postgres'):
        raise ValueError('Unsupported dialect')
    return ['CREATE TABLE IF NOT EXISTS stage5_schema(version TEXT PRIMARY KEY NOT NULL)',
        "INSERT INTO stage5_schema(version) VALUES ('"+SCHEMA_VERSION+"') ON CONFLICT(version) DO NOTHING"]


class Stage5Store(Stage4Store):
    def require_ready(self):
        super().require_ready()
        try:
            ready=self.query('SELECT version FROM stage5_schema WHERE version=?',(SCHEMA_VERSION,))
        except Exception:
            ready=False
        if not ready:
            raise RegistryError('stage5_migration_required','段階5の検証用データ構造が未準備です。',409)

    def operation(self,actor,source_key,payload,callback,customer_id=None,staff_required=False,finance=False):
        # Shared transaction/lock ordering and immutable audit with Stage4.
        return super().operation(actor,'stage5:'+source_key,payload,callback,
            customer_id=customer_id,staff_required=staff_required,finance=finance)
