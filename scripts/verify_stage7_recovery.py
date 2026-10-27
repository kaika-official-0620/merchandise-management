"""Whole Stage 2-7 fictional DB/photo recovery drill; never imports app.py.

Creates new labelled OS-temp files only. No providers, live records, secrets,
network, scheduler, authentication session or application startup are used.
This SQLite drill does NOT claim PostgreSQL/Render/age verification.
"""
from __future__ import annotations

import argparse
import ast
from contextlib import closing
from datetime import datetime, timezone
import importlib
import json
import os
from pathlib import Path
import sqlite3
import shutil
import sys
import subprocess
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import platform_recovery as recovery
from recovery_runtime_guard import guard_new_connection, RecoveryQuarantineError
from item_registry import Actor, Registry, RegistryError, _json
from item_registry_schema import migrate_registry
from stage6_common import Stage6Store
from stage3_funds import FundsService
from stage3_billing import BillingService, _catalog
from stage3_profit import ProfitService
from stage5_payments import PhysicalPaymentService, MemoryTestPSP
from stage6_notifications import NotificationService
from stage6_transports import MemoryNotificationTransport

STAFF, CUSTOMER, OTHER = Actor(9, True), Actor(1), Actor(2)
WHEN = '2026-10-22T01:00:00+00:00'


def runtime_sqlite_get_db(database):
    """Execute the exact SQLite get_db body without loading legacy startup."""
    tree = ast.parse((ROOT / 'app.py').read_text(encoding='utf-8-sig'))
    candidates = [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
                  and node.name == 'get_db' and any(isinstance(child, ast.Name)
                      and child.id == 'sqlite3' for child in ast.walk(node))]
    if len(candidates) != 1:
        raise AssertionError('Expected one SQLite application connector')
    namespace = dict(sqlite3=sqlite3, DATABASE=str(database), guard_new_connection=guard_new_connection)
    exec(compile(ast.Module(body=candidates, type_ignores=[]), '<isolated-app-connector>', 'exec'), namespace)
    return namespace['get_db']()


def seed_fixture(database, uploads, *, include_stage7=True):
    """Actual schemas + public domain methods with explicitly fictional terms."""
    db = sqlite3.connect(database)
    db.execute('PRAGMA foreign_keys=ON')
    db.executescript('''CREATE TABLE users(id INTEGER PRIMARY KEY,role TEXT,admin_permissions TEXT);
        INSERT INTO users VALUES(1,'user',NULL),(2,'user',NULL),(9,'owner',NULL),(8,'admin','["merchandise"]');
        CREATE TABLE merchandise(id INTEGER PRIMARY KEY,user_id INTEGER,product_name TEXT);
        CREATE TABLE push_devices(installation TEXT PRIMARY KEY,enabled INTEGER,token TEXT);
        INSERT INTO push_devices VALUES('fictional-device',1,'fictional-device-token');
        CREATE TABLE push_outbox(id TEXT PRIMARY KEY,state TEXT,receipt TEXT);
        INSERT INTO push_outbox VALUES('fixture-queued','queued',NULL),('fixture-done','provider_accepted','fictional-receipt');''')
    migrate_registry(db, allowed_test_path=database)
    for stage in range(3, 8 if include_stage7 else 7):
        module = importlib.import_module('stage%d_migration' % stage)
        module.migrate_test_connection(db, allowed_test_path=database)
    return seed_business_fixture(db, uploads, include_stage7=include_stage7)


def seed_business_fixture(db, uploads, *, include_stage7=True, dialect='sqlite'):
    """Owned disposable connection with schemas prepared by the drill only."""
    registry = Registry(db, dialect)
    store = Stage6Store(registry)
    item = registry.create_item(CUSTOMER, '架空の自己商品', occurred_at=WHEN,
        evidence_reference='fictional-origin', source_key='fixture-item')['physical_item_id']
    other = registry.create_item(OTHER, '架空の別顧客商品', occurred_at=WHEN,
        evidence_reference='fictional-origin', source_key='fixture-other')['physical_item_id']
    registry.record_event(STAFF, item, 'received', WHEN, 'fixture-received', 'fictional-receipt')
    registry.record_work(STAFF, item, 'photography', WHEN, 'fixture-work', 'fictional-work', duration_seconds=90, measurement_source='observed')
    registry.record_event(STAFF, item, 'photographed', WHEN, 'fixture-photographed', 'fictional-photo')
    registry.record_event(STAFF, item, 'registration_completed', WHEN, 'fixture-registered', 'fictional-registration')
    registry.set_deadline(STAFF, item, deadline_at='2026-12-22T01:00:00+00:00',
        evidence_reference='fictional-individual-term', source_key='fixture-deadline')
    photo = uploads / '架空写真' / 'item.png'
    photo.parent.mkdir()
    # Valid 1x1 PNG, fabricated entirely for the drill.
    import base64
    photo.write_bytes(base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j5l0AAAAASUVORK5CYII='))
    assignment = registry._roles(item)['owner']['assignment_id']
    db.execute('''INSERT INTO stage4_media(media_id,physical_item_id,ownership_assignment_id,storage_path,
        content_sha256,byte_size,actor_user_id,recorded_at) VALUES(?,?,?,?,?,?,?,?)''',
        ('fictional-photo', item, assignment, '架空写真/item.png', recovery.digest_file(photo), photo.stat().st_size, 9, WHEN))
    funds = FundsService(store.finance)
    funds.post(STAFF, 1, kind='deposit', amount_yen=300000, occurred_at=WHEN,
        source_key='fixture-deposit', evidence_reference='fictional-test-funds')
    reserved = funds.post(STAFF, 1, kind='reserve', amount_yen=30000, occurred_at=WHEN,
        source_key='fixture-reserve', evidence_reference='fictional-test-reservation', physical_item_id=item)
    funds.post(STAFF, 1, kind='purchase', amount_yen=30000, occurred_at=WHEN,
        source_key='fixture-purchase', evidence_reference='fictional-test-purchase', original_record_id=reserved['record']['record_id'])
    funds.post(STAFF, 1, kind='sales_record', amount_yen=40000, occurred_at=WHEN,
        source_key='fixture-sales', evidence_reference='fictional-test-sale', physical_item_id=item)
    ProfitService(store.finance).record_facts(STAFF, item,
        amounts=dict(purchase_cost_yen=30000, sale_gross_yen=40000, channel_fee_yen=4000,
            materials_yen=100, kaika_fee_yen=None, return_adjustment_yen=0, monthly_allocation_yen=None),
        channel='multichannel', source_reference='fictional-profit', occurred_at=WHEN, source_key='fixture-profit',
        shipping_entries=[dict(source_id='fictional-shipping', amount_yen=800, origin='staff', source_reference='fictional-shipping')],
        channel_fee_includes_shipping=False)
    # Approved ONLY inside this fictional fixture, not through a production path.
    evidence = dict(consent_reference='fictional-consent', signed_document_reference='fictional-contract',
        billing_timezone='Asia/Tokyo', billing_timezone_approved=True, rounding_scope='invoice_per_rate', rounding_method='floor')
    db.execute('''INSERT INTO stage3_agreements(agreement_id,customer_id,actor_user_id,recorded_at,
        evidence_json,catalog_json,status,source_key) VALUES('fictional-agreement',1,9,?,?,?,'fixture_approved','fictional-agreement')''',
        (WHEN, _json(evidence), _json(_catalog())))
    billing = BillingService(store.finance)
    agreement = billing._load_agreement(1, 'fictional-agreement')
    db.execute('''INSERT INTO stage3_charge_drafts(draft_id,customer_id,agreement_id,fee_kind,amount_net_yen,
        tax_rate_percent,tax_treatment,occurred_at,recorded_at,evidence_reference,snapshot_json,reasons_json,source_key,status)
        VALUES('fictional-charge',1,'fictional-agreement','expense',1050,10,'exclusive',?,?,?,?,'[]','fictional-charge','draft')''',
        (WHEN, WHEN, 'fictional-charge', _json(dict(agreement_snapshot=agreement, calculation={'rounding_method':'floor'}))))
    invoice = billing.create_month(STAFF, 1, '2026-10', source_key='fixture-month')['invoice_id']
    payments = PhysicalPaymentService(store, MemoryTestPSP())
    bill = payments.prepare_invoice(STAFF, invoice, review_reference='fictional-reviewed', source_key='fixture-bill')['bill_id']
    consent = payments.record_consent(CUSTOMER, 1, terms_reference='fictional-card-consent',
        payment_method_reference='pm_test_fixture', provider_customer_reference='cus_test_fixture',
        off_session=False, source_key='fixture-consent')['consent_id']
    attempt = payments.create_attempt(CUSTOMER, bill, consent, source_key='fixture-attempt')['attempt_id']
    service = NotificationService(store, transports={'push':MemoryNotificationTransport()}, clock=lambda:1792630800,
        validator=lambda actor,event:True, destination_validator=lambda dest:True, monthly_limits={'push':100})
    service.register_push(CUSTOMER, 'ExpoPushToken[fictionalRecoveryPush123456]',
        'fictional-installation', 'fictional-binding-12345678901234567890', 'ios', source_key='fixture-push')
    service.set_preferences(CUSTOMER, 'listing_approval', ['push'], source_key='fixture-preferences')
    for index, state in enumerate(('queued','claimed','sending','accepted','unknown','failed','provider_accepted','suppressed')):
        event = dict(customer_id=1,event_type='listing_approval',source_type='listing',source_id='fictional-listing-'+str(index),
            source_version='v1',dedupe_key='fictional:'+str(index),physical_item_id=item,ownership_assignment_id=assignment,
            path='/commerce/listings/fictional',execution_mode='test',external_delivery_allowed=False)
        notice = service.publish_event(STAFF,event,source_key='fixture-notice-'+str(index))['notification_id']
        db.execute('UPDATE stage6_notification_outbox SET state=?,claim_hash=?,claimed_at=? WHERE notification_id=?',
            (state, 'fictional-claim-hash', 1792630800, notice))
    service.request_email_verification(CUSTOMER, 'fictional@example.invalid', source_key='fixture-email-proof')
    db.execute('''INSERT INTO stage6_line_invitations VALUES('fictional-invite','fictional-webhook',
        'fictional-line-user','fictional-line-proof',1999999999,NULL)''')
    if include_stage7:
        from stage7_common import Stage7Store
        from stage7_measurement import MeasurementService
        from stage7_economics import EconomicsService
        measurement=MeasurementService(Stage7Store(registry),clock=lambda:WHEN)
        campaign=measurement.create_campaign(STAFF,title='架空の復旧演習',phase='pilot5',
            planned_start_at=WHEN,planned_end_at='2026-11-05T01:00:00+00:00',source_key='fixture-pilot')['campaign_id']
        measurement.enroll(STAFF,campaign,1,experience='existing_reseller',source_key='fixture-enroll')
        measurement.observe(STAFF,campaign,1,event_type='registration_completed',occurred_at=WHEN,
            evidence_reference='fictional-observation',source_key='fixture-observe',basis='test',execution_mode='test')
        EconomicsService(Stage7Store(registry)).record_cost(STAFF,period='2026-10',scope='physical',
            category='materials',amount_yen=100,basis='measured',tax_basis='exclusive',
            reference='fictional-cost-not-business-actual',source_key='fixture-cost')
    db.commit()
    return db, dict(item=item, other=other, invoice=invoice, bill=bill, attempt=attempt,
                    photo='架空写真/item.png', include_stage7=include_stage7)


def semantic_snapshot(connection, identifiers, *, dialect='sqlite'):
    """Business meaning and actual domain authorization, not just row hashes."""
    registry = Registry(connection, dialect)
    store = Stage6Store(registry)
    funds = FundsService(store.finance)
    billing = BillingService(store.finance)
    item = registry.detail(CUSTOMER, identifiers['item'])
    denied = {}
    operations = dict(item=lambda:registry.detail(OTHER, identifiers['item']),
        funds=lambda:funds.summary(OTHER,1), invoice=lambda:billing.get_month(OTHER,identifiers['invoice']),
        card_bill=lambda:PhysicalPaymentService(store,MemoryTestPSP()).get_bill(OTHER, identifiers['bill']))
    for name, operation in operations.items():
        try:
            operation()
            denied[name] = False
        except RegistryError as error:
            denied[name] = error.status in (403,404)
    return dict(item=item, customer_items=[row['physical_item_id'] for row in registry.list_items(CUSTOMER)],
        other_customer_items=[row['physical_item_id'] for row in registry.list_items(OTHER)],
        funds=funds.summary(CUSTOMER,1), invoice=billing.get_month(CUSTOMER,identifiers['invoice']),
        card_bill=PhysicalPaymentService(store,MemoryTestPSP()).get_bill(CUSTOMER,identifiers['bill']),
        profit=ProfitService(store.finance).profit(CUSTOMER,identifiers['item']),
        owner_photo_allowed=store.listing_photo_allowed(identifiers['item'],1,'fictional-photo'),
        other_photo_denied=not store.listing_photo_allowed(identifiers['item'],2,'fictional-photo'),
        tenant_denied=denied)


def run_drill(*, include_stage7=True):
    started = time.monotonic()
    directory = Path(tempfile.mkdtemp(prefix='kaika-stage7-recovery-')).resolve()
    source, target = directory/'source', directory/'target'
    for folder in (source,target):
        folder.mkdir()
        (folder/recovery.FIXTURE_MARKER).write_text(recovery.FIXTURE_TEXT,encoding='utf-8')
        (folder/'uploads').mkdir()
    database, restored_database = source/'fixture.sqlite3', target/'restored.sqlite3'
    db, identifiers = seed_fixture(database,source/'uploads', include_stage7=include_stage7)
    with closing(db):
        before = semantic_snapshot(db,identifiers)
        db.commit()
    source_hash = recovery.digest_file(database)
    # Other developers may still be editing the shared checkout. Capture a
    # code/public-assets-only release first, then fingerprint that fixed copy.
    # This helper excludes .env, all DB files and customer uploads.
    from scripts.run_app_preview import mirror_source
    code = mirror_source(directory, None, None)
    (code/'scripts').mkdir()
    for script in (ROOT/'scripts').iterdir():
        if script.is_file() and script.suffix in ('.py','.sh','.ps1'):
            recovery.plain_path(script)
            shutil.copyfile(script,code/'scripts'/script.name)
    for filename in ('requirements.txt','render.yaml','render.staging.yaml'):
        if (ROOT/filename).is_file():
            shutil.copyfile(ROOT/filename,code/filename)
    bundle=directory/'bundle'; bundle.mkdir()
    recovery.snapshot('sqlite-fixture',database,source/'uploads',code,bundle,'https://source.example.invalid')
    snapshot_seconds = time.monotonic()-started
    restore_started = time.monotonic()
    restored = recovery.restore_bundle(bundle,restored_database,target/'uploads',code,
        'https://production.example.invalid','https://recovery.example.invalid')
    checks={}
    with closing(sqlite3.connect(database)) as original, closing(sqlite3.connect(restored_database)) as recovered:
        after=semantic_snapshot(recovered,identifiers)
        for key in before:
            checks['semantic_'+key]=before[key]==after[key]
        checks['tenant_access_denied_all']=all(after['tenant_denied'].values())
        checks['photo_access_owner_only']=after['owner_photo_allowed'] and after['other_photo_denied']
        checks['funds_not_inflated_by_unpaid_sale']=(after['funds']['balances_yen']['purchase_available']==270000
            and after['funds']['balances_yen']['sales_unpaid']==40000)
        checks['invoice_tax_due_date_and_no_auto_collection']=(after['card_bill']['amount_yen']==1155
            and after['card_bill']['due_date']=='2026-11-10' and not after['card_bill']['automatic_collection_enabled'])
        checks['work_duration_not_estimated']=recovered.execute('SELECT duration_seconds FROM business_work_records').fetchone()[0]==90
        mutable={'push_devices','push_outbox','stage6_destinations','stage6_destination_proofs',
            'stage6_line_invitations','stage6_notification_outbox','stage6_delivery_audit'}
        tables=[row[0] for row in original.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        for table in tables:
            if table not in mutable:
                columns=[row[1] for row in original.execute('PRAGMA table_info("'+table+'")')]
                ordering=','.join('"'+column+'"' for column in columns)
                sql='SELECT * FROM "'+table+'" ORDER BY '+ordering
                checks['rows_'+table]=original.execute(sql).fetchall()==recovered.execute(sql).fetchall()
        checks['foreign_keys_valid']=not recovered.execute('PRAGMA foreign_key_check').fetchall()
        media=recovered.execute('SELECT storage_path,content_sha256,byte_size FROM stage4_media').fetchone()
        restored_photo=target/'uploads'/media[0]
        checks['photo_metadata_matches_restored_file']=(recovery.digest_file(restored_photo)==media[1]
            and restored_photo.stat().st_size==media[2])
        checks['all_active_destinations_revoked']=recovered.execute('SELECT COUNT(*) FROM stage6_destinations WHERE revoked_at IS NULL').fetchone()[0]==0
        checks['all_verification_proofs_invalidated']=recovered.execute('SELECT COUNT(*) FROM stage6_destination_proofs WHERE consumed_at IS NULL').fetchone()[0]==0
        checks['line_invitations_invalidated']=recovered.execute('SELECT COUNT(*) FROM stage6_line_invitations WHERE consumed_at IS NULL').fetchone()[0]==0
        checks['no_notification_can_be_claimed']=recovered.execute("SELECT COUNT(*) FROM stage6_notification_outbox WHERE state IN ('queued','claimed','sending','accepted','failed')").fetchone()[0]==0
        checks['unknown_outcomes_preserved']=recovered.execute("SELECT COUNT(*) FROM stage6_notification_outbox WHERE state='unknown'").fetchone()[0]==3
        checks['completed_delivery_history_retained']=recovered.execute("SELECT COUNT(*) FROM stage6_notification_outbox WHERE state='provider_accepted'").fetchone()[0]==1
        checks['pending_claims_invalidated']=recovered.execute("SELECT COUNT(*) FROM stage6_notification_outbox WHERE outcome_code='recovery_quarantine' AND claim_hash IS NOT NULL").fetchone()[0]==0
        checks['quarantine_delivery_audit_added']=recovered.execute("SELECT COUNT(*) FROM stage6_delivery_audit WHERE outcome_code LIKE 'recovery_quarantine_from_%'").fetchone()[0]==7
    checks['source_db_bytes_unchanged']=recovery.digest_file(database)==source_hash
    checks['photo_bytes_and_reference_equal']=recovery.digest_file(source/'uploads'/identifiers['photo'])==recovery.digest_file(target/'uploads'/identifiers['photo'])
    checks['ordinary_runtime_accepts_source']=False
    with closing(runtime_sqlite_get_db(database)):
        checks['ordinary_runtime_accepts_source']=True
    try:
        runtime_sqlite_get_db(restored_database)
        checks['ordinary_runtime_refuses_quarantined_database']=False
    except RecoveryQuarantineError:
        checks['ordinary_runtime_refuses_quarantined_database']=True
    report=dict(date=datetime.now(timezone.utc).isoformat(),scope='fictional OS-temp SQLite Stage2-7 DB and generated photo',
        include_stage7=include_stage7,quarantine=restored,checks=checks,check_count=len(checks),
        failures=[key for key,value in checks.items() if not value],
        timings=dict(snapshot_seconds=round(snapshot_seconds,3),restore_and_semantic_verification_seconds=round(time.monotonic()-restore_started,3)),
        timing_scope='Tiny fictional fixture only; not accepted RPO/RTO or production volume.',
        production_data_accessed=False,application_started=False,external_communications=False,
        real_payments=False,live_notifications=False,postgres_verified_this_drill=False,age_encryption_verified_this_drill=False)
    path=directory/'verification.json'
    path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    return report,path


def verify_encrypted_copy(report_path, age_bin):
    """Real age CLI, but exclusively the just-created fictional SQLite drill."""
    report_path=Path(report_path).resolve()
    directory=report_path.parent
    recovery.require(directory.parent==Path(tempfile.gettempdir()).resolve()
        and directory.name.startswith('kaika-stage7-recovery-') and report_path.name=='verification.json',
        'Use only an owned fictional recovery drill report.')
    original_report=json.loads(report_path.read_text(encoding='utf-8'))
    recovery.require(original_report['include_stage7'] and not original_report['failures'], 'The fictional baseline must pass first.')
    source=directory/'source'/'fixture.sqlite3'
    recovery.assert_fixture(source)
    target=directory/'encrypted-target';target.mkdir()
    (target/recovery.FIXTURE_MARKER).write_text(recovery.FIXTURE_TEXT,encoding='utf-8')
    (target/'uploads').mkdir()
    key=directory/'fictional.agekey'
    age_bin=Path(age_bin).resolve()
    keep={'SYSTEMROOT','WINDIR','COMSPEC','PATH','PATHEXT','TEMP','TMP','LOCALAPPDATA','APPDATA','USERPROFILE','PYTHONPATH'}
    environment={key:value for key,value in os.environ.items() if key.upper() in keep}
    environment.update(PYTHONDONTWRITEBYTECODE='1',PYTHONIOENCODING='utf-8')
    environment['PATH']=str(age_bin)+os.pathsep+environment.get('PATH','')
    def command(args):
        result=subprocess.run(args,stdout=subprocess.PIPE,stderr=subprocess.PIPE,stdin=subprocess.DEVNULL,
            env=environment,timeout=90,shell=False,creationflags=0x08000000 if os.name=='nt' else 0)
        recovery.require(result.returncode==0,'Fictional encrypted recovery command failed; private output withheld.')
        return result.stdout.decode('utf-8')
    command([str(age_bin/'age-keygen.exe'),'--output',str(key)])
    recipient=command([str(age_bin/'age-keygen.exe'),'-y',str(key)]).strip()
    environment.update(FIXTURE_SOURCE_DB=str(source),FIXTURE_TARGET_DB=str(target/'restored.sqlite3'),
        FIXTURE_SOURCE_ORIGIN='https://source.example.invalid',FIXTURE_TARGET_ORIGIN='https://recovery.example.invalid',
        FIXTURE_PRODUCTION_ORIGIN='https://production.example.invalid')
    cli=[sys.executable,str(ROOT/'scripts'/'platform_recovery.py')]
    archive=directory/'fictional-backup.tar.age'
    saved=json.loads(command(cli+['backup','--kind','sqlite-fixture','--database-env','FIXTURE_SOURCE_DB',
        '--source-origin-env','FIXTURE_SOURCE_ORIGIN','--output',str(archive),'--recipient',recipient,
        '--uploads',str(directory/'source'/'uploads'),'--code-dir',str(directory/'site'),
        '--work-root',str(directory),'--all-writers-stopped']))
    restored=json.loads(command(cli+['restore','--backup',str(archive),'--expected-backup-sha256',saved['backup_sha256'],
        '--identity-file',str(key),'--database-env','FIXTURE_TARGET_DB','--production-origin-env','FIXTURE_PRODUCTION_ORIGIN',
        '--target-origin-env','FIXTURE_TARGET_ORIGIN','--report',str(directory/'encrypted-restored.json'),
        '--max-bytes','100000000','--uploads',str(target/'uploads'),'--code-dir',str(directory/'site'),
        '--work-root',str(directory),'--all-writers-stopped','--isolated-target-confirmed']))
    with closing(sqlite3.connect(source)) as before_db,closing(sqlite3.connect(target/'restored.sqlite3')) as after_db:
        item=before_db.execute("SELECT physical_item_id FROM business_item_profiles WHERE initial_customer_id=1").fetchone()[0]
        other=before_db.execute("SELECT physical_item_id FROM business_item_profiles WHERE initial_customer_id=2").fetchone()[0]
        invoice,bill=before_db.execute('SELECT invoice_id,bill_id FROM stage5_physical_bills').fetchone()
        identifiers=dict(item=item,other=other,invoice=invoice,bill=bill)
        before,after=semantic_snapshot(before_db,identifiers),semantic_snapshot(after_db,identifiers)
    checks={'actual_encrypted_archive_created':saved['state']=='encrypted-backup-created',
        'actual_decrypt_restore_complete':restored['state']=='quarantined-not-activated',
        'all_stage_tables_included':restored['tables_checked']==original_report['quarantine']['tables_checked'],
        'generated_photo_preserved':recovery.digest_file(directory/'source'/'uploads'/'架空写真/item.png')==recovery.digest_file(target/'uploads'/'架空写真/item.png'),
        'encrypted_archive_digest_matches_inventory':recovery.digest_file(archive)==saved['backup_sha256'],
        'notification_replays_suppressed':restored['suppressed_notifications']==4 and restored['unknown_notifications']==3}
    checks.update({'semantic_'+key:before[key]==after[key] for key in before})
    try:
        connection=runtime_sqlite_get_db(target/'restored.sqlite3')
        connection.close()
        checks['normal_runtime_refuses_restore']=False
    except RecoveryQuarantineError:
        checks['normal_runtime_refuses_restore']=True
    result=dict(date=recovery.utc_now(),scope='actual age encryption/decryption of fictional Stage2-7 SQLite and generated photo',
        age_version=command([str(age_bin/'age.exe'),'--version']).strip(),check_count=len(checks),checks=checks,
        failures=[name for name,value in checks.items() if not value],tables_checked=restored['tables_checked'],
        uploads_checked=restored['uploads_checked'],production_data_accessed=False,application_started=False,
        postgres_verified=False,external_communications=False,identity_stored_separately=True,
        identity_scope='Disposable fictional drill key; not a production or organizational recovery key.')
    path=directory/'encrypted-verification.json'
    path.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    return result,path


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--without-stage7',action='store_true',help='Diagnostic only; final Stage7 evidence must include Stage7.')
    parser.add_argument('--age-bin',help='Optional verified portable age directory for actual encryption of the fictional baseline only.')
    args=parser.parse_args()
    try:
        report,path=run_drill(include_stage7=not args.without_stage7)
        print(json.dumps(dict(report=str(path),check_count=report['check_count'],failures=report['failures']),ensure_ascii=True))
        if args.age_bin:
            encrypted,encrypted_path=verify_encrypted_copy(path,args.age_bin)
            print(json.dumps(dict(encrypted_report=str(encrypted_path),check_count=encrypted['check_count'],failures=encrypted['failures']),ensure_ascii=True))
            if encrypted['failures']:
                return 1
        return 1 if report['failures'] else 0
    except Exception:
        print('FICTIONAL_RECOVERY_FAILED: inspect the isolated fixture without sharing its data.',file=sys.stderr)
        return 1


if __name__=='__main__':
    raise SystemExit(main())
