"""Inventory custody and receiving. Existing stock remains managed by Kaika.

Own inventory is never accepted into Kaika custody merely because a client
requests shipping. Receipt is an authenticated staff action with an audit log.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date
from functools import wraps
import hashlib
import json
import re
import uuid

from flask import abort, flash, g, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from werkzeug.exceptions import HTTPException

from self_inventory import _row, _truthy, _lock_submission

ACTIVE_INTAKES = ('requested', 'approved', 'shipped', 'received')
STATE_NAMES = {'requested': '開花の受付待ち', 'approved': '発送のご案内済み',
               'shipped': '開花へ配送中', 'received': '開花で受領・登録中',
               'completed': '開花での登録完了', 'cancelled': '取り消し済み'}


def custody_location(item):
    return dict(item or {}).get('custody_location') or 'kaika'


def custody_label(item):
    return {'self': '自己保管（自宅など）', 'transit': '開花へ配送中',
            'kaika': '開花で保管・管理'}.get(custody_location(item), '保管先を確認してください')


def ensure_schema(runtime):
    """Explicit local/deployment migration; never touch a DB on module import."""
    if getattr(runtime, '_inventory_custody_schema_ready', False):
        return
    pg = bool(runtime.DATABASE_URL)
    conn = runtime.get_db()
    cur = conn.cursor()
    try:
        if pg:
            cur.execute("SELECT pg_advisory_xact_lock(574920261)")
            cur.execute("SELECT column_name FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='merchandise'")
            columns = {next(iter(row.values())) if isinstance(row, dict) else row[0] for row in cur.fetchall()}
        else:
            cur.execute('BEGIN IMMEDIATE')
            cur.execute('PRAGMA table_info(merchandise)')
            columns = {row['name'] if hasattr(row, 'keys') else row[1] for row in cur.fetchall()}
        added = 'custody_location' not in columns
        for name, definition in [('custody_location', "VARCHAR(16) NOT NULL DEFAULT 'kaika'"),
                                 ('custody_version', 'INTEGER NOT NULL DEFAULT 1'),
                                 ('custody_received_at', 'TIMESTAMP')]:
            if name not in columns:
                cur.execute(f'ALTER TABLE merchandise ADD COLUMN {name} {definition}')
        # Only the dedicated previous self-registration path is inferred as own
        # stock. Records already involved in an active Kaika workflow keep the
        # existing classification and require staff review before migration.
        def exists(table):
            if pg:
                cur.execute('SELECT to_regclass(%s)', (table,))
                value = cur.fetchone()
                return bool(next(iter(value.values())) if isinstance(value, dict) else value[0])
            cur.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,))
            return cur.fetchone() is not None
        if added and exists('self_inventory_items'):
            filters = []
            for table in ('sale_requests', 'item_disposal_requests'):
                if exists(table):
                    filters.append(f"NOT EXISTS (SELECT 1 FROM {table} w WHERE w.merchandise_id=merchandise.id AND COALESCE(w.status,'pending') NOT IN ('cancelled','rejected','deal_failed'))")
            if exists('sales_agency_request_items') and exists('sales_agency_requests'):
                filters.append("NOT EXISTS (SELECT 1 FROM sales_agency_request_items wi JOIN sales_agency_requests w ON w.id=wi.request_id WHERE wi.merchandise_id=merchandise.id AND COALESCE(w.status,'pending') NOT IN ('cancelled','rejected','deal_failed'))")
            suffix = ''.join(' AND ' + clause for clause in filters)
            cur.execute("UPDATE merchandise SET custody_location='self' WHERE id IN (SELECT merchandise_id FROM self_inventory_items)" + suffix)
        pk = 'SERIAL PRIMARY KEY' if pg else 'INTEGER PRIMARY KEY AUTOINCREMENT'
        cur.execute(f'''CREATE TABLE IF NOT EXISTS inventory_intakes (
            id {pk}, user_id INTEGER NOT NULL REFERENCES users(id),
            kind VARCHAR(20) NOT NULL, status VARCHAR(20) NOT NULL DEFAULT 'requested',
            expected_count INTEGER NOT NULL, received_count INTEGER,
            client_note TEXT NOT NULL DEFAULT '', shipping_instructions TEXT NOT NULL DEFAULT '',
            carrier VARCHAR(100) NOT NULL DEFAULT '', tracking_number VARCHAR(100) NOT NULL DEFAULT '',
            submission_hash VARCHAR(64) NOT NULL UNIQUE, version INTEGER NOT NULL DEFAULT 1,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            received_at TIMESTAMP, received_by INTEGER REFERENCES users(id))''')
        cur.execute('''CREATE TABLE IF NOT EXISTS inventory_intake_items (
            intake_id INTEGER NOT NULL REFERENCES inventory_intakes(id),
            merchandise_id INTEGER NOT NULL REFERENCES merchandise(id),
            registration_key VARCHAR(64) UNIQUE,
            PRIMARY KEY (intake_id, merchandise_id))''')
        cur.execute(f'''CREATE TABLE IF NOT EXISTS inventory_custody_events (
            id {pk}, merchandise_id INTEGER REFERENCES merchandise(id),
            intake_id INTEGER REFERENCES inventory_intakes(id), user_id INTEGER NOT NULL REFERENCES users(id),
            actor_id INTEGER NOT NULL REFERENCES users(id), event_type VARCHAR(40) NOT NULL,
            details TEXT NOT NULL DEFAULT '', created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP)''')
        cur.execute('CREATE INDEX IF NOT EXISTS idx_inventory_intake_user_status ON inventory_intakes(user_id,status)')
        cur.execute('CREATE INDEX IF NOT EXISTS idx_inventory_intake_item ON inventory_intake_items(merchandise_id)')
        conn.commit()
        runtime._inventory_custody_schema_ready = True
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()


class InventoryCustody:
    def __init__(self, runtime):
        self.runtime = runtime
        self.app = runtime.app
        self.plans = self.app.extensions['kaika_feature_plans']
        self.pg = bool(runtime.DATABASE_URL)
        self.mark = '%s' if self.pg else '?'
        self.signer = URLSafeTimedSerializer(self.app.secret_key, salt='kaika-custody-v1')

    @contextmanager
    def db(self, write=False):
        conn = self.runtime.get_db()
        cur = conn.cursor()
        try:
            if write and not self.pg:
                cur.execute('BEGIN IMMEDIATE')
            yield conn, cur
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.close()
            conn.close()

    def require_user(self, feature=True):
        if current_user.role != 'user':
            abort(403)
        if feature and (not self.plans.has_feature('inventory_manage') or not current_user.can_edit_merchandise()):
            abort(403, description='この操作には在庫管理を利用できる契約が必要です。利用プランをご確認ください。')

    def guard_legacy_operations(self):
        """Kaika operations cannot modify goods which have not been received.

        An independent route guard also covers runtime-replaced legacy views.
        Custody only progresses towards Kaika, so a concurrent receipt cannot
        turn an allowed Kaika operation into an operation on self stock.
        """
        if not current_user.is_authenticated:
            return
        endpoint = request.endpoint or ''
        singles = {'submit_sale_request': 'item_id', 'edit_item': 'id',
                   'delete_item': 'id', 'admin_delete_item': 'id',
                   'admin_transfer_item': 'id',
                   'admin_proxy_service_toggle_item': 'item_id',
                   'admin_proxy_service_toggle_item_quick': 'item_id'}
        batches = {'sales_agency_apply': 'merchandise_ids',
                   'submit_disposal_request': 'merchandise_ids[]',
                   'submit_long_term_disposal_request': 'merchandise_ids',
                   'admin_transfer_items_bulk': 'item_ids',
                   'admin_proxy_service_bulk_toggle': 'item_ids'}
        if endpoint in singles:
            ids = [(request.view_args or {}).get(singles[endpoint])]
        elif endpoint in batches and request.method == 'POST':
            payload = request.get_json(silent=True) if request.is_json else None
            if endpoint == 'admin_proxy_service_bulk_toggle' and payload is None and not request.form:
                payload = request.get_json(silent=True, force=True)
            if endpoint == 'admin_proxy_service_bulk_toggle' and isinstance(payload, dict) and 'item_ids' in payload and not isinstance(payload['item_ids'], list):
                abort(400, description='商品IDは配列で指定してください。')
            ids = payload.get(batches[endpoint], []) if isinstance(payload, dict) else request.form.getlist(batches[endpoint])
            if endpoint == 'admin_proxy_service_bulk_toggle' and not ids and isinstance(payload, dict):
                entries = payload.get('items', [])
                if not isinstance(entries, list) or any(not isinstance(entry, dict) for entry in entries):
                    abort(400)
                ids = [entry.get('id') for entry in entries]
            if isinstance(ids, str):
                ids = ids.split(',')
        else:
            return
        if not isinstance(ids, (list, tuple)) or len(ids) > 1000:
            abort(400)
        if not ids:
            return
        if any(not re.fullmatch(r'\d{1,10}', str(value or '')) for value in ids):
            abort(400)
        with self.db() as (_, cur):
            cur.execute(f"SELECT id,user_id,custody_location FROM merchandise WHERE id IN ({','.join([self.mark]*len(ids))})", tuple(int(value) for value in ids))
            rows = [_row(row,cur) for row in cur.fetchall()]
        for item in rows:
            if not current_user.is_admin() and item['user_id'] != current_user.id:
                abort(404)
            if custody_location(item) != 'kaika':
                abort(409, description='自己保管・配送中の商品には開花の作業を依頼できません。自己販売は商品詳細の「自分の販売を記録」、預け入れは「開花へ預ける」をご利用ください。')

    def token(self, purpose, target=None, version=None):
        return self.signer.dumps({'user': current_user.id, 'purpose': purpose,
                                  'target': target, 'version': version, 'nonce': uuid.uuid4().hex})

    def verify(self, purpose, target=None):
        self.plans.check_csrf()
        try:
            data = self.signer.loads(request.form.get('operation_token', ''), max_age=4*60*60)
        except (BadSignature, SignatureExpired):
            abort(400, description='画面の有効期限が切れました。開き直してください。')
        if not isinstance(data, dict) or (data.get('user'), data.get('purpose'), data.get('target')) != (current_user.id, purpose, target):
            abort(400)
        return data

    def version(self, row, token, key='version'):
        if row[key] != token.get('version'):
            abort(409, description='別の操作で更新されています。画面を開き直してください。')

    def event(self, cur, event_type, *, item=None, intake=None, user_id, details=''):
        cur.execute(f'''INSERT INTO inventory_custody_events
            (merchandise_id,intake_id,user_id,actor_id,event_type,details)
            VALUES ({','.join([self.mark]*6)})''',
            (item, intake, user_id, current_user.id, event_type, details))

    def notify(self, intake):
        push = self.app.extensions.get('push_notifications')
        if push:
            try:
                push.enqueue(intake['user_id'], f"intake:{intake['id']}:{intake['version']}",
                             f"/inventory/intakes/{intake['id']}", event_type='inventory')
            except Exception:
                self.app.logger.exception('Could not queue inventory intake notification')

    def intake(self, cur, intake_id, lock=False):
        suffix = ' FOR UPDATE' if lock and self.pg else ''
        cur.execute(f'SELECT * FROM inventory_intakes WHERE id={self.mark}{suffix}', (intake_id,))
        row = _row(cur.fetchone(), cur)
        if not row or (not current_user.is_admin() and row['user_id'] != current_user.id):
            abort(404)
        return row

    def active_for_item(self, cur, item_id):
        cur.execute(f'''SELECT i.* FROM inventory_intakes i
            JOIN inventory_intake_items ii ON ii.intake_id=i.id
            WHERE ii.merchandise_id={self.mark} AND i.status IN ('requested','approved','shipped','received')
            ORDER BY i.id DESC LIMIT 1''', (item_id,))
        return _row(cur.fetchone(), cur)

    def own_item(self, cur, item_id, lock=False):
        suffix = ' FOR UPDATE' if lock and self.pg else ''
        cur.execute(f'SELECT * FROM merchandise WHERE id={self.mark} AND user_id={self.mark} AND scope=\'user\'{suffix}', (item_id, current_user.id))
        item = _row(cur.fetchone(), cur)
        if not item:
            abort(404)
        if custody_location(item) != 'self' or self.active_for_item(cur, item_id):
            abort(409, description='開花への依頼中・配送中・開花保管の商品です。受付内容をご確認ください。')
        # Do not mutate goods concurrently referenced by existing workflows.
        helper = self.app.extensions.get('kaika_self_inventory')
        if helper:
            stripped = dict(item)
            for key in ('sale_date', 'sale_price', 'shipping_cost', 'commission', 'sales_destination', 'is_shipped', 'item_status'):
                stripped[key] = None
            if helper.locked(cur, stripped):
                abort(409, description='開花側で進行中の処理があるため変更できません。')
        return item

    def item_context(self, item):
        item = dict(item)
        location = custody_location(item)
        active = None
        if location != 'kaika' and current_user.is_authenticated:
            cache = getattr(g, '_custody_intake_cache', None)
            if cache is None:
                with self.db() as (_, cur):
                    condition = '' if current_user.is_admin() else f' AND i.user_id={self.mark}'
                    cur.execute("SELECT i.id,i.status,ii.merchandise_id FROM inventory_intakes i JOIN inventory_intake_items ii ON ii.intake_id=i.id WHERE i.status IN ('requested','approved','shipped','received')" + condition,
                                () if current_user.is_admin() else (current_user.id,))
                    cache = {row['merchandise_id']: row for row in (_row(r,cur) for r in cur.fetchall())}
                g._custody_intake_cache = cache
            active = cache.get(item.get('id'))
        return {'location': location, 'label': custody_label(item), 'intake': active,
                'can_self_manage': location == 'self' and not active and not current_user.is_admin()
                                   and self.plans.has_feature('inventory_manage') and current_user.can_edit_merchandise()}

    @staticmethod
    def text(name, maximum, required=False):
        value = request.form.get(name, '').strip()
        if '\x00' in value or len(value) > maximum or (required and not value):
            abort(400, description='入力内容の長さと必須項目をご確認ください。')
        return value

    @staticmethod
    def number(name, maximum=1_000_000_000, minimum=0):
        value = request.form.get(name, '').strip()
        if not re.fullmatch(r'\d{1,10}', value) or not minimum <= int(value) <= maximum:
            abort(400, description='数量・金額は指定範囲の整数で入力してください。')
        return int(value)

    def sale(self, item_id):
        self.require_user()
        if request.method == 'GET':
            with self.db() as (_, cur):
                item = self.own_item(cur, item_id)
            return render_template('self_inventory_sale.html', item=item,
                                   operation_token=self.token('sale', item_id, item['custody_version']),
                                   csrf_token_value=self.plans.csrf_token(), today=self.runtime.get_jst_now().date().isoformat())
        token = self.verify('sale', item_id)
        with self.db(write=True) as (_, cur):
            item = self.own_item(cur, item_id, lock=True)
            self.version(item, token, 'custody_version')
            action = request.form.get('action')
            if action == 'listing':
                if item.get('sale_date'):
                    abort(409)
                updates = {'is_listed': not _truthy(item.get('is_listed'))}
            elif action == 'cancel_sale':
                if not item.get('sale_date'):
                    abort(409)
                if not self.text('correction_reason', 500, required=True):
                    abort(400)
                updates = {'sale_date': None, 'sale_price': 0, 'shipping_cost': 0,
                           'commission': 0, 'other_cost': 0, 'sales_destination': '', 'is_shipped': False, 'is_listed': False}
            elif action == 'sale':
                raw_date = self.text('sale_date', 10, required=True)
                try:
                    sold_on = date.fromisoformat(raw_date)
                except ValueError:
                    abort(400, description='売却日を正しく入力してください。')
                if sold_on > self.runtime.get_jst_now().date():
                    abort(400, description='売却日には今日以前の日付を入力してください。')
                updates = {'sale_date': raw_date, 'sale_price': self.number('sale_price'),
                           'shipping_cost': self.number('shipping_cost'), 'commission': self.number('commission'),
                           'sales_destination': self.text('sales_destination', 100),
                           'is_shipped': request.form.get('is_shipped') == '1', 'is_listed': False}
                updates['other_cost'] = self.number('other_cost') if 'other_cost' in request.form else (item.get('other_cost') or 0)
                if item.get('sale_date'):
                    self.text('correction_reason', 500, required=True)
            else:
                abort(400)
            before = {key: item.get(key) for key in updates}
            assignments = ','.join(f'{key}={self.mark}' for key in updates)
            cur.execute(f'UPDATE merchandise SET {assignments},custody_version=custody_version+1,updated_by={self.mark},updated_at=CURRENT_TIMESTAMP WHERE id={self.mark}', (*updates.values(), current_user.id, item_id))
            self.event(cur, action, item=item_id, user_id=current_user.id,
                       details=json.dumps({'before':before,'after':updates,'reason':request.form.get('correction_reason','')},ensure_ascii=False,default=str))
        flash('ご自身の販売記録を保存しました。開花への作業依頼は発生しません。', 'success')
        return redirect(url_for('view_item', id=item_id))

    def new_intake(self):
        self.require_user()
        helper = self.app.extensions.get('kaika_self_inventory')
        with self.db() as (_, cur):
            cur.execute(f"SELECT * FROM merchandise WHERE user_id={self.mark} AND scope='user' AND custody_location='self' AND sale_date IS NULL ORDER BY id DESC", (current_user.id,))
            items = [_row(row,cur) for row in cur.fetchall()]
            items = [item for item in items if not self.active_for_item(cur,item['id']) and not helper.locked(cur,item)] if helper else []
        if request.method == 'GET':
            return render_template('inventory_intake_new.html', items=items, selected_id=request.args.get('item_id',type=int),
                                   operation_token=self.token('new_intake'), csrf_token_value=self.plans.csrf_token())
        token = self.verify('new_intake')
        kind = request.form.get('kind')
        if kind not in ('transfer','registration'):
            abort(400)
        note = self.text('client_note',2000)
        ids = request.form.getlist('item_ids')
        if kind == 'transfer':
            if not ids or len(ids)>100 or any(not re.fullmatch(r'\d{1,10}',v) for v in ids) or len(set(ids))!=len(ids):
                abort(400,description='預ける商品を1〜100点選択してください。')
            ids = sorted(int(v) for v in ids)
            expected = len(ids)
        else:
            ids = []
            expected = self.number('expected_count',1000,1)
        submission = hashlib.sha256(token['nonce'].encode()).hexdigest()
        with self.db(write=True) as (_,cur):
            if self.pg:
                _lock_submission(cur, 'kaika-inventory-intake-create-v1', submission)
            cur.execute(f'SELECT id FROM inventory_intakes WHERE submission_hash={self.mark} AND user_id={self.mark}',(submission,current_user.id))
            previous = _row(cur.fetchone(),cur)
            if previous:
                return redirect(url_for('inventory_intake_detail',intake_id=previous['id']))
            for item_id in ids:
                item = self.own_item(cur,item_id,lock=True)
                if item.get('sale_date') or _truthy(item.get('is_shipped')) or (helper and helper.locked(cur,item)):
                    abort(409,description='売却済み・依頼中の商品は預け入れできません。')
            ending=' RETURNING id' if self.pg else ''
            cur.execute(f'INSERT INTO inventory_intakes (user_id,kind,expected_count,client_note,submission_hash) VALUES ({",".join([self.mark]*5)}){ending}',
                        (current_user.id,kind,expected,note,submission))
            intake_id=_row(cur.fetchone(),cur)['id'] if self.pg else cur.lastrowid
            for item_id in ids:
                cur.execute(f'INSERT INTO inventory_intake_items (intake_id,merchandise_id) VALUES ({self.mark},{self.mark})',(intake_id,item_id))
                cur.execute(f'UPDATE merchandise SET custody_version=custody_version+1 WHERE id={self.mark}',(item_id,))
            self.event(cur,'intake_requested',intake=intake_id,user_id=current_user.id)
        flash('受付を作成しました。開花から発送先・条件の案内が届くまで商品を発送せずにお待ちください。','success')
        return redirect(url_for('inventory_intake_detail',intake_id=intake_id))

    def intake_list(self):
        if not current_user.is_admin():
            self.require_user(feature=False)
        with self.db() as (_,cur):
            condition='' if current_user.is_admin() else f' WHERE i.user_id={self.mark}'
            cur.execute('SELECT i.*,u.display_name,u.username FROM inventory_intakes i JOIN users u ON u.id=i.user_id'+condition+' ORDER BY i.id DESC',
                        () if current_user.is_admin() else (current_user.id,))
            rows=[_row(r,cur) for r in cur.fetchall()]
        return render_template('inventory_intakes.html',intakes=rows,state_names=STATE_NAMES)

    def detail(self,intake_id):
        if not current_user.is_admin():
            self.require_user(feature=False)
        if request.method=='POST':
            token=self.verify('intake',intake_id)
            with self.db(write=True) as (_,cur):
                intake=self.intake(cur,intake_id,lock=True)
                self.version(intake,token)
                action=request.form.get('action')
                state=intake['status']
                staff=current_user.is_admin()
                if action=='cancel' and not staff and state in ('requested','approved'):
                    updates={'status':'cancelled'}
                elif action=='approve' and staff and state=='requested':
                    updates={'status':'approved','shipping_instructions':self.text('shipping_instructions',3000,required=True)}
                elif action=='ship' and not staff and state=='approved':
                    updates={'status':'shipped','carrier':self.text('carrier',100,required=True),'tracking_number':self.text('tracking_number',100,required=True)}
                elif action=='receive' and staff and state in ('approved','shipped'):
                    count=self.number('received_count',1000,1)
                    if intake['kind']=='transfer' and count!=intake['expected_count']:
                        abort(409,description='預け入れ対象の全商品を確認後に受領してください。不足・相違は問い合わせで確認してください。')
                    updates={'status':'completed' if intake['kind']=='transfer' else 'received',
                             'received_count':count,'received_by':current_user.id,'received_at':self.runtime.get_jst_now()}
                elif action=='complete' and staff and state=='received' and intake['kind']=='registration':
                    cur.execute(f'SELECT COUNT(*) AS n FROM inventory_intake_items WHERE intake_id={self.mark}',(intake_id,))
                    if _row(cur.fetchone(),cur)['n']!=intake['received_count']:
                        abort(409,description='受領した点数と登録済みの商品数を合わせてください。')
                    updates={'status':'completed'}
                else:
                    abort(409,description='現在の受付状況ではこの操作はできません。')
                cur.execute(f'SELECT merchandise_id FROM inventory_intake_items WHERE intake_id={self.mark} ORDER BY merchandise_id',(intake_id,))
                ids=[_row(r,cur)['merchandise_id'] for r in cur.fetchall()]
                for item_id in ids:
                    suffix=' FOR UPDATE' if self.pg else ''
                    cur.execute(f'SELECT * FROM merchandise WHERE id={self.mark}{suffix}',(item_id,))
                    item=_row(cur.fetchone(),cur)
                    if not item or item['user_id']!=intake['user_id']:
                        abort(409,description='商品の所有者が変更されています。管理者が確認してください。')
                    if action in ('ship','receive') and intake['kind']=='transfer':
                        expected_location='self' if action=='ship' or state=='approved' else 'transit'
                        if custody_location(item)!=expected_location or item.get('sale_date'):
                            abort(409,description='商品の状態が変わっています。受領対象を確認してください。')
                        cur.execute(f'UPDATE merchandise SET custody_location={self.mark},custody_version=custody_version+1,updated_by={self.mark},updated_at=CURRENT_TIMESTAMP WHERE id={self.mark}',
                                    ('transit' if action=='ship' else 'kaika',current_user.id,item_id))
                        if action=='receive':
                            cur.execute(f'UPDATE merchandise SET storage_start_date={self.mark},custody_received_at={self.mark},is_listed={self.mark} WHERE id={self.mark}',(self.runtime.get_jst_now().date().isoformat(),self.runtime.get_jst_now(),False,item_id))
                    elif action=='cancel':
                        cur.execute(f'UPDATE merchandise SET custody_version=custody_version+1 WHERE id={self.mark}',(item_id,))
                assignments=','.join(f'{key}={self.mark}' for key in updates)
                cur.execute(f'UPDATE inventory_intakes SET {assignments},version=version+1,updated_at=CURRENT_TIMESTAMP WHERE id={self.mark}',(*updates.values(),intake_id))
                self.event(cur,'intake_'+action,intake=intake_id,user_id=intake['user_id'])
                intake.update(updates)
                intake['version']+=1
            if staff:
                self.notify(intake)
            flash('受付状況を更新しました。','success')
            return redirect(url_for('inventory_intake_detail',intake_id=intake_id))
        with self.db() as (_,cur):
            intake=self.intake(cur,intake_id)
            cur.execute(f'SELECT m.* FROM merchandise m JOIN inventory_intake_items ii ON ii.merchandise_id=m.id WHERE ii.intake_id={self.mark} ORDER BY m.id',(intake_id,))
            items=[_row(r,cur) for r in cur.fetchall()]
            cur.execute(f'SELECT e.*,u.display_name,u.username FROM inventory_custody_events e JOIN users u ON u.id=e.actor_id WHERE e.intake_id={self.mark} ORDER BY e.id',(intake_id,))
            events=[_row(r,cur) for r in cur.fetchall()]
            cur.execute(f'SELECT display_name,username FROM users WHERE id={self.mark}',(intake['user_id'],))
            owner=_row(cur.fetchone(),cur)
        return render_template('inventory_intake_detail.html',intake=intake,items=items,events=events,owner=owner,state_names=STATE_NAMES,
                               operation_token=self.token('intake',intake_id,intake['version']),csrf_token_value=self.plans.csrf_token())

    def register_received(self,intake_id):
        if not current_user.is_admin():
            abort(403)
        helper=self.app.extensions.get('kaika_self_inventory')
        if not helper:
            abort(503)
        created=[]
        with self.db() as (_,cur):
            intake=self.intake(cur,intake_id)
        if intake['kind']!='registration' or intake['status']!='received':
            abort(409,description='開花での受領確認後に登録できます。')
        if request.method=='GET':
            return render_template('inventory_received_form.html',intake=intake,
                                   operation_token=self.token('register_received',intake_id,intake['version']),csrf_token_value=self.plans.csrf_token())
        if request.content_length and request.content_length>40*1024*1024:
            abort(413)
        token=self.verify('register_received',intake_id)
        try:
            values={key:self.text(key,limit,required=key=='product_name') for key,limit in
                    [('product_name',200),('brand_name',100),('model_number',100),('item_condition',10),('store_name',200),('supplier_detail',50),('payment_method',50),('notes',10000)]}
            values.update({key:self.number(key) for key in ('purchase_price','listing_price','expected_shipping','expected_commission')})
            raw_date=self.text('purchase_date',10)
            if raw_date:
                try: date.fromisoformat(raw_date)
                except ValueError: abort(400,description='仕入日をご確認ください。')
            values['purchase_date']=raw_date or None
            with self.db(write=True) as (_,cur):
                intake=self.intake(cur,intake_id,lock=True)
                registration_key=hashlib.sha256(token['nonce'].encode()).hexdigest()
                cur.execute(f'SELECT merchandise_id FROM inventory_intake_items WHERE registration_key={self.mark}',(registration_key,))
                previous=_row(cur.fetchone(),cur)
                if previous:
                    return redirect(url_for('inventory_intake_detail',intake_id=intake_id))
                self.version(intake,token)
                if intake['status']!='received': abort(409)
                cur.execute(f'SELECT COUNT(*) AS n FROM inventory_intake_items WHERE intake_id={self.mark}',(intake_id,))
                if _row(cur.fetchone(),cur)['n']>=intake['received_count']:
                    abort(409,description='受領数まで登録済みです。内容を確認して登録完了にしてください。')
                values.update(helper.save_photos(None,created))
                values.update(user_id=intake['user_id'],scope='user',custody_location='kaika',
                              wholesale_price=0,wholesale_fee_rate=0,sale_type='normal',is_listed=False,
                              is_shipped=False,sale_price=0,shipping_cost=0,commission=0,
                              storage_start_date=str(intake['received_at'])[:10],custody_received_at=intake['received_at'],updated_by=current_user.id)
                ending=' RETURNING id' if self.pg else ''
                cur.execute(f'INSERT INTO merchandise ({",".join(values)}) VALUES ({",".join([self.mark]*len(values))}){ending}',tuple(values.values()))
                item_id=_row(cur.fetchone(),cur)['id'] if self.pg else cur.lastrowid
                cur.execute(f'INSERT INTO inventory_intake_items (intake_id,merchandise_id,registration_key) VALUES ({self.mark},{self.mark},{self.mark})',(intake_id,item_id,registration_key))
                cur.execute(f'UPDATE inventory_intakes SET version=version+1,updated_at=CURRENT_TIMESTAMP WHERE id={self.mark}',(intake_id,))
                self.event(cur,'registered_by_kaika',item=item_id,intake=intake_id,user_id=intake['user_id'])
            flash('開花保管の商品として登録しました。','success')
            return redirect(url_for('inventory_intake_detail',intake_id=intake_id))
        except Exception as error:
            if created:
                self.runtime.remove_uploaded_relative_paths(created)
            if isinstance(error, ValueError):
                abort(400, description=str(error))
            raise


def register_inventory_custody(runtime):
    app=runtime.app
    if 'inventory_custody' in app.extensions:
        return app.extensions['inventory_custody']
    ensure_schema(runtime)
    service=InventoryCustody(runtime)
    app.extensions['inventory_custody']=service
    app.before_request(service.guard_legacy_operations)
    app.jinja_env.globals.update(custody_context=service.item_context,custody_location=custody_location,custody_label=custody_label)
    runtime.inventory_custody=service
    def recoverable(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            try:
                return view(*args, **kwargs)
            except HTTPException as error:
                if error.code not in (400, 403, 404, 409, 413):
                    raise
                messages = {400:'入力内容と画面の有効期限をご確認ください。',
                            403:'この操作を利用できません。利用プランと受付状況をご確認ください。',
                            404:'対象の受付・商品は見つかりません。ご自身の一覧から開き直してください。',
                            409:'商品の状態が更新されています。画面を開き直して最新の状況をご確認ください。',
                            413:'写真の合計サイズを40MB以下にして、もう一度選択してください。'}
                detail = str(error.description or '')
                # Default Werkzeug English messages add no useful guidance.
                if not any(ord(char)>127 for char in detail):
                    detail = messages[error.code]
                return render_template('inventory_custody_error.html', message=detail,
                                       retry_path=request.path, error_code=error.code), error.code
        return wrapped
    for rule,endpoint,view,methods in [
        ('/inventory/self/<int:item_id>/sale','self_inventory_sale',service.sale,['GET','POST']),
        ('/inventory/intakes/new','inventory_intake_new',service.new_intake,['GET','POST']),
        ('/inventory/intakes','inventory_intakes',service.intake_list,['GET']),
        ('/inventory/intakes/<int:intake_id>','inventory_intake_detail',service.detail,['GET','POST']),
        ('/inventory/intakes/<int:intake_id>/register','inventory_received_register',service.register_received,['GET','POST']),
    ]:
        app.add_url_rule(rule,endpoint,login_required(recoverable(view)),methods=methods)
    return service
