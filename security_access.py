"""Final HTTP boundary for private uploads and legacy destructive routes.

Registration never opens a database or moves files. File access is read-only;
unrecognised references are denied. This is deliberately not an account/data
deletion implementation: retention and financial reconciliation are unresolved.
"""
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
import hmac
import json
import re
import secrets

from flask import abort, jsonify, make_response, render_template_string, request, send_file, session
from flask_login import current_user


# Neither the original handlers nor their image cleanup may run until a
# reviewed retention/deletion workflow replaces them.
HELD_DELETIONS = frozenset({
    'delete_user', 'admin_delete_client', 'admin_delete_operator',
    'delete_item', 'admin_delete_item',
})
CUSTOMER_DOCUMENT_MUTATIONS = frozenset({
    'user_invoice_delete', 'user_mitsumori_delete', 'user_keisan_delete',
    'user_kaitori_shoudaku_delete', 'user_invoice_send',
})
CONFIRMED_MUTATIONS = frozenset({
    'delete_customer', 'toggle_admin', 'toggle_tuition_exempt',
    'admin_delete_announcement', 'admin_toggle_announcement',
    'admin_shikiriosho_delete', 'admin_kaitori_delete', 'admin_seisan_delete',
    'admin_mitsumori_delete',
    'admin_kaitori_shoudaku_delete', 'admin_vendor_document_delete',
    'admin_set_role', 'admin_invoice_approve',
    'admin_invoice_reject', 'admin_seisan_send', 'admin_shikiriosho_send',
}) | CUSTOMER_DOCUMENT_MUTATIONS
MUTATIONS = HELD_DELETIONS | CONFIRMED_MUTATIONS

CONFIRMATION = '''<!doctype html><html lang="ja"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="kaika-security-csrf" content="{{ token }}">
<title>操作の確認</title><body><main style="max-width:42em;margin:3em auto;padding:1em;font-family:sans-serif">
<h1>{{ '削除は現在停止しています' if held else '操作の確認' }}</h1>
{% if held %}<p>商品・取引・請求記録の保持条件を確認するまで、この削除操作は実行できません。データと画像は変更していません。</p>
{% else %}<p>この操作を実行しますか？ 戻る場合はブラウザーの戻るボタンをご利用ください。</p>
<form method="post" action="{{ action }}"><input type="hidden" name="security_csrf_token" value="{{ token }}">
<button type="submit">確認して実行する</button></form>{% endif %}
<p><a href="/">ホームへ戻る</a></p></main></body></html>'''


def _paths(value):
    if not isinstance(value, (str, list)):
        return ()
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (ValueError, TypeError):
            parsed = value
    else:
        parsed = value
    values = parsed if isinstance(parsed, list) else [parsed]
    result = []
    for path in values:
        if not isinstance(path, str):
            continue
        normalized = path.replace('\\', '/')
        if normalized.startswith('static/'):
            normalized = normalized[7:]
        if normalized.startswith('uploads/'):
            result.append(normalized)
    return result


def staff_has_permission(user, permission=None):
    if not user.is_admin():
        return False
    if permission is None or getattr(user, 'role', None) == 'owner':
        return True
    checker = getattr(user, 'has_permission', None)
    return bool(callable(checker) and checker(permission))


class SecurityAccess:
    def __init__(self, runtime):
        self.runtime = runtime
        self.app = runtime.app
        self.postgres = bool(getattr(runtime, 'DATABASE_URL', None))
        self.mark = '%s' if self.postgres else '?'

    @contextmanager
    def db(self):
        connection = self.runtime.get_db()
        cursor = connection.cursor()
        try:
            yield cursor
        finally:
            cursor.close()
            connection.close()

    def columns(self, cur, table):
        # Table names below are a fixed application allowlist, never URL input.
        if self.postgres:
            cur.execute('SELECT column_name FROM information_schema.columns WHERE table_schema=current_schema() AND table_name=%s', (table,))
            return {next(iter(row.values())) if isinstance(row, dict) else row[0] for row in cur.fetchall()}
        cur.execute(f'PRAGMA table_info({table})')
        return {row['name'] if hasattr(row, 'keys') else row[1] for row in cur.fetchall()}

    def references(self, cur, table, fields, relative):
        columns = self.columns(cur, table)
        selected = [name for name in fields if name in columns]
        if not selected:
            return []
        # LIKE only narrows candidates. Exact decoded path membership below is
        # authoritative, so wildcard filenames and JSON substrings cannot grant
        # access to a different file.
        query = ' OR '.join(f'{name} LIKE {self.mark}' for name in selected)
        cur.execute(f'SELECT * FROM {table} WHERE {query}', tuple('%' + relative + '%' for _ in selected))
        names = [column[0] for column in cur.description]
        rows = [dict(row) if hasattr(row, 'keys') else dict(zip(names, row)) for row in cur.fetchall()]
        return [(row, field) for row in rows for field in selected if relative in _paths(row.get(field))]

    def staff(self, permission=None):
        return staff_has_permission(current_user, permission)

    def own(self, row):
        return row.get('user_id') is not None and str(row['user_id']) == str(current_user.id)

    def catalog_photo(self, cur, row):
        """Only explicitly published company stock and assigned viewers.

        Do not call legacy audience helpers here: some ensure schema or update
        auction results. Serving an image must not mutate business state.
        """
        if not row.get('auction_id') or not row.get('show_in_proxy_service'):
            return False
        required = {'users': {'id', 'role'},
                    'proxy_service_settings': {'id', 'is_public'},
                    'proxy_service_auction_users': {'auction_id', 'user_id', 'is_enabled'}}
        if any(not fields <= self.columns(cur, table) for table, fields in required.items()):
            return False
        if row.get('user_id') is not None:
            cur.execute(f'SELECT role FROM users WHERE id={self.mark}', (row['user_id'],))
            owner = cur.fetchone()
            role = (owner['role'] if hasattr(owner, 'keys') else owner[0]) if owner else None
            if role not in {'admin', 'owner'}:
                return False
        cur.execute(f'''SELECT 1 FROM proxy_service_settings s
            JOIN proxy_service_auction_users a ON a.auction_id=s.id
            WHERE s.id={self.mark} AND s.is_public=TRUE AND a.user_id={self.mark} AND a.is_enabled=TRUE''',
                    (row['auction_id'], current_user.id))
        return cur.fetchone() is not None

    def allowed(self, relative):
        with self.db() as cur:
            merchandise = self.references(cur, 'merchandise',
                ('photo_path', 'additional_photos', 'id_document_path', 'consent_form_path'), relative)
            # Personal source documents must never inherit a photograph's
            # wider visibility when the same path has multiple references.
            sensitive = any(field in ('id_document_path', 'consent_form_path') for _, field in merchandise)
            vendor = self.references(cur, 'vendor_documents', ('stored_path',), relative)
            vendor_responses = self.references(cur, 'admin_kaitori_shoudaku', ('vendor_response_file_path',), relative)
            if sensitive or vendor or vendor_responses:
                return self.staff('users') if sensitive else self.staff('invoices')
            if merchandise and (self.staff('merchandise') or any(self.own(row) for row, _ in merchandise)):
                return True
            if any(self.catalog_photo(cur, row) for row, field in merchandise
                   if field in {'photo_path', 'additional_photos'}):
                return True
            for table, fields in (
                ('sale_requests', ('qr_image_path', 'qr_image_path2', 'additional_image_paths')),
                ('inquiries', ('image_path',)),
            ):
                if any(self.own(row) or self.staff() for row, _ in self.references(cur, table, fields, relative)):
                    return True
            if self.agency_photo(cur, merchandise, relative):
                return True
        return False

    def agency_photo(self, cur, merchandise, relative):
        if not {'id', 'user_id'} <= self.columns(cur, 'sales_agency_requests'):
            return False
        invoice_staff = self.staff('invoices')
        inventory_staff = self.staff('merchandise')
        # Historical agency documents retain their own photo after an item edit.
        snapshots = self.references(cur, 'sales_agency_request_items', ('snapshot_photo_path',), relative)
        for row, _ in snapshots:
            cur.execute(f'SELECT user_id FROM sales_agency_requests WHERE id={self.mark}', (row.get('request_id'),))
            owner = cur.fetchone()
            if owner:
                owner_id = owner['user_id'] if hasattr(owner, 'keys') else owner[0]
                if invoice_staff or inventory_staff or str(owner_id) == str(current_user.id):
                    return True
        if not invoice_staff or not {'request_id', 'merchandise_id'} <= self.columns(cur, 'sales_agency_request_items'):
            return False
        # The agency detail and invoice forms can also show a linked item's
        # current main photo. Require the request and item to have the same owner.
        for row, field in merchandise:
            if field == 'photo_path':
                cur.execute(f'''SELECT 1 FROM sales_agency_request_items i
                    JOIN sales_agency_requests r ON r.id=i.request_id
                    WHERE i.merchandise_id={self.mark} AND r.user_id={self.mark} LIMIT 1''',
                    (row['id'], row['user_id']))
                if cur.fetchone():
                    return True
        return False

    def require_upload_access(self, relative):
        try:
            allowed = self.allowed(relative)
        except Exception:
            # Fail closed without exposing connection or personal data.
            self.app.logger.error('Private upload authorization unavailable')
            abort(503)
        if not allowed:
            abort(404)

    def authorize_photo_archive(self):
        if not current_user.is_authenticated or not current_user.is_admin() or self.staff('merchandise'):
            return  # The download route itself checks customer ownership.
        item_id = (request.view_args or {}).get('id')
        if not isinstance(item_id, int) or not 1 <= item_id <= 2_147_483_647:
            abort(404)
        with self.db() as cur:
            cur.execute(f'SELECT 1 FROM merchandise WHERE id={self.mark} AND user_id={self.mark}',
                        (item_id, current_user.id))
            if not cur.fetchone():
                abort(404)

    def upload(self, filename):
        root = Path(self.app.static_folder).resolve()
        raw = str(filename or '')
        if '\x00' in raw:
            abort(404)
        # Reject aliases before considering whether the target is an upload.
        parts = raw.replace('\\', '/').split('/')
        try:
            candidate = (root / raw).resolve()
        except (OSError, RuntimeError, ValueError):
            abort(404)
        uploads = (root / 'uploads').resolve()
        is_upload = 'uploads' in parts or candidate == uploads or uploads in candidate.parents
        if not is_upload:
            return None
        if (not raw.startswith('uploads/') or '\\' in raw or '%' in raw or ':' in raw
                or any(part in ('', '.', '..') or part.startswith('.') for part in parts)
                or PurePosixPath(raw).is_absolute()):
            abort(404)
        if not current_user.is_authenticated:
            abort(401)
        # The virtual /uploads/mobile path is handled by the existing mobile
        # ownership handler. The physical .mobile-private path is always denied.
        if raw.startswith('uploads/mobile/'):
            if request.endpoint != 'kaika_mobile_browser_photo':
                abort(404)
            return None
        if root not in candidate.parents or uploads not in candidate.parents:
            abort(404)
        target = root
        for part in parts:
            target = target / part
            if target.is_symlink() or getattr(target, 'is_junction', lambda: False)():
                abort(404)
        if not candidate.is_file():
            abort(404)
        self.require_upload_access(raw)
        inline = candidate.suffix.lower() in {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.pdf'}
        response = send_file(candidate, conditional=False, etag=False, as_attachment=not inline)
        response.headers.update({'Cache-Control': 'private, no-store', 'X-Content-Type-Options': 'nosniff',
                                 'Content-Security-Policy': "sandbox; default-src 'none'"})
        return response

    def csrf_token(self):
        binding = str(current_user.get_id())
        if session.get('_security_csrf_user') != binding or not session.get('_security_csrf'):
            session['_security_csrf_user'] = binding
            session['_security_csrf'] = secrets.token_urlsafe(32)
        return session['_security_csrf']

    def check_csrf(self):
        supplied = request.form.get('security_csrf_token') or request.headers.get('X-Security-CSRF-Token', '')
        expected = session.get('_security_csrf', '')
        if (session.get('_security_csrf_user') != str(current_user.get_id()) or not supplied or not expected
                or not hmac.compare_digest(str(supplied), str(expected))):
            abort(400, description='画面を開き直してから操作してください。')

    def boundary(self):
        if request.endpoint == 'static':
            return self.upload((request.view_args or {}).get('filename', ''))
        if request.endpoint == 'kaika_mobile_browser_photo':
            return self.upload('uploads/mobile/' + (request.view_args or {}).get('filename', ''))
        endpoint = request.endpoint or ''
        if endpoint == 'download_all_images':
            return self.authorize_photo_archive()
        if endpoint not in MUTATIONS:
            return None
        if not current_user.is_authenticated:
            abort(401)
        if endpoint in CUSTOMER_DOCUMENT_MUTATIONS and current_user.role != 'user':
            abort(403)
        if endpoint.startswith('admin_') or endpoint in {'delete_user', 'toggle_admin', 'toggle_tuition_exempt'}:
            if not self.staff():
                abort(403)
            if endpoint in {'delete_user', 'admin_delete_client', 'admin_delete_operator', 'toggle_admin', 'admin_set_role', 'toggle_tuition_exempt'} and not self.staff('users'):
                abort(403)
        if request.method in {'GET', 'HEAD'}:
            response = make_response(render_template_string(CONFIRMATION, held=endpoint in HELD_DELETIONS,
                action=request.full_path.rstrip('?'), token=self.csrf_token()))
            response.headers['Cache-Control'] = 'no-store'
            return response
        if request.method != 'POST':
            abort(405)
        self.check_csrf()
        if endpoint in HELD_DELETIONS:
            return jsonify(error={'code': 'deletion_policy_pending',
                'message': '保持条件の確認が必要なため、商品・顧客・担当者の削除は停止しています。データと画像は変更していません。'}), 409
        return None


def register_security_access(runtime):
    app = runtime.app
    if 'kaika_security_access' in app.extensions:
        return app.extensions['kaika_security_access']
    service = SecurityAccess(runtime)
    app.extensions['kaika_security_access'] = service
    # Preserve URLs and original authorization decorators, but make GET a
    # confirmation-only operation. Add POST to former GET-only routes.
    rules = list(app.url_map.iter_rules())
    for rule in rules:
        if rule.endpoint in MUTATIONS and 'POST' not in rule.methods:
            app.add_url_rule(rule.rule, endpoint=rule.endpoint,
                             view_func=app.view_functions[rule.endpoint], methods=['POST'])
        if rule.endpoint in MUTATIONS and 'GET' not in rule.methods:
            app.add_url_rule(rule.rule, endpoint=rule.endpoint,
                             view_func=app.view_functions[rule.endpoint], methods=['GET'])
    @app.context_processor
    def security_form_context():
        patterns = []
        for rule in app.url_map.iter_rules():
            if rule.endpoint not in MUTATIONS:
                continue
            parts = re.split(r'(<[^>]+>)', rule.rule)
            pattern = '^' + re.escape(request.script_root) + ''.join(
                (r'\d+' if part.startswith('<int:') else '[^/]+') if part.startswith('<')
                else re.escape(part) for part in parts) + '$'
            patterns.append(pattern)
        return {'security_form_token': service.csrf_token() if current_user.is_authenticated else None,
                'security_guarded_form_patterns': sorted(set(patterns)),
                'security_business_review_available': 'business_review.index' in app.view_functions}
    app.before_request_funcs.setdefault(None, []).insert(0, service.boundary)
    return service
