"""Actual PostgreSQL transaction/race checks; fictional data and captured HTTP.

Accept only verify_postgres_recovery's explicitly owned localhost context.
Creates a fresh database and restricted role; never imports the app or calls a
provider. Caller must stop the cluster afterwards, including on failure.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import hashlib
import json
from pathlib import Path
import secrets
import sys
from threading import Barrier, Lock

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from verify_postgres_recovery import read_context, fixture_connect, source_fixture
from verify_stage7_postgres_recovery import extend_fixture
from platform_recovery import postgres_parameters
from item_registry import Registry, RegistryError
from stage6_common import Stage6Store
from stage6_notifications import NotificationService
from stage6_external_transports import ExpoTestTransport, TestDeliveryPolicy
from account_lifecycle import LifecycleService, migration_statements, outbound_allowed_in_store
from scripts.verify_stage7_recovery import STAFF,CUSTOMER,WHEN

PUSH='ExpoPushToken[fictionalRecoveryPush123456]'


def verify(context,report_path):
    from psycopg2 import sql
    from psycopg2.extensions import make_dsn
    name='kaika_fixture_source_'+secrets.token_hex(6)
    role='kaika_fixture_owner_'+secrets.token_hex(6)
    password=secrets.token_urlsafe(32)
    with closing(fixture_connect(context['admin_database_url'])) as admin:
        admin.autocommit=True
        with admin.cursor() as cur:
            cur.execute(sql.SQL('CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD %s').format(sql.Identifier(role)),(password,))
            cur.execute(sql.SQL("CREATE DATABASE {} OWNER {} TEMPLATE template0 ENCODING 'UTF8'").format(sql.Identifier(name),sql.Identifier(role)))
    dsn=make_dsn(**{**postgres_parameters(context['admin_database_url']),'user':role,'password':password,'dbname':name})
    def connect():
        conn=fixture_connect(dsn)
        with conn.cursor() as cur:cur.execute("SET statement_timeout='15s'; SET lock_timeout='10s'")
        conn.commit()
        return conn
    checks={};calls=[];call_lock=Lock()
    policy=TestDeliveryPolicy(True,'test',({'channel':'push','customer_id':1,
        'recipient_sha256':hashlib.sha256(PUSH.encode()).hexdigest(),'approval_reference':'fictional-owned-pg-drill'},))
    def service(conn,http):
        store=Stage6Store(Registry(conn,'postgres'))
        return NotificationService(store,transports={'push':ExpoTestTransport(policy,http=http)},clock=lambda:1792630800,
            validator=lambda *a:True,destination_validator=lambda *a:True,monthly_limits={'push':100},
            customer_active=lambda customer:outbound_allowed_in_store(store,customer))
    uploads=Path(context['root'])/('notification-fictional-uploads-'+secrets.token_hex(4));uploads.mkdir()
    with closing(connect()) as conn:
        source_fixture(conn);ids=extend_fixture(conn,uploads)
        with conn.cursor() as cur:
            # Recovery's intentionally minimal legacy table predates the real
            # push owner column. Add fictional ownership for lifecycle checks.
            cur.execute('ALTER TABLE push_outbox ADD COLUMN user_id BIGINT REFERENCES users(id)')
            cur.execute('UPDATE push_outbox SET user_id=1')
            for statement in migration_statements('postgres'):cur.execute(statement)
            cur.execute('SELECT rolsuper,rolcreatedb,rolcreaterole FROM pg_roles WHERE rolname=current_user')
            checks['restricted_owner']=cur.fetchone()==(False,False,False)
        conn.commit()
        api=service(conn,lambda *a:None)
        assignment=api.store.registry._roles(ids['item'])['owner']['assignment_id']
        def new_claim(label):
            event=dict(customer_id=1,event_type='listing_approval',source_type='listing',source_id='pg-'+label,
                source_version='v1',dedupe_key='pg:'+label,physical_item_id=ids['item'],ownership_assignment_id=assignment,
                path='/commerce/listings/fictional',execution_mode='test',external_delivery_allowed=False)
            note=api.publish_event(STAFF,event,source_key='pg-notice-'+label)['notification_id']
            delivery=api.store.query('SELECT delivery_id FROM stage6_notification_outbox WHERE notification_id=?',(note,))[0]['delivery_id']
            claim=api.claim(STAFF,delivery,source_key='pg-claim-'+label)
            conn.commit()
            return delivery,claim['claim_token']
        delivery,token=new_claim('race')
        barrier=Barrier(2)
        def contender(index):
            with closing(connect()) as worker:
                def captured(plan,bearer):
                    idle=worker.get_transaction_status()==0
                    with closing(connect()) as observer:
                        with observer.cursor() as cur:
                            cur.execute('SELECT state,attempts FROM stage6_notification_outbox WHERE delivery_id=%s',(delivery,))
                            visible=cur.fetchone()==('sending',1)
                    with call_lock:calls.append({'phase':'send','idle':idle,'visible':visible})
                    return 200,{},{'data':{'status':'ok','id':'fictional-pg-ticket'}}
                worker_api=service(worker,captured)
                barrier.wait(timeout=10)
                try:
                    result=worker_api.dispatch_claim(STAFF,delivery,token)
                    return {'state':result['state']}
                except RegistryError as error:
                    worker.rollback();return {'error':error.code}
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(contender,(0,1)))
        checks['one_dispatch_accepted']=results.count({'state':'accepted'})==1
        checks['duplicate_dispatch_rejected']=results.count({'error':'notification_claim_invalid'})==1
        checks['one_provider_send']=len(calls)==1
        checks['committed_sending_visible_before_send']=len(calls)==1 and calls[0]['visible']
        checks['send_outside_postgres_transaction']=len(calls)==1 and calls[0]['idle']
        def receipt(plan,bearer):
            checks['receipt_outside_postgres_transaction']=conn.get_transaction_status()==0
            checks['receipt_uses_original_ticket']=plan['json']=={'ids':['fictional-pg-ticket']}
            calls.append({'phase':'receipt'})
            return 200,{},{'data':{'fictional-pg-ticket':{'status':'ok'}}}
        result=service(conn,receipt).reconcile(STAFF,delivery)
        checks['provider_receipt_recorded']=result['state']=='provider_accepted';conn.commit()
        delivery,token=new_claim('timeout')
        def timeout(*a):calls.append({'phase':'timeout'});raise TimeoutError('fictional-private-detail')
        result=service(conn,timeout).dispatch_claim(STAFF,delivery,token)
        checks['timeout_unknown']=result['state']=='unknown';conn.commit()
        try:service(conn,timeout).dispatch_claim(STAFF,delivery,token);checks['unknown_not_resent']=False
        except RegistryError as error:checks['unknown_not_resent']=error.code=='notification_claim_invalid';conn.rollback()
        try:api.retry_rejected(STAFF,delivery,source_key='pg-timeout-retry');checks['unknown_not_retryable']=False
        except RegistryError as error:checks['unknown_not_retryable']=error.code=='notification_retry_not_safe';conn.rollback()
        checks['timeout_single_call']=len([x for x in calls if x['phase']=='timeout'])==1
        delivery,token=new_claim('paused')
        lifecycle=LifecycleService(api.store,clock=lambda:WHEN)
        request=lifecycle.request(CUSTOMER);conn.commit()
        checks['lifecycle_blocks_outbound']=not outbound_allowed_in_store(api.store,1)
        checks['pending_request_suppresses_claim']=api.store.query('SELECT state FROM stage6_notification_outbox WHERE delivery_id=?',(delivery,))[0]['state']=='suppressed'
        checks['pause_keeps_destination']=api.store.query("SELECT count(*) AS n FROM stage6_destinations WHERE channel='push' AND revoked_at IS NULL")[0]['n']==1
        lifecycle.withdraw(CUSTOMER,request['request_id']);conn.commit()
        checks['withdrawal_reenables_future_outbound']=outbound_allowed_in_store(api.store,1)
        checks['withdrawal_never_requeues_old_delivery']=api.store.query('SELECT state FROM stage6_notification_outbox WHERE delivery_id=?',(delivery,))[0]['state']=='suppressed';conn.commit()
        before=len(calls)
        try:service(conn,timeout).dispatch_claim(STAFF,delivery,token);checks['suppressed_not_dispatched']=False
        except RegistryError as error:checks['suppressed_not_dispatched']=error.code=='notification_claim_invalid';conn.rollback()
        checks['pause_no_network']=len(calls)==before
        checks['lifecycle_immutable_events_present']=api.store.query('SELECT count(*) AS n FROM account_deletion_events')[0]['n']==2
        conn.commit()
    files=['stage6_common.py','stage6_notifications.py','stage6_external_transports.py','stage6_notifications_schema.py','stage6_web.py','account_lifecycle.py','scripts/verify_stage6_postgres_delivery.py']
    report=dict(scope='fresh fictional localhost PostgreSQL, captured transport only',checks=checks,
        check_count=len(checks),passed=all(checks.values()),provider_network_calls=0,
        captured_calls=len(calls),database=name,role_privileges='NOSUPERUSER NOCREATEDB NOCREATEROLE',
        source_sha256={file:hashlib.sha256((ROOT/file).read_bytes()).hexdigest() for file in files})
    Path(report_path).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    return {'checks':len(checks),'failures':[key for key,value in checks.items() if not value],'provider_network_calls':0,'report':str(report_path)}


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--context',required=True);parser.add_argument('--report',required=True)
    args=parser.parse_args()
    result=verify(read_context(args.context),args.report)
    print(json.dumps(result,ensure_ascii=False))
    raise SystemExit(bool(result['failures']))
