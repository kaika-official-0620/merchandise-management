# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import os
import sqlite3
import traceback
from datetime import date, datetime, timedelta
from typing import Any

from flask import flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user
from werkzeug.utils import secure_filename


APPRAISAL_STATUS_LABELS = {
    "none": "",
    "waiting": "査定待ち",
    "completed": "査定完了",
}

SALES_AGENCY_SERVICE_TYPES = {
    "wholesale": "業者卸販売サービス",
    "simultaneous": "同時出品サービス",
    "auction": "業者オークション出品",
}

SALES_AGENCY_STATUS = {
    "pending": "承認待ち",
    "approved": "認証済み",
    "appraising": "査定中",
    "inspecting": "検品中",
    "completed": "処理完了",
    "rejected": "却下申請",
}

SALES_AGENCY_STATUS_CLIENT = {
    "pending": "認証待ち",
    "approved": "認証済み",
    "appraising": "査定中",
    "inspecting": "検品中",
    "completed": "売却済み",
    "rejected": "却下申請",
}


def apply(module: Any) -> None:
    if getattr(module, "_runtime_patches_applied", False):
        return
    module._runtime_patches_applied = True

    app = module.app
    DATABASE_URL = module.DATABASE_URL
    RealDictCursor = getattr(module, "RealDictCursor", None)
    login_required = module.login_required
    get_db = module.get_db
    get_jst_now = getattr(module, "get_jst_now", datetime.now)
    fetch_sales_agency_request_source = getattr(module, "fetch_sales_agency_request_source", None)
    generate_admin_mitsumori_document_no = getattr(
        module,
        "generate_admin_mitsumori_document_no",
        lambda: f"MIT-{datetime.now().strftime('%Y%m%d%H%M%S')}",
    )
    generate_admin_kaitori_document_no = getattr(
        module,
        "generate_admin_kaitori_document_no",
        lambda: f"KAI-{datetime.now().strftime('%Y%m%d%H%M%S')}",
    )
    build_mitsumori_items_from_form = getattr(
        module,
        "build_mitsumori_items_from_form",
        lambda: ([], 0),
    )
    generate_password_hash = module.generate_password_hash
    get_monthly_fee = module.get_monthly_fee
    User = module.User
    send_line_push = getattr(module, "send_line_push", None)
    safe_int = getattr(module, "safe_int", None)
    clean_display_text = getattr(module, "clean_display_text", lambda value, fallback="-": (value or fallback))
    format_sales_destination = getattr(module, "format_sales_destination", None)
    is_kaika_inventory_item = getattr(module, "is_kaika_inventory_item", None)
    build_user_fee_components = getattr(module, "build_user_fee_components", None)
    get_fee_settings = getattr(module, "get_fee_settings", None)
    allowed_file = getattr(module, "allowed_file", None)
    parse_money_value = getattr(module, "parse_money_value", None)
    normalize_kaika_sale_type = getattr(module, "normalize_kaika_sale_type", None)
    resolve_kaika_sales_destination = getattr(module, "resolve_kaika_sales_destination", None)
    calculate_kaika_marketplace_fee = getattr(module, "calculate_kaika_marketplace_fee", None)
    get_item_back_url = getattr(module, "get_item_back_url", None)
    resolve_internal_back_url = getattr(module, "resolve_internal_back_url", None)
    derive_appraisal_status = getattr(module, "derive_appraisal_status", lambda status: "completed" if status == "sold" else ("waiting" if status == "listed" else "none"))
    db_boolean_param = getattr(module, "db_boolean_param", None)
    ensure_user_profile_columns = getattr(module, "ensure_user_profile_columns", lambda: None)
    get_inventory_summary_period_options = getattr(
        module,
        "get_inventory_summary_period_options",
        lambda: [
            {"key": "all", "label": "全期間"},
            {"key": "current_month", "label": "今月"},
            {"key": "previous_month", "label": "先月"},
            {"key": "last_3_months", "label": "直近3か月"},
        ],
    )
    normalize_inventory_summary_period = getattr(module, "normalize_inventory_summary_period", lambda value: (value or "all"))
    get_monthly_plan_options = getattr(module, "get_monthly_plan_options", lambda: [])
    normalize_monthly_plan_key = getattr(module, "normalize_monthly_plan_key", lambda value: (value or "").strip())
    get_monthly_plan_label = getattr(module, "get_monthly_plan_label", lambda value: "")
    resolve_plan_effective_month = getattr(module, "resolve_plan_effective_month", lambda value: "")

    if not callable(db_boolean_param):
        def db_boolean_param(value):
            normalized = bool(value)
            if DATABASE_URL:
                return normalized
            return 1 if normalized else 0

    if not callable(fetch_sales_agency_request_source):
        def fetch_sales_agency_request_source(*_args, **_kwargs):
            return None, []

    module.APPRAISAL_STATUS_LABELS = APPRAISAL_STATUS_LABELS
    module.SALES_AGENCY_SERVICE_TYPES = SALES_AGENCY_SERVICE_TYPES
    module.SALES_AGENCY_STATUS = SALES_AGENCY_STATUS
    module.SALES_AGENCY_STATUS_CLIENT = SALES_AGENCY_STATUS_CLIENT

    def ensure_admin():
        if not getattr(current_user, "is_authenticated", False):
            return redirect(url_for("login"))
        if not current_user.is_admin():
            flash("この操作を行う権限がありません。", "error")
            return redirect(url_for("index"))
        return None

    def ensure_logged_in():
        if not getattr(current_user, "is_authenticated", False):
            return redirect(url_for("login"))
        return None

    def ensure_permission(permission_key):
        denied = ensure_admin()
        if denied:
            return denied
        has_permission = getattr(current_user, "has_permission", None)
        if callable(has_permission) and not has_permission(permission_key):
            flash("この操作を実行する権限がありません。", "error")
            return redirect("/admin/home")
        return None

    def open_cursor():
        conn = get_db()
        if DATABASE_URL:
            cur = conn.cursor(cursor_factory=RealDictCursor)
        else:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
        return conn, cur

    def find_existing_user_by_login(cur, username, email):
        placeholder = "%s" if DATABASE_URL else "?"
        cur.execute(
            f"""
            SELECT id, username, email
            FROM users
            WHERE LOWER(username) = LOWER({placeholder})
               OR LOWER(email) = LOWER({placeholder})
            LIMIT 1
            """,
            (username, email),
        )
        row = cur.fetchone()
        return dict(row) if row else None

    def sync_users_id_sequence(cur):
        if not DATABASE_URL:
            return
        cur.execute("SELECT pg_get_serial_sequence('users', 'id') AS seq_name")
        row = cur.fetchone()
        seq_name = None
        if row:
            seq_name = row.get("seq_name") if isinstance(row, dict) else row[0]
        if not seq_name:
            return
        cur.execute(
            """
            SELECT setval(
                %s::regclass,
                COALESCE((SELECT MAX(id) FROM users), 0) + 1,
                false
            )
            """,
            (seq_name,),
        )

    def get_table_columns(cur, table_name):
        if DATABASE_URL:
            cur.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = current_schema()
                  AND table_name = %s
                """,
                (table_name,),
            )
            return {row["column_name"] if isinstance(row, dict) else row[0] for row in cur.fetchall()}

        cur.execute(f"PRAGMA table_info({table_name})")
        columns = set()
        for row in cur.fetchall():
            if isinstance(row, sqlite3.Row):
                columns.add(row["name"])
            else:
                columns.add(row[1])
        return columns

    def rows_to_dicts(rows):
        return [dict(row) for row in rows]

    def client_form_context(user=None):
        return {
            "user": user,
            "plan_options": get_monthly_plan_options(),
            "summary_period_options": get_inventory_summary_period_options(),
        }

    def as_timestamp_text(value):
        if not value:
            return ""
        if hasattr(value, "strftime"):
            return value.strftime("%Y-%m-%d %H:%M:%S")
        return str(value)

    def format_history_date(value):
        if not value:
            return ""
        if hasattr(value, "strftime"):
            return value.strftime("%Y-%m-%d")
        return str(value)[:10]

    def get_sales_agency_status_label(status, viewer="admin", service_type=None):
        status_key = (status or "").strip()
        service_key = (service_type or "").strip()
        if service_key == "simultaneous" and status_key == "appraising":
            return "出品準備中"
        status_map = SALES_AGENCY_STATUS_CLIENT if viewer == "client" else SALES_AGENCY_STATUS
        return status_map.get(status_key, status_key)

    def get_service_display_name(service_type):
        return SALES_AGENCY_SERVICE_TYPES.get((service_type or "").strip(), service_type or "")

    def get_document_status_label(document_kind, status):
        status_key = (status or "").strip()
        status_map = {
            "draft": "下書き",
            "completed": "完了",
            "approved": "承認済み",
            "rejected": "却下",
            "submitted": "送信済み",
            "sent": "送信済み",
        }
        if document_kind == "invoice" and status_key == "sent":
            return "送付可能"
        if document_kind == "user_mitsumori" and status_key == "sent":
            return "受信済み"
        return status_map.get(status_key, status_key or "-")

    def normalize_admin_permissions(raw_value):
        if not raw_value:
            return []
        if isinstance(raw_value, list):
            return raw_value
        try:
            parsed = json.loads(raw_value)
        except Exception:
            return []
        return parsed if isinstance(parsed, list) else []

    def summarize_admin_permissions(user_dict):
        if user_dict.get("role") == "owner":
            return "すべて"
        permission_keys = normalize_admin_permissions(user_dict.get("admin_permissions"))
        if not permission_keys:
            return "すべて"
        labels = [User.ADMIN_PERMISSION_OPTIONS.get(key, key) for key in permission_keys]
        return " / ".join(labels)

    def to_int_local(value):
        if callable(safe_int):
            try:
                return safe_int(value)
            except Exception:
                pass
        try:
            if value in (None, ""):
                return 0
            return int(float(value))
        except (TypeError, ValueError):
            return 0

    def normalize_item_date(value):
        if not value:
            return None
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        if isinstance(value, str):
            for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
                try:
                    sample = value[:26] if fmt.endswith("%f") else value[:19] if (" " in fmt or "T" in fmt) else value[:10]
                    return datetime.strptime(sample, fmt).date()
                except ValueError:
                    continue
        return None

    def form_value(name, fallback=None):
        if name in request.form:
            return request.form.get(name)
        return fallback

    def money_value(name, fallback=0):
        raw_value = request.form.get(name) if name in request.form else fallback
        if callable(parse_money_value):
            try:
                return parse_money_value(raw_value, to_int_local(fallback))
            except Exception:
                pass
        return to_int_local(raw_value)

    def float_value(name, fallback=0.0):
        raw_value = request.form.get(name) if name in request.form else fallback
        try:
            if raw_value in (None, ""):
                return float(fallback or 0)
            return float(raw_value)
        except (TypeError, ValueError):
            return float(fallback or 0)

    def normalize_path_list(raw_value):
        if not raw_value:
            return []
        parsed = raw_value
        if isinstance(raw_value, str):
            try:
                parsed = json.loads(raw_value)
            except Exception:
                parsed = raw_value
        if isinstance(parsed, (list, tuple)):
            values = parsed
        else:
            values = [parsed]
        normalized = []
        seen = set()
        for value in values:
            if value in (None, ""):
                continue
            path_value = str(value).replace("\\", "/")
            if path_value in seen:
                continue
            seen.add(path_value)
            normalized.append(path_value)
        return normalized

    def save_uploaded_image(file_storage, prefix=""):
        if not file_storage or not getattr(file_storage, "filename", ""):
            return None
        if callable(allowed_file):
            try:
                if not allowed_file(file_storage.filename):
                    return None
            except Exception:
                pass
        timestamp_prefix = datetime.now().strftime("%Y%m%d_%H%M%S_")
        filename = timestamp_prefix + prefix + secure_filename(file_storage.filename)
        destination = os.path.join(app.config["UPLOAD_FOLDER"], filename)
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        file_storage.save(destination)
        return f"uploads/{filename}"

    def save_uploaded_document(file_storage, label):
        if not file_storage or not getattr(file_storage, "filename", ""):
            return None
        timestamp_prefix = datetime.now().strftime("%Y%m%d_%H%M%S_")
        filename = timestamp_prefix + f"{label}_" + secure_filename(file_storage.filename)
        destination = os.path.join(app.config["UPLOAD_FOLDER"], filename)
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        file_storage.save(destination)
        return f"uploads/{filename}"

    def resolve_back_url(candidate, fallback=""):
        if callable(resolve_internal_back_url):
            try:
                return resolve_internal_back_url(candidate, fallback)
            except Exception:
                pass
        if isinstance(candidate, str) and candidate.startswith("/"):
            return candidate
        return fallback

    def derive_appraisal_status(item_status):
        status = (item_status or "unlisted").strip()
        if status == "appraisal_pending":
            return "waiting"
        if status in {"listed", "sold"}:
            return "completed"
        return "none"

    def resolve_sale_date_for_status(item_status, sale_date_value=None):
        if (item_status or "").strip() != "sold":
            return None
        return sale_date_value or get_jst_now().strftime("%Y-%m-%d")

    def fallback_item_redirect(item_dict):
        fallback = url_for("admin_items")
        try:
            if callable(is_kaika_inventory_item) and is_kaika_inventory_item(item_dict):
                return fallback
        except Exception:
            pass
        user_id = item_dict.get("user_id")
        if user_id:
            return url_for("admin_user_items", id=user_id)
        return fallback

    def classify_company_scope(item_dict, role_map):
        item_user_id = item_dict.get("user_id")
        if item_user_id is None:
            return "kaika"
        role = role_map.get(item_user_id)
        if role:
            return "user" if role == "user" else "kaika"
        if callable(is_kaika_inventory_item):
            try:
                return "kaika" if is_kaika_inventory_item(item_dict) else "user"
            except Exception:
                pass
        return "kaika"

    def format_destination_label(item_dict):
        if callable(format_sales_destination):
            try:
                return format_sales_destination(item_dict.get("sales_destination"), item_dict.get("sale_type"))
            except TypeError:
                try:
                    return format_sales_destination(item_dict.get("sales_destination"))
                except Exception:
                    pass
            except Exception:
                pass
        return item_dict.get("sales_destination") or item_dict.get("sale_type") or "-"

    def build_company_sales_analytics_context():
        today = datetime.now().date()
        current_month_start = today.replace(day=1)
        current_month_key = today.strftime("%Y-%m")
        fee_settings = get_fee_settings() if callable(get_fee_settings) else None

        users = []
        items = []
        conn, cur = open_cursor()
        try:
            cur.execute("SELECT id, username, display_name, role FROM users")
            users = rows_to_dicts(cur.fetchall())
            cur.execute(
                """
                SELECT id, user_id, product_name, purchase_price, sale_price, shipping_cost, commission,
                       sale_date, purchase_date, sales_destination, sale_type
                FROM merchandise
                """
            )
            items = rows_to_dicts(cur.fetchall())
        finally:
            cur.close()
            conn.close()

        role_map = {row.get("id"): row.get("role") for row in users}
        user_lookup = {
            row.get("id"): row.get("display_name") or row.get("username") or f"ID:{row.get('id')}"
            for row in users
        }

        analytics = {
            "company": {
                "total_revenue": 0,
                "current_month_revenue": 0,
                "total_profit": 0,
                "current_month_profit": 0,
                "sold_count": 0,
            },
            "kaika": {
                "sales": 0,
                "current_month_sales": 0,
                "profit": 0,
                "current_month_profit": 0,
                "sold_count": 0,
                "inventory_count": 0,
                "inventory_value": 0,
                "combined_revenue": 0,
                "current_month_combined_revenue": 0,
                "combined_profit": 0,
            },
            "client": {
                "sales": 0,
                "current_month_sales": 0,
                "profit": 0,
                "current_month_profit": 0,
                "sold_count": 0,
                "inventory_count": 0,
                "inventory_value": 0,
            },
            "support": {
                "service_fee_revenue": 0,
                "current_month_service_fee_revenue": 0,
                "subscription_revenue": 0,
                "total_revenue": 0,
                "current_month_revenue": 0,
            },
        }
        recent_sales = []
        monthly_item_counts = {}

        for item in items:
            scope = classify_company_scope(item, role_map)
            sale_date = normalize_item_date(item.get("sale_date"))
            purchase_date = normalize_item_date(item.get("purchase_date")) or today
            purchase_price = to_int_local(item.get("purchase_price"))
            sale_price = to_int_local(item.get("sale_price"))
            shipping_cost = to_int_local(item.get("shipping_cost"))
            commission = to_int_local(item.get("commission"))

            if role_map.get(item.get("user_id")) == "user" and purchase_date.strftime("%Y-%m") == current_month_key:
                monthly_item_counts[item.get("user_id")] = monthly_item_counts.get(item.get("user_id"), 0) + 1

            if sale_date:
                profit = sale_price - purchase_price - shipping_cost - commission
                if scope == "user":
                    analytics["client"]["sales"] += sale_price
                    analytics["client"]["profit"] += profit
                    analytics["client"]["sold_count"] += 1
                else:
                    analytics["kaika"]["sales"] += sale_price
                    analytics["kaika"]["profit"] += profit
                    analytics["kaika"]["sold_count"] += 1

                if callable(build_user_fee_components) and scope == "user":
                    try:
                        fee_data = build_user_fee_components(item, fee_settings)
                    except Exception:
                        fee_data = {}
                    fee_value = to_int_local((fee_data or {}).get("kaika_revenue_total"))
                    analytics["support"]["service_fee_revenue"] += fee_value
                    if sale_date >= current_month_start:
                        analytics["support"]["current_month_service_fee_revenue"] += fee_value

                if sale_date >= current_month_start:
                    if scope == "user":
                        analytics["client"]["current_month_sales"] += sale_price
                        analytics["client"]["current_month_profit"] += profit
                    else:
                        analytics["kaika"]["current_month_sales"] += sale_price
                        analytics["kaika"]["current_month_profit"] += profit

                recent_sales.append(
                    {
                        "id": item.get("id") or 0,
                        "sale_date": sale_date,
                        "sale_date_text": sale_date.strftime("%Y-%m-%d"),
                        "scope_label": "クライアント" if scope == "user" else "開花",
                        "product_name": clean_display_text(item.get("product_name"), fallback="商品名未登録"),
                        "owner_name": user_lookup.get(item.get("user_id")) if scope == "user" else "開花",
                        "destination_label": format_destination_label(item),
                        "sale_price": sale_price,
                        "profit": profit,
                    }
                )
            else:
                if scope == "user":
                    analytics["client"]["inventory_count"] += 1
                    analytics["client"]["inventory_value"] += purchase_price
                else:
                    analytics["kaika"]["inventory_count"] += 1
                    analytics["kaika"]["inventory_value"] += purchase_price

        subscription_revenue = 0
        for user in users:
            if user.get("role") != "user":
                continue
            subscription_revenue += get_monthly_fee(monthly_item_counts.get(user.get("id"), 0))
        analytics["support"]["subscription_revenue"] = subscription_revenue
        analytics["support"]["total_revenue"] = analytics["support"]["service_fee_revenue"] + subscription_revenue
        analytics["support"]["current_month_revenue"] = analytics["support"]["current_month_service_fee_revenue"] + subscription_revenue

        analytics["kaika"]["combined_revenue"] = analytics["kaika"]["sales"] + analytics["support"]["total_revenue"]
        analytics["kaika"]["current_month_combined_revenue"] = analytics["kaika"]["current_month_sales"] + analytics["support"]["current_month_revenue"]
        analytics["kaika"]["combined_profit"] = analytics["kaika"]["profit"] + analytics["support"]["total_revenue"]

        analytics["company"]["total_revenue"] = analytics["kaika"]["combined_revenue"] + analytics["client"]["sales"]
        analytics["company"]["current_month_revenue"] = analytics["kaika"]["current_month_combined_revenue"] + analytics["client"]["current_month_sales"]
        analytics["company"]["total_profit"] = analytics["kaika"]["combined_profit"] + analytics["client"]["profit"]
        analytics["company"]["current_month_profit"] = (
            analytics["kaika"]["current_month_profit"]
            + analytics["support"]["current_month_revenue"]
            + analytics["client"]["current_month_profit"]
        )
        analytics["company"]["sold_count"] = analytics["kaika"]["sold_count"] + analytics["client"]["sold_count"]

        recent_sales.sort(key=lambda row: (row.get("sale_date") or date.min, row.get("id") or 0), reverse=True)
        recent_sales = recent_sales[:10]

        return analytics, recent_sales

    def sync_shared_master_settings():
        conn, cur = open_cursor()
        try:
            placeholder = "%s" if DATABASE_URL else "?"
            simple_tables = {
                "master_suppliers": ["value", "display_name", "display_order", "is_active", "created_at"],
                "master_conditions": ["value", "display_name", "description", "display_order", "is_active", "created_at"],
                "master_payment_methods": ["value", "display_name", "display_order", "is_active", "created_at"],
                "master_supplier_details": ["value", "display_name", "display_order", "is_active", "created_at"],
            }

            cur.execute(f"DELETE FROM master_brands WHERE scope = {placeholder}", ("user",))
            cur.execute(f"DELETE FROM master_brand_categories WHERE scope = {placeholder}", ("user",))
            for table_name in simple_tables:
                cur.execute(f"DELETE FROM {table_name} WHERE scope = {placeholder}", ("user",))

            category_map = {}
            cur.execute(
                f"""
                SELECT id, name, display_order, is_active, created_at
                FROM master_brand_categories
                WHERE scope = {placeholder} OR scope IS NULL
                ORDER BY display_order, id
                """,
                ("admin",),
            )
            for row in rows_to_dicts(cur.fetchall()):
                if DATABASE_URL:
                    cur.execute(
                        """
                        INSERT INTO master_brand_categories
                        (name, display_order, is_active, created_at, scope)
                        VALUES (%s, %s, %s, %s, %s)
                        RETURNING id
                        """,
                        (row.get("name"), row.get("display_order"), row.get("is_active"), row.get("created_at"), "user"),
                    )
                    inserted = cur.fetchone()
                    new_id = inserted["id"] if isinstance(inserted, dict) else inserted[0]
                else:
                    cur.execute(
                        """
                        INSERT INTO master_brand_categories
                        (name, display_order, is_active, created_at, scope)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (row.get("name"), row.get("display_order"), row.get("is_active"), row.get("created_at"), "user"),
                    )
                    new_id = cur.lastrowid
                category_map[row["id"]] = new_id

            cur.execute(
                f"""
                SELECT category_id, value, display_name, keywords, display_order, is_active, created_at
                FROM master_brands
                WHERE scope = {placeholder} OR scope IS NULL
                ORDER BY display_order, id
                """,
                ("admin",),
            )
            for row in rows_to_dicts(cur.fetchall()):
                category_id = category_map.get(row.get("category_id"))
                if DATABASE_URL:
                    cur.execute(
                        """
                        INSERT INTO master_brands
                        (category_id, value, display_name, keywords, display_order, is_active, created_at, scope)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            category_id,
                            row.get("value"),
                            row.get("display_name"),
                            row.get("keywords"),
                            row.get("display_order"),
                            row.get("is_active"),
                            row.get("created_at"),
                            "user",
                        ),
                    )
                else:
                    cur.execute(
                        """
                        INSERT INTO master_brands
                        (category_id, value, display_name, keywords, display_order, is_active, created_at, scope)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            category_id,
                            row.get("value"),
                            row.get("display_name"),
                            row.get("keywords"),
                            row.get("display_order"),
                            row.get("is_active"),
                            row.get("created_at"),
                            "user",
                        ),
                    )

            for table_name, columns in simple_tables.items():
                cur.execute(
                    f"""
                    SELECT {", ".join(columns)}
                    FROM {table_name}
                    WHERE scope = {placeholder} OR scope IS NULL
                    ORDER BY display_order, id
                    """,
                    ("admin",),
                )
                for row in rows_to_dicts(cur.fetchall()):
                    insert_columns = columns + ["scope"]
                    insert_values = [row.get(column) for column in columns] + ["user"]
                    placeholders_sql = ", ".join(["%s"] * len(insert_columns)) if DATABASE_URL else ", ".join(["?"] * len(insert_columns))
                    cur.execute(
                        f"INSERT INTO {table_name} ({', '.join(insert_columns)}) VALUES ({placeholders_sql})",
                        tuple(insert_values),
                    )

            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.close()
            conn.close()

    def fetch_management_user(id):
        conn, cur = open_cursor()
        try:
            placeholder = "%s" if DATABASE_URL else "?"
            cur.execute(f"SELECT * FROM users WHERE id = {placeholder}", (id,))
            row = cur.fetchone()
            return dict(row) if row else None
        finally:
            cur.close()
            conn.close()

    def fetch_client_rows(search_query=""):
        conn, cur = open_cursor()
        try:
            placeholder = "%s" if DATABASE_URL else "?"
            params = ["user"]
            where_clause = [f"role = {placeholder}"]
            if search_query:
                search_clause = "(username ILIKE %s OR display_name ILIKE %s OR email ILIKE %s)" if DATABASE_URL else "(username LIKE ? OR display_name LIKE ? OR email LIKE ?)"
                like_value = f"%{search_query}%"
                where_clause.append(search_clause)
                params.extend([like_value, like_value, like_value])

            cur.execute(
                f"""
                SELECT *
                FROM users
                WHERE {' AND '.join(where_clause)}
                ORDER BY created_at DESC, id DESC
                """,
                tuple(params),
            )
            users = rows_to_dicts(cur.fetchall())
            for user in users:
                if DATABASE_URL:
                    cur.execute(
                        f"""
                        SELECT COUNT(*) AS count
                        FROM merchandise
                        WHERE user_id = {placeholder}
                          AND DATE_TRUNC('month', COALESCE(purchase_date::date, CURRENT_DATE)) = DATE_TRUNC('month', CURRENT_DATE)
                        """,
                        (user["id"],),
                    )
                else:
                    cur.execute(
                        f"""
                        SELECT COUNT(*) AS count
                        FROM merchandise
                        WHERE user_id = {placeholder}
                          AND strftime('%Y-%m', COALESCE(purchase_date, date('now'))) = strftime('%Y-%m', 'now')
                        """,
                        (user["id"],),
                    )
                count_row = cur.fetchone()
                user["monthly_item_count"] = dict(count_row).get("count", 0) if count_row else 0
                user["monthly_fee"] = get_monthly_fee(user["monthly_item_count"])
                user["requested_plan_label"] = get_monthly_plan_label(user.get("requested_monthly_plan"))

                cur.execute(
                    f"""
                    SELECT COALESCE(SUM(commission), 0) AS total_kaika_fee
                    FROM merchandise
                    WHERE user_id = {placeholder}
                      AND sale_date IS NOT NULL
                      AND COALESCE(sale_type, 'normal') != 'normal'
                    """,
                    (user["id"],),
                )
                fee_row = cur.fetchone()
                user["kaika_fee"] = dict(fee_row).get("total_kaika_fee", 0) if fee_row else 0
                user["proxy_service_budget"] = int(user.get("proxy_service_budget") or 0)
                user["tuition_exempt"] = bool(user.get("tuition_exempt"))
            return users
        finally:
            cur.close()
            conn.close()

    def fetch_operator_rows(search_query=""):
        conn, cur = open_cursor()
        try:
            params = []
            where_sql = "role IN ('owner', 'admin')"
            if search_query:
                search_clause = "(username ILIKE %s OR display_name ILIKE %s OR email ILIKE %s)" if DATABASE_URL else "(username LIKE ? OR display_name LIKE ? OR email LIKE ?)"
                like_value = f"%{search_query}%"
                params.extend([like_value, like_value, like_value])
                where_sql = f"{where_sql} AND {search_clause}"

            cur.execute(
                f"""
                SELECT *
                FROM users
                WHERE {where_sql}
                ORDER BY CASE WHEN role = 'owner' THEN 0 ELSE 1 END, created_at DESC, id DESC
                """,
                tuple(params),
            )
            users = rows_to_dicts(cur.fetchall())
            for user in users:
                user["admin_permissions_list"] = normalize_admin_permissions(user.get("admin_permissions"))
                user["permissions_summary"] = summarize_admin_permissions(user)
            return users
        finally:
            cur.close()
            conn.close()

    def admin_users_clients_view():
        denied = ensure_permission("users")
        if denied:
            return denied
        ensure_user_profile_columns()
        search_query = request.args.get("search", "").strip()
        return render_template("admin/clients.html", users=fetch_client_rows(search_query), search_query=search_query)

    def admin_add_client_view():
        denied = ensure_permission("users")
        if denied:
            return denied
        ensure_user_profile_columns()

        if request.method == "POST":
            username = (request.form.get("username") or "").strip()
            email = (request.form.get("email") or "").strip()
            password = request.form.get("password") or ""
            display_name = (request.form.get("display_name") or "").strip()
            phone = (request.form.get("phone") or "").strip()
            postal_code = (request.form.get("postal_code") or "").strip()
            address = (request.form.get("address") or "").strip()
            inventory_summary_period_default = normalize_inventory_summary_period(request.form.get("inventory_summary_period_default"))
            requested_monthly_plan = normalize_monthly_plan_key(request.form.get("requested_monthly_plan"))
            plan_effective_month = resolve_plan_effective_month(requested_monthly_plan)
            plan_change_requested_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S") if requested_monthly_plan else None
            proxy_service_budget = request.form.get("proxy_service_budget", "0").strip()
            tuition_exempt = db_boolean_param(request.form.get("tuition_exempt") == "1")

            if not username or not email or not password:
                flash("ユーザー名、メール、パスワードは必須です。", "error")
                return render_template("admin/client_form.html", **client_form_context(None))
            if len(password) < 6:
                flash("パスワードは6文字以上で設定してください。", "error")
                return render_template("admin/client_form.html", **client_form_context(None))

            try:
                budget_value = int(proxy_service_budget or 0)
            except ValueError:
                budget_value = 0

            conn, cur = open_cursor()
            try:
                placeholder = "%s" if DATABASE_URL else "?"
                duplicate_user = find_existing_user_by_login(cur, username, email)
                if duplicate_user:
                    flash("ユーザー名またはメールアドレスがすでに使用されています。", "error")
                    return render_template("admin/client_form.html", **client_form_context(None))
                sync_users_id_sequence(cur)
                cur.execute(
                    f"""
                    INSERT INTO users
                    (
                        username, email, password_hash, role, display_name, phone, postal_code, address,
                        inventory_summary_period_default, requested_monthly_plan, plan_change_effective_month,
                        plan_change_requested_at, proxy_service_budget, tuition_exempt
                    )
                    VALUES (
                        {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder},
                        {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}
                    )
                    """,
                    (
                        username,
                        email,
                        generate_password_hash(password),
                        "user",
                        display_name or username,
                        phone,
                        postal_code,
                        address,
                        inventory_summary_period_default,
                        requested_monthly_plan,
                        plan_effective_month,
                        plan_change_requested_at,
                        budget_value,
                        tuition_exempt,
                    ),
                )
                conn.commit()
                flash("クライアントを追加しました。", "success")
                return redirect(url_for("admin_users"))
            except Exception as exc:
                conn.rollback()
                print(f"[ERROR] admin_add_client_view failed: {exc}", flush=True)
                flash("ユーザー名またはメールアドレスがすでに使用されています。", "error")
            finally:
                cur.close()
                conn.close()

        ensure_user_profile_columns()
        return render_template("admin/client_form.html", **client_form_context(None))

    def admin_edit_client_view(id):
        denied = ensure_permission("users")
        if denied:
            return denied
        ensure_user_profile_columns()

        user = fetch_management_user(id)
        if not user or user.get("role") != "user":
            flash("クライアントが見つかりません。", "error")
            return redirect(url_for("admin_users"))

        if request.method == "POST":
            display_name = (request.form.get("display_name") or "").strip()
            email = (request.form.get("email") or "").strip()
            phone = (request.form.get("phone") or "").strip()
            postal_code = (request.form.get("postal_code") or "").strip()
            address = (request.form.get("address") or "").strip()
            inventory_summary_period_default = normalize_inventory_summary_period(request.form.get("inventory_summary_period_default"))
            requested_monthly_plan = normalize_monthly_plan_key(request.form.get("requested_monthly_plan"))
            plan_effective_month = resolve_plan_effective_month(requested_monthly_plan)
            plan_change_requested_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S") if requested_monthly_plan else None
            new_password = request.form.get("new_password") or ""
            proxy_service_budget = request.form.get("proxy_service_budget", "0").strip()
            tuition_exempt = db_boolean_param(request.form.get("tuition_exempt") == "1")
            try:
                budget_value = int(proxy_service_budget or 0)
            except ValueError:
                budget_value = 0

            conn, cur = open_cursor()
            try:
                placeholder = "%s" if DATABASE_URL else "?"
                if new_password:
                    if len(new_password) < 6:
                        flash("パスワードは6文字以上で設定してください。", "error")
                        return render_template("admin/client_form.html", **client_form_context(user))
                    cur.execute(
                        f"""
                        UPDATE users
                        SET display_name = {placeholder},
                            email = {placeholder},
                            phone = {placeholder},
                            postal_code = {placeholder},
                            address = {placeholder},
                            inventory_summary_period_default = {placeholder},
                            requested_monthly_plan = {placeholder},
                            plan_change_effective_month = {placeholder},
                            plan_change_requested_at = {placeholder},
                            password_hash = {placeholder},
                            proxy_service_budget = {placeholder},
                            tuition_exempt = {placeholder},
                            role = 'user'
                        WHERE id = {placeholder}
                        """,
                        (
                            display_name or user.get("username"),
                            email,
                            phone,
                            postal_code,
                            address,
                            inventory_summary_period_default,
                            requested_monthly_plan,
                            plan_effective_month,
                            plan_change_requested_at,
                            generate_password_hash(new_password),
                            budget_value,
                            tuition_exempt,
                            id,
                        ),
                    )
                else:
                    cur.execute(
                        f"""
                        UPDATE users
                        SET display_name = {placeholder},
                            email = {placeholder},
                            phone = {placeholder},
                            postal_code = {placeholder},
                            address = {placeholder},
                            inventory_summary_period_default = {placeholder},
                            requested_monthly_plan = {placeholder},
                            plan_change_effective_month = {placeholder},
                            plan_change_requested_at = {placeholder},
                            proxy_service_budget = {placeholder},
                            tuition_exempt = {placeholder},
                            role = 'user'
                        WHERE id = {placeholder}
                        """,
                        (
                            display_name or user.get("username"),
                            email,
                            phone,
                            postal_code,
                            address,
                            inventory_summary_period_default,
                            requested_monthly_plan,
                            plan_effective_month,
                            plan_change_requested_at,
                            budget_value,
                            tuition_exempt,
                            id,
                        ),
                    )
                conn.commit()
                flash("クライアント情報を更新しました。", "success")
                return redirect(url_for("admin_users"))
            except Exception as exc:
                conn.rollback()
                flash(f"更新に失敗しました: {exc}", "error")
            finally:
                cur.close()
                conn.close()

        return render_template("admin/client_form.html", **client_form_context(user))

    def admin_delete_client_view(id):
        denied = ensure_permission("users")
        if denied:
            return denied
        if id == current_user.id:
            flash("自分自身は削除できません。", "error")
            return redirect(url_for("admin_users"))

        user = fetch_management_user(id)
        if not user or user.get("role") != "user":
            flash("クライアントが見つかりません。", "error")
            return redirect(url_for("admin_users"))

        conn, cur = open_cursor()
        try:
            placeholder = "%s" if DATABASE_URL else "?"
            def table_exists(table_name):
                if DATABASE_URL:
                    cur.execute(
                        """
                        SELECT EXISTS (
                            SELECT 1
                            FROM information_schema.tables
                            WHERE table_schema = current_schema()
                              AND table_name = %s
                        ) AS exists
                        """,
                        (table_name,),
                    )
                    row = cur.fetchone()
                    return bool(row.get("exists") if isinstance(row, dict) else row[0])
                cur.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?", (table_name,))
                return cur.fetchone() is not None

            if table_exists("client_monthly_fee_settings"):
                cur.execute(f"DELETE FROM client_monthly_fee_settings WHERE user_id = {placeholder}", (id,))
            cur.execute(
                f"""
                DELETE FROM user_kaitori_shoudaku_items
                WHERE kaitori_shoudaku_id IN (
                    SELECT id FROM user_kaitori_shoudaku WHERE user_id = {placeholder}
                )
                """,
                (id,),
            )
            cur.execute(f"DELETE FROM user_kaitori_shoudaku WHERE user_id = {placeholder}", (id,))
            cur.execute(f"DELETE FROM merchandise WHERE user_id = {placeholder}", (id,))
            cur.execute(f"DELETE FROM customers WHERE user_id = {placeholder}", (id,))
            cur.execute(f"DELETE FROM users WHERE id = {placeholder}", (id,))
            conn.commit()
            flash("クライアントを削除しました。", "info")
        finally:
            cur.close()
            conn.close()
        return redirect(url_for("admin_users"))

    def admin_operator_users_view():
        denied = ensure_permission("users")
        if denied:
            return denied
        search_query = request.args.get("search", "").strip()
        return render_template("admin/operators.html", users=fetch_operator_rows(search_query), search_query=search_query)

    def admin_operator_add_user_view():
        denied = ensure_permission("users")
        if denied:
            return denied

        if request.method == "POST":
            username = (request.form.get("username") or "").strip()
            email = (request.form.get("email") or "").strip()
            password = request.form.get("password") or ""
            display_name = (request.form.get("display_name") or "").strip()
            role = (request.form.get("role") or "admin").strip()
            if role == "owner" and not current_user.is_owner():
                role = "admin"
            admin_permissions = request.form.getlist("admin_permissions")
            admin_permissions_json = json.dumps(admin_permissions) if role == "admin" and admin_permissions else None

            if not username or not email or not password:
                flash("ユーザー名、メール、パスワードは必須です。", "error")
                return render_template("admin/operator_form.html", user=None, permission_options=User.ADMIN_PERMISSION_OPTIONS)
            if len(password) < 6:
                flash("パスワードは6文字以上で設定してください。", "error")
                return render_template("admin/operator_form.html", user=None, permission_options=User.ADMIN_PERMISSION_OPTIONS)

            conn, cur = open_cursor()
            try:
                placeholder = "%s" if DATABASE_URL else "?"
                duplicate_user = find_existing_user_by_login(cur, username, email)
                if duplicate_user:
                    flash("ユーザー名またはメールアドレスがすでに使用されています。", "error")
                    return render_template("admin/operator_form.html", user=None, permission_options=User.ADMIN_PERMISSION_OPTIONS)
                sync_users_id_sequence(cur)
                cur.execute(
                    f"""
                    INSERT INTO users
                    (username, email, password_hash, role, display_name, admin_permissions)
                    VALUES ({placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder})
                    """,
                    (
                        username,
                        email,
                        generate_password_hash(password),
                        role,
                        display_name or username,
                        admin_permissions_json,
                    ),
                )
                conn.commit()
                flash("運営アカウントを追加しました。", "success")
                return redirect(url_for("admin_operator_users"))
            except Exception as exc:
                conn.rollback()
                print(f"[ERROR] admin_operator_add_user_view failed: {exc}", flush=True)
                flash("ユーザー名またはメールアドレスがすでに使用されています。", "error")
            finally:
                cur.close()
                conn.close()

        return render_template("admin/operator_form.html", user=None, permission_options=User.ADMIN_PERMISSION_OPTIONS)

    def admin_operator_edit_user_view(id):
        denied = ensure_permission("users")
        if denied:
            return denied

        user = fetch_management_user(id)
        if not user or user.get("role") not in {"owner", "admin"}:
            flash("運営アカウントが見つかりません。", "error")
            return redirect(url_for("admin_operator_users"))
        if user.get("role") == "owner" and not current_user.is_owner() and id != current_user.id:
            flash("オーナーアカウントはオーナーのみ編集できます。", "error")
            return redirect(url_for("admin_operator_users"))

        user["admin_permissions_list"] = normalize_admin_permissions(user.get("admin_permissions"))

        if request.method == "POST":
            display_name = (request.form.get("display_name") or "").strip()
            email = (request.form.get("email") or "").strip()
            requested_role = (request.form.get("role") or user.get("role") or "admin").strip()
            new_password = request.form.get("new_password") or ""

            if id == current_user.id:
                requested_role = user.get("role")
            if requested_role == "owner" and not current_user.is_owner():
                requested_role = user.get("role")
            if requested_role not in {"owner", "admin"}:
                requested_role = "admin"

            admin_permissions = request.form.getlist("admin_permissions")
            admin_permissions_json = json.dumps(admin_permissions) if requested_role == "admin" and admin_permissions else None

            conn, cur = open_cursor()
            try:
                placeholder = "%s" if DATABASE_URL else "?"
                if new_password:
                    if len(new_password) < 6:
                        flash("パスワードは6文字以上で設定してください。", "error")
                        return render_template("admin/operator_form.html", user=user, permission_options=User.ADMIN_PERMISSION_OPTIONS)
                    cur.execute(
                        f"""
                        UPDATE users
                        SET display_name = {placeholder},
                            email = {placeholder},
                            role = {placeholder},
                            password_hash = {placeholder},
                            admin_permissions = {placeholder}
                        WHERE id = {placeholder}
                        """,
                        (
                            display_name or user.get("username"),
                            email,
                            requested_role,
                            generate_password_hash(new_password),
                            admin_permissions_json,
                            id,
                        ),
                    )
                else:
                    cur.execute(
                        f"""
                        UPDATE users
                        SET display_name = {placeholder},
                            email = {placeholder},
                            role = {placeholder},
                            admin_permissions = {placeholder}
                        WHERE id = {placeholder}
                        """,
                        (
                            display_name or user.get("username"),
                            email,
                            requested_role,
                            admin_permissions_json,
                            id,
                        ),
                    )
                conn.commit()
                flash("運営アカウントを更新しました。", "success")
                return redirect(url_for("admin_operator_users"))
            except Exception as exc:
                conn.rollback()
                flash(f"更新に失敗しました: {exc}", "error")
            finally:
                cur.close()
                conn.close()

        return render_template("admin/operator_form.html", user=user, permission_options=User.ADMIN_PERMISSION_OPTIONS)

    def admin_delete_operator_view(id):
        denied = ensure_permission("users")
        if denied:
            return denied
        if id == current_user.id:
            flash("自分自身は削除できません。", "error")
            return redirect(url_for("admin_operator_users"))

        user = fetch_management_user(id)
        if not user or user.get("role") not in {"owner", "admin"}:
            flash("運営アカウントが見つかりません。", "error")
            return redirect(url_for("admin_operator_users"))
        if user.get("role") == "owner" and not current_user.is_owner():
            flash("オーナーアカウントはオーナーのみ削除できます。", "error")
            return redirect(url_for("admin_operator_users"))

        conn, cur = open_cursor()
        try:
            placeholder = "%s" if DATABASE_URL else "?"
            cur.execute(f"DELETE FROM users WHERE id = {placeholder}", (id,))
            conn.commit()
            flash("運営アカウントを削除しました。", "info")
        finally:
            cur.close()
            conn.close()
        return redirect(url_for("admin_operator_users"))

    def fetch_admin_document_history_rows():
        rows = []
        conn, cur = open_cursor()
        try:
            mitsumori_request_map = {}
            invoice_request_map = {}
            sales_agency_columns = get_table_columns(cur, "sales_agency_requests")
            if {"created_mitsumori_id", "created_invoice_id"}.issubset(sales_agency_columns):
                cur.execute(
                    """
                    SELECT created_mitsumori_id, created_invoice_id, service_type
                    FROM sales_agency_requests
                    WHERE created_mitsumori_id IS NOT NULL OR created_invoice_id IS NOT NULL
                    """
                )
                for mapping_row in cur.fetchall():
                    mapping_dict = dict(mapping_row)
                    if mapping_dict.get("created_mitsumori_id"):
                        mitsumori_request_map[mapping_dict["created_mitsumori_id"]] = mapping_dict.get("service_type")
                    if mapping_dict.get("created_invoice_id"):
                        invoice_request_map[mapping_dict["created_invoice_id"]] = mapping_dict.get("service_type")

            cur.execute(
                """
                SELECT s.id, s.document_no, s.issue_date, s.total_amount, s.status, s.created_at,
                       s.recipient_name, u.display_name AS client_name, u.username
                FROM shikiriosho s
                LEFT JOIN users u ON s.recipient_id = u.id
                ORDER BY COALESCE(s.issue_date, s.created_at) DESC, s.id DESC
                """
            )
            for row in rows_to_dicts(cur.fetchall()):
                client_name = row.get("client_name") or row.get("recipient_name") or row.get("username") or "未設定"
                rows.append(
                    {
                        "kind": "shikiriosho",
                        "id": row["id"],
                        "document_type": "精算書",
                        "document_no": row.get("document_no") or "-",
                        "client_name": client_name,
                        "service_type": "",
                        "service_name": "",
                        "issue_date": format_history_date(row.get("issue_date") or row.get("created_at")),
                        "total_amount": int(row.get("total_amount") or 0),
                        "status": row.get("status") or "",
                        "status_label": get_document_status_label("shikiriosho", row.get("status")),
                        "direction_key": "client_outgoing",
                        "direction_label": "開花→クライアント",
                        "subject": "",
                        "notes": "",
                        "detail_endpoint": "admin_shikiriosho_view",
                    }
                )

            cur.execute(
                """
                SELECT i.id, i.invoice_no, i.issue_date, i.total_amount, i.status, i.created_at,
                       i.notes, i.service_type, i.recipient_name,
                       u.display_name AS client_name, u.username
                FROM invoices i
                LEFT JOIN users u ON i.sender_id = u.id
                WHERE i.invoice_no LIKE 'KT-%'
                ORDER BY COALESCE(i.issue_date, i.created_at) DESC, i.id DESC
                """
            )
            for row in rows_to_dicts(cur.fetchall()):
                linked_service_type = invoice_request_map.get(row["id"]) or row.get("service_type") or ""
                client_name = row.get("client_name") or row.get("recipient_name") or row.get("username") or "未設定"
                rows.append(
                    {
                        "kind": "invoice",
                        "id": row["id"],
                        "document_type": "買取明細書",
                        "document_no": row.get("invoice_no") or "-",
                        "client_name": client_name,
                        "service_type": linked_service_type,
                        "service_name": get_service_display_name(linked_service_type),
                        "issue_date": format_history_date(row.get("issue_date") or row.get("created_at")),
                        "total_amount": int(row.get("total_amount") or 0),
                        "status": row.get("status") or "",
                        "status_label": get_document_status_label("invoice", row.get("status")),
                        "direction_key": "client_outgoing",
                        "direction_label": "開花→クライアント",
                        "subject": "",
                        "notes": row.get("notes") or "",
                        "detail_endpoint": "admin_kaitori_view",
                    }
                )

            cur.execute(
                """
                SELECT m.id, m.document_no, m.issue_date, m.total_amount, m.status, m.created_at,
                       m.subject, m.notes, m.company_name,
                       u.display_name AS client_name, u.username
                FROM user_mitsumori m
                LEFT JOIN users u ON m.user_id = u.id
                ORDER BY COALESCE(m.issue_date, m.created_at) DESC, m.id DESC
                """
            )
            for row in rows_to_dicts(cur.fetchall()):
                is_admin_created = (row.get("document_no") or "").startswith("MT-")
                linked_service_type = mitsumori_request_map.get(row["id"]) or ""
                rows.append(
                    {
                        "kind": "user_mitsumori",
                        "id": row["id"],
                        "document_type": "見積依頼書",
                        "document_no": row.get("document_no") or "-",
                        "client_name": row.get("client_name") or row.get("username") or "未設定",
                        "service_type": linked_service_type,
                        "service_name": get_service_display_name(linked_service_type),
                        "issue_date": format_history_date(row.get("issue_date") or row.get("created_at")),
                        "total_amount": int(row.get("total_amount") or 0),
                        "status": row.get("status") or "",
                        "status_label": get_document_status_label("user_mitsumori", row.get("status")),
                        "direction_key": "vendor_outgoing" if is_admin_created else "client_incoming",
                        "direction_label": "開花→業者" if is_admin_created else "クライアント→開花",
                        "subject": row.get("subject") or "",
                        "notes": row.get("notes") or "",
                        "detail_endpoint": "admin_mitsumori_view" if is_admin_created else "admin_user_mitsumori_view",
                    }
                )

            cur.execute(
                """
                SELECT k.id, k.document_no, k.issue_date, k.total_amount, k.status, k.created_at,
                       k.subject, k.notes, k.recipient_name,
                       u.display_name AS client_name, u.username
                FROM user_keisan k
                LEFT JOIN users u ON k.user_id = u.id
                ORDER BY COALESCE(k.issue_date, k.created_at) DESC, k.id DESC
                """
            )
            for row in rows_to_dicts(cur.fetchall()):
                client_name = row.get("client_name") or row.get("recipient_name") or row.get("username") or "未設定"
                rows.append(
                    {
                        "kind": "user_keisan",
                        "id": row["id"],
                        "document_type": "計算書",
                        "document_no": row.get("document_no") or "-",
                        "client_name": client_name,
                        "service_type": "",
                        "service_name": "",
                        "issue_date": format_history_date(row.get("issue_date") or row.get("created_at")),
                        "total_amount": int(row.get("total_amount") or 0),
                        "status": row.get("status") or "",
                        "status_label": get_document_status_label("user_keisan", row.get("status")),
                        "direction_key": "client_outgoing",
                        "direction_label": "開花→クライアント",
                        "subject": row.get("subject") or "",
                        "notes": row.get("notes") or "",
                        "detail_endpoint": "admin_auction_keisan_view",
                    }
                )

            for history_row in rows:
                endpoint = history_row.get("detail_endpoint")
                history_row["request_url"] = None
                if endpoint and history_row.get("id") and endpoint in app.view_functions:
                    try:
                        history_row["detail_url"] = url_for(endpoint, id=history_row["id"])
                    except Exception:
                        history_row["detail_url"] = None
                else:
                    history_row["detail_url"] = None

            rows.sort(key=lambda row: (row.get("issue_date") or "", row.get("document_no") or ""), reverse=True)
            return rows
        finally:
            cur.close()
            conn.close()

    def apply_admin_document_history_filters(rows, filters):
        doc_type = (filters.get("doc_type") or "all").strip()
        client = (filters.get("client") or "").strip().lower()
        status = (filters.get("status") or "all").strip()
        direction = (filters.get("direction") or "all").strip()
        service_type = (filters.get("service_type") or "all").strip()
        date_from = (filters.get("date_from") or "").strip()
        date_to = (filters.get("date_to") or "").strip()
        keyword = (filters.get("keyword") or "").strip().lower()

        filtered_rows = []
        for row in rows:
            if doc_type != "all" and row.get("kind") != doc_type:
                continue
            if status != "all" and row.get("status") != status:
                continue
            if direction != "all" and row.get("direction_key") != direction:
                continue
            if service_type != "all" and (row.get("service_type") or "") != service_type:
                continue
            if client and client not in (row.get("client_name") or "").lower():
                continue
            if date_from and (row.get("issue_date") or "") < date_from:
                continue
            if date_to and (row.get("issue_date") or "") > date_to:
                continue
            if keyword:
                haystack = " ".join(
                    [
                        row.get("document_no") or "",
                        row.get("client_name") or "",
                        row.get("document_type") or "",
                        row.get("service_name") or "",
                        row.get("subject") or "",
                        row.get("notes") or "",
                    ]
                ).lower()
                if keyword not in haystack:
                    continue
            filtered_rows.append(row)
        return filtered_rows

    def admin_documents_dashboard_v2():
        denied = ensure_admin()
        if denied:
            return denied

        history_rows = fetch_admin_document_history_rows()
        document_counts = {"精算書": 0, "見積依頼書": 0, "買取明細書": 0, "計算書": 0}
        for row in history_rows:
            if row.get("document_type") in document_counts:
                document_counts[row["document_type"]] += 1

        recent_incoming_documents = [row for row in history_rows if row.get("direction_key") == "incoming"][:10]

        ongoing_request_rows = []
        conn, cur = open_cursor()
        try:
            cur.execute(
                """
                SELECT sar.*, u.display_name AS user_name, u.username,
                       p.display_name AS processor_name
                FROM sales_agency_requests sar
                JOIN users u ON sar.user_id = u.id
                LEFT JOIN users p ON sar.processed_by = p.id
                WHERE sar.status IN ('pending', 'approved', 'appraising')
                ORDER BY CASE sar.status
                    WHEN 'pending' THEN 0
                    WHEN 'approved' THEN 1
                    WHEN 'appraising' THEN 2
                    ELSE 9
                END, sar.created_at DESC
                LIMIT 20
                """
            )
            for request_row in rows_to_dicts(cur.fetchall()):
                source_request, items = fetch_sales_agency_request_source(request_row["id"], waiting_only=False)
                request_row["merchandise_items"] = items
                request_row["service_name"] = get_service_display_name(request_row.get("service_type"))
                request_row["client_name"] = request_row.get("user_name") or request_row.get("username") or "未設定"
                request_row["status_label"] = get_sales_agency_status_label(request_row.get("status"), viewer="admin")
                request_row["pending_appraisal_count"] = source_request.get("pending_appraisal_count") if source_request else 0
                request_row["request_can_create_documents"] = (
                    request_row.get("service_type") == "wholesale"
                    and request_row.get("status") in {"approved", "appraising"}
                    and (request_row.get("pending_appraisal_count") or 0) > 0
                    and not request_row.get("created_mitsumori_id")
                    and not request_row.get("created_invoice_id")
                )
                request_row["created_at"] = as_timestamp_text(request_row.get("created_at"))
                request_row["processed_at"] = as_timestamp_text(request_row.get("processed_at"))
                ongoing_request_rows.append(request_row)
        finally:
            cur.close()
            conn.close()

        return render_template(
            "admin/documents_dashboard.html",
            document_counts=document_counts,
            recent_incoming_documents=recent_incoming_documents,
            ongoing_request_rows=ongoing_request_rows,
        )

    def admin_documents_history_view():
        denied = ensure_admin()
        if denied:
            return denied

        filters = {
            "doc_type": request.args.get("doc_type", "all"),
            "client": request.args.get("client", "").strip(),
            "status": request.args.get("status", "all"),
            "direction": request.args.get("direction", "all"),
            "service_type": request.args.get("service_type", "all"),
            "date_from": request.args.get("date_from", "").strip(),
            "date_to": request.args.get("date_to", "").strip(),
            "keyword": request.args.get("keyword", "").strip(),
        }
        all_rows = fetch_admin_document_history_rows()
        history_rows = apply_admin_document_history_filters(all_rows, filters)
        client_options = sorted({row.get("client_name") for row in all_rows if row.get("client_name")})
        status_options = sorted({(row.get("status"), row.get("status_label")) for row in all_rows if row.get("status")})
        document_type_options = [
            ("shikiriosho", "精算書"),
            ("user_mitsumori", "見積依頼書 / 業者向け依頼書"),
            ("user_kaitori_shoudaku", "買取依頼書"),
            ("invoice", "買取明細書"),
            ("user_keisan", "計算書"),
            ("admin_kaitori_shoudaku", "業者買取明細書"),
        ]

        def count_document_history(direction=None, doc_type=None):
            return sum(
                1
                for row in all_rows
                if (direction in (None, "all") or row.get("direction_key") == direction)
                and (doc_type in (None, "all") or row.get("kind") == doc_type)
            )

        document_history_category_cards = [
            {
                "step": "1",
                "title": "クライアントから届いた依頼書",
                "copy": "クライアントから届いた見積依頼書・買取依頼書を確認します。",
                "count_label": f"{count_document_history(direction='client_incoming')}件",
                "url": url_for("admin_documents_history", direction="client_incoming"),
            },
            {
                "step": "2",
                "title": "開花から業者への依頼書",
                "copy": "開花から業者へ送った依頼書の履歴を確認します。",
                "count_label": f"{count_document_history(direction='vendor_outgoing')}件",
                "url": url_for("admin_documents_history", direction="vendor_outgoing"),
            },
            {
                "step": "3",
                "title": "業者から届いた回答書類",
                "copy": "業者から戻ってきた回答書類・明細を確認します。",
                "count_label": f"{count_document_history(direction='vendor_incoming')}件",
                "url": url_for("admin_documents_history", direction="vendor_incoming"),
            },
            {
                "step": "4",
                "title": "クライアントへ返送した書類",
                "copy": "クライアントへ返送した買取明細書・精算書・計算書の履歴を確認します。",
                "count_label": f"{count_document_history(doc_type='invoice', direction='client_outgoing')}件",
                "url": url_for("admin_documents_history", doc_type="invoice", direction="client_outgoing"),
            },
            {
                "step": "5",
                "title": "クライアント返信確認",
                "copy": "クライアント側から届いた見積依頼書や確認書類を後から確認します。",
                "count_label": f"{count_document_history(doc_type='user_mitsumori', direction='client_outgoing')}件",
                "url": url_for("admin_documents_history", doc_type="user_mitsumori", direction="client_outgoing"),
            },
            {
                "step": "6",
                "title": "精算書",
                "copy": "精算書の作成・送付履歴をクライアント別に確認します。",
                "count_label": f"{count_document_history(doc_type='shikiriosho', direction='client_outgoing')}件",
                "url": url_for("admin_documents_history", doc_type="shikiriosho", direction="client_outgoing"),
            },
            {
                "step": "7",
                "title": "計算書",
                "copy": "代行仕入れで作成した計算書と送付履歴を確認します。",
                "count_label": f"{count_document_history(doc_type='user_keisan', direction='client_outgoing')}件",
                "url": url_for("admin_documents_history", doc_type="user_keisan", direction="client_outgoing"),
            },
        ]
        return render_template(
            "admin/documents_history.html",
            history_rows=history_rows,
            filters=filters,
            client_options=client_options,
            status_options=status_options,
            document_type_options=document_type_options,
            document_history_category_cards=document_history_category_cards,
            service_types=SALES_AGENCY_SERVICE_TYPES,
        )

    def admin_company_sales_analytics_view():
        denied = ensure_permission("analytics")
        if denied:
            return denied

        analytics, recent_sales = build_company_sales_analytics_context()
        return render_template(
            "admin/company_sales_analytics.html",
            analytics=analytics,
            recent_sales=recent_sales,
        )

    def sales_agency_my_requests_v2():
        denied = ensure_logged_in()
        if denied:
            return denied

        requests_list = []
        conn, cur = open_cursor()
        try:
            placeholder = "%s" if DATABASE_URL else "?"
            cur.execute(
                f"""
                SELECT sar.id, sar.user_id, sar.service_type, sar.status, sar.admin_note,
                       sar.created_at, sar.processed_at, sar.processed_by, sar.result_notified,
                       u.display_name AS processor_name
                FROM sales_agency_requests sar
                LEFT JOIN users u ON sar.processed_by = u.id
                WHERE sar.user_id = {placeholder}
                ORDER BY sar.created_at DESC
                """,
                (current_user.id,),
            )
            for req_dict in rows_to_dicts(cur.fetchall()):
                req_dict["created_at"] = as_timestamp_text(req_dict.get("created_at"))
                req_dict["processed_at"] = as_timestamp_text(req_dict.get("processed_at"))
                source_request, items = fetch_sales_agency_request_source(req_dict["id"], waiting_only=False)
                req_dict["request_items"] = items
                req_dict["service_name"] = get_service_display_name(req_dict.get("service_type"))
                req_dict["status_label"] = get_sales_agency_status_label(req_dict.get("status"), viewer="client")
                req_dict["pending_appraisal_count"] = source_request.get("pending_appraisal_count") if source_request else 0
                requests_list.append(req_dict)
        finally:
            cur.close()
            conn.close()

        return render_template(
            "sales_agency_requests.html",
            requests=requests_list,
            service_types=SALES_AGENCY_SERVICE_TYPES,
            statuses=SALES_AGENCY_STATUS_CLIENT,
        )

    def admin_sales_agency_requests_v3():
        denied = ensure_admin()
        if denied:
            return denied

        status_filter = request.args.get("status", "all")
        service_filter = request.args.get("service_type", "all")
        box_title = {
            "wholesale": "業者卸販売BOX",
            "auction": "業者オークションBOX",
            "simultaneous": "同時出品BOX",
        }.get(service_filter, "販売代行サービス申請BOX")

        requests_list = []
        stats = {"pending": 0, "approved": 0, "appraising": 0, "completed": 0, "rejected": 0}

        conn, cur = open_cursor()
        try:
            params = []
            conditions = []
            placeholder = "%s" if DATABASE_URL else "?"

            if status_filter != "all":
                conditions.append(f"sar.status = {placeholder}")
                params.append(status_filter)
            if service_filter != "all":
                conditions.append(f"sar.service_type = {placeholder}")
                params.append(service_filter)

            where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
            cur.execute(
                f"""
                SELECT sar.*, u.display_name AS user_name, u.username,
                       p.display_name AS processor_name
                FROM sales_agency_requests sar
                JOIN users u ON sar.user_id = u.id
                LEFT JOIN users p ON sar.processed_by = p.id
                {where_clause}
                ORDER BY CASE sar.status
                    WHEN 'pending' THEN 0
                    WHEN 'approved' THEN 1
                    WHEN 'appraising' THEN 2
                    WHEN 'completed' THEN 3
                    WHEN 'rejected' THEN 4
                    ELSE 9
                END, sar.created_at DESC
                """,
                tuple(params),
            )
            for request_row in rows_to_dicts(cur.fetchall()):
                source_request, items = fetch_sales_agency_request_source(request_row["id"], waiting_only=False)
                request_row["merchandise_items"] = items
                request_row["service_name"] = get_service_display_name(request_row.get("service_type"))
                request_row["client_name"] = request_row.get("user_name") or request_row.get("username") or "未設定"
                request_row["status_label"] = get_sales_agency_status_label(request_row.get("status"), viewer="admin")
                request_row["pending_appraisal_count"] = source_request.get("pending_appraisal_count") if source_request else 0
                request_row["request_can_create_documents"] = (
                    request_row.get("service_type") == "wholesale"
                    and request_row.get("status") in {"approved", "appraising"}
                    and (request_row.get("pending_appraisal_count") or 0) > 0
                    and not request_row.get("created_mitsumori_id")
                    and not request_row.get("created_invoice_id")
                )
                request_row["available_actions"] = {
                    "pending": ["approve", "reject"],
                    "approved": ["appraising", "reject"],
                    "appraising": ["complete", "reject"],
                }.get(request_row.get("status"), [])
                request_row["created_at"] = as_timestamp_text(request_row.get("created_at"))
                request_row["processed_at"] = as_timestamp_text(request_row.get("processed_at"))
                requests_list.append(request_row)

            stat_params = []
            stat_conditions = []
            if service_filter != "all":
                stat_conditions.append(f"service_type = {placeholder}")
                stat_params.append(service_filter)
            stat_where_clause = f"WHERE {' AND '.join(stat_conditions)}" if stat_conditions else ""
            cur.execute(
                f"SELECT status, COUNT(*) AS cnt FROM sales_agency_requests {stat_where_clause} GROUP BY status",
                tuple(stat_params),
            )
            for row in rows_to_dicts(cur.fetchall()):
                if row.get("status") in stats:
                    stats[row["status"]] = row.get("cnt", 0)
        finally:
            cur.close()
            conn.close()

        return render_template(
            "admin/sales_agency_requests.html",
            requests=requests_list,
            stats=stats,
            status_filter=status_filter,
            service_filter=service_filter,
            box_title=box_title,
            service_types=SALES_AGENCY_SERVICE_TYPES,
            statuses=SALES_AGENCY_STATUS,
        )

    def admin_sales_agency_process_v3(id):
        denied = ensure_admin()
        if denied:
            return jsonify({"success": False, "error": "権限がありません。"}), 403

        payload = request.get_json(silent=True) or {}
        action = (payload.get("action") or request.form.get("action") or "").strip()
        admin_note = (payload.get("admin_note") or request.form.get("admin_note") or "").strip()

        action_to_status = {
            "approve": "approved",
            "appraising": "appraising",
            "complete": "completed",
            "reject": "rejected",
        }
        if action not in action_to_status:
            return jsonify({"success": False, "error": "無効な操作です。"}), 400

        conn, cur = open_cursor()
        try:
            placeholder = "%s" if DATABASE_URL else "?"
            cur.execute(f"SELECT * FROM sales_agency_requests WHERE id = {placeholder}", (id,))
            row = cur.fetchone()
            if not row:
                return jsonify({"success": False, "error": "対象の申請が見つかりません。"}), 404
            req = dict(row)

            allowed_actions = {
                "pending": {"approve", "reject"},
                "approved": {"appraising", "reject"},
                "appraising": {"complete", "reject"},
            }
            current_status = req.get("status") or "pending"
            if action not in allowed_actions.get(current_status, set()):
                return jsonify({"success": False, "error": "現在の状態ではこの操作はできません。"}), 400

            now = datetime.now()
            new_status = action_to_status[action]
            cur.execute(
                f"""
                UPDATE sales_agency_requests
                SET status = {placeholder},
                    admin_note = {placeholder},
                    processed_at = {placeholder},
                    processed_by = {placeholder}
                WHERE id = {placeholder}
                """,
                (
                    new_status,
                    admin_note,
                    now if DATABASE_URL else now.strftime("%Y-%m-%d %H:%M:%S"),
                    current_user.id,
                    id,
                ),
            )

            if req.get("service_type") == "wholesale":
                appraisal_status = None
                if new_status in {"approved", "appraising"}:
                    appraisal_status = "waiting"
                elif new_status == "completed":
                    appraisal_status = "completed"
                elif new_status == "rejected":
                    appraisal_status = "none"

                if appraisal_status is not None:
                    cur.execute(
                        f"""
                        UPDATE merchandise
                        SET appraisal_status = {placeholder},
                            updated_at = {placeholder},
                            updated_by = {placeholder}
                        WHERE id IN (
                            SELECT merchandise_id
                            FROM sales_agency_request_items
                            WHERE request_id = {placeholder}
                        )
                        """,
                        (
                            appraisal_status,
                            now if DATABASE_URL else now.strftime("%Y-%m-%d %H:%M:%S"),
                            current_user.id,
                            id,
                        ),
                    )

            cur.execute(f"SELECT * FROM users WHERE id = {placeholder}", (req["user_id"],))
            user_row = cur.fetchone()
            user = dict(user_row) if user_row else None

            conn.commit()
        except Exception as exc:
            traceback.print_exc()
            conn.rollback()
            return jsonify({"success": False, "error": str(exc)}), 500
        finally:
            cur.close()
            conn.close()

        if user and user.get("line_user_id") and callable(send_line_push):
            try:
                service_name = get_service_display_name(req.get("service_type"))
                status_text = get_sales_agency_status_label(new_status, viewer="client")
                message = f"販売代行サービスの申請状況を更新しました。\n{service_name}: {status_text}"
                if admin_note:
                    message += f"\n\n備考: {admin_note}"
                send_line_push(user["line_user_id"], message, line_account_id=user.get("line_account_id"))
            except Exception:
                traceback.print_exc()

        return jsonify(
            {
                "success": True,
                "status": new_status,
                "status_label": get_sales_agency_status_label(new_status, viewer="admin"),
            }
        )

    def admin_mitsumori_add_v3():
        denied = ensure_admin()
        if denied:
            return denied

        source_request = None
        source_request_products = []
        request_id = request.args.get("request_id", type=int)
        target_user_id = request.args.get("target_user_id", type=int)

        if request.method == "POST":
            request_id = request.form.get("request_id", type=int)
            target_user_id = request.form.get("target_user_id", type=int)
            source_service_type = (request.form.get("source_service_type") or "normal").strip() or "normal"
            issue_date = request.form.get("issue_date")
            valid_until = request.form.get("valid_until") or None
            company_name = request.form.get("company_name", "").strip()
            department = request.form.get("department", "").strip()
            contact_person = request.form.get("contact_person", "").strip()
            address = request.form.get("address", "").strip()
            subject = request.form.get("subject", "").strip()
            notes = request.form.get("notes", "").strip()
            raw_status = request.form.get("status", "draft")
            document_status = "completed" if raw_status == "completed" else "draft"
            invoice_status = "sent" if raw_status == "completed" else "draft"
            items_data, total_amount = build_mitsumori_items_from_form()

            if request_id:
                source_request, source_request_products = fetch_sales_agency_request_source(request_id, waiting_only=True)
                if source_request:
                    source_service_type = source_request.get("service_type") or source_service_type
                    target_user_id = target_user_id or source_request.get("user_id")

            if not target_user_id:
                flash("対象クライアントを特定できませんでした。", "error")
                return redirect(request.url)
            if not items_data:
                flash("見積依頼書に追加する商品を選択してください。", "error")
                return redirect(request.url)

            conn, cur = open_cursor()
            try:
                now = datetime.now()
                document_no = generate_admin_mitsumori_document_no()
                invoice_no = generate_admin_kaitori_document_no()
                auto_note = f"見積依頼書 {document_no} の内容から自動作成"
                placeholder = "%s" if DATABASE_URL else "?"

                if DATABASE_URL:
                    cur.execute(
                        """
                        INSERT INTO user_mitsumori
                        (document_no, user_id, issue_date, valid_until, company_name, department,
                         contact_person, address, subject, total_amount, notes, status)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        RETURNING id
                        """,
                        (
                            document_no,
                            target_user_id,
                            issue_date,
                            valid_until,
                            company_name,
                            department,
                            contact_person,
                            address,
                            subject,
                            total_amount,
                            notes,
                            document_status,
                        ),
                    )
                    mitsumori_id = cur.fetchone()["id"]
                else:
                    cur.execute(
                        """
                        INSERT INTO user_mitsumori
                        (document_no, user_id, issue_date, valid_until, company_name, department,
                         contact_person, address, subject, total_amount, notes, status)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            document_no,
                            target_user_id,
                            issue_date,
                            valid_until,
                            company_name,
                            department,
                            contact_person,
                            address,
                            subject,
                            total_amount,
                            notes,
                            document_status,
                        ),
                    )
                    mitsumori_id = cur.lastrowid

                for item in items_data:
                    cur.execute(
                        f"""
                        INSERT INTO user_mitsumori_items
                        (mitsumori_id, item_no, item_name, merchandise_id, quantity, unit, unit_price, amount)
                        VALUES ({placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder})
                        """,
                        (
                            mitsumori_id,
                            item["item_no"],
                            item["item_name"],
                            item["merchandise_id"],
                            item["quantity"],
                            item["unit"],
                            item["unit_price"],
                            item["amount"],
                        ),
                    )

                if DATABASE_URL:
                    cur.execute(
                        """
                        INSERT INTO invoices
                        (invoice_no, sender_id, issue_date, total_amount, notes, status, created_at, service_type)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        RETURNING id
                        """,
                        (invoice_no, target_user_id, issue_date, total_amount, auto_note, invoice_status, now, source_service_type),
                    )
                    invoice_id = cur.fetchone()["id"]
                else:
                    cur.execute(
                        """
                        INSERT INTO invoices
                        (invoice_no, sender_id, issue_date, total_amount, notes, status, created_at, service_type)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            invoice_no,
                            target_user_id,
                            issue_date,
                            total_amount,
                            auto_note,
                            invoice_status,
                            now.strftime("%Y-%m-%d %H:%M:%S"),
                            source_service_type,
                        ),
                    )
                    invoice_id = cur.lastrowid

                for item in items_data:
                    cur.execute(
                        f"""
                        INSERT INTO invoice_items
                        (invoice_id, item_no, product_name, merchandise_id, quantity, unit, unit_price, amount)
                        VALUES ({placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder})
                        """,
                        (
                            invoice_id,
                            item["item_no"],
                            item["item_name"],
                            item["merchandise_id"],
                            item["quantity"],
                            item["unit"],
                            item["unit_price"],
                            item["amount"],
                        ),
                    )

                if request_id:
                    cur.execute(
                        f"""
                        UPDATE sales_agency_requests
                        SET created_mitsumori_id = {placeholder},
                            created_invoice_id = {placeholder},
                            documents_created_at = {placeholder},
                            status = CASE
                                WHEN status IN ('pending', 'approved') THEN 'appraising'
                                ELSE status
                            END
                        WHERE id = {placeholder}
                        """,
                        (
                            mitsumori_id,
                            invoice_id,
                            now if DATABASE_URL else now.strftime("%Y-%m-%d %H:%M:%S"),
                            request_id,
                        ),
                    )

                conn.commit()
                flash("見積依頼書と買取明細書を作成しました。", "success")
                if request_id:
                    return redirect(url_for("admin_sales_agency_requests", service_type=source_service_type))
                return redirect(url_for("admin_mitsumori_view", id=mitsumori_id))
            except Exception as exc:
                traceback.print_exc()
                conn.rollback()
                flash(f"見積依頼書の作成に失敗しました: {exc}", "error")
                return redirect(request.url)
            finally:
                cur.close()
                conn.close()

        if request_id:
            source_request, source_request_products = fetch_sales_agency_request_source(request_id, waiting_only=True)
            if not source_request:
                flash("対象の申請が見つかりません。", "error")
                return redirect(url_for("admin_sales_agency_requests"))
            target_user_id = target_user_id or source_request.get("user_id")
            if source_request.get("created_mitsumori_id") or source_request.get("created_invoice_id"):
                flash("この申請ではすでに書類が作成されています。", "info")
                return redirect(url_for("admin_sales_agency_requests", service_type=source_request.get("service_type")))
            if not source_request_products:
                flash("査定待ちの商品がないため、見積依頼書を作成できません。", "error")
                return redirect(url_for("admin_sales_agency_requests", service_type=source_request.get("service_type")))

        today = datetime.now()
        default_valid_until = (today + timedelta(days=30)).strftime("%Y-%m-%d")
        return render_template(
            "admin/mitsumori_form.html",
            mitsumori=None,
            items=[],
            today=today.strftime("%Y-%m-%d"),
            document_no=generate_admin_mitsumori_document_no(),
            default_valid_until=default_valid_until,
            company_name_default=source_request.get("client_name") if source_request else "",
            department_default="",
            contact_person_default="",
            address_default="",
            subject_default=f"{source_request.get('service_name')} 見積依頼書" if source_request else "見積依頼書",
            notes_default="",
            source_request=source_request,
            source_request_products=source_request_products,
            target_user_id=target_user_id,
        )

    original_admin_master_add = app.view_functions.get("admin_master_add")
    original_admin_master_edit = app.view_functions.get("admin_master_edit")
    original_admin_master_delete = app.view_functions.get("admin_master_delete")
    original_admin_master_settings_init = app.view_functions.get("admin_master_settings_init")

    def user_master_settings_redirect():
        denied = ensure_admin()
        if denied:
            return denied
        return redirect(url_for("admin_master_settings"))

    def user_master_action_redirect(*_args, **_kwargs):
        denied = ensure_admin()
        if denied:
            return denied
        return redirect(url_for("admin_master_settings"))

    def synced_admin_master_add(table_name):
        response = original_admin_master_add(table_name)
        try:
            sync_shared_master_settings()
        except Exception as exc:
            traceback.print_exc()
            flash(f"マスター設定の同期に失敗しました: {exc}", "error")
        return response

    def synced_admin_master_edit(table_name, id):
        response = original_admin_master_edit(table_name, id)
        try:
            sync_shared_master_settings()
        except Exception as exc:
            traceback.print_exc()
            flash(f"マスター設定の同期に失敗しました: {exc}", "error")
        return response

    def synced_admin_master_delete(table_name, id):
        response = original_admin_master_delete(table_name, id)
        try:
            sync_shared_master_settings()
        except Exception as exc:
            traceback.print_exc()
            flash(f"マスター設定の同期に失敗しました: {exc}", "error")
        return response

    def synced_admin_master_settings_init():
        if original_admin_master_settings_init is None:
            return redirect(url_for("admin_master_settings"))
        response = original_admin_master_settings_init()
        try:
            sync_shared_master_settings()
        except Exception as exc:
            traceback.print_exc()
            flash(f"マスター設定の同期に失敗しました: {exc}", "error")
        return response

    def edit_item_v2(id):
        if not current_user.is_admin():
            flash("管理者権限が必要です。", "error")
            return redirect(url_for("index"))

        can_edit_merchandise = getattr(current_user, "can_edit_merchandise", None)
        if callable(can_edit_merchandise) and not can_edit_merchandise():
            flash("商品の編集権限がありません。", "error")
            return redirect(url_for("disposal_options"))

        conn, cur = open_cursor()
        placeholder = "%s" if DATABASE_URL else "?"
        try:
            cur.execute(f"SELECT * FROM merchandise WHERE id = {placeholder}", (id,))
            item = cur.fetchone()
            if not item:
                flash("商品が見つかりません。", "error")
                fallback = url_for("admin_items")
                if callable(get_item_back_url):
                    try:
                        fallback = get_item_back_url(fallback)
                    except Exception:
                        pass
                return redirect(fallback)

            item_dict = dict(item)

            if request.method == "POST":
                photo_path = request.form.get("google_drive_photo_path") or item_dict.get("photo_path")
                uploaded_photo = save_uploaded_image(request.files.get("photo"))
                if uploaded_photo:
                    photo_path = uploaded_photo

                additional_photos = normalize_path_list(item_dict.get("additional_photos"))
                additional_photos.extend(normalize_path_list(request.form.get("google_drive_additional_paths")))
                for file_storage in request.files.getlist("additional_photos"):
                    if len(additional_photos) >= 19:
                        break
                    saved_path = save_uploaded_image(file_storage, prefix=f"{len(additional_photos) + 2}_")
                    if saved_path:
                        additional_photos.append(saved_path)
                delete_photos = set(request.form.getlist("delete_photos"))
                additional_photos = [path for path in normalize_path_list(additional_photos) if path not in delete_photos]
                additional_photos_json = json.dumps(additional_photos) if additional_photos else None

                id_document_path = item_dict.get("id_document_path")
                uploaded_id_document = save_uploaded_document(request.files.get("id_document"), "id")
                if uploaded_id_document:
                    id_document_path = uploaded_id_document

                consent_form_path = item_dict.get("consent_form_path")
                uploaded_consent_form = save_uploaded_document(request.files.get("consent_form"), "consent")
                if uploaded_consent_form:
                    consent_form_path = uploaded_consent_form

                purchase_price = money_value("purchase_price", item_dict.get("purchase_price"))
                wholesale_fee_rate = float_value("wholesale_fee_rate", item_dict.get("wholesale_fee_rate"))
                wholesale_price = money_value("wholesale_price", item_dict.get("wholesale_price"))
                if wholesale_price <= 0:
                    wholesale_price = int(round(purchase_price * (1 + wholesale_fee_rate / 100)))

                item_status = (request.form.get("item_status") or "unlisted").strip()
                appraisal_status = derive_appraisal_status(item_status)
                is_listed_db_value = db_boolean_param(item_status in {"listed", "sold"})
                sale_date_value = resolve_sale_date_for_status(
                    item_status,
                    form_value("sale_date", item_dict.get("sale_date")) or None,
                )

                is_kaika_scope = False
                if callable(is_kaika_inventory_item):
                    try:
                        is_kaika_scope = is_kaika_inventory_item(item_dict)
                    except Exception:
                        is_kaika_scope = False

                default_sale_type = "normal" if is_kaika_scope else "photo_packing,normal"
                sale_type_value = form_value("sale_type", item_dict.get("sale_type") or default_sale_type) or default_sale_type
                if is_kaika_scope and callable(normalize_kaika_sale_type):
                    try:
                        sale_type_value = normalize_kaika_sale_type(sale_type_value)
                    except Exception:
                        pass

                sale_price_value = money_value("sale_price", item_dict.get("sale_price"))
                sales_destination_value = form_value("sales_destination", item_dict.get("sales_destination"))
                if is_kaika_scope and callable(resolve_kaika_sales_destination):
                    try:
                        sales_destination_value = resolve_kaika_sales_destination(sale_type_value, sales_destination_value)
                    except Exception:
                        pass

                commission_value = money_value("commission", item_dict.get("commission"))
                if is_kaika_scope and callable(calculate_kaika_marketplace_fee):
                    try:
                        commission_value = calculate_kaika_marketplace_fee(
                            sale_type_value,
                            sale_price_value,
                            form_value("commission_rate"),
                            form_value("commission"),
                        )
                    except Exception:
                        pass
                is_shipped_db_value = db_boolean_param(bool(item_dict.get("is_shipped")))

                update_fields = [
                    ("purchase_date", form_value("purchase_date", item_dict.get("purchase_date")) or None),
                    ("photo_path", photo_path),
                    ("additional_photos", additional_photos_json),
                    ("product_name", form_value("product_name", item_dict.get("product_name"))),
                    ("kaika_product_code", form_value("kaika_product_code", item_dict.get("kaika_product_code"))),
                    ("brand_name", form_value("brand_name", item_dict.get("brand_name"))),
                    ("model_number", form_value("model_number", item_dict.get("model_number"))),
                    ("item_condition", form_value("item_condition", item_dict.get("item_condition"))),
                    ("store_name", form_value("store_name", item_dict.get("store_name"))),
                    ("supplier_detail", form_value("supplier_detail", item_dict.get("supplier_detail"))),
                    ("id_document_path", id_document_path),
                    ("consent_form_path", consent_form_path),
                    ("wholesale_price", wholesale_price),
                    ("wholesale_fee_rate", wholesale_fee_rate),
                    ("purchase_price", purchase_price),
                    ("payment_method", form_value("payment_method", item_dict.get("payment_method"))),
                    ("listing_price", money_value("listing_price", item_dict.get("listing_price"))),
                    ("expected_shipping", money_value("expected_shipping", item_dict.get("expected_shipping"))),
                    ("expected_commission", money_value("expected_commission", item_dict.get("expected_commission"))),
                    ("is_listed", is_listed_db_value),
                    ("listing_date", (form_value("listing_date", item_dict.get("listing_date")) or None) if item_status in {"listed", "sold"} else None),
                    ("sale_date", sale_date_value),
                    ("sale_type", sale_type_value),
                    ("sale_price", sale_price_value),
                    ("shipping_cost", money_value("shipping_cost", item_dict.get("shipping_cost"))),
                    ("sales_destination", sales_destination_value),
                    ("commission", commission_value),
                    ("is_shipped", is_shipped_db_value),
                    ("notes", form_value("notes", item_dict.get("notes") or "") or ""),
                    ("updated_by", current_user.id),
                    ("appraisal_status", appraisal_status),
                ]
                set_sql = ", ".join([f"{column} = {placeholder}" for column, _ in update_fields] + ["updated_at = CURRENT_TIMESTAMP"])
                cur.execute(
                    f"UPDATE merchandise SET {set_sql} WHERE id = {placeholder}",
                    tuple(value for _, value in update_fields) + (id,),
                )
                conn.commit()
                flash("商品を更新しました。", "success")

                back_url = resolve_back_url(request.form.get("back_url", ""), "")
                return redirect(back_url or fallback_item_redirect(item_dict))
        except Exception as exc:
            traceback.print_exc()
            conn.rollback()
            flash(f"商品更新に失敗しました: {exc}", "error")
            retry_back_url = resolve_back_url(request.form.get("back_url", ""), "")
            return redirect(url_for("edit_item", id=id, back_url=retry_back_url))
        finally:
            cur.close()
            conn.close()

        item_dict = dict(item)
        if item_dict.get("photo_path"):
            item_dict["photo_path"] = str(item_dict["photo_path"]).replace("\\", "/")
        item_dict["additional_photos_list"] = normalize_path_list(item_dict.get("additional_photos"))

        fallback_back_url = fallback_item_redirect(item_dict)
        if callable(get_item_back_url):
            try:
                back_url = get_item_back_url(fallback_back_url)
            except Exception:
                back_url = fallback_back_url
        else:
            back_url = resolve_back_url(request.args.get("back_url", ""), "") or resolve_back_url(request.referrer or "", "") or fallback_back_url

        is_kaika_scope = False
        if callable(is_kaika_inventory_item):
            try:
                is_kaika_scope = is_kaika_inventory_item(item_dict)
            except Exception:
                is_kaika_scope = False

        return render_template(
            "form.html",
            item=item_dict,
            back_url=back_url,
            fee_settings=get_fee_settings(),
            is_kaika_scope=is_kaika_scope,
        )

    def admin_add_item_v2():
        denied = ensure_admin()
        if denied:
            return denied

        mode = (request.form.get("mode") or request.args.get("mode") or "admin").strip().lower()
        if mode not in {"admin", "user"}:
            mode = "admin"

        if request.method == "POST":
            conn, cur = open_cursor()
            placeholder = "%s" if DATABASE_URL else "?"
            try:
                target_user_id = current_user.id
                scope_value = "admin"
                if mode == "user":
                    target_user_id = to_int_local(request.form.get("target_user_id"))
                    if target_user_id <= 0:
                        flash("登録先ユーザーを選択してください。", "error")
                        return redirect(url_for("admin_add_item", mode="user"))
                    cur.execute(f"SELECT id, role FROM users WHERE id = {placeholder}", (target_user_id,))
                    target_user = cur.fetchone()
                    target_user = dict(target_user) if target_user else None
                    if not target_user or target_user.get("role") != "user":
                        flash("登録先ユーザーが見つかりません。", "error")
                        return redirect(url_for("admin_add_item", mode="user"))
                    scope_value = "user"

                purchase_price = money_value("purchase_price")
                wholesale_fee_rate = float_value("wholesale_fee_rate")
                wholesale_price = money_value("wholesale_price")
                if wholesale_price <= 0:
                    wholesale_price = int(round(purchase_price * (1 + wholesale_fee_rate / 100)))

                item_status = (request.form.get("item_status") or "unlisted").strip()
                appraisal_status = derive_appraisal_status(item_status)
                is_listed_db_value = db_boolean_param(item_status in {"listed", "sold"})
                sale_date_value = resolve_sale_date_for_status(item_status, request.form.get("sale_date") or None)
                default_sale_type = "normal" if mode == "admin" else "photo_packing,normal"
                sale_type_value = request.form.get("sale_type") or default_sale_type
                if mode == "admin" and callable(normalize_kaika_sale_type):
                    try:
                        sale_type_value = normalize_kaika_sale_type(sale_type_value)
                    except Exception:
                        pass

                sale_price_value = money_value("sale_price")
                sales_destination_value = form_value("sales_destination") or None
                if mode == "admin" and callable(resolve_kaika_sales_destination):
                    try:
                        sales_destination_value = resolve_kaika_sales_destination(sale_type_value, sales_destination_value)
                    except Exception:
                        pass

                commission_value = money_value("commission")
                if mode == "admin" and callable(calculate_kaika_marketplace_fee):
                    try:
                        commission_value = calculate_kaika_marketplace_fee(
                            sale_type_value,
                            sale_price_value,
                            form_value("commission_rate"),
                            form_value("commission"),
                        )
                    except Exception:
                        pass
                is_shipped_db_value = db_boolean_param("is_shipped" in request.form)

                photo_path = request.form.get("google_drive_photo_path") or save_uploaded_image(request.files.get("photo"))
                additional_photos = normalize_path_list(request.form.get("google_drive_additional_paths"))
                for file_storage in request.files.getlist("additional_photos"):
                    if len(additional_photos) >= 19:
                        break
                    saved_path = save_uploaded_image(file_storage, prefix=f"{len(additional_photos) + 2}_")
                    if saved_path:
                        additional_photos.append(saved_path)
                additional_photos_json = json.dumps(normalize_path_list(additional_photos)) if additional_photos else None

                id_document_path = save_uploaded_document(request.files.get("id_document"), "id")
                consent_form_path = save_uploaded_document(request.files.get("consent_form"), "consent")

                columns = [
                    "user_id",
                    "purchase_date",
                    "photo_path",
                    "additional_photos",
                    "product_name",
                    "kaika_product_code",
                    "brand_name",
                    "model_number",
                    "item_condition",
                    "store_name",
                    "supplier_detail",
                    "id_document_path",
                    "consent_form_path",
                    "wholesale_price",
                    "wholesale_fee_rate",
                    "purchase_price",
                    "payment_method",
                    "listing_price",
                    "expected_shipping",
                    "expected_commission",
                    "is_listed",
                    "listing_date",
                    "sale_date",
                    "sale_type",
                    "sale_price",
                    "shipping_cost",
                    "sales_destination",
                    "commission",
                    "is_shipped",
                    "notes",
                    "scope",
                    "appraisal_status",
                ]
                values = [
                    target_user_id,
                    request.form.get("purchase_date") or None,
                    photo_path,
                    additional_photos_json,
                    request.form.get("product_name"),
                    form_value("kaika_product_code") or None,
                    form_value("brand_name") or None,
                    form_value("model_number") or None,
                    form_value("item_condition") or None,
                    form_value("store_name") or None,
                    form_value("supplier_detail") or None,
                    id_document_path,
                    consent_form_path,
                    wholesale_price,
                    wholesale_fee_rate,
                    purchase_price,
                    form_value("payment_method") or None,
                    money_value("listing_price"),
                    money_value("expected_shipping"),
                    money_value("expected_commission"),
                    is_listed_db_value,
                    (request.form.get("listing_date") or None) if item_status in {"listed", "sold"} else None,
                    sale_date_value,
                    sale_type_value,
                    sale_price_value,
                    money_value("shipping_cost"),
                    sales_destination_value,
                    commission_value,
                    is_shipped_db_value,
                    form_value("notes") or "",
                    scope_value,
                    appraisal_status,
                ]
                placeholders_sql = ", ".join([placeholder] * len(columns))
                cur.execute(
                    f"INSERT INTO merchandise ({', '.join(columns)}) VALUES ({placeholders_sql})",
                    tuple(values),
                )
                conn.commit()
                flash("商品を登録しました。", "success")
                return redirect(url_for("admin_user_products" if mode == "user" else "admin_items"))
            except Exception as exc:
                traceback.print_exc()
                conn.rollback()
                flash(f"商品登録に失敗しました: {exc}", "error")
                return redirect(url_for("admin_add_item", mode=mode, user_id=request.form.get("target_user_id", "")))
            finally:
                cur.close()
                conn.close()

        users = []
        if mode == "user":
            conn, cur = open_cursor()
            try:
                cur.execute("SELECT id, username, display_name, role FROM users WHERE role = 'user' ORDER BY display_name, username")
                users = rows_to_dicts(cur.fetchall())
            finally:
                cur.close()
                conn.close()

        return render_template(
            "admin/item_form.html",
            item=None,
            users=users,
            default_user_id=request.args.get("user_id", ""),
            mode=mode,
            fee_settings=get_fee_settings(),
        )

    sync_shared_master_settings()

    module.get_sales_agency_status_label = get_sales_agency_status_label
    module.get_service_display_name = get_service_display_name
    module.get_document_status_label = get_document_status_label
    module.format_history_date = format_history_date
    module.fetch_admin_document_history_rows = fetch_admin_document_history_rows
    module.apply_admin_document_history_filters = apply_admin_document_history_filters

    if "admin_documents_dashboard" not in app.view_functions:
        app.view_functions["admin_documents_dashboard"] = login_required(admin_documents_dashboard_v2)
    app.view_functions["sales_agency_my_requests"] = login_required(sales_agency_my_requests_v2)
    app.view_functions["admin_sales_agency_requests"] = login_required(admin_sales_agency_requests_v3)
    app.view_functions["admin_sales_agency_process"] = login_required(admin_sales_agency_process_v3)
    app.view_functions["admin_mitsumori_add"] = login_required(admin_mitsumori_add_v3)
    app.view_functions["admin_users"] = login_required(admin_users_clients_view)
    app.view_functions["admin_add_user"] = login_required(admin_add_client_view)
    app.view_functions["admin_edit_user"] = login_required(admin_edit_client_view)
    app.view_functions["delete_user"] = login_required(admin_delete_client_view)
    app.view_functions["edit_item"] = login_required(edit_item_v2)
    app.view_functions["admin_add_item"] = login_required(admin_add_item_v2)
    app.view_functions["user_master_settings"] = login_required(user_master_settings_redirect)
    app.view_functions["user_master_add"] = login_required(user_master_action_redirect)
    app.view_functions["user_master_edit"] = login_required(user_master_action_redirect)
    app.view_functions["user_master_delete"] = login_required(user_master_action_redirect)
    if original_admin_master_add is not None:
        app.view_functions["admin_master_add"] = login_required(synced_admin_master_add)
    if original_admin_master_edit is not None:
        app.view_functions["admin_master_edit"] = login_required(synced_admin_master_edit)
    if original_admin_master_delete is not None:
        app.view_functions["admin_master_delete"] = login_required(synced_admin_master_delete)
    if original_admin_master_settings_init is not None:
        app.view_functions["admin_master_settings_init"] = login_required(synced_admin_master_settings_init)

    if "admin_company_sales_analytics" not in app.view_functions:
        app.add_url_rule(
            "/admin/analytics/company",
            endpoint="admin_company_sales_analytics",
            view_func=login_required(admin_company_sales_analytics_view),
            methods=["GET"],
        )
    else:
        app.view_functions["admin_company_sales_analytics"] = login_required(admin_company_sales_analytics_view)

    if "admin_operator_users" not in app.view_functions:
        app.add_url_rule("/admin/operators", endpoint="admin_operator_users", view_func=login_required(admin_operator_users_view), methods=["GET"])
    else:
        app.view_functions["admin_operator_users"] = login_required(admin_operator_users_view)
    if "admin_operator_add_user" not in app.view_functions:
        app.add_url_rule("/admin/operators/add", endpoint="admin_operator_add_user", view_func=login_required(admin_operator_add_user_view), methods=["GET", "POST"])
    else:
        app.view_functions["admin_operator_add_user"] = login_required(admin_operator_add_user_view)
    if "admin_operator_edit_user" not in app.view_functions:
        app.add_url_rule("/admin/operators/<int:id>/edit", endpoint="admin_operator_edit_user", view_func=login_required(admin_operator_edit_user_view), methods=["GET", "POST"])
    else:
        app.view_functions["admin_operator_edit_user"] = login_required(admin_operator_edit_user_view)
    if "admin_delete_operator" not in app.view_functions:
        app.add_url_rule("/admin/operators/<int:id>/delete", endpoint="admin_delete_operator", view_func=login_required(admin_delete_operator_view), methods=["GET"])
    else:
        app.view_functions["admin_delete_operator"] = login_required(admin_delete_operator_view)
    if "admin_delete_client" not in app.view_functions:
        app.add_url_rule("/admin/users/<int:id>/delete-client", endpoint="admin_delete_client", view_func=login_required(admin_delete_client_view), methods=["GET"])
    else:
        app.view_functions["admin_delete_client"] = login_required(admin_delete_client_view)

    if "admin_documents_history" in app.view_functions:
        app.view_functions["admin_documents_history"] = login_required(admin_documents_history_view)
    else:
        app.add_url_rule(
            "/admin/documents/history",
            endpoint="admin_documents_history",
            view_func=login_required(admin_documents_history_view),
            methods=["GET"],
        )
