"""Real Flask HTTP forms in an isolated copied runtime with offline transports.

Never loads source .env/data/uploads, never sends external notifications, never
tests or modifies real provider accounts. Every user and item is fictional.
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
from urllib.parse import parse_qs, urlsplit

import run_app_preview as sandbox
from verify_stage5 import Forms


def run(module, users):
    from item_registry import Actor, Registry
    from stage6_common import Stage6Store
    from stage3_billing import BillingService
    from stage3_funds import FundsService
    from stage6_transports import MemoryNotificationTransport, DeliveryResult
    app = module.app
    app.config['PROPAGATE_EXCEPTIONS'] = True
    app.logger.setLevel(logging.ERROR)
    clients = {role: app.test_client() for role in ('customer','other','staff','anonymous')}
    checks, requests = [], []
    now = ['2026-10-22T01:50:00+00:00']
    app.extensions['stage6_clock'] = lambda: now[0]
    actor = Actor(users['admin']['id'],True)
    owner = users['normal']['id']
    other_id = users['business']['id']

    def check(name, condition):
        checks.append({'name':name,'pass':bool(condition)})

    def http(role,path,data=None):
        response=clients[role].open(path,method='GET' if data is None else 'POST',data=data,
            base_url='http://127.0.0.1:18786',headers={'User-Agent':'Mozilla/5.0 Stage6Fixture'})
        response.get_data()
        requests.append({'role':role,'method':'GET' if data is None else 'POST','path':urlsplit(path).path,'status':response.status_code})
        return response

    def form(role,page,action):
        response=http(role,page)
        check('form page '+role+' '+action,response.status_code==200)
        fields=Forms(response).fields(action)
        check('protected form '+role+' '+action,bool(fields.get('operation_token')) and bool(fields.get('registry_csrf')))
        return fields

    def post(name,role,page,action,values=None,expected=302):
        response=http(role,action,{**form(role,page,action),**(values or {})})
        check(name,response.status_code==expected)
        if response.status_code!=expected:
            raise AssertionError(name+': status '+str(response.status_code))
        return response

    def rows(sql,params=()):
        conn=module.get_db()
        try:
            return [dict(row) for row in conn.execute(sql,params)]
        finally:
            conn.close()

    def db_action(callback):
        conn=module.get_db()
        try:
            conn.execute('PRAGMA foreign_keys=ON')
            conn.execute('BEGIN IMMEDIATE')
            result=callback(Stage6Store(Registry(conn, clock=lambda: now[0])))
            conn.commit()
            return result
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def protected_baseline():
        result={}
        for table in ('user_invoices','invoice_payments','payment_transactions','monthly_settlements',
                      'business_charge_drafts','business_fund_records','stage3_fund_records','feature_plan_subscriptions'):
            if rows("SELECT name FROM sqlite_master WHERE type='table' AND name=?",(table,)):
                data=sorted(json.dumps(row,sort_keys=True,default=str) for row in rows('SELECT * FROM '+table))
                result[table]=hashlib.sha256(json.dumps(data).encode()).hexdigest()
        return result

    def notification_baseline():
        names=[row['name'] for row in rows("SELECT name FROM sqlite_master WHERE type='table' AND (name LIKE 'stage6_%' OR name='stage4_operations')")]
        return {table:hashlib.sha256(json.dumps(sorted(json.dumps(row,sort_keys=True,default=str)
            for row in rows('SELECT * FROM '+table))).encode()).hexdigest() for table in names}

    def deadline(item,value,key):
        return db_action(lambda store:store.registry.set_deadline(actor,item,deadline_at=value,evidence_reference='fixture-customer-contract',source_key=key))

    def refresh():
        return post('notification refresh records no external delivery','customer','/notifications','/notifications/refresh')

    try:
        for role,account in (('customer','normal'),('other','business'),('staff','admin')):
            response=http(role,'/login',{'username':users[account]['username'],'password':users[account]['password']})
            check('real login '+role,response.status_code==302 and '/login' not in (response.location or ''))
        check('Stage6 default off',app.config.get('STAGE6_ENABLED') is not True)
        runner=app.test_cli_runner()
        args=['stage6-notifications','--staff-user-id',str(actor.user_id)]
        disabled=runner.invoke(args=args)
        check('CLI refuses disabled stage',disabled.exit_code!=0 and 'disabled' in disabled.output)
        check('disabled home unavailable',http('customer','/workspace-home').status_code==404)
        check('disabled notifications unavailable',http('customer','/notifications').status_code==404)
        app.config['STAGE6_ENABLED']=True
        check('anonymous authentication required',http('anonymous','/workspace-home').status_code in (302,401))
        check('old home routes to new home only when enabled',urlsplit(http('customer','/home').location or '').path=='/workspace-home')

        def seed(store):
            r=store.registry
            item=r.create_item(actor,'【架空】段階6 商品 <private>',owner_user_id=owner,custodian='self',occurred_at='2026-10-20T00:00:00+09:00',evidence_reference='fixture-only',source_key='stage6-item')['physical_item_id']
            for minute,kind in enumerate(('received','photographed','registration_completed'),1):
                r.record_event(actor,item,kind,f'2026-10-20T01:0{minute}:00+09:00','stage6-'+kind,'fixture-only')
            r.set_deadline(actor,item,deadline_at='2026-10-29T01:50:00+00:00',evidence_reference='fixture-customer-term',source_key='stage6-term')
            r.create_item(actor,'【架空】他のお客様専用',owner_user_id=other_id,custodian='self',occurred_at='2026-10-20T00:00:00+09:00',evidence_reference='fixture-other',source_key='stage6-other')
            for mode,amount in [('test',5000),('record_only',7000)]:
                FundsService(store.finance).post(actor,owner,kind='deposit',amount_yen=amount,mode=mode,
                    occurred_at='2026-10-20T00:00:00+09:00',source_key='stage6-fund-'+mode,evidence_reference='fixture-only')
            BillingService(store.finance).create_month(actor,owner,'2026-10',source_key='stage6-month')
            return item
        item=db_action(seed)
        baseline=protected_baseline()
        prior_notifications=notification_baseline()
        dry=runner.invoke(args=args)
        check('CLI defaults read-only',dry.exit_code==0 and notification_baseline()==prior_notifications and 'created: 0' in dry.output)
        unauthorized=runner.invoke(args=['stage6-notifications','--staff-user-id',str(owner)])
        check('CLI cannot impersonate staff',unauthorized.exit_code!=0 and notification_baseline()==prior_notifications)
        created=runner.invoke(args=args+['--record'])
        count=len(rows('SELECT * FROM stage6_notifications'))
        replay=runner.invoke(args=args+['--record'])
        check('CLI explicit record is idempotent',created.exit_code==0 and replay.exit_code==0 and count>0 and len(rows('SELECT * FROM stage6_notifications'))==count and 'created: 0' in replay.output)
        check('CLI does not dispatch',not rows('SELECT * FROM stage6_notification_outbox'))
        home=http('customer','/workspace-home')
        body=home.get_data(as_text=True)
        check('customer home renders',home.status_code==200)
        for label in ('売上の入力記録','確定条件を満たす商品利益','見込の商品利益','購入に使える実残高','売上の精算待ち','開花への未払い料金'):
            check('separate home card '+label,label in body)
        check('unknown amounts explicitly visible','未確認' in body)
        check('test records labelled','検証用の資金・売上' in body)
        check('other customer item excluded','他のお客様専用' not in body)
        check('customer item escaped','&lt;private&gt;' in body and '<private>' not in body)
        check('home private cache',home.headers.get('Cache-Control')=='private, no-store')
        check('staff home renders',http('staff','/workspace-home').status_code==200)
        check('customer cannot choose another customer item',http('other','/notifications/items/'+item+'/choice').status_code==404)
        check('refresh missing CSRF denied',http('customer','/notifications/refresh',{}).status_code==400)
        check('customer cannot recover staff deliveries',http('customer','/notifications/recover',{}).status_code==403)
        check('recover missing CSRF denied',http('staff','/notifications/recover',{}).status_code==400)
        post('staff can inspect stale deliveries without external sends','staff','/notifications','/notifications/recover')
        refresh()
        notes=rows('SELECT * FROM stage6_notifications WHERE customer_id=?',(owner,))
        check('pre-due and invoice notifications created',{row['event_kind'] for row in notes}=={'storage_pre_due','invoice_draft'})
        before=len(notes); refresh()
        check('refresh idempotent',len(rows('SELECT * FROM stage6_notifications WHERE customer_id=?',(owner,)))==before)
        note=next(row for row in notes if row['event_kind']=='storage_pre_due')
        path='/notifications/'+note['notification_id']
        check('notification visible only to owner',http('customer',path).status_code==200 and http('other',path).status_code==403)
        post('mark own notification read','customer',path,path+'/read')
        check('read recorded',rows('SELECT read_at FROM stage6_notifications WHERE notification_id=?',(note['notification_id'],))[0]['read_at'] is not None)
        check('staff cannot mark customer read',http('staff',path+'/read',{}).status_code==403)

        settings='/notifications/settings'
        preference=settings+'/storage_due'
        post('customer sets email preference','customer',settings,preference,{'channels':['email']})
        check('preferences scope own customer',all(row['customer_id']==owner for row in rows('SELECT * FROM stage6_notification_preferences')))
        post('invalid preference channel blocked','customer',settings,preference,{'channels':['sms']},expected=400)
        check('no email destination before proof',not rows("SELECT * FROM stage6_destinations WHERE channel='email'"))
        post('email request enqueued only','customer',settings,'/notifications/email',{'email':'fixture-owner@example.invalid'})
        outbox=rows('SELECT * FROM stage6_notification_outbox WHERE proof_id IS NOT NULL')[0]
        private=json.loads(outbox['private_payload_json'])
        verification=urlsplit(private['verification_url'])
        token=(parse_qs(verification.fragment).get('token') or parse_qs(verification.query).get('token'))[0]
        check('verification bearer kept out of query',not verification.query and bool(verification.fragment))
        check('settings never expose verification token',token not in http('customer',settings).get_data(as_text=True))
        confirm='/notifications/email/confirm'
        check('confirm GET only shows form',http('customer',confirm).status_code==200)
        check('GET did not consume proof',rows('SELECT consumed_at FROM stage6_destination_proofs')[0]['consumed_at'] is None)
        post('other account cannot use proof','other',confirm,confirm,{'token':token},expected=409)
        check('bad account did not consume proof',rows('SELECT consumed_at FROM stage6_destination_proofs')[0]['consumed_at'] is None)
        post('own account confirms proof','customer',confirm,confirm,{'token':token})
        check('verified email belongs to correct customer',rows("SELECT customer_id FROM stage6_destinations WHERE channel='email'")[0]['customer_id']==owner)
        check('email bearer absent from operation audit',all(token not in row['result_json'] for row in rows('SELECT result_json FROM stage4_operations')))

        now[0]='2026-10-29T01:50:00+00:00'
        refresh()
        check('pre-due old notification now stale','対象の状態' in http('customer',path).get_data(as_text=True))
        due=rows("SELECT * FROM stage6_notifications WHERE event_kind='storage_due'")[0]
        choice_note=rows("SELECT * FROM stage6_notifications WHERE event_kind='return_selection'")[0]
        check('due and return choices have separate identities',due['notification_id']!=choice_note['notification_id'])
        delivery=rows('SELECT * FROM stage6_notification_outbox WHERE notification_id=?',(due['notification_id'],))[0]
        check('requested channel only is queued',delivery['channel']=='email' and delivery['test_only']==1)
        app.config['STAGE6_TEST_MONTHLY_LIMITS']={'email':20,'push':20,'line':20}
        transport=MemoryNotificationTransport()
        transport.timeout_after_send=True
        transport.next_result=DeliveryResult('provider_accepted','fixture-result')
        app.extensions['stage6_test_transports']={'email':transport}
        simulate='/notifications/deliveries/'+delivery['delivery_id']+'/simulate'
        reconcile='/notifications/deliveries/'+delivery['delivery_id']+'/reconcile'
        check('customer cannot dispatch',http('customer',simulate,{}).status_code==403)
        post('staff simulates committed claim then timeout','staff','/notifications',simulate)
        check('timeout retained as unknown',rows('SELECT state FROM stage6_notification_outbox WHERE delivery_id=?',(delivery['delivery_id'],))[0]['state']=='unknown')
        check('transport called once',len(transport.sent)==1)
        check('delivery claim bearer absent from operation audit',all('claim_token' not in row['result_json'] for row in rows('SELECT result_json FROM stage4_operations')))
        post('unknown reconciles without sending again','staff','/notifications',reconcile)
        check('provider result reconciled',rows('SELECT state FROM stage6_notification_outbox WHERE delivery_id=?',(delivery['delivery_id'],))[0]['state']=='provider_accepted')
        check('reconciliation never sends twice',len(transport.sent)==1)
        check('provider content no private title or amount','private' not in transport.sent[0]['body'] and '円' not in transport.sent[0]['body'])

        choice='/notifications/items/'+item+'/choice'
        post('customer requests return with optional note','customer',choice,choice,{'choice':'return','note':''})
        request=rows('SELECT * FROM stage6_custody_requests')[0]
        post('duplicate outstanding request blocked','customer',choice,choice,{'choice':'alternate_channel','note':''},expected=409)
        check('original due notification stale after choice','対象の状態' in http('customer','/notifications/'+due['notification_id']).get_data(as_text=True))
        review='/notifications/choices/'+request['request_id']+'/review'
        check('customer cannot review staff response',http('customer',review,{}).status_code==403)
        post('staff acknowledges choice','staff',choice,review,{'status':'acknowledged','note':'確認してご連絡します'})
        post('staff records resolution','staff',choice,review,{'status':'resolved','note':'返送条件の相談結果を記録'})
        check('choice history retained',len(rows('SELECT * FROM stage6_custody_responses'))==2)
        check('choice never ships item',rows('SELECT physical_state FROM business_item_profiles WHERE physical_item_id=?',(item,))[0]['physical_state']=='received')
        deadline(item,'2026-10-29T01:50:00+00:00','stage6-new-confirmed-term')
        refresh()
        check('deadline revision creates new notification',len(rows("SELECT notification_id FROM stage6_notifications WHERE event_kind='storage_due'"))==2)
        latest=rows("SELECT * FROM stage6_notifications WHERE event_kind='storage_due' ORDER BY created_at DESC,notification_id DESC")
        current_note=next(row for row in latest if row['notification_id']!=due['notification_id'])
        revoke=rows("SELECT * FROM stage6_destinations WHERE channel='email'")[0]
        post('email destination can be revoked','customer',settings,'/notifications/destinations/'+revoke['destination_id']+'/revoke')
        queued=rows('SELECT * FROM stage6_notification_outbox WHERE notification_id=?',(current_note['notification_id'],))[0]
        post('revocation suppresses queued delivery','staff','/notifications','/notifications/deliveries/'+queued['delivery_id']+'/simulate')
        check('revoked destination not sent',rows('SELECT state FROM stage6_notification_outbox WHERE delivery_id=?',(queued['delivery_id'],))[0]['state']=='suppressed' and len(transport.sent)==1)
        now[0]='2026-10-29T02:00:00+00:00'
        db_action(lambda store:store.registry.change_assignment(actor,item,'owner','customer',other_id,
            occurred_at='2026-10-29T02:00:00+00:00',evidence_reference='fixture-transfer',source_key='stage6-transfer'))
        check('former owner choice no longer accessible',http('customer',choice).status_code==404)
        new_page=http('other',choice)
        check('new owner cannot see old choice history',new_page.status_code==200 and request['request_id'] not in new_page.get_data(as_text=True) and '確認してご連絡します' not in new_page.get_data(as_text=True))
        check('ownership change invalidates deep link','対象の状態' in http('customer','/notifications/'+current_note['notification_id']).get_data(as_text=True))
        check('all legacy money and entitlements untouched',protected_baseline()==baseline)
        check('all notifications test only',all(row['test_only']==1 for row in rows('SELECT test_only FROM stage6_notifications')))
        check('scheduler and real Stripe remain disabled',not module.STRIPE_ENABLED and not module.SCHEDULER_ENABLED)
        app.config['STAGE6_ENABLED']=False
        check('flag off closes all new routes',http('customer','/workspace-home').status_code==404 and http('customer','/notifications').status_code==404)
        check('flag off restores old home entry',urlsplit(http('customer','/home').location or '').path!='/workspace-home')
        return dict(scope='Copied real Flask runtime, authenticated forms, offline transports and fictional SQLite only',checks=checks,requests=requests,
            passed=sum(row['pass'] for row in checks),failed=sum(not row['pass'] for row in checks),server_errors=sum(row['status']>=500 for row in requests),
            not_verified=['real Expo/APNs/FCM delivery','real SMTP/email delivery','real LINE account link or delivery','native devices','PostgreSQL concurrent workers','production data and charges'])
    except Exception as error:
        return dict(scope='isolated Stage6 HTTP',checks=checks,requests=requests,passed=sum(row['pass'] for row in checks),
            failed=max(1,sum(not row['pass'] for row in checks)),server_errors=sum(row['status']>=500 for row in requests),
            error_type=type(error).__name__,error=str(error),traceback=traceback.format_exc())


def main():
    sys.stdout.reconfigure(encoding='utf-8',errors='replace')
    original=Path.cwd()
    preview=Path(tempfile.mkdtemp(prefix='kaika-stage6-verification-')).resolve()
    runtime=sandbox.mirror_source(preview,None,None)
    sandbox.isolate_environment(preview,18786)
    connections=sandbox.install_runtime_boundary(preview,18786)
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
            for migrate in (prepare_stage2_fixture,prepare_stage3_fixture,prepare_stage4_fixture,prepare_stage5_fixture,prepare_stage6_fixture):
                migrate(loaded.module,allowed_test_path=preview/'preview.sqlite3')
            loaded.module.app.config.update(STAGE2_INVENTORY_ENABLED=True,STAGE3_FINANCE_ENABLED=True,STAGE4_COMMERCE_ENABLED=True,STAGE5_PAYMENTS_ENABLED=True)
            report=run(loaded.module,users)
    except Exception as error:
        report=dict(scope='isolated Stage6 integration',passed=0,failed=1,server_errors=1,error_type=type(error).__name__,error=str(error),traceback=traceback.format_exc())
    finally:
        for conn in connections:
            conn.close()
        os.chdir(original)
    if report['failed'] or report['server_errors']:
        report['isolated_runtime_diagnostics']=diagnostics.getvalue()[-14000:]
    path=preview/'verification.json'
    path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    summary={key:report.get(key) for key in ('passed','failed','server_errors','error_type','error')}
    summary.update(failed_checks=[row['name'] for row in report.get('checks',[]) if not row['pass']],requests=len(report.get('requests',[])),report_path=str(path))
    print('STAGE6_VERIFICATION='+json.dumps(summary,ensure_ascii=False))
    return 1 if report['failed'] or report['server_errors'] else 0


if __name__=='__main__':
    raise SystemExit(main())
