# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import sqlite3
import traceback
from datetime import date, datetime, timedelta
from typing import Any

from flask import flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user


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
    "pending": "認証中",
    "approved": "認証済み",
    "appraising": "査定中",
    "completed": "処理完了",
    "rejected": "却下申請",
}

SALES_AGENCY_STATUS_CLIENT = {
    "pending": "認証中",
    "approved": "認証済み",
    "appraising": "査定中",
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
    fetch_sales_agency_request_source = module.fetch_sales_agency_request_source
    generate_admin_mitsumori_document_no = module.generate_admin_mitsumori_document_no
    generate_admin_kaitori_document_no = module.generate_admin_kaitori_document_no
    build_mitsumori_items_from_form = module.build_mitsumori_items_from_form
    generate_password_hash = module.generate_password_hash
    get_monthly_fee = module.get_monthly_fee
    User = module.User
    send_line_push = getattr(module, "send_line_push", None)
    safe_int = getattr(module, "safe_int", None)
    format_sales_destination = getattr(module, "format_sales_destination", None)
    is_kaika_inventory_item = getattr(module, "is_kaika_inventory_item", None)
    build_user_fee_components = getattr(module, "build_user_fee_components", None)
    get_fee_settings = getattr(module, "get_fee_settings", None)

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

    def rows_to_dicts(rows):
        return [dict(row) for row in rows]

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

    def get_sales_agency_status_label(status, viewer="admin"):
        status_key = (status or "").strip()
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
                        "product_name": item.get("product_name") or "-",
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
        search_query = request.args.get("search", "").strip()
        return render_template("admin/clients.html", users=fetch_client_rows(search_query), search_query=search_query)

    def admin_add_client_view():
        denied = ensure_permission("users")
        if denied:
            return denied

        if request.method == "POST":
            username = (request.form.get("username") or "").strip()
            email = (request.form.get("email") or "").strip()
            password = request.form.get("password") or ""
            display_name = (request.form.get("display_name") or "").strip()
            proxy_service_budget = request.form.get("proxy_service_budget", "0").strip()
            tuition_exempt = 1 if request.form.get("tuition_exempt") == "1" else 0

            if not username or not email or not password:
                flash("ユーザー名、メール、パスワードは必須です。", "error")
                return render_template("admin/client_form.html", user=None)
            if len(password) < 6:
                flash("パスワードは6文字以上で設定してください。", "error")
                return render_template("admin/client_form.html", user=None)

            try:
                budget_value = int(proxy_service_budget or 0)
            except ValueError:
                budget_value = 0

            conn, cur = open_cursor()
            try:
                placeholder = "%s" if DATABASE_URL else "?"
                cur.execute(
                    f"""
                    INSERT INTO users
                    (username, email, password_hash, role, display_name, proxy_service_budget, tuition_exempt)
                    VALUES ({placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder})
                    """,
                    (
                        username,
                        email,
                        generate_password_hash(password),
                        "user",
                        display_name or username,
                        budget_value,
                        tuition_exempt,
                    ),
                )
                conn.commit()
                flash("クライアントを追加しました。", "success")
                return redirect(url_for("admin_users"))
            except Exception:
                conn.rollback()
                flash("ユーザー名またはメールアドレスがすでに使用されています。", "error")
            finally:
                cur.close()
                conn.close()

        return render_template("admin/client_form.html", user=None)

    def admin_edit_client_view(id):
        denied = ensure_permission("users")
        if denied:
            return denied

        user = fetch_management_user(id)
        if not user or user.get("role") != "user":
            flash("クライアントが見つかりません。", "error")
            return redirect(url_for("admin_users"))

        if request.method == "POST":
            display_name = (request.form.get("display_name") or "").strip()
            email = (request.form.get("email") or "").strip()
            new_password = request.form.get("new_password") or ""
            proxy_service_budget = request.form.get("proxy_service_budget", "0").strip()
            tuition_exempt = 1 if request.form.get("tuition_exempt") == "1" else 0
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
                        return render_template("admin/client_form.html", user=user)
                    cur.execute(
                        f"""
                        UPDATE users
                        SET display_name = {placeholder},
                            email = {placeholder},
                            password_hash = {placeholder},
                            proxy_service_budget = {placeholder},
                            tuition_exempt = {placeholder},
                            role = 'user'
                        WHERE id = {placeholder}
                        """,
                        (
                            display_name or user.get("username"),
                            email,
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
                            proxy_service_budget = {placeholder},
                            tuition_exempt = {placeholder},
                            role = 'user'
                        WHERE id = {placeholder}
                        """,
                        (
                            display_name or user.get("username"),
                            email,
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

        return render_template("admin/client_form.html", user=user)

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
            except Exception:
                conn.rollback()
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
                        "direction_key": "outgoing",
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
                        "direction_key": "outgoing",
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
                        "direction_key": "vendor" if is_admin_created else "incoming",
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
                        "direction_key": "outgoing",
                        "direction_label": "開花→クライアント",
                        "subject": row.get("subject") or "",
                        "notes": row.get("notes") or "",
                        "detail_endpoint": "user_keisan_view",
                    }
                )

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
            ("user_mitsumori", "見積依頼書"),
            ("invoice", "買取明細書"),
            ("user_keisan", "計算書"),
        ]
        return render_template(
            "admin/documents_history.html",
            history_rows=history_rows,
            filters=filters,
            client_options=client_options,
            status_options=status_options,
            document_type_options=document_type_options,
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

    sync_shared_master_settings()

    module.get_sales_agency_status_label = get_sales_agency_status_label
    module.get_service_display_name = get_service_display_name
    module.get_document_status_label = get_document_status_label
    module.format_history_date = format_history_date
    module.fetch_admin_document_history_rows = fetch_admin_document_history_rows
    module.apply_admin_document_history_filters = apply_admin_document_history_filters

    app.view_functions["admin_documents_dashboard"] = login_required(admin_documents_dashboard_v2)
    app.view_functions["sales_agency_my_requests"] = login_required(sales_agency_my_requests_v2)
    app.view_functions["admin_sales_agency_requests"] = login_required(admin_sales_agency_requests_v3)
    app.view_functions["admin_sales_agency_process"] = login_required(admin_sales_agency_process_v3)
    app.view_functions["admin_mitsumori_add"] = login_required(admin_mitsumori_add_v3)
    app.view_functions["admin_users"] = login_required(admin_users_clients_view)
    app.view_functions["admin_add_user"] = login_required(admin_add_client_view)
    app.view_functions["admin_edit_user"] = login_required(admin_edit_client_view)
    app.view_functions["delete_user"] = login_required(admin_delete_client_view)
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
