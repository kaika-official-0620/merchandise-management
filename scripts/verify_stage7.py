"""Stage 7 real Flask forms in a disposable, network-denied copied runtime.

No source database, environment file, real photograph, customer or money is used.
"""
from contextlib import redirect_stdout, redirect_stderr
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
from verify_stage5 import Forms


def run(module, users):
    from item_registry import Actor, Registry
    from stage7_common import Stage7Store
    from stage7_measurement import MeasurementService
    app = module.app
    app.config['PROPAGATE_EXCEPTIONS'] = True
    app.logger.setLevel(logging.ERROR)
    app.extensions['stage7_clock'] = lambda: '2026-10-22T03:00:00+00:00'
    clients = {role: app.test_client() for role in ('customer','staff','inventory','anonymous')}
    checks, requests = [], []
    actor, owner = Actor(users['admin']['id'], True), users['normal']['id']

    def check(name, condition):
        checks.append({'name': name, 'pass': bool(condition)})

    def http(role, path, data=None):
        try:
            response = clients[role].open(path, method='GET' if data is None else 'POST', data=data,
                base_url='http://127.0.0.1:18787', headers={'User-Agent':'Mozilla/5.0 Stage7Fixture'})
            response.get_data()
            requests.append(dict(role=role, method='GET' if data is None else 'POST', path=urlsplit(path).path, status=response.status_code))
            return response
        except Exception:
            requests.append(dict(role=role, method='GET' if data is None else 'POST', path=urlsplit(path).path, status=500))
            raise

    def form(role, page, action):
        response = http(role, page)
        check('form page ' + role + ' ' + action, response.status_code == 200)
        fields = Forms(response).fields(action)
        check('protected form ' + role + ' ' + action, bool(fields.get('operation_token')) and bool(fields.get('registry_csrf')))
        return fields

    def post(name, role, page, action, values=None, expected=302):
        response = http(role, action, {**form(role,page,action), **(values or {})})
        check(name, response.status_code == expected)
        if response.status_code != expected:
            raise AssertionError(name + ': status ' + str(response.status_code) + ' body=' + response.get_data(as_text=True)[-1800:])
        return response

    def rows(sql, params=()):
        conn = module.get_db()
        try:
            return [dict(row) for row in conn.execute(sql, params)]
        finally:
            conn.close()

    def action(callback):
        conn = module.get_db()
        try:
            conn.execute('PRAGMA foreign_keys=ON')
            conn.execute('BEGIN IMMEDIATE')
            result = callback(Stage7Store(Registry(conn)))
            conn.commit()
            return result
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def protected_baseline():
        names = [row['name'] for row in rows("SELECT name FROM sqlite_master WHERE type='table'")]
        selected = [name for name in names if name not in ('stage3_customer_locks','stage4_operations','stage4_locks') and
            (name.startswith(('business_', 'stage2_', 'stage3_', 'stage4_', 'stage5_', 'stage6_', 'feature_', 'push_'))
            or name in ('merchandise','user_invoices','invoice_payments','payment_transactions','monthly_settlements'))]
        return {table: hashlib.sha256(json.dumps(sorted(json.dumps(row, sort_keys=True, default=str)
            for row in rows('SELECT * FROM ' + table))).encode()).hexdigest() for table in selected}

    try:
        # This is a new fixture-only user; no business customer is promoted.
        inventory_id = action(lambda store: store.execute('''INSERT INTO users
            (username,email,password_hash,role,display_name,subscription_status,tuition_exempt,admin_permissions)
            SELECT 'preview_inventory','inventory@preview.invalid',password_hash,'admin','架空の商品管理担当',
                'active',1,'["merchandise"]' FROM users WHERE id=?''', (users['business']['id'],)).lastrowid)
        inventory = dict(username='preview_inventory', password=users['business']['password'], id=inventory_id)
        for role, account in (('customer',users['normal']), ('staff',users['admin']), ('inventory',inventory)):
            response = http(role, '/login', {'username':account['username'], 'password':account['password']})
            check('real login ' + role, response.status_code == 302 and '/login' not in (response.location or ''))
        check('Stage7 disabled by default', app.config.get('STAGE7_ENABLED') is not True)
        for path in ('/pilot-review','/pilot-review/costs','/pilot-review/cohorts','/pilot-review/scenarios'):
            check('disabled route ' + path, http('staff',path).status_code == 404)
        app.config['STAGE7_ENABLED'] = True
        check('anonymous requires login', http('anonymous','/pilot-review').status_code in (302,401))
        check('customer cannot inspect company pilot', http('customer','/pilot-review').status_code == 403)
        app.config['STAGE6_ENABLED'] = False
        check('missing dependency closes stage', http('staff','/pilot-review').status_code == 404)
        app.config['STAGE6_ENABLED'] = True
        conn=module.get_db()
        try:
            version=conn.execute('SELECT version FROM stage7_schema').fetchone()[0]
            conn.execute('DELETE FROM stage7_schema')
            conn.commit()
            check('missing migration marker refuses stage',http('staff','/pilot-review').status_code==409)
            conn.execute('INSERT INTO stage7_schema(version) VALUES(?)',(version,))
            conn.commit()
        finally:
            conn.close()

        def seed(store):
            result=[]
            for number in (1,2):
                item=store.registry.create_item(actor, '架空の段階7商品 '+str(number), owner_user_id=owner,
                    custodian='self',occurred_at='2026-10-19T00:00:00+09:00',evidence_reference='stage7-fixture',source_key='stage7-item-'+str(number))['physical_item_id']
                for minute,kind in enumerate(('received','photographed','registration_completed'),1):
                    store.registry.record_event(actor,item,kind,f'2026-10-20T01:0{minute}:00+09:00',f'stage7-{number}-{kind}','fixture-source')
                store.registry.record_work(actor,item,'photography','2026-10-20T01:02:00+09:00',f'stage7-work-{number}','fixture-measured',
                    duration_seconds=60 if number==1 else None,measurement_source='observed' if number==1 else 'unknown')
                if number==1:
                    store.registry.record_event(actor,item,'sold','2026-10-21T01:00:00+09:00','stage7-sale','fixture-sale')
                    store.registry.record_event(actor,item,'shipped','2026-10-21T02:00:00+09:00','stage7-ship','fixture-shipment')
                result.append(item)
            return result
        items = action(seed)
        baseline = protected_baseline()
        index = http('staff','/pilot-review')
        check('staff pilot page renders',index.status_code==200)
        check('private no-store',index.headers.get('Cache-Control')=='private, no-store')
        check('draft and undecided responsible staff visible','下書き' in index.get_data(as_text=True) and '未設定' in index.get_data(as_text=True))
        check('inventory staff can review operational pilot',http('inventory','/pilot-review').status_code==200)
        for role in ('customer','inventory'):
            for path in ('/pilot-review/costs','/pilot-review/scenarios'):
                check(role+' denied financial page '+path,http(role,path).status_code==403)
        check('CSRF absent denied',http('staff','/pilot-review/campaigns',{}).status_code==400)
        fields=form('staff','/pilot-review','/pilot-review/campaigns')
        campaign_data=dict(title='架空の実証 <escape>',phase='pilot5',planned_start_at='2026-10-20T00:00',planned_end_at='2026-11-03T00:00')
        created=http('staff','/pilot-review/campaigns',{**fields,**campaign_data})
        check('campaign creates draft',created.status_code==302)
        campaign_path=urlsplit(created.location or '').path
        campaign_id=campaign_path.rsplit('/',1)[-1]
        repeated=http('staff','/pilot-review/campaigns',{**fields,**campaign_data})
        check('campaign same token idempotent',repeated.status_code==302 and repeated.location==created.location and len(rows('SELECT * FROM stage7_campaigns'))==1)
        check('same token changed payload denied',http('staff','/pilot-review/campaigns',{**fields,**campaign_data,'title':'different'}).status_code==409)
        check('foreign actor CSRF token denied',http('inventory','/pilot-review/campaigns',{**fields,**campaign_data}).status_code==400)
        body=http('staff',campaign_path).get_data(as_text=True)
        check('campaign title escaped','&lt;escape&gt;' in body and '<escape>' not in body)
        check('campaign remains draft',rows('SELECT status FROM stage7_campaigns')[0]['status']=='draft')
        post('invalid planning period rejected','staff','/pilot-review','/pilot-review/campaigns',dict(campaign_data,planned_end_at='2026-10-19T00:00'),400)
        post('inventory cannot create paid experiment','inventory','/pilot-review','/pilot-review/campaigns',dict(campaign_data,title='paid',paid='yes'),403)
        post('novice cannot be mixed with existing participants','staff',campaign_path,campaign_path+'/participants',dict(customer_id=owner,experience='novice'),400)
        post('existing customer candidate enrolled','staff',campaign_path,campaign_path+'/participants',dict(customer_id=owner,experience='existing_reseller'))
        post('duplicate candidate blocked','staff',campaign_path,campaign_path+'/participants',dict(customer_id=owner,experience='existing_reseller'),409)
        check('customer cannot read campaign detail',http('customer',campaign_path).status_code==403)
        observe_path=campaign_path+'/observations'
        def observe(name,event,**changes):
            data=dict(customer_id=owner,event_type=event,occurred_at='2026-10-21T10:00',basis='observed',execution_mode='record_only',evidence_reference='fixture-'+event,outcome='')
            data.update(changes)
            expected=data.pop('expected',302)
            return post(name,'staff',campaign_path,observe_path,data,expected)
        observe('registration start recorded','registration_started')
        observe('registration complete recorded','registration_completed')
        observe('unknown support duration stays unknown','support')
        observe('major task observed','major_task_attempt',outcome='yes',duration_seconds=90)
        original=rows("SELECT * FROM stage7_observations WHERE event_type='major_task_attempt'")[0]
        observe('observation correction appends history','major_task_attempt',outcome='no',duration_seconds=95,
            correction_of=original['observation_id'],correction_reason='observer corrected result',evidence_reference='fixture-corrected')
        check('correction history contains both records',len(rows("SELECT * FROM stage7_observations WHERE event_type='major_task_attempt'"))==2)
        observe('same observation cannot be corrected twice','major_task_attempt',outcome='no',duration_seconds=95,
            correction_of=original['observation_id'],correction_reason='duplicate',evidence_reference='duplicate-correction',expected=409)
        observe('future actual observation rejected','support',occurred_at='2026-10-23T10:00',expected=400)
        observe('mode mismatch rejected','support',basis='test',execution_mode='record_only',expected=400)
        observe('negative duration rejected','support',duration_seconds='-1',expected=400)
        observe('financial observation recorded without payment execution','paid_offer',evidence_reference='FINANCE_SECRET_FIXTURE')
        financial_data=dict(customer_id=owner,event_type='payment_success',occurred_at='2026-10-21T10:00',basis='observed',execution_mode='record_only',evidence_reference='forbidden-finance')
        post('inventory cannot write financial observation','inventory',campaign_path,observe_path,financial_data,403)
        inventory_body=http('inventory',campaign_path).get_data(as_text=True)
        check('financial evidence hidden from inventory staff','FINANCE_SECRET_FIXTURE' not in inventory_body and 'value="payment_success"' not in inventory_body)
        staff_body=http('staff',campaign_path).get_data(as_text=True)
        check('staff history retains evidence','fixture-corrected' in staff_body and 'FINANCE_SECRET_FIXTURE' in staff_body)
        check('retention not presumed after short observation','期間未経過' in staff_body and '未確認' in staff_body)

        costs='/pilot-review/costs'
        cost_data=dict(period='2026-10',scope='free_digital',category='hosting',amount_yen=100,basis='measured',tax_basis='exclusive',reference='fixture-invoice/line-1')
        post('cost evidence recorded','staff',costs,costs,cost_data)
        entry=rows('SELECT * FROM stage7_economic_entries')[0]
        post('same evidence not duplicated','staff',costs,costs,cost_data,409)
        post('money decimals rejected','staff',costs,costs,dict(cost_data,reference='decimal',amount_yen='1.5'),400)
        post('unknown amount recorded explicitly','staff',costs,costs,dict(cost_data,reference='pending',amount_yen=''))
        post('reference costs remain separate','staff',costs,costs,dict(cost_data,reference='reference-cost',amount_yen=500,basis='reference'))
        corrected=dict(period='2026-10',amount_yen=150,basis='measured',tax_basis='exclusive',reference='fixture-correction',reason='invoice revised')
        post('cost correction append only','staff',costs,costs+'/'+entry['entry_id']+'/corrections',corrected)
        check('cost history preserved',len(rows('SELECT * FROM stage7_economic_revisions WHERE entry_id=?',(entry['entry_id'],)))==2)
        before_correction=rows('SELECT * FROM stage7_economic_revisions WHERE entry_id=?',(entry['entry_id'],))
        post('missing cost correction period rejected before mutation','staff',costs,costs+'/'+entry['entry_id']+'/corrections',dict(corrected,period='',amount_yen=999),400)
        check('invalid cost correction changed no revision',rows('SELECT * FROM stage7_economic_revisions WHERE entry_id=?',(entry['entry_id'],))==before_correction)
        history_path=costs+'/'+entry['entry_id']
        history=http('staff',history_path)
        history_body=history.get_data(as_text=True)
        check('cost correction history shows source and revised evidence',history.status_code==200 and 'fixture-correction' in history_body and 'invoice revised' in history_body)
        check('cost history restricted from customer and inventory staff',http('customer',history_path).status_code==403 and http('inventory',history_path).status_code==403)
        cost_body=http('staff',costs+'?period=2026-10&free_digital=3').get_data(as_text=True)
        check('cost amount unknown and basis labels visible','未確認' in cost_body and '参考' in cost_body and '実測' in cost_body)
        check('invalid cost denominator rejected',http('staff',costs+'?free_digital=-1').status_code==400)
        resource=dict(period='2026-10',scope='free_digital',metric='image_bytes',quantity='',basis='reference',reference='fixture-storage')
        post('resource unknown recorded','staff',costs,'/pilot-review/resources',resource)
        resource_id=rows("SELECT entry_id FROM stage7_economic_entries WHERE kind='resource'")[0]['entry_id']
        post('resource same month dimension not duplicated','staff',costs,'/pilot-review/resources',dict(resource,reference='new-report'),409)
        post('resource corrected with measurement','staff',costs,'/pilot-review/resources/'+resource_id+'/corrections',dict(period='2026-10',quantity=12345,basis='measured',reference='fixture-storage-measured',reason='measurement completed'))
        check('resource original unknown retained',len(rows('SELECT * FROM stage7_economic_revisions WHERE entry_id=?',(resource_id,)))==2)
        before_resource=rows('SELECT * FROM stage7_economic_revisions WHERE entry_id=?',(resource_id,))
        post('missing resource correction period rejected before mutation','staff',costs,'/pilot-review/resources/'+resource_id+'/corrections',dict(period='',quantity=99999,basis='measured',reference='invalid-period',reason='invalid-period'),400)
        check('invalid resource correction changed no revision',rows('SELECT * FROM stage7_economic_revisions WHERE entry_id=?',(resource_id,))==before_resource)
        check('inventory cost mutation denied',http('inventory',costs,{}).status_code==403)
        check('customer resource mutation denied',http('customer','/pilot-review/resources',{}).status_code==403)

        scenarios='/pilot-review/scenarios'
        scenario=dict(period='2026-10',name='架空比較',save='yes',basis='assumption',reference='fixture-scenario',tax_basis='exclusive',labor_mode='time_allocated',timing_basis='steady_state_assumption',
            free_accounts=10,paid_accounts=5,paid_monthly_price_yen=3000,receipts=20,sellthrough_bps=5000,physical_fee_per_sale_yen=1000,
            free_digital_per_account_yen=20,paid_digital_per_account_yen=100,physical_other_per_received_yen=30,physical_other_per_sold_yen=40,
            fixed_expenses_yen=1000,hourly_labor_yen=1200,fixed_salary_yen='',work_minutes_per_received=3,work_minutes_per_sold=5,other_staff_minutes=0,
            digital_payment_fee_bps=300,physical_payment_fee_bps=300,available_staff_minutes=120,opening_inventory=200,shelf_capacity=1000,
            working_capital_yen=700000,purchase_cash_per_received_yen=30000,operating_cash_outflow_yen=10000)
        scenario_fields=form('staff',scenarios,scenarios)
        compared=http('staff',scenarios,{**scenario_fields,**scenario})
        check('exact scenario renders and saves',compared.status_code==200 and '19350' in compared.get_data(as_text=True))
        same=http('staff',scenarios,{**scenario_fields,**scenario})
        check('scenario retry does not duplicate saved comparison',same.status_code==200 and len(rows('SELECT * FROM stage7_economic_scenarios'))==1)
        saved=json.loads(rows('SELECT result_json FROM stage7_economic_scenarios')[0]['result_json'])
        check('saved scenario has exact rational profit',saved['metrics']['expected_profit_yen']==dict(value=19350,numerator=19350,denominator=1))
        unknown=post('unknown available hours still compare safely','staff',scenarios,scenarios,dict(scenario,save='no',available_staff_minutes='',other_staff_minutes=''),200)
        check('unknown hours displayed without default','未確認' in unknown.get_data(as_text=True))
        post('salary double count rejected','staff',scenarios,scenarios,dict(scenario,save='no',fixed_salary_yen=440000),400)
        post('scenario cannot be labelled measured profit','staff',scenarios,scenarios,dict(scenario,save='no',basis='measured'),400)
        post('out of range sales rate rejected','staff',scenarios,scenarios,dict(scenario,save='no',sellthrough_bps=10001),400)
        post('unknown tax kept unresolved','staff',scenarios,scenarios,dict(scenario,save='no',tax_basis='unknown'),200)
        timing=post('unknown timing stays unresolved','staff',scenarios,scenarios,dict(scenario,save='no',timing_basis='unknown'),200)
        check('unknown timing does not assert known monthly profit','19350' not in timing.get_data(as_text=True).split('保存した比較案')[0])
        check('unsaved comparisons do not append scenarios',len(rows('SELECT * FROM stage7_economic_scenarios'))==1)

        cohorts='/pilot-review/cohorts'
        check('unfiltered cohort page renders',http('inventory',cohorts).status_code==200)
        check('bad cohort dates rejected',http('staff',cohorts+'?start=bad&end=2026-10-22').status_code==400)
        check('reversed cohort dates rejected',http('staff',cohorts+'?start=2026-10-22&end=2026-10-20').status_code==400)
        cohort=http('staff',cohorts+'?start=2026-10-20&end=2026-10-21')
        check('cohort report renders source items',cohort.status_code==200 and all(item in cohort.get_data(as_text=True) for item in items))
        report=action(lambda store: MeasurementService(store,clock=app.extensions['stage7_clock']).receipt_cohort(actor,start_at='2026-10-20T00:00:00+09:00',end_at='2026-10-21T00:00:00+09:00'))
        group=next(group for group in report['groups'] if group['execution_mode']=='record_only')
        check('cohort includes sold and unsold same denominator',group['item_count']==2 and group['sale_shipped_count']==1 and group['unsold_or_unresolved_count']==1)
        check('cohort actual final sell through unknown',group['final_sell_through_percent'] is None and group['measured_company_profit_yen'] is None)
        check('cohort unknown duration never default experience',group['work_duration']['unknown_count']==1 and group['work_duration']['total_seconds'] is None)
        protected_after=protected_baseline()
        protected_changes=[name for name in baseline if protected_after.get(name)!=baseline[name]]
        check('all business money and notification records unchanged',not protected_changes)
        check('real payments scheduler still disabled',not module.STRIPE_ENABLED and not module.SCHEDULER_ENABLED)
        app.config['STAGE7_ENABLED']=False
        check('flag off closes new routes',http('staff','/pilot-review').status_code==404)
        return dict(scope='Copied Flask runtime, fictional SQLite, real authenticated forms, all external networking denied',
            checks=checks,requests=requests,protected_tables_changed=protected_changes,
            passed=sum(row['pass'] for row in checks),failed=sum(not row['pass'] for row in checks),
            server_errors=sum(row['status']>=500 for row in requests),
            not_verified=['real customer pilot','actual cost measurements','PostgreSQL','production restoration','native devices','real notifications or charges'])
    except Exception as error:
        return dict(scope='isolated Stage7 HTTP',checks=checks,requests=requests,passed=sum(row['pass'] for row in checks),
            failed=max(1,sum(not row['pass'] for row in checks)),server_errors=sum(row['status']>=500 for row in requests),
            error_type=type(error).__name__,error=str(error),traceback=traceback.format_exc())


def main():
    sys.stdout.reconfigure(encoding='utf-8',errors='replace')
    original=Path.cwd()
    preview=Path(tempfile.mkdtemp(prefix='kaika-stage7-verification-')).resolve()
    runtime=sandbox.mirror_source(preview,None,None)
    sandbox.isolate_environment(preview,18787)
    connections=sandbox.install_runtime_boundary(preview,18787)
    sys.path[:]=[str(runtime)]+[path for path in sys.path if path and not sandbox.inside(Path(path).resolve(),sandbox.SOURCE)]
    os.chdir(runtime)
    diagnostics=io.StringIO()
    try:
        with redirect_stdout(diagnostics),redirect_stderr(diagnostics):
            loaded=importlib.import_module('render_app')
            users=sandbox.seed_preview(loaded.module,preview)
            from stage2_migration import prepare_stage2_fixture
            from stage3_migration import prepare_stage3_fixture
            from stage4_migration import prepare_stage4_fixture
            from stage5_migration import prepare_stage5_fixture
            from stage6_migration import prepare_stage6_fixture
            from stage7_migration import prepare_stage7_fixture
            for migrate in (prepare_stage2_fixture,prepare_stage3_fixture,prepare_stage4_fixture,
                            prepare_stage5_fixture,prepare_stage6_fixture,prepare_stage7_fixture):
                migrate(loaded.module,allowed_test_path=preview/'preview.sqlite3')
            loaded.module.app.config.update(STAGE2_INVENTORY_ENABLED=True,STAGE3_FINANCE_ENABLED=True,
                STAGE4_COMMERCE_ENABLED=True,STAGE5_PAYMENTS_ENABLED=True,STAGE6_ENABLED=True)
            report=run(loaded.module,users)
    except Exception as error:
        report=dict(scope='isolated Stage7 integration',passed=0,failed=1,server_errors=1,
            error_type=type(error).__name__,error=str(error),traceback=traceback.format_exc())
    finally:
        for conn in connections:
            conn.close()
        os.chdir(original)
    if report['failed'] or report['server_errors']:
        report['isolated_runtime_diagnostics']=diagnostics.getvalue()[-14000:]
    path=preview/'verification.json'
    path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    summary={key:report.get(key) for key in ('passed','failed','server_errors','error_type','error')}
    summary.update(failed_checks=[row['name'] for row in report.get('checks',[]) if not row['pass']],
                   requests=len(report.get('requests',[])),report_path=str(path))
    print('STAGE7_VERIFICATION='+json.dumps(summary,ensure_ascii=False))
    return 1 if report['failed'] or report['server_errors'] else 0


if __name__=='__main__':
    raise SystemExit(main())
