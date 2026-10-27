"""Customer documents: save first, submit separately to Kaika's private inbox.

Uses the existing document/item/event tables. Registration performs no DDL,
payment, email or delivery. Candidate filtering never reserves an item: the
submission transaction locks the actual items and checks again.
"""
from contextlib import contextmanager
from collections import Counter
from datetime import date, timedelta
from functools import wraps
import hmac
import json
import re

from flask import abort, before_render_template, flash, jsonify, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required


SENT = frozenset({'sent', 'submitted', 'approved', 'pending', 'accepted'})
KINDS = {
    'estimate': ('user_mitsumori', 'user_mitsumori_items', 'mitsumori_id', 'user_id', 'user_mitsumori_view', 'admin_user_mitsumori_view', '見積依頼書'),
    'consent': ('user_kaitori_shoudaku', 'user_kaitori_shoudaku_items', 'kaitori_shoudaku_id', 'user_id', 'user_kaitori_shoudaku_view', 'admin_user_kaitori_shoudaku_view', '買取承諾書'),
    'calculation': ('user_keisan', 'user_keisan_items', 'keisan_id', 'user_id', 'user_keisan_view', 'admin_auction_keisan_view', '計算書'),
    'purchase_statement': ('invoices', 'invoice_items', 'invoice_id', 'sender_id', 'user_invoice_view', 'admin_kaitori_view', '買取明細書'),
}
FORMS = {
    'user_mitsumori_add': 'estimate', 'user_mitsumori_edit': 'estimate',
    'user_kaitori_shoudaku_add': 'consent', 'user_kaitori_shoudaku_edit': 'consent',
    'user_keisan_add': 'calculation', 'user_keisan_edit': 'calculation',
    'user_invoice_add': 'purchase_statement', 'user_invoice_edit': 'purchase_statement',
}
MAX_TOTAL = 2_000_000_000


class DocumentWorkflow:
    def __init__(self, runtime):
        self.runtime = runtime
        self.app = runtime.app
        self.pg = bool(getattr(runtime, 'DATABASE_URL', None))
        self.mark = '%s' if self.pg else '?'

    @contextmanager
    def db(self, *, write=False):
        conn = self.runtime.get_db()
        cur = conn.cursor()
        try:
            if write and not self.pg:
                cur.execute('BEGIN IMMEDIATE')
            yield conn, cur
            if write:
                conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.close()
            conn.close()

    def rows(self, cur):
        names = [column[0] for column in cur.description]
        return [dict(row) if hasattr(row, 'keys') else dict(zip(names, row)) for row in cur.fetchall()]

    def one(self, cur):
        rows = self.rows(cur)
        return rows[0] if rows else None

    def columns(self, cur, table):
        if self.pg:
            cur.execute('SELECT column_name FROM information_schema.columns WHERE table_schema=current_schema() AND table_name=%s', (table,))
            return {next(iter(row.values())) if hasattr(row, 'keys') else row[0] for row in cur.fetchall()}
        cur.execute(f'PRAGMA table_info({table})')
        return {row['name'] if hasattr(row, 'keys') else row[1] for row in cur.fetchall()}

    @staticmethod
    def owned_customer_document(kind, row, user_id):
        owner = KINDS[kind][3]
        if not row or str(row.get(owner)) != str(user_id) or row.get('is_admin_created') or row.get('created_by_admin_id'):
            return False
        if kind in {'estimate', 'consent'}:
            return (row.get('document_scope') or 'client_incoming') == 'client_incoming'
        if kind == 'calculation':
            return not row.get('proxy_service_auction_id')
        return (row.get('document_scope') or 'user_created') in {'client_outgoing', 'user_created'} and not row.get('source_admin_kaitori_id') and not str(row.get('invoice_no') or '').upper().startswith('KT-')

    def document(self, cur, kind, document_id, user_id, *, lock=False):
        table, _, _, owner, *_ = KINDS[kind]
        cur.execute(f'SELECT * FROM {table} WHERE id={self.mark} AND {owner}={self.mark}' + (' FOR UPDATE' if self.pg and lock else ''), (document_id, user_id))
        row = self.one(cur)
        if not self.owned_customer_document(kind, row, user_id):
            abort(404)
        return row

    def submitted_item_ids(self, cur, user_id, *, exclude=None, source_invoice_id=None):
        found = set()
        for kind, (table, items, key, owner, *_) in KINDS.items():
            if 'merchandise_id' not in self.columns(cur, items):
                continue
            cols = self.columns(cur, table)
            if not {'id', owner, 'status'} <= cols:
                continue
            where = [f'd.{owner}={self.mark}', "d.status IN ('sent','submitted','approved','pending','accepted')", 'i.merchandise_id IS NOT NULL']
            values = [user_id]
            if 'is_admin_created' in cols:
                where.append('COALESCE(d.is_admin_created, FALSE)=FALSE' if self.pg else 'COALESCE(d.is_admin_created,0)=0')
            if 'document_scope' in cols:
                scope = 'client_outgoing' if kind == 'purchase_statement' else 'client_incoming'
                if kind == 'purchase_statement':
                    where.append("COALESCE(d.document_scope, 'user_created') IN ('user_created','client_outgoing')")
                else:
                    where.append(f"COALESCE(d.document_scope, '{scope}')={self.mark}")
                    values.append(scope)
            if exclude and exclude[0] == kind:
                where.append(f'd.id<>{self.mark}')
                values.append(exclude[1])
            if kind == 'purchase_statement' and source_invoice_id is not None:
                where.append(f'd.id<>{self.mark}')
                values.append(source_invoice_id)
            cur.execute(f"SELECT i.merchandise_id FROM {items} i JOIN {table} d ON d.id=i.{key} WHERE " + ' AND '.join(where), tuple(values))
            for row in self.rows(cur):
                found.add(int(row['merchandise_id']))
        return found

    def products(self, cur, user_id, *, exclude=None, include_sold=False):
        cols = self.columns(cur, 'merchandise')
        fields = [name for name in ('id', 'product_name', 'brand_name', 'listing_price', 'purchase_price', 'sale_price', 'purchase_date', 'sale_date', 'item_condition', 'commission', 'shipping_cost', 'sales_destination', 'photo_path', 'management_number', 'management_no', 'product_code', 'kaika_product_code') if name in cols]
        where = f'user_id={self.mark}'
        if 'scope' in cols:
            where += " AND scope='user'"
        if 'sale_date' in cols and not include_sold:
            where += " AND (sale_date IS NULL OR CAST(sale_date AS TEXT)='')"
        cur.execute('SELECT ' + ','.join(fields) + f' FROM merchandise WHERE {where} ORDER BY id DESC', (user_id,))
        products = self.rows(cur)
        blocked = self.submitted_item_ids(cur, user_id, exclude=exclude)
        available, history = [], []
        for product in products:
            product['management_number'] = product.get('management_number') or product.get('management_no') or product.get('kaika_product_code') or product.get('product_code') or str(product['id'])
            if int(product['id']) in blocked:
                product['submission_status_label'] = '申請済み'
                history.append(product)
            else:
                available.append(product)
        return available, history

    def check_items(self, cur, user_id, item_ids, *, exclude=None, lock=False, allow_sold=False, source_invoice_id=None):
        if len(item_ids) != len(set(item_ids)):
            abort(400, description='同じ商品を重複して追加することはできません。')
        if not item_ids:
            return
        marks = ','.join(self.mark for _ in item_ids)
        cols = self.columns(cur, 'merchandise')
        scope_filter = " AND scope='user'" if 'scope' in cols else ''
        cur.execute(f'SELECT id, user_id, sale_date FROM merchandise WHERE id IN ({marks}){scope_filter} ORDER BY id' + (' FOR UPDATE' if self.pg and lock else ''), tuple(sorted(item_ids)))
        rows = self.rows(cur)
        if len(rows) != len(item_ids) or any(str(row['user_id']) != str(user_id) for row in rows):
            abort(404)
        if not allow_sold and any(row.get('sale_date') for row in rows):
            abort(409, description='売却済みの商品が含まれています。商品を選び直してください。')
        if set(item_ids) & self.submitted_item_ids(cur, user_id, exclude=exclude, source_invoice_id=source_invoice_id):
            abort(409, description='すでに申請済みの商品が含まれています。申請済みの一覧で確認してください。')

    def source_invoice(self, cur, invoice_id, user_id, *, lock=False):
        """The one existing, explicit invoice -> estimate continuation only."""
        cur.execute(f'SELECT * FROM invoices WHERE id={self.mark} AND sender_id={self.mark}' + (' FOR UPDATE' if self.pg and lock else ''), (invoice_id, user_id))
        invoice = self.one(cur)
        if not invoice:
            abort(404)
        if ((invoice.get('document_scope') or 'client_outgoing') != 'client_outgoing'
                or invoice.get('status') not in {'sent', 'submitted', 'approved', 'completed', 'accepted'}):
            abort(409, description='元の買取明細書は後続の見積依頼に利用できない状態です。')
        cur.execute(f'SELECT * FROM invoice_items WHERE invoice_id={self.mark} ORDER BY item_no,id', (invoice_id,))
        return self.rows(cur)

    @staticmethod
    def source_snapshot(row, index):
        quantity = int(row.get('quantity') or 1)
        amount = int(row.get('amount') or 0)
        price = int(row.get('unit_price') or 0)
        if not price and quantity:
            price = amount // quantity
        return (row.get('merchandise_id'), row.get('product_name') or row.get('item_name') or f'Item {index}',
                quantity, row.get('unit') or '点', price, amount)

    def validate_form(self, kind, document_id=None):
        status = request.form.get('status', 'draft')
        if status not in {'draft', 'completed'}:
            abort(400, description='まず作成を完了し、詳細画面の送信欄から送信してください。')
        if document_id:
            with self.db() as (_, cur):
                doc = self.document(cur, kind, document_id, current_user.id)
                if doc.get('status') not in {'draft', 'in_progress', None, ''}:
                    abort(409, description='この書類は編集できません。')
        source_id = request.form.get('source_invoice_id', type=int) or request.args.get('source_invoice_id', type=int)
        with self.db() as (_, cur):
            if kind == 'estimate' and source_id:
                source_rows = self.source_invoice(cur, source_id, current_user.id)
                ids = request.form.getlist('source_invoice_item_id[]') or request.form.getlist('source_invoice_item_choice[]')
                if any(not str(value).isdigit() for value in ids) or len(ids) != len(set(ids)):
                    abort(400)
                allowed = {str(row['id']): row for row in source_rows}
                if not ids or any(value not in allowed for value in ids):
                    abort(400, description='元の書類から商品を選択してください。')
                item_ids = [int(allowed[value]['merchandise_id']) for value in ids if allowed[value].get('merchandise_id')]
            else:
                names = request.form.getlist('product_name[]' if kind in {'consent', 'purchase_statement'} else 'item_name[]')
                quantities, prices = request.form.getlist('quantity[]'), request.form.getlist('unit_price[]')
                if len(names) > 500:
                    abort(400, description='一度に登録する明細は500行までです。')
                active = [i for i, value in enumerate(names) if value.strip()]
                if status == 'completed' and not active:
                    abort(400, description='商品明細を1件以上入力してください。')
                total = 0
                for i in active:
                    qty = quantities[i] if i < len(quantities) and quantities[i] else '1'
                    price = prices[i] if i < len(prices) and prices[i] else '0'
                    if not re.fullmatch(r'[0-9]{1,7}', qty) or not re.fullmatch(r'[0-9]{1,10}', price) or int(qty) < 1:
                        abort(400, description='数量は1以上、単価は0以上の整数で入力してください。')
                    total += int(qty) * int(price)
                    if total > MAX_TOTAL:
                        abort(400, description='合計金額が入力可能な範囲を超えています。')
                raw_ids = request.form.getlist('merchandise_id[]')
                selected = [raw_ids[i] for i in active if i < len(raw_ids) and raw_ids[i]]
                if any(not re.fullmatch(r'[0-9]{1,12}', value) for value in selected):
                    abort(400)
                item_ids = [int(value) for value in selected]
            explicit_source = source_id if kind == 'estimate' and source_id else None
            self.check_items(cur, current_user.id, item_ids, exclude=(kind, document_id) if document_id else None,
                             allow_sold=kind != 'estimate' or explicit_source is not None, source_invoice_id=explicit_source)

    def submit(self, kind, document_id, user_id):
        if kind not in KINDS:
            abort(404)
        if kind == 'purchase_statement':
            abort(409, description='買取明細書の送信は、適用する契約と手数料の計算を確認するまで保留しています。保存済みの内容は保持されています。')
        with self.db(write=True) as (_, cur):
            doc = self.document(cur, kind, document_id, user_id, lock=True)
            if doc.get('status') in SENT:
                return False
            if doc.get('status') != 'completed':
                abort(409, description='作成完了した書類だけを送信できます。')
            table, items, key, _, *_ = KINDS[kind]
            cur.execute(f'SELECT * FROM {items} WHERE {key}={self.mark} ORDER BY id', (document_id,))
            details = self.rows(cur)
            if not details:
                abort(409, description='商品明細のない書類は送信できません。')
            ids = [int(item['merchandise_id']) for item in details if item.get('merchandise_id')]
            source_id = doc.get('source_invoice_id') if kind == 'estimate' else None
            if source_id:
                if (doc.get('source_document_type') not in {None, '', 'invoice'}
                        or doc.get('source_document_id') not in {None, source_id}):
                    abort(409, description='元書類の対応関係を確認してください。')
                source_rows = self.source_invoice(cur, source_id, user_id, lock=True)
                source_values = Counter(self.source_snapshot(row, i) for i,row in enumerate(source_rows, 1))
                snapshots = Counter((row.get('merchandise_id'), row.get('item_name'), int(row.get('quantity') or 1),
                    row.get('unit') or '点', int(row.get('unit_price') or 0), int(row.get('amount') or 0)) for row in details)
                if snapshots - source_values:
                    abort(409, description='元の買取明細と見積依頼の内容が一致しません。内容を確認してください。')
            self.check_items(cur, user_id, ids, exclude=(kind, document_id), lock=True,
                             allow_sold=kind != 'estimate' or source_id is not None, source_invoice_id=source_id)
            cur.execute(f"UPDATE {table} SET status='submitted', updated_at=CURRENT_TIMESTAMP WHERE id={self.mark} AND status='completed'", (document_id,))
            if cur.rowcount != 1:
                abort(409)
            # Same transaction as the status transition; no email or notification.
            cur.execute('INSERT INTO final_document_events (document_kind,document_id,action,actor_id,reason,before_status,after_status,metadata) VALUES (' + ','.join([self.mark]*8) + ')',
                        (table, document_id, 'customer_submit', user_id, '開花の書類受付へ送信', 'completed', 'submitted', json.dumps({'destination': 'kaika', 'channel': 'in_app'}, ensure_ascii=False)))
        return True

    def inbox(self):
        result = []
        with self.db() as (_, cur):
            for kind, (table, _, _, owner, _, admin_view, title) in KINDS.items():
                cur.execute(f"SELECT d.*, u.display_name AS customer_display_name FROM {table} d JOIN users u ON u.id=d.{owner} WHERE d.status='submitted' ORDER BY d.updated_at DESC, d.id DESC LIMIT 200")
                for row in self.rows(cur):
                    if not self.owned_customer_document(kind, row, row[owner]):
                        continue
                    result.append({'kind':kind, 'title':title, 'id':row['id'], 'number':row.get('document_no') or row.get('invoice_no'), 'customer':row.get('customer_display_name'), 'sent_at':row.get('updated_at'), 'url':url_for('admin_customer_submission_detail', kind=kind, document_id=row['id'])})
        return sorted(result, key=lambda row:str(row['sent_at'] or ''), reverse=True)


def register_document_workflow(runtime):
    app = runtime.app
    if 'kaika_document_workflow' in app.extensions:
        return app.extensions['kaika_document_workflow']
    service = DocumentWorkflow(runtime)
    app.extensions['kaika_document_workflow'] = service

    def form_wrapper(original, kind):
        @wraps(original)
        def guarded(*args, **kwargs):
            if request.method == 'POST' and current_user.is_authenticated:
                service.validate_form(kind, kwargs.get('id'))
            return original(*args, **kwargs)
        return guarded
    for endpoint, kind in FORMS.items():
        if endpoint in app.view_functions:
            app.view_functions[endpoint] = form_wrapper(app.view_functions[endpoint], kind)

    def csrf():
        supplied = request.form.get('security_csrf_token', '')
        expected = session.get('_security_csrf', '')
        if (session.get('_security_csrf_user') != str(current_user.get_id())
                or not isinstance(expected, str) or not isinstance(supplied, str)
                or not re.fullmatch(r'[A-Za-z0-9_-]{43}', supplied)
                or not re.fullmatch(r'[A-Za-z0-9_-]{43}', expected)
                or not hmac.compare_digest(expected, supplied)):
            abort(400, description='画面を開き直してから送信してください。')

    @login_required
    def submit(kind, document_id):
        if current_user.is_admin():
            abort(403)
        csrf()
        if request.form.get('destination') != 'kaika':
            abort(400, description='送信先は開花の書類受付です。')
        changed = service.submit(kind, document_id, current_user.id)
        flash('開花の書類受付へ送信しました。' if changed else 'この書類はすでに送信済みです。', 'success' if changed else 'info')
        return redirect(url_for(KINDS[kind][4], id=document_id))

    @login_required
    def inbox():
        if not current_user.is_admin() or not (getattr(current_user, 'role', '') == 'owner' or current_user.has_permission('invoices')):
            abort(403)
        return render_template('customer_documents/inbox.html', submissions=service.inbox())

    @login_required
    def received_detail(kind, document_id):
        if not current_user.is_admin() or not (getattr(current_user, 'role', '') == 'owner' or current_user.has_permission('invoices')):
            abort(403)
        if kind not in KINDS:
            abort(404)
        table, items, key, owner, *_ = KINDS[kind]
        with service.db() as (_, cur):
            cur.execute(f'SELECT d.*,u.display_name AS customer_display_name FROM {table} d JOIN users u ON u.id=d.{owner} WHERE d.id={service.mark}', (document_id,))
            doc = service.one(cur)
            if not doc or doc.get('status') not in SENT or not service.owned_customer_document(kind, doc, doc[owner]):
                abort(404)
            cur.execute(f'SELECT * FROM {items} WHERE {key}={service.mark} ORDER BY item_no,id', (document_id,))
            details = service.rows(cur)
        return render_template('customer_documents/received_detail.html', document=doc, details=details, title=KINDS[kind][6])

    @login_required
    def product_candidates():
        if current_user.is_admin():
            abort(403)
        with service.db() as (_, cur):
            available, _ = service.products(cur, current_user.id, include_sold=True)
        if request.args.get('sold_only') == '1':
            available = [row for row in available if row.get('sale_date')]
        elif request.args.get('inventory_only') == '1':
            available = [row for row in available if not row.get('sale_date')]
        days = request.args.get('sold_days', '')
        if days:
            if not days.isdigit() or not 0 <= int(days) <= 36600:
                abort(400)
            now = runtime.get_jst_now().date() if hasattr(runtime, 'get_jst_now') else date.today()
            cutoff = (now - timedelta(days=int(days))).isoformat()
            available = [row for row in available if row.get('sale_date') and str(row['sale_date'])[:10] >= cutoff]
        return jsonify(available)

    app.add_url_rule('/documents/submit/<kind>/<int:document_id>', endpoint='customer_document_submit', view_func=submit, methods=['POST'])
    app.add_url_rule('/admin/customer-submissions', endpoint='admin_customer_submissions', view_func=inbox, methods=['GET'])
    app.add_url_rule('/admin/customer-submissions/<kind>/<int:document_id>', endpoint='admin_customer_submission_detail', view_func=received_detail, methods=['GET'])
    app.add_url_rule('/api/document-products', endpoint='customer_document_products', view_func=product_candidates, methods=['GET'])

    # The old invoice sender must not bypass the same state and item checks.
    if 'user_invoice_send' in app.view_functions:
        @login_required
        def legacy_invoice_send(id):
            if request.method != 'POST':
                abort(405)
            if current_user.is_admin():
                abort(403)
            csrf()
            service.submit('purchase_statement', id, current_user.id)
            flash('開花の書類受付へ送信しました。', 'success')
            return redirect(url_for('user_invoice_view', id=id))
        app.view_functions['user_invoice_send'] = legacy_invoice_send

    def template_context(sender, template, context, **extra):
        if not current_user.is_authenticated:
            return
        context['customer_document_workflow_available'] = True
        if template.name == 'mitsumori_form.html' and not context.get('source_invoice'):
            doc = context.get('mitsumori') or {}
            with service.db() as (_, cur):
                available, history = service.products(cur, current_user.id, exclude=('estimate', doc['id']) if doc.get('id') else None)
            context.update(my_merchandise=available, document_product_history=history,
                           document_product_counts={'available':len(available), 'submitted':len(history)})
        for kind, key in (('estimate','mitsumori'), ('consent','kaitori'), ('calculation','keisan'), ('purchase_statement','invoice')):
            doc = context.get(key)
            if isinstance(doc, dict) and service.owned_customer_document(kind, doc, current_user.id) and not current_user.is_admin():
                context['document_delivery'] = {'kind':kind, 'id':doc['id'], 'title':KINDS[kind][6], 'status':doc.get('status') or 'draft', 'can_send':doc.get('status') == 'completed' and kind != 'purchase_statement', 'sent':doc.get('status') in SENT,
                    'hold_reason': '買取明細書は適用する契約と手数料計算の確認待ちです。作成内容を保存できますが、開花への送信は保留しています。' if kind == 'purchase_statement' else None}
    before_render_template.connect(template_context, app, weak=False)
    return service
