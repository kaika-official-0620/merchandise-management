"""Exercise independent PC/app HTTP sessions against a copied real site.

This verifies shared server data, not WebView rendering or a native device.
No original database, saved upload, credential or external service is used.
"""
from __future__ import annotations

from datetime import datetime, timezone
from html.parser import HTMLParser
import importlib
import io
import json
import logging
import os
from pathlib import Path
import sys
import tempfile

import run_app_preview as sandbox


class Tokens(HTMLParser):
    def __init__(self, response):
        super().__init__()
        self.values = {}
        self.feed(response.get_data(as_text=True))

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == 'input' and values.get('name') in {'csrf_token', 'submission_token', 'operation_token'}:
            self.values[values['name']] = values.get('value', '')


def run(module, users, preview):
    app = module.app
    app.config['PROPAGATE_EXCEPTIONS'] = True
    app.logger.setLevel(logging.ERROR)
    base = 'http://127.0.0.1:18779'
    clients = {name: app.test_client() for name in ('pc', 'app', 'staff', 'other')}
    checks, requests = [], []

    def check(name, condition):
        checks.append({'name': name, 'pass': bool(condition)})

    def request(actor, path, method='GET', data=None, json_body=None):
        headers = {'User-Agent': 'Mozilla/5.0 KaikaApp/0.1.0' if actor == 'app' else 'Mozilla/5.0 PCVerification/1.0'}
        response = clients[actor].open(path, method=method, data=data, json=json_body, headers=headers, base_url=base)
        response.get_data()
        requests.append({'actor': actor, 'method': method, 'path': path, 'status': response.status_code})
        response.close()
        return response

    def row(sql, params=()):
        connection = module.get_db()
        try:
            found = connection.execute(sql, params).fetchone()
            return dict(found) if found is not None else None
        finally:
            connection.close()

    def form(actor, path):
        response = request(actor, path)
        check(actor + ' form opens ' + path, response.status_code == 200)
        return Tokens(response).values

    def visible(actor, path, *markers):
        response = request(actor, path)
        text = response.get_data(as_text=True)
        check(actor + ' sees ' + ' / '.join(markers), response.status_code == 200 and all(marker in text for marker in markers))
        return response

    # Real login endpoint, separate cookie jars; no shared session injection.
    for actor, role in [('pc', 'business'), ('app', 'business'), ('staff', 'admin'), ('other', 'normal')]:
        user = users[role]
        response = request(actor, '/login', 'POST', {'username': user['username'], 'password': user['password']})
        check(actor + ' logs in with its own cookie jar', response.status_code == 302 and '/login' not in (response.location or ''))

    name = '架空・PC登録から開花販売まで'
    payload = form('pc', '/inventory/self/new')
    photo = (preview / 'site/static/uploads/preview/sample-0.png').read_bytes()
    response = request('pc', '/inventory/self/new', 'POST', {**payload, 'product_name': name,
        'purchase_price': '3000', 'listing_price': '7000', 'photo': (io.BytesIO(photo), 'fictional.png')})
    item = row('SELECT * FROM merchandise WHERE product_name=?', (name,))
    check('PC registration persists once with self custody and correct owner', response.status_code == 302 and item and
          item['custody_location'] == 'self' and item['user_id'] == users['business']['id'])
    if not item:
        return finish(checks, requests)
    item_id = item['id']
    detail = f'/view/{item_id}'
    visible('app', detail, name, '自己保管・未出品')
    photo_url = '/static/uploads/' + item['photo_path'].removeprefix('uploads/')
    pc_photo = request('pc', photo_url)
    app_photo = request('app', photo_url)
    check('PC and app read the same uploaded image bytes', pc_photo.status_code == app_photo.status_code == 200 and
          pc_photo.get_data() == app_photo.get_data() and bool(app_photo.get_data()))

    edit_path = f'/inventory/self/{item_id}/edit'
    pc_stale = form('pc', edit_path)
    app_edit = form('app', edit_path)
    response = request('app', edit_path, 'POST', {**app_edit, 'product_name': name,
        'purchase_price': '3000', 'listing_price': '7000', 'notes': '携帯側で編集した架空メモ'})
    check('App edit saves against the same item ID', response.status_code == 302)
    visible('pc', detail, '携帯側で編集した架空メモ')
    stale = request('pc', edit_path, 'POST', {**pc_stale, 'product_name': '古い画面からの上書き', 'purchase_price': '3000'})
    check('Stale PC edit cannot overwrite app changes', stale.status_code == 409 and
          row('SELECT product_name FROM merchandise WHERE id=?', (item_id,))['product_name'] == name)
    forbidden = request('other', detail)
    check('Another account cannot read the shared item', forbidden.status_code in {302, 403, 404} and name not in forbidden.get_data(as_text=True))

    intake_form = form('app', '/inventory/intakes/new')
    response = request('app', '/inventory/intakes/new', 'POST', {**intake_form, 'kind': 'transfer',
        'item_ids': str(item_id), 'client_note': '架空の連動確認・実物は発送しません'})
    intake = row("SELECT * FROM inventory_intakes WHERE user_id=? AND kind='transfer' ORDER BY id DESC LIMIT 1", (users['business']['id'],))
    check('App intake is a shared request without premature custody transfer', response.status_code == 302 and intake and
          row('SELECT custody_location FROM merchandise WHERE id=?', (item_id,))['custody_location'] == 'self')
    if not intake:
        return finish(checks, requests)
    intake_path = f"/inventory/intakes/{intake['id']}"
    visible('pc', intake_path, name)
    response = request('staff', intake_path, 'POST', {**form('staff', intake_path), 'action': 'approve',
        'shipping_instructions': '架空の発送案内・実物は発送しません'})
    check('Staff approves the same intake', response.status_code == 302)
    visible('app', intake_path, '架空の発送案内')
    response = request('app', intake_path, 'POST', {**form('app', intake_path), 'action': 'ship',
        'carrier': '架空配送', 'tracking_number': 'FICTIONAL-SYNC-ONLY'})
    check('App shipping report changes shared stock to transit', response.status_code == 302 and
          row('SELECT custody_location FROM merchandise WHERE id=?', (item_id,))['custody_location'] == 'transit')
    visible('pc', intake_path, 'FICTIONAL-SYNC-ONLY')
    response = request('staff', intake_path, 'POST', {**form('staff', intake_path), 'action': 'receive', 'received_count': '1'})
    received = row('SELECT * FROM merchandise WHERE id=?', (item_id,))
    check('Staff receipt preserves item ID owner and photos', response.status_code == 302 and
          received['custody_location'] == 'kaika' and received['user_id'] == item['user_id'] and
          received['photo_path'] == item['photo_path'] and bool(received['custody_received_at']))
    visible('pc', detail, '開花で保管・管理')
    visible('app', detail, '開花で保管・管理')

    response = request('app', '/sales-agency/apply', 'POST', {**form('app', '/'), 'service_type': 'wholesale',
        'merchandise_ids': str(item_id), 'next': '/sales-agency/my-requests'})
    agency = row('SELECT * FROM sales_agency_requests WHERE user_id=? ORDER BY id DESC LIMIT 1', (users['business']['id'],))
    check('App dealer request reaches the shared server', response.status_code == 302 and agency and agency['status'] == 'pending')
    if agency:
        process = f"/admin/sales-agency-requests/{agency['id']}/process"
        for action, expected in [('approve', 'approved'), ('appraising', 'appraising'), ('inspect', 'inspecting')]:
            response = request('staff', process, 'POST', {'action': action})
            check('Staff dealer action ' + action, response.status_code == 200 and
                  row('SELECT status FROM sales_agency_requests WHERE id=?', (agency['id'],))['status'] == expected)
        response = request('staff', process, 'POST', json_body={'action': 'complete', 'sale_price': 7000})
        sold = row('SELECT * FROM merchandise WHERE id=?', (item_id,))
        check('Dealer sale updates original stock and request', response.status_code == 200 and sold['sale_price'] == 7000 and
              bool(sold['sale_date']) and row('SELECT status FROM sales_agency_requests WHERE id=?', (agency['id'],))['status'] == 'completed')
        for actor in ('pc', 'app'):
            visible(actor, detail, name, '7,000')
            history_path = '/sales-agency/my-requests/' + module.sales_agency_service_slug('wholesale') + '?period=past'
            visible(actor, history_path, name)

    # Reverse direction: register and sell at home in the app, inspect on PC.
    self_name = '架空・携帯で登録して自己販売'
    response = request('app', '/inventory/self/new', 'POST', {**form('app', '/inventory/self/new'),
        'product_name': self_name, 'purchase_price': '2500'})
    own = row('SELECT * FROM merchandise WHERE product_name=?', (self_name,))
    check('App self registration persists', response.status_code == 302 and own and own['custody_location'] == 'self')
    if own:
        sale_path = f"/inventory/self/{own['id']}/sale"
        response = request('app', sale_path, 'POST', {**form('app', sale_path), 'action': 'sale',
            'sale_date': module.get_jst_now().date().isoformat(), 'sale_price': '6000', 'shipping_cost': '500',
            'commission': '600', 'other_cost': '100', 'sales_destination': '架空販売先', 'is_shipped': '1'})
        sold = row('SELECT * FROM merchandise WHERE id=?', (own['id'],))
        check('Self sale keeps home custody and all recorded actual costs', response.status_code == 302 and
              sold['custody_location'] == 'self' and sold['sale_price'] == 6000 and sold['shipping_cost'] == 500 and
              sold['commission'] == 600 and sold['other_cost'] == 100 and sold['is_shipped'])
        visible('pc', f"/view/{own['id']}", self_name, '自己販売・発送済み')
        request('pc', sale_path, 'POST', {**form('pc', sale_path), 'action': 'cancel_sale', 'correction_reason': '架空確認の取消'})
        visible('app', f"/view/{own['id']}", self_name, '自己保管・未出品')
    request('app', '/logout')
    denied = request('app', detail)
    check('App logout does not end PC session', denied.status_code == 302 and request('pc', detail).status_code == 200)
    return finish(checks, requests)


def finish(checks, requests):
    return {'verified_at': datetime.now(timezone.utc).isoformat(),
        'scope': 'Independent PC/app user-agent HTTP clients, real login, copied render_app, disposable SQLite, no external network',
        'limits': ['Not a native build or device test.', 'Not deployed PostgreSQL, external notification delivery, payment or disaster recovery.'],
        'checks': checks, 'requests': requests, 'passed': sum(x['pass'] for x in checks),
        'failed': sum(not x['pass'] for x in checks), 'server_errors': sum(x['status'] >= 500 for x in requests)}


def main():
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    original = Path.cwd()
    with tempfile.TemporaryDirectory(prefix='kaika-platform-sync-') as temporary:
        preview = Path(temporary).resolve()
        runtime = sandbox.mirror_source(preview, None, None)
        sandbox.isolate_environment(preview, 18779)
        connections = sandbox.install_runtime_boundary(preview, 18779)
        sys.path[:] = [str(runtime)] + [p for p in sys.path if p and not sandbox.inside(Path(p).resolve(), sandbox.SOURCE)]
        os.chdir(runtime)
        try:
            loaded = importlib.import_module('render_app')
            users = sandbox.seed_preview(loaded.module, preview)
            sandbox.install_preview_routes(loaded.module, preview, users, 18779)
            report = run(loaded.module, users, preview)
            print('PLATFORM_SYNC_JSON=' + json.dumps(report, ensure_ascii=False))
        finally:
            for connection in connections:
                connection.close()
            os.chdir(original)
    return 1 if report['failed'] or report['server_errors'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
