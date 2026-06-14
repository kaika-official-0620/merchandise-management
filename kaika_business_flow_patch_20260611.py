# -*- coding: utf-8 -*-
from __future__ import annotations

import calendar
import mimetypes
import os
import sqlite3
from datetime import date, datetime, timedelta
from typing import Any, Iterable

from flask import abort, flash, g, redirect, render_template, request, send_file, url_for
from werkzeug.utils import secure_filename


ALLOWED_VENDOR_EXTENSIONS = {"pdf", "png", "jpg", "jpeg"}
ADMIN_HISTORY_CATEGORIES = [
    ("client_incoming", "顧客からの書類受付", "顧客から届いた依頼書や確認書類"),
    ("vendor_estimate", "業者向け見積依頼書", "開花から業者へ送る見積依頼書"),
    ("vendor", "業者関連書類", "業者から届いたPDF・画像書類"),
    ("kaitori", "顧客向け買取明細書", "顧客へ送付した買取明細書"),
    ("shikiriosho", "精算書", "月額利用料や作業費の精算書"),
    ("kaika_shoudaku", "開花買取承諾書", "開花が買い取る場合の承諾書"),
    ("user_shoudaku", "ユーザー買取承諾書", "ユーザーが作成した買取承諾書"),
    ("keisan", "計算書", "代行仕入れサービス側の計算書"),
    ("kaika_mitsumori", "開花用見積依頼書", "開花商品・個人顧客向けに作成した見積依頼書"),
]
USER_HISTORY_CATEGORIES = [
    ("client_incoming", "見積依頼書", "開花へ送付した見積依頼書"),
    ("kaitori", "買取明細書", "開花から送付された買取明細書"),
    ("shikiriosho", "精算書", "月額利用料や代行費用の精算書"),
    ("keisan", "計算書", "代行仕入れサービスなどの計算書"),
    ("user_shoudaku", "ユーザー買取承諾書", "ユーザーが作成した買取承諾書"),
]
HISTORY_CATEGORIES = ADMIN_HISTORY_CATEGORIES
STATUS_LABELS = {
    "draft": "下書き",
    "in_progress": "作成中",
    "pending": "確認待ち",
    "received": "受領済み",
    "registered": "登録済み",
    "completed": "送付待機",
    "sent": "送付済み",
    "approved": "承認済み",
    "shared": "共有済み",
    "rejected": "却下",
    "processing": "処理中",
}


def apply(module: Any) -> None:
    if getattr(module, "_kaika_business_flow_patch_20260611_applied", False):
        return
    module._kaika_business_flow_patch_20260611_applied = True

    app = module.app
    get_db = module.get_db
    DATABASE_URL = getattr(module, "DATABASE_URL", None)
    RealDictCursor = getattr(module, "RealDictCursor", None)
    login_required = module.login_required
    admin_required = module.admin_required
    current_user = module.current_user
    get_jst_now = getattr(module, "get_jst_now", datetime.now)
    get_monthly_fee = getattr(module, "get_monthly_fee", None)

    def placeholder() -> str:
        return "%s" if DATABASE_URL else "?"

    def open_cursor(dict_rows: bool = True):
        conn = get_db()
        if DATABASE_URL and dict_rows and RealDictCursor is not None:
            cur = conn.cursor(cursor_factory=RealDictCursor)
        else:
            if not DATABASE_URL:
                conn.row_factory = sqlite3.Row
            cur = conn.cursor()
        return conn, cur

    def row_to_dict(row):
        if row is None:
            return None
        return row if isinstance(row, dict) else dict(row)

    def rows_to_dicts(rows: Iterable[Any]) -> list[dict[str, Any]]:
        return [row_to_dict(row) for row in rows]

    def fetch_scalar(sql: str, params: tuple[Any, ...] = ()) -> Any:
        conn, cur = open_cursor()
        try:
            cur.execute(sql, params)
            row = cur.fetchone()
            if row is None:
                return None
            if isinstance(row, dict):
                return next(iter(row.values()))
            return row[0]
        finally:
            cur.close()
            conn.close()

    def table_exists(cur, table_name: str) -> bool:
        if DATABASE_URL:
            cur.execute("SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = %s)", (table_name,))
            row = cur.fetchone()
            return bool(row["exists"] if isinstance(row, dict) else row[0])
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name = ?", (table_name,))
        return cur.fetchone() is not None

    def column_exists(cur, table_name: str, column_name: str) -> bool:
        if DATABASE_URL:
            cur.execute(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM information_schema.columns
                    WHERE table_name = %s AND column_name = %s
                )
                """,
                (table_name, column_name),
            )
            row = cur.fetchone()
            return bool(row["exists"] if isinstance(row, dict) else row[0])
        cur.execute(f"PRAGMA table_info({table_name})")
        return any((row["name"] if isinstance(row, sqlite3.Row) else row[1]) == column_name for row in cur.fetchall())

    def add_column_if_missing(cur, table_name: str, column_name: str, pg_definition: str, sqlite_definition: str) -> None:
        if not table_exists(cur, table_name) or column_exists(cur, table_name, column_name):
            return
        definition = pg_definition if DATABASE_URL else sqlite_definition
        cur.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}")

    performance_indexes_ready = False

    def create_index_if_possible(cur, index_name: str, table_name: str, columns: tuple[str, ...]) -> None:
        if not table_exists(cur, table_name):
            return
        if not all(column_exists(cur, table_name, column) for column in columns):
            return
        column_sql = ", ".join(columns)
        cur.execute(f"CREATE INDEX IF NOT EXISTS {index_name} ON {table_name} ({column_sql})")

    def ensure_performance_indexes() -> None:
        nonlocal performance_indexes_ready
        if performance_indexes_ready:
            return
        conn, cur = open_cursor(False)
        try:
            index_specs = [
                ("idx_user_mitsumori_scope_created", "user_mitsumori", ("document_scope", "created_at")),
                ("idx_user_mitsumori_user_scope_created", "user_mitsumori", ("user_id", "document_scope", "created_at")),
                ("idx_user_mitsumori_document_no", "user_mitsumori", ("document_no",)),
                ("idx_invoices_sender_created", "invoices", ("sender_id", "created_at")),
                ("idx_invoices_created", "invoices", ("created_at",)),
                ("idx_user_shoudaku_user_created", "user_kaitori_shoudaku", ("user_id", "created_at")),
                ("idx_user_shoudaku_created", "user_kaitori_shoudaku", ("created_at",)),
                ("idx_admin_shoudaku_scope_created", "admin_kaitori_shoudaku", ("document_scope", "created_at")),
                ("idx_admin_shoudaku_created", "admin_kaitori_shoudaku", ("created_at",)),
                ("idx_shikiriosho_recipient_issue", "shikiriosho", ("recipient_id", "issue_date")),
                ("idx_shikiriosho_issue", "shikiriosho", ("issue_date",)),
                ("idx_user_keisan_user_created", "user_keisan", ("user_id", "created_at")),
                ("idx_user_keisan_user_admin_status", "user_keisan", ("user_id", "is_admin_created", "status")),
                ("idx_vendor_documents_scope_user_registered", "vendor_documents", ("document_scope", "user_id", "registered_at")),
                ("idx_vendor_documents_scope_registered", "vendor_documents", ("document_scope", "registered_at")),
                ("idx_service_documents_user_created", "service_documents", ("user_id", "created_at")),
                ("idx_sales_agency_requests_service_status_created", "sales_agency_requests", ("service_type", "status", "created_at")),
                ("idx_sales_agency_requests_user_service_created", "sales_agency_requests", ("user_id", "service_type", "created_at")),
                ("idx_sales_agency_request_items_request_status", "sales_agency_request_items", ("request_id", "item_status")),
                ("idx_sale_requests_type_status_user_created", "sale_requests", ("request_type", "status", "user_id", "created_at")),
                ("idx_sale_requests_merchandise_type_status", "sale_requests", ("merchandise_id", "request_type", "status")),
                ("idx_sale_request_events_request_created", "sale_request_events", ("sale_request_id", "created_at")),
                ("idx_disposal_requests_user_status_created", "item_disposal_requests", ("user_id", "status", "created_at")),
                ("idx_disposal_requests_merchandise_status", "item_disposal_requests", ("merchandise_id", "status")),
                ("idx_merchandise_user_created", "merchandise", ("user_id", "created_at")),
                ("idx_merchandise_scope_created", "merchandise", ("scope", "created_at")),
                ("idx_merchandise_user_sale_date", "merchandise", ("user_id", "sale_date")),
            ]
            for index_name, table_name, columns in index_specs:
                create_index_if_possible(cur, index_name, table_name, columns)
            conn.commit()
            performance_indexes_ready = True
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            app.logger.warning("Failed to ensure lightweight performance indexes", exc_info=True)
        finally:
            cur.close()
            conn.close()

    def ensure_schema() -> None:
        conn, cur = open_cursor()
        try:
            if DATABASE_URL:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS vendor_documents (
                        id SERIAL PRIMARY KEY,
                        user_id INTEGER REFERENCES users(id),
                        client_id INTEGER REFERENCES users(id),
                        item_id INTEGER REFERENCES merchandise(id),
                        related_document_id INTEGER,
                        source_request_id INTEGER,
                        document_scope VARCHAR(40) DEFAULT 'user_flow',
                        title VARCHAR(200),
                        original_filename VARCHAR(255) NOT NULL,
                        stored_path TEXT NOT NULL,
                        mime_type VARCHAR(120),
                        file_size INTEGER DEFAULT 0,
                        status VARCHAR(30) DEFAULT 'received',
                        notes TEXT,
                        created_by INTEGER REFERENCES users(id),
                        registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS vendors (
                        id SERIAL PRIMARY KEY,
                        name VARCHAR(200) NOT NULL,
                        contact_name VARCHAR(120),
                        phone VARCHAR(80),
                        email VARCHAR(200),
                        address TEXT,
                        memo TEXT,
                        created_by INTEGER REFERENCES users(id),
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS client_monthly_fee_settings (
                        id SERIAL PRIMARY KEY,
                        user_id INTEGER UNIQUE REFERENCES users(id),
                        monthly_fee_enabled BOOLEAN DEFAULT TRUE,
                        monthly_fee_amount INTEGER DEFAULT 0,
                        free_campaign_enabled BOOLEAN DEFAULT FALSE,
                        free_period_days INTEGER DEFAULT 0,
                        closing_day INTEGER,
                        updated_by INTEGER REFERENCES users(id),
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
                cur.execute("CREATE INDEX IF NOT EXISTS idx_vendor_documents_user ON vendor_documents (user_id, registered_at)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_vendor_documents_item ON vendor_documents (item_id)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_vendor_documents_request ON vendor_documents (source_request_id)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_vendors_name ON vendors (name)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_client_monthly_fee_settings_user ON client_monthly_fee_settings (user_id)")
            else:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS vendor_documents (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id INTEGER,
                        client_id INTEGER,
                        item_id INTEGER,
                        related_document_id INTEGER,
                        source_request_id INTEGER,
                        document_scope TEXT DEFAULT 'user_flow',
                        title TEXT,
                        original_filename TEXT NOT NULL,
                        stored_path TEXT NOT NULL,
                        mime_type TEXT,
                        file_size INTEGER DEFAULT 0,
                        status TEXT DEFAULT 'received',
                        notes TEXT,
                        created_by INTEGER,
                        registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS vendors (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name TEXT NOT NULL,
                        contact_name TEXT,
                        phone TEXT,
                        email TEXT,
                        address TEXT,
                        memo TEXT,
                        created_by INTEGER,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS client_monthly_fee_settings (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id INTEGER UNIQUE,
                        monthly_fee_enabled INTEGER DEFAULT 1,
                        monthly_fee_amount INTEGER DEFAULT 0,
                        free_campaign_enabled INTEGER DEFAULT 0,
                        free_period_days INTEGER DEFAULT 0,
                        closing_day INTEGER,
                        updated_by INTEGER,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
                cur.execute("CREATE INDEX IF NOT EXISTS idx_vendor_documents_user ON vendor_documents (user_id, registered_at)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_vendor_documents_item ON vendor_documents (item_id)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_vendor_documents_request ON vendor_documents (source_request_id)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_vendors_name ON vendors (name)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_client_monthly_fee_settings_user ON client_monthly_fee_settings (user_id)")

            vendor_columns = {
                "client_id": ("INTEGER REFERENCES users(id)", "INTEGER"),
                "related_document_id": ("INTEGER", "INTEGER"),
                "source_request_id": ("INTEGER", "INTEGER"),
                "document_scope": ("VARCHAR(40) DEFAULT 'user_flow'", "TEXT DEFAULT 'user_flow'"),
                "vendor_id": ("INTEGER REFERENCES vendors(id)", "INTEGER"),
                "vendor_name": ("VARCHAR(200)", "TEXT"),
                "extracted_item_name": ("VARCHAR(255)", "TEXT"),
                "vendor_amount": ("INTEGER DEFAULT 0", "INTEGER DEFAULT 0"),
                "customer_amount": ("INTEGER DEFAULT 0", "INTEGER DEFAULT 0"),
                "amount_difference": ("INTEGER DEFAULT 0", "INTEGER DEFAULT 0"),
                "difference_rate": ("NUMERIC(10,2) DEFAULT 0", "REAL DEFAULT 0"),
                "edited_by": ("INTEGER REFERENCES users(id)", "INTEGER"),
                "edited_at": ("TIMESTAMP", "TIMESTAMP"),
                "reception_number": ("VARCHAR(20)", "TEXT"),
                "mime_type": ("VARCHAR(120)", "TEXT"),
                "file_size": ("INTEGER DEFAULT 0", "INTEGER DEFAULT 0"),
                "created_by": ("INTEGER REFERENCES users(id)", "INTEGER"),
            }
            for name, (pg_def, sqlite_def) in vendor_columns.items():
                add_column_if_missing(cur, "vendor_documents", name, pg_def, sqlite_def)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_vendor_documents_vendor ON vendor_documents (vendor_id)")

            monthly_columns = {
                "monthly_fee_enabled": ("BOOLEAN DEFAULT TRUE", "INTEGER DEFAULT 1"),
                "monthly_fee_amount": ("INTEGER DEFAULT 0", "INTEGER DEFAULT 0"),
                "free_campaign_enabled": ("BOOLEAN DEFAULT FALSE", "INTEGER DEFAULT 0"),
                "free_period_days": ("INTEGER DEFAULT 0", "INTEGER DEFAULT 0"),
                "closing_day": ("INTEGER", "INTEGER"),
                "updated_by": ("INTEGER REFERENCES users(id)", "INTEGER"),
            }
            for name, (pg_def, sqlite_def) in monthly_columns.items():
                add_column_if_missing(cur, "client_monthly_fee_settings", name, pg_def, sqlite_def)

            shikiriosho_columns = {
                "settlement_month": ("VARCHAR(7)", "TEXT"),
                "source_type": ("VARCHAR(40)", "TEXT"),
                "monthly_fee_amount": ("INTEGER DEFAULT 0", "INTEGER DEFAULT 0"),
                "manual_expense_total": ("INTEGER DEFAULT 0", "INTEGER DEFAULT 0"),
                "sales_agency_request_id": ("INTEGER", "INTEGER"),
            }
            for name, (pg_def, sqlite_def) in shikiriosho_columns.items():
                add_column_if_missing(cur, "shikiriosho", name, pg_def, sqlite_def)

            conn.commit()
        finally:
            cur.close()
            conn.close()

    def safe_int(value: Any, default: int = 0) -> int:
        try:
            if value is None or str(value).strip() == "":
                return default
            return int(float(str(value).replace(",", "")))
        except (TypeError, ValueError):
            return default

    def bool_from_form(name: str) -> bool:
        return request.form.get(name) in {"1", "true", "on", "yes"}

    def clean_text(value: Any, fallback: str = "-") -> str:
        cleaner = getattr(module, "clean_display_text", None)
        if callable(cleaner):
            return cleaner(value, fallback=fallback)
        text = str(value or "").strip()
        return text or fallback

    def display_user_name(row: dict[str, Any] | None) -> str:
        if not row:
            return "-"
        return (
            clean_text(row.get("display_name"), "")
            or clean_text(row.get("last_name"), "")
            or clean_text(row.get("username"), "")
            or f"ID:{row.get('id')}"
        )

    def format_date(value: Any) -> str:
        if not value:
            return "-"
        if isinstance(value, (datetime, date)):
            return value.strftime("%Y-%m-%d")
        text = str(value)
        return text[:10] if len(text) >= 10 else text

    def format_datetime(value: Any) -> str:
        if not value:
            return "-"
        if isinstance(value, datetime):
            return value.strftime("%Y-%m-%d %H:%M")
        text = str(value)
        return text[:16].replace("T", " ") if len(text) >= 16 else text

    def daily_sequence_for_request(row: dict[str, Any], base_date: datetime) -> int | None:
        request_id = safe_int(row.get("source_request_id") or row.get("id"))
        if request_id <= 0:
            return None
        conn, cur = open_cursor()
        try:
            if not table_exists(cur, "sales_agency_requests"):
                return None
            day_text = base_date.strftime("%Y-%m-%d")
            ph = placeholder()
            if DATABASE_URL:
                cur.execute(
                    f"""
                    SELECT COUNT(*) AS count
                    FROM sales_agency_requests
                    WHERE DATE(created_at) = DATE({ph})
                      AND id <= {ph}
                    """,
                    (day_text, request_id),
                )
            else:
                cur.execute(
                    f"""
                    SELECT COUNT(*) AS count
                    FROM sales_agency_requests
                    WHERE date(created_at) = date({ph})
                      AND id <= {ph}
                    """,
                    (day_text, request_id),
                )
            count_row = row_to_dict(cur.fetchone()) or {}
            sequence = safe_int(count_row.get("count"), 0)
            return sequence if sequence > 0 else None
        except Exception:
            return None
        finally:
            cur.close()
            conn.close()

    def reception_number_for(row: dict[str, Any] | None) -> str:
        row = row or {}
        base_value = row.get("created_at") or row.get("registered_at") or row.get("issue_date") or get_jst_now()
        if isinstance(base_value, datetime):
            base_date = base_value
        elif isinstance(base_value, date):
            base_date = datetime.combine(base_value, datetime.min.time())
        else:
            text = str(base_value or "")
            try:
                base_date = datetime.fromisoformat(text[:19].replace("Z", "+00:00")).replace(tzinfo=None)
            except Exception:
                base_date = get_jst_now()
        sequence = safe_int(row.get("daily_sequence"), 0) or daily_sequence_for_request(row, base_date) or safe_int(row.get("id"), 1)
        return f"{base_date.strftime('%y%m%d')}{sequence % 1000:03d}"

    def status_label(status: Any) -> str:
        return STATUS_LABELS.get(str(status or "").strip(), str(status or "-"))

    def category_cards(endpoint: str, selected: str | None = None, admin: bool = False) -> list[dict[str, Any]]:
        cards = []
        categories = ADMIN_HISTORY_CATEGORIES if admin else USER_HISTORY_CATEGORIES
        for idx, (key, title, description) in enumerate(categories, 1):
            url_args = {"category": key}
            cards.append(
                {
                    "key": key,
                    "step": idx,
                    "title": title,
                    "description": description,
                    "url": url_for(endpoint, **url_args),
                    "selected": selected == key,
                    "count_label": f"{count_history_rows(key, admin=admin)}件",
                }
            )
        return cards

    def load_clients() -> list[dict[str, Any]]:
        conn, cur = open_cursor()
        try:
            cur.execute(
                """
                SELECT id, username, display_name, email, created_at
                FROM users
                WHERE role = 'user'
                ORDER BY COALESCE(NULLIF(display_name, ''), username), id
                """
            )
            clients = rows_to_dicts(cur.fetchall())
            for client in clients:
                client["name"] = display_user_name(client)
            return clients
        finally:
            cur.close()
            conn.close()

    def load_item_options(user_id: int | None = None, limit: int = 300, scope: str = "user") -> list[dict[str, Any]]:
        conn, cur = open_cursor()
        try:
            ph = placeholder()
            params: list[Any] = []
            where_parts: list[str] = []
            if user_id:
                where_parts.append(f"m.user_id = {ph}")
                params.append(user_id)
            if scope == "kaika":
                scope_clause = "COALESCE(NULLIF(m.scope, ''), 'admin') = 'admin'"
                if table_exists(cur, "users"):
                    scope_clause = f"({scope_clause} AND (m.user_id IS NULL OR COALESCE(u.role, '') IN ('admin', 'owner')))"
                where_parts.append(scope_clause)
            elif scope == "user":
                where_parts.append("(m.user_id IS NOT NULL AND COALESCE(u.role, 'user') NOT IN ('admin', 'owner'))")
            where_sql = "WHERE " + " AND ".join(where_parts) if where_parts else ""
            cur.execute(
                f"""
                SELECT m.id, m.product_name, m.brand_name, m.user_id, m.kaika_product_code,
                       m.photo_path, m.purchase_price, m.listing_price,
                       u.display_name AS owner_display_name, u.username AS owner_username
                FROM merchandise m
                LEFT JOIN users u ON m.user_id = u.id
                {where_sql}
                ORDER BY m.created_at DESC, m.id DESC
                LIMIT {int(limit)}
                """,
                tuple(params),
            )
            items = rows_to_dicts(cur.fetchall())
            for item in items:
                name = clean_text(item.get("product_name"), "")
                code = clean_text(item.get("kaika_product_code"), "")
                owner = clean_text(item.get("owner_display_name"), "") or clean_text(item.get("owner_username"), "")
                item["label"] = f"{name or '商品名未登録'}{(' / ' + code) if code else ''}{(' / ' + owner) if owner and scope != 'kaika' else ''}"
            return items
        finally:
            cur.close()
            conn.close()

    def load_vendors() -> list[dict[str, Any]]:
        ensure_schema()
        conn, cur = open_cursor()
        try:
            cur.execute(
                """
                SELECT *
                FROM vendors
                ORDER BY name, id
                """
            )
            vendors = rows_to_dicts(cur.fetchall())
            for vendor in vendors:
                vendor["display_name"] = clean_text(vendor.get("name"), "業者名未登録")
            return vendors
        finally:
            cur.close()
            conn.close()

    def load_monthly_settings_map(user_ids: list[int] | None = None) -> dict[int, dict[str, Any]]:
        ensure_schema()
        conn, cur = open_cursor()
        try:
            params: tuple[Any, ...] = ()
            where = ""
            if user_ids:
                marks = ",".join([placeholder()] * len(user_ids))
                where = f"WHERE user_id IN ({marks})"
                params = tuple(user_ids)
            cur.execute(
                f"""
                SELECT *
                FROM client_monthly_fee_settings
                {where}
                """,
                params,
            )
            return {int(row["user_id"]): row for row in rows_to_dicts(cur.fetchall()) if row.get("user_id")}
        finally:
            cur.close()
            conn.close()

    def default_monthly_fee_for_user(user_id: int) -> int:
        if not callable(get_monthly_fee):
            return 0
        count = safe_int(
            fetch_scalar(
                f"SELECT COUNT(*) FROM merchandise WHERE user_id = {placeholder()}",
                (user_id,),
            )
        )
        try:
            return safe_int(get_monthly_fee(count))
        except Exception:
            return 0

    def normalized_setting_for_user(user_id: int, existing: dict[str, Any] | None = None) -> dict[str, Any]:
        if not existing:
            return {
                "user_id": user_id,
                "monthly_fee_enabled": True,
                "monthly_fee_amount": default_monthly_fee_for_user(user_id),
                "free_campaign_enabled": False,
                "free_period_days": 0,
                "closing_day": None,
            }
        return {
            "user_id": user_id,
            "monthly_fee_enabled": bool(existing.get("monthly_fee_enabled")),
            "monthly_fee_amount": safe_int(existing.get("monthly_fee_amount")),
            "free_campaign_enabled": bool(existing.get("free_campaign_enabled")),
            "free_period_days": safe_int(existing.get("free_period_days")),
            "closing_day": existing.get("closing_day"),
        }

    def month_bounds(month_text: str | None = None) -> tuple[str, date, date]:
        if not month_text:
            today = get_jst_now().date()
            month_text = today.strftime("%Y-%m")
        try:
            year, month = [int(part) for part in month_text.split("-", 1)]
            first = date(year, month, 1)
        except Exception:
            today = get_jst_now().date()
            first = date(today.year, today.month, 1)
            month_text = first.strftime("%Y-%m")
        last = date(first.year, first.month, calendar.monthrange(first.year, first.month)[1])
        return month_text, first, last

    def effective_monthly_fee(client: dict[str, Any], setting: dict[str, Any], target_month: str) -> int:
        if not setting.get("monthly_fee_enabled"):
            return 0
        amount = safe_int(setting.get("monthly_fee_amount"))
        if not setting.get("free_campaign_enabled") or safe_int(setting.get("free_period_days")) <= 0:
            return amount
        _month_text, first, last = month_bounds(target_month)
        created_at = client.get("created_at")
        created_date = None
        if isinstance(created_at, datetime):
            created_date = created_at.date()
        elif isinstance(created_at, date):
            created_date = created_at
        elif created_at:
            try:
                created_date = datetime.fromisoformat(str(created_at)[:19]).date()
            except ValueError:
                created_date = None
        if not created_date:
            return amount
        free_until = created_date + timedelta(days=safe_int(setting.get("free_period_days")))
        return 0 if created_date <= last and free_until >= first else amount

    def save_vendor_upload(file_storage) -> tuple[str, str, str, int]:
        original_filename = secure_filename(file_storage.filename or "")
        if "." not in original_filename:
            raise ValueError("拡張子がないファイルは登録できません。")
        ext = original_filename.rsplit(".", 1)[1].lower()
        if ext not in ALLOWED_VENDOR_EXTENSIONS:
            raise ValueError("PDF、png、jpg、jpeg のみ登録できます。")
        upload_dir = os.path.join(app.static_folder, "uploads", "vendor_documents")
        os.makedirs(upload_dir, exist_ok=True)
        stamp = get_jst_now().strftime("%Y%m%d%H%M%S%f")
        stored_filename = f"{stamp}_{original_filename}"
        absolute_path = os.path.join(upload_dir, stored_filename)
        file_storage.save(absolute_path)
        stored_path = f"uploads/vendor_documents/{stored_filename}"
        mime_type = file_storage.mimetype or mimetypes.guess_type(original_filename)[0] or "application/octet-stream"
        file_size = os.path.getsize(absolute_path)
        return stored_path, original_filename, mime_type, file_size

    def fetch_vendor_documents(user_id: int | None = None, limit: int = 200, scope: str = "user_flow") -> list[dict[str, Any]]:
        ensure_schema()
        conn, cur = open_cursor()
        try:
            ph = placeholder()
            params: list[Any] = []
            where_parts: list[str] = []
            if user_id:
                where_parts.append(f"vd.user_id = {ph}")
                params.append(user_id)
            if table_exists(cur, "vendor_documents") and column_exists(cur, "vendor_documents", "document_scope"):
                if scope == "kaika":
                    where_parts.append(f"COALESCE(vd.document_scope, 'user_flow') = {ph}")
                    params.append("kaika")
                elif scope == "user_flow":
                    where_parts.append(f"COALESCE(vd.document_scope, 'user_flow') = {ph}")
                    params.append("user_flow")
            where = "WHERE " + " AND ".join(where_parts) if where_parts else ""
            cur.execute(
                f"""
                SELECT vd.*,
                       u.display_name AS client_display_name,
                       u.username AS client_username,
                       editor.display_name AS editor_display_name,
                       editor.username AS editor_username,
                       m.product_name AS item_product_name,
                       m.kaika_product_code AS item_code,
                       v.name AS master_vendor_name
                FROM vendor_documents vd
                LEFT JOIN users u ON vd.user_id = u.id
                LEFT JOIN users editor ON vd.edited_by = editor.id
                LEFT JOIN merchandise m ON vd.item_id = m.id
                LEFT JOIN vendors v ON vd.vendor_id = v.id
                {where}
                ORDER BY vd.registered_at DESC, vd.id DESC
                LIMIT {int(limit)}
                """,
                params,
            )
            docs = rows_to_dicts(cur.fetchall())
            for doc in docs:
                doc["client_name"] = clean_text(doc.get("client_display_name"), "") or clean_text(doc.get("client_username"), "")
                doc["vendor_display_name"] = clean_text(doc.get("master_vendor_name"), "") or clean_text(doc.get("vendor_name"), "未指定")
                doc["item_name"] = clean_text(doc.get("item_product_name"), "") or clean_text(doc.get("extracted_item_name"), "未指定")
                doc["registered_label"] = format_datetime(doc.get("registered_at"))
                doc["edited_label"] = format_datetime(doc.get("edited_at"))
                doc["editor_name"] = clean_text(doc.get("editor_display_name"), "") or clean_text(doc.get("editor_username"), "")
                doc["status_label"] = status_label(doc.get("status"))
                doc["amount_difference"] = safe_int(doc.get("amount_difference"))
                doc["difference_rate"] = safe_int(doc.get("difference_rate"))
                doc["download_url"] = url_for("admin_vendor_document_download", document_id=doc["id"])
                doc["delete_url"] = url_for("admin_vendor_document_delete", document_id=doc["id"])
            return docs
        finally:
            cur.close()
            conn.close()

    def count_history_rows(category: str, admin: bool = False) -> int:
        ensure_schema()
        ensure_performance_indexes()
        category_aliases = {
            "user_mitsumori": "client_incoming",
            "mitsumori_houjin": "vendor_estimate",
            "mitsumori": "client_incoming",
            "invoice": "kaitori",
            "shoudaku": "user_shoudaku",
            "user_kaitori_shoudaku": "user_shoudaku",
            "admin_kaitori_shoudaku": "kaika_shoudaku",
            "user_keisan": "keisan",
            "kaika_estimate": "kaika_mitsumori",
        }
        category = category_aliases.get(category, category)
        cache_key = "_kaika_admin_history_counts" if admin else "_kaika_user_history_counts"
        loading_key = "_kaika_history_counts_loading"
        if not getattr(g, loading_key, False):
            cached_counts = getattr(g, cache_key, None)
            if cached_counts is None:
                setattr(g, loading_key, True)
                try:
                    categories = ADMIN_HISTORY_CATEGORIES if admin else USER_HISTORY_CATEGORIES
                    cached_counts = count_history_rows_bulk([key for key, _title, _description in categories], admin=admin)
                    setattr(g, cache_key, cached_counts)
                finally:
                    setattr(g, loading_key, False)
            if category in cached_counts:
                return safe_int(cached_counts.get(category))
        if not admin and category not in {key for key, _title, _desc in USER_HISTORY_CATEGORIES}:
            return 0
        if category == "client_incoming":
            total = count_history_rows("client_mitsumori", admin=admin)
            if admin:
                total += count_history_rows("user_shoudaku", admin=admin)
            return total
        if category == "vendor_estimate":
            category = "vendor_mitsumori"

        ph = placeholder()
        conn, cur = open_cursor()
        try:
            params_user = None if admin else current_user.id

            def count_from(table_name: str, where_parts: list[str] | None = None, params: list[Any] | tuple[Any, ...] = ()) -> int:
                if not table_exists(cur, table_name):
                    return 0
                where_parts = where_parts or []
                where_sql = "WHERE " + " AND ".join(where_parts) if where_parts else ""
                cur.execute(f"SELECT COUNT(*) AS count FROM {table_name} {where_sql}", tuple(params))
                row = cur.fetchone()
                if row is None:
                    return 0
                if isinstance(row, dict):
                    return safe_int(row.get("count"))
                return safe_int(row[0])

            if category in {"client_mitsumori", "vendor_mitsumori", "kaika_mitsumori"}:
                if not table_exists(cur, "user_mitsumori"):
                    return 0
                where_parts: list[str] = []
                params_list: list[Any] = []
                has_document_scope = column_exists(cur, "user_mitsumori", "document_scope")
                if not admin:
                    where_parts.append(f"user_id = {ph}")
                    params_list.append(params_user)
                if category == "vendor_mitsumori":
                    if has_document_scope:
                        where_parts.append(f"COALESCE(document_scope, '') = {ph}")
                        params_list.append("vendor_outgoing")
                    else:
                        where_parts.append(f"document_no LIKE {ph}")
                        params_list.append("MT-%")
                elif category == "kaika_mitsumori":
                    if has_document_scope:
                        where_parts.append(f"COALESCE(document_scope, '') IN ({ph}, {ph})")
                        params_list.extend(["kaika_vendor_outgoing", "kaika_estimate"])
                    else:
                        where_parts.append("1 = 0")
                else:
                    if has_document_scope:
                        where_parts.append(f"COALESCE(document_scope, 'client_incoming') NOT IN ({ph}, {ph}, {ph})")
                        params_list.extend(["vendor_outgoing", "kaika_vendor_outgoing", "kaika_estimate"])
                    where_parts.append(f"COALESCE(document_no, '') NOT LIKE {ph}")
                    params_list.append("MT-%")
                return count_from("user_mitsumori", where_parts, params_list)

            if category == "kaitori":
                where_parts = [] if admin else [f"sender_id = {ph}"]
                params = [] if admin else [params_user]
                return count_from("invoices", where_parts, params)

            if category == "user_shoudaku":
                where_parts = [] if admin else [f"user_id = {ph}"]
                params = [] if admin else [params_user]
                return count_from("user_kaitori_shoudaku", where_parts, params)

            if category == "kaika_shoudaku":
                if not table_exists(cur, "admin_kaitori_shoudaku"):
                    return 0
                where_parts = []
                params: list[Any] = []
                if column_exists(cur, "admin_kaitori_shoudaku", "document_scope"):
                    where_parts.append(f"COALESCE(document_scope, {ph}) <> {ph}")
                    params.extend(["kaika_shoudaku", "vendor_incoming"])
                return count_from("admin_kaitori_shoudaku", where_parts, params)

            if category == "shikiriosho":
                where_parts = [] if admin else [f"recipient_id = {ph}"]
                params = [] if admin else [params_user]
                return count_from("shikiriosho", where_parts, params)

            if category == "keisan":
                where_parts = [] if admin else [f"user_id = {ph}"]
                params = [] if admin else [params_user]
                if not admin and table_exists(cur, "user_keisan") and column_exists(cur, "user_keisan", "is_admin_created"):
                    where_parts.append(
                        "(COALESCE(is_admin_created, FALSE) = FALSE OR status = 'submitted')"
                        if DATABASE_URL
                        else "(COALESCE(is_admin_created, 0) = 0 OR status = 'submitted')"
                    )
                return count_from("user_keisan", where_parts, params)

            if category == "vendor":
                where_parts: list[str] = []
                params: list[Any] = []
                if table_exists(cur, "vendor_documents") and column_exists(cur, "vendor_documents", "document_scope"):
                    where_parts.append(f"COALESCE(document_scope, 'user_flow') = {ph}")
                    params.append("user_flow")
                if not admin:
                    where_parts.append(f"user_id = {ph}")
                    where_parts.append("COALESCE(status, '') IN ('shared', 'sent')")
                    params.append(params_user)
                return count_from("vendor_documents", where_parts, params)

            if category == "other":
                where_parts = [] if admin else [f"user_id = {ph}"]
                params = [] if admin else [params_user]
                return count_from("service_documents", where_parts, params)

            return 0
        except Exception:
            app.logger.warning("Failed to count history rows for %s", category, exc_info=True)
            return 0
        finally:
            cur.close()
            conn.close()

    def count_history_rows_bulk(category_keys: Iterable[str], admin: bool = False) -> dict[str, int]:
        ensure_schema()
        ensure_performance_indexes()
        allowed_keys = {key for key, _title, _description in (ADMIN_HISTORY_CATEGORIES if admin else USER_HISTORY_CATEGORIES)}
        requested = {key for key in category_keys if admin or key in allowed_keys}
        if not requested:
            return {}

        ph = placeholder()
        conn, cur = open_cursor()
        statements: list[str] = []
        params: list[Any] = []

        def add_count(key: str, table_name: str, where_parts: list[str] | None = None, where_params: list[Any] | tuple[Any, ...] = ()) -> None:
            if not table_exists(cur, table_name):
                return
            where_sql = "WHERE " + " AND ".join(where_parts or []) if where_parts else ""
            statements.append(f"SELECT {ph} AS category, COUNT(*) AS count FROM {table_name} {where_sql}")
            params.append(key)
            params.extend(where_params)

        try:
            params_user = None if admin else current_user.id
            needs_client = "client_incoming" in requested
            needs_vendor_estimate = "vendor_estimate" in requested
            has_mitsumori = table_exists(cur, "user_mitsumori")
            has_mitsumori_scope = has_mitsumori and column_exists(cur, "user_mitsumori", "document_scope")

            if has_mitsumori and needs_client:
                where_parts: list[str] = []
                where_params: list[Any] = []
                if not admin:
                    where_parts.append(f"user_id = {ph}")
                    where_params.append(params_user)
                if has_mitsumori_scope:
                    where_parts.append(f"COALESCE(document_scope, 'client_incoming') NOT IN ({ph}, {ph}, {ph})")
                    where_params.extend(["vendor_outgoing", "kaika_vendor_outgoing", "kaika_estimate"])
                where_parts.append(f"COALESCE(document_no, '') NOT LIKE {ph}")
                where_params.append("MT-%")
                add_count("client_mitsumori", "user_mitsumori", where_parts, where_params)

            if has_mitsumori and needs_vendor_estimate:
                where_parts = []
                where_params = []
                if not admin:
                    where_parts.append(f"user_id = {ph}")
                    where_params.append(params_user)
                if has_mitsumori_scope:
                    where_parts.append(f"COALESCE(document_scope, '') = {ph}")
                    where_params.append("vendor_outgoing")
                else:
                    where_parts.append(f"document_no LIKE {ph}")
                    where_params.append("MT-%")
                add_count("vendor_mitsumori", "user_mitsumori", where_parts, where_params)

            if has_mitsumori and "kaika_mitsumori" in requested:
                where_parts = []
                where_params = []
                if has_mitsumori_scope:
                    where_parts.append(f"COALESCE(document_scope, '') IN ({ph}, {ph})")
                    where_params.extend(["kaika_vendor_outgoing", "kaika_estimate"])
                else:
                    where_parts.append("1 = 0")
                add_count("kaika_mitsumori", "user_mitsumori", where_parts, where_params)

            if "kaitori" in requested:
                add_count("kaitori", "invoices", [] if admin else [f"sender_id = {ph}"], [] if admin else [params_user])

            if "user_shoudaku" in requested or (admin and needs_client):
                add_count("user_shoudaku", "user_kaitori_shoudaku", [] if admin else [f"user_id = {ph}"], [] if admin else [params_user])

            if "kaika_shoudaku" in requested:
                where_parts = []
                where_params = []
                if table_exists(cur, "admin_kaitori_shoudaku") and column_exists(cur, "admin_kaitori_shoudaku", "document_scope"):
                    where_parts.append(f"COALESCE(document_scope, {ph}) <> {ph}")
                    where_params.extend(["kaika_shoudaku", "vendor_incoming"])
                add_count("kaika_shoudaku", "admin_kaitori_shoudaku", where_parts, where_params)

            if "shikiriosho" in requested:
                add_count("shikiriosho", "shikiriosho", [] if admin else [f"recipient_id = {ph}"], [] if admin else [params_user])

            if "keisan" in requested:
                where_parts = [] if admin else [f"user_id = {ph}"]
                where_params = [] if admin else [params_user]
                if not admin and table_exists(cur, "user_keisan") and column_exists(cur, "user_keisan", "is_admin_created"):
                    where_parts.append(
                        "(COALESCE(is_admin_created, FALSE) = FALSE OR status = 'submitted')"
                        if DATABASE_URL
                        else "(COALESCE(is_admin_created, 0) = 0 OR status = 'submitted')"
                    )
                add_count("keisan", "user_keisan", where_parts, where_params)

            if "vendor" in requested:
                where_parts = []
                where_params = []
                if table_exists(cur, "vendor_documents") and column_exists(cur, "vendor_documents", "document_scope"):
                    where_parts.append(f"COALESCE(document_scope, 'user_flow') = {ph}")
                    where_params.append("user_flow")
                if not admin:
                    where_parts.append(f"user_id = {ph}")
                    where_parts.append("COALESCE(status, '') IN ('shared', 'sent')")
                    where_params.append(params_user)
                add_count("vendor", "vendor_documents", where_parts, where_params)

            if "other" in requested:
                add_count("other", "service_documents", [] if admin else [f"user_id = {ph}"], [] if admin else [params_user])

            count_map = {key: 0 for key in requested}
            if statements:
                cur.execute(" UNION ALL ".join(statements), tuple(params))
                for row in rows_to_dicts(cur.fetchall()):
                    count_map[str(row.get("category"))] = safe_int(row.get("count"))
            if "client_incoming" in requested:
                count_map["client_incoming"] = count_map.get("client_mitsumori", 0) + (count_map.get("user_shoudaku", 0) if admin else 0)
            if "vendor_estimate" in requested:
                count_map["vendor_estimate"] = count_map.get("vendor_mitsumori", 0)
            return {key: safe_int(count_map.get(key)) for key in requested}
        except Exception:
            app.logger.warning("Failed to count history rows in bulk", exc_info=True)
            return {key: 0 for key in requested}
        finally:
            cur.close()
            conn.close()

    def build_history_rows(category: str, admin: bool = False, user_id: int | None = None, limit: int = 200) -> list[dict[str, Any]]:
        ensure_schema()
        category_aliases = {
            "user_mitsumori": "client_incoming",
            "mitsumori_houjin": "vendor_estimate",
            "mitsumori": "client_incoming",
            "invoice": "kaitori",
            "shoudaku": "user_shoudaku",
            "user_kaitori_shoudaku": "user_shoudaku",
            "admin_kaitori_shoudaku": "kaika_shoudaku",
            "user_keisan": "keisan",
            "kaika_estimate": "kaika_mitsumori",
        }
        category = category_aliases.get(category, category)
        if not admin and category not in {key for key, _title, _desc in USER_HISTORY_CATEGORIES}:
            return []
        if category == "client_incoming":
            combined: list[dict[str, Any]] = []
            combined.extend(build_history_rows("client_mitsumori", admin=admin, user_id=user_id, limit=limit))
            if admin:
                combined.extend(build_history_rows("user_shoudaku", admin=admin, user_id=user_id, limit=limit))
            combined.sort(key=lambda row: str(row.get("issue_date") or ""), reverse=True)
            return combined[: int(limit)]
        if category == "vendor_estimate":
            category = "vendor_mitsumori"
        ph = placeholder()
        rows: list[dict[str, Any]] = []
        conn, cur = open_cursor()
        try:
            params_user = user_id if user_id else (None if admin else current_user.id)

            def add_row(
                *,
                document_type: str,
                document_no: Any,
                client_name: Any,
                issue_date: Any,
                total_amount: Any = 0,
                status: Any = "",
                detail_url: str | None = None,
                download_url: str | None = None,
                source: str = "",
                can_delete: bool = False,
            ) -> None:
                rows.append(
                    {
                        "document_type": document_type,
                        "document_no": document_no or "-",
                        "client_name": clean_text(client_name, "-"),
                        "issue_date": format_date(issue_date),
                        "total_amount": safe_int(total_amount),
                        "status": status or "",
                        "status_label": status_label(status),
                        "detail_url": detail_url,
                        "download_url": download_url,
                        "source": source,
                        "can_delete": can_delete,
                    }
                )

            if category in {"client_mitsumori", "vendor_mitsumori", "kaika_mitsumori"}:
                where_parts: list[str] = []
                params_list: list[Any] = []
                has_document_scope = table_exists(cur, "user_mitsumori") and column_exists(cur, "user_mitsumori", "document_scope")
                if not admin:
                    where_parts.append(f"user_id = {ph}")
                    params_list.append(params_user)
                if category == "vendor_mitsumori":
                    if has_document_scope:
                        where_parts.append(f"COALESCE(document_scope, '') = {ph}")
                        params_list.append("vendor_outgoing")
                    else:
                        where_parts.append(f"document_no LIKE {ph}")
                        params_list.append("MT-%")
                elif category == "kaika_mitsumori":
                    if has_document_scope:
                        where_parts.append(f"COALESCE(document_scope, '') IN ({ph}, {ph})")
                        params_list.extend(["kaika_vendor_outgoing", "kaika_estimate"])
                    else:
                        where_parts.append("1 = 0")
                else:
                    if has_document_scope:
                        where_parts.append(f"COALESCE(document_scope, 'client_incoming') NOT IN ({ph}, {ph}, {ph})")
                        params_list.extend(["vendor_outgoing", "kaika_vendor_outgoing", "kaika_estimate"])
                    where_parts.append(f"COALESCE(document_no, '') NOT LIKE {ph}")
                    params_list.append("MT-%")
                where = "WHERE " + " AND ".join(where_parts) if where_parts else ""
                cur.execute(f"SELECT * FROM user_mitsumori {where} ORDER BY created_at DESC LIMIT {int(limit)}", tuple(params_list))
                for doc in rows_to_dicts(cur.fetchall()):
                    add_row(
                        document_type=(
                            "業者向け見積依頼書"
                            if category == "vendor_mitsumori"
                            else "開花用見積依頼書"
                            if category == "kaika_mitsumori"
                            else "見積依頼書"
                        ),
                        document_no=doc.get("document_no") or f"ID:{doc.get('id')}",
                        client_name=doc.get("customer_name") or doc.get("user_name") or (current_user.display_name if not admin else ""),
                        issue_date=doc.get("issue_date") or doc.get("created_at"),
                        total_amount=doc.get("total_amount") or 0,
                        status=doc.get("status"),
                        detail_url=url_for("admin_user_mitsumori_view", id=doc["id"]) if admin and "admin_user_mitsumori_view" in app.view_functions else None,
                        source="user_mitsumori",
                    )
            elif category == "kaitori":
                where = "" if admin else f"WHERE sender_id = {ph}"
                params = () if admin else (params_user,)
                cur.execute(
                    f"""
                    SELECT i.*, u.display_name AS client_display_name, u.username AS client_username
                    FROM invoices i
                    LEFT JOIN users u ON i.sender_id = u.id
                    {where}
                    ORDER BY i.created_at DESC
                    LIMIT {int(limit)}
                    """,
                    params,
                )
                for doc in rows_to_dicts(cur.fetchall()):
                    add_row(
                        document_type="買取明細書",
                        document_no=doc.get("document_no") or f"ID:{doc.get('id')}",
                        client_name=doc.get("recipient_name") or doc.get("client_display_name") or doc.get("client_username"),
                        issue_date=doc.get("issue_date") or doc.get("created_at"),
                        total_amount=doc.get("total_amount") or doc.get("subtotal") or 0,
                        status=doc.get("status"),
                        detail_url=url_for("admin_kaitori_view", id=doc["id"]) if admin and "admin_kaitori_view" in app.view_functions else None,
                        source="invoices",
                    )
            elif category == "user_shoudaku":
                where = "" if admin else f"WHERE user_id = {ph}"
                params = () if admin else (params_user,)
                cur.execute(f"SELECT * FROM user_kaitori_shoudaku {where} ORDER BY created_at DESC LIMIT {int(limit)}", params)
                for doc in rows_to_dicts(cur.fetchall()):
                    add_row(
                        document_type="ユーザー買取承諾書",
                        document_no=doc.get("document_no") or f"ID:{doc.get('id')}",
                        client_name=doc.get("customer_name") or doc.get("user_name") or (current_user.display_name if not admin else ""),
                        issue_date=doc.get("issue_date") or doc.get("created_at"),
                        total_amount=doc.get("total_amount") or 0,
                        status=doc.get("status"),
                        detail_url=url_for("admin_user_kaitori_shoudaku_view", id=doc["id"]) if admin and "admin_user_kaitori_shoudaku_view" in app.view_functions else None,
                        source="user_kaitori_shoudaku",
                    )
            elif category == "kaika_shoudaku":
                if table_exists(cur, "admin_kaitori_shoudaku"):
                    where = ""
                    params = ()
                    if column_exists(cur, "admin_kaitori_shoudaku", "document_scope"):
                        where = f"WHERE COALESCE(document_scope, {ph}) <> {ph}"
                        params = ("kaika_shoudaku", "vendor_incoming")
                    cur.execute(f"SELECT * FROM admin_kaitori_shoudaku {where} ORDER BY created_at DESC LIMIT {int(limit)}", params)
                    for doc in rows_to_dicts(cur.fetchall()):
                        add_row(
                            document_type="開花買取承諾書",
                            document_no=doc.get("document_no") or f"ID:{doc.get('id')}",
                            client_name=doc.get("company_name") or "-",
                            issue_date=doc.get("issue_date") or doc.get("created_at"),
                            total_amount=doc.get("total_amount") or 0,
                            status=doc.get("status"),
                            detail_url=url_for("admin_kaitori_shoudaku_view", id=doc["id"]) if admin and "admin_kaitori_shoudaku_view" in app.view_functions else None,
                            source="admin_kaitori_shoudaku",
                        )
            elif category == "shikiriosho":
                where = "" if admin else f"WHERE s.recipient_id = {ph}"
                params = () if admin else (params_user,)
                cur.execute(
                    f"""
                    SELECT s.*, u.display_name AS client_display_name, u.username AS client_username
                    FROM shikiriosho s
                    LEFT JOIN users u ON s.recipient_id = u.id
                    {where}
                    ORDER BY s.issue_date DESC, s.id DESC
                    LIMIT {int(limit)}
                    """,
                    params,
                )
                for doc in rows_to_dicts(cur.fetchall()):
                    add_row(
                        document_type="精算書",
                        document_no=doc.get("document_no") or f"ID:{doc.get('id')}",
                        client_name=doc.get("recipient_name") or doc.get("client_display_name") or doc.get("client_username"),
                        issue_date=doc.get("issue_date") or doc.get("created_at"),
                        total_amount=doc.get("total_amount") or 0,
                        status=doc.get("status"),
                        detail_url=url_for("admin_shikiriosho_view", id=doc["id"]) if admin and "admin_shikiriosho_view" in app.view_functions else None,
                        source="shikiriosho",
                    )
            elif category == "keisan":
                where = "" if admin else f"WHERE user_id = {ph}"
                params = () if admin else (params_user,)
                if not admin and table_exists(cur, "user_keisan") and column_exists(cur, "user_keisan", "is_admin_created"):
                    where += (" AND " if where else "WHERE ") + (
                        "(COALESCE(is_admin_created, FALSE) = FALSE OR status = 'submitted')"
                        if DATABASE_URL
                        else "(COALESCE(is_admin_created, 0) = 0 OR status = 'submitted')"
                    )
                cur.execute(f"SELECT * FROM user_keisan {where} ORDER BY created_at DESC LIMIT {int(limit)}", params)
                for doc in rows_to_dicts(cur.fetchall()):
                    add_row(
                        document_type="計算書",
                        document_no=doc.get("document_no") or f"ID:{doc.get('id')}",
                        client_name=doc.get("customer_name") or doc.get("user_name") or (current_user.display_name if not admin else ""),
                        issue_date=doc.get("issue_date") or doc.get("created_at"),
                        total_amount=doc.get("total_amount") or 0,
                        status=doc.get("status"),
                        detail_url=url_for("admin_auction_keisan_view", id=doc["id"]) if admin and "admin_auction_keisan_view" in app.view_functions else None,
                        source="user_keisan",
                    )
            elif category == "vendor":
                where_parts = []
                params_list = []
                if table_exists(cur, "vendor_documents") and column_exists(cur, "vendor_documents", "document_scope"):
                    where_parts.append(f"COALESCE(vd.document_scope, 'user_flow') = {ph}")
                    params_list.append("user_flow")
                if not admin:
                    where_parts.append(f"vd.user_id = {ph}")
                    where_parts.append("COALESCE(vd.status, '') IN ('shared', 'sent')")
                    params_list.append(params_user)
                where = "WHERE " + " AND ".join(where_parts) if where_parts else ""
                cur.execute(
                    f"""
                    SELECT vd.*, u.display_name AS client_display_name, u.username AS client_username,
                           m.product_name AS item_product_name
                    FROM vendor_documents vd
                    LEFT JOIN users u ON vd.user_id = u.id
                    LEFT JOIN merchandise m ON vd.item_id = m.id
                    {where}
                    ORDER BY vd.registered_at DESC, vd.id DESC
                    LIMIT {int(limit)}
                    """,
                    tuple(params_list),
                )
                for doc in rows_to_dicts(cur.fetchall()):
                    add_row(
                        document_type="業者関連書類",
                        document_no=doc.get("title") or doc.get("original_filename") or f"ID:{doc.get('id')}",
                        client_name=doc.get("client_display_name") or doc.get("client_username"),
                        issue_date=doc.get("registered_at"),
                        total_amount=0,
                        status=doc.get("status"),
                        detail_url=url_for("admin_vendor_documents") if admin else None,
                        download_url=url_for("admin_vendor_document_download", document_id=doc["id"]) if admin else None,
                        source="vendor_documents",
                        can_delete=bool(admin),
                    )
                    if admin and rows:
                        rows[-1]["delete_url"] = url_for("admin_vendor_document_delete", document_id=doc["id"])
            elif category == "other":
                where = "" if admin else f"WHERE user_id = {ph}"
                params = () if admin else (params_user,)
                cur.execute(f"SELECT * FROM service_documents {where} ORDER BY created_at DESC LIMIT {int(limit)}", params)
                for doc in rows_to_dicts(cur.fetchall()):
                    add_row(
                        document_type="その他書類",
                        document_no=doc.get("document_no") or f"ID:{doc.get('id')}",
                        client_name=doc.get("customer_name") or (current_user.display_name if not admin else ""),
                        issue_date=doc.get("created_at"),
                        total_amount=doc.get("total_amount") or 0,
                        status=doc.get("status"),
                        detail_url=url_for("service_document_view", id=doc["id"]) if not admin and "service_document_view" in app.view_functions else None,
                        source="service_documents",
                    )
            return rows
        finally:
            cur.close()
            conn.close()

    def admin_vendor_documents():
        ensure_schema()
        selected_scope = (request.values.get("scope") or "user_flow").strip()
        is_kaika_scope = selected_scope == "kaika"
        clients = [] if is_kaika_scope else load_clients()
        vendors = load_vendors()
        selected_user_id = None if is_kaika_scope else request.values.get("user_id", type=int)
        selected_vendor_id = request.values.get("vendor_id", type=int)
        items = load_item_options(None if is_kaika_scope else selected_user_id, scope="kaika" if is_kaika_scope else "user")
        if request.method == "POST":
            try:
                user_id = None if is_kaika_scope else request.form.get("user_id", type=int)
                vendor_id = request.form.get("vendor_id", type=int)
                item_id = request.form.get("item_id", type=int)
                related_document_id = request.form.get("related_document_id", type=int)
                source_request_id = request.form.get("source_request_id", type=int)
                title = (request.form.get("title") or "").strip()
                status = (request.form.get("status") or "received").strip()
                notes = (request.form.get("notes") or "").strip()
                extracted_item_name = (request.form.get("extracted_item_name") or "").strip()
                vendor_amount = max(0, safe_int(request.form.get("vendor_amount")))
                customer_amount = max(0, safe_int(request.form.get("customer_amount")))
                file_storage = request.files.get("file")
                vendor_row = next((vendor for vendor in vendors if vendor.get("id") == vendor_id), None)
                if not vendor_row:
                    raise ValueError("流し先業者を選択してください。")
                if not file_storage or not file_storage.filename:
                    raise ValueError("登録するファイルを選択してください。")
                amount_difference = max(vendor_amount - customer_amount, 0)
                difference_rate = round((amount_difference / vendor_amount) * 100, 2) if vendor_amount else 0
                stored_path, original_filename, mime_type, file_size = save_vendor_upload(file_storage)
                conn, cur = open_cursor(False)
                ph = placeholder()
                try:
                    cur.execute(
                        f"""
                        INSERT INTO vendor_documents
                        (user_id, client_id, item_id, related_document_id, source_request_id, document_scope,
                         vendor_id, vendor_name, extracted_item_name, vendor_amount, customer_amount,
                         amount_difference, difference_rate, edited_by, edited_at, reception_number,
                         title, original_filename, stored_path, mime_type, file_size, status, notes, created_by, registered_at)
                        VALUES ({ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph})
                        """,
                        (
                            user_id,
                            user_id,
                            item_id,
                            related_document_id,
                            source_request_id,
                            "kaika" if is_kaika_scope else "user_flow",
                            vendor_id,
                            vendor_row.get("name"),
                            extracted_item_name,
                            vendor_amount,
                            customer_amount,
                            amount_difference,
                            difference_rate,
                            current_user.id,
                            get_jst_now(),
                            reception_number_for({"id": source_request_id or item_id or 1, "registered_at": get_jst_now()}),
                            title or original_filename,
                            original_filename,
                            stored_path,
                            mime_type,
                            file_size,
                            status,
                            notes,
                            current_user.id,
                            get_jst_now(),
                        ),
                    )
                    conn.commit()
                finally:
                    cur.close()
                    conn.close()
                flash("業者関連書類を登録しました。", "success")
                return redirect(url_for("admin_vendor_documents", user_id=user_id or "", vendor_id=vendor_id, scope="kaika" if is_kaika_scope else "user_flow"))
            except Exception as exc:
                flash(str(exc), "error")

        docs = fetch_vendor_documents(selected_user_id, scope="kaika" if is_kaika_scope else "user_flow")
        return render_template(
            "admin/vendor_documents.html",
            clients=clients,
            vendors=vendors,
            items=items,
            documents=docs,
            selected_user_id=selected_user_id,
            selected_vendor_id=selected_vendor_id,
            selected_scope="kaika" if is_kaika_scope else "user_flow",
            is_kaika_scope=is_kaika_scope,
            allowed_extensions=", ".join(sorted(ALLOWED_VENDOR_EXTENSIONS)),
        )

    def admin_vendor_document_download(document_id: int):
        ensure_schema()
        ph = placeholder()
        conn, cur = open_cursor()
        try:
            cur.execute(f"SELECT * FROM vendor_documents WHERE id = {ph}", (document_id,))
            doc = row_to_dict(cur.fetchone())
        finally:
            cur.close()
            conn.close()
        if not doc:
            abort(404)
        stored_path = str(doc.get("stored_path") or "").replace("\\", "/").lstrip("/")
        if stored_path.startswith("static/"):
            stored_path = stored_path[len("static/") :]
        absolute_path = os.path.abspath(os.path.join(app.static_folder, stored_path))
        static_root = os.path.abspath(app.static_folder)
        if not absolute_path.startswith(static_root) or not os.path.exists(absolute_path):
            abort(404)
        return send_file(absolute_path, as_attachment=True, download_name=doc.get("original_filename") or os.path.basename(absolute_path))

    def admin_vendor_document_delete(document_id: int):
        ensure_schema()
        ph = placeholder()
        conn, cur = open_cursor()
        doc = None
        try:
            cur.execute(f"SELECT * FROM vendor_documents WHERE id = {ph}", (document_id,))
            doc = row_to_dict(cur.fetchone())
            if not doc:
                flash("削除対象の業者関連書類が見つかりません。", "error")
                return redirect(url_for("admin_vendor_documents"))
            cur.execute(f"DELETE FROM vendor_documents WHERE id = {ph}", (document_id,))
            conn.commit()
        finally:
            cur.close()
            conn.close()

        stored_path = str(doc.get("stored_path") or "").replace("\\", "/").lstrip("/")
        if stored_path.startswith("static/"):
            stored_path = stored_path[len("static/") :]
        absolute_path = os.path.abspath(os.path.join(app.static_folder, stored_path))
        static_root = os.path.abspath(app.static_folder)
        if absolute_path.startswith(static_root) and os.path.exists(absolute_path):
            try:
                os.remove(absolute_path)
            except OSError:
                flash("書類データは削除しましたが、ファイル削除は後処理が必要です。", "warning")
                return redirect(url_for("admin_vendor_documents", user_id=doc.get("user_id") or ""))
        flash("業者関連書類を削除しました。", "success")
        return redirect(url_for("admin_vendor_documents", user_id=doc.get("user_id") or ""))

    def admin_vendors(vendor_id: int | None = None):
        ensure_schema()
        editing_vendor = None
        if vendor_id:
            ph = placeholder()
            conn, cur = open_cursor()
            try:
                cur.execute(f"SELECT * FROM vendors WHERE id = {ph}", (vendor_id,))
                editing_vendor = row_to_dict(cur.fetchone())
            finally:
                cur.close()
                conn.close()
            if not editing_vendor:
                flash("業者が見つかりません。", "error")
                return redirect(url_for("admin_vendors"))

        if request.method == "POST":
            name = (request.form.get("name") or "").strip()
            if not name:
                flash("業者名を入力してください。", "error")
                return redirect(request.url)
            payload = {
                "name": name,
                "contact_name": (request.form.get("contact_name") or "").strip(),
                "phone": (request.form.get("phone") or "").strip(),
                "email": (request.form.get("email") or "").strip(),
                "address": (request.form.get("address") or "").strip(),
                "memo": (request.form.get("memo") or "").strip(),
            }
            ph = placeholder()
            conn, cur = open_cursor(False)
            try:
                if editing_vendor:
                    cur.execute(
                        f"""
                        UPDATE vendors
                        SET name = {ph}, contact_name = {ph}, phone = {ph}, email = {ph},
                            address = {ph}, memo = {ph}, updated_at = {ph}
                        WHERE id = {ph}
                        """,
                        (
                            payload["name"],
                            payload["contact_name"],
                            payload["phone"],
                            payload["email"],
                            payload["address"],
                            payload["memo"],
                            get_jst_now(),
                            editing_vendor["id"],
                        ),
                    )
                    flash("業者情報を更新しました。", "success")
                else:
                    cur.execute(
                        f"""
                        INSERT INTO vendors
                        (name, contact_name, phone, email, address, memo, created_by, created_at, updated_at)
                        VALUES ({ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph})
                        """,
                        (
                            payload["name"],
                            payload["contact_name"],
                            payload["phone"],
                            payload["email"],
                            payload["address"],
                            payload["memo"],
                            current_user.id,
                            get_jst_now(),
                            get_jst_now(),
                        ),
                    )
                    flash("業者を登録しました。", "success")
                conn.commit()
            finally:
                cur.close()
                conn.close()
            return redirect(url_for("admin_vendors"))

        return render_template("admin/vendors.html", vendors=load_vendors(), editing_vendor=editing_vendor)

    def admin_vendor_delete(vendor_id: int):
        ensure_schema()
        ph = placeholder()
        conn, cur = open_cursor()
        try:
            cur.execute(f"SELECT * FROM vendors WHERE id = {ph}", (vendor_id,))
            vendor = row_to_dict(cur.fetchone())
            if not vendor:
                flash("業者が見つかりません。", "error")
                return redirect(url_for("admin_vendors"))
            cur.execute(f"SELECT COUNT(*) AS count FROM vendor_documents WHERE vendor_id = {ph}", (vendor_id,))
            usage = row_to_dict(cur.fetchone())
            if safe_int(usage.get("count")) > 0:
                flash("業者関連書類に紐づいているため削除できません。", "error")
                return redirect(url_for("admin_vendors"))
            cur.execute(f"DELETE FROM vendors WHERE id = {ph}", (vendor_id,))
            conn.commit()
            flash("業者を削除しました。", "success")
        finally:
            cur.close()
            conn.close()
        return redirect(url_for("admin_vendors"))

    def admin_monthly_fee_settings():
        ensure_schema()
        clients = load_clients()
        settings_map = load_monthly_settings_map([client["id"] for client in clients])
        if request.method == "POST":
            user_id = request.form.get("user_id", type=int)
            amount = max(0, safe_int(request.form.get("monthly_fee_amount")))
            free_days = max(0, safe_int(request.form.get("free_period_days")))
            closing_day = request.form.get("closing_day")
            closing_day_value = safe_int(closing_day) if closing_day else None
            if closing_day_value is not None:
                closing_day_value = min(31, max(1, closing_day_value))
            if not user_id:
                flash("対象クライアントを選択してください。", "error")
                return redirect(url_for("admin_monthly_fee_settings"))
            ph = placeholder()
            conn, cur = open_cursor(False)
            try:
                if DATABASE_URL:
                    cur.execute(
                        """
                        INSERT INTO client_monthly_fee_settings
                        (user_id, monthly_fee_enabled, monthly_fee_amount, free_campaign_enabled,
                         free_period_days, closing_day, updated_by, updated_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
                        ON CONFLICT (user_id) DO UPDATE SET
                            monthly_fee_enabled = EXCLUDED.monthly_fee_enabled,
                            monthly_fee_amount = EXCLUDED.monthly_fee_amount,
                            free_campaign_enabled = EXCLUDED.free_campaign_enabled,
                            free_period_days = EXCLUDED.free_period_days,
                            closing_day = EXCLUDED.closing_day,
                            updated_by = EXCLUDED.updated_by,
                            updated_at = CURRENT_TIMESTAMP
                        """,
                        (
                            user_id,
                            bool_from_form("monthly_fee_enabled"),
                            amount,
                            bool_from_form("free_campaign_enabled"),
                            free_days,
                            closing_day_value,
                            current_user.id,
                        ),
                    )
                else:
                    cur.execute(
                        """
                        INSERT INTO client_monthly_fee_settings
                        (user_id, monthly_fee_enabled, monthly_fee_amount, free_campaign_enabled,
                         free_period_days, closing_day, updated_by, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                        ON CONFLICT(user_id) DO UPDATE SET
                            monthly_fee_enabled = excluded.monthly_fee_enabled,
                            monthly_fee_amount = excluded.monthly_fee_amount,
                            free_campaign_enabled = excluded.free_campaign_enabled,
                            free_period_days = excluded.free_period_days,
                            closing_day = excluded.closing_day,
                            updated_by = excluded.updated_by,
                            updated_at = CURRENT_TIMESTAMP
                        """,
                        (
                            user_id,
                            1 if bool_from_form("monthly_fee_enabled") else 0,
                            amount,
                            1 if bool_from_form("free_campaign_enabled") else 0,
                            free_days,
                            closing_day_value,
                            current_user.id,
                        ),
                    )
                conn.commit()
                flash("月額利用料設定を保存しました。", "success")
            finally:
                cur.close()
                conn.close()
            return redirect(url_for("admin_monthly_fee_settings", user_id=user_id))

        for client in clients:
            client["monthly_setting"] = normalized_setting_for_user(client["id"], settings_map.get(client["id"]))
        return render_template("admin/monthly_fee_settings.html", clients=clients)

    def admin_monthly_settlements():
        ensure_schema()
        selected_month, first_day, last_day = month_bounds(request.args.get("month"))
        clients = load_clients()
        settings_map = load_monthly_settings_map([client["id"] for client in clients])
        for client in clients:
            setting = normalized_setting_for_user(client["id"], settings_map.get(client["id"]))
            client["monthly_setting"] = setting
            client["effective_monthly_fee"] = effective_monthly_fee(client, setting, selected_month)
            client["settlement_url"] = url_for("admin_monthly_settlement_create", user_id=client["id"], month=selected_month)
        return render_template(
            "admin/monthly_settlements.html",
            clients=clients,
            selected_month=selected_month,
            first_day=first_day,
            last_day=last_day,
        )

    def load_client(user_id: int) -> dict[str, Any] | None:
        ph = placeholder()
        conn, cur = open_cursor()
        try:
            cur.execute(f"SELECT id, username, display_name, email, created_at FROM users WHERE id = {ph}", (user_id,))
            client = row_to_dict(cur.fetchone())
            if client:
                client["name"] = display_user_name(client)
            return client
        finally:
            cur.close()
            conn.close()

    def generate_monthly_document_no(month_text: str) -> str:
        prefix = f"MS-{month_text.replace('-', '')}"
        ph = placeholder()
        count = safe_int(fetch_scalar(f"SELECT COUNT(*) FROM shikiriosho WHERE document_no LIKE {ph}", (prefix + "%",)))
        return f"{prefix}-{count + 1:03d}"

    def admin_monthly_settlement_create():
        ensure_schema()
        selected_month, first_day, last_day = month_bounds(request.values.get("month"))
        user_id = request.values.get("user_id", type=int)
        client = load_client(user_id) if user_id else None
        if not client:
            flash("対象クライアントが見つかりません。", "error")
            return redirect(url_for("admin_monthly_settlements", month=selected_month))
        settings_map = load_monthly_settings_map([client["id"]])
        setting = normalized_setting_for_user(client["id"], settings_map.get(client["id"]))
        monthly_fee = effective_monthly_fee(client, setting, selected_month)

        if request.method == "POST":
            photo_packing_fee = max(0, safe_int(request.form.get("photo_packing_fee")))
            shipping_support_fee = max(0, safe_int(request.form.get("shipping_support_fee")))
            other_fee = max(0, safe_int(request.form.get("other_fee")))
            notes = (request.form.get("notes") or "").strip()
            status = request.form.get("status") or "completed"
            line_items = [
                (f"月額利用料 {selected_month}", monthly_fee),
                ("撮影・梱包費", photo_packing_fee),
                ("発送サポート費", shipping_support_fee),
                ("その他費用", other_fee),
            ]
            total_amount = sum(amount for _label, amount in line_items)
            if total_amount == 0:
                line_items = [("月次精算", 0)]
            document_no = generate_monthly_document_no(selected_month)
            tax_rate = 10.0
            tax_amount = int(total_amount * tax_rate / (100 + tax_rate)) if total_amount else 0
            manual_total = photo_packing_fee + shipping_support_fee + other_fee
            ph = placeholder()
            conn, cur = open_cursor(False)
            try:
                if DATABASE_URL:
                    cur.execute(
                        """
                        INSERT INTO shikiriosho
                        (document_no, sender_id, recipient_id, recipient_name, issue_date, due_date,
                         subtotal, tax_amount, total_amount, tax_rate, notes, status,
                         settlement_month, source_type, monthly_fee_amount, manual_expense_total)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        RETURNING id
                        """,
                        (
                            document_no,
                            current_user.id,
                            client["id"],
                            client["name"],
                            last_day,
                            None,
                            total_amount,
                            tax_amount,
                            total_amount,
                            tax_rate,
                            notes,
                            status,
                            selected_month,
                            "monthly_settlement",
                            monthly_fee,
                            manual_total,
                        ),
                    )
                    inserted = cur.fetchone()
                    shikiriosho_id = inserted["id"] if isinstance(inserted, dict) else inserted[0]
                else:
                    cur.execute(
                        """
                        INSERT INTO shikiriosho
                        (document_no, sender_id, recipient_id, recipient_name, issue_date, due_date,
                         subtotal, tax_amount, total_amount, tax_rate, notes, status,
                         settlement_month, source_type, monthly_fee_amount, manual_expense_total)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            document_no,
                            current_user.id,
                            client["id"],
                            client["name"],
                            last_day.strftime("%Y-%m-%d"),
                            None,
                            total_amount,
                            tax_amount,
                            total_amount,
                            tax_rate,
                            notes,
                            status,
                            selected_month,
                            "monthly_settlement",
                            monthly_fee,
                            manual_total,
                        ),
                    )
                    shikiriosho_id = cur.lastrowid
                for index, (label, amount) in enumerate(line_items, 1):
                    if amount == 0 and len(line_items) > 1:
                        continue
                    cur.execute(
                        f"""
                        INSERT INTO shikiriosho_items
                        (shikiriosho_id, item_no, product_name, product_date, product_code, quantity, unit_price, amount)
                        VALUES ({ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph})
                        """,
                        (shikiriosho_id, index, label, last_day, "", 1, amount, amount),
                    )
                conn.commit()
                flash("月次精算書を作成しました。送付待機一覧から確認・編集・送付できます。", "success")
                return redirect(url_for("admin_shikiriosho_list"))
            finally:
                cur.close()
                conn.close()

        return render_template(
            "admin/monthly_settlement_form.html",
            client=client,
            setting=setting,
            selected_month=selected_month,
            first_day=first_day,
            last_day=last_day,
            monthly_fee=monthly_fee,
        )

    def load_sales_agency_items_by_request(request_ids: list[int]) -> dict[int, list[dict[str, Any]]]:
        cleaned_ids = []
        for raw_id in request_ids:
            try:
                request_id = int(raw_id)
            except (TypeError, ValueError):
                continue
            if request_id > 0 and request_id not in cleaned_ids:
                cleaned_ids.append(request_id)
        if not cleaned_ids:
            return {}
        ph = placeholder()
        marks = ",".join([ph] * len(cleaned_ids))
        conn, cur = open_cursor()
        try:
            if not table_exists(cur, "sales_agency_request_items"):
                return {}
            cur.execute(
                f"""
                SELECT
                    sari.request_id,
                    COALESCE(m.id, sari.merchandise_id) AS id,
                    sari.merchandise_id AS merchandise_id,
                    COALESCE(m.product_name, '') AS product_name,
                    COALESCE(m.brand_name, '') AS brand_name,
                    COALESCE(m.photo_path, '') AS photo_path,
                    COALESCE(m.kaika_product_code, '') AS kaika_product_code
                FROM sales_agency_request_items sari
                LEFT JOIN merchandise m ON sari.merchandise_id = m.id
                WHERE sari.request_id IN ({marks})
                ORDER BY sari.request_id, COALESCE(sari.merchandise_id, m.id) DESC
                """,
                tuple(cleaned_ids),
            )
            item_map: dict[int, list[dict[str, Any]]] = {}
            for item in rows_to_dicts(cur.fetchall()):
                request_id = safe_int(item.get("request_id"))
                if not request_id:
                    continue
                if not item.get("product_name"):
                    item["product_name"] = f"商品ID {item.get('merchandise_id') or item.get('id') or '-'}"
                item_map.setdefault(request_id, []).append(item)
            return item_map
        finally:
            cur.close()
            conn.close()

    def admin_documents_dashboard_clean():
        ensure_schema()
        selected_group = (request.args.get("group") or "all").strip()
        allowed_groups = {"all", "client_incoming", "vendor_outgoing", "vendor_incoming", "client_outgoing"}
        if selected_group not in allowed_groups:
            selected_group = "all"

        stage_cards = [
            {
                "key": "client_incoming",
                "step": 1,
                "title": "顧客からの書類受付",
                "description": "見積依頼書や買取承諾書など、顧客側から届いた書類を確認します。",
                "url": url_for("admin_documents_dashboard", group="client_incoming"),
            },
            {
                "key": "vendor_outgoing",
                "step": 2,
                "title": "業者へ依頼する見積依頼書作成",
                "description": "対象商品を確認し、業者向け見積依頼書を作成します。",
                "url": url_for("admin_documents_dashboard", group="vendor_outgoing"),
            },
            {
                "key": "vendor_incoming",
                "step": 3,
                "title": "業者関連書類登録",
                "description": "業者から届いたPDF・画像書類を登録、確認、ダウンロードします。",
                "url": url_for("admin_documents_dashboard", group="vendor_incoming"),
            },
            {
                "key": "client_outgoing",
                "step": 4,
                "title": "顧客へ送付する買取明細書作成",
                "description": "業者回答をもとに顧客向け買取明細書を作成します。",
                "url": url_for("admin_documents_dashboard", group="client_outgoing"),
            },
        ]

        request_rows: list[dict[str, Any]] = []
        vendor_documents = []
        client_outgoing_rows = []
        if selected_group != "all":
            if selected_group == "vendor_incoming":
                vendor_documents = fetch_vendor_documents(scope="user_flow")
            elif selected_group == "client_outgoing":
                client_outgoing_rows = build_history_rows("kaitori", admin=True, limit=120)
            else:
                try:
                    fetcher = getattr(module, "_fetch_admin_documents_request_summaries", None)
                    if callable(fetcher):
                        request_rows = fetcher(limit=120)
                    else:
                        request_rows = []
                except Exception:
                    request_rows = []
                request_item_map = load_sales_agency_items_by_request([row.get("id") for row in request_rows])
                for row in request_rows:
                    row_id = safe_int(row.get("id"))
                    row["client_name"] = row.get("client_name") or row.get("user_name") or row.get("username") or f"ID:{row.get('user_id') or '-'}"
                    row["merchandise_items"] = request_item_map.get(row_id, row.get("merchandise_items") or [])
                    row["reception_number"] = row.get("reception_number") or reception_number_for(row)
                    row["client_number"] = row.get("client_number") or row.get("user_id") or row.get("client_id") or "-"
                    row["created_date_label"] = (
                        row.get("created_date_label")
                        or format_datetime(row.get("created_at"))
                        or format_datetime(row.get("submitted_at"))
                        or format_datetime(row.get("updated_at"))
                    )
                    if row.get("id") and not row.get("detail_url"):
                        row["detail_url"] = url_for("admin_sales_agency_request_detail", id=row.get("id"))
                    if not row.get("item_detail_url"):
                        item_candidates = row.get("merchandise_items") or row.get("items") or []
                        if isinstance(item_candidates, (list, tuple)) and item_candidates and isinstance(item_candidates[0], dict):
                            first_item = item_candidates[0]
                        else:
                            first_item = {}
                        item_id = first_item.get("id") or first_item.get("merchandise_id") or row.get("merchandise_id") or row.get("item_id")
                        if item_id:
                            try:
                                row["item_detail_url"] = url_for("view_item", id=item_id)
                            except Exception:
                                row["item_detail_url"] = None
                        if first_item:
                            item_names = [
                                clean_text(item.get("product_name"), "")
                                or clean_text(item.get("item_name"), "")
                                or f"商品ID {item.get('id') or item.get('merchandise_id')}"
                                for item in item_candidates
                                if isinstance(item, dict)
                            ]
                            if item_names:
                                row["item_summary"] = " / ".join(item_names[:3])
                                if len(item_names) > 3:
                                    row["item_summary"] += f" ほか{len(item_names) - 3}点"
                            image_path = clean_text(first_item.get("photo_path"), "")
                            if image_path and image_path != "-":
                                try:
                                    row["item_image_url"] = url_for("static", filename=image_path)
                                except Exception:
                                    row["item_image_url"] = None
                    row["status_label"] = row.get("status_label") or getattr(module, "get_sales_agency_status_label", lambda status, **_kwargs: status)(row.get("status"), viewer="admin")
        kaika_document_cards = [
            {
                "step": 1,
                "title": "開花商品用 業者向け見積依頼書作成",
                "description": "開花商品一覧の商品だけを選び、業者へ送る見積依頼書を作成します。",
                "url": url_for("admin_mitsumori_add", scope="kaika_vendor"),
            },
            {
                "step": 2,
                "title": "開花商品用 業者関連書類登録",
                "description": "開花商品だけに紐づけて、業者回答のPDF・画像と金額を登録します。",
                "url": url_for("admin_vendor_documents", scope="kaika"),
            },
            {
                "step": 3,
                "title": "開花用 見積依頼書作成",
                "description": "個人顧客情報を手入力し、開花用の見積依頼書を作成します。",
                "url": url_for("admin_mitsumori_add", scope="kaika_estimate"),
            },
            {
                "step": 4,
                "title": "開花買取承諾書作成",
                "description": "開花側で扱う買取承諾書を作成し、PDFで確認します。",
                "url": url_for("admin_kaitori_shoudaku_add", scope="kaika"),
            },
        ]
        return render_template(
            "admin/documents_dashboard_clean.html",
            selected_group=selected_group,
            stage_cards=stage_cards,
            kaika_document_cards=kaika_document_cards,
            request_rows=request_rows,
            vendor_documents=vendor_documents,
            client_outgoing_rows=client_outgoing_rows,
        )

    def admin_documents_history_clean():
        ensure_schema()
        selected_category = (request.args.get("category") or request.args.get("doc_type") or "").strip()
        category_keys = {key for key, _title, _desc in ADMIN_HISTORY_CATEGORIES}
        if selected_category in {"user_mitsumori", "mitsumori"}:
            selected_category = "client_incoming"
        elif selected_category in {"mitsumori_houjin", "admin_mitsumori"}:
            selected_category = "vendor_estimate"
        elif selected_category in {"invoice"}:
            selected_category = "kaitori"
        elif selected_category in {"user_kaitori_shoudaku", "shoudaku"}:
            selected_category = "user_shoudaku"
        elif selected_category in {"admin_kaitori_shoudaku"}:
            selected_category = "kaika_shoudaku"
        elif selected_category not in category_keys:
            selected_category = ""
        history_rows = build_history_rows(selected_category, admin=True) if selected_category else []
        selected_meta = next(({"key": k, "title": t, "description": d} for k, t, d in ADMIN_HISTORY_CATEGORIES if k == selected_category), None)
        return render_template(
            "admin/documents_history_clean.html",
            category_cards=category_cards("admin_documents_history", selected_category, admin=True),
            selected_category=selected_category,
            selected_meta=selected_meta,
            history_rows=history_rows,
        )

    def documents_clean():
        ensure_schema()
        selected_category = (request.args.get("category") or request.args.get("tab") or "").strip()
        tab_map = {
            "kaitori": "kaitori",
            "mitsumori": "client_incoming",
            "shoudaku": "user_shoudaku",
            "shikiri": "shikiriosho",
            "keisan": "keisan",
        }
        selected_category = tab_map.get(selected_category, selected_category)
        category_keys = {key for key, _title, _desc in USER_HISTORY_CATEGORIES}
        if selected_category not in category_keys:
            selected_category = ""
        history_rows = build_history_rows(selected_category, admin=False, user_id=current_user.id) if selected_category else []
        selected_meta = next(({"key": k, "title": t, "description": d} for k, t, d in USER_HISTORY_CATEGORIES if k == selected_category), None)
        return render_template(
            "documents_clean.html",
            category_cards=category_cards("documents", selected_category, admin=False),
            selected_category=selected_category,
            selected_meta=selected_meta,
            history_rows=history_rows,
        )

    def register(endpoint: str, rule: str, view_func, methods: list[str], *, admin: bool = True) -> None:
        wrapped = login_required(admin_required(view_func)) if admin else login_required(view_func)
        if endpoint in app.view_functions:
            app.view_functions[endpoint] = wrapped
            return
        app.add_url_rule(rule, endpoint=endpoint, view_func=wrapped, methods=methods)

    ensure_schema()
    register("admin_vendor_documents", "/admin/vendor-documents", admin_vendor_documents, ["GET", "POST"], admin=True)
    register("admin_vendor_document_download", "/admin/vendor-documents/<int:document_id>/download", admin_vendor_document_download, ["GET"], admin=True)
    register("admin_vendor_document_delete", "/admin/vendor-documents/<int:document_id>/delete", admin_vendor_document_delete, ["POST"], admin=True)
    register("admin_vendors", "/admin/vendors", admin_vendors, ["GET", "POST"], admin=True)
    register("admin_vendor_edit", "/admin/vendors/<int:vendor_id>/edit", admin_vendors, ["GET", "POST"], admin=True)
    register("admin_vendor_delete", "/admin/vendors/<int:vendor_id>/delete", admin_vendor_delete, ["POST"], admin=True)
    register("admin_monthly_fee_settings", "/admin/monthly-fee-settings", admin_monthly_fee_settings, ["GET", "POST"], admin=True)
    register("admin_monthly_settlements", "/admin/monthly-settlements", admin_monthly_settlements, ["GET"], admin=True)
    register("admin_monthly_settlement_create", "/admin/monthly-settlements/create", admin_monthly_settlement_create, ["GET", "POST"], admin=True)
    register("admin_documents_dashboard", "/admin/documents", admin_documents_dashboard_clean, ["GET"], admin=True)
    register("admin_documents_history", "/admin/documents/history", admin_documents_history_clean, ["GET"], admin=True)
    register("documents", "/documents", documents_clean, ["GET"], admin=False)

    final_admin_mitsumori_add = getattr(module, "admin_mitsumori_add_from_documents", None)
    if callable(final_admin_mitsumori_add):
        app.view_functions["admin_mitsumori_add"] = final_admin_mitsumori_add
        module.admin_mitsumori_add = final_admin_mitsumori_add

    module.admin_vendor_documents = admin_vendor_documents
    module.admin_vendors = admin_vendors
    module.admin_monthly_fee_settings = admin_monthly_fee_settings
    module.admin_monthly_settlements = admin_monthly_settlements
    module.admin_documents_dashboard = admin_documents_dashboard_clean
    module.admin_documents_history = admin_documents_history_clean
    module.documents = documents_clean
