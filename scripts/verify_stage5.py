"""Copied runtime, fictional contracts and HTTP forms with an offline PSP only.

No real data, provider SDK, environment file or network is available. Approved
agreements below are explicit test fixtures, never approval of customer terms.
"""
from contextlib import redirect_stdout, redirect_stderr
from html.parser import HTMLParser
import hashlib
import importlib
import io
import json
import logging
import os
from pathlib import Path
import sys
import tempfile
import traceback
from urllib.parse import urlsplit

import run_app_preview as sandbox


class Forms(HTMLParser):
    def __init__(self, response):
        super().__init__()
        self.forms, self.current = [], None
        self.feed(response.get_data(as_text=True))

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'form':
            self.current = {'action': attrs.get('action', ''), 'fields': {}}
            self.forms.append(self.current)
        elif tag == 'input' and self.current is not None and attrs.get('type') == 'hidden':
            self.current['fields'][attrs.get('name')] = attrs.get('value', '')

    def handle_endtag(self, tag):
        if tag == 'form':
            self.current = None

    def fields(self, action):
        return next((dict(form['fields']) for form in self.forms if urlsplit(form['action']).path == action), {})


def seed_invoices(module, customer_id, staff_id):
    from item_registry import Actor, Registry, _json
    from stage3_common import FinanceStore
    from stage3_billing import BillingService, _catalog
    from stage3_profit import ProfitService
    staff = Actor(staff_id, True)
    conn = module.get_db()
    try:
        conn.execute('PRAGMA foreign_keys=ON')
        conn.execute('BEGIN IMMEDIATE')
        registry = Registry(conn)
        store = FinanceStore(registry)
        billing, profit = BillingService(store), ProfitService(store)
        invoices = {}
        for month, name, approved in ((9, 'unapproved', False), (10, 'customer_payment', True), (11, 'automatic_payment', True)):
            def at(day):
                return f'2026-{month:02d}-{day:02d}T00:00:00+09:00'
            evidence = dict(contract_version_id='fictional-contract-' + name, fee_version_id='fictional-fee-' + name,
                signed_document_reference='fixture-only-signed', consent_reference='fixture-only-consent',
                consent_at=at(1), charge_start_at=at(10), exception_status='none', tax_rate_percent=10,
                rounding_method='floor', rounding_scope='invoice_per_rate', billing_timezone='Asia/Tokyo',
                billing_timezone_approved=True, work_scope_confirmed=True,
                work_kinds=['photography', 'registration', 'packing', 'dispatch'],
                approval_reference='fixture-only-approval', approved_at=at(1), effective_from=at(1),
                channel_authorization_references={'multichannel': 'fixture-only-authorization'}, sale_price_basis_confirmed=True)
            if approved:
                agreement = 'fixture-stage5-agreement-' + name
                conn.execute('''INSERT INTO stage3_agreements(agreement_id,customer_id,actor_user_id,recorded_at,
                    evidence_json,catalog_json,status,source_key) VALUES (?,?,?,?,?,?,'fixture_approved',?)''',
                    (agreement, customer_id, staff_id, at(1), _json(evidence), _json(_catalog()), agreement))
            else:
                agreement = billing.save_agreement(staff, customer_id, evidence=evidence,
                    source_key='fixture-stage5-unapproved')['agreement_id']
            item = registry.create_item(staff, '【架空】決済検証商品 ' + name, owner_user_id=customer_id,
                custodian='self', occurred_at=at(2), evidence_reference='fixture-stage5-item', source_key=name + ':item')
            item_id = item['physical_item_id']
            registry.record_event(staff, item_id, 'received', at(3), name + ':received', 'fixture-received')
            for kind, day in (('photography', 11), ('registration', 12)):
                registry.record_work(staff, item_id, kind, at(day), name + ':' + kind, 'fixture-work')
            registry.record_event(staff, item_id, 'sold', at(20), name + ':sold', 'fixture-sale')
            profit.record_facts(staff, item_id, amounts={'sale_gross_yen': 30000}, channel='multichannel',
                source_reference='fixture-stage5-sale-' + name, occurred_at=at(20), source_key=name + ':facts')
            registry.record_event(staff, item_id, 'shipped', at(21), name + ':shipped', 'fixture-shipped')
            billing.create_item_drafts(staff, item_id, source_key=name + ':charges', agreement_id=agreement)
            if name == 'customer_payment':
                # The physical adapter must exclude this unapproved digital
                # monthly proposal instead of charging a second monthly fee.
                billing.record_component(staff, customer_id, fee_kind='digital_monthly', amount_net_yen=2980,
                    tax_rate_percent=10, occurred_at=at(22), evidence_reference='fixture-digital-separate',
                    source_key=name + ':digital', agreement_id=agreement)
            invoices[name] = billing.create_month(staff, customer_id, f'2026-{month:02d}', source_key=name + ':month')['invoice_id']
        conn.commit()
        return invoices
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def run(module, users):
    from stage5_payments import MemoryTestPSP
    app = module.app
    app.config['PROPAGATE_EXCEPTIONS'] = True
    app.logger.setLevel(logging.ERROR)
    clients = {role: app.test_client() for role in ('customer', 'other', 'staff', 'anonymous')}
    checks, requests = [], []

    def check(name, condition):
        checks.append({'name': name, 'pass': bool(condition)})

    def http(role, path, data=None):
        response = clients[role].open(path, method='GET' if data is None else 'POST', data=data,
            base_url='http://127.0.0.1:18785', headers={'User-Agent': 'Mozilla/5.0 Stage5Fixture'})
        response.get_data()
        requests.append({'role': role, 'method': 'GET' if data is None else 'POST', 'path': path, 'status': response.status_code})
        return response

    def form(role, page, action):
        response = http(role, page)
        check('form page ' + role + ' ' + action, response.status_code == 200)
        fields = Forms(response).fields(action)
        check('protected form ' + role + ' ' + action, bool(fields.get('operation_token')) and bool(fields.get('registry_csrf')))
        return fields

    def post(name, role, page, action, values=None, expected=302):
        data = {**form(role, page, action), **(values or {})}
        response = http(role, action, data)
        check(name, response.status_code == expected)
        if response.status_code != expected:
            raise AssertionError(name + ': status ' + str(response.status_code) + ': ' + response.get_data(as_text=True)[-600:])
        return response, data

    def rows(sql, params=()):
        conn = module.get_db()
        try:
            return [dict(row) for row in conn.execute(sql, params)]
        finally:
            conn.close()

    def baseline():
        result = {}
        for table in ('user_invoices', 'invoice_payments', 'payment_transactions', 'monthly_settlements',
                      'business_charge_drafts', 'business_fund_records', 'stage3_fund_records', 'feature_plan_subscriptions'):
            if rows("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)):
                serialized = sorted(json.dumps(row, sort_keys=True, ensure_ascii=False, default=str) for row in rows('SELECT * FROM ' + table))
                result[table] = hashlib.sha256(json.dumps(serialized).encode()).hexdigest()
        return result

    def state(table, key, identifier):
        return rows('SELECT state FROM ' + table + ' WHERE ' + key + '=? ORDER BY sequence DESC', (identifier,))[0]['state']

    owner = users['normal']['id']
    try:
        for role, account in (('customer', 'normal'), ('other', 'business'), ('staff', 'admin')):
            user = users[account]
            response = http(role, '/login', {'username': user['username'], 'password': user['password']})
            check('real login ' + role, response.status_code == 302 and '/login' not in (response.location or ''))
        check('Stage5 default disabled', app.config.get('STAGE5_PAYMENTS_ENABLED') is not True)
        check('disabled payments inaccessible', http('staff', '/payments').status_code == 404)
        app.config['STAGE5_PAYMENTS_ENABLED'] = True
        check('anonymous login required', http('anonymous', '/payments').status_code in (302, 401))
        invoices = seed_invoices(module, owner, users['admin']['id'])
        before = baseline()
        check('no real provider auto-initialized', 'stage5_test_provider' not in app.extensions)
        check('missing customer CSRF rejected', http('customer', '/payments/consents', {'agree': 'yes'}).status_code == 400)
        check('staff cannot consent as customer', http('staff', '/payments/consents', {}).status_code in (400, 403))
        primary_invoice = '/payments/invoices/' + invoices['customer_payment']
        check('customer invoice visible', http('customer', primary_invoice).status_code == 200)
        check('other customer invoice forbidden', http('other', primary_invoice).status_code == 403)
        check('customer cannot approve invoice', http('customer', primary_invoice, {}).status_code == 403)
        check('staff missing prepare CSRF denied', http('staff', primary_invoice, {}).status_code == 400)
        unapproved = '/payments/invoices/' + invoices['unapproved']
        bad_page = http('staff', unapproved)
        check('unapproved contract visibly held', bad_page.status_code == 200 and ('要確認' in bad_page.get_data(as_text=True) or '未確定' in bad_page.get_data(as_text=True)))
        bad_fields = Forms(bad_page).fields(unapproved)
        if not bad_fields:
            # The UI intentionally hides a forbidden action. This adversarial
            # request still checks the handler's domain gate with a valid staff
            # session and signer; no approval API is invented.
            with clients['staff']:
                clients['staff'].get(unapproved, base_url='http://127.0.0.1:18785')
                service = app.extensions['stage5_payments']
                bad_fields = {'registry_csrf': service.csrf(), 'operation_token': service.operation('stage5_prepare', invoices['unapproved'])}
        rejected = http('staff', unapproved, {**bad_fields, 'review_reference': 'fixture-cannot-approve', 'status': 'approved', 'amount_yen': '1'})
        check('unapproved terms cannot create bill from HTTP', rejected.status_code == 409)
        check('unapproved creates no physical bill', not rows('SELECT bill_id FROM stage5_physical_bills'))

        response, prepare = post('reviewed fixture invoice prepared', 'staff', primary_invoice, primary_invoice,
            {'review_reference': 'fixture-physical-reviewed'})
        bill_path = urlsplit(response.location).path
        bill = rows('SELECT * FROM stage5_physical_bills')[0]
        check('digital monthly excluded from physical amount', bill['amount_yen'] == 1155)
        check('prepared snapshot excludes digital line', all(line['fee_kind'] != 'digital_monthly' for line in json.loads(bill['snapshot_json'])['lines']))
        check('prepare replay accepted', http('staff', primary_invoice, prepare).status_code == 302)
        check('prepare replay creates one bill', len(rows('SELECT bill_id FROM stage5_physical_bills')) == 1)
        check('other customer bill forbidden', http('other', bill_path).status_code == 403)
        page = http('customer', bill_path)
        check('bill explicitly test-only', '検証' in page.get_data(as_text=True) and ('1,155' in page.get_data(as_text=True) or '1155' in page.get_data(as_text=True)))
        check('private financial response', 'no-store' in page.headers.get('Cache-Control', '') and 'private' in page.headers.get('Cache-Control', ''))

        consent_values = {'agree': 'yes', 'customer_id': str(users['business']['id']), 'payment_method_reference': 'ignored', 'card_number': 'fixture-not-a-card'}
        _, consent_form = post('customer gives test payment consent', 'customer', '/payments', '/payments/consents', consent_values)
        consent = rows('SELECT * FROM stage5_card_consents')[0]
        check('customer identity from session', consent['customer_id'] == owner)
        check('default consent is not off-session', consent['off_session'] == 0)
        check('submitted card-like fields not stored', 'fixture-not-a-card' not in json.dumps(rows('SELECT * FROM stage5_card_consents')))
        check('consent replay accepted', http('customer', '/payments/consents', consent_form).status_code == 302)
        check('consent replay dedup', len(rows('SELECT consent_id FROM stage5_card_consents')) == 1)
        check('other customer consent hidden', consent['consent_id'] not in http('other', '/payments').get_data(as_text=True))
        staff_bill_page = http('staff', bill_path)
        check('staff form fixes automatic mode', Forms(staff_bill_page).fields(bill_path).get('off_session') == 'yes')
        check('ordinary consent not offered to staff', f'value="{consent["consent_id"]}"' not in staff_bill_page.get_data(as_text=True))
        post('staff cannot bypass automatic consent by submitting manual mode', 'staff', bill_path, bill_path,
            {'consent_id': consent['consent_id'], 'off_session': 'no'}, expected=403)
        response, attempt_form = post('customer starts own test payment', 'customer', bill_path, bill_path,
            {'consent_id': consent['consent_id'], 'amount_yen': '1', 'currency': 'USD', 'payment_scope': 'digital_subscription'})
        attempt_path = urlsplit(response.location).path
        attempt = rows('SELECT * FROM stage5_payment_attempts')[0]
        attempt_id = attempt['attempt_id']
        check('attempt uses immutable server amount and currency', attempt['amount_yen'] == 1155 and attempt['currency'] == 'JPY')
        check('attempt replay accepted', http('customer', bill_path, attempt_form).status_code == 302)
        check('one attempt after retry', len(rows('SELECT attempt_id FROM stage5_payment_attempts')) == 1)
        check('other customer attempt forbidden', http('other', attempt_path).status_code == 403)
        # No adapter is configured by ordinary environment or HTTP input.
        disconnected_page = http('customer', attempt_path)
        check('unconnected dispatch button hidden', not Forms(disconnected_page).fields(attempt_path + '/dispatch'))
        with clients['customer']:
            clients['customer'].get(attempt_path, base_url='http://127.0.0.1:18785')
            service = app.extensions['stage5_payments']
            dispatch_form = {'registry_csrf': service.csrf(), 'operation_token': service.operation('stage5_payment_dispatch', attempt_id)}
        check('unconnected payment cannot execute', http('customer', attempt_path + '/dispatch', dispatch_form).status_code == 409)
        check('unconnected attempt remains prepared', state('stage5_payment_events', 'attempt_id', attempt_id) == 'prepared')
        adapter = MemoryTestPSP()
        app.extensions['stage5_test_provider'] = adapter
        check('staff manual dispatch action hidden', not Forms(http('staff', attempt_path)).fields(attempt_path + '/dispatch'))
        with clients['staff']:
            clients['staff'].get(attempt_path, base_url='http://127.0.0.1:18785')
            service = app.extensions['stage5_payments']
            staff_dispatch = {'registry_csrf': service.csrf(), 'operation_token': service.operation('stage5_payment_dispatch', attempt_id)}
        check('staff cannot dispatch customer manual attempt', http('staff', attempt_path + '/dispatch', staff_dispatch).status_code == 403)
        check('denied staff dispatch never calls PSP', adapter.create_calls == 0)
        adapter.timeout_after_create = True
        _, dispatch_form = post('unknown provider outcome retained', 'customer', attempt_path, attempt_path + '/dispatch', {'status': 'succeeded'})
        check('timeout produces unknown state', state('stage5_payment_events', 'attempt_id', attempt_id) == 'unknown')
        check('provider accepted only one payment', adapter.create_calls == 1)
        check('dispatch replay accepted', http('customer', attempt_path + '/dispatch', dispatch_form).status_code == 302)
        check('unknown retry does not create another payment', adapter.create_calls == 1)
        adapter.timeout_after_create = False
        post('unknown outcome reconciles existing provider object', 'customer', attempt_path, attempt_path + '/reconcile')
        check('test authentication remains required', state('stage5_payment_events', 'attempt_id', attempt_id) == 'requires_action')
        http('customer', attempt_path + '?status=succeeded&payment_intent=forged')
        check('browser return cannot mark paid', state('stage5_payment_events', 'attempt_id', attempt_id) == 'requires_action')
        adapter.advance(attempt['provider_idempotency_key'], 'succeeded')
        _, reconcile = post('trusted test authentication reconciles successful payment', 'customer', attempt_path, attempt_path + '/reconcile')
        check('retrieved payment succeeds', state('stage5_payment_events', 'attempt_id', attempt_id) == 'succeeded')
        count = len(rows('SELECT event_id FROM stage5_payment_events'))
        check('success notification replay accepted', http('customer', attempt_path + '/reconcile', reconcile).status_code == 302)
        check('success replay no extra ledger event', len(rows('SELECT event_id FROM stage5_payment_events')) == count)
        post('staff can reconcile existing customer payment', 'staff', attempt_path, attempt_path + '/reconcile')
        post('paid bill prevents second payment intent', 'customer', bill_path, bill_path, {'consent_id': consent['consent_id']}, expected=409)
        check('one external test payment after retry', adapter.create_calls == 1)
        check('customer cannot refund', http('customer', attempt_path + '/refund', {}).status_code == 403)
        _, refund_form = post('staff requests partial test refund', 'staff', attempt_path, attempt_path + '/refund', {'amount_yen': '500', 'reason': 'fixture-partial'})
        refund = rows('SELECT * FROM stage5_payment_refunds')[0]
        check('refund replay accepted', http('staff', attempt_path + '/refund', refund_form).status_code == 302)
        check('refund retry no duplicate', len(rows('SELECT refund_id FROM stage5_payment_refunds')) == 1)
        refund_action = '/payments/refunds/' + refund['refund_id'] + '/dispatch'
        _, refund_dispatch = post('partial refund dispatched only in memory', 'staff', attempt_path, refund_action)
        check('partial refund success', state('stage5_refund_events', 'refund_id', refund['refund_id']) == 'succeeded')
        check('partial refund retry accepted', http('staff', refund_action, refund_dispatch).status_code == 302)
        check('provider sees one refund only', adapter.create_calls == 2)
        post('refund above remaining original amount blocked', 'staff', attempt_path, attempt_path + '/refund',
            {'amount_yen': '656', 'reason': 'fixture-over-refund'}, expected=409)
        post('remaining refund reserved', 'staff', attempt_path, attempt_path + '/refund', {'amount_yen': '655', 'reason': 'fixture-remainder'})
        remaining = next(row for row in rows('SELECT * FROM stage5_payment_refunds') if row['refund_id'] != refund['refund_id'])
        post('pending refund amount also prevents overrefund', 'staff', attempt_path, attempt_path + '/refund',
            {'amount_yen': '1', 'reason': 'fixture-over-pending'}, expected=409)
        adapter.next_refund_status = 'processing'
        post('pending refund remains pending', 'staff', attempt_path, '/payments/refunds/' + remaining['refund_id'] + '/dispatch')
        check('remaining refund processing', state('stage5_refund_events', 'refund_id', remaining['refund_id']) == 'processing')
        adapter.advance(remaining['provider_idempotency_key'], 'succeeded')
        post('pending refund reconciles separately', 'staff', attempt_path, '/payments/refunds/' + remaining['refund_id'] + '/reconcile')
        check('full refund sum equals original payment', sum(row['amount_yen'] for row in rows('SELECT * FROM stage5_payment_refunds')) == 1155)

        second_invoice = '/payments/invoices/' + invoices['automatic_payment']
        response, _ = post('second fictional month prepared', 'staff', second_invoice, second_invoice, {'review_reference': 'fixture-next-period'})
        second_bill_path = urlsplit(response.location).path
        post('staff cannot use ordinary consent off-session', 'staff', second_bill_path, second_bill_path,
            {'consent_id': consent['consent_id'], 'off_session': 'yes'}, expected=409)
        post('customer cannot initiate staff off-session action', 'customer', second_bill_path, second_bill_path,
            {'consent_id': consent['consent_id'], 'off_session': 'yes'}, expected=403)
        post('customer explicitly records test off-session consent', 'customer', '/payments', '/payments/consents', {'agree': 'yes', 'off_session': 'yes'})
        automatic = rows('SELECT * FROM stage5_card_consents WHERE off_session=1')[0]
        response, _ = post('staff starts consented off-session test', 'staff', second_bill_path, second_bill_path,
            {'consent_id': automatic['consent_id'], 'off_session': 'yes'})
        second_attempt_path = urlsplit(response.location).path
        second = rows('SELECT * FROM stage5_payment_attempts WHERE off_session=1')[0]
        check('customer automatic dispatch action hidden', not Forms(http('customer', second_attempt_path)).fields(second_attempt_path + '/dispatch'))
        adapter.next_payment_status = 'failed'
        post('payment failure retained for recovery', 'staff', second_attempt_path, second_attempt_path + '/dispatch')
        check('provider failure recorded', state('stage5_payment_events', 'attempt_id', second['attempt_id']) == 'failed')
        post('failure alone cannot start another charge', 'staff', second_bill_path, second_bill_path,
            {'consent_id': automatic['consent_id'], 'off_session': 'yes'}, expected=409)
        adapter.advance(second['provider_idempotency_key'], 'canceled')
        post('terminal cancellation reconciled', 'staff', second_attempt_path, second_attempt_path + '/reconcile')
        response, _ = post('new attempt only after verified cancellation', 'staff', second_bill_path, second_bill_path,
            {'consent_id': automatic['consent_id'], 'off_session': 'yes'})
        third_attempt_path = urlsplit(response.location).path
        post('customer revokes future automatic payment consent', 'customer', '/payments', '/payments/consents/' + automatic['consent_id'] + '/revoke', {'reason': 'fixture-revoked'})
        post('revocation blocks prepared automatic payment dispatch', 'staff', third_attempt_path, third_attempt_path + '/dispatch', expected=409)
        check('provider created no charge after revocation', adapter.create_calls == 4)
        check('legacy invoices funds and digital entitlements unchanged', baseline() == before)
        check('all physical rows remain test mode', all(row['execution_mode'] == 'test' for row in rows('SELECT execution_mode FROM stage5_physical_bills')))
        check('scheduler and Stripe disabled', not module.STRIPE_ENABLED and not module.SCHEDULER_ENABLED)
        app.config['STAGE5_PAYMENTS_ENABLED'] = False
        check('disabling payment screens works', http('customer', '/payments').status_code == 404)
        check('disabled attempt cannot execute', http('staff', third_attempt_path + '/dispatch', {}).status_code == 404)
        return dict(scope='Copied render_app with isolated fictional SQLite, authenticated forms, offline MemoryTestPSP only',
            checks=checks, requests=requests, passed=sum(row['pass'] for row in checks), failed=sum(not row['pass'] for row in checks),
            server_errors=sum(row['status'] >= 500 for row in requests),
            fixture_only=['two explicitly seeded approved agreements and one unapproved agreement', 'offline test PSP authentication/failure/refund transitions'],
            not_verified=['real PSP sandbox', 'live charges', 'store purchases and notifications', 'PostgreSQL concurrency', 'native devices', 'real customer consent'])
    except Exception as exc:
        return dict(scope='isolated Stage5 HTTP integration', checks=checks, requests=requests,
            passed=sum(row['pass'] for row in checks), failed=max(1, sum(not row['pass'] for row in checks)),
            server_errors=sum(row['status'] >= 500 for row in requests), error_type=type(exc).__name__, error=str(exc), traceback=traceback.format_exc())


def main():
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    original = Path.cwd()
    preview = Path(tempfile.mkdtemp(prefix='kaika-stage5-verification-')).resolve()
    runtime = sandbox.mirror_source(preview, None, None)
    sandbox.isolate_environment(preview, 18785)
    connections = sandbox.install_runtime_boundary(preview, 18785)
    sys.path[:] = [str(runtime)] + [path for path in sys.path if path and not sandbox.inside(Path(path).resolve(), sandbox.SOURCE)]
    os.chdir(runtime)
    diagnostics = io.StringIO()
    try:
        with redirect_stdout(diagnostics), redirect_stderr(diagnostics):
            loaded = importlib.import_module('render_app')
            users = sandbox.seed_preview(loaded.module, preview)
            from stage2_migration import prepare_stage2_fixture
            from stage3_migration import prepare_stage3_fixture
            from stage4_migration import prepare_stage4_fixture
            from stage5_migration import prepare_stage5_fixture
            for migrate in (prepare_stage2_fixture, prepare_stage3_fixture, prepare_stage4_fixture, prepare_stage5_fixture):
                migrate(loaded.module, allowed_test_path=preview / 'preview.sqlite3')
            loaded.module.app.config.update(STAGE2_INVENTORY_ENABLED=True, STAGE3_FINANCE_ENABLED=True, STAGE4_COMMERCE_ENABLED=True)
            report = run(loaded.module, users)
    except Exception as exc:
        report = dict(scope='isolated Stage5 integration', passed=0, failed=1, server_errors=1,
            error_type=type(exc).__name__, error=str(exc), traceback=traceback.format_exc())
    finally:
        for conn in connections:
            conn.close()
        os.chdir(original)
    if report['failed'] or report['server_errors']:
        report['isolated_runtime_diagnostics'] = diagnostics.getvalue()[-18000:]
    path = preview / 'verification.json'
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    summary = {key: report.get(key) for key in ('passed', 'failed', 'server_errors', 'error_type', 'error')}
    summary.update(failed_checks=[row['name'] for row in report.get('checks', []) if not row['pass']],
                   requests=len(report.get('requests', [])), report_path=str(path))
    print('STAGE5_VERIFICATION=' + json.dumps(summary, ensure_ascii=False))
    return 1 if report['failed'] or report['server_errors'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
