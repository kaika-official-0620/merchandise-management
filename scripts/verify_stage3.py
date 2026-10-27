"""Actual render_app HTTP, copied source + disposable fictional data only."""
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timezone
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
            self.current = {'action':attrs.get('action',''), 'fields':{}}
            self.forms.append(self.current)
        elif tag == 'input' and self.current is not None and attrs.get('type') == 'hidden':
            self.current['fields'][attrs.get('name')] = attrs.get('value','')

    def handle_endtag(self, tag):
        if tag == 'form':
            self.current = None

    def fields(self, action):
        return next((dict(f['fields']) for f in self.forms if urlsplit(f['action']).path == action), {})


def run(module, users):
    from item_registry import Actor, Registry
    from stage3_common import FinanceStore
    from stage3_profit import ProfitService
    app = module.app
    app.config['PROPAGATE_EXCEPTIONS'] = True
    app.logger.setLevel(logging.ERROR)
    clients = {role: app.test_client() for role in ('customer','other','staff','anonymous')}
    checks, requests = [], []
    def check(name, condition):
        checks.append({'name':name, 'pass':bool(condition)})
    def http(role, path, data=None):
        response = clients[role].open(path, method='GET' if data is None else 'POST', data=data,
            base_url='http://127.0.0.1:18783', headers={'User-Agent':'Mozilla/5.0 Stage3Fixture'})
        response.get_data()
        requests.append({'role':role,'method':'GET' if data is None else 'POST','path':path,'status':response.status_code})
        return response
    def form(page, action):
        response = http('staff',page)
        check('form opens '+action, response.status_code == 200)
        fields = Forms(response).fields(action)
        check('protected form '+action, bool(fields.get('operation_token')) and bool(fields.get('registry_csrf')))
        return fields
    def rows(sql, params=()):
        conn = module.get_db()
        try:
            return [dict(row) for row in conn.execute(sql,params)]
        finally:
            conn.close()
    def baseline():
        result = {}
        for table in ('user_invoices','user_keisan','admin_kaitori','admin_shikiriosho','monthly_settlements',
                      'invoice_payments','payment_transactions','business_charge_drafts','business_fund_records'):
            if not rows("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)):
                continue
            serialized = sorted(json.dumps(r,sort_keys=True,ensure_ascii=False,default=str) for r in rows('SELECT * FROM '+table))
            result[table] = hashlib.sha256(json.dumps(serialized).encode()).hexdigest()
        return result
    before = baseline()
    for role, account in (('customer','normal'),('other','business'),('staff','admin')):
        user = users[account]
        response = http(role,'/login',{'username':user['username'],'password':user['password']})
        check('real login '+role, response.status_code == 302 and '/login' not in (response.location or ''))
    check('anonymous login required', http('anonymous','/finance').status_code in (302,401))
    owner_id, staff_id = users['normal']['id'], users['admin']['id']
    staff = Actor(staff_id,True)
    conn = module.get_db()
    try:
        conn.execute('PRAGMA foreign_keys=ON')
        conn.execute('BEGIN IMMEDIATE')
        registry = Registry(conn)
        item = registry.create_item(staff,'架空・料金検証商品',owner_user_id=owner_id,custodian='kaika',
            occurred_at='2026-10-01T00:00:00+09:00',evidence_reference='fixture-created',source_key='stage3-created')
        item_id = item['physical_item_id']
        for kind, day in (('received',2),('photographed',3),('registration_completed',4)):
            registry.record_event(staff,item_id,kind,occurred_at=f'2026-10-{day:02d}T12:00:00+09:00',
                source_key='fixture-'+kind,evidence_reference='fixture-'+kind)
        for kind, day in (('photography',3),('registration',4),('dispatch',21)):
            registry.record_work(staff,item_id,kind,performed_at=f'2026-10-{day:02d}T11:00:00+09:00',
                source_key='fixture-work-'+kind,evidence_reference='fixture-work',duration_seconds=60,measurement_source='observed')
        for kind, day in (('sold',20),('shipped',21)):
            registry.record_event(staff,item_id,kind,occurred_at=f'2026-10-{day:02d}T12:00:00+09:00',
                source_key='fixture-'+kind,evidence_reference='fixture-'+kind)
        event_id = [e['event_id'] for e in registry.detail(staff,item_id)['events'] if e['kind']=='sold'][0]
        conn.commit()
    finally:
        conn.close()
    item_path = '/finance/items/'+item_id
    customer_path = '/finance/customers/'+str(owner_id)
    check('finance list customer',http('customer','/finance').status_code == 200)
    check('finance list staff',http('staff','/finance').status_code == 200)
    check('own item readable',http('customer',item_path).status_code == 200)
    check('other item denied',http('other',item_path).status_code in (403,404))
    check('other customer summary denied',http('other',customer_path).status_code == 403)
    check('customer cannot write facts',http('customer',item_path+'/facts',{}).status_code == 403)
    check('staff CSRF missing denied',http('staff',item_path+'/facts',{}).status_code == 400)
    body = http('staff',item_path).get_data(as_text=True)
    check('unknown original amount displayed', '未確認' in body and 'name="purchase_cost_yen" value=""' in body)
    fact_fields = form(item_path,item_path+'/facts')
    facts = {**fact_fields, 'purchase_cost_yen':'30000','sale_gross_yen':'40000','channel_fee_yen':'4000',
        'shipping_cost_yen':'750','shipping_source_id':'fixture-delivery-1','materials_yen':'100',
        'kaika_fee_yen':'1155','return_adjustment_yen':'0','monthly_allocation_yen':'',
        'channel':'multichannel','shipping_scope':'separate','basis':'actual','sale_event_id':event_id,
        'occurred_at':'2026-10-22T10:00:00','source_reference':'fixture-order-delivery'}
    check('facts stored',http('staff',item_path+'/facts',facts).status_code == 302)
    check('same facts retry accepted',http('staff',item_path+'/facts',facts).status_code == 302)
    check('facts retry adds no version',len(rows('SELECT version_id FROM stage3_item_financial_facts')) == 1)
    check('different same token blocked',http('staff',item_path+'/facts',{**facts,'purchase_cost_yen':'1'}).status_code == 409)
    check('known subtotal is 3995', '3,995円' in http('customer',item_path).get_data(as_text=True))
    for status in ('unpaid','paid','settlement_ready'):
        data = {**form(item_path,item_path+'/status'),'status':status,'occurred_at':'2026-10-22T11:00:00',
                'source_reference':'fixture-'+status}
        check('sale status '+status,http('staff',item_path+'/status',data).status_code == 302)
    agreement_fields = form(customer_path,customer_path+'/agreement')
    agreement = {**agreement_fields,'contract_version_id':'fixture-contract','signed_document_reference':'fixture-signed',
        'consent_reference':'fixture-consent','consent_at':'2026-10-01T00:00:00','charge_start_at':'2026-10-01T00:00:00',
        'exception_status':'none','note':'Fictional, not actual consent','status':'fixture_approved','work_scope_confirmed':'true'}
    check('agreement saved as draft',http('staff',customer_path+'/agreement',agreement).status_code == 302)
    agreed = rows('SELECT * FROM stage3_agreements')[0]
    check('agreement cannot be live',agreed['status']=='draft')
    evidence = json.loads(agreed['evidence_json'])
    check('request cannot approve bundle scope',evidence['work_scope_confirmed'] is False)
    check('operator tax and time choices reflected',evidence['rounding_method']=='floor' and evidence['billing_timezone']=='Asia/Tokyo')
    data = {**form(item_path,item_path+'/drafts'),'agreement_id':agreed['agreement_id']}
    check('shipping candidate draft creation',http('staff',item_path+'/drafts',data).status_code == 302)
    check('shipping candidate duplicate retry',http('staff',item_path+'/drafts',data).status_code == 302)
    check('one bundle and one sales fee', len(rows('SELECT draft_id FROM stage3_charge_drafts')) == 2)
    monthly = {**form(customer_path,customer_path+'/month'),'period':'2026-10'}
    check('monthly draft created',http('staff',customer_path+'/month',monthly).status_code == 302)
    invoice = rows('SELECT * FROM stage3_invoice_revisions')[0]
    invoice_path = '/finance/months/'+invoice['invoice_id']
    check('own invoice readable',http('customer',invoice_path).status_code == 200)
    check('other invoice denied',http('other',invoice_path).status_code == 403)
    csv = http('customer',invoice_path+'/export.csv')
    check('CSV own draft exported safely',csv.status_code == 200 and '実請求' in csv.get_data(as_text=True))
    check('other CSV denied',http('other',invoice_path+'/export.csv').status_code == 403)
    check('no payable total',json.loads(invoice['snapshot_json'])['totals']['payable_total_yen'] is None)
    duplicate_month = {**form(customer_path,customer_path+'/month'),'period':'2026-10'}
    check('same month content accepted',http('staff',customer_path+'/month',duplicate_month).status_code == 302)
    check('same content no new monthly revision',len(rows('SELECT invoice_id FROM stage3_invoice_revisions')) == 1)
    component = {**form(customer_path,customer_path+'/component'),'fee_kind':'expense','amount_net_yen':'100',
        'tax_rate_percent':'','occurred_at':'2026-10-22T12:00:00','evidence_reference':'=fictional-receipt',
        'item_id':item_id,'agreement_id':agreed['agreement_id']}
    check('unknown tax expense stays draft',http('staff',customer_path+'/component',component).status_code==302)
    check('duplicate expense retry',http('staff',customer_path+'/component',component).status_code==302)
    check('expense onlyonce',len(rows("SELECT draft_id FROM stage3_charge_drafts WHERE fee_kind='expense'"))==1)
    saved = http('customer',invoice_path).get_data(as_text=True)
    check('new evidence disclosed on old invoice','保存後の変更' in saved)
    conn = module.get_db()
    try:
        pending = ProfitService(FinanceStore(Registry(conn))).profit(staff,item_id)
        check('pending Kaika fee prevents realized profit',pending['realized_profit_yen'] is None and
              'kaika_fee_draft_pending' in pending['review_reasons'])
    finally:
        conn.close()
    app.config['STAGE3_TEST_LEDGER_ENABLED'] = True
    funds = {**form(customer_path,customer_path+'/test-funds'),'kind':'deposit','amount_yen':'50000',
             'occurred_at':'2026-10-22T12:00:00','evidence_reference':'fixture-fake-deposit'}
    check('test deposit record',http('staff',customer_path+'/test-funds',funds).status_code == 302)
    check('test deposit retry',http('staff',customer_path+'/test-funds',funds).status_code == 302)
    check('test deposit onlyonce',len(rows('SELECT record_id FROM stage3_fund_records')) == 1)
    check('own test record distinctly labelled','実際のお金ではありません' in http('customer',customer_path).get_data(as_text=True))
    app.config['STAGE3_TEST_LEDGER_ENABLED'] = False
    check('test ledger disabled blocks mutation',http('staff',customer_path+'/test-funds',funds).status_code == 404)
    conn = module.get_db()
    try:
        registry = Registry(conn)
        registry.record_event(staff,item_id,'returned',occurred_at='2026-10-23T12:00:00+09:00',
            source_key='fixture-return',evidence_reference='fixture-return',details={'return_reason':'fixture-damaged'})
        profit = ProfitService(FinanceStore(registry)).profit(staff,item_id)
        check('return holds realized profit',profit['realized_profit_yen'] is None and profit['recognition']=='return_hold')
        conn.commit()
    finally:
        conn.close()
    check('invoice remains viewable after return',http('customer',invoice_path).status_code == 200)
    check('return review generates revised snapshot',http('staff',customer_path+'/month',
          {**form(customer_path,customer_path+'/month'),'period':'2026-10'}).status_code == 302)
    check('monthly revision preserves original',len(rows('SELECT invoice_id FROM stage3_invoice_revisions')) == 2)
    revised = rows('SELECT * FROM stage3_invoice_revisions ORDER BY revision DESC')[0]
    csv = http('customer','/finance/months/'+revised['invoice_id']+'/export.csv').get_data(as_text=True)
    check('CSV formula-like source escaped',"'=fictional-receipt" in csv)
    check('legacy money records unchanged',baseline()==before)
    check('PSP scheduler disabled',not module.STRIPE_ENABLED and not module.SCHEDULER_ENABLED)
    app.config['STAGE3_FINANCE_ENABLED'] = False
    check('flag off does not expose new finance',http('staff','/finance').status_code==404)
    return {'scope':'Copied render_app, real Web login, fictional isolated SQLite only',
        'checks':checks,'requests':requests,'passed':sum(x['pass'] for x in checks),
        'failed':sum(not x['pass'] for x in checks),'server_errors':sum(x['status']>=500 for x in requests),
        'not_verified':['production data','PostgreSQL','native device','store','real payment','real notification']}


def main():
    sys.stdout.reconfigure(encoding='utf-8',errors='replace')
    original = Path.cwd()
    preview = Path(tempfile.mkdtemp(prefix='kaika-stage3-verification-')).resolve()
    runtime = sandbox.mirror_source(preview,None,None)
    sandbox.isolate_environment(preview,18783)
    connections = sandbox.install_runtime_boundary(preview,18783)
    sys.path[:] = [str(runtime)] + [p for p in sys.path if p and not sandbox.inside(Path(p).resolve(),sandbox.SOURCE)]
    os.chdir(runtime)
    diagnostics = io.StringIO()
    try:
        with redirect_stdout(diagnostics), redirect_stderr(diagnostics):
            loaded = importlib.import_module('render_app')
            users = sandbox.seed_preview(loaded.module,preview)
            from stage2_migration import prepare_stage2_fixture
            from stage3_migration import prepare_stage3_fixture
            prepare_stage2_fixture(loaded.module,allowed_test_path=preview/'preview.sqlite3')
            prepare_stage3_fixture(loaded.module,allowed_test_path=preview/'preview.sqlite3')
            loaded.module.app.config.update(STAGE2_INVENTORY_ENABLED=True,STAGE2_FREE_LIMIT_PROPOSAL_ENABLED=True,
                                           STAGE3_FINANCE_ENABLED=True)
            report = run(loaded.module,users)
    except Exception as exc:
        report = {'scope':'isolated Stage3 integration','passed':0,'failed':1,'server_errors':1,
            'error_type':type(exc).__name__,'error':str(exc),'traceback':traceback.format_exc()}
    finally:
        for conn in connections:
            conn.close()
        os.chdir(original)
    if report['failed'] or report['server_errors']:
        report['isolated_runtime_diagnostics'] = diagnostics.getvalue()[-18000:]
    path = preview/'verification.json'
    path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    summary = {k:report.get(k) for k in ('passed','failed','server_errors','error_type','error')}
    summary.update(failed_checks=[c['name'] for c in report.get('checks',[]) if not c['pass']],
                   requests=len(report.get('requests',[])),report_path=str(path))
    print('STAGE3_VERIFICATION='+json.dumps(summary,ensure_ascii=False))
    return 1 if report['failed'] or report['server_errors'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
