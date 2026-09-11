"""Inventory and exercise the real patched site only inside the preview sandbox.

Writes one JSON report to stdout. Existing databases, credentials, user uploads,
network calls, payment providers and subprocesses remain unavailable. GET route
coverage is reported separately from successful business workflow coverage.
"""
from __future__ import annotations

from collections import Counter
import base64
from html.parser import HTMLParser
import importlib
import io
import json
import logging
import mimetypes
import os
from pathlib import Path
import re
import sys
import tempfile
import traceback
import zipfile
from urllib.parse import urlsplit, urljoin

import run_app_preview as sandbox


class References(HTMLParser):
    def __init__(self):
        super().__init__()
        self.assets, self.links, self.forms = set(), set(), set()
    def handle_starttag(self, tag, values):
        attrs = dict(values)
        if tag in {'script', 'img', 'source'} and attrs.get('src'):
            self.assets.add(attrs['src'])
        if tag == 'link' and attrs.get('href'):
            self.assets.add(attrs['href'])
        if tag == 'a' and attrs.get('href'):
            self.links.add(attrs['href'])
        if tag == 'form':
            self.forms.add((attrs.get('method', 'get').upper(), attrs.get('action', ''), attrs.get('id', '')))


EXTERNAL_PREFIXES = ('/billing/', '/stripe/', '/admin/stripe', '/api/stripe/', '/line/', '/admin/line',
                     '/admin/backup', '/backup/', '/internal/backups', '/api/google-drive/download')
MUTATING_GET = re.compile(r'/(?:delete|remove|send|toggle|set_role|approve|reject|logout)(?:/|[-_]|$)|^/admin/monthly-settlements/create$')


def run(module, preview, users):
    app = module.app
    app.logger.setLevel(logging.ERROR)
    app.config['PROPAGATE_EXCEPTIONS'] = True
    base = 'http://127.0.0.1:18769'
    report = {'scope': 'copied render_app runtime; disposable SQLite; no external network',
              'routes': [], 'requests': [], 'assets': [], 'source_syntax': [], 'template_syntax': [],
              'forms': [], 'workflow': [], 'artifacts': [], 'limits': [
                  'Missing-record probes do not establish record-specific behavior.',
                  'No production PostgreSQL, live data, external accounts, iOS or Android execution.',
                  'GET status and template compilation do not establish client-side interaction correctness.',
                  'External integration and mutating GET routes are catalogued separately, not blindly followed.']}
    for path in sorted(Path(module.__file__).parent.glob('*.py')):
        try:
            compile(path.read_bytes(), str(path), 'exec')
            report['source_syntax'].append({'file': path.name, 'result': 'pass'})
        except Exception as exc:
            report['source_syntax'].append({'file': path.name, 'result': 'fail', 'error': str(exc)})
    for name in sorted(app.jinja_env.list_templates()):
        try:
            app.jinja_env.get_template(name)
            report['template_syntax'].append({'file': name, 'result': 'pass'})
        except Exception as exc:
            report['template_syntax'].append({'file': name, 'result': 'fail', 'error': str(exc)})

    clients = {role: app.test_client() for role in ('anonymous', 'normal', 'business', 'admin')}
    for role, client in clients.items():
        if role != 'anonymous':
            client.get('/__preview/start/' + role, base_url=base)
    adapter = app.url_map.bind('127.0.0.1:18769')
    assets = set()
    links = set()
    form_routes = set()
    seen = set()

    def request(role, path, method='GET', data=None, label=None, json_body=None):
        result = {'role': role, 'method': method, 'path': path}
        if label:
            result['label'] = label
        try:
            response = clients[role].open(path, method=method, data=data, json=json_body, base_url=base)
            result.update(status=response.status_code, mime=response.mimetype,
                          bytes=len(response.get_data()))
            if response.location:
                result['location'] = response.location
            if response.status_code == 200 and response.mimetype == 'text/html':
                parsed = References()
                parsed.feed(response.get_data(as_text=True))
                assets.update((role, src) for src in parsed.assets if src.startswith('/static/'))
                for link in parsed.links:
                    absolute = urlsplit(urljoin(base + path, link))
                    if absolute.netloc == '127.0.0.1:18769' and absolute.scheme == 'http':
                        links.add((role, absolute.path + ('?' + absolute.query if absolute.query else '')))
                for form_method, action, form_id in parsed.forms:
                    if action.startswith('/') or not action:
                        form_routes.add((form_method, action or path, not action and bool(form_id)))
            if response.status_code >= 500:
                result['body'] = response.get_data(as_text=True)[:6000]
            report['requests'].append(result)
            # Flask's send_file iterator holds a Windows file handle until the
            # response is closed; get_data above caches the bytes for assertions.
            response.close()
            return response
        except Exception as exc:
            result.update(status=500, error=f'{type(exc).__name__}: {exc}',
                          traceback=traceback.format_exc())
            report['requests'].append(result)
            return None

    for rule in sorted(app.url_map.iter_rules(), key=lambda rule: rule.rule):
        if rule.rule.startswith('/__preview/'):
            continue
        methods = sorted(rule.methods - {'HEAD', 'OPTIONS'})
        classification = ('external_or_backup' if rule.rule.startswith(EXTERNAL_PREFIXES) else
                          'mutating_get' if 'GET' in methods and MUTATING_GET.search(rule.rule) else
                          'post_workflow' if 'GET' not in methods else
                          'record_or_parameter_get' if rule.arguments else 'page_or_api_get')
        report['routes'].append({'rule': rule.rule, 'endpoint': rule.endpoint, 'methods': methods,
                                 'arguments': sorted(rule.arguments), 'classification': classification})
        if classification in {'external_or_backup', 'mutating_get', 'post_workflow'} or rule.endpoint == 'static':
            continue
        paths = []
        if not rule.arguments:
            paths = [rule.rule]
        else:
            values = {name: 999999 for name in rule.arguments}
            for name in rule.arguments:
                if name == 'report_type':
                    values[name] = 'sales'
                elif name == 'service_slug':
                    values[name] = 'wholesale'
                elif name == 'service_type':
                    values[name] = 'storage'
                elif name == 'filename':
                    values[name] = 'css/style.css'
            try:
                paths = [adapter.build(rule.endpoint, values, method='GET')]
            except Exception as exc:
                report['routes'][-1]['build_error'] = str(exc)
        for path in paths:
            for role in clients:
                key = role, path
                if key not in seen:
                    seen.add(key)
                    request(role, path, label='missing_record_or_sample_parameter' if rule.arguments else 'route_get')

    # Valid ownership probes exercise the detail/edit routes for real fixtures.
    for role in ('normal', 'business', 'admin'):
        with module.get_db() as conn:
            ids = [row[0] for row in conn.execute('SELECT id FROM merchandise WHERE user_id=?', (users[role]['id'],))]
        for item_id in ids[:1]:
            for path in (f'/view/{item_id}', f'/edit/{item_id}', f'/inventory/self/{item_id}/edit'):
                request(role, path, label='owned_fixture')
            other = 'business' if role == 'normal' else 'normal'
            request(other, f'/inventory/self/{item_id}/edit', label='cross_owner_denied')

    exercise_workflows(module, preview, users, clients, request, report)

    # Revisit populated hubs, then follow their concrete same-site links. Never
    # crawl GET endpoints whose existing implementation mutates or sends data.
    for role in ('normal', 'business', 'admin'):
        for path in ('/', '/documents', '/documents/list', '/admin/users' if role == 'admin' else '/customers'):
            request(role, path, label='populated_hub')
    for _round in range(3):
        pending = sorted(links - seen)
        for role, path in pending:
            seen.add((role, path))
            plain = urlsplit(path).path
            if plain.startswith(('/__preview/', *EXTERNAL_PREFIXES)) or MUTATING_GET.search(plain):
                continue
            try:
                endpoint, values = adapter.match(plain, method='GET')
                if endpoint == 'static':
                    continue
            except Exception:
                continue
            request(role, path, label='linked_page')

    for method, path, dynamic in sorted(form_routes):
        if dynamic:
            report['forms'].append({'method': method, 'path': path, 'result': 'javascript_action_requires_browser'})
            continue
        try:
            endpoint, values = adapter.match(urlsplit(path).path, method=method)
            report['forms'].append({'method': method, 'path': path, 'endpoint': endpoint, 'result': 'route_exists'})
        except Exception as exc:
            report['forms'].append({'method': method, 'path': path, 'result': 'unresolved', 'error': str(exc)})

    # Check every copied public asset, not only those referenced by the fixture pages.
    static = Path(app.static_folder)
    public = [p for p in static.rglob('*') if p.is_file() and 'uploads' not in p.relative_to(static).parts]
    for file in public:
        assets.add(('anonymous', '/static/' + file.relative_to(static).as_posix()))
    for role, path in sorted(assets):
        response = request(role, path, label='static_asset')
        report['assets'].append({'role': role, 'path': path,
                                 'status': response.status_code if response is not None else 500})

    report['summary'] = {'routes': len(report['routes']), 'route_classes': dict(Counter(x['classification'] for x in report['routes'])),
                         'requests': len(report['requests']), 'status': dict(Counter(x['status'] for x in report['requests'])),
                         'source_files': len(report['source_syntax']), 'templates': len(report['template_syntax']),
                         'assets': len(report['assets']), 'forms': len(report['forms']),
                         'workflow_checks': dict(Counter(x['result'] for x in report['workflow']))}
    return report


def exercise_workflows(module, preview, users, clients, request, report):
    def artifact(name, response):
        if response is None or response.status_code != 200:
            return
        content = response.get_data()
        if response.mimetype == 'text/html':
            html_text = response.get_data(as_text=True)
            def embed(match):
                path = (Path(module.app.static_folder) / match.group(1)).resolve()
                if not sandbox.inside(path, preview) or not path.is_file():
                    return match.group(0)
                mime = mimetypes.guess_type(path.name)[0] or 'application/octet-stream'
                return 'src="data:' + mime + ';base64,' + base64.b64encode(path.read_bytes()).decode('ascii') + '"'
            html_text = re.sub(r'src="/static/([^"?]+)"', embed, html_text)
            content = html_text.encode('utf-8')
        report['artifacts'].append({'name': name, 'mime': response.mimetype, 'fictional_data_only': True,
                                    'base64': base64.b64encode(content).decode('ascii')})
    def check(name, condition, detail=''):
        report['workflow'].append({'name': name, 'result': 'pass' if condition else 'fail', 'detail': detail})

    def row(sql, params=()):
        conn = module.get_db()
        try:
            result = conn.execute(sql, params).fetchone()
            return dict(result) if result else None
        finally:
            conn.close()

    def tokens(response):
        class Fields(HTMLParser):
            def __init__(self):
                super().__init__()
                self.values = {}
            def handle_starttag(self, tag, attrs):
                value = dict(attrs)
                if tag == 'input' and value.get('name') in {'csrf_token', 'submission_token', 'operation_token'}:
                    self.values[value['name']] = value.get('value', '')
        fields = Fields()
        fields.feed(response.get_data(as_text=True))
        return fields.values

    def expect_page(role, path, marker=None):
        result = request(role, path, label='workflow_page')
        check(role + ' ' + path, result is not None and result.status_code == 200 and
              (marker is None or marker in result.get_data(as_text=True)), 'render and expected fixture content')
        return result

    if 'store_billing.context' in module.app.view_functions:
        for provider in ('apple', 'google'):
            response = request('normal', '/api/plans/store-context?provider=' + provider,
                               label='store_unconfigured_context')
            state = response.get_json(silent=True) if response is not None else None
            check(provider + ' unconfigured store does not advertise purchase', response is not None and
                  response.status_code == 200 and state and state.get('available') is False and state.get('products') == [])

    for path in ('/invoices', '/invoices/view/999999', '/invoices/edit/999999', '/invoices/delete/999999',
                 '/invoices/send/999999', '/mitsumori', '/mitsumori/view/999999', '/mitsumori/edit/999999',
                 '/mitsumori/delete/999999', '/kaitori-shoudaku', '/kaitori-shoudaku/999999',
                 '/kaitori-shoudaku/999999/edit', '/kaitori-shoudaku/999999/delete'):
        result = request('anonymous', path, label='document_login_regression')
        check('anonymous document redirects to login ' + path, result is not None and result.status_code == 302 and
              urlsplit(result.location).path == '/login')

    page = request('normal', '/inventory/self/new', label='inventory_create_form')
    form = tokens(page)
    form.update(product_name='確認商品・登録連動', brand_name='SAMPLE', purchase_price='1234', listing_price='3200',
                purchase_date='2026-09-10', notes='ワークフロー確認')
    response = request('normal', '/inventory/self/new', 'POST', dict(form), 'inventory_create')
    created = row('SELECT * FROM merchandise WHERE product_name=?', ('確認商品・登録連動',))
    check('inventory create owns and persists', response is not None and response.status_code == 302 and
          created and created['user_id'] == users['normal']['id'] and created['scope'] == 'user')
    if created:
        item_id = created['id']
        expect_page('normal', f'/view/{item_id}', '確認商品・登録連動')
        request('normal', '/inventory/self/new', 'POST', dict(form), 'inventory_duplicate_submit')
        check('inventory duplicate submission idempotent', row('SELECT COUNT(*) AS n FROM merchandise WHERE product_name=?', ('確認商品・登録連動',))['n'] == 1)
        edit = request('normal', f'/inventory/self/{item_id}/edit', label='inventory_edit_form')
        edit_form = tokens(edit)
        edit_form.update(product_name='確認商品・変更連動', notes='編集済み', purchase_price='1234', listing_price='3300')
        image = (preview / 'site' / 'static' / 'uploads' / 'preview' / 'sample-0.png').read_bytes()
        upload_form = {**edit_form, 'photo': (io.BytesIO(image), 'verification.png')}
        saved = request('normal', f'/inventory/self/{item_id}/edit', 'POST', upload_form, 'inventory_edit_photo')
        updated = row('SELECT * FROM merchandise WHERE id=?', (item_id,))
        check('inventory edit and image upload persists', saved is not None and saved.status_code == 302 and
              updated['product_name'] == '確認商品・変更連動' and bool(updated['photo_path']))
        if updated['photo_path']:
            image_url = '/static/uploads/' + updated['photo_path'].removeprefix('uploads/')
            img = request('normal', image_url, label='uploaded_inventory_photo')
            check('uploaded inventory photo can be read', img is not None and img.status_code == 200 and img.mimetype.startswith('image/'))
        stale = request('normal', f'/inventory/self/{item_id}/edit', 'POST', edit_form, 'inventory_stale_edit')
        check('stale edit does not overwrite', stale is not None and stale.status_code == 409)
        denied = request('business', f'/inventory/self/{item_id}/edit', 'POST', edit_form, 'inventory_cross_owner_edit')
        check('cross owner cannot change inventory', denied is not None and denied.status_code in {400, 403, 404} and
              row('SELECT product_name FROM merchandise WHERE id=?', (item_id,))['product_name'] == '確認商品・変更連動')

    if created and 'inventory_custody' in module.app.extensions:
        own_id = created['id']
        detail = expect_page('normal', f'/view/{own_id}', '自己保管・未出品')
        check('self inventory never advertises Kaika shipping operation',
              'data-action="create-shipping"' not in detail.get_data(as_text=True) and
              row('SELECT custody_location FROM merchandise WHERE id=?', (own_id,))['custody_location'] == 'self')
        for location in ('self','kaika','transit'):
            filtered = expect_page('normal', '/?filter=custody_' + location)
            html_body = filtered.get_data(as_text=True)
            check('inventory filters custody ' + location, all('data-custody="'+other+'"' not in html_body for other in ('self','kaika','transit') if other!=location)
                  and (location=='transit' or 'data-custody="'+location+'"' in html_body))
        with module.app.test_request_context():
            from flask import url_for
            shipping_url = url_for('submit_sale_request', item_id=own_id)
        denied = request('normal', shipping_url, 'POST', {}, 'self_legacy_shipping_guard')
        check('self stock cannot request Kaika shipping', denied is not None and denied.status_code == 409)
        sale_path = f'/inventory/self/{own_id}/sale'
        sale_form = tokens(expect_page('normal', sale_path, '自分の販売を記録'))
        sold = request('normal', sale_path, 'POST', {**sale_form, 'action':'sale', 'sale_date':module.get_jst_now().date().isoformat(),
                       'sale_price':'4000','shipping_cost':'500','commission':'400','sales_destination':'架空販売先','is_shipped':'1'}, 'self_sale_record')
        sale_row = row('SELECT * FROM merchandise WHERE id=?', (own_id,))
        check('self sale amounts and dispatch persist independently', sold is not None and sold.status_code == 302 and
              sale_row['sale_price'] == 4000 and sale_row['shipping_cost'] == 500 and sale_row['commission'] == 400 and sale_row['is_shipped'])
        expect_page('normal', f'/view/{own_id}', '自己販売・発送済み')
        cancel_form = tokens(expect_page('normal', sale_path, '販売記録を修正'))
        cancelled = request('normal', sale_path, 'POST', {**cancel_form, 'action':'cancel_sale','correction_reason':'架空取引の取消'}, 'self_sale_cancel')
        check('self sale cancellation returns to unsold stock', cancelled is not None and cancelled.status_code == 302 and
              row('SELECT sale_date FROM merchandise WHERE id=?', (own_id,))['sale_date'] is None)
        intake_form = tokens(expect_page('normal', '/inventory/intakes/new', '開花へ預ける'))
        transfer = request('normal', '/inventory/intakes/new', 'POST', {**intake_form,'kind':'transfer','item_ids':str(own_id),'client_note':'架空の預け入れ'}, 'custody_transfer_request')
        intake = row("SELECT * FROM inventory_intakes WHERE user_id=? AND kind='transfer' ORDER BY id DESC LIMIT 1", (users['normal']['id'],))
        check('transfer request preserves self custody until physical receipt', transfer is not None and transfer.status_code == 302 and intake and
              row('SELECT custody_location FROM merchandise WHERE id=?', (own_id,))['custody_location'] == 'self')
        if intake:
            intake_path = f"/inventory/intakes/{intake['id']}"
            wrong = request('business', intake_path, label='custody_cross_owner')
            check('intake contents private to owner and staff', wrong is not None and wrong.status_code == 404)
            admin_form = tokens(expect_page('admin', intake_path, '発送案内'))
            request('admin', intake_path, 'POST', {**admin_form,'action':'approve','shipping_instructions':'確認用案内・実際には発送しないでください'}, 'custody_approve')
            shipping_form = tokens(expect_page('normal', intake_path, '発送したら報告'))
            request('normal', intake_path, 'POST', {**shipping_form,'action':'ship','carrier':'架空配送','tracking_number':'TEST-ONLY'}, 'custody_ship')
            check('dispatch changes custody to transit without changing owner', row('SELECT custody_location,user_id FROM merchandise WHERE id=?',(own_id,)) ==
                  {'custody_location':'transit','user_id':users['normal']['id']})
            received_form = tokens(expect_page('admin', intake_path, '実物を確認して受領'))
            request('admin', intake_path, 'POST', {**received_form,'action':'receive','received_count':'1'}, 'custody_receive')
            received = row('SELECT * FROM merchandise WHERE id=?',(own_id,))
            check('staff receipt preserves ID owner and starts Kaika custody', received['custody_location']=='kaika' and received['user_id']==users['normal']['id'] and bool(received['custody_received_at']))
            expect_page('normal', intake_path, '登録が完了しました')
            received_edit = request('normal', f'/inventory/self/{own_id}/edit', label='kaika_own_edit_denied')
            check('received self editor is read only', received_edit is not None and received_edit.status_code==200 and
                  'readonly' in received_edit.get_data(as_text=True) and '変更を保存する' not in received_edit.get_data(as_text=True))
            tampered = request('normal', f'/inventory/self/{own_id}/edit', 'POST',
                               {**tokens(received_edit),'product_name':'拒否される変更'}, 'kaika_own_write_denied')
            check('client cannot edit goods after Kaika receipt', tampered is not None and tampered.status_code==409 and
                  row('SELECT product_name FROM merchandise WHERE id=?',(own_id,))['product_name']==received['product_name'])
        registration_form = tokens(expect_page('normal', '/inventory/intakes/new'))
        count_before = row('SELECT COUNT(*) AS n FROM merchandise')['n']
        request('normal', '/inventory/intakes/new', 'POST', {**registration_form,'kind':'registration','expected_count':'1'}, 'kaika_registration_request')
        intake = row("SELECT * FROM inventory_intakes WHERE user_id=? AND kind='registration' ORDER BY id DESC LIMIT 1", (users['normal']['id'],))
        check('Kaika registration request creates no unreceived merchandise', intake and row('SELECT COUNT(*) AS n FROM merchandise')['n']==count_before)
        if intake:
            intake_path = f"/inventory/intakes/{intake['id']}"
            for action,data in [('approve',{'shipping_instructions':'持込確認用・実際の受付ではありません'}),('receive',{'received_count':'1'})]:
                fields = tokens(expect_page('admin', intake_path))
                result = request('admin', intake_path, 'POST', {**fields,'action':action,**data}, 'kaika_registration_'+action)
                check('Kaika registration '+action+' succeeds', result is not None and result.status_code==302)
            registration_path = intake_path + '/register'
            fields = tokens(expect_page('admin', registration_path, '受領商品を登録'))
            result = request('admin', registration_path, 'POST', {**fields,'product_name':'確認用・開花が記帳した商品',
                       'purchase_price':'2500','listing_price':'5000','expected_shipping':'500','expected_commission':'500'}, 'kaika_register_item')
            registered = row('SELECT * FROM merchandise WHERE product_name=?',('確認用・開花が記帳した商品',))
            check('staff registration belongs to client and Kaika custody', result is not None and result.status_code==302 and registered and registered['user_id']==users['normal']['id'] and registered['custody_location']=='kaika')
            fields = tokens(expect_page('admin', intake_path, '登録完了を確定'))
            request('admin', intake_path, 'POST', {**fields,'action':'complete'}, 'kaika_registration_complete')
            expect_page('normal', intake_path, '登録が完了しました')
            expect_page('normal', f"/view/{registered['id']}", '確認用・開花が記帳した商品')
            check('Kaika registration finishes only after counted items recorded', row('SELECT status FROM inventory_intakes WHERE id=?',(intake['id'],))['status']=='completed')

    customer = {'name': '確認顧客', 'email': 'customer@preview.invalid', 'phone': '00000000000', 'address': '架空住所',
                'total_purchase': '1200', 'purchase_count': '1', 'notes': '確認用'}
    request('normal', '/customers/add', 'POST', customer, 'customer_create')
    stored = row('SELECT * FROM customers WHERE name=?', ('確認顧客',))
    check('customer create persists', stored and stored['user_id'] == users['normal']['id'])
    if stored:
        customer_id = stored['id']
        expect_page('normal', f'/customers/edit/{customer_id}', '確認顧客')
        customer['name'] = '確認顧客・変更'
        request('normal', f'/customers/edit/{customer_id}', 'POST', customer, 'customer_edit')
        check('customer edit persists', row('SELECT name FROM customers WHERE id=?', (customer_id,))['name'] == customer['name'])
        request('business', f'/customers/delete/{customer_id}', label='customer_cross_owner_delete')
        check('customer cross owner deletion denied', row('SELECT id FROM customers WHERE id=?', (customer_id,)) is not None)
        request('normal', f'/customers/delete/{customer_id}', label='customer_delete_fixture')
        check('customer delete own fixture', row('SELECT id FROM customers WHERE id=?', (customer_id,)) is None)

    request('normal', '/inquiry/new', 'POST', {'category': 'general', 'title': '確認問い合わせ', 'content': '架空データの問合せ確認'}, 'inquiry_create')
    inquiry = row('SELECT * FROM inquiries WHERE title=?', ('確認問い合わせ',))
    check('inquiry create persists', inquiry and inquiry['user_id'] == users['normal']['id'])
    if inquiry:
        inquiry_id = inquiry['id']
        expect_page('normal', f'/inquiry/{inquiry_id}', '確認問い合わせ')
        expect_page('admin', f'/admin/inquiry/{inquiry_id}', '確認問い合わせ')
        request('admin', f'/admin/inquiry/{inquiry_id}/reply', 'POST', {'content': '確認返信'}, 'inquiry_admin_reply')
        check('admin inquiry reply persists', row('SELECT COUNT(*) AS n FROM inquiry_replies WHERE inquiry_id=?', (inquiry_id,))['n'] == 1)
        request('normal', f'/inquiry/{inquiry_id}/reply', 'POST', {'content': '確認再返信'}, 'inquiry_user_reply')
        check('user inquiry reply persists', row('SELECT COUNT(*) AS n FROM inquiry_replies WHERE inquiry_id=?', (inquiry_id,))['n'] == 2)
        request('business', f'/inquiry/{inquiry_id}/delete', 'POST', {}, 'inquiry_cross_owner_delete')
        check('inquiry cross owner deletion denied', row('SELECT id FROM inquiries WHERE id=?', (inquiry_id,)) is not None)
        request('normal', f'/inquiry/{inquiry_id}/delete', 'POST', {}, 'inquiry_delete')
        check('inquiry delete removes fixture and replies', row('SELECT id FROM inquiries WHERE id=?', (inquiry_id,)) is None and
              row('SELECT COUNT(*) AS n FROM inquiry_replies WHERE inquiry_id=?', (inquiry_id,))['n'] == 0)

    document_data = {'issue_date': '2026-09-10', 'recipient_name': '確認先', 'company_name': '確認会社',
                     'subject': '確認帳票', 'status': 'draft', 'notes': '確認保存',
                     'item_name[]': '確認明細', 'product_name[]': '確認明細', 'quantity[]': '2',
                     'unit_price[]': '1200', 'unit[]': '点', 'tax_category[]': '10'}
    for root, table, view_pattern, edit_pattern, pdf_pattern, download_pattern in (
        ('/invoices', 'invoices', '/invoices/view/{}', '/invoices/edit/{}', '/invoices/pdf/{}', '/invoices/download/{}'),
        ('/mitsumori', 'user_mitsumori', '/mitsumori/view/{}', '/mitsumori/edit/{}', '/mitsumori/pdf/{}', '/mitsumori/download/{}'),
        ('/keisan', 'user_keisan', '/keisan/view/{}', '/keisan/edit/{}', '/keisan/pdf/{}', '/keisan/download/{}'),
        ('/kaitori-shoudaku', 'user_kaitori_shoudaku', '/kaitori-shoudaku/{}', '/kaitori-shoudaku/{}/edit', '/kaitori-shoudaku/{}/pdf', '/kaitori-shoudaku/{}/download')):
        request('normal', root + '/add', 'POST', dict(document_data), 'document_create')
        document = row(f'SELECT * FROM {table} ORDER BY id DESC LIMIT 1')
        check(root + ' document persists', document is not None)
        if not document:
            continue
        document_id = document['id']
        listed = request('normal', root, label='document_created_listing')
        own_targets = (view_pattern.format(document_id), edit_pattern.format(document_id))
        check(root + ' newly created document remains in own list', listed is not None and listed.status_code == 200 and
              any('href="' + target + '"' in listed.get_data(as_text=True) for target in own_targets),
              'actual document link; a flash notification alone does not establish list visibility')
        expect_page('normal', view_pattern.format(document_id), '確認')
        expect_page('normal', edit_pattern.format(document_id))
        printable = expect_page('normal', pdf_pattern.format(document_id))
        if printable is not None and root in {'/invoices', '/mitsumori'}:
            check(root + ' printed quantity sums quantities, not rows',
                  '<td class="text-right">2 点</td>' in printable.get_data(as_text=True))
        if printable is not None and root == '/invoices':
            printed = printable.get_data(as_text=True)
            check('user invoice printed tax matches existing additive calculation',
                  '金額(税抜)' in printed and '消費税(8%)' in printed and '消費税(10%)' in printed and
                  '内税(10%)' not in printed and '2,640' in printed and document['total_amount'] == 2640)
        artifact(root.strip('/') + '-print.html', printable)
        downloaded = request('normal', download_pattern.format(document_id), label='document_download')
        check(root + ' downloadable artifact', downloaded is not None and downloaded.status_code == 200 and len(downloaded.get_data()) > 100,
              'actual generated download bytes; native sharing separately unverified')
        artifact(root.strip('/') + '-download.csv', downloaded)
        denied = request('business', view_pattern.format(document_id), label='document_cross_owner')
        check(root + ' cross owner view denied', denied is not None and denied.status_code in {302, 403, 404})
        for output_pattern in (pdf_pattern, download_pattern):
            denied = request('business', output_pattern.format(document_id), label='document_cross_owner_export')
            check(root + ' cross owner export denied ' + output_pattern, denied is not None and denied.status_code in {302, 403, 404})
        request('normal', edit_pattern.format(document_id), 'POST', {**document_data, 'notes': '確認更新'}, 'document_edit')
        check(root + ' edit persists', row(f'SELECT notes FROM {table} WHERE id=?', (document_id,))['notes'] == '確認更新')
        if root == '/invoices':
            # Keep the separate administrator-created, tax-inclusive template
            # branch covered without changing any accounting calculation.
            clone = dict(document)
            clone.pop('id')
            clone.update(invoice_no=str(document['invoice_no']) + '-ADMIN', document_scope='client_outgoing',
                         source_admin_kaitori_id=1, status='draft')
            conn = module.get_db()
            try:
                cur = conn.execute(f'INSERT INTO invoices ({",".join(clone)}) VALUES ({",".join("?" for _ in clone)})', tuple(clone.values()))
                clone_id = cur.lastrowid
                conn.commit()
            finally:
                conn.close()
            response = request('normal', '/invoices', label='admin_draft_invoice_hidden')
            check('administrator draft invoice stays hidden from user list', response is not None and response.status_code == 200 and
                  ('/invoices/view/' + str(clone_id) + '"') not in response.get_data(as_text=True) and
                  ('/invoices/edit/' + str(clone_id) + '"') not in response.get_data(as_text=True))
            for output in ('view', 'download', 'pdf'):
                response = request('normal', f'/invoices/{output}/{clone_id}', label='admin_draft_invoice_output_hidden')
                check('administrator draft invoice ' + output + ' hidden from recipient', response is not None and response.status_code in {302, 403, 404})
                allowed = request('admin', f'/invoices/{output}/{clone_id}', label='admin_draft_invoice_admin_access')
                if output != 'view':
                    check('administrator retains draft invoice ' + output, allowed is not None and allowed.status_code == 200)
            conn = module.get_db()
            try:
                conn.execute("UPDATE invoices SET source_admin_kaitori_id=NULL, sales_agency_request_id=NULL, invoice_no='KT-VERIFICATION-DRAFT' WHERE id=?", (clone_id,))
                conn.commit()
            finally:
                conn.close()
            for output in ('view', 'download', 'pdf'):
                response = request('normal', f'/invoices/{output}/{clone_id}', label='unlinked_admin_draft_invoice_hidden')
                check('unlinked administrator draft invoice ' + output + ' hidden from recipient', response is not None and response.status_code in {302, 403, 404})
            conn = module.get_db()
            try:
                conn.execute("UPDATE invoices SET status='in_progress' WHERE id=?", (clone_id,))
                conn.commit()
            finally:
                conn.close()
            for output in ('view', 'download', 'pdf'):
                response = request('normal', f'/invoices/{output}/{clone_id}', label='in_progress_admin_invoice_hidden')
                check('in-progress administrator invoice ' + output + ' hidden from recipient', response is not None and response.status_code in {302, 403, 404})
            check('unpublished invoice probes do not mark it as read', row('SELECT is_read FROM invoices WHERE id=?', (clone_id,))['is_read'] == 0)
            conn = module.get_db()
            try:
                conn.execute("UPDATE invoices SET status='sent' WHERE id=?", (clone_id,))
                conn.commit()
            finally:
                conn.close()
            response = request('normal', f'/invoices/pdf/{clone_id}', label='admin_invoice_tax_layout')
            check('admin invoice retains existing tax-inclusive print layout', response is not None and response.status_code == 200 and
                  '内税(10%)' in response.get_data(as_text=True) and '金額(税抜)' not in response.get_data(as_text=True))
            response = request('normal', f'/invoices/download/{clone_id}', label='published_admin_invoice_download')
            check('published administrator invoice CSV remains available to recipient', response is not None and response.status_code == 200 and 'KT-VERIFICATION-DRAFT' in response.get_data(as_text=True))

    for version, kinds in (('report', ('monthly', 'inventory', 'expenses', 'annual', 'kaitori', 'sales')),
                           ('report-v2', ('sales_ledger', 'purchase_ledger', 'inventory_snapshot', 'expense_ledger',
                                          'management_summary', 'inventory_turnover'))):
        for kind in kinds:
            for role in ('normal', 'admin'):
                query = f'?year=2026&month=9&client_id={users["normal"]["id"]}'
                for suffix in ('', '/download'):
                    response = request(role, f'/api/{version}/{kind}{suffix}{query}', label='report_json_csv')
                    check(role + ' ' + version + ' ' + kind + suffix,
                          response is not None and response.status_code == 200 and len(response.get_data()) > 2)
                if version == 'report-v2':
                    response = request(role, f'/api/{version}/{kind}/download{query}&format=pdf', label='report_pdf')
                    check(role + ' PDF ' + kind, response is not None and response.status_code == 200 and
                          response.mimetype == 'application/pdf' and response.get_data().startswith(b'%PDF-'))
                    if role == 'normal' and kind == 'inventory_snapshot':
                        artifact('inventory-report.pdf', response)

    business_form = tokens(request('business', '/', label='dealer_service_form'))
    business_form.update(service_type='wholesale', merchandise_ids='4', next='/sales-agency/my-requests')
    request('business', '/sales-agency/apply', 'POST', business_form, 'dealer_service_apply')
    agency = row('SELECT * FROM sales_agency_requests WHERE user_id=? ORDER BY id DESC LIMIT 1', (users['business']['id'],))
    check('business dealer application persists', agency is not None)
    if agency:
        expect_page('business', '/sales-agency/my-requests', '申請')
        expect_page('admin', f'/admin/sales-agency-requests/{agency["id"]}')
        request('business', f'/sales-agency/cancel-request/{agency["id"]}', 'POST', {'next': '/sales-agency/my-requests'}, 'dealer_service_cancel')
        updated = row('SELECT status FROM sales_agency_requests WHERE id=?', (agency['id'],))
        check('dealer cancellation request awaits administrator', updated and updated['status'] == 'cancel_requested')
        request('admin', f'/admin/sales-agency-requests/{agency["id"]}/process', 'POST',
                {'action': 'cancel_approve', 'admin_note': '確認用キャンセル承認'}, 'dealer_cancel_approve')
        updated = row('SELECT status FROM sales_agency_requests WHERE id=?', (agency['id'],))
        check('administrator confirms dealer cancellation', updated and updated['status'] == 'cancelled')

    # Three representative management workflows use the remaining unused seed
    # items: ordinary shipping/completion, long-term liquidation, and wholesale.
    image_bytes = (preview / 'site' / 'static' / 'uploads' / 'preview' / 'sample-1.png').read_bytes()
    def proof_form(request_type):
        return {'request_type': request_type, 'qr_image': (io.BytesIO(image_bytes), 'verification-proof.png'),
                'user_note': '架空商品の確認用画像'}
    shipping_item = 2
    request('normal', f'/sale-request/submit/{shipping_item}', 'POST', proof_form('completion_report'), 'completion_before_shipping')
    check('completion requires approved shipping first', row('SELECT COUNT(*) AS n FROM sale_requests WHERE merchandise_id=?', (shipping_item,))['n'] == 0)
    request('business', f'/sale-request/submit/{shipping_item}', 'POST', {'request_type': 'shipping_request'}, 'shipping_cross_owner')
    check('shipping cannot be requested by another owner', row('SELECT COUNT(*) AS n FROM sale_requests WHERE merchandise_id=?', (shipping_item,))['n'] == 0)
    request('normal', f'/sale-request/submit/{shipping_item}', 'POST', proof_form('shipping_request'), 'shipping_submit')
    shipping = row("SELECT * FROM sale_requests WHERE merchandise_id=? AND request_type='shipping_request'", (shipping_item,))
    check('shipping request persists pending with proof image', shipping and shipping['status'] == 'pending' and bool(shipping['qr_image_path']))
    if shipping:
        shipping_id = shipping['id']
        request('normal', f'/sale-request/submit/{shipping_item}', 'POST', {'request_type': 'shipping_request'}, 'shipping_duplicate')
        check('pending shipping request is not duplicated', row("SELECT COUNT(*) AS n FROM sale_requests WHERE merchandise_id=? AND request_type='shipping_request'", (shipping_item,))['n'] == 1)
        proof = request('normal', '/static/' + shipping['qr_image_path'], label='shipping_proof_download')
        check('shipping proof is available to its owner', proof is not None and proof.status_code == 200 and proof.mimetype.startswith('image/'))
        expect_page('admin', '/admin/sale-requests/shipping', '確認用')
        request('normal', f'/admin/sale-request/{shipping_id}/approve', 'POST', {}, 'shipping_user_cannot_approve')
        check('ordinary user cannot approve shipment', row('SELECT status FROM sale_requests WHERE id=?', (shipping_id,))['status'] == 'pending')
        request('admin', f'/admin/sale-request/{shipping_id}/shipment', 'POST', {'action': 'mark'}, 'shipping_mark_before_approval')
        check('shipment marking requires approval', row('SELECT is_shipped FROM merchandise WHERE id=?', (shipping_item,))['is_shipped'] == 0)
        request('admin', f'/admin/sale-request/{shipping_id}/approve', 'POST', {'admin_note': '確認承認'}, 'shipping_approve')
        shipment = row('SELECT * FROM sale_requests WHERE id=?', (shipping_id,))
        check('shipping approval reaches waiting shipment', shipment['status'] == 'approved' and shipment['shipment_status'] == 'approved_waiting_shipment')
        for action, expected_status, shipped in (('mark', 'shipped', 1), ('revert', 'approved_waiting_shipment', 0), ('mark', 'shipped', 1)):
            request('admin', f'/admin/sale-request/{shipping_id}/shipment', 'POST', {'action': action}, 'shipment_' + action)
            check('shipment ' + action + ' synchronizes request and inventory', row('SELECT shipment_status FROM sale_requests WHERE id=?', (shipping_id,))['shipment_status'] == expected_status and
                  row('SELECT is_shipped FROM merchandise WHERE id=?', (shipping_item,))['is_shipped'] == shipped)
        request('normal', f'/sale-request/submit/{shipping_item}', 'POST', proof_form('completion_report'), 'completion_submit')
        completion = row("SELECT * FROM sale_requests WHERE merchandise_id=? AND request_type='completion_report'", (shipping_item,))
        check('completion report persists with evidence after shipping', completion and completion['status'] == 'pending' and bool(completion['qr_image_path']))
        if completion:
            completion_id = completion['id']
            request('admin', f'/admin/sale-request/{completion_id}/approve', 'POST', {'approved_sale_price': '0'}, 'completion_reject_zero_price')
            check('completion zero sale price cannot be approved', row('SELECT status FROM sale_requests WHERE id=?', (completion_id,))['status'] == 'pending')
            request('admin', f'/admin/sale-request/{completion_id}/approve', 'POST',
                    {'approved_sale_price': '5000', 'approved_shipping_cost': '500', 'approved_commission': '500', 'approved_other_cost': '100', 'admin_note': '確認売却'}, 'completion_approve')
            sold = row('SELECT * FROM merchandise WHERE id=?', (shipping_item,))
            check('completion approval synchronizes sale values and date', row('SELECT status FROM sale_requests WHERE id=?', (completion_id,))['status'] == 'approved' and
                  sold['sale_price'] == 5000 and sold['shipping_cost'] == 500 and sold['commission'] == 500 and sold['other_cost'] == 100 and bool(sold['sale_date']) and sold['is_shipped'] == 1)
            request('admin', f'/admin/sale-request/{shipping_id}/shipment', 'POST', {'action': 'revert'}, 'shipment_revert_after_completion')
            check('completed trade prevents shipment rollback', row('SELECT is_shipped FROM merchandise WHERE id=?', (shipping_item,))['is_shipped'] == 1)
            request('normal', f'/sale-request/submit/{shipping_item}', 'POST', proof_form('completion_report'), 'completion_duplicate')
            check('completed report is not duplicated', row("SELECT COUNT(*) AS n FROM sale_requests WHERE merchandise_id=? AND request_type='completion_report'", (shipping_item,))['n'] == 1)

    disposal_item = 3
    conn = module.get_db()
    try:
        conn.execute("UPDATE merchandise SET storage_start_date='2025-01-01',purchase_date='2025-01-01' WHERE id=?", (disposal_item,))
        conn.commit()
    finally:
        conn.close()
    expect_page('normal', '/long-term-items', '確認用')
    request('business', '/long-term-disposal-request', 'POST', {'disposal_type': 'liquidation', 'merchandise_ids': str(disposal_item)}, 'disposal_cross_owner')
    check('long-term disposal cannot target another owner', row('SELECT COUNT(*) AS n FROM item_disposal_requests WHERE merchandise_id=?', (disposal_item,))['n'] == 0)
    request('normal', '/long-term-disposal-request', 'POST', {'disposal_type': 'liquidation', 'merchandise_ids': str(disposal_item)}, 'disposal_submit')
    disposal = row('SELECT * FROM item_disposal_requests WHERE merchandise_id=?', (disposal_item,))
    check('long-term disposal request persists', disposal and disposal['reason'] == 'long_term' and disposal['status'] == 'pending')
    if disposal:
        disposal_id = disposal['id']
        expect_page('admin', '/admin/disposal-requests', '確認用')
        request('normal', f'/admin/disposal-request/{disposal_id}/process', 'POST', {'action': 'liquidation_completed'}, 'disposal_user_cannot_complete')
        check('ordinary user cannot complete disposal', row('SELECT status FROM item_disposal_requests WHERE id=?', (disposal_id,))['status'] == 'pending')
        for action, status in (('liquidation_processing', 'processing'), ('liquidation_completed', 'completed')):
            request('admin', f'/admin/disposal-request/{disposal_id}/process', 'POST', {'action': action, 'admin_note': '架空商品の処分確認'}, 'disposal_' + action)
            check(action + ' state persists', row('SELECT status FROM item_disposal_requests WHERE id=?', (disposal_id,))['status'] == status)
        disposed = row('SELECT * FROM merchandise WHERE id=?', (disposal_item,))
        check('liquidation completion records zero sale and result', bool(disposed['sale_date']) and disposed['sale_price'] == 0 and disposed['sales_destination'] == '長期保存処分')

    dealer_item = 5
    dealer_form = tokens(request('business', '/', label='dealer_lifecycle_form'))
    dealer_form.update(service_type='wholesale', merchandise_ids=str(dealer_item), next='/sales-agency/my-requests')
    request('business', '/sales-agency/apply', 'POST', dealer_form, 'dealer_lifecycle_submit')
    dealer = row('SELECT * FROM sales_agency_requests WHERE user_id=? ORDER BY id DESC LIMIT 1', (users['business']['id'],))
    check('dealer lifecycle fixture created', dealer and dealer['status'] == 'pending')
    if dealer and dealer['status'] == 'pending':
        dealer_id = dealer['id']
        process_path = f'/admin/sales-agency-requests/{dealer_id}/process'
        request('normal', process_path, 'POST', {'action': 'approve'}, 'dealer_user_cannot_approve')
        check('ordinary user cannot approve dealer request', row('SELECT status FROM sales_agency_requests WHERE id=?', (dealer_id,))['status'] == 'pending')
        rejected = request('admin', process_path, 'POST', {'action': 'complete'}, 'dealer_skip_stage_rejected')
        check('dealer cannot skip directly from pending to completed', rejected is not None and rejected.status_code == 400 and
              row('SELECT status FROM sales_agency_requests WHERE id=?', (dealer_id,))['status'] == 'pending')
        for action, status in (('approve', 'approved'), ('appraising', 'appraising'), ('inspect', 'inspecting')):
            response = request('admin', process_path, 'POST', {'action': action}, 'dealer_' + action)
            check('dealer ' + action + ' updates workflow', response is not None and response.status_code == 200 and
                  row('SELECT status FROM sales_agency_requests WHERE id=?', (dealer_id,))['status'] == status)
        invalid = request('admin', process_path, 'POST', label='dealer_reject_zero_price', json_body={'action': 'complete', 'sale_price': 0})
        check('dealer invalid sale rolls back completion', invalid is not None and invalid.status_code == 400 and
              row('SELECT status FROM sales_agency_requests WHERE id=?', (dealer_id,))['status'] == 'inspecting' and
              not row('SELECT sale_date FROM merchandise WHERE id=?', (dealer_item,))['sale_date'])
        result = request('admin', process_path, 'POST', label='dealer_complete', json_body={'action': 'complete', 'sale_price': 7000})
        sold = row('SELECT * FROM merchandise WHERE id=?', (dealer_item,))
        check('dealer completion synchronizes inventory sale', result is not None and result.status_code == 200 and
              row('SELECT status FROM sales_agency_requests WHERE id=?', (dealer_id,))['status'] == 'completed' and
              sold['sale_price'] == 7000 and bool(sold['sale_date']) and sold['sale_type'] == module.build_sales_agency_sale_type('wholesale'))
        again = request('admin', process_path, 'POST', label='dealer_duplicate_complete', json_body={'action': 'complete', 'sale_price': 8000})
        check('duplicate dealer completion cannot alter sale price', again is not None and again.status_code == 400 and row('SELECT sale_price FROM merchandise WHERE id=?', (dealer_item,))['sale_price'] == 7000)
        request('admin', process_path, 'POST', {'action': 'revert_inspecting'}, 'dealer_revert_completion')
        check('dealer return to inspection clears sale date', row('SELECT status FROM sales_agency_requests WHERE id=?', (dealer_id,))['status'] == 'inspecting' and
              not row('SELECT sale_date FROM merchandise WHERE id=?', (dealer_item,))['sale_date'])
        expect_page('business', '/sales-agency/my-requests', '申請')
        expect_page('admin', f'/admin/sales-agency-requests/{dealer_id}', '確認用')

    # File outputs and administrator documents with actual related fixtures.
    for item_id in (1, 10):
        response = request('normal', f'/item/{item_id}/download_all', label='photo_zip_real_item')
        try:
            with zipfile.ZipFile(io.BytesIO(response.get_data())) as archive:
                files = archive.namelist()
                valid_zip = bool(files) and all(name.startswith('image_') for name in files)
                from PIL import Image
                for name in files:
                    with Image.open(io.BytesIO(archive.read(name))) as zipped_image:
                        zipped_image.verify()
        except Exception:
            valid_zip = False
        check('photo ZIP contains real image bytes ' + str(item_id), response is not None and response.status_code == 200 and valid_zip)
        denied = request('business', f'/item/{item_id}/download_all', label='photo_zip_cross_owner')
        check('photo ZIP rejects another owner ' + str(item_id), denied is not None and denied.status_code in {302, 403, 404})
        allowed = request('admin', f'/item/{item_id}/download_all', label='photo_zip_admin')
        check('photo ZIP remains available to administrators ' + str(item_id), allowed is not None and allowed.status_code == 200)

    original_photos = row('SELECT photo_path, additional_photos FROM merchandise WHERE id=1')
    outside_uploads = Path(module.app.static_folder) / 'private-zip-fixture.png'
    outside_uploads.write_bytes((Path(module.app.static_folder) / 'uploads' / 'preview' / 'sample-0.png').read_bytes())
    unsafe_photos = ('../private-zip-fixture.png', str(outside_uploads.resolve()),
                     str((Path(module.app.static_folder) / 'uploads' / 'preview' / 'sample-0.png').resolve()),
                     'uploads/../private-zip-fixture.png', 'uploads/verification-missing.png')
    try:
        for unsafe_photo in unsafe_photos:
            conn = module.get_db()
            try:
                conn.execute('UPDATE merchandise SET photo_path=?, additional_photos=NULL WHERE id=1', (unsafe_photo,))
                conn.commit()
            finally:
                conn.close()
            response = request('normal', '/item/1/download_all', label='photo_zip_unsafe_or_missing_path')
            check('photo ZIP rejects unsafe or missing path ' + unsafe_photo.split('/')[-1], response is not None and response.status_code in {302, 403, 404})
    finally:
        conn = module.get_db()
        try:
            conn.execute('UPDATE merchandise SET photo_path=?, additional_photos=? WHERE id=1',
                         (original_photos['photo_path'], original_photos['additional_photos']))
            conn.commit()
        finally:
            conn.close()
        outside_uploads.unlink()

    request('normal', '/service-document/create', 'POST', {'service_type': 'photo_packing', 'customer_name': '確認顧客',
            'product_name': '確認サービス商品', 'sales_amount': '5000', 'commission': '500', 'notes': '架空書類'}, 'service_document_create')
    service_doc = row('SELECT * FROM service_documents ORDER BY id DESC LIMIT 1')
    check('service document creates owned fixture', service_doc and service_doc['user_id'] == users['normal']['id'])
    if service_doc:
        for suffix in ('', '/pdf'):
            path = f'/service-document/{service_doc["id"]}{suffix}'
            response = expect_page('normal', path, '確認サービス商品')
            denied = request('business', path, label='service_document_cross_owner')
            check('service document rejects another owner ' + suffix, denied is not None and denied.status_code in {302, 403, 404})
            if suffix:
                artifact('service-document-print.html', response)

    settlement_form = {'recipient_id': str(users['normal']['id']), 'recipient_name': '確認受取人', 'issue_date': '2026-09-10',
                       'tax_rate': '10', 'status': 'draft', 'item_name[]': '確認精算商品', 'amount[]': '5500', 'notes': '架空精算書'}
    request('admin', '/admin/shikiriosho/add', 'POST', settlement_form, 'settlement_create_draft')
    settlement = row('SELECT * FROM shikiriosho ORDER BY id DESC LIMIT 1')
    check('administrator settlement draft persists', settlement and settlement['recipient_id'] == users['normal']['id'] and settlement['status'] == 'draft')
    if settlement:
        settlement_id = settlement['id']
        for path in (f'/admin/shikiriosho/view/{settlement_id}', f'/admin/shikiriosho/edit/{settlement_id}'):
            expect_page('admin', path, '確認精算商品')
        for path in (f'/shikiriosho/view/{settlement_id}', f'/shikiriosho/download/{settlement_id}', f'/shikiriosho/pdf/{settlement_id}'):
            response = request('normal', path, label='settlement_draft_hidden')
            check('unpublished settlement inaccessible ' + path, response is not None and response.status_code in {302, 403, 404})
        request('admin', f'/admin/shikiriosho/send/{settlement_id}', label='settlement_send_fixture')
        check('settlement publication persists sent state', row('SELECT status FROM shikiriosho WHERE id=?', (settlement_id,))['status'] == 'sent')
        expect_page('normal', '/shikiriosho', str(settlement['document_no']))
        for suffix in ('view', 'download', 'pdf'):
            path = f'/shikiriosho/{suffix}/{settlement_id}'
            response = request('normal', path, label='published_settlement_output')
            check('published settlement ' + suffix + ' contains own fixture', response is not None and response.status_code == 200 and '確認精算商品' in response.get_data(as_text=True))
            denied = request('business', path, label='settlement_cross_owner')
            check('settlement ' + suffix + ' rejects another owner', denied is not None and denied.status_code in {302, 403, 404})
            if suffix in ('download', 'pdf'):
                artifact('shikiriosho-' + ('download.csv' if suffix == 'download' else 'print.html'), response)
        for path in (f'/admin/seisan/{settlement_id}', f'/admin/seisan/{settlement_id}/edit', f'/admin/seisan/{settlement_id}/pdf'):
            response = request('admin', path, label='settlement_legacy_alias')
            check('legacy settlement route resolves ' + path, response is not None and response.status_code in {200, 302})

    # These administrator forms create coherent parent/item rows through the
    # site's existing handlers, without inventing workflow or document statuses.
    request('admin', '/admin/vendors', 'POST', {'name': '確認業者', 'memo': '架空業者'}, 'vendor_create_fixture')
    vendor = row('SELECT * FROM vendors ORDER BY id DESC LIMIT 1')
    check('administrator can create fictional vendor', vendor and vendor['name'] == '確認業者')
    if vendor:
        expect_page('admin', f'/admin/vendors/{vendor["id"]}/edit', '確認業者')
        uploaded_bytes = (Path(module.app.static_folder) / 'uploads' / 'preview' / 'sample-0.png').read_bytes()
        request('admin', '/admin/vendor-documents', 'POST', {'user_id': str(users['normal']['id']),
                'vendor_id': str(vendor['id']), 'item_id': '1', 'title': '確認業者回答',
                'file': (io.BytesIO(uploaded_bytes), 'fictional-response.png')}, 'vendor_document_upload')
        vendor_document = row('SELECT * FROM vendor_documents ORDER BY id DESC LIMIT 1')
        check('vendor response upload persists document and file', vendor_document and vendor_document['title'] == '確認業者回答')
        if vendor_document:
            path = f'/admin/vendor-documents/{vendor_document["id"]}/download'
            response = request('admin', path, label='vendor_document_download')
            check('vendor response download returns uploaded bytes', response is not None and response.status_code == 200 and response.get_data() == uploaded_bytes)
            for role in ('normal', 'business', 'anonymous'):
                denied = request(role, path, label='vendor_document_denied')
                check(role + ' cannot download administrator vendor response', denied is not None and denied.status_code in {302, 403, 404})
    admin_docs = [
        ('/admin/kaitori-shoudaku/add', 'admin_kaitori_shoudaku', dict(document_data),
         ('/admin/kaitori-shoudaku/{}', '/admin/kaitori-shoudaku/{}/download', '/admin/kaitori-shoudaku/{}/pdf')),
        ('/admin/mitsumori/add', 'user_mitsumori', {**document_data, 'target_user_id': str(users['normal']['id']), 'vendor_id': str(vendor['id']) if vendor else ''},
         ('/admin/mitsumori/{}', '/admin/mitsumori/{}/edit', '/admin/mitsumori/{}/pdf')),
    ]
    for form_path, table, data, paths in admin_docs:
        before = row(f'SELECT MAX(id) AS id FROM {table}')['id'] or 0
        request('admin', form_path, 'POST', data, 'admin_document_create_fixture')
        created_doc = row(f'SELECT * FROM {table} WHERE id>? ORDER BY id DESC LIMIT 1', (before,))
        check(form_path + ' creates parent and line items', created_doc is not None)
        if not created_doc:
            continue
        for pattern in paths:
            path = pattern.format(created_doc['id'])
            response = request('admin', path, label='admin_document_actual_record')
            if table == 'user_mitsumori' and path.endswith('/edit'):
                check('legacy administrator estimate edit remains explicitly unavailable', response is not None and response.status_code == 302 and response.location.endswith('/admin/mitsumori'))
                report.setdefault('unavailable_legacy_features', []).append({'path': path, 'reason': 'Existing handler shows この機能は準備中です and redirects to list; edit is not implemented.'})
            else:
                check('admin document output ' + path, response is not None and response.status_code == 200 and len(response.get_data()) > 100)
            if table == 'user_mitsumori' and path.endswith('/pdf'):
                html_text = response.get_data(as_text=True) if response is not None else ''
                check('administrator estimate PDF sums item quantities', bool(re.search(r'合計点数</td>\s*<td[^>]*>\s*2\s*点', html_text)))
                artifact('admin-mitsumori-print.html', response)
            denied = request('normal', path, label='admin_document_user_denied')
            check('admin document not exposed to ordinary user ' + path, denied is not None and denied.status_code in {302, 403, 404})
        if table == 'user_mitsumori':
            for scope in ('vendor_outgoing', 'kaika_vendor_outgoing', 'kaika_estimate'):
                conn = module.get_db()
                try:
                    conn.execute('UPDATE user_mitsumori SET document_scope=? WHERE id=?', (scope, created_doc['id']))
                    conn.commit()
                finally:
                    conn.close()
                for output in ('view', 'download', 'pdf'):
                    denied = request('normal', f'/mitsumori/{output}/{created_doc["id"]}', label='vendor_document_hidden_from_client')
                    check(scope + ' estimate ' + output + ' remains outside user document scope', denied is not None and denied.status_code in {302, 403, 404})
                allowed = request('admin', f'/admin/mitsumori/{created_doc["id"]}/pdf', label='vendor_document_admin_export')
                check(scope + ' estimate remains printable for administrator', allowed is not None and allowed.status_code == 200)

    for path in ('/admin/invoices/view/1', '/admin/kaitori/1', '/admin/kaitori/1/edit', '/admin/kaitori/1/pdf'):
        response = request('admin', path, label='admin_invoice_actual_record')
        check('administrator can inspect existing invoice ' + path, response is not None and response.status_code == 200)

    # Complete the remaining local dynamic GET fixtures. These rows live only
    # in the disposable database and deliberately contain no contact accounts.
    def insert_fixture(table, values):
        conn = module.get_db()
        try:
            cursor = conn.execute(f'INSERT INTO {table} ({",".join(values)}) VALUES ({",".join("?" for _ in values)})', tuple(values.values()))
            result = cursor.lastrowid
            conn.commit()
            return result
        finally:
            conn.close()

    announcement_form = {'title': '確認対象者限定のお知らせ', 'content': '架空の確認用お知らせです。',
                         'recipient_scope': 'selected', 'recipient_user_ids': str(users['normal']['id'])}
    request('admin', '/admin/announcements/add', 'POST', dict(announcement_form), 'announcement_create_inactive')
    announcement = row('SELECT * FROM announcements ORDER BY id DESC LIMIT 1')
    check('targeted announcement creates inactive without external notification', announcement and not announcement['is_active'] and not announcement['line_notify_enabled'])
    if announcement:
        announcement_id = announcement['id']
        path = f'/announcements/{announcement_id}'
        denied = request('normal', path, label='inactive_announcement_hidden')
        check('inactive announcement is hidden from recipient', denied is not None and denied.status_code in {302, 403, 404})
        expect_page('admin', f'/admin/announcements/edit/{announcement_id}', '確認対象者限定のお知らせ')
        request('admin', f'/admin/announcements/edit/{announcement_id}', 'POST', {**announcement_form, 'is_active': '1'}, 'announcement_publish_selected')
        check('announcement edit persists active state', row('SELECT is_active FROM announcements WHERE id=?', (announcement_id,))['is_active'] == 1)
        expect_page('normal', path, '架空の確認用お知らせです。')
        denied = request('business', path, label='announcement_other_recipient_denied')
        check('targeted announcement rejects unselected user', denied is not None and denied.status_code in {302, 403, 404})
        denied = request('normal', f'/admin/announcements/edit/{announcement_id}', label='announcement_nonadmin_edit_denied')
        check('announcement management requires permission', denied is not None and denied.status_code in {302, 403, 404})

    for service in ('normal', 'photo_packing', 'wholesale', 'multi_listing', 'auction'):
        expect_page('admin', '/admin/fee-detail/' + service, '詳細')
        denied = request('normal', '/admin/fee-detail/' + service, label='fee_detail_nonadmin_denied')
        check('fee detail requires administrator ' + service, denied is not None and denied.status_code in {302, 403, 404})

    request('admin', '/admin/auction-keisan/add', 'POST', {**document_data, 'user_id': str(users['normal']['id'])}, 'admin_auction_keisan_create')
    admin_keisan = row('SELECT * FROM user_keisan WHERE is_admin_created=1 ORDER BY id DESC LIMIT 1')
    check('administrator can create fictional auction calculation', admin_keisan is not None)
    if admin_keisan:
        expect_page('admin', f'/admin/auction-keisan/{admin_keisan["id"]}/edit', '確認明細')
        denied = request('normal', f'/admin/auction-keisan/{admin_keisan["id"]}/edit', label='admin_auction_keisan_nonadmin_denied')
        check('administrator calculation edit rejects ordinary user', denied is not None and denied.status_code in {302, 403, 404})

    auction_id = insert_fixture('proxy_service_settings', {'auction_name': '確認代行オークション', 'is_public': 1,
                               'sale_mode': 'auction', 'start_datetime': '2026-01-01 00:00:00', 'end_datetime': '2099-12-31 23:59:59'})
    insert_fixture('proxy_service_auction_users', {'auction_id': auction_id, 'user_id': users['business']['id'], 'is_enabled': 1})
    status_path = f'/proxy-service/{auction_id}/status'
    response = request('business', status_path, label='proxy_status_allowed_real_auction')
    state = response.get_json(silent=True) if response is not None else None
    check('allowed user receives live auction status', response is not None and response.status_code == 200 and state and state.get('success') is True and isinstance(state.get('snapshot'), dict))
    for role in ('normal', 'anonymous'):
        denied = request(role, status_path, label='proxy_status_not_allowed')
        check('auction live status rejects ' + role, denied is not None and denied.status_code in {401, 403, 404})

    from PIL import Image
    mobile_filename = 'a' * 40 + '.jpg'
    private_media = Path(module.app.extensions['kaika_mobile_media'])
    private_media.mkdir(parents=True, exist_ok=True)
    mobile_image = private_media / mobile_filename
    with Image.open(Path(module.app.static_folder) / 'uploads' / 'preview' / 'sample-0.png') as photo:
        photo.convert('RGB').save(mobile_image, format='JPEG')
    mobile_relative = 'uploads/mobile/' + mobile_filename
    original_item = row('SELECT photo_path, additional_photos FROM merchandise WHERE id=10')
    old_photos = json.loads(original_item['additional_photos'] or '[]')
    if original_item['photo_path']:
        old_photos.insert(0, original_item['photo_path'])
    conn = module.get_db()
    try:
        conn.execute('UPDATE merchandise SET photo_path=?, additional_photos=? WHERE id=10', (mobile_relative, json.dumps(old_photos)))
        conn.commit()
    finally:
        conn.close()
    for role in ('normal', 'admin'):
        response = request(role, '/static/' + mobile_relative, label='mobile_photo_browser_owned')
        check('protected mobile photo is readable by ' + role, response is not None and response.status_code == 200 and
              response.get_data() == mobile_image.read_bytes() and 'no-store' in response.headers.get('Cache-Control', ''))
    for role in ('business', 'anonymous'):
        denied = request(role, '/static/' + mobile_relative, label='mobile_photo_browser_denied')
        check('protected mobile photo rejects ' + role, denied is not None and denied.status_code in {401, 403, 404})
    denied = request('normal', '/static/uploads/.mobile-private/' + mobile_filename, label='mobile_private_path_denied')
    check('mobile private physical path cannot bypass protected route', denied is not None and denied.status_code in {401, 403, 404})
    response = request('normal', '/item/10/download_all', label='mobile_and_web_photo_zip')
    try:
        with zipfile.ZipFile(io.BytesIO(response.get_data())) as archive:
            zip_has_mobile = mobile_image.read_bytes() in [archive.read(name) for name in archive.namelist()]
    except Exception:
        zip_has_mobile = False
    check('inventory photo ZIP includes protected mobile photo', response is not None and response.status_code == 200 and zip_has_mobile)
    # Model a resolved symlink into another upload directory without needing
    # Windows symlink privileges. Browser media and ZIP must enforce the same
    # private-root boundary even when the target remains inside uploads.
    from unittest.mock import patch
    original_realpath = os.path.realpath
    redirected_photo = original_realpath(Path(module.app.static_folder) / old_photos[0])
    private_filename_path = os.path.normcase(str(mobile_image))
    def redirect_mobile_realpath(path, *args, **kwargs):
        if os.path.normcase(os.fspath(path)) == private_filename_path:
            return redirected_photo
        return original_realpath(path, *args, **kwargs)
    with patch.object(os.path, 'realpath', side_effect=redirect_mobile_realpath):
        response = request('normal', '/item/10/download_all', label='mobile_zip_resolved_path_boundary')
    try:
        with zipfile.ZipFile(io.BytesIO(response.get_data())) as archive:
            ordinary_photos_only = len(archive.namelist()) == len(old_photos)
    except Exception:
        ordinary_photos_only = False
    check('mobile ZIP excludes resolved path outside private media', response is not None and response.status_code == 200 and ordinary_photos_only)

    if vendor:
        stage_request_id = insert_fixture('sales_agency_requests', {'user_id': users['business']['id'], 'service_type': 'wholesale', 'status': 'approved'})
        stage_item_id = insert_fixture('sales_agency_request_items', {'request_id': stage_request_id, 'merchandise_id': 6,
                                       'item_status': 'active', 'workflow_status': 'step1_pending'})
        request('admin', f'/admin/documents/sales-agency-item/{stage_item_id}/verify', 'POST', {}, 'step1_verify_item')
        check('item verification reaches vendor preparation stage', row('SELECT workflow_status FROM sales_agency_request_items WHERE id=?', (stage_item_id,))['workflow_status'] == 'step2_ready')
        step2_path = f'/admin/documents/step2/user/{users["business"]["id"]}/vendor-mitsumori'
        expect_page('admin', step2_path, '確認用')
        denied = request('normal', step2_path, label='step2_nonadmin_denied')
        check('vendor preparation form requires administrator', denied is not None and denied.status_code in {302, 403, 404})
        request('admin', step2_path, 'POST', {'vendor_id': str(vendor['id']), 'request_item_ids': str(stage_item_id),
                'issue_date': '2026-09-10', 'status': 'draft', 'subject': '確認ステップ2'}, 'step2_create_vendor_estimate')
        stage_item = row('SELECT * FROM sales_agency_request_items WHERE id=?', (stage_item_id,))
        check('vendor estimate advances item to waiting for vendor', stage_item['workflow_status'] == 'step3_vendor_wait' and bool(stage_item['vendor_mitsumori_id']))
        response_file = Path(module.app.static_folder) / 'uploads' / 'preview' / 'sample-0.png'
        vendor_response_id = insert_fixture('vendor_documents', {'user_id': users['business']['id'], 'client_id': users['business']['id'],
                'item_id': 6, 'source_request_id': stage_request_id, 'document_scope': 'user_flow', 'vendor_id': vendor['id'],
                'vendor_name': '確認業者', 'title': '確認ステップ3回答画像', 'vendor_amount': 1200, 'customer_amount': 1000,
                'original_filename': 'fictional-vendor.png', 'stored_path': 'uploads/preview/sample-0.png',
                'mime_type': 'image/png', 'file_size': response_file.stat().st_size, 'created_by': users['admin']['id'],
                'status': 'received', 'match_status': 'unlinked'})
        request('admin', f'/admin/vendor-documents/{vendor_response_id}/link-items', 'POST',
                {'request_item_ids': str(stage_item_id)}, 'step3_link_vendor_response')
        stage_item = row('SELECT * FROM sales_agency_request_items WHERE id=?', (stage_item_id,))
        check('vendor response link reaches client invoice stage', stage_item['workflow_status'] == 'step4_ready' and stage_item['vendor_document_id'] == vendor_response_id)
        step4_path = f'/admin/documents/step4/user/{users["business"]["id"]}/client-invoice'
        expect_page('admin', step4_path, '確認用')
        denied = request('normal', step4_path, label='step4_nonadmin_denied')
        check('client invoice preparation form requires administrator', denied is not None and denied.status_code in {302, 403, 404})
        request('admin', step4_path, 'POST', {'request_item_ids': str(stage_item_id),
                f'vendor_reference_amount_{stage_item_id}': '1200', f'client_payment_amount_{stage_item_id}': '1000',
                f'item_note_{stage_item_id}': '架空取引', 'issue_date': '2026-09-10'}, 'step4_create_client_invoice')
        stage_item = row('SELECT * FROM sales_agency_request_items WHERE id=?', (stage_item_id,))
        check('client invoice creation marks step4 sent', stage_item['workflow_status'] == 'step4_sent' and bool(stage_item['client_invoice_id']))
        if stage_item['client_invoice_id']:
            response = request('business', f'/invoices/view/{stage_item["client_invoice_id"]}', label='step4_client_receives_invoice')
            check('completed stage invoice is visible to its client', response is not None and response.status_code == 200)
            response = request('business', f'/invoices/pdf/{stage_item["client_invoice_id"]}', label='step4_client_printable_invoice')
            check('completed stage invoice is printable by its client', response is not None and response.status_code == 200)
            artifact('step4-client-invoice-print.html', response)

    # Exercise the real exception handler without disclosing diagnostic text.
    records = []
    class Capture(logging.Handler):
        def emit(self, record):
            records.append(record)
    previous_handlers = list(module.app.logger.handlers)
    previous_propagate = module.app.logger.propagate
    module.app.logger.handlers = [Capture()]
    module.app.logger.propagate = False
    try:
        with module.app.test_request_context('/'):
            try:
                raise RuntimeError('verification-private-diagnostic')
            except RuntimeError as exc:
                response = module.app.make_response(module.handle_exception(exc))
    finally:
        module.app.logger.handlers = previous_handlers
        module.app.logger.propagate = previous_propagate
    body = response.get_data(as_text=True)
    check('error page contains recovery action without internal diagnostics', response.status_code == 500 and
          '商品一覧へ戻る' in body and 'verification-private-diagnostic' not in body and 'Traceback' not in body)
    check('error diagnostics remain available in server log', bool(records) and bool(records[0].exc_info))


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    original = Path.cwd()
    with tempfile.TemporaryDirectory(prefix='kaika-site-verification-') as temporary:
        preview = Path(temporary).resolve()
        runtime = sandbox.mirror_source(preview, None, None)
        sandbox.isolate_environment(preview, 18769)
        connections = sandbox.install_runtime_boundary(preview, 18769)
        sys.path[:] = [str(runtime)] + [p for p in sys.path if p and not sandbox.inside(Path(p).resolve(), sandbox.SOURCE)]
        os.chdir(runtime)
        try:
            loaded = importlib.import_module('render_app')
            users = sandbox.seed_preview(loaded.module, preview)
            sandbox.install_preview_routes(loaded.module, preview, users, 18769)
            report = run(loaded.module, preview, users)
            print('SITE_VERIFICATION_JSON=' + json.dumps(report, ensure_ascii=False))
        finally:
            for connection in connections:
                connection.close()
            os.chdir(original)
    return 1 if (any(r['status'] >= 500 for r in report['requests']) or
                 any(r['result'] == 'fail' for r in report['workflow'] + report['source_syntax'] + report['template_syntax'])) else 0


if __name__ == '__main__':
    raise SystemExit(main())
