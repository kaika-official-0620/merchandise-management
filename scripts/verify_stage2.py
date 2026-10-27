"""Real render_app stage-2 workflow, isolated source copy and fake data only.

Uses the existing preview boundary before importing application code. No real
database, uploads, secrets, networking, PSP, scheduler or device is involved.
The temporary JSON report contains checks and HTTP status codes, not passwords.
"""
from __future__ import annotations

from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timedelta, timezone
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
import time
import traceback
from urllib.parse import urlsplit

import run_app_preview as sandbox


class Forms(HTMLParser):
    def __init__(self, response):
        super().__init__()
        self.forms = []
        self.current = None
        self.feed(response.get_data(as_text=True))

    def handle_starttag(self, tag, attrs):
        attr = dict(attrs)
        if tag == 'form':
            self.current = {'action': attr.get('action', ''), 'fields': {}}
            self.forms.append(self.current)
        elif tag == 'input' and self.current is not None and attr.get('name') and attr.get('type') == 'hidden':
            self.current['fields'][attr['name']] = attr.get('value', '')

    def handle_endtag(self, tag):
        if tag == 'form':
            self.current = None

    def fields(self, action=None):
        matches = [f for f in self.forms if action is None or urlsplit(f['action']).path == action]
        return dict(matches[0]['fields']) if matches else {}


def run(module, users, preview):
    app = module.app
    app.config['PROPAGATE_EXCEPTIONS'] = True
    app.logger.setLevel(logging.ERROR)
    base = 'http://127.0.0.1:18781'
    clients = {role: app.test_client() for role in ('owner', 'other', 'staff', 'anonymous')}
    checks, requests = [], []

    def check(name, condition):
        checks.append({'name': name, 'pass': bool(condition)})

    def request(role, path, method='GET', data=None):
        response = clients[role].open(path, method=method, data=data, base_url=base,
            headers={'User-Agent': 'Mozilla/5.0 Stage2Fixture/1.0'})
        response.get_data()
        requests.append({'role': role, 'method': method, 'path': path, 'status': response.status_code})
        return response

    def form(role, page, action=None):
        response = request(role, page)
        check('opens form ' + role + ' ' + page, response.status_code == 200)
        return Forms(response).fields(action)

    def query(sql, params=()):
        conn = module.get_db()
        try:
            return [dict(row) for row in conn.execute(sql, params).fetchall()]
        finally:
            conn.close()

    def row(sql, params=()):
        result = query(sql, params)
        return result[0] if result else None

    def financial_counts():
        tables = query("SELECT name FROM sqlite_master WHERE type='table'")
        allowed = {'user_invoices', 'user_keisan', 'admin_kaitori', 'admin_shikiriosho', 'business_charge_drafts',
                   'business_fund_records', 'monthly_settlements', 'invoice_payments', 'payment_transactions'}
        snapshot = {}
        for table in tables:
            name = table['name']
            if name not in allowed:
                continue
            # Stable per-row serialization also detects in-place edits, while
            # query-order changes alone cannot cause a false mutation report.
            rows = query('SELECT * FROM ' + name)
            serialized = sorted(json.dumps(r, sort_keys=True, ensure_ascii=False,
                                           separators=(',', ':'), default=str) for r in rows)
            snapshot[name] = {'count': len(rows), 'sha256': hashlib.sha256(
                json.dumps(serialized, ensure_ascii=False, separators=(',', ':')).encode('utf-8')).hexdigest()}
        return snapshot

    # Free basic management must work without a digital subscription/grant.
    with module.get_db() as conn:
        conn.execute('DELETE FROM feature_manual_grants WHERE user_id=?', (users['normal']['id'],))
        conn.commit()
    for role, account in [('owner', 'normal'), ('other', 'business'), ('staff', 'admin')]:
        user = users[account]
        response = request(role, '/login', 'POST', {'username': user['username'], 'password': user['password']})
        check(role + ' real login succeeds', response.status_code == 302 and '/login' not in (response.location or ''))
    baseline_money = financial_counts()
    check('anonymous cannot open registry', request('anonymous', '/inventory/items').status_code in (302, 401))
    check('customer cannot open staff import', request('owner', '/inventory/imports').status_code == 403)

    name = '架空・段階2無料自己管理の商品'
    new_tokens = form('owner', '/inventory/self/new')
    response = request('owner', '/inventory/self/new', 'POST', {**new_tokens, 'product_name': name,
        'purchase_price': '30000', 'photo': (io.BytesIO((preview / 'site/static/uploads/preview/sample-0.png').read_bytes()), 'fixture.png')})
    legacy = row('SELECT * FROM merchandise WHERE product_name=?', (name,))
    check('free self registration persists with own custody', response.status_code == 302 and legacy and
          legacy['custody_location'] == 'self' and legacy['user_id'] == users['normal']['id'])
    if not legacy:
        return finish(checks, requests)
    link = row('SELECT physical_item_id FROM business_item_links WHERE merchandise_id=?', (legacy['id'],))
    check('same transaction links one physical item', bool(link))
    if not link:
        return finish(checks, requests)
    item_id = link['physical_item_id']
    detail = '/inventory/items/' + item_id
    check('owner can read physical item', request('owner', detail).status_code == 200)
    other = request('other', detail)
    check('other customer cannot read physical item or title', other.status_code in (403, 404) and name not in other.get_data(as_text=True))
    check('self registration creates no staff work', query('SELECT count(*) AS n FROM business_work_records')[0]['n'] == 0)
    photo = '/static/uploads/' + legacy['photo_path'].removeprefix('uploads/')
    check('owner can read compressed private photo', request('owner', photo).status_code == 200)
    check('another customer cannot read private photo', request('other', photo).status_code in (403, 404))
    for path, payload in [('/edit/' + str(legacy['id']), {'product_name': 'unsafe overwrite', 'item_status': 'sold'}),
                          ('/admin/items/' + str(legacy['id']) + '/transfer', {'target_user_id': str(users['business']['id'])}),
                          ('/admin/items/transfer-bulk', {'item_ids': str(legacy['id']), 'target_user_id': str(users['business']['id'])})]:
        response = request('staff', path, 'POST', payload)
        check('unsynchronized legacy mutation held ' + path, response.status_code == 409)
    unchanged = row('SELECT product_name,user_id,sale_date FROM merchandise WHERE id=?', (legacy['id'],))
    check('legacy mutation guard keeps title owner and sale unchanged', unchanged ==
          {'product_name': name, 'user_id': users['normal']['id'], 'sale_date': None})

    check('free basic access does not silently grant Kaika intake', request('owner', '/inventory/intakes/new').status_code == 403)
    # Explicit disposable entitlement is a fixture, not a payment or approved
    # production tariff. It isolates the physical-service workflow test.
    with module.get_db() as conn:
        conn.execute('INSERT INTO feature_manual_grants VALUES(?,?,?)',
                     (users['normal']['id'], 'normal', int(time.time()) + 3600))
        conn.commit()

    intake_tokens = form('owner', '/inventory/intakes/new')
    response = request('owner', '/inventory/intakes/new', 'POST', {**intake_tokens, 'kind': 'transfer',
        'item_ids': str(legacy['id']), 'client_note': '架空の受領試験・実物は送付しません'})
    intake = row('SELECT * FROM inventory_intakes WHERE user_id=? ORDER BY id DESC LIMIT 1', (users['normal']['id'],))
    check('fixture entitlement permits transfer without premature receipt', response.status_code == 302 and intake and
          row('SELECT custody_location FROM merchandise WHERE id=?', (legacy['id'],))['custody_location'] == 'self')
    if not intake:
        return finish(checks, requests)
    intake_path = '/inventory/intakes/' + str(intake['id'])
    for role, payload in [('staff', {'action': 'approve', 'shipping_instructions': '架空の送付案内'}),
                          ('owner', {'action': 'ship', 'carrier': '架空便', 'tracking_number': 'STAGE2-FAKE'}),
                          ('staff', {'action': 'receive', 'received_count': '1'})]:
        response = request(role, intake_path, 'POST', {**form(role, intake_path), **payload})
        check('existing intake action ' + payload['action'], response.status_code == 302)
    profile = row('SELECT * FROM business_item_profiles WHERE physical_item_id=?', (item_id,))
    check('receipt preserves physical identity and records actual receipt', bool(profile and profile['received_at']) and
          profile['physical_state'] == 'received' and
          row('SELECT physical_item_id FROM business_item_links WHERE merchandise_id=?', (legacy['id'],))['physical_item_id'] == item_id)
    # The pre-existing custody guard intentionally refuses the legacy staff
    # editor while an item is at the customer's home. Once received, its GET
    # still works; only unsynchronized writes are held by the stage-2 guard.
    check('linked staff edit page remains readable after receipt', request('staff', '/edit/' + str(legacy['id'])).status_code == 200)

    # Staff may add the actual received item's photographs without reopening
    # the legacy editor, which can mutate unrelated sale/ownership fields.
    staff_photo_path = detail + '/photos'
    old_photos = row('SELECT photo_path,additional_photos FROM merchandise WHERE id=?', (legacy['id'],))
    photo_tokens = form('staff', staff_photo_path)
    response = request('staff', staff_photo_path, 'POST', {**photo_tokens,
        'photos': (io.BytesIO((preview / 'site/static/uploads/preview/sample-0.png').read_bytes()), 'staff-fixture.png')})
    new_photos = row('SELECT photo_path,additional_photos FROM merchandise WHERE id=?', (legacy['id'],))
    added_photos = json.loads(new_photos['additional_photos'] or '[]')
    check('staff adds a received item photo while retaining its original', response.status_code == 302 and
          new_photos['photo_path'] == old_photos['photo_path'] and
          len(added_photos) == len(json.loads(old_photos['additional_photos'] or '[]')) + 1)
    if added_photos:
        staff_photo_url = '/static/uploads/' + added_photos[-1].removeprefix('uploads/')
        check('owner can read the staff-added private photo', request('owner', staff_photo_url).status_code == 200)
        check('another customer cannot read the staff-added private photo', request('other', staff_photo_url).status_code in (403, 404))
    check('customers cannot open staff photo management', request('owner', staff_photo_path).status_code == 403 and
          request('other', staff_photo_path).status_code == 403)
    check('staff photo upload records size without fabricating work',
          query('SELECT count(*) AS n FROM stage2_photo_records WHERE merchandise_id=? AND measurement_status=? AND byte_size>0',
                (legacy['id'], 'measured'))[0]['n'] == 2 and
          query('SELECT count(*) AS n FROM business_work_records')[0]['n'] == 0)
    before_work = query('SELECT count(*) AS n FROM business_work_records')[0]['n']
    event_path = detail + '/events'
    for kind in ('inspection_completed', 'photographed', 'registration_completed'):
        tokens = form('staff', detail, event_path)
        response = request('staff', event_path, 'POST', {**tokens, 'kind': kind, 'occurred_at': datetime.now(timezone.utc).isoformat(),
                                                       'note': '架空試験の確認結果:' + kind})
        check('staff records ' + kind, response.status_code == 302)
    profile = row('SELECT * FROM business_item_profiles WHERE physical_item_id=?', (item_id,))
    check('registration completion is explicit and has no guessed deadline', bool(profile['registration_completed_at']) and profile['deadline_at'] is None)
    check('state records do not fabricate work minutes', query('SELECT count(*) AS n FROM business_work_records')[0]['n'] == before_work)
    work_path = detail + '/work'
    work_tokens = form('staff', detail, work_path)
    work_payload = {**work_tokens, 'kind': 'photography', 'performed_at': datetime.now(timezone.utc).isoformat(),
                    'duration_minutes': '1.5', 'note': '架空試験・実測入力90秒'}
    response = request('staff', work_path, 'POST', work_payload)
    check('staff work records measured integer seconds', response.status_code == 302 and
          row('SELECT duration_seconds FROM business_work_records ORDER BY performed_at DESC LIMIT 1')['duration_seconds'] == 90)
    repeated = request('staff', work_path, 'POST', work_payload)
    check('same work submission is idempotent', repeated.status_code == 302 and
          query('SELECT count(*) AS n FROM business_work_records')[0]['n'] == before_work + 1)
    missing_csrf = request('staff', work_path, 'POST', {'kind': 'photography'})
    check('work requires actor CSRF token', missing_csrf.status_code == 400)
    check('customer cannot create staff work', request('owner', work_path, 'POST', work_payload).status_code == 403)

    # Returning an unsold customer's item must resume their basic self
    # management on the same record, without inventing another physical item.
    for kind in ('return_shipped', 'return_received'):
        tokens = form('staff', detail, event_path)
        response = request('staff', event_path, 'POST', {**tokens, 'kind': kind,
            'occurred_at': datetime.now(timezone.utc).isoformat(), 'note': '架空試験・顧客への返送確認:' + kind})
        check('staff records synchronized ' + kind, response.status_code == 302)
    returned = row('SELECT custody_location FROM merchandise WHERE id=?', (legacy['id'],))
    profile = row('SELECT physical_state FROM business_item_profiles WHERE physical_item_id=?', (item_id,))
    check('return receipt restores self custody on the same physical item',
          returned['custody_location'] == 'self' and profile['physical_state'] == 'with_customer' and
          row('SELECT physical_item_id FROM business_item_links WHERE merchandise_id=?', (legacy['id'],))['physical_item_id'] == item_id and
          query('SELECT count(*) AS n FROM self_inventory_items WHERE merchandise_id=? AND user_id=?',
                (legacy['id'], users['normal']['id']))[0]['n'] == 1)
    self_edit_path = '/inventory/self/' + str(legacy['id']) + '/edit'
    response = request('owner', self_edit_path, 'POST', {**form('owner', self_edit_path),
        'product_name': name, 'purchase_price': '30000', 'notes': '架空試験・返送後に自分で管理を再開'})
    check('customer can resume self editing after physical return', response.status_code == 302 and
          row('SELECT notes FROM merchandise WHERE id=?', (legacy['id'],))['notes'] == '架空試験・返送後に自分で管理を再開')
    check('another customer cannot edit the returned item', request('other', self_edit_path).status_code == 404)

    source = '外部キー,商品名,仕入額,入庫日,登録完了日\nHTTP-1,架空の既存取込商品,28000,,\n'
    before_import_work = query('SELECT count(*) AS n FROM business_work_records')[0]['n']

    def upload(content):
        payload = form('staff', '/inventory/imports')
        return request('staff', '/inventory/imports', 'POST', {**payload, 'customer_id': str(users['normal']['id']),
            'source_namespace': 'stage2-http-fixture', 'file': (io.BytesIO(content.encode('utf-8-sig')), 'fixture.csv')})

    response = upload(source)
    check('CSV upload only creates a preview', response.status_code == 302 and
          row('SELECT id FROM merchandise WHERE product_name=?', ('架空の既存取込商品',)) is None)
    if response.status_code != 302:
        return finish(checks, requests)
    batch_path = urlsplit(response.location).path
    preview_page = request('staff', batch_path)
    check('CSV preview shows actual product name and hold policy', preview_page.status_code == 200 and
          '架空の既存取込商品' in preview_page.get_data(as_text=True))
    confirm_path = batch_path + '/confirm'
    confirm_payload = {**Forms(preview_page).fields(confirm_path), 'reviewed': 'yes'}
    response = request('staff', confirm_path, 'POST', confirm_payload)
    imported = row('SELECT * FROM merchandise WHERE product_name=?', ('架空の既存取込商品',))
    check('CSV confirm creates one historical row with correct owner', response.status_code == 302 and imported and imported['user_id'] == users['normal']['id'])
    repeated = request('staff', confirm_path, 'POST', confirm_payload)
    check('CSV confirmation replay cannot duplicate stock', repeated.status_code == 302 and
          query('SELECT count(*) AS n FROM merchandise WHERE product_name=?', ('架空の既存取込商品',))[0]['n'] == 1)
    response = upload(source + '\n')
    reimport_path = urlsplit(response.location).path if response.status_code == 302 else ''
    if reimport_path:
        p2 = request('staff', reimport_path)
        confirmation = reimport_path + '/confirm'
        result = request('staff', confirmation, 'POST', {**Forms(p2).fields(confirmation), 'reviewed': 'yes'})
        check('same source ID in another upload is skipped', result.status_code == 302 and
              query('SELECT count(*) AS n FROM merchandise WHERE product_name=?', ('架空の既存取込商品',))[0]['n'] == 1)
    else:
        check('same source ID in another upload is skipped', False)
    check('historical import creates no work or fabricated receipt',
          query('SELECT count(*) AS n FROM business_work_records')[0]['n'] == before_import_work and
          row('''SELECT p.received_at,p.registration_completed_at FROM business_item_profiles p JOIN business_item_links l
              ON l.physical_item_id=p.physical_item_id WHERE l.merchandise_id=?''', (imported['id'],)) ==
          {'received_at': None, 'registration_completed_at': None})
    check('customer cannot open another customer import batch', request('other', batch_path).status_code == 403)
    check('reconciliation view remains staff-only', request('staff', '/inventory/reconcile').status_code == 200 and
          request('owner', '/inventory/reconcile').status_code == 403)

    # The legacy auction patch reconciles expired auctions during GET and in a
    # scheduler. Exercise its actual closure, not only HTTP endpoint blocking.
    now = module.get_jst_now()
    # The legacy membership table is deliberately created lazily by its own
    # helper. Prepare it in this disposable fixture before inserting bidders.
    module.ensure_proxy_service_auction_user_table()
    with module.get_db() as conn:
        auction = conn.execute('''INSERT INTO proxy_service_settings
            (auction_name,page_title,start_datetime,end_datetime,sale_mode,is_public,updated_by)
            VALUES(?,?,?,?,?,?,?)''', ('架空・個品連動の停止確認', '架空',
            (now - timedelta(hours=2)).strftime('%Y-%m-%d %H:%M:%S'),
            (now - timedelta(minutes=1)).strftime('%Y-%m-%d %H:%M:%S'), 'auction', 1, users['admin']['id'])).lastrowid
        source_id = conn.execute('''INSERT INTO merchandise
            (user_id,scope,product_name,auction_id,show_in_proxy_service,purchase_price,listing_price,custody_location)
            VALUES(?,?,?,?,?,?,?,?)''', (users['normal']['id'], 'user', '架空・旧販売停止商品', auction, 1, 1000, 3000, 'kaika')).lastrowid
        conn.execute('UPDATE users SET proxy_service_budget=50000 WHERE id=?', (users['normal']['id'],))
        conn.execute('INSERT INTO proxy_service_auction_users(auction_id,user_id,is_enabled) VALUES(?,?,1)', (auction, users['normal']['id']))
        conn.execute('INSERT INTO proxy_service_bids(auction_id,merchandise_id,user_id,bidder_name,bid_amount) VALUES(?,?,?,?,?)',
                     (auction, source_id, users['normal']['id'], '架空入札者', 3000))
        conn.commit()
    response = request('staff', '/inventory/reconcile/link', 'POST', {
        **form('staff', '/inventory/reconcile', '/inventory/reconcile/link'),
        'merchandise_id': str(source_id), 'reviewed': 'yes', 'note': '架空・旧処理停止試験のための個品対応'})
    check('auction source is explicitly linked for guard test', response.status_code == 302 and
          row('SELECT physical_item_id FROM business_item_links WHERE merchandise_id=?', (source_id,)) is not None)
    from stage2_legacy_guard import LinkedInventoryMutationPending
    held = False
    try:
        module.reconcile_proxy_service_auction(auction, now=now, actor_user_id=users['admin']['id'], require_ended=True)
    except LinkedInventoryMutationPending:
        held = True
    check('actual background reconciliation refuses linked source before settlement', held)
    conn = module.get_db()
    try:
        held = False
        try:
            module.create_proxy_service_reflected_item(conn, {'id': source_id}, users['admin']['id'])
        except LinkedInventoryMutationPending:
            held = True
        check('actual old reflection helper refuses physical duplication', held)
    finally:
        conn.rollback()
        conn.close()
    check('proxy read page survives deferred background settlement', request('staff', '/admin/proxy-service/' + str(auction)).status_code == 200)
    check('proxy finalization POST is held before legacy handler', request('staff', '/admin/proxy-service/' + str(auction) + '/finalize', 'POST').status_code == 409)
    check('deferred auction leaves balance and source sale unchanged',
          row('SELECT proxy_service_budget FROM users WHERE id=?', (users['normal']['id'],))['proxy_service_budget'] == 50000 and
          row('SELECT sale_date FROM merchandise WHERE id=?', (source_id,))['sale_date'] is None)
    check('all workflows leave financial documents and balances unchanged', financial_counts() == baseline_money)
    check('payment SDK and scheduler remain disabled', not module.STRIPE_ENABLED and not module.SCHEDULER_ENABLED)
    return finish(checks, requests)


def finish(checks, requests):
    return {'verified_at': datetime.now(timezone.utc).isoformat(), 'scope': 'Copied render_app, explicit stage2 migration, real Web logins, disposable SQLite',
        'limitations': ['No real customer stock or workbook was read.', 'Not native-device, PostgreSQL, PSP, notification or store validation.'],
        'checks': checks, 'requests': requests, 'passed': sum(c['pass'] for c in checks),
        'failed': sum(not c['pass'] for c in checks), 'server_errors': sum(r['status'] >= 500 for r in requests)}


def main():
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    original = Path.cwd()
    preview = Path(tempfile.mkdtemp(prefix='kaika-stage2-verification-')).resolve()
    runtime = sandbox.mirror_source(preview, None, None)
    sandbox.isolate_environment(preview, 18781)
    connections = sandbox.install_runtime_boundary(preview, 18781)
    sys.path[:] = [str(runtime)] + [p for p in sys.path if p and not sandbox.inside(Path(p).resolve(), sandbox.SOURCE)]
    os.chdir(runtime)
    report = None
    diagnostics = io.StringIO()
    try:
        # Legacy startup can be noisy. Do not echo startup/account/debug values.
        with redirect_stdout(diagnostics), redirect_stderr(diagnostics):
            loaded = importlib.import_module('render_app')
            users = sandbox.seed_preview(loaded.module, preview)
            from stage2_migration import prepare_stage2_fixture
            prepare_stage2_fixture(loaded.module, allowed_test_path=preview / 'preview.sqlite3')
            loaded.module.app.config.update(STAGE2_INVENTORY_ENABLED=True, STAGE2_FREE_LIMIT_PROPOSAL_ENABLED=True)
            sandbox.install_preview_routes(loaded.module, preview, users, 18781)
            report = run(loaded.module, users, preview)
    except Exception as exc:
        report = {'scope': 'isolated stage2 integration', 'passed': 0, 'failed': 1, 'server_errors': 1,
                  'error_type': type(exc).__name__, 'error': str(exc), 'traceback': traceback.format_exc()}
    finally:
        for conn in connections:
            conn.close()
        os.chdir(original)
    if report['failed'] or report['server_errors']:
        report['isolated_runtime_diagnostics'] = diagnostics.getvalue()[-18000:]
    destination = preview / 'verification.json'
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    summary = {k: report.get(k) for k in ('passed', 'failed', 'server_errors', 'error_type', 'error')}
    summary['failed_checks'] = [c['name'] for c in report.get('checks', []) if not c['pass']]
    summary['requests'] = len(report.get('requests', []))
    summary['report_path'] = str(destination)
    print('STAGE2_VERIFICATION=' + json.dumps(summary, ensure_ascii=False))
    return 1 if report['failed'] or report['server_errors'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
