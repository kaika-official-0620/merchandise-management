"""Opt-in self-owned inventory, using the existing site's records and templates.

Importing/registering this module never opens the application's database. Schema
creation is lazy and confined to enabled, authenticated requests. Existing items
are not enrolled; only records created through this module may be edited here.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date
import hashlib
import json
import re
import uuid

from flask import abort, flash, g, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from werkzeug.exceptions import HTTPException


TEXT_LIMITS = {
    "product_name": 200, "brand_name": 100, "model_number": 100,
    "item_condition": 10, "store_name": 200, "supplier_detail": 50,
    "payment_method": 50, "notes": 10000,
}
MONEY_FIELDS = ("purchase_price", "listing_price", "expected_shipping", "expected_commission")
FORM_FIELDS = set(TEXT_LIMITS) | set(MONEY_FIELDS) | {
    "purchase_date", "csrf_token", "submission_token", "remove_photo", "remove_additional",
    "continue_register",
}
FILE_FIELDS = {"photo", "additional_photos"}
MAX_REQUEST_BYTES = 40 * 1024 * 1024


def _row(row, cursor):
    if row is None:
        return None
    if hasattr(row, "keys"):
        return dict(row)
    return dict(zip((column[0] for column in cursor.description), row))


def _truthy(value):
    return value is True or str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _lock_submission(cursor, namespace, submission_hash):
    """Serialize a PG retry before checking its idempotency key.

    The namespace separates independent create workflows. Transaction-scoped
    locks are released by commit/rollback; no persistent lock state is stored.
    """
    digest = hashlib.sha256((namespace + ':' + submission_hash).encode('utf-8')).digest()
    key = int.from_bytes(digest[:8], byteorder='big', signed=True)
    cursor.execute('SELECT pg_advisory_xact_lock(%s)', (key,))


def _financially_locked(item):
    return bool(
        item.get("sale_date") or _truthy(item.get("is_shipped"))
        or str(item.get("item_status") or "").lower() == "sold"
        or item.get("sale_price") or item.get("shipping_cost") or item.get("commission")
        or item.get("sales_destination") or item.get("proxy_parent_item_id")
        or _truthy(item.get("show_in_proxy_service"))
    )


class SelfInventory:
    def __init__(self, runtime, plans):
        self.runtime = runtime
        self.app = runtime.app
        self.plans = plans
        self.postgres = bool(runtime.DATABASE_URL)
        self.mark = "%s" if self.postgres else "?"
        self.signer = URLSafeTimedSerializer(self.app.secret_key, salt="kaika-self-inventory-v1")

    def _ensure_schema(self, cur):
        if self.postgres:
            cur.execute("SELECT to_regclass('self_inventory_items') AS table_name")
            result = cur.fetchone()
            exists = (next(iter(result.values())) if hasattr(result, 'keys') else result[0]) if result else None
            if exists:
                return
            # CREATE IF NOT EXISTS alone can still race in PostgreSQL's
            # catalog. Serialize only first-time creation, never an existing
            # table's normal reads/writes. The next CREATE sees prior commits.
            _lock_submission(cur, 'kaika-self-inventory-schema-v1', 'self_inventory_items')
        cur.execute("""
            CREATE TABLE IF NOT EXISTS self_inventory_items (
                merchandise_id INTEGER PRIMARY KEY REFERENCES merchandise(id) ON DELETE CASCADE,
                user_id INTEGER NOT NULL REFERENCES users(id),
                submission_hash VARCHAR(64) NOT NULL UNIQUE,
                version INTEGER NOT NULL DEFAULT 1,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)

    @contextmanager
    def connection(self, write=False):
        conn = self.runtime.get_db()
        cur = conn.cursor()
        try:
            if write and not self.postgres:
                cur.execute("BEGIN IMMEDIATE")
            self._ensure_schema(cur)
            yield conn, cur
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.close()
            conn.close()

    def require_user(self):
        if current_user.role != "user":
            abort(403, description="この画面はご自身の商品を管理するための画面です。")
        if not current_user.can_edit_merchandise():
            abort(403, description="現在、商品情報の変更はご利用いただけません。")

    def _load(self, cur, item_id, lock=False):
        suffix = " FOR UPDATE OF m, si" if lock and self.postgres else ""
        cur.execute(f"""
            SELECT m.*, si.version AS self_inventory_version
            FROM merchandise m JOIN self_inventory_items si ON si.merchandise_id = m.id
            WHERE m.id = {self.mark} AND m.user_id = {self.mark}
              AND si.user_id = {self.mark} AND m.scope = 'user'{suffix}
        """, (item_id, current_user.id, current_user.id))
        item = _row(cur.fetchone(), cur)
        if not item:
            abort(404)
        return item

    def _table_exists(self, cur, table):
        if self.postgres:
            cur.execute("SELECT to_regclass(%s)", (table,))
            result = cur.fetchone()
            return bool(next(iter(result.values())) if isinstance(result, dict) else result[0])
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,))
        return cur.fetchone() is not None

    def locked(self, cur, item):
        custody = self.app.extensions.get('inventory_custody')
        own = custody and item.get('custody_location') == 'self'
        if custody and (item.get('custody_location', 'kaika') != 'self' or custody.active_for_item(cur,item['id'])):
            return True
        if _financially_locked(item) and not own:
            return True
        # Never change merchandise already used by a pending/completed workflow.
        # Rejected/cancelled requests may be corrected and submitted again.
        for table in ("sale_requests", "item_disposal_requests"):
            if self._table_exists(cur, table):
                cur.execute(f"""SELECT 1 FROM {table} WHERE merchandise_id = {self.mark}
                    AND COALESCE(status, 'pending') NOT IN ('cancelled', 'rejected', 'deal_failed') LIMIT 1""",
                    (item["id"],))
                if cur.fetchone():
                    return True
        if self._table_exists(cur, "sales_agency_request_items") and self._table_exists(cur, "sales_agency_requests"):
            cur.execute(f"""SELECT 1 FROM sales_agency_request_items sari
                JOIN sales_agency_requests sar ON sar.id = sari.request_id
                WHERE sari.merchandise_id = {self.mark}
                  AND COALESCE(sar.status, 'pending') NOT IN ('cancelled', 'rejected', 'deal_failed') LIMIT 1""",
                (item["id"],))
            if cur.fetchone():
                return True
        return False

    def can_edit(self, item):
        if not current_user.is_authenticated or current_user.role != "user" or not self.plans.has_feature("inventory_manage"):
            return False
        if not current_user.can_edit_merchandise() or not item:
            return False
        item = dict(item)
        custody = self.app.extensions.get('inventory_custody')
        if item.get("user_id") != current_user.id or item.get("scope") != "user" or (_financially_locked(item) and not (custody and item.get('custody_location') == 'self')):
            return False
        if custody and not custody.item_context(item)['can_self_manage']:
            return False
        # A single query per request even when an inventory list renders many rows.
        cache_key = "kaika_self_inventory_ids"
        if not hasattr(g, cache_key):
            with self.connection() as (_, cur):
                cur.execute(f"SELECT merchandise_id FROM self_inventory_items WHERE user_id = {self.mark}", (current_user.id,))
                ids = {_row(row, cur)["merchandise_id"] for row in cur.fetchall()}
            setattr(g, cache_key, ids)
        return item.get("id") in getattr(g, cache_key)

    def token(self, item=None):
        return self.signer.dumps({"user": current_user.id, "item": item["id"] if item else None,
                                  "version": item.get("self_inventory_version") if item else None,
                                  "snapshot": self.snapshot(item) if item else None,
                                  "nonce": uuid.uuid4().hex})

    @staticmethod
    def snapshot(item):
        # Admin routes do not know this module's version counter. Include the
        # editable record values so their updates cannot be lost to an old form.
        fields = (*TEXT_LIMITS, *MONEY_FIELDS, "purchase_date", "photo_path",
                  "additional_photos", "updated_by", "updated_at", "custody_location", "custody_version")
        payload = json.dumps({key: item.get(key) for key in fields}, sort_keys=True, default=str)
        return hashlib.sha256(payload.encode()).hexdigest()

    def check_token(self, item_id):
        try:
            data = self.signer.loads(request.form.get("submission_token", ""), max_age=4 * 60 * 60)
        except (BadSignature, SignatureExpired):
            abort(400, description="フォームの有効期限が切れました。画面を開き直してください。")
        if not isinstance(data, dict) or data.get("user") != current_user.id or data.get("item") != item_id:
            abort(400, description="このフォームは使用できません。画面を開き直してください。")
        return data

    def values(self):
        if set(request.form) - FORM_FIELDS or set(request.files) - FILE_FIELDS:
            raise ValueError("この画面では変更できない項目が含まれています。")
        for key in request.form:
            if key != "remove_additional" and len(request.form.getlist(key)) != 1:
                raise ValueError("入力内容が重複しています。画面を開き直してください。")
        if len(request.files.getlist("photo")) > 1:
            raise ValueError("メイン写真は1枚だけ選択してください。")
        values = {}
        for key, limit in TEXT_LIMITS.items():
            value = request.form.get(key, "").strip()
            if len(value) > limit or "\x00" in value:
                raise ValueError(f"入力できる文字数を超えています（{limit}文字以内）。")
            values[key] = value
        if not values["product_name"]:
            raise ValueError("商品名を入力してください。")
        for key in MONEY_FIELDS:
            raw = request.form.get(key, "").strip()
            if raw and not re.fullmatch(r"[0-9]{1,10}", raw):
                raise ValueError("金額は0以上の整数で入力してください。")
            value = int(raw or 0)
            if value > 1_000_000_000:
                raise ValueError("金額は10億円以下で入力してください。")
            values[key] = value
        raw_date = request.form.get("purchase_date", "").strip()
        if raw_date:
            try:
                if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw_date):
                    raise ValueError
                date.fromisoformat(raw_date)
            except ValueError:
                raise ValueError("仕入日を正しい日付で入力してください。") from None
        values["purchase_date"] = raw_date or None
        return values

    @staticmethod
    def photos(item):
        try:
            photos = json.loads(item.get("additional_photos") or "[]") if item else []
            return photos if isinstance(photos, list) and all(isinstance(p, str) for p in photos) else []
        except (ValueError, TypeError):
            return []

    def save_photos(self, item, created):
        main = item.get("photo_path") if item else None
        additional = self.photos(item)
        removals = request.form.getlist("remove_additional")
        if any(not re.fullmatch(r"\d{1,2}", index) or int(index) >= len(additional) for index in removals):
            raise ValueError("削除する写真が見つかりません。画面を開き直してください。")
        additional = [path for i, path in enumerate(additional) if str(i) not in removals]
        if request.form.get("remove_photo") not in (None, "", "1"):
            raise ValueError("写真の選択内容を確認してください。")
        if request.form.get("remove_photo") == "1":
            main = None
        selected_main = request.files.get("photo")
        selected_additional = [f for f in request.files.getlist("additional_photos") if f.filename]
        if len(additional) + len(selected_additional) > 19:
            raise ValueError("追加写真は合計19枚まで選択できます。")
        helper = getattr(self.runtime, "save_validated_product_image", None)
        if (selected_main and selected_main.filename) or selected_additional:
            if not callable(helper):
                raise ValueError("写真の保存を利用できません。管理者にお問い合わせください。")
        if selected_main and selected_main.filename:
            main = helper(selected_main, prefix="self_")
            if not main:
                raise ValueError("メイン写真を保存できませんでした。")
            created.append(main)
        for photo in selected_additional:
            path = helper(photo, prefix="self_")
            if not path:
                raise ValueError("追加写真を保存できませんでした。")
            created.append(path)
            additional.append(path)
        return {"photo_path": main, "additional_photos": json.dumps(additional) if additional else None}

    def form(self, item=None, error=None, locked=False, status=200):
        values = dict(item or {})
        if error and request.method == "POST":
            values.update({key: request.form.get(key, "") for key in (*TEXT_LIMITS, *MONEY_FIELDS, "purchase_date")})
        return render_template("self_inventory_form.html", item=item, values=values, error=error,
                               locked=locked, additional_photos=self.photos(item),
                               submission_token=self.token(item), csrf_token_value=self.plans.csrf_token(),
                               photos_enabled=callable(getattr(self.runtime, "save_validated_product_image", None))), status

    def handle(self, item_id=None):
        self.require_user()
        if request.content_length and request.content_length > MAX_REQUEST_BYTES:
            abort(413, description="写真を含む送信データは40MB以下にしてください。")
        item = None
        if request.method == "GET":
            if item_id is None:
                return self.form()
            with self.connection() as (_, cur):
                item = self._load(cur, item_id)
                locked = self.locked(cur, item)
            return self.form(item, locked=locked)

        self.plans.check_csrf()
        token = self.check_token(item_id)
        created = []
        try:
            with self.connection(write=True) as (_, cur):
                if item_id is not None:
                    item = self._load(cur, item_id, lock=True)
                    if self.locked(cur, item):
                        abort(409, description="申請中・売却済み・発送済みの商品は、この画面で編集できません。")
                    if item["self_inventory_version"] != token.get("version") or self.snapshot(item) != token.get("snapshot"):
                        abort(409, description="別の画面で商品が更新されています。画面を開き直してください。")
                else:
                    submission_hash = hashlib.sha256(token["nonce"].encode()).hexdigest()
                    if self.postgres:
                        _lock_submission(cur, 'kaika-self-inventory-create-v1', submission_hash)
                    cur.execute(f"SELECT merchandise_id FROM self_inventory_items WHERE submission_hash = {self.mark} AND user_id = {self.mark}", (submission_hash, current_user.id))
                    previous = _row(cur.fetchone(), cur)
                    if previous:
                        return redirect(url_for("view_item", id=previous["merchandise_id"]))
                values = self.values()
                values.update(self.save_photos(item, created))
                if item_id is None:
                    # Ownership, inventory scope, and workflow states are server-owned.
                    values.update(user_id=current_user.id, scope="user", wholesale_price=0,
                                  wholesale_fee_rate=0, sale_type="normal", is_listed=False,
                                  is_shipped=False, sale_date=None, sale_price=0,
                                  shipping_cost=0, commission=0, updated_by=current_user.id)
                    if self.app.extensions.get('inventory_custody'):
                        values['custody_location'] = 'self'
                    columns = list(values)
                    suffix = " RETURNING id" if self.postgres else ""
                    cur.execute(f"INSERT INTO merchandise ({', '.join(columns)}) VALUES ({', '.join([self.mark] * len(columns))}){suffix}", tuple(values.values()))
                    item_id = _row(cur.fetchone(), cur)["id"] if self.postgres else cur.lastrowid
                    cur.execute(f"INSERT INTO self_inventory_items (merchandise_id, user_id, submission_hash) VALUES ({self.mark}, {self.mark}, {self.mark})", (item_id, current_user.id, submission_hash))
                else:
                    values["updated_by"] = current_user.id
                    updates = ", ".join(f"{column} = {self.mark}" for column in values)
                    cur.execute(f"UPDATE merchandise SET {updates}, updated_at = CURRENT_TIMESTAMP WHERE id = {self.mark} AND user_id = {self.mark} AND scope = 'user'", (*values.values(), item_id, current_user.id))
                    cur.execute(f"UPDATE self_inventory_items SET version = version + 1, updated_at = CURRENT_TIMESTAMP WHERE merchandise_id = {self.mark} AND user_id = {self.mark}", (item_id, current_user.id))
                custody = self.app.extensions.get('inventory_custody')
                if custody:
                    custody.event(cur, 'self_created' if item is None else 'self_edited', item=item_id, user_id=current_user.id,
                                  details=json.dumps({'before': {key: item.get(key) for key in values} if item else None,
                                                      'after': values}, ensure_ascii=False, default=str))
            flash("商品を登録しました" if item is None else "商品情報を保存しました", "success")
            if item is None and request.form.get('continue_register') == '1':
                return redirect(url_for('self_inventory_new'))
            return redirect(url_for("view_item", id=item_id))
        except Exception as exc:
            cleanup = getattr(self.runtime, "remove_uploaded_relative_paths", None)
            if created and callable(cleanup):
                cleanup(created)
            if isinstance(exc, HTTPException):
                raise
            if isinstance(exc, ValueError):
                return self.form(item, error=str(exc), status=400)
            self.app.logger.exception("Self inventory save failed")
            return self.form(item, error="保存できませんでした。時間をおいて、もう一度お試しください。", status=500)


def register_self_inventory(runtime):
    app = runtime.app
    plans = app.extensions.get("kaika_feature_plans")
    if not plans or not plans.enabled:
        return False
    if "kaika_self_inventory" in app.extensions:
        return True
    service = SelfInventory(runtime, plans)
    app.extensions["kaika_self_inventory"] = service
    runtime.can_edit_self_inventory_item = service.can_edit
    app.jinja_env.globals["can_edit_self_inventory_item"] = service.can_edit
    app.add_url_rule("/inventory/self/new", endpoint="self_inventory_new",
                     view_func=login_required(plans.require("inventory_manage")(lambda: service.handle())),
                     methods=["GET", "POST"])
    app.add_url_rule("/inventory/self/<int:item_id>/edit", endpoint="self_inventory_edit",
                     view_func=login_required(plans.require("inventory_manage")(lambda item_id: service.handle(item_id))),
                     methods=["GET", "POST"])
    return True
