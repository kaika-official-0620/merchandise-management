"""Review-only Stage 1 UI and a fail-closed boundary for unbound Kaika fees.

Registration performs no database migration, network call or payment. Never
accept approval evidence from request JSON. Production deployment is separate.
"""
import hmac
import secrets
from functools import wraps

from flask import Blueprint, abort, jsonify, render_template, request, session, url_for
from flask_login import current_user, login_required
from werkzeug.exceptions import HTTPException

from business_policy import draft_catalog, draft_sale_quote, evaluate_invoice_eligibility

CASE_LABELS = {
    'normal': '開始後の作業（契約・料金が未承認）',
    'before_start': '開始前に行った作業',
    'mixed_start': '開始前の撮影と開始後の発送',
    'return_unresolved': '返品の扱いが未決定',
}
CHANNEL_LABELS = {'wholesale': '業者卸', 'multichannel': '同時出品',
                  'dealer_auction': '業者競売', 'self_sale_shipping': '自己販売の発送のみ',
                  'live': 'LIVE（初期提供は未決定）'}
DRAFT_CREATORS = {'admin_monthly_settlement_create', 'admin_shikiriosho_add',
                  'admin_shikiriosho_add_from_documents', 'admin_kaitori_add',
                  'admin_kaitori_add_from_documents', 'admin_seisan_add'}
DRAFT_EDITORS = {'admin_shikiriosho_edit', 'admin_kaitori_edit', 'admin_seisan_edit'}
DRAFT_DELETORS = {'admin_shikiriosho_delete', 'admin_seisan_delete', 'admin_kaitori_delete'}


def _is_staff():
    return any(callable(getattr(current_user, name, None)) and getattr(current_user, name)()
               for name in ('is_admin', 'is_owner'))


def _token():
    actor = str(current_user.get_id())
    if session.get('_business_review_actor') != actor or not session.get('_business_review_csrf'):
        session['_business_review_actor'] = actor
        session['_business_review_csrf'] = secrets.token_urlsafe(32)
    return session['_business_review_csrf']


def _check_csrf():
    expected = session.get('_business_review_csrf', '')
    if (not expected or session.get('_business_review_actor') != str(current_user.get_id())
            or not hmac.compare_digest(expected, request.form.get('business_review_csrf', ''))):
        abort(403)


def register_business_review(runtime):
    app = runtime.app
    if 'business_review' in app.blueprints:
        return
    bp = Blueprint('business_review', __name__)

    def staff_only(fn):
        @wraps(fn)
        @login_required
        def wrapped(*args, **kwargs):
            if not _is_staff():
                abort(403)
            return fn(*args, **kwargs)
        return wrapped

    @bp.route('/admin/business-policy', methods=['GET'])
    @staff_only
    def index():
        return render_template('admin/business_policy_review.html', catalog=draft_catalog(),
                               cases=CASE_LABELS, channels=CHANNEL_LABELS, result=None,
                               quote=None, business_review_csrf=_token())

    @bp.route('/admin/business-policy/preview', methods=['POST'])
    @staff_only
    def preview():
        _check_csrf()
        from business_policy import example_invoice_context
        case = request.form.get('case', '')
        channel = request.form.get('channel', '')
        raw_amount = request.form.get('sale_price', '')
        if (case not in CASE_LABELS or channel not in CHANNEL_LABELS or
                not raw_amount.isascii() or not raw_amount.isdigit() or len(raw_amount) > 12):
            abort(400)
        amount = int(raw_amount)
        if amount < 1:
            abort(400)
        result = evaluate_invoice_eligibility(example_invoice_context(case)).as_dict()
        quote = draft_sale_quote(amount, channel)
        payload = dict(status='draft', purpose='fictional_stage1_review', live_payment_enabled=False,
                       catalog_version=draft_catalog()['version'], case=case,
                       quote=quote, eligibility=result)
        if request.form.get('download') == 'json':
            response = jsonify(payload)
            response.headers['Content-Disposition'] = 'attachment; filename="kaika-stage1-draft.json"'
            return response
        return render_template('admin/business_policy_review.html', catalog=draft_catalog(),
                               cases=CASE_LABELS, channels=CHANNEL_LABELS, result=result,
                               quote=quote, business_review_csrf=_token())

    def unavailable(message=None, code='contract_review_required'):
        # No request-supplied checkbox/fee-version can authorize a legacy invoice.
        message = message or ('この明細は顧客の契約版・同意・請求開始日との照合が済んでいません。'
                              '請求確定・送付は保留しています。下書きと作業記録は残せます。')
        if request.is_json or request.accept_mimetypes.best == 'application/json':
            return jsonify(error={'code': code, 'message': message},
                           review_url=url_for('business_review.index')), 409
        return render_template('admin/business_policy_hold.html', message=message), 409

    def lookup(table, identifiers):
        # Table names are internal constants. Missing/malformed data never grants permission.
        if not identifiers:
            return []
        mark = '%s' if getattr(runtime, 'DATABASE_URL', None) else '?'
        connection = runtime.get_db()
        cursor = connection.cursor()
        try:
            cursor.execute('SELECT * FROM ' + table + ' WHERE id IN (' +
                           ','.join([mark] * len(identifiers)) + ')', tuple(identifiers))
            names = [c[0] for c in cursor.description]
            return [dict(row) if hasattr(row, 'keys') else dict(zip(names, row))
                    for row in cursor.fetchall()]
        finally:
            cursor.close()
            connection.close()

    @app.before_request
    def hold_unverified_legacy_finalization():
        endpoint = request.endpoint
        if not current_user.is_authenticated:
            return None  # Original authentication remains responsible.
        is_write = request.method not in ('GET', 'HEAD', 'OPTIONS')
        if endpoint in DRAFT_DELETORS and is_write:
            if not _is_staff():
                abort(403)
            table = 'invoices' if endpoint == 'admin_kaitori_delete' else 'shikiriosho'
            retention_message = ('送付・確定済みの原明細、または状態を確認できない明細の削除は保留しています。'
                                 '訂正と記録保持の手順が整うまで元の書類を残します。')
            try:
                rows = lookup(table, [int(request.view_args['id'])])
                if not rows or rows[0].get('status') not in ('draft', 'in_progress'):
                    return unavailable(retention_message, 'document_retention_required')
            except Exception:
                return unavailable(retention_message, 'document_retention_required')
        if endpoint == 'admin_step4_client_invoice_create' and is_write:
            if not _is_staff():
                abort(403)
            return unavailable()  # Old handler creates and sends in one transaction.
        if endpoint in DRAFT_CREATORS | DRAFT_EDITORS and is_write:
            if not _is_staff():
                abort(403)
            if request.form.get('status', '') not in ('draft', 'in_progress'):
                return unavailable()
            # Legacy editors can mutate a sent amount without a correction record.
            if endpoint in DRAFT_EDITORS:
                table = 'shikiriosho' if endpoint in ('admin_shikiriosho_edit', 'admin_seisan_edit') else 'invoices'
                try:
                    rows = lookup(table, [int(request.view_args['id'])])
                    if not rows or rows[0].get('status') not in ('draft', 'in_progress'):
                        return unavailable()
                except Exception:
                    return unavailable()
        if endpoint in {'admin_shikiriosho_send', 'admin_shikiriosho_send_bulk', 'admin_seisan_send',
                        'admin_invoice_approve'}:
            if not _is_staff():
                abort(403)
            # No existing Kaika settlement has a verified Stage 1 binding yet.
            # The old approval handler also creates a settlement on every replay.
            # Keep it held until a bound, idempotent approval flow replaces it.
            return unavailable()
        if endpoint == 'user_invoice_send':
            try:
                rows = lookup('invoices', [int(request.view_args['id'])])
                if not rows:
                    return None
                row = rows[0]
                if str(row.get('sender_id')) != str(current_user.get_id()):
                    abort(403)
                if row.get('source_admin_kaitori_id') or row.get('document_scope') in ('admin_kaitori', 'client_outgoing'):
                    return unavailable()
            except HTTPException:
                raise
            except Exception:
                return unavailable()
        return None

    @bp.after_request
    def no_cache(response):
        response.headers['Cache-Control'] = 'private, no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        return response

    app.register_blueprint(bp)

