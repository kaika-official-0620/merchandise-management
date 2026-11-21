"""The staff member page keeps outgoing documents and received forms scoped."""

from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest

from flask import Flask
from flask_login import LoginManager, UserMixin
from jinja2 import ChoiceLoader, DictLoader, FileSystemLoader

from document_workflow import KINDS, register_document_workflow
from member_documents import register_member_documents


class Actor(UserMixin):
    def __init__(self, user_id, role, permissions):
        self.id, self.role, self.permissions = user_id, role, permissions.split(',')

    def is_admin(self):
        return self.role in ('owner', 'admin')

    def has_permission(self, permission):
        return self.role == 'owner' or permission in self.permissions


class MemberDocumentsTests(unittest.TestCase):
    def setUp(self):
        workspace = tempfile.TemporaryDirectory()
        self.addCleanup(workspace.cleanup)
        self.db_path = Path(workspace.name) / 'fixture.db'
        self.app = Flask(__name__)
        self.app.config.update(TESTING=True, SECRET_KEY='fictional-members-only')
        self.app.jinja_loader = ChoiceLoader([
            DictLoader({'base.html': '{% block content %}{% endblock %}'}),
            FileSystemLoader(Path(__file__).resolve().parents[1] / 'templates'),
        ])
        known_users = [(1,'a','対象会員','user',''), (2,'b','別会員','user',''),
                       (70,'staff','担当さん','admin','users,invoices'),
                       (71,'member','会員管理担当','admin','users'),
                       (72,'docs','書類担当','admin','invoices')]

        with closing(self.connect()) as db, db:
            db.execute('CREATE TABLE users (id INTEGER PRIMARY KEY,username,display_name,role,admin_permissions)')
            db.executemany('INSERT INTO users VALUES (?,?,?,?,?)', known_users)
            for _, (table, _, _, owner, *_) in KINDS.items():
                db.execute(f'CREATE TABLE {table}(id INTEGER PRIMARY KEY,{owner} INTEGER, document_no, invoice_no, status, '
                           'document_scope, issue_date, updated_at, is_admin_created, created_by_admin_id, source_admin_kaitori_id, '
                           'proxy_service_auction_id)')
            db.execute('CREATE TABLE shikiriosho(id INTEGER PRIMARY KEY,recipient_id,document_no,status,issue_date)')
            db.execute('CREATE TABLE final_document_events(id INTEGER PRIMARY KEY, document_kind,document_id,action,after_status,metadata)')
            db.executemany('INSERT INTO user_mitsumori(id,user_id,document_no,status,document_scope,issue_date,updated_at) VALUES (?,?,?,?,?,?,?)', [
                (1,1,'UM-SELF-SENT','submitted','client_incoming','2026-11-21','2026-11-21 05:01:00'),
                (2,1,'UM-SELF-DRAFT','draft','client_incoming','2026-11-21','2026-11-21 05:02:00'),
                (3,1,'MT-STAFF-MEMBER','draft','vendor_outgoing','2026-11-21','2026-11-21 05:03:00'),
                (4,2,'MT-STAFF-OTHER','draft','vendor_outgoing','2026-11-21','2026-11-21 05:04:00'),
                (5,1,'KM-COMPANY','draft','kaika_estimate','2026-11-21','2026-11-21 05:05:00'),
                (6,2,'UM-OTHER-SENT','submitted','client_incoming','2026-11-21','2026-11-21 05:06:00'),
            ])
            db.execute("INSERT INTO final_document_events VALUES(1,'user_mitsumori',1,'staff_inbox_update','in_progress','{\"assignee_user_id\":70}')")
            db.execute("INSERT INTO invoices(id,sender_id,invoice_no,status,document_scope,issue_date) VALUES(1,1,'KT-MEMBER','sent','admin_kaitori','2026-11-22')")
            db.execute("INSERT INTO shikiriosho VALUES(1,1,'SS-MEMBER','sent','2026-11-22')")
            db.execute("INSERT INTO user_keisan(id,user_id,document_no,status,issue_date,is_admin_created) VALUES(1,1,'KS-MEMBER','sent','2026-11-22',true)")
            db.execute('CREATE TABLE sales_agency_requests(id INTEGER PRIMARY KEY,user_id)')
            db.execute('CREATE TABLE sales_agency_request_items(id INTEGER PRIMARY KEY,request_id,item_status,workflow_status,vendor_document_id)')
            db.execute('CREATE TABLE vendor_document_item_links(request_item_id,vendor_document_id)')
            db.execute('INSERT INTO sales_agency_requests VALUES (1,1),(2,2)')
            db.execute("INSERT INTO sales_agency_request_items VALUES (1,1,'active','step2_ready',NULL),(2,1,'active','step4_ready',NULL),(3,2,'cancelled','step2_ready',NULL)")
            db.execute('INSERT INTO vendor_document_item_links VALUES (2,101)')

        users = {str(row[0]): Actor(row[0], row[3], row[4]) for row in known_users}
        manager = LoginManager(self.app)
        manager.user_loader(users.get)
        runtime = SimpleNamespace(app=self.app, DATABASE_URL=None, get_db=self.connect,
                                  build_user_from_record=lambda row: users[str(row['id'])])
        admin_pages = {
            'admin_users':'/admin/users',
            'admin_documents_dashboard':'/admin/documents',
            'admin_mitsumori_view':'/admin/mitsumori/<int:id>',
            'admin_kaitori_view':'/admin/kaitori/<int:id>',
            'admin_shikiriosho_view':'/admin/shikiriosho/<int:id>',
            'admin_auction_keisan_view':'/admin/auction-keisan/<int:id>',
            'admin_vendor_mitsumori_create':'/admin/documents/step2/user/<int:user_id>/vendor-mitsumori',
            'admin_step4_client_invoice_create':'/admin/documents/step4/user/<int:user_id>/client-invoice',
        }
        for endpoint, path in admin_pages.items():
            self.app.add_url_rule(path, endpoint, lambda **_: 'existing destination')
        register_document_workflow(runtime)
        register_member_documents(runtime)
        self.client = self.app.test_client()

    def connect(self):
        db = sqlite3.connect(self.db_path)
        db.row_factory = sqlite3.Row
        return db

    def login(self, user_id):
        with self.client.session_transaction() as session:
            session['_user_id'], session['_fresh'] = str(user_id), True

    def test_staff_with_both_permissions_sees_scoped_member_sections(self):
        self.login(70)
        response = self.client.get('/admin/users/1/documents?search=sample')
        self.assertEqual(response.status_code, 200)
        text = response.get_data(as_text=True)
        for expected in ('対象会員の書類・手続き', '会員から届いた書類', 'UM-SELF-SENT', '対応中', '担当さん',
                         '開花がこの会員のために作成した書類', 'MT-STAFF-MEMBER', 'KT-MEMBER', 'SS-MEMBER', 'KS-MEMBER',
                         '/admin/customer-submissions/estimate/1?member_id=1', '/admin/users?search=sample',
                         '/admin/documents/step2/user/1/vendor-mitsumori', '/admin/documents/step4/user/1/client-invoice'):
            self.assertIn(expected, text)
        for forbidden in ('UM-OTHER-SENT', 'UM-SELF-DRAFT', 'MT-STAFF-OTHER', 'KM-COMPANY'):
            self.assertNotIn(forbidden, text)
        other = self.client.get('/admin/users/2/documents').get_data(as_text=True)
        self.assertIn('UM-OTHER-SENT', other)
        self.assertNotIn('UM-SELF-SENT', other)
        self.assertNotIn('業者向け見積依頼書を作る', other)

    def test_customer_and_staff_missing_permissions_are_denied(self):
        for user_id in (1,2,71,72):
            self.login(user_id)
            self.assertEqual(self.client.get('/admin/users/1/documents').status_code,403)
        self.login(70)
        self.assertEqual(self.client.get('/admin/users/70/documents').status_code,404)
        self.assertEqual(self.client.get('/admin/users/999/documents').status_code,404)
        self.assertEqual(self.client.post('/admin/users/1/documents').status_code,405)
