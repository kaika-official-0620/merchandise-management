"""Photo-only staff edits for received, linked Kaika stock.

Uses the caller's existing image validator and the registry operation ledger.
No migration, capture timestamp, work record, fee or lifecycle event is created.
"""
from contextlib import contextmanager
import hashlib
import hmac
import json
import secrets

from flask import Blueprint, abort, flash, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from werkzeug.exceptions import HTTPException

from item_registry import Actor, Registry, RegistryError
from stage2_self_management import enabled, photo_summary, record_item_photos, reference_paths, require_schema


MAX_REQUEST_BYTES = 40 * 1024 * 1024
FORM_FIELDS = {'staff_photo_csrf', 'operation_token'}


class StaffPhotos:
    def __init__(self, runtime):
        self.runtime, self.app = runtime, runtime.app
        self.postgres = bool(runtime.DATABASE_URL)
        self.signer = URLSafeTimedSerializer(self.app.secret_key, salt='kaika-stage2-staff-photos-v1')

    @contextmanager
    def db(self, write=False):
        conn = self.runtime.get_db()
        try:
            if not self.postgres:
                conn.execute('PRAGMA foreign_keys=ON')
                if write:
                    conn.execute('BEGIN IMMEDIATE')
            registry = Registry(conn, 'postgres' if self.postgres else 'sqlite')
            cur = conn.cursor()
            try:
                require_schema(cur, self.postgres)
            finally:
                cur.close()
            yield registry
            if write:
                conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def actor(self):
        return Actor(int(current_user.get_id()), staff=True)

    def target(self, registry, item_id, lock=False):
        actor = self.actor()
        registry.actor(actor, staff_required=True)
        links = registry.query('SELECT merchandise_id FROM business_item_links WHERE physical_item_id=?', (item_id,))
        if not links:
            abort(409, description='先に既存の商品記録との対応を確認してください。')
        if len(links) != 1:
            abort(409, description='同じ個品への商品記録が複数あります。写真の保存先を照合してから再開してください。')
        suffix = ' FOR UPDATE' if lock and self.postgres else ''
        # The legacy workflow locks merchandise first, then the registry item.
        # Keep that order to avoid a reverse lock order during receipt/return.
        rows = registry.query('SELECT * FROM merchandise WHERE id=?' + suffix, (links[0]['merchandise_id'],))
        if not rows:
            abort(404)
        if lock and self.postgres:
            registry.query('SELECT physical_item_id FROM business_item_profiles WHERE physical_item_id=? FOR UPDATE', (item_id,))
        detail = registry.detail(actor, item_id)
        row = rows[0]
        if (row.get('scope') != 'user' or row.get('user_id') != detail['owner_user_id']
                or row.get('custody_location') != 'kaika' or detail['custodian'] != 'kaika'
                or detail['physical_state'] != 'received' or detail['held']
                or detail['sales_state'] in ('sold', 'shipped') or row.get('sale_date') or row.get('is_shipped')):
            abort(409, description='現在の所有者・開花での受領・保留状況を確認してください。自己保管、売却済み、返送中の商品はこの画面で変更できません。')
        return row, detail

    @staticmethod
    def snapshot(row, detail):
        value = {'row': {key: row.get(key) for key in ('id','user_id','scope','custody_location','custody_version',
                 'photo_path','additional_photos','updated_at','updated_by','sale_date','is_shipped')},
                 'registry': {key: detail.get(key) for key in ('physical_item_id','owner_user_id','custodian',
                 'physical_state','sales_state','held','received_at','registration_completed_at')}}
        return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()

    def csrf(self):
        actor = str(current_user.get_id())
        if session.get('_staff_photo_actor') != actor or not session.get('_staff_photo_csrf'):
            session['_staff_photo_actor'] = actor
            session['_staff_photo_csrf'] = secrets.token_urlsafe(32)
        return session['_staff_photo_csrf']

    def token(self, item_id, snapshot):
        return self.signer.dumps({'actor': str(current_user.get_id()), 'item': item_id,
                                  'snapshot': snapshot, 'nonce': secrets.token_hex(16)})

    def verify(self, item_id):
        expected = session.get('_staff_photo_csrf', '')
        if (not expected or session.get('_staff_photo_actor') != str(current_user.get_id())
                or not hmac.compare_digest(expected, request.form.get('staff_photo_csrf', ''))):
            abort(400, description='画面を開き直してから写真を追加してください。')
        token = request.form.get('operation_token', '')
        try:
            value = self.signer.loads(token, max_age=4*60*60)
        except (BadSignature, SignatureExpired):
            abort(400, description='操作の有効期限が切れました。画面を開き直してください。')
        if not isinstance(value, dict) or value.get('actor') != str(current_user.get_id()) or value.get('item') != item_id:
            abort(403)
        return value, 'staff-photo:' + hashlib.sha256(token.encode()).hexdigest()

    def manage(self, item_id):
        if request.method == 'GET':
            with self.db() as registry:
                row, detail = self.target(registry, item_id)
                cur = registry.connection.cursor()
                try:
                    usage = photo_summary(cur, row['id'], row['user_id'], self.postgres)
                finally:
                    cur.close()
            paths = reference_paths(row)
            return render_template('stage2_staff_photos.html', item=detail, product=row, paths=paths,
                photo_usage=usage, staff_photo_csrf=self.csrf(),
                operation_token=self.token(item_id, self.snapshot(row, detail)))

        if request.content_length is None:
            abort(411, description='写真を通常のアップロード画面から送信してください。')
        if request.content_length > MAX_REQUEST_BYTES:
            abort(413, description='写真を含む送信全体は40MB以下にしてください。')
        if set(request.form) != FORM_FIELDS or set(request.files) != {'photos'} or any(len(request.form.getlist(key)) != 1 for key in FORM_FIELDS):
            abort(400, description='この画面は写真の追加専用です。商品情報や料金は変更できません。')
        signed, key = self.verify(item_id)
        files = request.files.getlist('photos')
        if not files or len(files) > 20 or any(not file.filename for file in files):
            abort(400, description='追加する写真を1〜20枚選択してください。')
        digests, total = [], 0
        for file in files:
            digest = hashlib.sha256()
            while True:
                chunk = file.stream.read(65536)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_REQUEST_BYTES:
                    abort(413)
                digest.update(chunk)
            file.stream.seek(0)
            digests.append(digest.hexdigest())
        request_hash = hashlib.sha256(json.dumps({'item':item_id, 'files':digests}, sort_keys=True).encode()).hexdigest()
        created = []
        try:
            with self.db(write=True) as registry:
                row, detail = self.target(registry, item_id, lock=True)
                prior = registry.query('SELECT actor_user_id,request_hash FROM business_registry_operations WHERE source_key=?', (key,))
                if prior:
                    if prior[0]['actor_user_id'] != int(current_user.get_id()) or prior[0]['request_hash'] != request_hash:
                        abort(409, description='同じ操作情報で別の写真が送信されました。画面を開き直してください。')
                    return redirect(url_for('stage2_staff_photos.manage', item_id=item_id))
                if signed.get('snapshot') != self.snapshot(row, detail):
                    abort(409, description='別の操作で商品が更新されています。画面を開き直して写真を確認してください。')
                paths = reference_paths(row)
                if len(paths) + len(files) > 20:
                    abort(409, description='商品写真は合計20枚までです。既存写真は削除せず保持しています。')
                helper = getattr(self.runtime, 'save_validated_product_image', None)
                if not callable(helper):
                    abort(503, description='写真の保存を現在利用できません。')
                for file in files:
                    path = helper(file, prefix='staff_')
                    if not path:
                        raise ValueError('写真を保存できませんでした。')
                    created.append(path)
                all_paths = paths + created
                values = {'photo_path': all_paths[0], 'additional_photos': json.dumps(all_paths[1:]) if len(all_paths)>1 else None}
                registry.execute('''UPDATE merchandise SET photo_path=?,additional_photos=?,
                    custody_version=custody_version+1,updated_by=?,updated_at=CURRENT_TIMESTAMP WHERE id=?''',
                    (values['photo_path'], values['additional_photos'], int(current_user.get_id()), row['id']))
                cur = registry.connection.cursor()
                try:
                    usage = record_item_photos(cur, row['id'], row['user_id'], values, self.app, self.postgres)
                finally:
                    cur.close()
                registry.execute('''INSERT INTO business_registry_operations
                    (source_key,actor_user_id,request_hash,result_json) VALUES(?,?,?,?)''',
                    (key, int(current_user.get_id()), request_hash, json.dumps({'action':'staff_photo_add',
                    'physical_item_id':item_id, 'merchandise_id':row['id'], 'added_count':len(created),
                    'photo_usage':usage}, ensure_ascii=False)))
            flash('写真を追加しました。撮影日時・作業記録・料金は別途確認して記録してください。', 'success')
            return redirect(url_for('stage2_staff_photos.manage', item_id=item_id))
        except Exception:
            cleanup = getattr(self.runtime, 'remove_uploaded_relative_paths', None)
            if created and callable(cleanup):
                cleanup(created)
            raise


def register_stage2_staff_photos(runtime):
    app = runtime.app
    if 'stage2_staff_photos' in app.extensions:
        return app.extensions['stage2_staff_photos']
    service = StaffPhotos(runtime)
    bp = Blueprint('stage2_staff_photos', __name__)

    @bp.route('/inventory/items/<item_id>/photos', methods=['GET','POST'])
    @login_required
    def manage(item_id):
        if not enabled(app):
            abort(404)
        if not current_user.is_admin():
            abort(403)
        try:
            return service.manage(item_id)
        except HTTPException:
            raise
        except RegistryError as error:
            abort(error.status, description='商品の権限・所有者・保管状態を確認してください。')
        except ValueError as error:
            abort(400, description=str(error))
        except Exception:
            app.logger.exception('Stage 2 staff photo update failed')
            abort(503, description='写真の保存を完了できませんでした。内容を確認して再度お試しください。')

    @bp.after_request
    def private(response):
        response.headers['Cache-Control'] = 'private, no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        return response

    app.register_blueprint(bp)
    app.extensions['stage2_staff_photos'] = service
    return service
