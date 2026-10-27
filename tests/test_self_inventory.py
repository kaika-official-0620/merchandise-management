"""Self inventory safety checks against fresh, temporary SQLite fixtures only.

Never import app/render_app: selected image and workflow functions are parsed
from app.py, then compiled alone with fixture globals. PostgreSQL checks record
SQL only; no real PostgreSQL concurrency is exercised. No scheduler, network,
live DB or application initializer can run from this suite.
"""
import ast
import copy
from functools import wraps
import html
import io
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid
import warnings

from flask import Flask, abort, flash, redirect, request, url_for
from flask_login import LoginManager, UserMixin, current_user
from jinja2 import ChoiceLoader, DictLoader
from PIL import Image

from self_inventory import register_self_inventory


ROOT = Path(__file__).resolve().parents[1]
IMAGE_FUNCTIONS = {
    "ProductImageValidationError", "normalize_product_image_bytes", "read_limited_product_image",
    "save_validated_product_image", "remove_uploaded_relative_paths",
}


class User(UserMixin):
    def __init__(self, row):
        self.__dict__.update(dict(row))

    def can_edit_merchandise(self):
        return self.subscription_status != "past_due"

    def is_admin(self):
        return self.role in {"admin", "owner"}


class Plans:
    enabled = True

    def has_feature(self, feature, user=None):
        return feature == "inventory_manage" and bool((user or current_user).entitled)

    def require(self, feature):
        def decorate(view):
            @wraps(view)
            def wrapped(*args, **kwargs):
                if not self.has_feature(feature):
                    abort(403)
                return view(*args, **kwargs)
            return wrapped
        return decorate

    def csrf_token(self):
        return "fixture-csrf"

    def check_csrf(self):
        from flask import request
        if request.form.get("csrf_token") != self.csrf_token():
            abort(400)


class SelfInventoryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tree = ast.parse((ROOT / "app.py").read_text(encoding="utf-8-sig"))
        selected = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in IMAGE_FUNCTIONS]
        assert {node.name for node in selected} == IMAGE_FUNCTIONS
        cls.image_code = compile(ast.Module(body=selected, type_ignores=[]), "fixture-selected-image-helpers", "exec")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="kaika-self-inventory-fixture-")
        self.path = Path(self.temp.name) / "fixture.db"
        self.static = Path(self.temp.name) / "static"
        self.uploads = self.static / "uploads"
        self.uploads.mkdir(parents=True)
        self.connections = []
        self.app = Flask(__name__, template_folder=str(ROOT / "templates"), static_folder=str(self.static))
        self.app.config.update(TESTING=True, SECRET_KEY="fixture-only-not-live", UPLOAD_FOLDER=str(self.uploads))
        self.app.jinja_loader = ChoiceLoader([
            DictLoader({"base.html": "<!doctype html><html lang=ja><head><title>{% block title %}{% endblock %}</title></head><body>{% block content %}{% endblock %}</body></html>"}),
            self.app.jinja_loader,
        ])
        login = LoginManager(self.app)

        @login.user_loader
        def load_user(user_id):
            with self.db() as conn:
                row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
            return User(row) if row else None

        @self.app.get("/")
        def index():
            return "fixture inventory"

        @self.app.get("/view/<int:id>")
        def view_item(id):
            return f"fixture item {id}"

        self.plans = Plans()
        self.app.extensions["kaika_feature_plans"] = self.plans
        image_globals = dict(io=io, warnings=warnings, re=re, uuid=uuid, os=os, app=self.app,
                             GOOGLE_DRIVE_IMAGE_MAX_BYTES=1024 * 1024, PRODUCT_IMAGE_MAX_PIXELS=40_000_000,
                             PRODUCT_IMAGE_MAX_DIMENSION=1200, PRODUCT_IMAGE_JPEG_QUALITY=85,
                             GOOGLE_DRIVE_ALLOWED_MIME_TYPES={"image/jpeg": ("JPEG", ".jpg"), "image/png": ("PNG", ".png"), "image/webp": ("WEBP", ".webp")})
        exec(self.image_code, image_globals)
        self.runtime = SimpleNamespace(app=self.app, DATABASE_URL=None, get_db=self.db,
                                       save_validated_product_image=image_globals["save_validated_product_image"],
                                       remove_uploaded_relative_paths=image_globals["remove_uploaded_relative_paths"])
        with self.db() as conn:
            conn.executescript("""
                CREATE TABLE users (id INTEGER PRIMARY KEY, role TEXT, entitled INTEGER, subscription_status TEXT);
                INSERT INTO users VALUES (1,'user',1,'active'), (2,'user',1,'active'),
                    (3,'admin',1,'active'), (4,'user',0,'active'), (5,'user',1,'past_due');
                CREATE TABLE merchandise (
                    id INTEGER PRIMARY KEY, user_id INTEGER, scope TEXT, product_name VARCHAR(200),
                    brand_name VARCHAR(100), model_number VARCHAR(100), item_condition VARCHAR(10),
                    store_name VARCHAR(200), supplier_detail VARCHAR(50), payment_method VARCHAR(50),
                    notes TEXT, purchase_date DATE, purchase_price INTEGER DEFAULT 0,
                    wholesale_price INTEGER DEFAULT 0, wholesale_fee_rate INTEGER DEFAULT 0,
                    listing_price INTEGER DEFAULT 0, expected_shipping INTEGER DEFAULT 0,
                    expected_commission INTEGER DEFAULT 0, photo_path TEXT, additional_photos TEXT,
                    is_listed BOOLEAN DEFAULT FALSE, is_shipped BOOLEAN DEFAULT FALSE,
                    sale_date DATE, sale_type VARCHAR(50), sale_price INTEGER DEFAULT 0,
                    shipping_cost INTEGER DEFAULT 0, commission INTEGER DEFAULT 0,
                    sales_destination TEXT, updated_by INTEGER, updated_at TIMESTAMP,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
                CREATE TABLE sale_requests (id INTEGER PRIMARY KEY, merchandise_id INTEGER, status TEXT);
                CREATE TABLE item_disposal_requests (id INTEGER PRIMARY KEY, merchandise_id INTEGER, status TEXT);
                CREATE TABLE sales_agency_requests (id INTEGER PRIMARY KEY, user_id INTEGER, service_type TEXT, status TEXT);
                CREATE TABLE sales_agency_request_items (id INTEGER PRIMARY KEY, request_id INTEGER, merchandise_id INTEGER);
                INSERT INTO merchandise (id,user_id,scope,product_name,purchase_price)
                    VALUES (100,1,'user','管理者から割当済の商品',2000), (101,2,'user','他の利用者の商品',3000),
                           (102,3,'admin','開花商品',4000);
            """)
        self.assertTrue(register_self_inventory(self.runtime))
        self.service = self.app.extensions["kaika_self_inventory"]
        self.client = self.app.test_client()
        self.login()

    def tearDown(self):
        for conn in self.connections:
            conn.close()
        self.temp.cleanup()

    def db(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        self.connections.append(conn)
        return conn

    def login(self, user_id=1):
        with self.client.session_transaction() as session:
            session["_user_id"] = str(user_id)
            session["_fresh"] = True

    def form(self, item_id=None):
        url = f"/inventory/self/{item_id}/edit" if item_id else "/inventory/self/new"
        result = self.client.get(url)
        self.assertEqual(result.status_code, 200)
        match = re.search(r'name="submission_token" value="([^"]+)"', result.get_data(as_text=True))
        return url, {"submission_token": html.unescape(match[1]), "csrf_token": "fixture-csrf",
                     "product_name": "テストバッグ", "brand_name": "Brand", "purchase_price": "1500",
                     "listing_price": "2400", "purchase_date": "2026-09-10", "notes": "元のメモ"}

    def create(self, extra=None):
        url, data = self.form()
        if extra:
            data.update(extra)
        result = self.client.post(url, data=data)
        self.assertEqual(result.status_code, 302, result.get_data(as_text=True))
        return int(result.location.rsplit("/", 1)[1])

    def record(self, item_id):
        with self.db() as conn:
            return dict(conn.execute("SELECT * FROM merchandise WHERE id=?", (item_id,)).fetchone())

    @staticmethod
    def png(name="product.png"):
        content = io.BytesIO()
        Image.new("RGB", (8, 8), "blue").save(content, format="PNG")
        content.seek(0)
        return content, name

    def test_disabled_register_does_not_touch_database_or_routes(self):
        other = Flask("disabled-fixture")
        plan = Plans()
        plan.enabled = False
        other.extensions["kaika_feature_plans"] = plan
        def forbidden():
            self.fail("disabled registration opened DB")
        runtime = SimpleNamespace(app=other, get_db=forbidden)
        self.assertFalse(register_self_inventory(runtime))
        self.assertNotIn("self_inventory_new", other.view_functions)

    def test_registration_itself_does_not_create_schema(self):
        with self.db() as conn:
            self.assertIsNone(conn.execute("SELECT name FROM sqlite_master WHERE name='self_inventory_items'").fetchone())

    def test_login_subscription_payment_and_role_are_enforced(self):
        anonymous = self.app.test_client()
        self.assertEqual(anonymous.get("/inventory/self/new").status_code, 401)
        for user_id in (3, 4, 5):
            self.login(user_id)
            self.assertEqual(self.client.get("/inventory/self/new").status_code, 403)

    def test_self_registration_fixes_owner_scope_and_workflow_defaults(self):
        item_id = self.create()
        row = self.record(item_id)
        self.assertEqual((row["user_id"], row["scope"], row["purchase_price"]), (1, "user", 1500))
        self.assertEqual((row["wholesale_price"], row["sale_price"], row["is_shipped"], row["is_listed"]), (0, 0, 0, 0))
        self.assertEqual(row["sale_type"], "normal")
        self.assertIsNone(row["sale_date"])
        with self.db() as conn:
            self.assertEqual(conn.execute("SELECT user_id FROM self_inventory_items WHERE merchandise_id=?", (item_id,)).fetchone()[0], 1)

    def test_sensitive_fields_and_existing_paths_cannot_be_posted(self):
        for key, value in {"user_id": "2", "scope": "admin", "sale_date": "2026-09-10", "is_shipped": "1",
                           "sale_type": "wholesale", "commission": "1", "kaika_product_code": "KA-001",
                           "photo_path": "uploads/other.png", "google_drive_photo_path": "uploads/other.png"}.items():
            with self.subTest(key=key):
                url, data = self.form()
                data[key] = value
                self.assertEqual(self.client.post(url, data=data).status_code, 400)
        with self.db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM merchandise").fetchone()[0], 3)

    def test_admin_assigned_and_other_users_records_are_not_editable(self):
        for item_id in (100, 101, 102):
            self.assertEqual(self.client.get(f"/inventory/self/{item_id}/edit").status_code, 404)
        own = self.create()
        url, data = self.form(own)
        self.login(2)
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.client.post(url, data=data).status_code, 400)
        self.assertEqual(self.record(own)["product_name"], "テストバッグ")

    def test_self_editor_updates_only_basic_and_expected_values(self):
        item_id = self.create()
        url, data = self.form(item_id)
        data.update(product_name="変更したバッグ", purchase_price="1800", expected_shipping="500", notes="新しいメモ")
        self.assertEqual(self.client.post(url, data=data).status_code, 302)
        row = self.record(item_id)
        self.assertEqual((row["product_name"], row["purchase_price"], row["expected_shipping"]), ("変更したバッグ", 1800, 500))
        self.assertEqual((row["is_listed"], row["is_shipped"], row["sale_price"]), (0, 0, 0))
        self.assertEqual(row["updated_by"], 1)

    def test_csrf_and_signed_submission_are_required(self):
        for key in ("csrf_token", "submission_token"):
            url, data = self.form()
            data[key] = "forged"
            self.assertEqual(self.client.post(url, data=data).status_code, 400)

    def test_replayed_create_does_not_duplicate_merchandise(self):
        url, data = self.form()
        first = self.client.post(url, data=data)
        second = self.client.post(url, data=data)
        self.assertEqual(first.location, second.location)
        with self.db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM self_inventory_items").fetchone()[0], 1)

    def test_stale_edit_is_rejected(self):
        item_id = self.create()
        url, first = self.form(item_id)
        _, stale = self.form(item_id)
        first["product_name"] = "先に保存した商品名"
        stale["product_name"] = "古い画面の商品名"
        self.assertEqual(self.client.post(url, data=first).status_code, 302)
        self.assertEqual(self.client.post(url, data=stale).status_code, 409)
        self.assertEqual(self.record(item_id)["product_name"], first["product_name"])

    def test_admin_edit_after_form_open_cannot_be_overwritten(self):
        item_id = self.create()
        url, data = self.form(item_id)
        with self.db() as conn:
            conn.execute("UPDATE merchandise SET notes='管理者が更新したメモ',updated_by=3 WHERE id=?", (item_id,))
        self.assertEqual(self.client.post(url, data=data).status_code, 409)
        self.assertEqual(self.record(item_id)["notes"], "管理者が更新したメモ")

    def test_pending_completed_and_unknown_workflows_prevent_edits(self):
        for table in ("sale_requests", "item_disposal_requests", "sales_agency_requests"):
            for status in ("pending", "approved", "completed", "future_state"):
                with self.subTest(table=table, status=status):
                    item_id = self.create()
                    url, data = self.form(item_id)
                    with self.db() as conn:
                        if table == "sales_agency_requests":
                            req = conn.execute("INSERT INTO sales_agency_requests (user_id,status) VALUES (1,?)", (status,)).lastrowid
                            conn.execute("INSERT INTO sales_agency_request_items (request_id,merchandise_id) VALUES (?,?)", (req, item_id))
                        else:
                            conn.execute(f"INSERT INTO {table} (merchandise_id,status) VALUES (?,?)", (item_id, status))
                    self.assertEqual(self.client.post(url, data=data).status_code, 409)
                    page = self.client.get(url).get_data(as_text=True)
                    self.assertIn("この画面で編集できません", page)
                    self.assertNotIn('type="submit"', page)

    def test_cancelled_request_can_be_corrected(self):
        item_id = self.create()
        with self.db() as conn:
            conn.execute("INSERT INTO sale_requests (merchandise_id,status) VALUES (?,'cancelled')", (item_id,))
        url, data = self.form(item_id)
        data["product_name"] = "再申請前の修正"
        self.assertEqual(self.client.post(url, data=data).status_code, 302)

    def test_sale_or_shipping_state_changed_after_form_open_blocks_submit(self):
        for field, value in (("sale_date", "2026-09-10"), ("is_shipped", 1), ("sale_price", 2000)):
            with self.subTest(field=field):
                item_id = self.create()
                url, data = self.form(item_id)
                with self.db() as conn:
                    conn.execute(f"UPDATE merchandise SET {field}=? WHERE id=?", (value, item_id))
                self.assertEqual(self.client.post(url, data=data).status_code, 409)

    def test_input_validation_retains_edit_context_and_entered_values(self):
        item_id = self.create()
        for field, value in (("purchase_price", "-1"), ("purchase_price", "NaN"),
                             ("purchase_price", "1000000001"), ("purchase_date", "2026-02-30"),
                             ("supplier_detail", "x" * 51), ("product_name", "")):
            url, data = self.form(item_id)
            data.update(notes="残したい入力", **{field: value})
            response = self.client.post(url, data=data)
            self.assertEqual(response.status_code, 400)
            page = response.get_data(as_text=True)
            self.assertIn("商品情報の編集", page)
            self.assertIn("残したい入力", page)
        self.assertEqual(self.record(item_id)["notes"], "元のメモ")

    def test_real_image_validator_normalizes_and_saves_under_fixture_uploads(self):
        item_id = self.create({"photo": self.png("../../unsafe-name.exe")})
        path = self.record(item_id)["photo_path"]
        self.assertRegex(path, r"^uploads/self_[0-9a-f]{32}\.png$")
        with Image.open(self.static / path) as photo:
            self.assertEqual(photo.format, "PNG")

    def test_bad_second_image_rolls_back_record_and_first_image(self):
        url, data = self.form()
        data.update(photo=self.png(), additional_photos=[(io.BytesIO(b"<script>bad</script>"), "fake.png")])
        self.assertEqual(self.client.post(url, data=data).status_code, 400)
        self.assertEqual(list(self.uploads.iterdir()), [])
        with self.db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM merchandise").fetchone()[0], 3)

    def test_photo_count_and_forged_photo_removal_are_rejected(self):
        item_id = self.create({"photo": self.png(), "additional_photos": [self.png("second.png")]})
        original = self.record(item_id)
        url, data = self.form(item_id)
        data["remove_additional"] = "../../other"
        self.assertEqual(self.client.post(url, data=data).status_code, 400)
        self.assertEqual(self.record(item_id)["additional_photos"], original["additional_photos"])
        url, data = self.form(item_id)
        data["additional_photos"] = [self.png(f"{index}.png") for index in range(20)]
        self.assertEqual(self.client.post(url, data=data).status_code, 400)
        self.assertEqual(len(list(self.uploads.iterdir())), 2)

    def test_real_feature_plans_create_edit_and_expiry(self):
        """Exercise the real plan decorator, database snapshot and CSRF service."""
        from feature_plans import register_feature_plans

        app = Flask("real-plans-self-inventory-fixture", template_folder=str(ROOT / "templates"))
        app.config.update(TESTING=True, SECRET_KEY="real-plan-fixture-only")
        app.jinja_loader = self.app.jinja_loader
        app.add_url_rule("/", endpoint="index", view_func=self.app.view_functions["index"])
        app.add_url_rule("/view/<int:id>", endpoint="view_item", view_func=self.app.view_functions["view_item"])
        login = LoginManager(app)

        @login.user_loader
        def load_user(user_id):
            with self.db() as conn:
                row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
            return User(row) if row else None

        runtime = SimpleNamespace(app=app, DATABASE_URL=None, get_db=self.db, stripe=None)
        with patch.dict(os.environ, {"FEATURE_PLANS_ENABLED": "1"}):
            plans = register_feature_plans(runtime)
        self.assertTrue(register_self_inventory(runtime))
        self.app = app
        self.client = app.test_client()
        self.login()
        self.assertEqual(self.client.get("/inventory/self/new").status_code, 403)
        now = int(time.time())
        with plans.db(write=True) as cur:
            cur.execute("""INSERT INTO feature_subscriptions
                (provider, subscription_id, user_id, plan_code, status, period_end, verified_at)
                VALUES ('stripe', 'sub_fixture', 1, 'normal', 'active', ?, ?)""", (now + 3600, now))

        def read_form(item_id=None):
            url = f"/inventory/self/{item_id}/edit" if item_id else "/inventory/self/new"
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
            page = response.get_data(as_text=True)
            data = {key: html.unescape(re.search(fr'name="{key}" value="([^"]+)"', page)[1])
                    for key in ("csrf_token", "submission_token")}
            data.update(product_name="実サービスで登録", purchase_price="1800", notes="登録時")
            return url, data

        url, data = read_form()
        self.assertNotEqual(data["csrf_token"], "fixture-csrf")
        response = self.client.post(url, data=data)
        self.assertEqual(response.status_code, 302, response.get_data(as_text=True))
        item_id = int(response.location.rsplit("/", 1)[1])
        self.assertEqual(self.record(item_id)["user_id"], 1)

        url, data = read_form(item_id)
        data.update(product_name="契約中に編集", notes="編集済み")
        self.assertEqual(self.client.post(url, data=data).status_code, 302)
        self.assertEqual(self.record(item_id)["notes"], "編集済み")
        edit_url, edit_data = read_form(item_id)
        new_url, new_data = read_form()

        with plans.db(write=True) as cur:
            cur.execute("UPDATE feature_subscriptions SET period_end=? WHERE subscription_id='sub_fixture'", (now - 1,))
        self.assertEqual(self.client.get(new_url).status_code, 403)
        self.assertEqual(self.client.get(edit_url).status_code, 403)
        # Valid forms opened before expiry cannot bypass the fresh entitlement check.
        edit_data["notes"] = "期限後の改ざん"
        self.assertEqual(self.client.post(edit_url, data=edit_data).status_code, 403)
        self.assertEqual(self.client.post(new_url, data=new_data).status_code, 403)
        self.assertEqual(self.record(item_id)["notes"], "編集済み")
        with self.db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM self_inventory_items").fetchone()[0], 1)


class WorkflowLockSqlTest(unittest.TestCase):
    """Run AST-extracted existing handlers against recording cursors, never PG."""

    @classmethod
    def setUpClass(cls):
        tree = ast.parse((ROOT / "app.py").read_text(encoding="utf-8-sig"))
        names = {"fetch_sales_agency_selected_merchandise_snapshots", "sales_agency_row_value",
                 "sales_agency_placeholder", "submit_sale_request"}
        functions = [copy.deepcopy(node) for node in tree.body
                     if isinstance(node, ast.FunctionDef) and node.name in names]
        assert {node.name for node in functions} == names
        for node in functions:
            node.decorator_list = []
        cls.code = compile(ast.Module(body=functions, type_ignores=[]), "fixture-selected-workflow-handlers", "exec")
        cls.snapshot_columns = next(ast.literal_eval(node.value) for node in tree.body
            if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and
                target.id == "SALES_AGENCY_ITEM_SNAPSHOT_COLUMNS" for target in node.targets))

    class Cursor:
        def __init__(self, rows=()):
            self.calls = []
            self.description = [(key,) for key in ("id", "product_name")]
            self.return_rows = rows

        def execute(self, sql, args=()):
            self.calls.append((" ".join(sql.split()), args))

        def fetchall(self):
            return self.return_rows

        def fetchone(self):
            return None

        def close(self):
            pass

    def test_dealer_snapshot_locks_in_id_order_and_preserves_ownership_filter(self):
        for postgres in (False, True):
            with self.subTest(postgres=postgres):
                namespace = dict(DATABASE_URL="fixture-postgres" if postgres else None,
                    SALES_AGENCY_ITEM_SNAPSHOT_COLUMNS=self.snapshot_columns,
                    sales_agency_column_exists=lambda cur, table, column: column == "product_name")
                exec(self.code, namespace)
                cur = self.Cursor([(11, "First"), (27, "Second")])
                snapshots = namespace["fetch_sales_agency_selected_merchandise_snapshots"](cur, 7, [27, 11])
                self.assertEqual(snapshots, {11: {"id": 11, "product_name": "First"},
                                            27: {"id": 27, "product_name": "Second"}})
                mark = "%s" if postgres else "?"
                suffix = " FOR UPDATE" if postgres else ""
                self.assertEqual(cur.calls, [(f"SELECT id, product_name FROM merchandise WHERE user_id = {mark} "
                    f"AND sale_date IS NULL AND id IN ({mark},{mark}) ORDER BY id{suffix}", (7, 27, 11))])
                cur.calls.clear()
                self.assertEqual(namespace["fetch_sales_agency_selected_merchandise_snapshots"](cur, 7, []), {})
                self.assertEqual(cur.calls, [])

    def test_shipping_request_locks_only_the_requested_owners_merchandise(self):
        app = Flask("workflow-query-fixture")
        app.secret_key = "query-fixture-only"
        app.add_url_rule("/", endpoint="index", view_func=lambda: "fixture inventory")
        for postgres in (False, True):
            with self.subTest(postgres=postgres):
                cur = self.Cursor()
                conn = SimpleNamespace(cursor=lambda **kwargs: cur, close=lambda: None)
                namespace = dict(DATABASE_URL="fixture-postgres" if postgres else None,
                    get_db=lambda: conn, ensure_sale_request_financial_columns=lambda conn: None,
                    RealDictCursor=object(), current_user=SimpleNamespace(id=7),
                    normalize_sale_request_type=lambda value: "shipping_request",
                    request=request, flash=flash, redirect=redirect, url_for=url_for)
                exec(self.code, namespace)
                with app.test_request_context("/sale-request/submit/27", method="POST",
                                              data={"request_type": "shipping_request"}):
                    response = namespace["submit_sale_request"](27)
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response.location, "/")
                mark = "%s" if postgres else "?"
                suffix = " FOR UPDATE" if postgres else ""
                self.assertEqual(cur.calls, [(f"SELECT * FROM merchandise WHERE id = {mark} "
                                              f"AND user_id = {mark}{suffix}", (27, 7))])


if __name__ == "__main__":
    unittest.main()
