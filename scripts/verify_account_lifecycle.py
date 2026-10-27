"""Real copied Flask runtime, synthetic customers only, all egress disabled."""
from contextlib import redirect_stdout, redirect_stderr
import importlib
import io
import json
import logging
import os
from pathlib import Path
import sys
import tempfile
import traceback

import run_app_preview as sandbox
from verify_stage5 import Forms


def run(module, users, preview):
    from account_lifecycle import LifecycleService, migration_statements
    from item_registry import Actor, Registry
    app=module.app
    app.config.update(ACCOUNT_LIFECYCLE_ENABLED=True,PROPAGATE_EXCEPTIONS=True)
    conn=module.get_db()
    for sql in migration_statements(): conn.execute(sql)
    conn.commit();conn.close()
    checks=[]
    clients={role:app.test_client() for role in ('normal','business','admin','anonymous')}
    def check(name,condition):
        checks.append({'name':name,'passed':bool(condition)})
        if not condition: raise AssertionError(name)
    def get(role,path): return clients[role].get(path,base_url='http://127.0.0.1:18810')
    def post(role,path,data): return clients[role].post(path,data=data,base_url='http://127.0.0.1:18810')
    check('public deletion information accessible',get('anonymous','/account-deletion-info').status_code==200)
    for role in users:
        response=post(role,'/login',{'username':users[role]['username'],'password':users[role]['password']})
        check('real password login '+role,response.status_code==302)
    path='/account/deletion'
    check('actual profile links deletion',b'/account/deletion' in get('normal','/profile').data)
    check('csrf required',post('normal',path,{}).status_code==400)
    fields=Forms(get('normal',path)).fields(path)
    check('rendered protected form',bool(fields.get('registry_csrf')) and bool(fields.get('operation_token')))
    fields.update(password=users['normal']['password'],confirm='delete_account')
    check('deletion requested with real form',post('normal',path,fields).status_code==302)
    check('duplicate submission safe',post('normal',path,fields).status_code==302)
    rowconn=module.get_db()
    row=Registry(rowconn).query('SELECT * FROM account_deletion_requests')[0]
    count=Registry(rowconn).query('SELECT COUNT(*) AS n FROM account_deletion_events')[0]['n']
    rowconn.close()
    check('one request event',count==1)
    check('pending customer can still log in',get('normal','/api/push/context').json['authenticated'] is True)
    check('pending new web purchase blocked',post('normal','/billing/checkout',{}).status_code==409)
    check('other customer has no access to staff review',get('business','/admin/account-deletions/'+str(row['customer_id'])).status_code==403)
    check('other request page hides first request',row['request_id'].encode() not in get('business',path).data)
    review='/admin/account-deletions/'+str(row['customer_id'])
    action=review+'/review'
    form=Forms(get('admin',review)).fields(action)
    form.update(password=users['admin']['password'],category='retention',reference='fictional-review-only',notice='保存期間は未確定です。確認後にご案内します。')
    check('staff records review',post('admin',action,form).status_code==302)
    check('customer sees notice', '保存期間は未確定' in get('normal',path).get_data(as_text=True))
    # Copy only this disposable DB; close authentication in that copy. No images removed.
    source=module.get_db()
    fixture=preview/'account-fixture-runtime.sqlite3'
    import sqlite3
    target=sqlite3.connect(fixture)
    source.backup(target);source.close()
    fixture.with_suffix('.fixture.json').write_text(json.dumps({'synthetic_only':True,'purpose':'account-deletion-rehearsal'}),encoding='utf-8')
    api=LifecycleService(Registry(target))
    api.close_fixture(Actor(users['admin']['id']),row['customer_id'],row['request_id'],fixture_database=fixture,review_reference='copied-runtime-rehearsal')
    target.commit();target.close()
    # All subsequent HTTP requests use only this explicitly named test copy.
    def fixture_connection():
        connection=sqlite3.connect(fixture)
        connection.row_factory=sqlite3.Row
        return connection
    module.get_db=fixture_connection
    check('old web session invalidated',get('normal','/profile').status_code==302)
    check('native sees authoritative signout',get('normal','/api/push/context').json=={'available':False,'authenticated':False})
    check('other customer still authenticated',get('business','/api/push/context').json['authenticated'] is True)
    response=post('normal','/login',{'username':users['normal']['username'],'password':users['normal']['password']})
    check('old credentials cannot log in',get('normal','/api/push/context').json['authenticated'] is False)
    app.config['ACCOUNT_LIFECYCLE_ENABLED']=False
    check('flag off does not resurrect old session',get('normal','/api/push/context').json['authenticated'] is False)
    check('actual charges remain disabled',not module.STRIPE_ENABLED and not module.SCHEDULER_ENABLED)
    return {'passed':len(checks),'failed':0,'checks':checks,'scope':'Copied real Flask runtime; fictional SQLite; all outbound connections denied',
        'not_verified':['real erasure/retention policy','third-party erasure','backup reapplication','real devices','production or actual charges']}


def main():
    sys.stdout.reconfigure(encoding='utf-8',errors='replace')
    original=Path.cwd()
    preview=Path(tempfile.mkdtemp(prefix='kaika-account-verification-')).resolve()
    runtime=sandbox.mirror_source(preview,None,None)
    sandbox.isolate_environment(preview,18810)
    connections=sandbox.install_runtime_boundary(preview,18810)
    sys.path[:]=[str(runtime)]+[p for p in sys.path if p and not sandbox.inside(Path(p).resolve(),sandbox.SOURCE)]
    os.chdir(runtime)
    diagnostic=io.StringIO()
    try:
        with redirect_stdout(diagnostic),redirect_stderr(diagnostic):
            module=importlib.import_module('render_app').module
            users=sandbox.seed_preview(module,preview)
            report=run(module,users,preview)
    except Exception as error:
        report={'passed':0,'failed':1,'error_type':type(error).__name__,'error':str(error),'traceback':traceback.format_exc()}
    finally:
        for conn in connections: conn.close()
        os.chdir(original)
    path=preview/'verification.json'
    path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k in ('passed','failed','error_type','error','traceback')},ensure_ascii=False))
    print('report='+str(path))
    return int(bool(report['failed']))

if __name__=='__main__': raise SystemExit(main())
