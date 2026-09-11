"""STEP4 tax workflow regression, in the disposable no-network preview only.

Writes JSON with base64 fictional artifacts to stdout. Run in a fresh process;
the preview audit boundary intentionally persists until the process exits.
"""
import base64
import importlib
import json
import mimetypes
import os
from pathlib import Path
import re
import sys
import tempfile

import run_app_preview as sandbox


def run(module, preview, users):
    app = module.app
    app.config['PROPAGATE_EXCEPTIONS'] = True
    report = {'checks': [], 'artifacts': [], 'requests': [], 'fictional_data_only': True}
    base = 'http://127.0.0.1:18779'
    clients = {role: app.test_client() for role in ('normal', 'business', 'admin')}
    for role, client in clients.items():
        client.get('/__preview/start/' + role, base_url=base)

    def check(name, passed):
        report['checks'].append({'name': name, 'pass': bool(passed)})

    def request(role, path, data=None):
        response = clients[role].open(path, method='POST' if data is not None else 'GET', data=data, base_url=base)
        report['requests'].append({'role': role, 'path': path, 'method': 'POST' if data is not None else 'GET', 'status': response.status_code})
        response.get_data()
        response.close()
        return response

    def row(sql, args=()):
        with module.get_db() as conn:
            value = conn.execute(sql, args).fetchone()
            return dict(value) if value else None

    def insert(table, data):
        with module.get_db() as conn:
            columns = {r[1] for r in conn.execute('PRAGMA table_info(' + table + ')')}
            data = {k: v for k, v in data.items() if k in columns}
            cursor = conn.execute('INSERT INTO ' + table + '(' + ','.join(data) + ') VALUES (' + ','.join('?' for _ in data) + ')', tuple(data.values()))
            return cursor.lastrowid

    def artifact(name, response):
        content = response.get_data()
        if response.mimetype == 'text/html':
            def embed(match):
                path = (Path(app.static_folder) / match.group(1)).resolve()
                if not sandbox.inside(path, preview) or not path.is_file():
                    return match.group(0)
                mime = mimetypes.guess_type(path.name)[0] or 'application/octet-stream'
                return 'src="data:' + mime + ';base64,' + base64.b64encode(path.read_bytes()).decode('ascii') + '"'
            content = re.sub(r'src="/static/([^"?]+)"', embed, response.get_data(as_text=True)).encode('utf-8')
        report['artifacts'].append({'name': name, 'mime': response.mimetype, 'base64': base64.b64encode(content).decode('ascii')})

    user_id = users['normal']['id']
    step4_url = f'/admin/documents/step4/user/{user_id}/client-invoice'
    request('admin', step4_url)  # Builds the same schema as the real entry point.
    with module.get_db() as conn:
        inventory_ids = [r[0] for r in conn.execute('SELECT id FROM merchandise WHERE user_id=? ORDER BY id', (user_id,))]
    vendor_id = insert('vendors', {'name': '【架空】税額確認業者', 'created_by': users['admin']['id']})
    vendor_document_id = insert('vendor_documents', {'vendor_id': vendor_id, 'vendor_name': '【架空】税額確認業者',
        'document_scope': 'user_flow', 'original_filename': 'fictional.png', 'stored_path': 'uploads/preview/sample-0.png',
        'title': '【架空】税額検証回答', 'status': 'received', 'match_status': 'unlinked', 'vendor_amount': 2000,
        'created_by': users['admin']['id']})

    def prepare(amounts):
        request_id = insert('sales_agency_requests', {'user_id': user_id, 'service_type': 'wholesale', 'status': 'approved'})
        ids = [insert('sales_agency_request_items', {'request_id': request_id, 'merchandise_id': inventory_ids[index % len(inventory_ids)],
                'item_status': 'active', 'workflow_status': 'step4_ready', 'vendor_document_id': vendor_document_id})
               for index, _ in enumerate(amounts)]
        data = {'request_item_ids': [str(value) for value in ids], 'issue_date': '2026-09-11',
                'recipient_name': '【架空】税額検証のお客様', 'notes': '検証専用の架空取引です。実際の請求・支払には使用できません。'}
        for value, (amount, category) in zip(ids, amounts):
            data[f'vendor_reference_amount_{value}'] = str(amount + 200)
            data[f'client_payment_amount_{value}'] = str(amount)
            if category is not None:
                data[f'tax_category_{value}'] = category
        return ids, data

    def outputs(invoice_id, name, expected, unwanted=()):
        for role, path, suffix in [('normal', f'/invoices/view/{invoice_id}', 'view.html'),
                                  ('admin', f'/admin/kaitori/{invoice_id}', 'admin.html'),
                                  ('normal', f'/invoices/pdf/{invoice_id}', 'print.html'),
                                  ('normal', f'/invoices/download/{invoice_id}', 'download.csv')]:
            response = request(role, path)
            text = response.get_data(as_text=True)
            check(name + ' ' + suffix + ' contains matching breakdown', response.status_code == 200 and all(v in text for v in expected) and all(v not in text for v in unwanted))
            if suffix in {'print.html', 'download.csv'}:
                artifact(name + '-' + suffix, response)
            if role == 'normal':
                check(name + ' cross owner denied ' + suffix, request('business', path).status_code in {302, 403, 404})

    for name, amounts, expected_taxes, markers in [
            ('tax-standard', [(1000, '10')], (0, 90), ['10%', '90', '1,000']),
            ('tax-mixed', [(1100, '10'), (1080, '8'), (500, 'outside')], (80, 100), ['10%', '8%', '対象外', '2,680']),
            ('tax-round-once', [(6, '10'), (6, '10')], (0, 1), ['うち消費税等（10%）', '12']),
            ('tax-unknown', [(1000, None)], (0, 0), ['未確認', '1,000']),
            ('tax-exempt-zero', [(0, 'exempt')], (0, 0), ['非課税', '0'])]:
        ids, data = prepare(amounts)
        form = request('admin', step4_url)
        check(name + ' tax selection form', form.status_code == 200 and 'data-tax-category' in form.get_data(as_text=True))
        if name == 'tax-standard':
            artifact('tax-step4-form.html', form)
        response = request('admin', step4_url, data)
        invoice_id = row('SELECT client_invoice_id FROM sales_agency_request_items WHERE id=?', (ids[0],))['client_invoice_id']
        check(name + ' create succeeds', response.status_code == 302 and invoice_id)
        invoice = row('SELECT * FROM invoices WHERE id=?', (invoice_id,))
        check(name + ' preserves entered gross and saves rate totals', invoice['total_amount'] == sum(a for a, _ in amounts) and
              (invoice['tax_amount_8'], invoice['tax_amount_10']) == expected_taxes and invoice['tax_breakdown_version'] == 1)
        check(name + ' advances existing workflow', row('SELECT workflow_status FROM sales_agency_request_items WHERE id=?', (ids[0],))['workflow_status'] == 'step4_sent')
        outputs(invoice_id, name, markers)
        if name == 'tax-standard':
            with module.get_db() as conn:
                conn.execute("UPDATE invoices SET tax_breakdown_version=0,tax_rounding_method=NULL,tax_amount_8=0,tax_amount_10=0 WHERE id=?", (invoice_id,))
                conn.execute("UPDATE invoice_items SET tax_category='0' WHERE invoice_id=?", (invoice_id,))
            before = row('SELECT subtotal,total_amount,tax_amount_8,tax_amount_10,tax_breakdown_version FROM invoices WHERE id=?', (invoice_id,))
            outputs(invoice_id, 'tax-legacy', ['未確認', '1,000'], ['内税(10%)', 'うち消費税等（10%）'])
            check('legacy reading never rewrites financial data', before == row('SELECT subtotal,total_amount,tax_amount_8,tax_amount_10,tax_breakdown_version FROM invoices WHERE id=?', (invoice_id,)))

    ids, data = prepare([(1000, '0')])
    before = row('SELECT COUNT(*) AS count FROM invoices')['count']
    request('admin', step4_url, data)
    check('invalid category rejects without creating invoice or advancing item', before == row('SELECT COUNT(*) AS count FROM invoices')['count'] and
          row('SELECT workflow_status FROM sales_agency_request_items WHERE id=?', (ids[0],))['workflow_status'] == 'step4_ready')
    data[f'tax_category_{ids[0]}'] = '10'
    data[f'client_payment_amount_{ids[0]}'] = '-1'
    request('admin', step4_url, data)
    check('negative gross rejects without creating invoice', before == row('SELECT COUNT(*) AS count FROM invoices')['count'])

    request('normal', '/invoices/add', {'issue_date': '2026-09-11', 'recipient_name': '【架空】自己作成',
        'status': 'draft', 'product_name[]': '【架空】従来明細', 'quantity[]': '2', 'unit_price[]': '1200', 'unit[]': '点', 'tax_category[]': '10'})
    invoice = row('SELECT * FROM invoices ORDER BY id DESC LIMIT 1')
    check('self created subtotal plus tax convention remains unchanged', invoice['subtotal'] == 2400 and invoice['tax_amount_10'] == 240 and invoice['total_amount'] == 2640)
    outputs(invoice['id'], 'tax-self-created', ['240', '2,640'], ['税区分・税額内訳は未確認'])
    pdf = request('normal', f'/invoices/pdf/{invoice["id"]}')
    check('self created printed headings remain tax exclusive', '金額(税抜)' in pdf.get_data(as_text=True))
    for path in ('/inquiry/new', '/sales-agency/my-requests'):
        check('service guide target ' + path, request('business', path).status_code == 200)
    with app.test_request_context('/'):
        guide = app.jinja_env.get_template('_sales_agency_service_guide.html').render()
    check('guide explains all methods fees and cancellation', all(text in guide for text in ['業者卸販売', '同時出品', '業者オークション', '手数料', '取消申請だけでは取消完了']))
    report['summary'] = {'passed': sum(c['pass'] for c in report['checks']), 'failed': sum(not c['pass'] for c in report['checks']),
                         'requests': len(report['requests'])}
    return report


def main():
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    original = Path.cwd()
    with tempfile.TemporaryDirectory(prefix='kaika-tax-verification-') as temporary:
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
            report = run(loaded.module, preview, users)
            print('INVOICE_TAX_JSON=' + json.dumps(report, ensure_ascii=False))
        finally:
            for connection in connections:
                connection.close()
            os.chdir(original)
    return 1 if report['summary']['failed'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
