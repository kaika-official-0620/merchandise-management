"""Private support intake using existing inquiry records; no external delivery."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from functools import wraps
import hmac
import io
import json
from pathlib import Path
import uuid
import warnings

from flask import abort, current_app, flash, make_response, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required
from PIL import Image, ImageOps, UnidentifiedImageError


CATEGORIES = {
    'general': '使い方・操作の相談', 'inventory': '商品・在庫・預かり',
    'documents': '書類・申請', 'billing': '料金・請求・支払い',
    'technical': '不具合・エラー', 'feature': '改善の希望', 'other': 'その他',
}
DEVICES = {'': '選択してください', 'iphone': 'iPhoneアプリ', 'android': 'Androidアプリ',
           'smartphone_web': 'スマホのブラウザ', 'pc_web': 'PCのブラウザ', 'other': 'その他・不明'}
OPTIONAL_FIELDS = (
    ('reference', '関連する商品・書類の番号', 120), ('page_name', '画面・メニュー名', 200),
    ('occurred_at', '発生した日時', 80), ('steps', '行った操作', 2000),
    ('expected', '期待していた結果', 1500), ('actual', '実際の表示・結果', 2000),
)
MAX_IMAGE_BYTES = 5 * 1024 * 1024


def inquiry_jst_timestamp():
    """Explicit local wall time for existing TIMESTAMP-without-zone columns."""
    return datetime.now(timezone(timedelta(hours=9))).strftime('%Y-%m-%d %H:%M:%S')


class InquiryValidationError(ValueError):
    pass


def unavailable_response():
    """A DB failure must never reveal an exception or resemble an empty inbox."""
    current_app.logger.error('inquiry_service_unavailable')
    response = make_response('''<!doctype html><html lang="ja"><meta charset="utf-8">
        <meta name="viewport" content="width=device-width,initial-scale=1">
        <title>お問い合わせを読み込めません</title>
        <main style="max-width:42rem;margin:3rem auto;padding:1.5rem;font-family:sans-serif;color:#203b37">
        <h1>一時的にお問い合わせを読み込めません</h1>
        <p>時間をおいて、もう一度お試しください。エラーを理由に再送を繰り返さず、送信済みの履歴を確認してください。</p>
        <p><a href="/inquiry">問い合わせの履歴を再確認</a></p>
        <p><a href="/my/support">お知らせ・サポートに戻る</a></p></main></html>''', 503)
    response.headers['Cache-Control'] = 'no-store'
    return response


def prepare_inquiry(form):
    values = {key: str(form.get(key, '')).strip() for key in
              ('category', 'title', 'content', 'device', *(row[0] for row in OPTIONAL_FIELDS))}
    values['category'] = values['category'] or 'general'
    if values['category'] not in CATEGORIES or values['device'] not in DEVICES:
        raise InquiryValidationError('お問い合わせの種類・利用環境を選び直してください。')
    if not values['title'] or not values['content']:
        raise InquiryValidationError('件名と相談内容を入力してください。')
    if len(values['title']) > 160 or len(values['content']) > 10000:
        raise InquiryValidationError('件名は160文字以内、相談内容は10,000文字以内で入力してください。')
    body = [values['content']]
    for key, label, limit in OPTIONAL_FIELDS:
        if len(values[key]) > limit:
            raise InquiryValidationError(f'{label}は{limit:,}文字以内で入力してください。')
        if values[key]:
            body.append(f'【{label}】\n{values[key]}')
    if values['device']:
        body.append('【利用環境】\n' + DEVICES[values['device']])
    return values, '\n\n'.join(body)


def prepare_images(files):
    """Validate the whole batch before writing; re-encode and remove metadata."""
    files = [file for file in files if file and file.filename]
    if len(files) > 5:
        raise InquiryValidationError('画像は5枚まで添付できます。')
    prepared = []
    for file in files:
        raw = file.stream.read(MAX_IMAGE_BYTES + 1)
        if not raw or len(raw) > MAX_IMAGE_BYTES:
            raise InquiryValidationError('画像は1枚5MB以内にしてください。')
        try:
            with warnings.catch_warnings():
                warnings.simplefilter('error', Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(raw)) as image:
                    if image.format not in {'JPEG', 'PNG', 'GIF', 'WEBP'} or image.width * image.height > 25000000:
                        raise InquiryValidationError('JPEG・PNG・GIF・WebPの画像を選んでください（2,500万画素以内）。')
                    image.load()
                    image = ImageOps.exif_transpose(image)
                    image.thumbnail((1920, 1920))
                    # Flatten transparency onto white and retain only the first frame.
                    normalized = Image.new('RGB', image.size, 'white')
                    rgba = image.convert('RGBA')
                    normalized.paste(rgba, mask=rgba.getchannel('A'))
                    output = io.BytesIO()
                    normalized.save(output, format='JPEG', quality=88, optimize=True)
                    prepared.append(output.getvalue())
        except InquiryValidationError:
            raise
        except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError,
                Image.DecompressionBombWarning) as error:
            raise InquiryValidationError('読み込めない画像があります。スクリーンショットなどの画像を選び直してください。') from error
    return prepared


def register_inquiry_support(runtime):
    app = runtime.app
    if app.extensions.get('kaika_inquiry_support'):
        return
    app.extensions['kaika_inquiry_support'] = True
    runtime.INQUIRY_CATEGORIES = CATEGORIES
    runtime.INQUIRY_STATUS.update(new='受付済み', in_progress='確認・対応中', resolved='解決済み', closed='対応終了')
    mark = '%s' if runtime.DATABASE_URL else '?'

    @contextmanager
    def database():
        connection = runtime.get_db()
        cursor = connection.cursor()
        try:
            yield connection, cursor
        except Exception:
            connection.rollback()
            raise
        finally:
            cursor.close()
            connection.close()

    def form_response(values=None, status=200):
        return render_template('inquiry/form.html', categories=CATEGORIES, devices=DEVICES,
                               values=values or request.form,
                               support_guide_available='customer_navigation.guide' in app.view_functions), status

    @login_required
    def create():
        if request.method != 'POST':
            return form_response({})
        saved = []
        try:
            values, body = prepare_inquiry(request.form)
            files = request.files.getlist('images') or request.files.getlist('image')
            pictures = prepare_images(files)
            upload = Path(app.config['UPLOAD_FOLDER']) / 'inquiries'
            if pictures:
                upload.mkdir(parents=True, exist_ok=True)
            for picture in pictures:
                name = uuid.uuid4().hex + '.jpg'
                path = upload / name
                with path.open('xb') as output:
                    output.write(picture)
                saved.append(path)
            paths = json.dumps(['uploads/inquiries/' + path.name for path in saved]) if saved else None
            recorded_at = inquiry_jst_timestamp()
            with database() as (connection, cursor):
                suffix = ' RETURNING id' if runtime.DATABASE_URL else ''
                cursor.execute(f'''INSERT INTO inquiries(user_id, category, title, content, image_path, created_at, updated_at)
                    VALUES({','.join([mark]*7)}){suffix}''',
                    (current_user.id, values['category'], values['title'], body, paths, recorded_at, recorded_at))
                new_id = cursor.fetchone()[0] if runtime.DATABASE_URL else cursor.lastrowid
                connection.commit()
            saved = []  # now owned by the committed inquiry
            flash(f'受付番号 {new_id} で開花へ送信しました。返信はこの画面で確認できます。', 'success')
            return redirect(url_for('inquiry_view', id=new_id))
        except InquiryValidationError as error:
            flash(str(error), 'error')
            return form_response(status=400)
        except Exception:
            # Do not expose SQL, paths or submitted customer data in diagnostics.
            app.logger.error('inquiry_create_failed')
            flash('送信できませんでした。入力内容を確認して、もう一度お試しください。', 'error')
            return form_response(status=503)
        finally:
            for path in saved:
                path.unlink(missing_ok=True)

    app.view_functions['inquiry_new'] = create

    # The common security service protects attachments. Require its session-bound
    # CSRF token on every support mutation as well, including JSON status changes.
    @app.before_request
    def inquiry_mutation_boundary():
        if request.method != 'POST' or request.endpoint not in {
            'inquiry_new', 'inquiry_reply', 'inquiry_delete', 'admin_inquiry_reply', 'admin_inquiry_status'}:
            return
        if not current_user.is_authenticated:
            abort(401)
        if request.endpoint == 'inquiry_new' and request.content_length and request.content_length > 26 * 1024 * 1024:
            abort(413, description='添付画像は5枚まで、1枚5MB以内にしてください。')
        supplied = request.form.get('security_csrf_token') or request.headers.get('X-Security-CSRF-Token', '')
        expected = session.get('_security_csrf', '')
        if (session.get('_security_csrf_user') != str(current_user.get_id()) or not supplied or not expected
                or not hmac.compare_digest(str(expected).encode(), str(supplied).encode())):
            abort(400, description='画面を開き直してから送信してください。')
        if request.endpoint.startswith('admin_') and not current_user.is_admin():
            abort(403)
        if request.endpoint == 'admin_inquiry_status':
            data = request.get_json(silent=True)
            if not isinstance(data, dict) or data.get('status') not in runtime.INQUIRY_STATUS:
                abort(400)
            with database() as (_, cursor):
                cursor.execute(f'SELECT id FROM inquiries WHERE id={mark}', ((request.view_args or {}).get('id'),))
                if cursor.fetchone() is None:
                    abort(404)

    def reply_guard(original, staff=False):
        @wraps(original)
        def wrapped(id):
            content = request.form.get('content', '').strip()
            if not content or len(content) > 10000:
                flash('返信は1〜10,000文字で入力してください。', 'error')
                return redirect(url_for('admin_inquiry_view' if staff else 'inquiry_view', id=id))
            if staff and request.form.get('status', '') not in {'', *runtime.INQUIRY_STATUS}:
                abort(400)
            with database() as (_, cursor):
                where = f'id={mark}' + ('' if staff else f' AND user_id={mark}')
                cursor.execute(f'SELECT status FROM inquiries WHERE {where}',
                               (id,) if staff else (id, current_user.id))
                row = cursor.fetchone()
                if not row:
                    abort(404)
                status = row['status'] if hasattr(row, 'keys') else row[0]
                if status == 'closed' and not staff:
                    abort(409, description='対応終了したお問い合わせには返信できません。新しいお問い合わせをご利用ください。')
            return original(id)
        return wrapped

    for endpoint, staff in [('inquiry_reply', False), ('admin_inquiry_reply', True)]:
        if endpoint in app.view_functions:
            app.view_functions[endpoint] = reply_guard(app.view_functions[endpoint], staff)
