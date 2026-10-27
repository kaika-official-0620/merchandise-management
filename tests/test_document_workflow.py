"""Fictional local DB: customer document state, scope and one-time delivery."""
from concurrent.futures import ThreadPoolExecutor
import ast
import csv
from datetime import datetime
import io
from pathlib import Path
import re
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest

from flask import Flask, flash, jsonify, redirect, render_template, request, send_file, url_for
from flask_login import LoginManager, UserMixin, current_user, login_user
from jinja2 import ChoiceLoader, DictLoader, Environment, FileSystemLoader
from werkzeug.exceptions import HTTPException

from document_workflow import KINDS, register_document_workflow
from invoice_tax import invoice_tax_context, invoice_tax_category_label


class Person(UserMixin):
    def __init__(self, id, role='user', permissions=True):
        self.id,self.role,self.permissions=id,role,permissions
    def is_admin(self): return self.role in {'admin','owner'}
    def has_permission(self, name): return self.permissions


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, *args):
        try:return super().__exit__(*args)
        finally:self.close()


class DocumentWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='kaika-document-workflow-')
        self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'fictional.sqlite'
        self.app=Flask(__name__)
        self.app.config.update(TESTING=True,SECRET_KEY='fictional-only')
        self.users={str(p.id):p for p in [Person(1),Person(2),Person(9,'admin'),Person(8,'admin',False)]}
        manager=LoginManager(self.app)
        manager.user_loader(lambda key:self.users.get(key))
        self.app.jinja_loader=DictLoader({'customer_documents/inbox.html':'{{ submissions|tojson }}',
            'customer_documents/received_detail.html':'{{ {"document":document,"details":details}|tojson }}',
            'mitsumori_form.html':'{{ {"available":my_merchandise,"history":document_product_history}|tojson }}'})
        for kind, spec in KINDS.items():
            self.app.add_url_rule('/view/'+kind+'/<int:id>',spec[4],lambda id:'detail')
        def form(id=None):
            if request.method=='POST': return jsonify(saved=True,status=request.form.get('status'))
            return render_template('mitsumori_form.html',mitsumori={'id':id} if id else None)
        self.app.add_url_rule('/mitsumori/add','user_mitsumori_add',form,methods=['GET','POST'])
        self.app.add_url_rule('/mitsumori/edit/<int:id>','user_mitsumori_edit',form,methods=['GET','POST'])
        self.app.add_url_rule('/invoices/send/<int:id>','user_invoice_send',lambda id:'unsafe',methods=['GET','POST'])
        self.app.add_url_rule('/invoices','user_invoice_list',lambda:'list')
        self.runtime=SimpleNamespace(app=self.app,DATABASE_URL=None,get_db=self.connect,get_jst_now=lambda:datetime(2026,10,27))
        with self.connect() as db:
            db.execute('CREATE TABLE users(id INTEGER PRIMARY KEY,display_name TEXT)')
            db.executemany('INSERT INTO users VALUES (?,?)',[(1,'Customer A'),(2,'Customer B')])
            db.execute('CREATE TABLE merchandise(id INTEGER PRIMARY KEY,user_id INTEGER,scope TEXT,sale_date TEXT,product_name TEXT,brand_name TEXT,listing_price INTEGER,kaika_product_code TEXT)')
            db.executemany('INSERT INTO merchandise VALUES (?,?,?,?,?,?,?,?)',[(11,1,'user',None,'Bag','Brand',1000,'A-11'),(12,1,'user',None,'Coat','Brand',2000,'A-12'),(13,2,'user',None,'Private B','B',3000,'B-13'),(14,1,'kaika',None,'Company','C',4000,'K-14'),(15,1,'user','2026-10-25','Sold','D',5000,'A-15')])
            for _,(table,items,key,owner,*_) in KINDS.items():
                db.execute(f'CREATE TABLE {table}(id INTEGER PRIMARY KEY,{owner} INTEGER,status TEXT,document_scope TEXT,is_admin_created INTEGER DEFAULT 0,created_by_admin_id INTEGER,source_admin_kaitori_id INTEGER,proxy_service_auction_id INTEGER,source_invoice_id INTEGER,source_document_id INTEGER,source_document_type TEXT,document_no TEXT,invoice_no TEXT,total_amount INTEGER,notes TEXT,updated_at TEXT DEFAULT CURRENT_TIMESTAMP)')
                db.execute(f'CREATE TABLE {items}(id INTEGER PRIMARY KEY,{key} INTEGER,merchandise_id INTEGER,item_no INTEGER,item_name TEXT,product_name TEXT,quantity INTEGER,unit_price INTEGER,amount INTEGER)')
            db.execute('CREATE TABLE final_document_events(id INTEGER PRIMARY KEY,document_kind TEXT,document_id INTEGER,action TEXT,actor_id INTEGER,reason TEXT,before_status TEXT,after_status TEXT,metadata TEXT,created_at TEXT DEFAULT CURRENT_TIMESTAMP)')
        self.service=register_document_workflow(self.runtime)
        self.client=self.app.test_client()
        self.login()

    def connect(self):
        db=sqlite3.connect(self.path,timeout=10,factory=ClosingConnection)
        db.row_factory=sqlite3.Row
        return db

    def login(self,id=1):
        with self.client.session_transaction() as s:
            s['_user_id']=str(id);s['_fresh']=True;s['_security_csrf_user']=str(id);s['_security_csrf']='A'*43

    def add_doc(self,id=101,kind='estimate',status='completed',owner=1,item=11,scope=None):
        table,items,key,owner_key,*_=KINDS[kind]
        scope=scope or ('user_created' if kind=='purchase_statement' else 'client_incoming')
        with self.connect() as db:
            db.execute(f'INSERT INTO {table}(id,{owner_key},status,document_scope,document_no,total_amount) VALUES (?,?,?,?,?,?)',(id,owner,status,scope,f'D-{id}',1000))
            if item is not None:
                db.execute(f'INSERT INTO {items}({key},merchandise_id,item_no,item_name,quantity,unit_price,amount) VALUES (?,?,?,?,?,?,?)',(id,item,1,'Item',1,1000,1000))

    def send(self,id=101,kind='estimate',**values):
        return self.client.post(f'/documents/submit/{kind}/{id}',data={'destination':'kaika','security_csrf_token':'A'*43,**values})

    def form_data(self,**values):
        return {'status':'completed','item_name[]':['Bag'],'quantity[]':['1'],'unit_price[]':['1000'],'merchandise_id[]':['11'],**values}

    def test_direct_complete_does_not_require_preview(self):
        response=self.client.post('/mitsumori/add',data=self.form_data())
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json['status'],'completed')

    def test_cannot_forge_sent_status_on_save(self):
        self.assertEqual(self.client.post('/mitsumori/add',data=self.form_data(status='submitted')).status_code,400)

    def test_saved_but_unsent_does_not_remove_candidate(self):
        self.add_doc(status='draft')
        self.add_doc(id=102,item=12)
        response=self.client.get('/mitsumori/add')
        data=response.get_json(force=True)
        self.assertEqual({row['id'] for row in data['available']},{11,12})
        self.assertEqual(data['history'],[])
        self.assertEqual(data['available'][1]['management_number'],'A-11')

    def test_sent_moves_to_history_without_erasing_product(self):
        self.add_doc()
        self.assertEqual(self.send().status_code,302)
        data=self.client.get('/mitsumori/add').get_json(force=True)
        self.assertEqual([r['id'] for r in data['available']],[12])
        self.assertEqual([r['id'] for r in data['history']],[11])
        with self.connect() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM merchandise').fetchone()[0],5)

    def test_send_once_and_event_once(self):
        self.add_doc()
        self.assertEqual(self.send().status_code,302)
        self.assertEqual(self.send().status_code,302)
        with self.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM final_document_events').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT status FROM user_mitsumori').fetchone()[0],'submitted')

    def test_other_customer_and_company_item_denied(self):
        self.add_doc(owner=2,item=13)
        self.assertEqual(self.send().status_code,404)
        for item in (13,14):
            self.assertEqual(self.client.post('/mitsumori/add',data=self.form_data(**{'merchandise_id[]':[str(item)]})).status_code,404)

    def test_sent_item_rejected_on_direct_post(self):
        self.add_doc(status='submitted')
        self.assertEqual(self.client.post('/mitsumori/add',data=self.form_data()).status_code,409)

    def test_cancelled_and_rejected_free_candidate(self):
        self.add_doc(status='cancelled')
        self.add_doc(id=102,status='rejected',item=12)
        self.assertEqual(len(self.client.get('/api/document-products').json),3)

    def test_bad_quantity_price_duplicate_and_empty(self):
        for values in ({'quantity[]':['-1']},{'quantity[]':['1.5']},{'unit_price[]':['-5']},{'unit_price[]':['9999999999']},{'item_name[]':['']}):
            self.assertEqual(self.client.post('/mitsumori/add',data=self.form_data(**values)).status_code,400)
        self.assertEqual(self.client.post('/mitsumori/add',data=self.form_data(**{'item_name[]':['a','b'],'merchandise_id[]':['11','11']})).status_code,400)

    def test_invalid_csrf_and_destination_fail_without_write(self):
        self.add_doc()
        for values in ({'security_csrf_token':'wrong'},{'security_csrf_token':'あ'*43},{'destination':'other'}):
            self.assertEqual(self.send(**values).status_code,400)
        with self.client.session_transaction() as s:s['_security_csrf']={'bad':'value'}
        self.assertEqual(self.send().status_code,400)
        with self.connect() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM final_document_events').fetchone()[0],0)

    def test_draft_and_empty_cannot_send(self):
        self.add_doc(status='draft')
        self.add_doc(id=102,item=None)
        self.assertEqual(self.send().status_code,409)
        self.assertEqual(self.send(id=102).status_code,409)

    def test_two_documents_compete_for_one_item_only_one_submits(self):
        self.add_doc();self.add_doc(id=102)
        def submit_one(id):
            with self.app.app_context():
                try:return self.service.submit('estimate',id,1)
                except HTTPException as error:return error.code
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(submit_one,(101,102)))
        self.assertCountEqual(results,[True,409])
        with self.connect() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM final_document_events').fetchone()[0],1)

    def test_inbox_only_receives_sent_and_enforces_staff_permission(self):
        self.add_doc();self.add_doc(id=102,item=12,status='draft')
        self.send()
        self.assertEqual(self.client.get('/admin/customer-submissions').status_code,403)
        self.login(8)
        self.assertEqual(self.client.get('/admin/customer-submissions').status_code,403)
        self.login(9)
        docs=self.client.get('/admin/customer-submissions').get_json(force=True)
        self.assertEqual([row['id'] for row in docs],[101])
        self.assertEqual(docs[0]['url'],'/admin/customer-submissions/estimate/101')
        self.assertEqual(self.client.get(docs[0]['url']).status_code,200)
        self.assertEqual(self.client.get('/admin/customer-submissions/estimate/102').status_code,404)

    def test_calculation_detail_uses_matching_table(self):
        self.add_doc(kind='calculation')
        self.assertEqual(self.send(kind='calculation').status_code,302)
        self.login(9)
        data=self.client.get('/admin/customer-submissions/calculation/101').get_json(force=True)
        self.assertEqual(data['document']['document_no'],'D-101')

    def test_purchase_statement_kept_on_hold_including_old_sender(self):
        self.add_doc(kind='purchase_statement')
        self.assertEqual(self.send(kind='purchase_statement').status_code,409)
        self.assertEqual(self.client.post('/invoices/send/101',data={'security_csrf_token':'A'*43}).status_code,409)
        self.assertEqual(self.client.get('/invoices/send/101').status_code,405)

    def test_api_product_filters_keep_invoice_sold_workflow(self):
        self.assertEqual({r['id'] for r in self.client.get('/api/document-products').json},{11,12,15})
        self.assertEqual([r['id'] for r in self.client.get('/api/document-products?sold_only=1&sold_days=7').json],[15])
        self.assertEqual(len(self.client.get('/api/document-products?inventory_only=1').json),2)
        self.assertEqual(self.client.get('/api/document-products?sold_days=bad').status_code,400)

    def source_pair(self, *, source_status='sent', source_owner=1, item=15):
        self.add_doc(id=501,kind='purchase_statement',status=source_status,owner=source_owner,item=item,scope='client_outgoing')
        self.add_doc(id=101,item=item)
        with self.connect() as db:
            db.execute("UPDATE user_mitsumori SET source_invoice_id=501,source_document_id=501,source_document_type='invoice' WHERE id=101")
            return db.execute('SELECT id FROM invoice_items WHERE invoice_id=501').fetchone()[0]

    def test_explicit_source_invoice_can_continue_with_sold_item(self):
        source_item=self.source_pair()
        form={'status':'completed','source_invoice_id':'501','source_invoice_item_id[]':[str(source_item)]}
        self.assertEqual(self.client.post('/mitsumori/add',data=form).status_code,200)
        self.assertEqual(self.send().status_code,302)

    def test_source_exception_does_not_allow_unrelated_new_request(self):
        self.source_pair()
        self.assertEqual(self.client.post('/mitsumori/add',data=self.form_data(**{'merchandise_id[]':['15']})).status_code,409)
        self.add_doc(id=102,item=15)
        self.assertEqual(self.send(id=102).status_code,409)

    def test_source_of_other_customer_is_never_allowed(self):
        source_item=self.source_pair(source_owner=2,item=13)
        form={'status':'completed','source_invoice_id':'501','source_invoice_item_id[]':[str(source_item)]}
        self.assertEqual(self.client.post('/mitsumori/add',data=form).status_code,404)
        self.assertEqual(self.send().status_code,404)

    def test_source_revalidated_at_send_after_cancellation_or_edit(self):
        self.source_pair()
        with self.connect() as db:db.execute("UPDATE invoices SET status='cancelled' WHERE id=501")
        self.assertEqual(self.send().status_code,409)
        with self.connect() as db:
            db.execute("UPDATE invoices SET status='sent' WHERE id=501")
            db.execute('UPDATE invoice_items SET amount=900 WHERE invoice_id=501')
        self.assertEqual(self.send().status_code,409)

    def test_source_only_exempts_that_invoice_and_not_another_estimate(self):
        self.source_pair(item=11)
        self.add_doc(id=502,kind='purchase_statement',status='sent',item=11,scope='client_outgoing')
        self.assertEqual(self.send().status_code,409)
        with self.connect() as db:db.execute("UPDATE invoices SET status='cancelled' WHERE id=502")
        self.add_doc(id=102,status='submitted',item=11)
        self.assertEqual(self.send().status_code,409)

    def test_source_item_must_belong_to_exact_invoice(self):
        self.source_pair(item=11)
        self.add_doc(id=502,kind='purchase_statement',status='sent',item=12,scope='client_outgoing')
        with self.connect() as db:wrong=db.execute('SELECT id FROM invoice_items WHERE invoice_id=502').fetchone()[0]
        self.assertEqual(self.client.post('/mitsumori/add',data={'status':'completed','source_invoice_id':'501','source_invoice_item_id[]':[str(wrong)]}).status_code,400)
        with self.connect() as db:db.execute('UPDATE user_mitsumori_items SET merchandise_id=12 WHERE mitsumori_id=101')
        self.assertEqual(self.send().status_code,409)

    def invoice_handlers(self):
        """Execute the actual legacy handlers without importing app startup/DB side effects."""
        with self.connect() as db:
            fields={'issue_date':'TEXT','payment_due_date':'TEXT','recipient_name':'TEXT','postal_number':'TEXT',
                    'subtotal':'INTEGER','tax_amount_8':'INTEGER','tax_amount_10':'INTEGER','service_type':'TEXT',
                    'commission_rate':'REAL','commission_amount':'INTEGER','bank_info':'TEXT','is_read':'INTEGER DEFAULT 0'}
            for name,typ in fields.items():db.execute(f'ALTER TABLE invoices ADD COLUMN {name} {typ}')
            for name in ('tax_category','product_date','product_code','unit'):
                db.execute(f'ALTER TABLE invoice_items ADD COLUMN {name} TEXT')
        tree=ast.parse((Path(__file__).resolve().parents[1]/'app.py').read_text(encoding='utf-8-sig'))
        functions=[node for node in tree.body if isinstance(node,ast.FunctionDef) and node.name in {'user_invoice_add','user_invoice_edit','invoice_download'}]
        self.assertEqual(len(functions),3)
        for func in functions:func.decorator_list=[]
        namespace=dict(get_db=self.connect,DATABASE_URL=None,request=request,current_user=current_user,
                       get_jst_now=lambda:datetime(2026,10,27),generate_invoice_no=lambda:'REF-TEST',
                       flash=flash,redirect=redirect,url_for=url_for,io=io,csv=csv,send_file=send_file)
        exec(compile(ast.Module(body=functions,type_ignores=[]),'app.py','exec'),namespace)
        return namespace

    def test_actual_invoice_add_never_activates_unapproved_rate(self):
        handlers=self.invoice_handlers()
        form={'status':'completed','issue_date':'2026-10-27','service_type':'wholesale',
              'commission_amount':'999999','commission_rate':'99','product_name[]':['item'],
              'quantity[]':['2'],'unit_price[]':['999'],'tax_category[]':['10']}
        with self.app.test_request_context('/invoices/add',method='POST',data=form):
            login_user(self.users['1'])
            response=handlers['user_invoice_add']()
            self.assertEqual(response.status_code,302)
        with self.connect() as db:
            row=dict(db.execute('SELECT * FROM invoices').fetchone())
        self.assertEqual((row['service_type'],row['commission_rate'],row['commission_amount']),('pending_review',0,0))
        self.assertEqual((row['subtotal'],row['tax_amount_10'],row['total_amount']),(1998,199,2197))

    def test_actual_invoice_edit_preserves_saved_fee_and_rate(self):
        handlers=self.invoice_handlers()
        self.add_doc(kind='purchase_statement',status='draft')
        with self.connect() as db:
            db.execute("UPDATE invoices SET service_type='legacy',commission_rate=3,commission_amount=650 WHERE id=101")
        form={'status':'completed','issue_date':'2026-10-27','service_type':'wholesale','commission_amount':'999999',
              'commission_rate':'99','product_name[]':['item'],'quantity[]':['2'],'unit_price[]':['999'],'tax_category[]':['10']}
        with self.app.test_request_context('/invoices/edit/101',method='POST',data=form):
            login_user(self.users['1'])
            self.assertEqual(handlers['user_invoice_edit'](101).status_code,302)
        with self.connect() as db:row=dict(db.execute('SELECT * FROM invoices WHERE id=101').fetchone())
        self.assertEqual((row['service_type'],row['commission_rate'],row['commission_amount']),('legacy',3,650))
        self.assertEqual((row['subtotal'],row['tax_amount_10'],row['total_amount']),(1998,199,2847))

    def reference_invoice(self, service_type='pending_review'):
        return dict(id=101,invoice_no='REF-101',document_scope='user_created',status='completed',
                    service_type=service_type,issue_date='2026-10-27',payment_due_date=None,
                    subtotal=5500,tax_amount_8=0,tax_amount_10=550,total_amount=6050,
                    commission_amount=0,recipient_name='Fictional recipient',notes='Reference note',bank_info='')

    def render_invoice_template(self, name, invoice):
        templates=Path(__file__).resolve().parents[1]/'templates'
        environment=Environment(loader=ChoiceLoader([
            DictLoader({'base.html':'{% block content %}{% endblock %}',
                        'customer_documents/delivery.html':'','pdf/_pdf_toolbar.html':''}),
            FileSystemLoader(str(templates))]),autoescape=True)
        environment.globals.update(invoice_tax_context=invoice_tax_context,
            invoice_tax_category_label=invoice_tax_category_label,url_for=lambda *a,**kw:'#',
            current_user=SimpleNamespace(display_name='Fictional customer'))
        html=environment.get_template(name).render(invoice=invoice,items=[dict(item_no=1,
            product_name='Fictional item',quantity=1,unit_price=5500,amount=5500,tax_category='10')])
        # Inspect visible document content, excluding CSS, script and explanatory HTML comments.
        return re.sub(r'<style\b[^>]*>.*?</style>|<script\b[^>]*>.*?</script>|<!--.*?-->','',html,flags=re.S)

    def test_pending_invoice_detail_and_pdf_are_reference_not_payment_request(self):
        invoice=self.reference_invoice()
        original=dict(invoice)
        for name in ('invoice_view.html','pdf/invoice_pdf.html'):
            with self.subTest(template=name):
                html=self.render_invoice_template(name,invoice)
                self.assertIn('参考合計・請求未確定',html)
                self.assertIn('単価が税込か税抜かは未確認',html)
                self.assertIn('開花料金は未確認',html)
                self.assertIn('¥6,050',html)
                self.assertNotIn('請求金額',html)
                self.assertNotIn('下記の通りご請求',html)
        self.assertEqual(invoice,original)

    def test_existing_invoice_detail_and_pdf_keep_saved_display_branch(self):
        invoice=self.reference_invoice('legacy')
        for name in ('invoice_view.html','pdf/invoice_pdf.html'):
            with self.subTest(template=name):
                html=self.render_invoice_template(name,invoice)
                self.assertIn('請求金額',html)
                self.assertIn('¥6,050',html)
                self.assertNotIn('参考合計・請求未確定',html)

    def invoice_csv(self, service_type):
        handlers=self.invoice_handlers()
        self.add_doc(kind='purchase_statement')
        values=self.reference_invoice(service_type)
        values.pop('id')
        with self.connect() as db:
            db.execute('UPDATE invoices SET '+','.join(f'{key}=?' for key in values)+' WHERE id=101',tuple(values.values()))
            before=dict(db.execute('SELECT * FROM invoices WHERE id=101').fetchone())
        with self.app.test_request_context('/invoices/download/101'):
            login_user(self.users['1'])
            response=handlers['invoice_download'](101)
            response.direct_passthrough=False
            content=response.get_data().decode('utf-8-sig')
        with self.connect() as db:after=dict(db.execute('SELECT * FROM invoices WHERE id=101').fetchone())
        before.pop('is_read');after.pop('is_read')
        self.assertEqual(after,before)
        return content

    def test_pending_invoice_csv_keeps_amounts_and_marks_fee_and_tax_unconfirmed(self):
        content=self.invoice_csv('pending_review')
        self.assertIn('参考合計・請求未確定',content)
        self.assertIn('単価の税込・税抜は未確認',content)
        self.assertIn('"開花料金","未確認"',content)
        self.assertIn('¥6,050',content)
        self.assertNotIn('請求金額 (税込)',content)

    def test_existing_invoice_csv_keeps_saved_display_branch(self):
        content=self.invoice_csv('legacy')
        self.assertIn('請求金額 (税込)',content)
        self.assertIn('¥6,050',content)
        self.assertNotIn('参考合計・請求未確定',content)


if __name__=='__main__':unittest.main()
