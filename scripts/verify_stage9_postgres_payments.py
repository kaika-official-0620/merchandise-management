"""Disposable localhost PostgreSQL + captured Stripe SDK payment checks.

Creates and always stops its OWN new OS-temp cluster. No runtime app imports,
real providers, existing databases, deployed settings, or actual charges.
The fake provider is deliberately not evidence of Stripe sandbox connectivity.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import secrets
import socket
import sys
import tempfile
import threading
import traceback

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))
from verify_postgres_recovery import prepare, read_context, fixture_connect, run, clean_environment
from platform_recovery import postgres_parameters, utc_now
from item_registry import Actor, Registry, RegistryError, _json
from item_registry_schema import migration_statements as registry_schema
from stage3_common import migration_statements as finance_schema
from stage3_billing_schema import migration_statements as billing_schema
from stage3_billing import BillingService, _catalog
from stage4_common import migration_statements as commerce_schema
from stage5_common import Stage5Store, migration_statements as common_schema
from stage5_payments_schema import migration_statements as payments_schema
from stage5_payments import PhysicalPaymentService
from stage9_payment_schema import statements as sandbox_schema, VERSION
from stage9_payments import SandboxPayments
from stage9_stripe import SandboxScope, StripeSandboxAdapter
from account_lifecycle import LifecycleService, migration_statements as lifecycle_schema, outbound_allowed_in_store
from test_stage9_payments import FakeSDK

STAFF, CUSTOMER, OTHER = Actor(9, True), Actor(1), Actor(2)
WHEN = '2026-10-26T00:00:00+09:00'


def verify(context, checks):
    from psycopg2 import sql
    from psycopg2.extensions import make_dsn
    name = 'kaika_fixture_source_' + secrets.token_hex(6)
    role = 'kaika_fixture_owner_' + secrets.token_hex(6)
    password = secrets.token_urlsafe(32)
    with closing(fixture_connect(context['admin_database_url'])) as admin:
        admin.autocommit = True
        with admin.cursor() as cur:
            cur.execute(sql.SQL('CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD %s').format(sql.Identifier(role)), (password,))
            cur.execute(sql.SQL("CREATE DATABASE {} OWNER {} TEMPLATE template0 ENCODING 'UTF8'").format(sql.Identifier(name), sql.Identifier(role)))
    dsn = make_dsn(**{**postgres_parameters(context['admin_database_url']), 'user': role, 'password': password, 'dbname': name})

    def connect():
        conn = fixture_connect(dsn)
        with conn.cursor() as cur:
            cur.execute("SET statement_timeout='15s'")
            cur.execute("SET lock_timeout='10s'")
        conn.commit()
        return conn

    local = threading.local()
    scope = SandboxScope('fixture-stage9-postgres-20261026', ((1, 'cus_fixture1'), (2, 'cus_fixture2')), 'https://sandbox.example.test')
    with closing(connect()) as conn:
        with conn.cursor() as cur:
            cur.execute('CREATE TABLE users(id BIGINT PRIMARY KEY,role TEXT,admin_permissions TEXT)')
            cur.execute('CREATE TABLE merchandise(id BIGINT PRIMARY KEY,user_id BIGINT,product_name TEXT)')
            cur.executemany('INSERT INTO users VALUES (%s,%s,%s)', [(1, 'user', None), (2, 'user', None), (9, 'owner', None)])
            statements = list(registry_schema('postgres')) + finance_schema('postgres') + billing_schema('postgres') + commerce_schema('postgres') + common_schema('postgres') + payments_schema('postgres')
            for statement in statements + sandbox_schema('postgres') + lifecycle_schema('postgres'):
                cur.execute(statement)
            # Re-apply the new DDL to establish that explicit migrations repeat safely.
            for statement in sandbox_schema('postgres'):
                cur.execute(statement)
            cur.execute("INSERT INTO stage9_payment_scope VALUES (%s,%s,'test')", (scope.sandbox_id, VERSION))
            cur.executemany('INSERT INTO stage9_payment_customers VALUES (%s,%s,1)', [(1, scope.sandbox_id), (2, scope.sandbox_id)])
            cur.execute('SELECT rolsuper,rolcreatedb,rolcreaterole FROM pg_roles WHERE rolname=current_user')
            checks['restricted_database_owner'] = cur.fetchone() == (False, False, False)
        conn.commit()
    checks['postgres_ddl_and_repeat_migration'] = True

    @contextmanager
    def db(write=False):
        conn = connect()
        local.inside = True
        try:
            yield Stage5Store(Registry(conn, 'postgres'))
            if write:
                conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
            local.inside = False

    io_checks = []
    def outside():
        good = not getattr(local, 'inside', False)
        io_checks.append(good)
        if not good:
            raise AssertionError('Provider fixture called within DB scope')

    sdk = FakeSDK(scope, outside)
    adapter = StripeSandboxAdapter(scope=scope, secret_key='sk_test_fixture123', webhook_secret='whsec_fixture123',
        sdk=sdk, allow_network=True, allow_writes=True, publishable_key='pk_test_fixture123')
    now = [1_000_000]
    def outbound(customer):
        with db() as store:
            return outbound_allowed_in_store(store, customer)
    api = SandboxPayments(db, scope, adapter, clock=lambda: now[0], outbound_allowed=outbound)

    def row(job):
        with db() as store:
            return store.query('SELECT * FROM stage9_payment_jobs WHERE job_id=?', (job,))[0]

    def error_code(action):
        try:
            action()
        except RegistryError as error:
            return error.code
        return 'no_error'

    def rejected_sql(statement, params=()):
        import psycopg2
        try:
            with db(True) as store:
                store.execute(statement, params)
        except psycopg2.DatabaseError:
            return True
        return False

    def bill(label, month, customer=1):
        # Explicit fictional approval, identical to existing test fixtures;
        # this does not approve any real contract or unknown fee component.
        with db(True) as store:
            evidence = dict(consent_reference='fixture-consent', signed_document_reference='fixture-contract',
                billing_timezone='Asia/Tokyo', billing_timezone_approved=True,
                rounding_scope='invoice_per_rate', rounding_method='floor')
            store.execute('''INSERT INTO stage3_agreements(agreement_id,customer_id,actor_user_id,recorded_at,
                evidence_json,catalog_json,status,source_key) VALUES (?,?,9,?,?,?,'fixture_approved',?)''',
                ('agreement-' + label, customer, WHEN, _json(evidence), _json(_catalog()), 'agreement-' + label))
            agreement = BillingService(store.finance)._load_agreement(customer, 'agreement-' + label)
            store.execute('''INSERT INTO stage3_charge_drafts(draft_id,customer_id,agreement_id,fee_kind,amount_net_yen,
                tax_rate_percent,tax_treatment,occurred_at,recorded_at,evidence_reference,snapshot_json,reasons_json,source_key,status)
                VALUES (?,?,?,'expense',1000,10,'exclusive',?,?,?,?,? ,?,'draft')''',
                (label, customer, 'agreement-' + label, month + '-22T00:00:00+09:00', WHEN, 'fixture-evidence',
                 _json(dict(agreement_snapshot=agreement, calculation={'rounding_method': 'floor'})), '[]', 'component-' + label))
            invoice = BillingService(store.finance).create_month(STAFF, customer, month, source_key='invoice-' + label)
            result = PhysicalPaymentService(store).prepare_invoice(STAFF, invoice['invoice_id'], review_reference='fixture', source_key='bill-' + label)
            return result['bill_id']

    observed_commits = []
    def committed_claim():
        outside()
        metadata = sdk.requests[-1][1]['metadata']
        # Capture SDK writes only after an independent PostgreSQL connection sees
        # the committed claim; the hook never calls Stripe or another provider.
        job = row(metadata.get('kaika_refund_id') or metadata.get('kaika_attempt_id') or metadata['kaika_consent_id'])
        observed_commits.append(job['state'] == 'sending' and bool(job['lease_token']) and job['first_sent_at'] is not None)
    sdk.on_create = committed_claim

    first_bill = bill('first', '2026-01')
    job = api.queue(CUSTOMER, 'checkout', bill_id=first_bill, source_key='checkout-first')['job_id']
    checks['invoice_tax_total_1100_yen'] = row(job)['amount_yen'] == 1100
    checks['queue_replay_returns_same_job'] = api.queue(CUSTOMER, 'checkout', bill_id=first_bill, source_key='checkout-first')['job_id'] == job
    checks['second_payment_for_bill_rejected'] = error_code(lambda: api.queue(CUSTOMER, 'checkout', bill_id=first_bill, source_key='checkout-other')) == 'sandbox_payment_exists_reconcile_first'
    checks['other_customer_denied'] = error_code(lambda: api.get(OTHER, job)) == 'customer_access_denied'

    barrier = threading.Barrier(2)
    def dispatch_worker(_):
        barrier.wait(timeout=10)
        try:
            return api.dispatch(CUSTOMER, job)['state']
        except RegistryError as error:
            return error.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(dispatch_worker, range(2)))
    checks['concurrent_workers_single_checkout'] = len(sdk.requests) == 1 and results.count('open') == 1 and all(x in ('open', 'sandbox_job_busy', 'sandbox_reconciliation_required') for x in results)
    checks['checkout_continuation_is_customer_only'] = api.continuation(CUSTOMER, job)['kind'] == 'redirect' and error_code(lambda: api.continuation(STAFF, job)) == 'customer_payment_action_required'
    sdk.finish(row(job)['provider_id'])
    checks['checkout_completion_retrieves_verified_latest'] = api.reconcile(CUSTOMER, job)['state'] == 'succeeded'
    checks['successful_payment_not_dispatched_again'] = error_code(lambda: api.dispatch(CUSTOMER, job)) == 'sandbox_reconciliation_required'

    obj = deepcopy(sdk.objects[row(job)['provider_id']])
    obj['status'] = 'expired'  # A stale event must not overwrite retrieved success.
    event = json.dumps(dict(id='evt_fixture_pg', object='event', livemode=False, data={'object': obj})).encode()
    api.accept_webhook(event, 'fixture-valid')
    checks['webhook_duplicate_deduplicated'] = api.accept_webhook(event, 'fixture-valid').get('duplicate') is True
    checks['stale_webhook_does_not_reverse_success'] = api.reconcile(CUSTOMER, job)['state'] == 'succeeded'
    changed = event.replace(b'expired', b'open')
    checks['event_id_changed_body_rejected'] = error_code(lambda: api.accept_webhook(changed, 'fixture-valid')) == 'stripe_event_content_changed'

    checks['immutable_request_amount'] = rejected_sql('UPDATE stage9_payment_jobs SET amount_yen=amount_yen+1 WHERE job_id=?', (job,))
    checks['append_only_payment_events'] = rejected_sql('DELETE FROM stage9_payment_events WHERE job_id=?', (job,))
    checks['append_only_webhooks'] = rejected_sql("DELETE FROM stage9_payment_webhooks WHERE event_id='evt_fixture_pg'")
    checks['append_only_scope'] = rejected_sql('DELETE FROM stage9_payment_scope')

    refund_barrier = threading.Barrier(2)
    def refund_worker(index):
        refund_barrier.wait(timeout=10)
        try:
            return api.queue(STAFF, 'refund', parent_job_id=job, amount_yen=700, reason='fixture-return', source_key='refund-' + str(index))
        except RegistryError as error:
            return {'error': error.code}
    with ThreadPoolExecutor(max_workers=2) as pool:
        refunds = list(pool.map(refund_worker, range(2)))
    accepted = [r['job_id'] for r in refunds if 'job_id' in r]
    checks['concurrent_refunds_reserve_original_amount'] = len(accepted) == 1 and len([r for r in refunds if r.get('error') == 'refund_exceeds_original_payment']) == 1
    if len(accepted) != 1:
        raise AssertionError('Refund reservation check failed')
    checks['refund_verified_succeeded'] = api.dispatch(STAFF, accepted[0])['state'] == 'succeeded'
    remainder = api.queue(STAFF, 'refund', parent_job_id=job, amount_yen=400, reason='fixture-remainder', source_key='refund-remainder')['job_id']
    checks['refund_remaining_exact_amount'] = api.dispatch(STAFF, remainder)['state'] == 'succeeded'
    checks['refund_beyond_full_payment_rejected'] = error_code(lambda: api.queue(STAFF, 'refund', parent_job_id=job, amount_yen=1, reason='fixture-excess', source_key='refund-excess')) == 'refund_exceeds_original_payment'

    lost_bill = bill('lost', '2026-02')
    lost = api.queue(CUSTOMER, 'checkout', bill_id=lost_bill, source_key='lost-checkout')['job_id']
    count = len(sdk.keys)
    sdk.raise_after = True
    checks['response_loss_persisted_unknown'] = api.dispatch(CUSTOMER, lost)['state'] == 'unknown'
    sdk.raise_after = False
    checks['unknown_not_automatically_retried'] = error_code(lambda: api.dispatch(CUSTOMER, lost)) == 'sandbox_reconciliation_required'
    checks['same_key_explicit_recovery_no_duplicate'] = api.dispatch(STAFF, lost, recover=True)['state'] == 'open' and len(sdk.keys) == count + 1

    setup = api.queue(CUSTOMER, 'setup', terms_reference='fixture-next-month-10th-revocable', source_key='setup')['job_id']
    api.dispatch(CUSTOMER, setup)
    sdk.finish(row(setup)['provider_id'])
    checks['setup_card_verified'] = api.reconcile(CUSTOMER, setup)['state'] == 'succeeded'
    off_bill = bill('offsession', '2026-03')
    off = api.queue(STAFF, 'off_session', bill_id=off_bill, consent_id=setup, source_key='offsession')['job_id']
    checks['consented_off_session_verified'] = api.dispatch(STAFF, off)['state'] == 'succeeded'
    revoke_bill = bill('revoke', '2026-04')
    revoke = api.queue(STAFF, 'off_session', bill_id=revoke_bill, consent_id=setup, source_key='revoke')['job_id']
    api.revoke_consent(CUSTOMER, setup)
    count = len(sdk.requests)
    checks['revoked_consent_suppresses_pending_charge'] = row(revoke)['state'] == 'suppressed' and error_code(lambda: api.dispatch(STAFF, revoke)) != 'no_error' and len(sdk.requests) == count
    checks['consent_revocation_cannot_be_undone'] = rejected_sql('UPDATE stage9_payment_consents SET revoked_at=NULL WHERE consent_id=?', (setup,))

    pending_bill = bill('delete', '2026-05')
    pending = api.queue(CUSTOMER, 'checkout', bill_id=pending_bill, source_key='delete-pending')['job_id']
    with db(True) as store:
        request = LifecycleService(store).request(CUSTOMER, source_key='fixture-delete')
    checks['deletion_suppresses_unsent_and_marks_sent_unknown'] = row(pending)['state'] == 'suppressed' and row(lost)['state'] == 'unknown'
    checks['deletion_preserves_success_and_refund_history'] = row(job)['state'] == 'succeeded' and row(accepted[0])['state'] == 'succeeded'
    count = len(sdk.requests)
    checks['deletion_blocks_future_payment_writes'] = error_code(lambda: api.dispatch(CUSTOMER, pending)) == 'sandbox_customer_not_allowed' and len(sdk.requests) == count
    with db(True) as store:
        LifecycleService(store).withdraw(CUSTOMER, request['request_id'])
        replay = LifecycleService(store).request(CUSTOMER, source_key='fixture-delete')
    checks['withdrawal_or_late_request_does_not_replay_payments'] = replay['state'] == 'withdrawn' and row(pending)['state'] == 'suppressed' and error_code(lambda: api.dispatch(CUSTOMER, pending)) == 'sandbox_customer_not_allowed'
    checks['append_only_deletion_history'] = rejected_sql('DELETE FROM account_deletion_events WHERE customer_id=1')
    checks['all_fake_provider_io_outside_db_scopes'] = bool(io_checks) and all(io_checks)
    checks['all_fake_provider_writes_have_committed_claim'] = bool(observed_commits) and all(observed_commits)
    return dict(fake_provider_create_calls=len(sdk.requests), fake_provider_unique_objects=len(sdk.keys),
        fake_provider_read_calls=len(sdk.reads), provider_network_calls=0, concurrent_dispatch_results=results,
        concurrent_refund_rejections=[r['error'] for r in refunds if 'error' in r])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pg-bin', required=True)
    parser.add_argument('--report', required=True)
    args = parser.parse_args()
    checks, details, context = {}, {}, None
    root = Path(tempfile.mkdtemp(prefix='kaika-stage9-payment-pg-')).resolve()
    error = None
    # The test SDK has no HTTP implementation. Also prohibit non-loopback Python
    # sockets so accidentally introduced provider code cannot leave this machine.
    def socket_guard(event, values):
        if event == 'socket.connect' and isinstance(values[1], tuple) and values[1][0] not in ('127.0.0.1', '::1'):
            raise RuntimeError('External sockets disabled for fictional PostgreSQL drill')
    sys.addaudithook(socket_guard)
    try:
        import psycopg2  # Check disposable-runtime dependency before starting PG.
        prepare(root, args.pg_bin)
        context = read_context(root / 'fixture-context.json')
        details = verify(context, checks)
    except Exception as caught:
        # Do not disclose database DSNs, fixture passwords, query parameters or
        # secret-bearing exception strings. Static filenames/lines are enough.
        error = {'type': type(caught).__name__, 'frames': [dict(file=Path(f.filename).name, line=f.lineno) for f in traceback.extract_tb(caught.__traceback__)]}
    finally:
        # prepare may have started the owned cluster before it raised.
        if context is None and (root / 'fixture-context.json').is_file():
            context = read_context(root / 'fixture-context.json')
        if context:
            try:
                run([str(Path(context['pg_bin']) / 'pg_ctl.exe'), 'stop', '--pgdata=' + context['cluster'], '--mode=fast', '--wait', '--timeout=30'], env=clean_environment())
                with socket.socket() as probe:
                    probe.settimeout(1)
                    checks['owned_cluster_stopped_and_port_closed'] = not (Path(context['cluster']) / 'postmaster.pid').exists() and probe.connect_ex(('127.0.0.1', context['port'])) != 0
            except Exception:
                checks['owned_cluster_stopped_and_port_closed'] = False
    source_files = ['scripts/verify_stage9_postgres_payments.py', 'scripts/verify_postgres_recovery.py', 'tests/test_stage9_payments.py',
        'stage9_payment_schema.py', 'stage9_payments.py', 'stage9_stripe.py', 'stage5_payments.py', 'account_lifecycle.py']
    report = dict(date=utc_now(), scope='new OS-temp localhost PostgreSQL; fictional customers; captured fake Stripe SDK only',
        postgres_version=run([str(Path(args.pg_bin) / 'postgres.exe'), '--version']).strip(),
        tls='verify-full with disposable local CA', checks=checks, check_count=len(checks),
        failures=[name for name, passed in checks.items() if not passed], error=error, **details,
        production_data_accessed=False, application_started=False, real_stripe_connection_tested=False,
        windows_service_created=False, source_sha256={f:hashlib.sha256((ROOT/f).read_bytes()).hexdigest() for f in source_files})
    Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'checks':len(checks), 'failures':report['failures'], 'error':error, 'owned_cluster_stopped':checks.get('owned_cluster_stopped_and_port_closed',False)}, ensure_ascii=True))
    return 1 if error or report['failures'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
