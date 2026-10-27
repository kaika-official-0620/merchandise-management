"""Actual copied Flask + synthetic erasure plan, no real DB or outbound traffic."""
import json
import hashlib
import sqlite3
import verify_account_lifecycle as harness
from verify_stage5 import Forms


def run(module, users, preview):
    from account_lifecycle import LifecycleService, migration_statements as lifecycle_schema
    from account_erasure import ErasureService, migration_statements
    from stage6_notifications_schema import migration_statements as notification_schema
    from item_registry import Actor, Registry
    app=module.app
    app.config.update(ACCOUNT_LIFECYCLE_ENABLED=True,PROPAGATE_EXCEPTIONS=True)
    db=module.get_db()
    for sql in lifecycle_schema()+notification_schema()+migration_statements():db.execute(sql)
    address='completion-fixture@example.invalid'
    db.execute('INSERT INTO stage6_destinations VALUES (?,?,?,?,?,?,?,?,?,?)',
        ('completion-fixture',users['normal']['id'],'email',address,hashlib.sha256(address.encode()).hexdigest(),None,None,None,1,None))
    db.commit();db.close()
    clients={role:app.test_client() for role in ('normal','business','admin','anonymous')}
    checks=[]
    def check(name,ok):
        checks.append({'name':name,'passed':bool(ok)})
        if not ok:raise AssertionError(name)
    def http(role,path,data=None):
        return clients[role].open(path,method='GET' if data is None else 'POST',data=data,base_url='http://127.0.0.1:18810')
    def fields(role,path,action):
        page=http(role,path)
        check('form page '+action,page.status_code==200)
        result=Forms(page).fields(action)
        check('CSRF '+action,bool(result.get('registry_csrf')))
        return result
    for role in ('normal','business','admin'):
        check('login '+role,http(role,'/login',{'username':users[role]['username'],'password':users[role]['password']}).status_code==302)
    request_path='/account/deletion'
    form=fields('normal',request_path,request_path)
    form.update(password=users['normal']['password'],confirm='delete_account')
    check('accept request',http('normal',request_path,form).status_code==302)
    db=module.get_db();store=Registry(db)
    customer=users['normal']['id'];staff=Actor(users['admin']['id'])
    row=LifecycleService(store).get(staff,customer);db.close()
    notice_path=request_path+'/completion-notice'
    check('completion consent requires CSRF',http('normal',notice_path,{}).status_code==400)
    consent=fields('normal',request_path,notice_path)
    consent.update(password=users['normal']['password'],request_id=row['request_id'],completion_email='yes')
    check('owner opts in to one completion email',http('normal',notice_path,consent).status_code==302)
    revoke=fields('normal',request_path,notice_path)
    revoke.update(password=users['normal']['password'],request_id=row['request_id'],completion_email='no')
    check('owner can revoke completion consent',http('normal',notice_path,revoke).status_code==302)
    check('late opt-in form replay returns without change',http('normal',notice_path,consent).status_code==302)
    check('late replay does not revive consent','メール連絡への同意なし' in http('normal',request_path).get_data(as_text=True))
    consent=fields('normal',request_path,notice_path)
    consent.update(password=users['normal']['password'],request_id=row['request_id'],completion_email='yes')
    check('fresh explicit consent is accepted',http('normal',notice_path,consent).status_code==302)
    other=fields('business',request_path,request_path)
    other.update(password=users['business']['password'],confirm='delete_account')
    check('unverified email does not prevent deletion request',http('business',request_path,other).status_code==302)
    other=fields('business',request_path,notice_path)
    other.update(password=users['business']['password'],completion_email='yes')
    check('unverified completion email refused',http('business',notice_path,other).status_code==409)
    check('request remains accepted after notice error','削除申請を受け付けました' in http('business',request_path).get_data(as_text=True))
    review='/admin/account-deletions/'+str(customer);prepare=review+'/plan';decide=prepare+'/decision'
    check('other customer cannot read plan',http('business',review).status_code==403)
    check('other customer cannot create plan',http('business',prepare,{}).status_code==403)
    check('staff create requires CSRF',http('admin',prepare,{}).status_code==400)
    form=fields('admin',review,prepare);form.update(password='incorrect-fictional-password',request_id=row['request_id'])
    check('staff reauthentication required',http('admin',prepare,form).status_code==403)
    form=fields('admin',review,prepare);form.update(password=users['admin']['password'],request_id=row['request_id'])
    check('create counts-only draft',http('admin',prepare,form).status_code==302)
    check('replay creates no extra plan',http('admin',prepare,form).status_code==302)
    db=module.get_db();store=Registry(db)
    check('exactly one plan',store.query('SELECT COUNT(*) AS n FROM account_erasure_plans')[0]['n']==1)
    check('plan content has no profile email', '@example.invalid' not in store.query('SELECT inventory_json FROM account_erasure_plans')[0]['inventory_json'])
    db.close()
    check('draft visible in real template','下書きです' in http('admin',review).get_data(as_text=True))
    stale=None
    for task in ('profile','notifications'):
        form=fields('admin',review,decide)
        form.update(password=users['admin']['password'],request_id=row['request_id'],task_key=task,action='erase',
                    reason='架空データの削除確認',reference='fictional-review-only',retain_until='')
        check('record decision '+task,http('admin',decide,form).status_code==302)
        if stale is None:stale=dict(form)
    check('stale form cannot overwrite newer review',http('admin',decide,stale).status_code==409)
    form=fields('admin',review,decide)
    form.update(password=users['admin']['password'],request_id=row['request_id'],task_key='money',action='erase',reason='fixture',reference='fixture',retain_until='')
    check('money cannot be selected for bulk erase',http('admin',decide,form).status_code==409)
    form=fields('admin',review,decide)
    form.update(password=users['admin']['password'],request_id=row['request_id'],task_key='records',action='retain',reason='fixture',reference='fixture',retain_until='')
    check('retention cannot silently default to forever',http('admin',decide,form).status_code==400)
    check('customer page hides internal plan evidence','fictional-review-only' not in http('normal',request_path).get_data(as_text=True))
    check('HTTP has no erase execution route',http('admin',review+'/execute',{}).status_code==404)
    source=module.get_db();fixture=preview/'account-fixture-erasure.sqlite3'
    target=sqlite3.connect(fixture);source.backup(target);source.close()
    fixture.with_suffix('.fixture.json').write_text(json.dumps({'synthetic_only':True,'purpose':'account-deletion-rehearsal'}),encoding='utf-8')
    api=ErasureService(Registry(target));plan=api.get(staff,customer,row['request_id'])
    result=api.rehearse_local(staff,customer,row['request_id'],fixture_database=fixture,revision=plan['revision'])
    target.commit()
    check('fixture local erasure remains incomplete',result['complete'] is False and result['account_state']=='erasure_pending')
    check('completion address only held after ordinary revocation',result['completion_contact_held'] is True and
          Registry(target).query("SELECT revoked_at FROM stage6_destinations WHERE destination_id='completion-fixture'")[0]['revoked_at'] is not None)
    check('fixture action replay is stable',result==api.rehearse_local(staff,customer,row['request_id'],fixture_database=fixture,revision=plan['revision']))
    target.close()
    def fixture_connection():
        conn=sqlite3.connect(fixture);conn.row_factory=sqlite3.Row;return conn
    module.get_db=fixture_connection
    check('closed customer session rejected',http('normal','/api/push/context').json['authenticated'] is False)
    check('other customer still logged in',http('business','/api/push/context').json['authenticated'] is True)
    check('staff sees partial rehearsal, not completed','架空データの一部消去を検証済み' in http('admin',review).get_data(as_text=True))
    check('real payment and scheduler disabled',not module.STRIPE_ENABLED and not module.SCHEDULER_ENABLED)
    return {'passed':len(checks),'failed':0,'checks':checks,'scope':'Copied real Flask, synthetic DB and no external sockets',
        'not_verified':['real erasure','retention policy','real completion mail','native devices','production migration']}


if __name__=='__main__':
    harness.run=run
    raise SystemExit(harness.main())
