"""Optional Stage2-7 extension to the dedicated localhost PostgreSQL drill.

Not a migration/activation command. Call only from verify_postgres_recovery's
new fictional databases and restricted owner. No pre-existing service is used.
"""
from pathlib import Path
import importlib
import sys

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0,str(ROOT))
from scripts.verify_stage7_recovery import seed_business_fixture, semantic_snapshot
from scripts import platform_recovery as recovery
from recovery_runtime_guard import guard_new_connection, RecoveryQuarantineError


class FixtureConnection:
    """DBAPI convenience for shared seed statements; owns no live connection."""
    def __init__(self, connection):
        self.connection=connection

    def cursor(self):
        return self.connection.cursor()

    def execute(self, sql, parameters=()):
        cursor=self.connection.cursor()
        cursor.execute(sql.replace('?', '%s'),parameters)
        return cursor

    def commit(self):
        self.connection.commit()


def extend_fixture(connection, uploads):
    from item_registry_schema import migration_statements
    from psycopg2.extensions import parse_dsn
    params=parse_dsn(connection.dsn)
    recovery.require(params.get('host')=='127.0.0.1' and params.get('dbname','').startswith('kaika_fixture_source_'),
        'Stage7 extension requires the newly owned fictional localhost database.')
    db=FixtureConnection(connection)
    db.execute("ALTER TABLE users ADD COLUMN role TEXT; ALTER TABLE users ADD COLUMN admin_permissions TEXT;")
    db.execute("UPDATE users SET role='user' WHERE id=1")
    db.execute("INSERT INTO users(id,username,role,admin_permissions) VALUES(2,'fictional-other','user',NULL),(9,'fictional-owner-admin','owner',NULL),(8,'fictional-inventory','admin','[\"merchandise\"]')")
    for sql in migration_statements('postgres'):
        db.execute(sql)
    for stage in range(3,8):
        for sql in importlib.import_module('stage%d_migration'%stage).statements('postgres'):
            db.execute(sql)
    # Use the additive real schema path, with a dummy runtime and no app import,
    # key, SDK request, or entitlement sync. Preserve the environment evidence.
    from types import SimpleNamespace
    from feature_plans import FeaturePlans
    FeaturePlans(SimpleNamespace(DATABASE_URL='fictional-localhost-only')).schema(db.cursor())
    db.execute("INSERT INTO feature_stripe_receipts VALUES('sub_stage8_fixture','cus_stage8_fixture','test',1792630800)")
    _,ids=seed_business_fixture(db,uploads,include_stage7=True,dialect='postgres')
    return ids


def extra_checks(source, target, identifiers, before, uploads, restored_uploads, *, target_connect):
    after=semantic_snapshot(target,identifiers,dialect='postgres')
    checks={'stage7_semantic_'+key:before[key]==after[key] for key in before}
    checks['stage7_tenant_access_denied']=all(after['tenant_denied'].values())
    checks['stage7_photo_owner_only']=after['owner_photo_allowed'] and after['other_photo_denied']
    checks['stage7_funds_separate']=(after['funds']['balances_yen']['purchase_available']==270000
        and after['funds']['balances_yen']['sales_unpaid']==40000)
    checks['stage7_invoice_tax_and_due']=(after['card_bill']['amount_yen']==1155 and after['card_bill']['due_date']=='2026-11-10')
    with target.cursor() as cursor:
        cursor.execute('SELECT storage_path,content_sha256,byte_size FROM stage4_media')
        path,digest,size=cursor.fetchone()
        checks['stage7_photo_metadata_and_file']=(recovery.digest_file(restored_uploads/path)==digest
            and (restored_uploads/path).stat().st_size==size and recovery.digest_file(uploads/path)==digest)
        cursor.execute("SELECT customer_id,environment,verified_at FROM feature_stripe_receipts WHERE subscription_id='sub_stage8_fixture'")
        checks['stage8_stripe_environment_receipt_preserved']=cursor.fetchone()==('cus_stage8_fixture','test',1792630800)
        for name,sql,expected in (
            ('no_replayable_notifications',"SELECT count(*) FROM stage6_notification_outbox WHERE state IN ('queued','claimed','sending','accepted','failed')",0),
            ('unknown_outcomes_retained',"SELECT count(*) FROM stage6_notification_outbox WHERE state='unknown'",3),
            ('completed_outcome_retained',"SELECT count(*) FROM stage6_notification_outbox WHERE state='provider_accepted'",1),
            ('quarantine_delivery_audit',"SELECT count(*) FROM stage6_delivery_audit WHERE outcome_code LIKE 'recovery_quarantine_from_%%'",7),
            ('destinations_revoked','SELECT count(*) FROM stage6_destinations WHERE revoked_at IS NULL',0),
            ('email_proofs_invalidated','SELECT count(*) FROM stage6_destination_proofs WHERE consumed_at IS NULL',0),
            ('line_invitations_invalidated','SELECT count(*) FROM stage6_line_invitations WHERE consumed_at IS NULL',0)):
            cursor.execute(sql)
            checks['stage7_'+name]=cursor.fetchone()[0]==expected
    # Actual pg_dump/restore normalizes associative CHECK parentheses. The
    # canonical server deparser must still detect a changed monetary bound or
    # boolean operator. All deliberate mutations are rolled back to a savepoint.
    original=recovery.postgres_metadata(target,allow_recovery_marker=True)['schema']
    for name,expression in (
            ('changed_money_bound',"sale_gross_yen >= 1 AND sale_gross_yen <= 8999999999999 AND sale_gross_yen IS NOT NULL"),
            ('changed_boolean_operator',"(sale_gross_yen >= 1 OR sale_gross_yen <= 9000000000000) AND sale_gross_yen IS NOT NULL")):
        with target.cursor() as cursor:
            cursor.execute('SAVEPOINT fixture_constraint_check')
            cursor.execute('ALTER TABLE stage4_channel_orders DROP CONSTRAINT stage4_channel_orders_sale_gross_yen_check')
            cursor.execute('ALTER TABLE stage4_channel_orders ADD CONSTRAINT stage4_channel_orders_sale_gross_yen_check CHECK ('+expression+')')
        changed=recovery.postgres_metadata(target,allow_recovery_marker=True)['schema']
        checks['stage8_metadata_detects_'+name]=changed!=original
        with target.cursor() as cursor:
            cursor.execute('ROLLBACK TO SAVEPOINT fixture_constraint_check')
            cursor.execute('RELEASE SAVEPOINT fixture_constraint_check')
    checks['stage8_deliberate_constraint_mutations_rolled_back']=recovery.postgres_metadata(target,allow_recovery_marker=True)['schema']==original
    with target.cursor() as cursor:
        cursor.execute('SAVEPOINT fixture_trigger_check')
        cursor.execute('ALTER TABLE stage3_fund_records DISABLE TRIGGER USER')
    checks['stage8_metadata_detects_disabled_ledger_trigger']=recovery.postgres_metadata(target,allow_recovery_marker=True)['schema']!=original
    with target.cursor() as cursor:
        cursor.execute('ROLLBACK TO SAVEPOINT fixture_trigger_check')
        cursor.execute('RELEASE SAVEPOINT fixture_trigger_check')
    checks['stage8_deliberate_trigger_mutation_rolled_back']=recovery.postgres_metadata(target,allow_recovery_marker=True)['schema']==original
    # Use a separate newly opened DBAPI connection: the guard intentionally
    # closes it when blocked, leaving this forensic comparison connection alone.
    # psycopg2 connection.dsn masks its password. Use the fixture-owned factory,
    # never reconstruct a connection from that redacted diagnostic string.
    checked=target_connect()
    try:
        guard_new_connection(checked,postgres=True)
        checks['stage7_runtime_quarantine_refused']=False
    except RecoveryQuarantineError:
        checks['stage7_runtime_quarantine_refused']=True
    finally:
        if not checked.closed:
            checked.close()
    return checks
