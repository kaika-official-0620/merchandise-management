"""Support intake against a disposable DB and actual scoped legacy handlers."""
import ast
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from flask import Flask, flash, jsonify, redirect, render_template, request, url_for
from flask_login import LoginManager, UserMixin, current_user, login_required
from jinja2 import ChoiceLoader, DictLoader, FileSystemLoader
from PIL import Image
from werkzeug.datastructures import FileStorage

from inquiry_support import InquiryValidationError, inquiry_jst_timestamp, prepare_images, register_inquiry_support
from security_access import register_security_access


ROOT = Path(__file__).resolve().parents[1]


class User(UserMixin):
    def __init__(self, id): self.id, self.role = int(id), 'admin' if str(id) == '9' else 'user'
    def is_admin(self): return self.role == 'admin'


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, *args):
        try: return super().__exit__(*args)
        finally: self.close()


class InquirySupportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tree = ast.parse((ROOT / 'app.py').read_text(encoding='utf-8-sig'))
        names = {'inquiry_view', 'inquiry_reply', 'inquiry_delete', 'admin_inquiry_view', 'admin_inquiry_reply', 'admin_inquiry_status', 'parse_inquiry_image_paths'}
        cls.handlers = ast.Module(body=[node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names], type_ignores=[])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='kaika-support-fictional-')
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'fictional.sqlite'
        static = Path(self.tmp.name) / 'static'
        (static / 'uploads').mkdir(parents=True)
        self.app = Flask(__name__, static_folder=str(static))
        self.app.config.update(SECRET_KEY='fictional-test-only', TESTING=True, UPLOAD_FOLDER=str(static / 'uploads'))
        self.app.jinja_loader = ChoiceLoader([DictLoader({'base.html': '{% block content %}{% endblock %}'}), FileSystemLoader(str(ROOT / 'templates'))])
        manager = LoginManager(self.app)
        manager.user_loader(lambda id: User(id))
        with self.connect() as db:
            db.executescript('''CREATE TABLE users(id INTEGER PRIMARY KEY, display_name TEXT, username TEXT, role TEXT);
                INSERT INTO users VALUES(1,'A','a','user'),(2,'B','b','user'),(9,'Staff','staff','admin');
                CREATE TABLE inquiries(id INTEGER PRIMARY KEY,user_id INTEGER,category TEXT,title TEXT,content TEXT,image_path TEXT,
                    status TEXT DEFAULT 'new',created_at TEXT DEFAULT CURRENT_TIMESTAMP,updated_at TEXT DEFAULT CURRENT_TIMESTAMP);
                CREATE TABLE inquiry_replies(id INTEGER PRIMARY KEY,inquiry_id INTEGER,user_id INTEGER,content TEXT,is_admin_reply INTEGER,created_at TEXT DEFAULT CURRENT_TIMESTAMP);
                CREATE TABLE merchandise(id INTEGER); CREATE TABLE vendor_documents(id INTEGER);
                CREATE TABLE admin_kaitori_shoudaku(id INTEGER); CREATE TABLE sale_requests(id INTEGER);
                CREATE TABLE sales_agency_request_items(id INTEGER); CREATE TABLE sales_agency_requests(id INTEGER);''')
        self.runtime = SimpleNamespace(app=self.app, DATABASE_URL=None, get_db=self.connect,
            INQUIRY_STATUS={'new': 'new', 'in_progress': 'in_progress', 'resolved': 'resolved', 'closed': 'closed'})
        namespace = dict(app=self.app, login_required=login_required, current_user=current_user,
            request=request, render_template=render_template, redirect=redirect, flash=flash,
            url_for=url_for, jsonify=jsonify, get_db=self.connect, DATABASE_URL=None, sqlite3=sqlite3,
            json=json, INQUIRY_STATUS=self.runtime.INQUIRY_STATUS, INQUIRY_CATEGORIES={})
        self.namespace = namespace
        exec(compile(self.handlers, str(ROOT / 'app.py'), 'exec'), namespace)
        self.app.add_url_rule('/inquiry', 'inquiry_list', lambda: 'list')
        self.app.add_url_rule('/admin/inquiries', 'admin_inquiries', lambda: 'list')
        self.app.add_url_rule('/inquiry/new', 'inquiry_new', lambda: 'unsafe', methods=['GET', 'POST'])
        self.app.add_url_rule('/', 'index', lambda: 'home')
        self.runtime.parse_inquiry_image_paths = namespace['parse_inquiry_image_paths']
        register_security_access(self.runtime)
        register_inquiry_support(self.runtime)
        namespace['INQUIRY_CATEGORIES'] = self.runtime.INQUIRY_CATEGORIES
        self.client = self.app.test_client()
        self.login()

    def connect(self):
        db = sqlite3.connect(self.path, factory=ClosingConnection)
        db.row_factory = sqlite3.Row
        return db

    def login(self, id=1):
        with self.client.session_transaction() as session:
            session['_user_id'] = str(id)
            session['_fresh'] = True
            session['_security_csrf_user'] = str(id)
            session['_security_csrf'] = 'A'*43

    def create(self, **data):
        values = dict(category='technical', title='Save error', content='Please check', security_csrf_token='A'*43)
        values.update(data)
        return self.client.post('/inquiry/new', data=values)

    def one(self):
        with self.connect() as db: return dict(db.execute('SELECT * FROM inquiries LIMIT 1').fetchone())

    def test_intake_saves_context_and_opens_receipt_without_external_delivery(self):
        response = self.create(reference='D-100', device='iphone', page_name='Documents', expected='Saved', actual='Error')
        self.assertEqual(response.status_code,302)
        self.assertEqual(response.location, '/inquiry/1')
        row = self.one()
        self.assertEqual(row['user_id'],1)
        self.assertIn('D-100',row['content'])
        self.assertIn('iPhone',row['content'])
        page = self.client.get(response.location).get_data(as_text=True)
        self.assertIn('受付番号 1',page)
        self.assertIn('D-100',page)

    def test_missing_csrf_and_other_user_token_cannot_create(self):
        for token in ['', 'B'*43]:
            self.assertEqual(self.create(security_csrf_token=token).status_code,400)
        with self.connect() as db: self.assertEqual(db.execute('SELECT count(*) FROM inquiries').fetchone()[0],0)

    def test_invalid_category_and_long_context_keep_form_values_without_inserting(self):
        response = self.create(category='forged', title='Keep this title')
        self.assertEqual(response.status_code,400)
        self.assertIn('Keep this title', response.get_data(as_text=True))
        self.assertEqual(self.create(reference='x'*121).status_code,400)
        self.assertEqual(self.create(title='x'*161).status_code,400)
        with self.connect() as db: self.assertEqual(db.execute('SELECT count(*) FROM inquiries').fetchone()[0],0)

    def test_create_failure_does_not_expose_exception_or_leave_attachment(self):
        image = io.BytesIO(); Image.new('RGB',(12,12)).save(image,'PNG'); image.seek(0)
        with patch.object(self.runtime,'get_db',side_effect=RuntimeError('SECRET-DB-CREDENTIAL')):
            response = self.create(images=(image,'test.png'))
        self.assertEqual(response.status_code,503)
        self.assertNotIn('SECRET-DB-CREDENTIAL',response.get_data(as_text=True))
        self.assertEqual(list((Path(self.app.config['UPLOAD_FOLDER']) / 'inquiries').glob('*')),[])

    def test_real_images_are_reencoded_unique_and_access_is_owner_scoped(self):
        def picture():
            data=io.BytesIO(); Image.new('RGB',(10,10)).save(data,'PNG'); data.seek(0); return data
        self.create(images=(picture(),'../../same.png'))
        path = json.loads(self.one()['image_path'])[0]
        self.assertRegex(path,r'^uploads/inquiries/[0-9a-f]{32}\.jpg$')
        own = self.client.get('/static/' + path)
        self.assertEqual(own.status_code,200)
        self.assertIn('no-store',own.headers['Cache-Control'])
        own.close()
        self.login(2)
        self.assertEqual(self.client.get('/static/' + path).status_code,404)
        self.assertEqual(self.client.get('/inquiry/1').status_code,302)
        self.assertEqual(self.client.post('/inquiry/1/reply',data={'content':'Forged','security_csrf_token':'A'*43}).status_code,404)

    def test_forged_image_extension_and_excess_batch_fail_before_any_write(self):
        for files in [[FileStorage(stream=io.BytesIO(b'<svg onload=alert(1)>'),filename='fake.png')],
                      [FileStorage(stream=io.BytesIO(b'x'),filename='x.png') for _ in range(6)]]:
            with self.assertRaises(InquiryValidationError): prepare_images(files)

    def test_html_in_content_and_reply_is_rendered_as_text_for_customer_and_staff(self):
        payload='<img src=x onerror=alert(1)>'
        self.create(content=payload)
        response=self.client.post('/inquiry/1/reply', data={'content':payload,'security_csrf_token':'A'*43})
        self.assertEqual(response.status_code,302)
        page=self.client.get('/inquiry/1').get_data(as_text=True)
        self.assertNotIn(payload,page)
        self.assertGreaterEqual(page.count('&lt;img'),2)
        with self.app.test_request_context():
            page=render_template('admin/inquiry_view.html',inquiry=self.one(),replies=[{'content':payload}],image_paths=[],categories={},statuses={})
        self.assertNotIn(payload,page)

    def test_closed_customer_thread_and_oversize_reply_are_rejected(self):
        self.create()
        with self.connect() as db: db.execute("UPDATE inquiries SET status='closed'"); db.commit()
        self.assertEqual(self.client.post('/inquiry/1/reply',data={'content':'Hello','security_csrf_token':'A'*43}).status_code,409)
        self.assertEqual(self.client.post('/inquiry/1/reply',data={'content':'x'*10001,'security_csrf_token':'A'*43}).status_code,302)
        with self.connect() as db: self.assertEqual(db.execute('SELECT count(*) FROM inquiry_replies').fetchone()[0],0)

    def test_admin_status_and_reply_require_role_csrf_valid_status(self):
        self.create()
        headers={'X-Security-CSRF-Token':'A'*43}
        self.assertEqual(self.client.post('/admin/inquiry/1/status',json={'status':'resolved'},headers=headers).status_code,403)
        self.login(9)
        self.assertEqual(self.client.post('/admin/inquiry/1/status',json={'status':'resolved'}).status_code,400)
        self.assertEqual(self.client.post('/admin/inquiry/1/status',json={'status':'invented'},headers=headers).status_code,400)
        self.assertEqual(self.client.post('/admin/inquiry/1/status',json={'status':'resolved'},headers=headers).status_code,200)
        self.assertEqual(self.one()['status'],'resolved')
        self.assertEqual(self.client.post('/admin/inquiry/1/reply',data={'content':'Done','status':'closed','security_csrf_token':'A'*43}).status_code,302)
        self.assertEqual(self.one()['status'],'closed')

    def test_detail_database_failures_return_generic_503_without_private_log_or_flash(self):
        def fail(): raise RuntimeError('SECRET-CREDENTIAL-AND-CUSTOMER-DATA')
        self.namespace['get_db'] = fail
        for user_id, path in [(1,'/inquiry/1'),(9,'/admin/inquiry/1'),(1,'/inquiry/1/delete')]:
            self.login(user_id)
            with self.assertLogs(self.app.logger,level='ERROR') as logs:
                response = (self.client.post(path,data={'security_csrf_token':'A'*43})
                            if path.endswith('/delete') else self.client.get(path))
            self.assertEqual(response.status_code,503)
            self.assertIn('no-store',response.headers['Cache-Control'])
            self.assertNotIn('SECRET-',response.get_data(as_text=True))
            self.assertNotIn('SECRET-',' '.join(logs.output))

    def test_runtime_customer_and_admin_list_failures_are_503_not_empty_success(self):
        tree=ast.parse((ROOT/'kaika_followup_patch_20260612.py').read_text(encoding='utf-8-sig'))
        nodes=[node for node in ast.walk(tree) if isinstance(node,ast.FunctionDef)
               and node.name in {'inquiry_list_followup','admin_inquiries_followup'}]
        def fail(): raise RuntimeError('SECRET-CONNECTION-STRING')
        namespace=dict(self.namespace,search_filters=lambda:{},open_cursor=fail,
                       render_permission_denied=None,module=self.runtime)
        exec(compile(ast.Module(body=nodes,type_ignores=[]),'inquiry-followup-failure','exec'),namespace)
        self.app.view_functions['inquiry_list']=namespace['inquiry_list_followup']
        self.app.view_functions['admin_inquiries']=namespace['admin_inquiries_followup']
        for user_id,path in [(1,'/inquiry'),(9,'/admin/inquiries')]:
            self.login(user_id)
            with self.assertLogs(self.app.logger,level='ERROR') as logs:
                response=self.client.get(path)
            self.assertEqual(response.status_code,503)
            self.assertNotIn('SECRET-',response.get_data(as_text=True))
            self.assertNotIn('SECRET-',' '.join(logs.output))

    def test_support_guide_link_only_appears_when_registered(self):
        self.app.add_url_rule('/my/support/guide','customer_navigation.guide',lambda:'guide')
        page=self.client.get('/inquiry/new').get_data(as_text=True)
        self.assertIn('href="/my/support/guide"',page)

    def test_new_inquiry_and_both_reply_roles_store_explicit_jst_without_changing_old_values(self):
        legacy='2026-09-01 01:02:03'
        with self.connect() as db:
            db.execute('INSERT INTO inquiries(id,user_id,title,content,created_at) VALUES(90,2,?,?,?)',('Old','Past',legacy))
        recorded='2026-10-28 00:01:02'
        with patch('inquiry_support.inquiry_jst_timestamp',return_value=recorded):
            response=self.create()
            new_id=int(response.location.rsplit('/',1)[-1])
            self.assertEqual(self.client.post(f'/inquiry/{new_id}/reply',data={'content':'Additional','security_csrf_token':'A'*43}).status_code,302)
            self.login(9)
            self.assertEqual(self.client.post(f'/admin/inquiry/{new_id}/reply',data={'content':'Reply','security_csrf_token':'A'*43}).status_code,302)
        with self.connect() as db:
            row=db.execute('SELECT created_at,updated_at FROM inquiries WHERE id=?',(new_id,)).fetchone()
            self.assertEqual(tuple(row),(recorded,recorded))
            replies=db.execute('SELECT created_at FROM inquiry_replies WHERE inquiry_id=?',(new_id,)).fetchall()
            self.assertEqual([row[0] for row in replies],[recorded,recorded])
            self.assertEqual(db.execute('SELECT created_at FROM inquiries WHERE id=90').fetchone()[0],legacy)
        self.login()
        page=self.client.get(f'/inquiry/{new_id}').get_data(as_text=True)
        self.assertIn('2026/10/28 00:01',page)
        self.assertIn('日本時間で記録',page)
        self.assertIn('時刻の基準が未確認',page)

    def test_clock_requests_japan_timezone_at_day_boundary(self):
        midnight=datetime(2026,10,28,0,1,2,tzinfo=timezone(timedelta(hours=9)))
        with patch('inquiry_support.datetime') as clock:
            clock.now.return_value=midnight
            self.assertEqual(inquiry_jst_timestamp(),'2026-10-28 00:01:02')
            self.assertEqual(clock.now.call_args.args[0].utcoffset(None),timedelta(hours=9))


if __name__ == '__main__': unittest.main()
