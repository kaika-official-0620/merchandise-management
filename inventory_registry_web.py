"""Stage 2 opt-in screens. No migration, customer scan or I/O on registration.

The existing self-registration/intake flows remain the entry points. This
module adds an authorized view of physical items, real work and reconciliation.
There is no invoice/payment/notification/disposal executor here.
"""
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal, InvalidOperation
from functools import wraps
import hashlib
import hmac
import json
import os
import secrets
from zoneinfo import ZoneInfo

from flask import Blueprint, abort, flash, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from werkzeug.exceptions import HTTPException


JST = ZoneInfo('Asia/Tokyo')
MAX_IMPORT_BYTES = 5 * 1024 * 1024
EVENT_LABELS = {
    'inspection_completed': '検品確認', 'authenticity_checked': '真贋確認の結果', 'photographed': '撮影完了',
    'registration_completed': '撮影・登録完了', 'approval_pending': '出品承認待ち',
    'listed': '出品記録', 'returned': '返品による再入庫', 'return_shipped': '顧客へ返送', 'return_received': 'お客様の返送受取確認',
    'hold': '検品差異・真贋疑義などの保留', 'release_hold': '根拠確認後の保留解除',
}
EVENT_DISPLAY_LABELS = {**EVENT_LABELS, 'expected':'預け入れ予定を記録', 'received':'開花で受領',
    'sold':'売却を記録', 'shipped':'販売商品の発送', 'sale_cancelled':'発送前の売却取消'}
WORK_LABELS = {
    'receipt': '受領確認', 'inspection': '検品', 'authenticity_check': '真贋確認',
    'photography': '撮影', 'registration': '商品登録', 'repricing': '価格変更',
    'packing': '梱包', 'dispatch': '発送手配', 'support': '問い合わせ対応',
    'return': '返品対応', 'other': 'その他',
}
STATE_LABELS = {
    'unknown': '未確認', 'unreviewed': '確認前', 'expected': '到着待ち',
    'with_customer': 'お客様が保管', 'received': '受領済み', 'dispatched': '発送済み',
    'return_in_transit': 'お客様へ返送中', 'unlisted': '未出品', 'listed': '出品中',
    'sold': '売却済み', 'shipped': '発送済み', 'inspected': '検品済み',
    'photographed': '撮影済み', 'registered': '登録完了', 'approval_pending': '承認待ち',
}


def enabled(app):
    return app.config.get('STAGE2_INVENTORY_ENABLED') is True


def _staff():
    return (current_user.is_authenticated and current_user.is_admin()
            and current_user.has_permission('merchandise'))


def _time(raw):
    try:
        value = datetime.fromisoformat(raw)
        return value.replace(tzinfo=JST) if value.tzinfo is None else value
    except (TypeError, ValueError):
        abort(400, description='日時を日本時間で入力してください。')


def _text(name, maximum=1000, required=False):
    value = request.form.get(name, '').strip()
    if len(value) > maximum or (required and not value):
        abort(400, description='入力内容の長さ、または必須項目を確認してください。')
    return value


def _display_time(value):
    # Stored dates without an offset remain unknown; do not invent a timezone.
    try:
        instant = datetime.fromisoformat(value) if isinstance(value, str) else value
        if not isinstance(instant, datetime) or instant.tzinfo is None:
            return '未確認'
        return instant.astimezone(JST).strftime('%Y/%m/%d %H:%M')
    except (ValueError, TypeError, OverflowError):
        return '未確認'


class RegistryWeb:
    def __init__(self, runtime):
        self.runtime, self.app = runtime, runtime.app
        self.dialect = 'postgres' if runtime.DATABASE_URL else 'sqlite'
        self.signer = URLSafeTimedSerializer(self.app.secret_key, salt='kaika-stage2-operations-v1')

    @contextmanager
    def db(self, write=False):
        from item_registry import Registry
        conn = self.runtime.get_db()
        try:
            if self.dialect == 'sqlite':
                conn.execute('PRAGMA foreign_keys=ON')
                if write:
                    conn.execute('BEGIN IMMEDIATE')
            registry = Registry(conn, dialect=self.dialect)
            yield registry
            if write:
                conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def actor(self):
        from item_registry import Actor
        return Actor(int(current_user.get_id()), staff=_staff())

    def csrf(self):
        actor = str(current_user.get_id())
        if session.get('_registry_actor') != actor or not session.get('_registry_csrf'):
            session['_registry_actor'] = actor
            session['_registry_csrf'] = secrets.token_urlsafe(32)
        return session['_registry_csrf']

    def operation(self, purpose, target=''):
        return self.signer.dumps(dict(actor=str(current_user.get_id()), purpose=purpose,
                                      target=str(target), issued_at=datetime.now(JST).isoformat(), nonce=secrets.token_hex(16)))

    def verify(self, purpose, target=''):
        expected = session.get('_registry_csrf', '')
        actual = request.form.get('registry_csrf', '')
        if (not expected or session.get('_registry_actor') != str(current_user.get_id())
                or not hmac.compare_digest(expected, actual)):
            abort(400, description='画面を開き直してから操作してください。')
        token = request.form.get('operation_token', '')
        try:
            payload = self.signer.loads(token, max_age=86400)
        except (BadSignature, SignatureExpired):
            abort(400, description='操作用の確認情報が失効しました。画面を開き直してください。')
        if (payload.get('actor') != str(current_user.get_id()) or payload.get('purpose') != purpose
                or payload.get('target') != str(target)):
            abort(403)
        return 'http:' + hashlib.sha256(token.encode()).hexdigest()

    def customers(self, registry):
        cur = registry.connection.cursor()
        try:
            cur.execute("SELECT id,display_name,username FROM users WHERE role='user' ORDER BY id")
            names = [c[0] for c in cur.description]
            return [dict(row) if hasattr(row, 'keys') else dict(zip(names, row)) for row in cur.fetchall()]
        finally:
            cur.close()


def register_inventory_registry(runtime):
    app = runtime.app
    if 'inventory_registry' in app.blueprints:
        return app.extensions['inventory_registry']
    app.config.setdefault('STAGE2_INVENTORY_ENABLED', os.environ.get('STAGE2_INVENTORY_ENABLED') == '1')
    app.config.setdefault('STAGE2_FREE_LIMIT_PROPOSAL_ENABLED', os.environ.get('STAGE2_FREE_LIMIT_PROPOSAL_ENABLED') == '1')
    service = RegistryWeb(runtime)
    bp = Blueprint('inventory_registry', __name__)

    def recover_form(item_id, message, status):
        action = {'inventory_registry.event':'event', 'inventory_registry.work':'work'}.get(request.endpoint)
        if request.method != 'POST' or not action or status not in (400,409) or not item_id:
            return None
        # Keep only bounded fields from this operation in the response. Never
        # persist drafts, reuse a failed operation token or include other forms.
        allowed_fields = ('kind','occurred_at','authenticity_result','note') if action == 'event' else (
            'kind','performed_at','duration_minutes','note','rework_of','rework_reason')
        values = {name:request.form.get(name,'')[:2000] for name in allowed_fields}
        from item_registry import RegistryError
        try:
            return render_item_detail(item_id, form_action=action, form_values=values,
                form_error=message), status
        except RegistryError:
            # The target may have disappeared or changed owner while the form
            # was open. A failed recovery read must not bypass its authorization.
            return None

    def allowed(staff=False):
        def decorate(view):
            @wraps(view)
            @login_required
            def inner(*args, **kwargs):
                if not enabled(app):
                    abort(404)
                if staff and not _staff():
                    abort(403)
                if not _staff() and current_user.role != 'user':
                    abort(403)
                try:
                    return view(*args, **kwargs)
                except HTTPException as exc:
                    recovered = recover_form(kwargs.get('item_id'), exc.description, exc.code)
                    if recovered is not None:
                        return recovered
                    raise
                except PermissionError:
                    abort(403)
                except KeyError:
                    abort(404)
                except ValueError as exc:
                    # Domain validation only. Database/provider exceptions are not echoed.
                    status = getattr(exc, 'status', None)
                    if status not in (400, 403, 404, 409):
                        status = {'staff_required': 403, 'batch_not_found': 404}.get(getattr(exc, 'code', ''), 409)
                    messages = {'invalid_state_transition':'現在の現物・販売状態と操作の順番が一致しません。記録の種類を確認してください。',
                        'invalid_workflow_transition':'撮影・登録などの確認順を確認してください。',
                        'event_time_before_current':'前回の状態記録より前の日時です。実際の日時と操作の順番を確認してください。',
                        'physical_work_requires_item_present':'現物が発送・返送中です。実作業の日時と商品の所在を確認してください。',
                        'item_on_hold':'この商品は保留中です。確認結果を残して保留を解除してから進めてください。',
                        'rework_item_mismatch':'元の作業がこの商品・所有者に対応していません。作業IDを確認してください。'}
                    message = messages.get(getattr(exc,'code',''),str(exc))
                    if getattr(exc,'code','') == 'idempotency_conflict':
                        return render_template('inventory_registry/error.html',
                            message='この操作番号は別の内容で処理済みです。履歴を確認してください。訂正を新しい作業として重複登録しないでください。'), status
                    recovered = recover_form(kwargs.get('item_id'), message, status)
                    if recovered is not None:
                        return recovered
                    return render_template('inventory_registry/error.html', message=message), status
            return inner
        return decorate

    @bp.get('/inventory/items')
    @allowed()
    def index():
        with service.db() as registry:
            items = registry.list_items(service.actor())
        return render_template('inventory_registry/index.html', items=items, staff=_staff())

    @bp.get('/inventory/items/<item_id>')
    @allowed()
    def detail(item_id):
        return render_item_detail(item_id)

    def render_item_detail(item_id, **extra):
        with service.db() as registry:
            item = registry.detail(service.actor(), item_id)
            from stage2_self_management import require_schema
            cur = registry.connection.cursor()
            try:
                require_schema(cur, service.dialect == 'postgres')
            finally:
                cur.close()
            # Count only this physical item's current owner's photo references.
            # No photo bytes, historical owner's paths or guessed sizes are exposed.
            photo_rows = registry.query('''SELECT p.byte_size,p.measurement_status FROM stage2_photo_records p
                JOIN business_item_links l ON l.merchandise_id=p.merchandise_id
                WHERE l.physical_item_id=? AND p.owner_user_id=? AND p.removed_from_item_at IS NULL''',
                (item_id, item['owner_user_id']))
            photos = {'count': len(photo_rows),
                      'known_bytes': sum(row['byte_size'] or 0 for row in photo_rows),
                      'unknown_count': sum(row['measurement_status'] != 'measured' for row in photo_rows)}
        return render_template('inventory_registry/detail.html', item=item, staff=_staff(),
                               event_labels=EVENT_LABELS, work_labels=WORK_LABELS, photos=photos,
                               now_local=datetime.now(JST).strftime('%Y-%m-%dT%H:%M:%S.%f'), **extra)

    @bp.post('/inventory/items/<item_id>/events')
    @allowed(staff=True)
    def event(item_id):
        key = service.verify('event', item_id)
        kind = _text('kind', 40, True)
        if kind not in EVENT_LABELS:
            abort(400)
        note = _text('note', 2000, True)
        details = {'note': note}
        if kind == 'authenticity_checked':
            details['authenticity_result'] = _text('authenticity_result', 20, True)
        if kind == 'returned':
            details['return_reason'] = note
        with service.db(write=True) as registry:
            when = _time(request.form.get('occurred_at'))
            if kind == 'return_received':
                from stage2_registry_hooks import record_return_received
                record_return_received(registry.connection, runtime, item_id, occurred_at=when,
                                       source_key=key, evidence_reference=note, details=details)
            else:
                registry.record_event(service.actor(), item_id, kind, occurred_at=when,
                                      source_key=key, evidence_reference=note, details=details)
        flash('商品の状態を記録しました。料金は発生させていません。', 'success')
        return redirect(url_for('inventory_registry.detail', item_id=item_id))

    @bp.post('/inventory/items/<item_id>/deadline')
    @allowed(staff=True)
    def deadline(item_id):
        key = service.verify('deadline', item_id)
        if request.form.get('contract_checked') != 'yes':
            abort(400, description='この顧客に適用する契約と期限の根拠を確認してください。')
        with service.db(write=True) as registry:
            registry.set_deadline(service.actor(), item_id, deadline_at=_time(request.form.get('deadline_at')),
                                  evidence_reference=_text('contract_reference', 1000, True), source_key=key)
        flash('確認した契約の保管期限を記録しました。処分や請求は行いません。', 'success')
        return redirect(url_for('inventory_registry.detail', item_id=item_id))

    @bp.post('/inventory/items/<item_id>/work')
    @allowed(staff=True)
    def work(item_id):
        key = service.verify('work', item_id)
        kind = _text('kind', 40, True)
        if kind not in WORK_LABELS:
            abort(400)
        raw_minutes = _text('duration_minutes', 12)
        duration = None
        if raw_minutes:
            try:
                seconds = Decimal(raw_minutes) * 60
                if not seconds.is_finite() or seconds < 0 or seconds > 86400 or seconds != int(seconds):
                    abort(400, description='実測した分数を入力してください。')
                duration = int(seconds)
            except InvalidOperation:
                abort(400)
        with service.db(write=True) as registry:
            registry.record_work(service.actor(), item_id, kind, performed_at=_time(request.form.get('performed_at')),
                                 source_key=key, evidence_reference=_text('note', 2000, True),
                                 duration_seconds=duration, measurement_source='observed' if duration is not None else 'unknown',
                                 rework_of=_text('rework_of', 100) or None, rework_reason=_text('rework_reason', 1000) or None)
        flash('実作業を記録しました。未入力の時間は不明として残し、請求は作成していません。', 'success')
        return redirect(url_for('inventory_registry.detail', item_id=item_id))

    @bp.get('/inventory/reconcile')
    @allowed(staff=True)
    def reconcile():
        with service.db() as registry:
            result = registry.reconcile(service.actor())
        return render_template('inventory_registry/reconcile.html', result=result)

    @bp.post('/inventory/reconcile/link')
    @allowed(staff=True)
    def link():
        key = service.verify('link')
        raw = _text('merchandise_id', 12, True)
        if not raw.isascii() or not raw.isdigit():
            abort(400)
        if request.form.get('reviewed') != 'yes':
            abort(400, description='現物と既存記録の対応を確認してください。')
        # Stable, signed timestamp keeps a repeated confirmation idempotent.
        payload, signed_at = service.signer.loads(request.form['operation_token'], return_timestamp=True)
        when = _time(payload['issued_at']) if payload.get('issued_at') else signed_at
        note = _text('note', 2000, True)
        with service.db(write=True) as registry:
            item = registry.attach_legacy(service.actor(), int(raw),
                                          physical_item_id=_text('physical_item_id', 100) or None,
                                          evidence_reference=note,
                                          occurred_at=when, source_key=key)
            registry.verify_identity(service.actor(), item['physical_item_id'], evidence_reference=note,
                                     occurred_at=when, source_key=key + ':identity')
        flash('既存記録と個品を対応付けました。過去の作業や料金は生成していません。', 'success')
        return redirect(url_for('inventory_registry.detail', item_id=item['physical_item_id']))

    @bp.route('/inventory/imports', methods=['GET', 'POST'])
    @allowed(staff=True)
    def imports():
        from inventory_reconciliation import parse_inventory_file, ReconciliationService
        if request.method == 'GET':
            with service.db() as registry:
                customers = service.customers(registry)
            return render_template('inventory_registry/import.html', customers=customers)
        service.verify('import')
        if request.content_length and request.content_length > MAX_IMPORT_BYTES + 65536:
            abort(413)
        upload = request.files.get('file')
        if not upload or not upload.filename:
            abort(400)
        content = upload.read(MAX_IMPORT_BYTES + 1)
        if len(content) > MAX_IMPORT_BYTES:
            abort(413)
        raw_customer = _text('customer_id', 12, True)
        if not raw_customer.isascii() or not raw_customer.isdigit():
            abort(400)
        raw_mapping = _text('mapping', 4000)
        try:
            mapping = json.loads(raw_mapping) if raw_mapping else None
        except (ValueError, TypeError):
            abort(400, description='列の対応指定を確認してください。')
        parsed = parse_inventory_file(content, upload.filename, mapping=mapping)
        with service.db(write=True) as registry:
            batch = ReconciliationService(registry).preview(service.actor(), int(raw_customer),
                                                            _text('source_namespace', 100, True), parsed)
        return redirect(url_for('inventory_registry.import_detail', batch_id=batch['batch_id']))

    @bp.get('/inventory/imports/<batch_id>')
    @allowed(staff=True)
    def import_detail(batch_id):
        from inventory_reconciliation import ReconciliationService
        with service.db() as registry:
            batch = ReconciliationService(registry).get_batch(service.actor(), batch_id)
        return render_template('inventory_registry/import_detail.html', batch=batch)

    @bp.post('/inventory/imports/<batch_id>/confirm')
    @allowed(staff=True)
    def confirm_import(batch_id):
        from inventory_reconciliation import ReconciliationService
        service.verify('confirm_import', batch_id)
        if request.form.get('reviewed') != 'yes':
            abort(400, description='取込対象・列・照合結果を確認してください。')
        with service.db(write=True) as registry:
            ReconciliationService(registry).confirm(service.actor(), batch_id,
                                                    _text('preview_hash', 64, True), datetime.now(JST).isoformat())
        flash('確認済みの取込を記録しました。料金と過去作業は生成していません。', 'success')
        return redirect(url_for('inventory_registry.import_detail', batch_id=batch_id))

    @bp.after_request
    def private(response):
        response.headers['Cache-Control'] = 'private, no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        return response

    @app.context_processor
    def context():
        return {'stage2_inventory_available': enabled(app),
                'stage2_staff_photos_available': 'stage2_staff_photos' in app.blueprints,
                'registry_state_labels': STATE_LABELS,
                'registry_event_labels': EVENT_DISPLAY_LABELS,
                'registry_time': _display_time,
                'registry_csrf': service.csrf() if enabled(app) and current_user.is_authenticated else None,
                'registry_operation': service.operation}

    app.register_blueprint(bp)
    app.extensions['inventory_registry'] = service
    return service
