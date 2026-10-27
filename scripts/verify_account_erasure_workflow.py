"""Cross-module account-erasure rehearsal with fresh synthetic data only.

No application startup, existing DB, provider call, production setting or real
image is used. A dedicated marked OS-temp SQLite/file fixture is removed after
all handles are closed. Reports contain only checks, counts and source hashes.
"""
import argparse
from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import secrets
import sqlite3
import sys
import tempfile
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))

from account_erasure import ErasureService, migration_statements as erasure_schema
from account_erasure_files import MARKER as FILE_MARKER, MARKER_NAME, plan_file_cleanup, execute_fixture_cleanup
from account_erasure_notice import NoticeService, CapturedCompletionTransport, protected_destination
from account_erasure_restore import (MARKER as DB_MARKER, prepare_fixture_bindings,
    export_fixture_manifest, replay_fixture_manifest, manifest_digest)
from account_lifecycle import (LifecycleService, migration_statements as lifecycle_schema,
    access_allowed_in_store, outbound_allowed_in_store)
from item_registry import Actor, Registry, RegistryError
from recovery_runtime_guard import guard_new_connection, RecoveryQuarantineError
from stage6_notifications_schema import migration_statements as notification_schema
from werkzeug.security import generate_password_hash, check_password_hash


def fixture(root):
    path = root/'account-fixture-workflow.sqlite3'
    path.with_suffix('.fixture.json').write_text(json.dumps(DB_MARKER), encoding='utf-8')
    (root/MARKER_NAME).write_text(json.dumps(FILE_MARKER), encoding='utf-8')
    db = sqlite3.connect(path)
    db.execute('PRAGMA foreign_keys=ON')
    db.executescript('''
        CREATE TABLE users(id INTEGER PRIMARY KEY, username TEXT UNIQUE, password_hash TEXT,
            email TEXT,role TEXT,admin_permissions TEXT,display_name TEXT,address TEXT,proxy_service_budget INTEGER);
        CREATE TABLE merchandise(id INTEGER PRIMARY KEY,user_id INTEGER,photo_path TEXT,additional_photos TEXT,amount INTEGER);
        CREATE TABLE stage2_photo_records(merchandise_id INTEGER,owner_user_id INTEGER,photo_path TEXT,removed_from_item_at TEXT);
        CREATE TABLE business_item_links(merchandise_id INTEGER,physical_item_id TEXT,link_status TEXT);
        CREATE TABLE business_parties(party_id TEXT,kind TEXT,user_id INTEGER);
        CREATE TABLE business_item_assignments(physical_item_id TEXT,role TEXT,party_id TEXT,ended_at TEXT);
        CREATE TABLE invoices(id INTEGER,customer_id INTEGER,amount INTEGER);
        CREATE TABLE mobile_sessions(token_hash TEXT,user_id INTEGER,revoked_at TEXT);
        CREATE TABLE push_devices(installation TEXT,user_id INTEGER,enabled INTEGER);
        CREATE TABLE push_outbox(id INTEGER,user_id INTEGER,state TEXT,token TEXT,binding TEXT,path TEXT);
    ''')
    password = 'Fictional-workflow-password-only'
    for uid, role in ((1,'user'),(2,'user'),(3,'owner')):
        db.execute('INSERT INTO users VALUES (?,?,?,?,?,?,?,?,?)', (uid,'fixture'+str(uid),
            generate_password_hash(password),'fixture'+str(uid)+'@example.invalid',role,None,
            'Fictional customer '+str(uid),'Fictional address',30000))
    for statement in lifecycle_schema()+notification_schema()+erasure_schema():
        db.execute(statement)
    for uid in (1,2):
        reference = f'uploads/fictional-{uid}.jpg'
        path_photo = root/'static'/reference
        path_photo.parent.mkdir(parents=True, exist_ok=True)
        path_photo.write_bytes(('Synthetic image fixture '+str(uid)).encode())
        db.execute('INSERT INTO merchandise VALUES (?,?,?,NULL,30000)', (uid,uid,reference))
        db.execute('INSERT INTO stage2_photo_records VALUES (?,?,?,NULL)', (uid,uid,reference))
        db.execute("INSERT INTO business_item_links VALUES (?,?,'verified')", (uid,'item'+str(uid)))
        db.execute("INSERT INTO business_parties VALUES (?,'customer',?)", ('party'+str(uid),uid))
        db.execute("INSERT INTO business_item_assignments VALUES (?,'owner',?,NULL)", ('item'+str(uid),'party'+str(uid)))
        db.execute('INSERT INTO invoices VALUES (?,?,1155)', (uid,uid))
        db.execute('INSERT INTO mobile_sessions VALUES (?,?,NULL)', ('fictional-token-'+str(uid),uid))
        db.execute('INSERT INTO push_devices VALUES (?,?,1)', ('fictional-installation-'+str(uid),uid))
        db.execute("INSERT INTO push_outbox VALUES (?,?,'queued',?,?,?)", (uid,uid,'fictional-push-token','fictional-binding','/home'))
        address = f'verified-{uid}@example.invalid'
        db.execute('INSERT INTO stage6_destinations VALUES (?,?,?,?,?,?,?,?,?,?)',
            ('email-'+str(uid),uid,'email',address,hashlib.sha256(address.encode()).hexdigest(),None,None,None,123,None))
        db.execute('INSERT INTO stage6_notification_preferences VALUES (?,?,?,?,?)', (uid,'invoice_draft','email',1,123))
        db.execute('''INSERT INTO stage6_notification_outbox(delivery_id,customer_id,destination_id,channel,
            private_payload_json,state,due_at,created_at,test_only) VALUES (?,?,?,'email',?,'queued',123,123,1)''',
            ('normal-'+str(uid),uid,'email-'+str(uid),json.dumps({'fictional_private':'data'})))
    db.commit()
    return db, path, password


def run():
    checks = []
    def check(name, condition):
        checks.append({'name':name,'passed':bool(condition)})
        if not condition:
            raise AssertionError(name)
    def refuses(name, expected, operation):
        try:
            operation()
        except RegistryError as error:
            check(name,error.code == expected)
        else:
            check(name,False)
    report = {'date':'2026-10-26','executed_at_utc':datetime.now(timezone.utc).isoformat(),
        'scope':'fresh marked OS-temp SQLite and fictional photos; cross-module rehearsal; no application startup',
        'checks':checks,'real_provider_calls':0,'production_changed':False,'real_charges':0,
        'not_verified':['real provider erasure or delivery','production migrations','legal retention periods',
            'all personal data erasure','native devices and store publication','production backup systems']}
    temporary = tempfile.TemporaryDirectory(prefix='account-erasure-files-workflow-')
    root = Path(temporary.name).resolve()
    connections = []
    try:
        # Verify the exact dedicated target before its recursive fixture cleanup.
        if Path(tempfile.gettempdir()).resolve() not in root.parents or not root.name.startswith('account-erasure-files-workflow-'):
            raise AssertionError('dedicated temporary directory required')
        with ExitStack() as boundaries:
            for operation in ('socket.socket.connect','socket.socket.connect_ex','socket.create_connection'):
                boundaries.enter_context(patch(operation, side_effect=AssertionError('external_socket_denied')))
            db, path, password = fixture(root)
            connections.append(db)
            store=Registry(db); lifecycle=LifecycleService(store); erasure=ErasureService(store)
            notices=NoticeService(store); customer=Actor(1); staff=Actor(3)
            key=secrets.token_bytes(32); environment=secrets.token_hex(16)
            prepare_fixture_bindings(store,staff,fixture_database=path,environment_id=environment)
            db.commit()
            check('bindings_prepared_before_backup',len(store.query('SELECT customer_id FROM account_restore_bindings'))==2)
            old_manifest=export_fixture_manifest(store,staff,fixture_database=path,environment_id=environment,generation=1,signing_key=key)
            restored_path=root/'account-fixture-restored.sqlite3'
            restored_path.with_suffix('.fixture.json').write_text(json.dumps(DB_MARKER),encoding='utf-8')
            restored_db=sqlite3.connect(restored_path);connections.append(restored_db)
            db.backup(restored_db)
            restored_db.execute('CREATE TABLE kaika_recovery_quarantine(reason TEXT)')
            restored_db.execute("INSERT INTO kaika_recovery_quarantine VALUES('synthetic recovery')")
            restored_db.commit();restored=Registry(restored_db)
            backup_photo=root/'old-backup-photo.jpg'
            source_photo=root/'static/uploads/fictional-1.jpg';other_photo=root/'static/uploads/fictional-2.jpg'
            backup_photo.write_bytes(source_photo.read_bytes())
            other_before=store.query('SELECT * FROM users WHERE id=2')
            financial_before=store.query('SELECT * FROM invoices ORDER BY id')
            ownership_before=store.query('SELECT * FROM business_item_assignments ORDER BY physical_item_id')
            request=lifecycle.request(customer,source_key='fictional-workflow-request')
            plan=erasure.prepare(staff,1,request['request_id'])
            for task in ('profile','notifications'):
                plan=erasure.decide(staff,1,request['request_id'],revision=plan['revision'],task_key=task,
                    action='erase',reason='Synthetic rehearsal only',reference='fictional-erasure-policy')
            check('request_and_draft_do_not_erase_or_close',access_allowed_in_store(store,1) and source_photo.exists()
                  and store.query('SELECT email FROM users WHERE id=1')[0]['email']!='')
            notices.set_consent(customer,request['request_id'],True,source_key='fictional-completion-opt-in')
            check('completion_email_separate_opt_in_bound',protected_destination(store,1)=='email-1'
                  and not outbound_allowed_in_store(store,1))
            result=erasure.rehearse_local(staff,1,request['request_id'],fixture_database=path,revision=plan['revision'])
            db.commit()
            check('core_erasure_stays_pending_and_revokes_access',result['complete'] is False and not access_allowed_in_store(store,1)
                  and lifecycle.get(staff,1)['state']=='erasure_pending')
            held=store.query("SELECT address,revoked_at FROM stage6_destinations WHERE destination_id='email-1'")[0]
            check('only_completion_contact_held_without_normal_delivery',held['address']=='verified-1@example.invalid'
                  and held['revoked_at'] is not None and result['completion_contact_held'] and not outbound_allowed_in_store(store,1))
            check('normal_queue_suppressed_and_body_scrubbed',store.query("SELECT state,private_payload_json FROM stage6_notification_outbox WHERE delivery_id='normal-1'")[0]
                  == {'state':'suppressed','private_payload_json':'{}'})
            check('other_customer_and_money_and_ownership_unchanged',other_before==store.query('SELECT * FROM users WHERE id=2')
                  and financial_before==store.query('SELECT * FROM invoices ORDER BY id')
                  and ownership_before==store.query('SELECT * FROM business_item_assignments ORDER BY physical_item_id'))
            file_plan=plan_file_cleanup(store,1,fixture_root=root,decisions={'uploads/fictional-1.jpg':
                {'action':'delete','synthetic_only':True,'reference':'fictional-photo-approval'}})
            file_result=execute_fixture_cleanup(store,file_plan,fixture_root=root)
            check('one_reviewed_photo_erased_other_customer_photo_kept',file_result['deleted']==1 and file_result['blocked']==0
                  and not source_photo.exists() and other_photo.exists())
            repeated=execute_fixture_cleanup(store,file_plan,fixture_root=root)
            check('file_plan_replay_idempotent_and_not_account_completion',repeated['deleted']==1
                  and not repeated['account_deletion_complete'] and backup_photo.exists())
            capture=CapturedCompletionTransport()
            refuses('unfinished_erasure_refuses_completion_dispatch','completion_notice_erasure_not_complete',
                lambda:notices.dispatch_fixture(staff,1,request['request_id'],fixture_database=path,
                    completion_evidence='fictional-not-complete',sender='fixture@example.invalid',transport=capture))
            check('no_completion_message_captured',not capture.messages)
            latest=export_fixture_manifest(store,staff,fixture_database=path,environment_id=environment,generation=2,signing_key=key)
            digest=manifest_digest(latest)
            refuses('signed_old_manifest_rejected_by_independent_latest_anchor','deletion_restore_latest_manifest_mismatch',
                lambda:replay_fixture_manifest(restored,staff,fixture_database=restored_path,envelope=old_manifest,
                    environment_id=environment,expected_manifest_sha256=digest,signing_key=key))
            replay=replay_fixture_manifest(restored,staff,fixture_database=restored_path,envelope=latest,
                environment_id=environment,expected_manifest_sha256=digest,signing_key=key)
            restored_db.commit()
            check('old_backup_cannot_resurrect_login_or_normal_delivery',not access_allowed_in_store(restored,1)
                  and not outbound_allowed_in_store(restored,1)
                  and not check_password_hash(restored.query('SELECT password_hash FROM users WHERE id=1')[0]['password_hash'],password)
                  and restored.query('SELECT revoked_at FROM mobile_sessions WHERE user_id=1')[0]['revoked_at'] is not None
                  and restored.query('SELECT enabled FROM push_devices WHERE user_id=1')[0]['enabled']==0)
            check('restored_queues_suppressed_only_for_target',restored.query('SELECT state FROM push_outbox WHERE user_id=1')[0]['state']=='cancelled'
                  and restored.query('SELECT state FROM push_outbox WHERE user_id=2')[0]['state']=='queued'
                  and restored.query("SELECT state FROM stage6_notification_outbox WHERE delivery_id='normal-1'")[0]['state']=='suppressed'
                  and restored.query("SELECT state FROM stage6_notification_outbox WHERE delivery_id='normal-2'")[0]['state']=='queued')
            check('restored_money_other_customer_and_remaining_photo_preserved',financial_before==restored.query('SELECT * FROM invoices ORDER BY id')
                  and other_before==restored.query('SELECT * FROM users WHERE id=2') and backup_photo.exists())
            check('restore_is_pending_with_photo_provider_retention_review',replay['erasure_complete'] is False
                  and replay['quarantine_retained'] and 'photos' in replay['remaining_review']
                  and restored.query('SELECT state FROM account_deletion_requests WHERE customer_id=1')[0]['state']=='erasure_pending')
            fresh=sqlite3.connect(restored_path)
            try:
                guard_new_connection(fresh)
            except RecoveryQuarantineError:
                check('runtime_quarantine_still_blocks_restored_database',True)
            else:
                check('runtime_quarantine_still_blocks_restored_database',False)
            finally:
                fresh.close()
            events=restored.query('SELECT COUNT(*) AS n FROM account_deletion_events')[0]['n']
            replay_again=replay_fixture_manifest(restored,staff,fixture_database=restored_path,envelope=latest,
                environment_id=environment,expected_manifest_sha256=digest,signing_key=key)
            check('manifest_replay_idempotent_without_duplicate_events',replay_again['already_recorded']
                  and events==restored.query('SELECT COUNT(*) AS n FROM account_deletion_events')[0]['n'])
            report.update(account_deletion_complete=False, completion_messages=0, deleted_fictional_photos=1,
                          restored_photo_requires_review=True, source_auth_closed=True, restored_auth_closed=True)
    except Exception as error:
        report['error_type']=type(error).__name__
        report['error_code']=error.code if isinstance(error,RegistryError) else 'workflow_check_or_fixture_failure'
        if isinstance(error,AssertionError):report['failed_check']=str(error)
    finally:
        for db in connections:
            db.close()
        # This is the same checked, newly generated fixture root, never a caller path.
        if root.parent.resolve()==Path(tempfile.gettempdir()).resolve() and root.name.startswith('account-erasure-files-workflow-'):
            temporary.cleanup()
            report['temporary_fixture_removed']=not root.exists()
        else:
            report['temporary_fixture_removed']=False
    report['passed']=sum(c['passed'] for c in checks)
    report['failed']=sum(not c['passed'] for c in checks) or int('error_type' in report)
    report['source_sha256']={name:hashlib.sha256((SOURCE/name).read_bytes()).hexdigest() for name in (
        'account_erasure.py','account_erasure_guard.py','account_erasure_files.py','account_erasure_restore.py',
        'account_erasure_notice.py','account_lifecycle.py','account_data_inventory.py','recovery_runtime_guard.py',
        'scripts/verify_account_erasure_workflow.py')}
    return report


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--report',type=Path,default=SOURCE/'docs/account-erasure-workflow-evidence-2026-10-26.json')
    args=parser.parse_args()
    report=run()
    args.report.parent.mkdir(parents=True,exist_ok=True)
    args.report.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({key:report.get(key) for key in ('passed','failed','error_type','error_code','failed_check','temporary_fixture_removed')},ensure_ascii=False))
    return 1 if report['failed'] else 0


if __name__=='__main__':
    raise SystemExit(main())
