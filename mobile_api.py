"""Opt-in native mobile API; browser login and business workflows stay separate.

Registration does not connect to a database. Only requests to an enabled API
create the two mobile-only authentication tables. Tests use a fixture runtime.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from functools import wraps
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import time
import warnings

from flask import Blueprint, abort, g, jsonify, request, send_file, url_for
from flask_login import current_user
from PIL import Image, UnidentifiedImageError
from werkzeug.exceptions import HTTPException, RequestEntityTooLarge
from werkzeug.security import check_password_hash, generate_password_hash


TOKEN_TTL = 30 * 24 * 60 * 60
MAX_PHOTO_BYTES = 10 * 1024 * 1024
MAX_IMAGE_PIXELS = 20_000_000
EDITABLE_FIELDS = frozenset({'product_name', 'notes'})
_DUMMY_PASSWORD = generate_password_hash('not-a-real-mobile-account')


def _register_mobile_media(runtime):
    """Keep native photos on the existing upload volume but outside static access.

    Install even when the API is disabled: turning off API access must not make
    previously uploaded media public. The virtual web URL still works with the
    existing browser session and inventory ownership rules.
    """
    app = runtime.app
    if 'kaika_mobile_media' in app.extensions:
        return app.extensions['kaika_mobile_media']
    static_root = Path(app.static_folder).resolve()
    private_root = static_root / 'uploads' / '.mobile-private'
    original_static = app.view_functions.get('static')
    if original_static:
        @wraps(original_static)
        def protected_static(filename):
            candidate = (static_root / filename.replace('\\', '/')).resolve()
            resolved_private = private_root.resolve()
            if candidate == resolved_private or resolved_private in candidate.parents:
                abort(404)
            return original_static(filename=filename)
        app.view_functions['static'] = protected_static

    @app.get('/static/uploads/mobile/<filename>', endpoint='kaika_mobile_browser_photo')
    def browser_photo(filename):
        if not re.fullmatch(r'[a-f0-9]{40}\.jpg', filename):
            abort(404)
        if not current_user.is_authenticated:
            abort(401)
        relative = 'uploads/mobile/' + filename
        connection = runtime.get_db()
        try:
            cursor = connection.cursor()
            try:
                sql = 'SELECT user_id, photo_path, additional_photos FROM merchandise WHERE (photo_path = ? OR additional_photos LIKE ?)'
                parameters = [relative, '%' + relative + '%']
                if not current_user.is_admin():
                    sql += ' AND user_id = ?'
                    parameters.append(current_user.id)
                cursor.execute(sql.replace('?', '%s') if getattr(runtime, 'DATABASE_URL', None) else sql, parameters)
                names = [column[0] for column in cursor.description]
                allowed = False
                for row in cursor.fetchall():
                    item = dict(row) if hasattr(row, 'keys') else dict(zip(names, row))
                    additional = item.get('additional_photos') or []
                    if isinstance(additional, str):
                        try:
                            additional = json.loads(additional)
                        except ValueError:
                            additional = []
                    if item.get('photo_path') == relative or (isinstance(additional, list) and relative in additional):
                        allowed = True
                        break
            finally:
                cursor.close()
        finally:
            connection.close()
        path = (private_root / filename).resolve()
        if not allowed or private_root.resolve() not in path.parents or not path.is_file():
            abort(404)
        response = send_file(path, conditional=False)
        response.headers['Cache-Control'] = 'private, no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        return response

    app.extensions['kaika_mobile_media'] = private_root
    return private_root


class MobileError(Exception):
    def __init__(self, code, message, status=400):
        self.code, self.message, self.status = code, message, status


def register_mobile_api(runtime):
    """Install routes only when explicitly enabled, against the existing runtime."""
    app = runtime.app
    private_photo_directory = _register_mobile_media(runtime)
    if os.environ.get('MOBILE_API_ENABLED') != '1':
        return False
    if 'kaika_mobile' in app.blueprints:
        return True
    api = Blueprint('kaika_mobile', __name__, url_prefix='/api/mobile/v1')
    postgres = bool(getattr(runtime, 'DATABASE_URL', None))

    @contextmanager
    def database():
        connection = runtime.get_db()
        try:
            cursor = connection.cursor()
            try:
                yield connection, cursor
            finally:
                cursor.close()
        finally:
            connection.close()

    def execute(cursor, sql, parameters=()):
        cursor.execute(sql.replace('?', '%s') if postgres else sql, parameters)

    def row_dict(cursor, row):
        if row is None:
            return None
        if hasattr(row, 'keys'):
            return dict(row)
        return dict(zip((column[0] for column in cursor.description), row))

    def one(cursor):
        return row_dict(cursor, cursor.fetchone())

    def many(cursor):
        return [row_dict(cursor, row) for row in cursor.fetchall()]

    def ensure_auth_schema(cursor):
        cursor.execute('''CREATE TABLE IF NOT EXISTS mobile_sessions (
            token_hash VARCHAR(64) PRIMARY KEY, user_id INTEGER NOT NULL,
            created_at BIGINT NOT NULL, expires_at BIGINT NOT NULL,
            revoked_at BIGINT, password_fingerprint VARCHAR(64) NOT NULL
        )''')
        cursor.execute('''CREATE TABLE IF NOT EXISTS mobile_login_attempts (
            bucket VARCHAR(64) PRIMARY KEY, window_start BIGINT NOT NULL,
            failures INTEGER NOT NULL DEFAULT 0
        )''')

    def digest(value):
        return hashlib.sha256(value.encode('utf-8')).hexdigest()

    def blocked_account(user):
        if user.get('role') in ('admin', 'owner') or user.get('subscription_status') != 'past_due':
            return False
        overdue = user.get('overdue_since')
        if not overdue:
            return False
        if isinstance(overdue, str):
            try:
                overdue = datetime.fromisoformat(overdue.replace('Z', '+00:00'))
            except ValueError:
                return False
        if not isinstance(overdue, datetime):
            return False
        now = runtime.get_jst_now()
        jst = timezone(timedelta(hours=9))
        if now.tzinfo is None:
            now = now.replace(tzinfo=jst)
        if overdue.tzinfo is None:
            overdue = overdue.replace(tzinfo=jst)
        return (now - overdue).days >= 90

    def capabilities(user):
        can_edit = bool(user.is_admin() and user.can_edit_merchandise())
        return {
            'can_create_item': False,
            'can_edit_items': can_edit,
            'can_upload_photos': can_edit,
            'can_view_all_inventory': bool(user.is_admin()),
            'can_view_analytics': bool(not user.is_admin() or user.has_permission('analytics')),
        }

    def serialize_user(user):
        return {
            'id': int(user.id), 'username': user.username,
            'display_name': user.display_name, 'role': user.role,
            'subscription_status': user.subscription_status,
            'capabilities': capabilities(user),
        }

    def login_required(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            match = re.fullmatch(r'Bearer ([A-Za-z0-9_-]{43})', request.headers.get('Authorization', ''))
            if not match:
                raise MobileError('unauthorized', 'ログインしてください。', 401)
            token_hash = digest(match.group(1))
            with database() as (connection, cursor):
                ensure_auth_schema(cursor)
                execute(cursor, '''SELECT user_id, expires_at, revoked_at, password_fingerprint
                    FROM mobile_sessions WHERE token_hash = ?''', (token_hash,))
                session = one(cursor)
                user = None
                if session and not session['revoked_at'] and int(session['expires_at']) > int(time.time()):
                    execute(cursor, 'SELECT * FROM users WHERE id = ?', (session['user_id'],))
                    user = one(cursor)
                connection.commit()
            if not user or digest(user.get('password_hash') or '') != session['password_fingerprint'] or blocked_account(user):
                raise MobileError('unauthorized', 'セッションの有効期限が切れました。もう一度ログインしてください。', 401)
            g.mobile_user = runtime.build_user_from_record(user)
            g.mobile_token_hash = token_hash
            # Existing pricing/announcement helpers may use current_user. This is
            # request-local only: no login_user(), browser session, or cookie write.
            g._login_user = g.mobile_user
            return function(*args, **kwargs)
        return wrapped

    def payload(max_bytes=8192):
        if request.content_length and request.content_length > max_bytes:
            raise MobileError('invalid_input', '入力が長すぎます。', 413)
        if not request.is_json:
            raise MobileError('invalid_input', 'JSON オブジェクトを送信してください。')
        raw = request.stream.read(max_bytes + 1)
        if len(raw) > max_bytes:
            raise MobileError('invalid_input', '入力が長すぎます。', 413)
        try:
            body = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise MobileError('invalid_input', 'JSON オブジェクトを送信してください。') from None
        if not isinstance(body, dict):
            raise MobileError('invalid_input', 'JSON オブジェクトを送信してください。')
        return body

    def number_arg(name, default, maximum):
        raw = request.args.get(name, str(default))
        if not raw.isdigit() or len(raw) > 6 or not 1 <= int(raw) <= maximum:
            raise MobileError('invalid_input', f'{name} の値が正しくありません。')
        return int(raw)

    def scope_clause():
        if g.mobile_user.is_admin():
            return '1 = 1', []
        return 'm.user_id = ?', [int(g.mobile_user.id)]

    def item_by_id(item_id):
        condition, parameters = scope_clause()
        with database() as (_, cursor):
            execute(cursor, f'SELECT m.* FROM merchandise m WHERE m.id = ? AND {condition}', [item_id] + parameters)
            item = one(cursor)
        if item is None:
            raise MobileError('not_found', '商品が見つかりません。', 404)
        return item

    def valid_photo_path(path):
        if not isinstance(path, str) or not path:
            return None
        normalized = path.replace('\\', '/')
        parts = PurePosixPath(normalized)
        if parts.is_absolute() or '..' in parts.parts or not normalized.startswith('uploads/'):
            return None
        return normalized

    def photo_paths(item):
        additional = item.get('additional_photos') or []
        if isinstance(additional, str):
            try:
                additional = json.loads(additional)
            except (ValueError, TypeError):
                additional = []
        if not isinstance(additional, list):
            additional = []
        paths = [valid_photo_path(path) for path in [item.get('photo_path')] + additional]
        return list(dict.fromkeys(path for path in paths if path))[:20]

    def scalar(value):
        return value.isoformat() if isinstance(value, (datetime, date)) else value

    def decorate(item):
        item = dict(item)
        runtime.apply_inventory_display_metrics(
            item, scope='admin' if g.mobile_user.is_admin() else 'user',
            fee_settings=get_fee_settings(),
        )
        return item

    def get_fee_settings():
        if 'mobile_fee_settings' not in g:
            g.mobile_fee_settings = runtime.get_fee_settings()
        return g.mobile_fee_settings

    def serialize_item(item, detailed=False):
        item = decorate(item)
        if detailed:
            # Preserve existing shipping and long-term workflow status display.
            runtime.enrich_item_sale_request_state(item, include_all_users=g.mobile_user.is_admin())
            runtime.attach_long_term_request_state(item)
        urls = [url_for('kaika_mobile.get_photo', item_id=item['id'], photo_index=index, _external=True)
                for index, _ in enumerate(photo_paths(item))]
        status = 'sold' if item.get('is_sold') else ('listed' if item.get('is_listed') else 'unlisted')
        fields = ('id', 'product_name', 'brand_name', 'model_number', 'item_condition',
                  'kaika_product_code', 'scope', 'purchase_date', 'storage_start_date',
                  'sale_date', 'listing_date', 'listing_price', 'sale_price',
                  'display_purchase_price', 'display_fee_total', 'shipping_cost',
                  'other_cost', 'notes', 'updated_at')
        result = {field: scalar(item.get(field)) for field in fields}
        result.update({
            'status': status,
            'status_label': runtime.resolve_inventory_mobile_status_label(item),
            'status_tags': runtime.build_inventory_status_tags(item),
            'photo_url': urls[0] if urls else None, 'photos': urls,
            'profit': item.get('display_profit', item.get('profit', 0)) or 0,
            'capabilities': {
                'can_edit': capabilities(g.mobile_user)['can_edit_items'],
                'can_upload_photos': capabilities(g.mobile_user)['can_upload_photos'],
            },
            'editable_fields': sorted(EDITABLE_FIELDS) if capabilities(g.mobile_user)['can_edit_items'] else [],
        })
        return result

    def announcements():
        if g.mobile_user.is_admin():
            return []
        return [runtime.serialize_announcement_payload(row)
                for row in runtime.get_active_announcements(g.mobile_user, limit=None)]

    @api.before_request
    def require_https():
        if not app.testing and not request.is_secure:
            raise MobileError('https_required', 'HTTPS 接続が必要です。', 403)

    @api.after_request
    def response_headers(response):
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Kaika-Mobile-API'] = '1'
        return response

    @api.errorhandler(MobileError)
    def mobile_error(error):
        return jsonify(error={'code': error.code, 'message': error.message}), error.status

    @api.errorhandler(Exception)
    def internal_error(error):
        if isinstance(error, RequestEntityTooLarge):
            return mobile_error(MobileError('invalid_input', 'ファイルが大きすぎます。', 413))
        if isinstance(error, HTTPException):
            return mobile_error(MobileError('request_failed', 'リクエストを処理できませんでした。', error.code))
        app.logger.error('Mobile API request failed (%s)', type(error).__name__)
        return mobile_error(MobileError('server_error', '処理できませんでした。しばらくしてから再度お試しください。', 500))

    @api.post('/session')
    def create_session():
        body = payload()
        username, password = body.get('username'), body.get('password')
        if (not isinstance(username, str) or not username.strip() or len(username) > 128
                or not isinstance(password, str) or not password or len(password) > 1024
                or set(body) - {'username', 'password'}):
            raise MobileError('invalid_input', 'ユーザー名とパスワードを入力してください。')
        username = username.strip()
        now = int(time.time())
        # Account and IP buckets independently limit distributed and single-host guessing.
        buckets = (digest('account:' + username.lower()), digest('ip:' + (request.remote_addr or 'unknown')))
        with database() as (connection, cursor):
            ensure_auth_schema(cursor)
            for bucket in buckets:
                execute(cursor, 'SELECT window_start, failures FROM mobile_login_attempts WHERE bucket = ?', (bucket,))
                attempt = one(cursor)
                if attempt and int(attempt['window_start']) > now - 900 and int(attempt['failures']) >= 10:
                    raise MobileError('rate_limited', '試行回数が多すぎます。15 分後にお試しください。', 429)
            execute(cursor, 'SELECT * FROM users WHERE username = ?', (username,))
            row = one(cursor)
            valid = check_password_hash(row['password_hash'] if row else _DUMMY_PASSWORD, password)
            if not row or not valid or blocked_account(row):
                for bucket in buckets:
                    execute(cursor, '''INSERT INTO mobile_login_attempts (bucket, window_start, failures)
                        VALUES (?, ?, 1) ON CONFLICT(bucket) DO UPDATE SET
                        failures = CASE WHEN mobile_login_attempts.window_start <= ? THEN 1 ELSE mobile_login_attempts.failures + 1 END,
                        window_start = CASE WHEN mobile_login_attempts.window_start <= ? THEN ? ELSE mobile_login_attempts.window_start END''',
                        (bucket, now, now - 900, now - 900, now))
                connection.commit()
                raise MobileError('invalid_credentials', 'ユーザー名またはパスワードが正しくないか、現在ログインできません。', 401)
            token = secrets.token_urlsafe(32)
            expires = now + TOKEN_TTL
            execute(cursor, '''INSERT INTO mobile_sessions
                (token_hash, user_id, created_at, expires_at, password_fingerprint)
                VALUES (?, ?, ?, ?, ?)''', (digest(token), row['id'], now, expires, digest(row['password_hash'])))
            execute(cursor, 'DELETE FROM mobile_login_attempts WHERE bucket = ?', (buckets[0],))
            execute(cursor, 'DELETE FROM mobile_sessions WHERE expires_at <= ? OR revoked_at IS NOT NULL', (now,))
            execute(cursor, 'UPDATE users SET last_login = ? WHERE id = ?', (runtime.get_jst_now().isoformat(sep=' '), row['id']))
            connection.commit()
        user = runtime.build_user_from_record(row)
        return jsonify(token=token, expires_at=datetime.fromtimestamp(expires, timezone.utc).isoformat(), user=serialize_user(user))

    @api.get('/session')
    @login_required
    def current_session():
        return jsonify(user=serialize_user(g.mobile_user))

    @api.delete('/session')
    @login_required
    def delete_session():
        with database() as (connection, cursor):
            execute(cursor, 'UPDATE mobile_sessions SET revoked_at = ? WHERE token_hash = ?', (int(time.time()), g.mobile_token_hash))
            connection.commit()
        return jsonify(success=True)

    @api.get('/items')
    @login_required
    def list_items():
        page, limit = number_arg('page', 1, 100000), number_arg('limit', 30, 100)
        query = request.args.get('q', '').strip()
        if len(query) > 100:
            raise MobileError('invalid_input', '検索文字が長すぎます。')
        status = request.args.get('status', 'all')
        if status not in ('all', 'unlisted', 'listed', 'sold'):
            raise MobileError('invalid_input', '商品ステータスが正しくありません。')
        condition, parameters = scope_clause()
        if query:
            condition += ' AND (LOWER(m.product_name) LIKE ? OR LOWER(m.brand_name) LIKE ? OR LOWER(m.kaika_product_code) LIKE ? OR LOWER(m.model_number) LIKE ?)'
            parameters += ['%' + query.lower() + '%'] * 4
        with database() as (_, cursor):
            if status == 'all':
                execute(cursor, f'SELECT COUNT(*) AS total FROM merchandise m WHERE {condition}', parameters)
                total = int(one(cursor)['total'])
                execute(cursor, f'''SELECT m.* FROM merchandise m WHERE {condition}
                    ORDER BY m.id DESC LIMIT ? OFFSET ?''', parameters + [limit, (page - 1) * limit])
                rows = many(cursor)
            else:
                # The shared sold predicate also accepts explicit workflow state,
                # and must stay consistent with detail and dashboard serialization.
                execute(cursor, f'SELECT m.* FROM merchandise m WHERE {condition} ORDER BY m.id DESC', parameters)
                rows = []
                for row in many(cursor):
                    sold = runtime.merchandise_is_sold(row)
                    row_status = 'sold' if sold else ('listed' if row.get('is_listed') else 'unlisted')
                    if row_status == status:
                        rows.append(row)
                total = len(rows)
                rows = rows[(page - 1) * limit:page * limit]
        return jsonify(items=[serialize_item(row) for row in rows], page=page, total=total, has_more=page * limit < total)

    @api.get('/items/<int:item_id>')
    @login_required
    def get_item(item_id):
        return jsonify(item=serialize_item(item_by_id(item_id), detailed=True))

    @api.get('/items/<int:item_id>/photos/<int:photo_index>')
    @login_required
    def get_photo(item_id, photo_index):
        paths = photo_paths(item_by_id(item_id))
        if photo_index >= len(paths):
            raise MobileError('not_found', '写真が見つかりません。', 404)
        root = Path(app.static_folder).resolve()
        relative = paths[photo_index]
        if relative.startswith('uploads/mobile/'):
            path = (private_photo_directory / PurePosixPath(relative).name).resolve()
            if private_photo_directory.resolve() not in path.parents or not path.is_file():
                raise MobileError('not_found', '写真が見つかりません。', 404)
            return send_file(path, conditional=False)
        path = (root / relative).resolve()
        uploads = root / 'uploads'
        if uploads not in path.parents or not path.is_file() or path.suffix.lower() not in ('.jpg', '.jpeg', '.png', '.gif', '.webp'):
            raise MobileError('not_found', '写真が見つかりません。', 404)
        return send_file(path, conditional=False)

    @api.patch('/items/<int:item_id>')
    @login_required
    def update_item(item_id):
        item_by_id(item_id)
        if not capabilities(g.mobile_user)['can_edit_items']:
            raise MobileError('forbidden', '商品編集の権限がありません。', 403)
        body = payload(16384)
        if not body or set(body) - EDITABLE_FIELDS:
            raise MobileError('unsupported_fields', 'アプリでは商品名とメモのみ編集できます。')
        for key, value in body.items():
            if not isinstance(value, str) or len(value) > (200 if key == 'product_name' else 4000):
                raise MobileError('invalid_input', '入力が長すぎるか形式が正しくありません。')
            if key == 'product_name' and not value.strip():
                raise MobileError('invalid_input', '商品名を入力してください。')
        columns = sorted(body)
        assignments = ', '.join(f'{column} = ?' for column in columns)
        with database() as (connection, cursor):
            execute(cursor, f'''UPDATE merchandise SET {assignments}, updated_by = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?''', [body[column].strip() if column == 'product_name' else body[column] for column in columns]
                + [g.mobile_user.id, item_id])
            connection.commit()
        return jsonify(item=serialize_item(item_by_id(item_id), detailed=True))

    @api.post('/items/<int:item_id>/photo')
    @login_required
    def upload_photo(item_id):
        existing_item = item_by_id(item_id)
        if not capabilities(g.mobile_user)['can_upload_photos']:
            raise MobileError('forbidden', '写真を変更する権限がありません。', 403)
        if request.content_length is None:
            raise MobileError('invalid_image', '写真のファイルサイズを送信してください。', 411)
        existing_paths = photo_paths(existing_item)
        if len(existing_paths) >= 20:
            raise MobileError('photo_limit', '写真は 20 枚まで登録できます。', 409)
        if request.content_length and request.content_length > MAX_PHOTO_BYTES + 65536:
            raise MobileError('invalid_image', '写真は 10 MB 以下にしてください。', 413)
        files = request.files.getlist('photo')
        if len(files) != 1 or set(request.files) != {'photo'} or request.form:
            raise MobileError('invalid_image', '写真を 1 枚選択してください。')
        content = files[0].stream.read(MAX_PHOTO_BYTES + 1)
        if len(content) > MAX_PHOTO_BYTES:
            raise MobileError('invalid_image', '写真は 10 MB 以下にしてください。', 413)
        try:
            with warnings.catch_warnings():
                warnings.simplefilter('error', Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(content)) as candidate:
                    if candidate.format not in ('JPEG', 'PNG', 'WEBP') or candidate.width * candidate.height > MAX_IMAGE_PIXELS:
                        raise MobileError('invalid_image', 'JPEG・PNG・WebP の写真を選択してください。')
                    candidate.load()
                    from PIL import ImageOps
                    cleaned = ImageOps.exif_transpose(candidate).convert('RGB')
                    cleaned.thumbnail((2400, 2400))
                    encoded = io.BytesIO()
                    cleaned.save(encoded, format='JPEG', quality=88)
        except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning):
            raise MobileError('invalid_image', '有効な写真ファイルを選択してください。') from None
        upload_directory = private_photo_directory
        upload_directory.mkdir(parents=True, exist_ok=True)
        filename = secrets.token_hex(20) + '.jpg'
        destination = upload_directory / filename
        relative = 'uploads/mobile/' + filename
        try:
            with destination.open('xb') as handle:
                handle.write(encoded.getvalue())
            with database() as (connection, cursor):
                execute(cursor, '''UPDATE merchandise SET photo_path = ?, additional_photos = ?, updated_by = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?''', (relative, json.dumps(existing_paths) if existing_paths else None, g.mobile_user.id, item_id))
                connection.commit()
        except Exception:
            destination.unlink(missing_ok=True)
            raise
        # Keep the old photo: it may also be referenced by existing business documents.
        return jsonify(item=serialize_item(item_by_id(item_id), detailed=True))

    @api.get('/dashboard')
    @login_required
    def dashboard():
        condition, parameters = scope_clause()
        with database() as (_, cursor):
            execute(cursor, f'SELECT m.* FROM merchandise m WHERE {condition} ORDER BY m.id DESC', parameters)
            rows = many(cursor)
        summary = dict(inventory_count=0, inventory_value=0, sold_count=0, total_sales=0,
                       total_profit=0, current_month_sales=0, current_month_profit=0)
        current_month = runtime.get_jst_now().strftime('%Y-%m')
        for row in rows:
            item = decorate(row)
            if item.get('is_sold'):
                summary['sold_count'] += 1
                sale = int(item.get('display_sale_price') or 0)
                profit = int(item.get('display_profit', item.get('profit', 0)) or 0)
                summary['total_sales'] += sale
                summary['total_profit'] += profit
                if str(item.get('sale_date') or '').startswith(current_month):
                    summary['current_month_sales'] += sale
                    summary['current_month_profit'] += profit
            else:
                summary['inventory_count'] += 1
                summary['inventory_value'] += int(item.get('display_purchase_price') or 0)
        summary['unread_count'] = sum(not row.get('is_read') for row in announcements())
        summary['recent_items'] = [serialize_item(row) for row in rows[:5]]
        summary['scope'] = 'all' if g.mobile_user.is_admin() else 'mine'
        # Inventory visibility does not imply the optional analytics permission.
        if not capabilities(g.mobile_user)['can_view_analytics']:
            for key in ('total_sales', 'total_profit', 'current_month_sales', 'current_month_profit'):
                summary[key] = None
        return jsonify(summary)

    @api.get('/announcements')
    @login_required
    def list_announcements():
        rows = announcements()
        return jsonify(announcements=rows, unread_count=sum(not row.get('is_read') for row in rows))

    @api.post('/announcements/<int:announcement_id>/read')
    @login_required
    def read_announcement(announcement_id):
        if g.mobile_user.is_admin():
            raise MobileError('forbidden', 'クライアントのみ利用できます。', 403)
        visible = set(runtime.get_visible_announcement_ids_for_user(g.mobile_user))
        if announcement_id not in visible:
            raise MobileError('not_found', 'お知らせが見つかりません。', 404)
        runtime.mark_announcements_read_for_user(g.mobile_user.id, [announcement_id])
        return jsonify(success=True, unread_count=sum(not row.get('is_read') for row in announcements()))

    app.register_blueprint(api)
    return True
