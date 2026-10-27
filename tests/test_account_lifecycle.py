import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from types import SimpleNamespace

from flask import Flask, jsonify
from flask_login import LoginManager, UserMixin, current_user, login_required
from werkzeug.security import generate_password_hash, check_password_hash

from account_lifecycle import LifecycleService, migration_statements, outbound_allowed_in_store, access_allowed_in_store
from account_lifecycle_web import register_account_lifecycle
from item_registry import Registry, Actor, RegistryError

ROOT = Path(__file__).resolve().parents[1]


class LifecycleFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='kaika-account-test-')
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)/'account-fixture-test.sqlite3'
        self.path.with_suffix('.fixture.json').write_text(json.dumps({'synthetic_only':True,'purpose':'account-deletion-rehearsal'}),encoding='utf-8')
        self.db = sqlite3.connect(self.path)
        self.addCleanup(self.db.close)
        self.db.execute('''CREATE TABLE users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password_hash TEXT,
            email TEXT,role TEXT,admin_permissions TEXT,display_name TEXT,address TEXT,proxy_service_budget INTEGER)''')
        self.password = 'Fictional-Account-Password-Only'
        for uid,role,perms in ((1,'user',None),(2,'user',None),(3,'admin','["users"]'),(4,'admin','["merchandise"]')):
            self.db.execute('INSERT INTO users VALUES (?,?,?,?,?,?,?,?,?)',(uid,'fixture'+str(uid),generate_password_hash(self.password),
                'fixture'+str(uid)+'@example.invalid',role,perms,'Fixture '+str(uid),'fictional address',30000))
        for sql in migration_statements():
            self.db.execute(sql)
        self.db.execute('CREATE TABLE mobile_sessions(token_hash TEXT,user_id INTEGER,revoked_at TEXT)')
        self.db.execute("INSERT INTO mobile_sessions VALUES ('fictional_token_hash',1,NULL)")
        self.db.execute('CREATE TABLE push_devices(installation TEXT,user_id INTEGER,enabled INTEGER)')
        self.db.execute("INSERT INTO push_devices VALUES ('fixture_installation',1,1)")
        self.db.execute('CREATE TABLE invoices(id INTEGER,customer_id INTEGER,amount INTEGER)')
        self.db.execute('INSERT INTO invoices VALUES(1,1,1155)')
        self.db.commit()
        self.store=Registry(self.db)
        self.api=LifecycleService(self.store)
        self.customer,self.other,self.staff=Actor(1),Actor(2),Actor(3)

    def error(self,code,fn):
        with self.assertRaises(RegistryError) as cm:
            fn()
        self.assertEqual(code,cm.exception.code)

    def close(self,request_id):
        return self.api.close_fixture(self.staff,1,request_id,fixture_database=self.path,review_reference='fixture-review')

class LifecycleTests(LifecycleFixture):
    def test_request_accepts_outstanding_money_and_is_idempotent(self):
        row=self.api.request(self.customer)
        self.assertEqual(row['request_id'],self.api.request(self.customer)['request_id'])
        self.assertEqual(1,self.db.execute('SELECT COUNT(*) FROM account_deletion_events').fetchone()[0])
        self.assertEqual(30000,self.db.execute('SELECT proxy_service_budget FROM users WHERE id=1').fetchone()[0])
        self.assertFalse(outbound_allowed_in_store(self.store,1))
        self.assertTrue(access_allowed_in_store(self.store,1))

    def test_customer_cannot_read_other_request_or_inventory(self):
        self.api.request(self.customer)
        self.error('account_access_denied',lambda:self.api.get(self.other,1))
        self.error('account_access_denied',lambda:self.api.inventory(self.customer,1))
        self.error('account_access_denied',lambda:self.api.inventory(Actor(4),1))

    def test_staff_role_flag_is_not_authorization(self):
        self.error('account_access_denied',lambda:self.api.inventory(Actor(1,True),1))

    def test_review_requires_correct_request_and_permission(self):
        row=self.api.request(self.customer)
        self.error('account_access_denied',lambda:self.api.review(self.other,1,row['request_id'],'funds','fixture'))
        self.error('deletion_state_conflict',lambda:self.api.review(self.staff,1,'stale','funds','fixture'))
        self.api.review(self.staff,1,row['request_id'],'funds','fixture-evidence','確認予定です')
        self.assertEqual('reviewing',self.api.get(self.customer,1)['state'])

    def test_withdraw_and_new_request_do_not_reuse_stale_identifier(self):
        row=self.api.request(self.customer)
        self.api.withdraw(self.customer,row['request_id'])
        self.assertTrue(outbound_allowed_in_store(self.store,1))
        new=self.api.request(self.customer)
        self.assertNotEqual(row['request_id'],new['request_id'])
        self.error('deletion_state_conflict',lambda:self.api.withdraw(self.customer,row['request_id']))

    def test_late_request_replay_cannot_reverse_withdrawal(self):
        row=self.api.request(self.customer,source_key='fixture-submission-1')
        self.api.withdraw(self.customer,row['request_id'])
        late=self.api.request(self.customer,source_key='fixture-submission-1')
        self.assertEqual('withdrawn',late['state'])
        self.assertTrue(outbound_allowed_in_store(self.store,1))
        self.api.request(self.customer,source_key='fixture-submission-2')
        self.error('deletion_state_conflict',lambda:self.api.request(self.customer,source_key='fixture-submission-1'))

    def test_fixture_closure_revokes_auth_preserves_money_not_erasure_complete(self):
        row=self.api.request(self.customer)
        result=self.close(row['request_id'])
        self.assertEqual('erasure_pending',result['state'])
        self.assertEqual('fixture_only',result['execution_scope'])
        self.assertFalse(access_allowed_in_store(self.store,1))
        self.assertFalse(outbound_allowed_in_store(self.store,1))
        self.assertTrue(access_allowed_in_store(self.store,2))
        self.assertIsNotNone(self.db.execute('SELECT revoked_at FROM mobile_sessions').fetchone()[0])
        self.assertEqual(0,self.db.execute('SELECT enabled FROM push_devices').fetchone()[0])
        user=self.store.query('SELECT * FROM users WHERE id=1')[0]
        self.assertFalse(check_password_hash(user['password_hash'],self.password))
        self.assertEqual('',user['email'])
        self.assertEqual(30000,user['proxy_service_budget'])
        self.assertEqual(1155,self.db.execute('SELECT amount FROM invoices').fetchone()[0])
        self.assertEqual(result,self.close(row['request_id']))
        self.error('deletion_state_conflict',lambda:self.api.withdraw(self.customer,row['request_id']))

    def test_no_closure_without_explicit_fixture_marker(self):
        row=self.api.request(self.customer)
        self.path.with_suffix('.fixture.json').unlink()
        self.error('fixture_only',lambda:self.close(row['request_id']))
        self.assertTrue(access_allowed_in_store(self.store,1))

    def test_cannot_point_executor_at_other_file(self):
        row=self.api.request(self.customer)
        self.error('fixture_only',lambda:self.api.close_fixture(self.staff,1,row['request_id'],fixture_database=self.path.parent/'else.sqlite3',review_reference='fixture'))

    def test_discovery_distinguishes_unknown_from_zero(self):
        self.db.execute('CREATE TABLE indirect_payloads(id INTEGER,payload TEXT)')
        inventory={r['table']:r for r in self.api.inventory(self.staff,1)}
        self.assertEqual(1,inventory['invoices']['direct_records'])
        self.assertIsNone(inventory['indirect_payloads']['direct_records'])

    def test_missing_migration_preserves_legacy_callback_but_blocks_requests(self):
        db=sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        store=Registry(db)
        self.assertTrue(outbound_allowed_in_store(store,1))
        self.error('account_lifecycle_migration_required',lambda:LifecycleService(store).request(self.customer))


class LifecycleHttpTests(LifecycleFixture):
    def setUp(self):
        super().setUp()
        from jinja2 import ChoiceLoader, DictLoader
        self.app=Flask(__name__,template_folder=str(ROOT/'templates'))
        self.app.secret_key='fixture-only-secret-account-lifecycle-2026'
        self.app.config.update(TESTING=True,ACCOUNT_LIFECYCLE_ENABLED=True)
        self.app.jinja_loader=ChoiceLoader([DictLoader({'base.html':'<!doctype html><html><head><title>{% block title %}{% endblock %}</title></head><body>{% block content %}{% endblock %}</body></html>'}),self.app.jinja_loader])
        login=LoginManager(self.app)
        login.login_view='login'
        @login.user_loader
        def load(uid):
            class User(UserMixin):
                pass
            user=User();user.id=int(uid)
            return user
        @self.app.get('/login')
        def login_page(): return 'login'
        self.app.view_functions['login']=login_page
        self.app.add_url_rule('/fixture-login',endpoint='login',view_func=login_page)
        @self.app.get('/private')
        @login_required
        def private(): return 'private'
        @self.app.get('/api/push/context')
        def context(): return jsonify(authenticated=current_user.is_authenticated,available=False)
        @self.app.post('/billing/checkout')
        def checkout(): return 'should not run'
        self.runtime=SimpleNamespace(app=self.app,DATABASE_URL=None,get_db=lambda:sqlite3.connect(self.path))
        self.web=register_account_lifecycle(self.runtime)
        self.client=self.app.test_client()

    def authenticate(self,uid=1):
        with self.client.session_transaction() as session:
            session['_user_id']=str(uid);session['_fresh']=True

    def form(self,path='/account/deletion',purpose='account_deletion_request',target=''):
        self.client.get(path)
        with self.client.session_transaction() as session:
            csrf=session['_registry_csrf']
            uid=session['_user_id']
        return {'registry_csrf':csrf,'operation_token':self.web.signer.dumps({'actor':uid,'purpose':purpose,'target':str(target),'nonce':'fixture'}),
            'password':self.password,'confirm':'delete_account'}

    def test_public_info_anonymous_and_auth_required_request(self):
        self.assertEqual(200,self.client.get('/account-deletion-info').status_code)
        self.assertEqual(302,self.client.get('/account/deletion').status_code)

    def test_private_pages_are_not_cached(self):
        self.authenticate()
        response=self.client.get('/account/deletion')
        self.assertEqual('private, no-store',response.headers['Cache-Control'])
        self.assertEqual('no-referrer',response.headers['Referrer-Policy'])

    def test_get_does_not_request_deletion(self):
        self.authenticate();self.assertEqual(200,self.client.get('/account/deletion').status_code)
        self.assertIsNone(self.api.get(self.customer,1))

    def test_post_needs_csrf_password_and_confirmation(self):
        self.authenticate()
        self.assertEqual(400,self.client.post('/account/deletion',data={}).status_code)
        form=self.form();form['password']='wrong'
        self.assertEqual(403,self.client.post('/account/deletion',data=form).status_code)
        form=self.form();form.pop('confirm')
        self.assertEqual(400,self.client.post('/account/deletion',data=form).status_code)
        self.assertIsNone(self.api.get(self.customer,1))

    def test_request_blocks_checkout_keeps_login_and_can_withdraw(self):
        self.authenticate()
        self.assertEqual(302,self.client.post('/account/deletion',data=self.form()).status_code)
        self.assertEqual(409,self.client.post('/billing/checkout').status_code)
        self.assertEqual(200,self.client.get('/private').status_code)
        row=self.api.get(self.customer,1)
        form=self.form(purpose='account_deletion_withdraw',target=row['request_id']);form['request_id']=row['request_id']
        self.assertEqual(302,self.client.post('/account/deletion/withdraw',data=form).status_code)
        self.assertEqual(200,self.client.post('/billing/checkout').status_code)

    def test_wrong_password_throttled_even_with_correct_next_password(self):
        self.authenticate();form=self.form();form['password']='wrong'
        for _ in range(5): self.assertEqual(403,self.client.post('/account/deletion',data=form).status_code)
        form['password']=self.password
        self.assertEqual(403,self.client.post('/account/deletion',data=form).status_code)

    def test_closed_session_is_rejected_even_after_flag_off(self):
        self.authenticate()
        row=self.api.request(self.customer);self.close(row['request_id']);self.db.commit()
        self.app.config['ACCOUNT_LIFECYCLE_ENABLED']=False
        self.assertEqual(302,self.client.get('/private').status_code)
        self.assertFalse(self.client.get('/api/push/context').json['authenticated'])
        with self.client.session_transaction() as session:
            self.assertNotIn('_user_id',session)
            self.assertNotIn('_push_binding',session)

    def test_customer_cannot_open_or_submit_staff_review(self):
        self.authenticate();self.assertEqual(403,self.client.get('/admin/account-deletions').status_code)
        self.assertEqual(403,self.client.post('/admin/account-deletions/2/review').status_code)

    def test_registration_does_not_migrate_database(self):
        before=set(r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'"))
        self.assertIs(self.web,register_account_lifecycle(self.runtime))
        self.assertEqual(before,set(r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")))


if __name__=='__main__': unittest.main()
