"""Owned disposable PostgreSQL: ownership/expense and event-order regressions.

No app import, environment DSN, existing cluster, customer data or external I/O.
Each contender has an independent restricted-owner connection. Only the newly
created localhost cluster is stopped by this process.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
import hashlib
import json
from pathlib import Path
import secrets
import socket
import sys
import tempfile
import threading
import time
import traceback
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from verify_postgres_recovery import prepare, read_context, fixture_connect, run, clean_environment
from platform_recovery import postgres_parameters, utc_now
from item_registry import Actor, Registry, RegistryError
from item_registry_schema import migration_statements as registry_schema
from stage3_common import FinanceStore, migration_statements as finance_schema
from stage3_billing import BillingService
from stage3_billing_schema import migration_statements as billing_schema
from stage3_profit import ProfitService
from stage3_profit_schema import migration_statements as profit_schema

STAFF, CUSTOMER, OTHER = Actor(9, True), Actor(1), Actor(2)
START = '2026-10-22T01:00:00+00:00'
SALE = '2026-10-22T02:00:00+00:00'
TRANSFER = '2026-10-22T03:00:00+00:00'
AFTER = '2026-10-22T04:00:00+00:00'
FROZEN = '2026-10-26T00:00:00.000000+00:00'


def verify(context, checks):
    from psycopg2 import sql
    from psycopg2.extensions import make_dsn
    role = 'kaika_fixture_owner_' + secrets.token_hex(6)
    database = 'kaika_fixture_source_' + secrets.token_hex(6)
    password = secrets.token_urlsafe(32)
    with closing(fixture_connect(context['admin_database_url'])) as admin:
        admin.autocommit = True
        with admin.cursor() as cur:
            cur.execute(sql.SQL('CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD %s').format(sql.Identifier(role)), (password,))
            cur.execute(sql.SQL("CREATE DATABASE {} OWNER {} TEMPLATE template0 ENCODING 'UTF8'").format(sql.Identifier(database), sql.Identifier(role)))
    dsn = make_dsn(**{**postgres_parameters(context['admin_database_url']), 'user':role, 'password':password, 'dbname':database})

    def connect():
        conn = fixture_connect(dsn)
        with conn.cursor() as cur:
            cur.execute("SET statement_timeout='15s'")
            cur.execute("SET lock_timeout='10s'")
        conn.commit()
        return conn

    @contextmanager
    def store(write=False):
        with closing(connect()) as conn:
            try:
                yield FinanceStore(Registry(conn, 'postgres'))
                if write:
                    conn.commit()
            except Exception:
                conn.rollback()
                raise

    with closing(connect()) as conn:
        with conn.cursor() as cur:
            cur.execute('CREATE TABLE users(id BIGINT PRIMARY KEY,role TEXT,admin_permissions TEXT)')
            cur.execute('CREATE TABLE merchandise(id BIGINT PRIMARY KEY,user_id BIGINT,product_name TEXT)')
            cur.executemany('INSERT INTO users VALUES (%s,%s,%s)', [(1,'user',None),(2,'user',None),(9,'owner',None)])
            for schema in (registry_schema, finance_schema, billing_schema, profit_schema):
                for statement in schema('postgres'):
                    cur.execute(statement)
            cur.execute('SELECT rolsuper,rolcreatedb,rolcreaterole FROM pg_roles WHERE rolname=current_user')
            checks['restricted_owner'] = cur.fetchone() == (False,False,False)
        conn.commit()
    checks['explicit_disposable_schema'] = True

    def item(label):
        with store(True) as local:
            return local.registry.create_item(STAFF, 'fictional-'+label, owner_user_id=1,
                occurred_at=START, evidence_reference='fictional-only', source_key='item-'+label)['physical_item_id']

    def expense(local, identifier, key, customer=1, when=AFTER, **extra):
        return BillingService(local).record_component(STAFF, customer, item_id=identifier,
            fee_kind=extra.pop('fee_kind','expense'), amount_net_yen=extra.pop('amount',500), tax_rate_percent=10,
            occurred_at=when, evidence_reference='fictional-'+key, source_key=key, **extra)

    def transfer(identifier, customer, key, when=TRANSFER):
        with store(True) as local:
            local.registry.change_assignment(STAFF, identifier, 'owner', 'customer', customer,
                occurred_at=when, evidence_reference='fictional-only', source_key=key)

    def code(action):
        try:
            action()
        except RegistryError as error:
            return error.code
        return 'not_rejected'

    # Pause after the actual initial ownership SELECT, allow another independent
    # transaction to commit, then resume the original business operation.
    def expense_transfer_race(identifier, roundtrip=False):
        selected, resume = threading.Event(), threading.Event()
        def writer():
            with store(True) as local:
                operation = local.operation
                def paused(actor, customer, key, payload, callback):
                    selected.set()
                    if not resume.wait(10):
                        raise AssertionError('Fixture coordination timed out')
                    return operation(actor, customer, key, payload, callback)
                local.operation = paused
                return code(lambda: expense(local,identifier,'racing-'+identifier))
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(writer)
            try:
                if not selected.wait(10):
                    raise AssertionError('Fixture initial read not reached')
                transfer(identifier,2,'transfer-'+identifier)
                if roundtrip:
                    transfer(identifier,1,'transferback-'+identifier,when='2026-10-22T03:30:00+00:00')
            finally:
                resume.set()
            return pending.result(timeout=15)

    for label,roundtrip in (('ownership-race',False),('ownership-roundtrip',True)):
        identifier = item(label)
        checks[label+'_rejected'] = expense_transfer_race(identifier,roundtrip) == 'financial_owner_changed'
        with store() as local:
            checks[label+'_no_charge'] = local.query('SELECT count(*) AS n FROM stage3_charge_drafts WHERE physical_item_id=?',(identifier,))[0]['n'] == 0

    identifier = item('historical-correction')
    with store(True) as local:
        original = expense(local,identifier,'historical-expense',when=SALE)['draft_id']
        original_snapshot = local.query('SELECT snapshot_json FROM stage3_charge_drafts WHERE draft_id=?',(original,))[0]['snapshot_json']
    transfer(identifier,2,'historical-transfer')
    with store(True) as local:
        checks['preownership_expense_rejected'] = code(lambda:expense(local,identifier,'preownership',customer=2,when=SALE)) == 'component_owner_period_mismatch'
        expense(local,identifier,'old-owner-adjustment',fee_kind='adjustment',amount=-100,original_draft_id=original)
        invoice = BillingService(local).create_month(STAFF,1,'2026-10',source_key='historical-month')['invoice_id']
        view = BillingService(local).get_month(CUSTOMER,invoice)
        checks['former_owner_can_review_own_correction'] = len(view['snapshot']['lines']) == 2 and all(line['customer_id']==1 for line in view['snapshot']['lines'])
        checks['new_owner_cannot_read_historical_invoice'] = code(lambda:BillingService(local).get_month(OTHER,invoice)) == 'customer_access_denied'
        checks['original_charge_unchanged'] = local.query('SELECT snapshot_json FROM stage3_charge_drafts WHERE draft_id=?',(original,))[0]['snapshot_json'] == original_snapshot
        checks['corrected_total_remains_original_customer'] = local.query('SELECT SUM(amount_net_yen) AS total FROM stage3_charge_drafts WHERE physical_item_id=? AND customer_id=1',(identifier,))[0]['total'] == 400

    # Use actual row-lock contention, not timing luck: the sale is uncommitted
    # until pg_stat_activity confirms the second connection waits on a lock.
    identifier = item('same-clock-sale')
    with store(True) as local:
        local.registry.record_event(STAFF,identifier,'received',START,'received-before-same-clock','fictional-only')
    sale_written, release_sale, ship_started = threading.Event(), threading.Event(), threading.Event()
    ship_pid = []
    def sell():
        with store(True) as local:
            local.registry.record_event(STAFF,identifier,'sold',SALE,'simultaneous-sale','fictional-only')
            sale_written.set()
            if not release_sale.wait(10):
                raise AssertionError('Fixture release timed out')
        return True
    def ship():
        with store(True) as local:
            ship_pid.append(local.connection.get_backend_pid())
            ship_started.set()
            local.registry.record_event(STAFF,identifier,'shipped',SALE,'simultaneous-shipment','fictional-only')
        return True
    with patch('item_registry._now',return_value=FROZEN):
        with ThreadPoolExecutor(max_workers=2) as pool:
            sale = pool.submit(sell)
            shipment = None
            try:
                if not sale_written.wait(10):
                    raise AssertionError('Fixture sale not reached')
                shipment = pool.submit(ship)
                if not ship_started.wait(10):
                    raise AssertionError('Fixture shipment not started')
                waiting = False
                until = time.monotonic()+5
                with closing(connect()) as observer:
                    observer.autocommit = True
                    while time.monotonic()<until:
                        with observer.cursor() as cur:
                            cur.execute('SELECT wait_event_type FROM pg_stat_activity WHERE pid=%s',(ship_pid[0],))
                            row = cur.fetchone()
                        if row and row[0]=='Lock':
                            waiting = True
                            break
                        time.sleep(.02)
                checks['second_event_waited_on_actual_postgres_lock'] = waiting
            finally:
                release_sale.set()
            checks['both_events_committed'] = sale.result(timeout=15) and shipment is not None and shipment.result(timeout=15)
    with store(True) as local:
        events = local.query("SELECT kind,occurred_at,recorded_at FROM business_item_observations WHERE physical_item_id=? AND kind IN ('sold','shipped') ORDER BY recorded_at",(identifier,))
        checks['equal_actual_time_preserves_sale_then_shipment'] = [row['kind'] for row in events] == ['sold','shipped'] and len({row['occurred_at'] for row in events})==1
        checks['recording_order_strictly_increases_with_frozen_clock'] = events[0]['recorded_at'] < events[1]['recorded_at']
        facts = ProfitService(local).record_facts(STAFF,identifier,amounts={'sale_gross_yen':30000},channel='multichannel',
            occurred_at=AFTER,source_reference='fictional-only',source_key='same-clock-facts')
        preview = BillingService(local).preview_item(STAFF,identifier)
        checks['fee_preview_uses_current_shipment'] = preview['shipment_event_id'] is not None and 'financial_event_order_unconfirmed' not in preview['lines'][0]['reason_codes']
        # Synthetic legacy row with exact duplicated event/recording timestamps.
        local.execute('''INSERT INTO business_item_observations VALUES
            ('legacy-ambiguous-return',?,'returned',?,?,9,1,'fictional-old-log','{}',NULL,'legacy-tie')''',
            (identifier,events[1]['occurred_at'],events[1]['recorded_at']))
        preview = BillingService(local).preview_item(STAFF,identifier)
        checks['legacy_ambiguous_order_holds_shipment'] = preview['shipment_event_id'] is None and 'financial_event_order_unconfirmed' in preview['lines'][0]['reason_codes']
        checks['legacy_ambiguous_order_has_no_charge_amount'] = all(line['amount_net_yen'] is None for line in preview['lines'])
        profit = ProfitService(local).profit(CUSTOMER,identifier)
        checks['legacy_ambiguous_order_cannot_realize_profit'] = profit['recognition']=='needs_review' and profit['realized_profit_yen'] is None
        checks['legacy_ambiguous_order_cannot_update_financial_evidence'] = code(lambda:ProfitService(local).record_facts(STAFF,identifier,
            amounts={'sale_gross_yen':40000},channel='multichannel',occurred_at=AFTER,source_reference='fictional-only',
            source_key='ambiguous-update',previous_version_id=facts['version_id'])) == 'financial_event_order_unconfirmed'
        checks['original_financial_evidence_preserved'] = len(ProfitService(local).history(CUSTOMER,identifier)) == 1
    return {'independent_connections':True,'external_provider_calls':0,'actual_postgres_row_lock_observed':checks['second_event_waited_on_actual_postgres_lock']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pg-bin',required=True)
    parser.add_argument('--report',required=True)
    args = parser.parse_args()
    checks,details,context,error = {},{},None,None
    root = Path(tempfile.mkdtemp(prefix='kaika-operations-pg-')).resolve()
    def socket_guard(event,values):
        if event=='socket.connect' and isinstance(values[1],tuple) and values[1][0] not in ('127.0.0.1','::1'):
            raise RuntimeError('External sockets disabled for fictional PostgreSQL drill')
    sys.addaudithook(socket_guard)
    try:
        import psycopg2
        prepare(root,args.pg_bin)
        context = read_context(root/'fixture-context.json')
        details = verify(context,checks)
    except Exception as caught:
        error = {'type':type(caught).__name__,'frames':[dict(file=Path(frame.filename).name,line=frame.lineno) for frame in traceback.extract_tb(caught.__traceback__)]}
    finally:
        if context is None and (root/'fixture-context.json').is_file():
            context = read_context(root/'fixture-context.json')
        if context:
            try:
                run([str(Path(context['pg_bin'])/'pg_ctl.exe'),'stop','--pgdata='+context['cluster'],'--mode=fast','--wait','--timeout=30'],env=clean_environment())
                with socket.socket() as probe:
                    probe.settimeout(1)
                    checks['owned_cluster_stopped_and_port_closed'] = not (Path(context['cluster'])/'postmaster.pid').exists() and probe.connect_ex(('127.0.0.1',context['port']))!=0
            except Exception:
                checks['owned_cluster_stopped_and_port_closed'] = False
    sources = ['scripts/verify_operations_postgres.py','scripts/verify_postgres_recovery.py','item_registry.py',
        'stage3_common.py','stage3_billing.py','stage3_profit.py']
    report = dict(date=utc_now(),scope='new owned OS-temp localhost PostgreSQL; synthetic data only',
        postgres_version=run([str(Path(args.pg_bin)/'postgres.exe'),'--version']).strip(),
        tls='verify-full with disposable local CA',checks=checks,check_count=len(checks),
        failures=[name for name,passed in checks.items() if not passed],error=error,**details,
        production_data_accessed=False,application_started=False,windows_service_created=False,
        source_sha256={name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in sources})
    Path(args.report).write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'checks':len(checks),'failures':report['failures'],'error':error,
        'owned_cluster_stopped':checks.get('owned_cluster_stopped_and_port_closed',False)}))
    return int(bool(error or report['failures']))


if __name__=='__main__':
    raise SystemExit(main())
