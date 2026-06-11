# -*- coding: utf-8 -*-
from __future__ import annotations

import calendar
import mimetypes
import os
import sqlite3
from datetime import date, datetime, timedelta
from typing import Any, Iterable

from flask import abort, flash, redirect, render_template, request, send_file, url_for
from werkzeug.utils import secure_filename


ALLOWED_VENDOR_EXTENSIONS = {"pdf", "png", "jpg", "jpeg"}
HISTORY_CATEGORIES = [
    ("mitsumori", "見積依頼書", "見積依頼書の作成・送付履歴"),
    ("kaitori", "買取明細書", "買取明細書の作成・送付履歴"),
    ("shoudaku", "買取承諾書", "買取承諾書の作成・送付履歴"),
    ("shikiriosho", "精算書", "精算書の作成・送付履歴"),
    ("keisan", "代行仕入れ計算書", "代行仕入れサービス側の計算書履歴"),
    ("vendor", "業者関連書類", "業者から届いた書類の管理履歴"),
    ("other", "その他書類", "その他の書類・サービス書類"),
]
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
                cur.execute("CREATE INDEX IF NOT EXISTS idx_client_monthly_fee_settings_user ON client_monthly_fee_settings (user_id)")

            vendor_columns = {
                "client_id": ("INTEGER REFERENCES users(id)", "INTEGER"),
                "related_document_id": ("INTEGER", "INTEGER"),
                "source_request_id": ("INTEGER", "INTEGER"),
                "mime_type": ("VARCHAR(120)", "TEXT"),
                "file_size": ("INTEGER DEFAULT 0", "INTEGER DEFAULT 0"),
                "created_by": ("INTEGER REFERENCES users(id)", "INTEGER"),
            }
            for name, (pg_def, sqlite_def) in vendor_columns.items():
                add_column_if_missing(cur, "vendor_documents", name, pg_def, sqlite_def)

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

    def status_label(status: Any) -> str:
        return STATUS_LABELS.get(str(status or "").strip(), str(status or "-"))

    def category_cards(endpoint: str, selected: str | None = None, admin: bool = False) -> list[dict[str, Any]]:
        cards = []
        for idx, (key, title, description) in enumerate(HISTORY_CATEGORIES, 1):
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

    def load_item_options(user_id: int | None = None, limit: int = 300) -> list[dict[str, Any]]:
        conn, cur = open_cursor()
        try:
            ph = placeholder()
            params: tuple[Any, ...] = ()
            where_sql = ""
            if user_id:
                where_sql = f"WHERE user_id = {ph}"
                params = (user_id,)
            cur.execute(
                f"""
                SELECT id, product_name, brand_name, user_id, kaika_product_code
                FROM merchandise
                {where_sql}
                ORDER BY created_at DESC, id DESC
                LIMIT {int(limit)}
                """,
                params,
            )
            items = rows_to_dicts(cur.fetchall())
            for item in items:
                name = clean_text(item.get("product_name"), "")
                code = clean_text(item.get("kaika_product_code"), "")
                item["label"] = f"{name or '商品名未登録'}{(' / ' + code) if code else ''}"
            return items
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
        _month_text, _first, last = month_bounds(target_month)
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
        return 0 if free_until > last else amount

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

    def fetch_vendor_documents(user_id: int | None = None, limit: int = 200) -> list[dict[str, Any]]:
        ensure_schema()
        conn, cur = open_cursor()
        try:
            ph = placeholder()
            params: tuple[Any, ...] = ()
            where = ""
            if user_id:
                where = f"WHERE vd.user_id = {ph}"
                params = (user_id,)
            cur.execute(
                f"""
                SELECT vd.*,
                       u.display_name AS client_display_name,
                       u.username AS client_username,
                       m.product_name AS item_product_name,
                       m.kaika_product_code AS item_code
                FROM vendor_documents vd
                LEFT JOIN users u ON vd.user_id = u.id
                LEFT JOIN merchandise m ON vd.item_id = m.id
                {where}
                ORDER BY vd.registered_at DESC, vd.id DESC
                LIMIT {int(limit)}
                """,
                params,
            )
            docs = rows_to_dicts(cur.fetchall())
            for doc in docs:
                doc["client_name"] = clean_text(doc.get("client_display_name"), "") or clean_text(doc.get("client_username"), "")
                doc["item_name"] = clean_text(doc.get("item_product_name"), "未指定")
                doc["registered_label"] = format_datetime(doc.get("registered_at"))
                doc["status_label"] = status_label(doc.get("status"))
                doc["download_url"] = url_for("admin_vendor_document_download", document_id=doc["id"])
                doc["delete_url"] = url_for("admin_vendor_document_delete", document_id=doc["id"])
            return docs
        finally:
            cur.close()
            conn.close()

    def count_history_rows(category: str, admin: bool = False) -> int:
        ensure_schema()
        try:
            rows = build_history_rows(category, admin=admin, limit=5000)
            return len(rows)
        except Exception:
            return 0

    def build_history_rows(category: str, admin: bool = False, user_id: int | None = None, limit: int = 200) -> list[dict[str, Any]]:
        ensure_schema()
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

            if category == "mitsumori":
                where = "" if admin else f"WHERE user_id = {ph}"
                params = () if admin else (params_user,)
                cur.execute(f"SELECT * FROM user_mitsumori {where} ORDER BY created_at DESC LIMIT {int(limit)}", params)
                for doc in rows_to_dicts(cur.fetchall()):
                    add_row(
                        document_type="見積依頼書",
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
            elif category == "shoudaku":
                where = "" if admin else f"WHERE user_id = {ph}"
                params = () if admin else (params_user,)
                cur.execute(f"SELECT * FROM user_kaitori_shoudaku {where} ORDER BY created_at DESC LIMIT {int(limit)}", params)
                for doc in rows_to_dicts(cur.fetchall()):
                    add_row(
                        document_type="買取承諾書",
                        document_no=doc.get("document_no") or f"ID:{doc.get('id')}",
                        client_name=doc.get("customer_name") or doc.get("user_name") or (current_user.display_name if not admin else ""),
                        issue_date=doc.get("issue_date") or doc.get("created_at"),
                        total_amount=doc.get("total_amount") or 0,
                        status=doc.get("status"),
                        detail_url=url_for("admin_user_kaitori_shoudaku_view", id=doc["id"]) if admin and "admin_user_kaitori_shoudaku_view" in app.view_functions else None,
                        source="user_kaitori_shoudaku",
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
                cur.execute(f"SELECT * FROM user_keisan {where} ORDER BY created_at DESC LIMIT {int(limit)}", params)
                for doc in rows_to_dicts(cur.fetchall()):
                    add_row(
                        document_type="代行仕入れ計算書",
                        document_no=doc.get("document_no") or f"ID:{doc.get('id')}",
                        client_name=doc.get("customer_name") or doc.get("user_name") or (current_user.display_name if not admin else ""),
                        issue_date=doc.get("issue_date") or doc.get("created_at"),
                        total_amount=doc.get("total_amount") or 0,
                        status=doc.get("status"),
                        detail_url=url_for("admin_auction_keisan_view", id=doc["id"]) if admin and "admin_auction_keisan_view" in app.view_functions else None,
                        source="user_keisan",
                    )
            elif category == "vendor":
                where = "" if admin else f"WHERE vd.user_id = {ph} AND COALESCE(vd.status, '') IN ('shared', 'sent')"
                params = () if admin else (params_user,)
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
                    params,
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
        clients = load_clients()
        selected_user_id = request.values.get("user_id", type=int)
        items = load_item_options(selected_user_id)
        if request.method == "POST":
            try:
                user_id = request.form.get("user_id", type=int)
                item_id = request.form.get("item_id", type=int)
                related_document_id = request.form.get("related_document_id", type=int)
                source_request_id = request.form.get("source_request_id", type=int)
                title = (request.form.get("title") or "").strip()
                status = (request.form.get("status") or "received").strip()
                notes = (request.form.get("notes") or "").strip()
                file_storage = request.files.get("file")
                if not user_id:
                    raise ValueError("対象クライアントを選択してください。")
                if not file_storage or not file_storage.filename:
                    raise ValueError("登録するファイルを選択してください。")
                stored_path, original_filename, mime_type, file_size = save_vendor_upload(file_storage)
                conn, cur = open_cursor(False)
                ph = placeholder()
                try:
                    cur.execute(
                        f"""
                        INSERT INTO vendor_documents
                        (user_id, client_id, item_id, related_document_id, source_request_id, title,
                         original_filename, stored_path, mime_type, file_size, status, notes, created_by, registered_at)
                        VALUES ({ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph})
                        """,
                        (
                            user_id,
                            user_id,
                            item_id,
                            related_document_id,
                            source_request_id,
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
                return redirect(url_for("admin_vendor_documents", user_id=user_id))
            except Exception as exc:
                flash(str(exc), "error")

        docs = fetch_vendor_documents(selected_user_id)
        return render_template(
            "admin/vendor_documents.html",
            clients=clients,
            items=items,
            documents=docs,
            selected_user_id=selected_user_id,
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
                "title": "業者へ依頼する書類",
                "description": "対象商品を確認し、業者へ渡す見積依頼書などを作成します。",
                "url": url_for("admin_documents_dashboard", group="vendor_outgoing"),
            },
            {
                "key": "vendor_incoming",
                "step": 3,
                "title": "業者関連書類",
                "description": "業者から届いたPDF・画像書類を登録、確認、ダウンロードします。",
                "url": url_for("admin_documents_dashboard", group="vendor_incoming"),
            },
            {
                "key": "client_outgoing",
                "step": 4,
                "title": "顧客へ送付する書類",
                "description": "買取明細書、精算書、送付待機中の書類を確認します。",
                "url": url_for("admin_documents_dashboard", group="client_outgoing"),
            },
        ]
        quick_links = [
            {"title": "業者関連書類を登録", "url": url_for("admin_vendor_documents"), "description": "PDF、png、jpg、jpegを登録"},
            {"title": "月額利用料設定", "url": url_for("admin_monthly_fee_settings"), "description": "顧客別の月額・無料期間を設定"},
            {"title": "精算書送付待機", "url": url_for("admin_monthly_settlements"), "description": "月末締めの精算書を作成"},
            {"title": "代行仕入れ計算書", "url": url_for("admin_auction_keisan_list") if "admin_auction_keisan_list" in app.view_functions else url_for("admin_documents_history", category="keisan"), "description": "代行仕入れサービス側の計算書を確認"},
        ]

        request_rows: list[dict[str, Any]] = []
        vendor_documents = []
        shikiriosho_waiting = []
        if selected_group != "all":
            if selected_group == "vendor_incoming":
                vendor_documents = fetch_vendor_documents()
            elif selected_group == "client_outgoing":
                shikiriosho_waiting = build_history_rows("shikiriosho", admin=True, limit=120)
                request_rows = build_history_rows("kaitori", admin=True, limit=80)
            else:
                try:
                    fetcher = getattr(module, "_fetch_admin_documents_request_summaries", None)
                    if callable(fetcher):
                        request_rows = fetcher(limit=120)
                    else:
                        request_rows = []
                except Exception:
                    request_rows = []
        return render_template(
            "admin/documents_dashboard_clean.html",
            selected_group=selected_group,
            stage_cards=stage_cards,
            quick_links=quick_links,
            request_rows=request_rows,
            vendor_documents=vendor_documents,
            shikiriosho_waiting=shikiriosho_waiting,
        )

    def admin_documents_history_clean():
        ensure_schema()
        selected_category = (request.args.get("category") or request.args.get("doc_type") or "").strip()
        category_keys = {key for key, _title, _desc in HISTORY_CATEGORIES}
        if selected_category in {"user_mitsumori", "mitsumori_houjin"}:
            selected_category = "mitsumori"
        elif selected_category in {"invoice"}:
            selected_category = "kaitori"
        elif selected_category in {"user_kaitori_shoudaku"}:
            selected_category = "shoudaku"
        elif selected_category not in category_keys:
            selected_category = ""
        history_rows = build_history_rows(selected_category, admin=True) if selected_category else []
        selected_meta = next(({"key": k, "title": t, "description": d} for k, t, d in HISTORY_CATEGORIES if k == selected_category), None)
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
        tab_map = {"kaitori": "kaitori", "mitsumori": "mitsumori", "shoudaku": "shoudaku", "shikiri": "shikiriosho", "keisan": "keisan"}
        selected_category = tab_map.get(selected_category, selected_category)
        category_keys = {key for key, _title, _desc in HISTORY_CATEGORIES}
        if selected_category not in category_keys:
            selected_category = ""
        history_rows = build_history_rows(selected_category, admin=False, user_id=current_user.id) if selected_category else []
        selected_meta = next(({"key": k, "title": t, "description": d} for k, t, d in HISTORY_CATEGORIES if k == selected_category), None)
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
    register("admin_monthly_fee_settings", "/admin/monthly-fee-settings", admin_monthly_fee_settings, ["GET", "POST"], admin=True)
    register("admin_monthly_settlements", "/admin/monthly-settlements", admin_monthly_settlements, ["GET"], admin=True)
    register("admin_monthly_settlement_create", "/admin/monthly-settlements/create", admin_monthly_settlement_create, ["GET", "POST"], admin=True)
    register("admin_documents_dashboard", "/admin/documents", admin_documents_dashboard_clean, ["GET"], admin=True)
    register("admin_documents_history", "/admin/documents/history", admin_documents_history_clean, ["GET"], admin=True)
    register("documents", "/documents", documents_clean, ["GET"], admin=False)

    module.admin_vendor_documents = admin_vendor_documents
    module.admin_monthly_fee_settings = admin_monthly_fee_settings
    module.admin_monthly_settlements = admin_monthly_settlements
    module.admin_documents_dashboard = admin_documents_dashboard_clean
    module.admin_documents_history = admin_documents_history_clean
    module.documents = documents_clean
