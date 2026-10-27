"""Fictional Stage2-7 PG race checks, called only by the owned recovery drill.

No environment discovery, app import, external adapters, or existing database.
All contenders use independent connections and actual domain transactions.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime
from threading import Barrier

from item_registry import Actor, Registry, RegistryError
from stage3_funds import FundsService
from stage4_catalog import CatalogService
from stage6_common import Stage6Store
from stage5_payments import PhysicalPaymentService, MemoryTestPSP
from stage6_notifications import NotificationService
from stage6_transports import MemoryNotificationTransport
from scripts.verify_stage7_recovery import STAFF, CUSTOMER, WHEN
from scripts import platform_recovery as recovery


def verify_races(connection, identifiers, *, connect):
    from psycopg2.extensions import parse_dsn
    params=parse_dsn(connection.dsn)
    recovery.require(params.get('host')=='127.0.0.1'
        and params.get('dbname','').startswith('kaika_fixture_source_'),
        'Race checks require the newly owned fictional localhost source.')
    with connection.cursor() as cursor:
        cursor.execute('SELECT rolsuper,rolcreatedb,rolcreaterole FROM pg_roles WHERE rolname=current_user')
        recovery.require(cursor.fetchone()==(False,False,False), 'Use the restricted fixture owner.')
    store=Stage6Store(Registry(connection,'postgres'))
    clock=lambda:datetime.fromisoformat(WHEN)
    catalog=CatalogService(store,clock=clock)
    item=catalog.create_company_item(STAFF,title='架空の競合検証品',occurred_at=WHEN,
        evidence_reference='fictional-only',source_key='stage8-race-item')['physical_item_id']
    assignment=store.registry._roles(item)['owner']['assignment_id']
    store.execute('INSERT INTO stage4_media VALUES(?,?,?,?,?,?,?,?)',
        ('stage8-race-photo',item,assignment,'fictional-race-photo','b'*64,1,9,WHEN))
    revision=catalog.revise(STAFF,item,title='架空の競合検証品',description='架空説明',condition='中古',
        price_yen=100,photo_refs=['stage8-race-photo'],occurred_at=WHEN,evidence_reference='fictional-only',
        source_key='stage8-race-revise')['revision_id']
    catalog.publish(STAFF,item,revision,occurred_at=WHEN,evidence_reference='fictional-only',source_key='stage8-race-publish')
    for customer in (1,2):
        FundsService(store.finance).post(STAFF,customer,kind='deposit',amount_yen=100,
            occurred_at=WHEN,evidence_reference='fictional-only',source_key='stage8-race-fund-'+str(customer))
    connection.commit()

    def race(callback):
        barrier=Barrier(2)
        def worker(index):
            with closing(connect()) as conn:
                with conn.cursor() as cursor:
                    cursor.execute("SET statement_timeout='15s'; SET lock_timeout='10s'")
                conn.commit()
                local=Stage6Store(Registry(conn,'postgres'))
                barrier.wait(timeout=10)
                try:
                    result=callback(local,index)
                    conn.commit()
                    return {'ok':True,'result':result}
                except RegistryError as error:
                    conn.rollback()
                    return {'ok':False,'code':error.code}
        with ThreadPoolExecutor(max_workers=2) as pool:
            return list(pool.map(worker,(0,1)))

    orders=race(lambda local,index:CatalogService(local,clock=clock).reserve(Actor(index+1),item,revision,
        custody_choice='kaika',consent_reference='fictional-only',occurred_at=WHEN,source_key='stage8-race-order-'+str(index)))
    checks={
        'two_customers_one_reservation':sum(result['ok'] for result in orders)==1,
        'loser_receives_unavailable':any(result.get('code')=='catalog_item_unavailable' for result in orders),
        'single_physical_claim':store.query('SELECT count(*) AS n FROM stage4_purchase_claims WHERE physical_item_id=?',(item,))[0]['n']==1,
        'single_funds_reservation':store.query("SELECT count(*) AS n FROM stage3_fund_records WHERE physical_item_id=? AND kind='reserve'",(item,))[0]['n']==1,
    }
    # Retire the fixture's original attempt via the exact trusted memory adapter.
    # Then race two NEW attempt IDs against the same bill. No real PSP is used.
    adapter=MemoryTestPSP();adapter.next_payment_status='canceled'
    service=PhysicalPaymentService(store,adapter)
    service.dispatch_attempt(CUSTOMER,identifiers['attempt'],source_key='stage8-cancel-fixture-attempt')
    consent=store.query('SELECT consent_id FROM stage5_payment_attempts WHERE attempt_id=?',(identifiers['attempt'],))[0]['consent_id']
    connection.commit()
    attempts=race(lambda local,index:PhysicalPaymentService(local,adapter).create_attempt(CUSTOMER,
        identifiers['bill'],consent,source_key='stage8-race-attempt-'+str(index)))
    checks['one_new_payment_attempt']=sum(result['ok'] for result in attempts)==1
    checks['second_attempt_requires_reconciliation']=any(result.get('code')=='payment_attempt_exists_reconcile_first' for result in attempts)
    winner=next((result['result']['attempt_id'] for result in attempts if result['ok']),None)
    if winner is not None:
        before=adapter.create_calls;adapter.next_payment_status='succeeded'
        dispatched=race(lambda local,index:PhysicalPaymentService(local,adapter).dispatch_attempt(CUSTOMER,winner,
            source_key='stage8-race-dispatch-'+str(index)))
        checks['memory_psp_single_object']=adapter.create_calls-before==1
        checks['payment_final_state_succeeded']=service.get_attempt(CUSTOMER,winner)['state']=='succeeded'
        checks['payment_race_no_unexpected_rejection']=all(result['ok'] for result in dispatched)
    else:
        checks['memory_psp_single_object']=False
    # Dispatch refuses a connection with pending reads/writes. Dedicated idle
    # connection dispatch is covered by verify_stage6_postgres_delivery.py.
    delivery=store.query("SELECT delivery_id FROM stage6_notification_outbox WHERE state='queued' AND notification_id IS NOT NULL ORDER BY delivery_id LIMIT 1")[0]['delivery_id']
    connection.commit()
    def notifications(local):
        return NotificationService(local,transports={'push':MemoryNotificationTransport()},clock=lambda:1792630800,
            validator=lambda actor,event:True,destination_validator=lambda destination:True,monthly_limits={'push':100})
    claims=race(lambda local,index:notifications(local).claim(STAFF,delivery,source_key='stage8-race-claim-'+str(index)))
    checks['one_notification_claim']=sum(result['ok'] for result in claims)==1
    checks['second_worker_cannot_claim']=any(result.get('code')=='notification_not_claimable' for result in claims)
    checks['single_claim_audit']=store.query("SELECT count(*) AS n FROM stage6_delivery_audit WHERE delivery_id=? AND state='claimed'",(delivery,))[0]['n']==1
    token=next((result['result'].get('claim_token') for result in claims if result['ok']),None)
    try:
        notifications(store).dispatch_claim(STAFF,delivery,token)
        checks['postgres_notification_pending_transaction_rejected']=False
    except RegistryError as error:
        checks['postgres_notification_pending_transaction_rejected']=error.code=='notification_claim_commit_required'
    connection.rollback()
    return checks
