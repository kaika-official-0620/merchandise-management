# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import re
import sqlite3
from functools import wraps
from typing import Any, Iterable

from flask import abort, flash, redirect, render_template, request, url_for


REPLACED_STATUS = "replaced"
REBUILD_PERMISSION = "can_rebuild_documents"
LEGACY_REBUILD_PERMISSIONS = {
    REBUILD_PERMISSION,
    "can_edit_final_documents",
    "can_cancel_documents",
}


def apply(module: Any) -> None:
    if getattr(module, "_stepd_document_rebuild_patch_applied", False):
        return
    module._stepd_document_rebuild_patch_applied = True

    app = module.app
    get_db = module.get_db
    DATABASE_URL = getattr(module, "DATABASE_URL", None)
    RealDictCursor = getattr(module, "RealDictCursor", None)
    current_user = module.current_user
    get_jst_now = getattr(module, "get_jst_now")

    def mark() -> str:
        return "%s" if DATABASE_URL else "?"

    def open_cursor(dict_rows: bool = True):
        conn = get_db()
        if DATABASE_URL and dict_rows and RealDictCursor is not None:
            return conn, conn.cursor(cursor_factory=RealDictCursor)
        if not DATABASE_URL:
            conn.row_factory = sqlite3.Row
        return conn, conn.cursor()

    def row_to_dict(row):
        if row is None:
            return None
        return dict(row)

    def rows_to_dicts(rows: Iterable[Any]) -> list[dict[str, Any]]:
        return [row_to_dict(row) for row in rows if row is not None]

    def table_exists(cur, table_name: str) -> bool:
        if DATABASE_URL:
            cur.execute(
                """
                SELECT 1
                FROM information_schema.tables
                WHERE table_schema = current_schema()
                  AND table_name = %s
                LIMIT 1
                """,
                (table_name,),
            )
            return cur.fetchone() is not None
        cur.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table_name,))
        return cur.fetchone() is not None

    def column_exists(cur, table_name: str, column_name: str) -> bool:
        if DATABASE_URL:
            cur.execute(
                """
                SELECT 1
                FROM information_schema.columns
                WHERE table_schema = current_schema()
                  AND table_name = %s
                  AND column_name = %s
                LIMIT 1
                """,
                (table_name, column_name),
            )
            return cur.fetchone() is not None
        cur.execute(f"PRAGMA table_info({table_name})")
        return any((row["name"] if hasattr(row, "keys") else row[1]) == column_name for row in cur.fetchall())

    def add_column_if_missing(cur, table_name: str, column_name: str, pg_type: str, sqlite_type: str) -> None:
        if table_exists(cur, table_name) and not column_exists(cur, table_name, column_name):
            cur.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {pg_type if DATABASE_URL else sqlite_type}")

    def ensure_schema() -> None:
        conn, cur = open_cursor()
        try:
            common_doc_columns = [
                ("rebuild_reason", "TEXT", "TEXT"),
                ("rebuild_requested_at", "TIMESTAMP", "TEXT"),
                ("rebuild_requested_by", "INTEGER", "INTEGER"),
            ]
            for table_name in ("user_mitsumori", "invoices"):
                for column_name, pg_type, sqlite_type in common_doc_columns:
                    add_column_if_missing(cur, table_name, column_name, pg_type, sqlite_type)
                for column_name in ("cancelled_at", "cancelled_by", "cancel_reason", "revision_of_document_id", "replacement_document_id"):
                    pg_type = "TEXT" if column_name == "cancel_reason" else "TIMESTAMP" if column_name == "cancelled_at" else "INTEGER"
                    sqlite_type = "TEXT" if column_name in {"cancel_reason", "cancelled_at"} else "INTEGER"
                    add_column_if_missing(cur, table_name, column_name, pg_type, sqlite_type)

            request_item_columns = [
                ("redo_source_mitsumori_id", "INTEGER", "INTEGER"),
                ("redo_source_invoice_id", "INTEGER", "INTEGER"),
                ("redo_requested_at", "TIMESTAMP", "TEXT"),
            ]
            for column_name, pg_type, sqlite_type in request_item_columns:
                add_column_if_missing(cur, "sales_agency_request_items", column_name, pg_type, sqlite_type)

            if DATABASE_URL:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS final_document_events (
                        id SERIAL PRIMARY KEY,
                        document_kind VARCHAR(60) NOT NULL,
                        document_id INTEGER NOT NULL,
                        action VARCHAR(60) NOT NULL,
                        actor_id INTEGER,
                        reason TEXT,
                        before_status VARCHAR(60),
                        after_status VARCHAR(60),
                        metadata TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
            else:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS final_document_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        document_kind TEXT NOT NULL,
                        document_id INTEGER NOT NULL,
                        action TEXT NOT NULL,
                        actor_id INTEGER,
                        reason TEXT,
                        before_status TEXT,
                        after_status TEXT,
                        metadata TEXT,
                        created_at TEXT DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )

            if DATABASE_URL:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS document_rebuild_events (
                        id SERIAL PRIMARY KEY,
                        document_kind VARCHAR(60) NOT NULL,
                        old_document_id INTEGER NOT NULL,
                        new_document_id INTEGER,
                        target_user_id INTEGER,
                        request_item_ids TEXT,
                        merchandise_ids TEXT,
                        reason TEXT NOT NULL,
                        actor_id INTEGER,
                        actor_name TEXT,
                        before_status VARCHAR(60),
                        after_status VARCHAR(60),
                        target_step VARCHAR(60),
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
            else:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS document_rebuild_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        document_kind TEXT NOT NULL,
                        old_document_id INTEGER NOT NULL,
                        new_document_id INTEGER,
                        target_user_id INTEGER,
                        request_item_ids TEXT,
                        merchandise_ids TEXT,
                        reason TEXT NOT NULL,
                        actor_id INTEGER,
                        actor_name TEXT,
                        before_status TEXT,
                        after_status TEXT,
                        target_step TEXT,
                        created_at TEXT DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
            cur.execute("CREATE INDEX IF NOT EXISTS idx_document_rebuild_events_old ON document_rebuild_events (document_kind, old_document_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_document_rebuild_events_new ON document_rebuild_events (document_kind, new_document_id)")
            conn.commit()
        finally:
            cur.close()
            conn.close()

    ensure_schema()

    def add_permission_option() -> None:
        permission_options = getattr(module.User, "ADMIN_PERMISSION_OPTIONS", {}) or {}
        permission_options.setdefault(REBUILD_PERMISSION, "書類の作り直し")
        module.User.ADMIN_PERMISSION_OPTIONS = permission_options

    def normalize_permissions(value: Any) -> list[str]:
        if not value:
            return []
        if isinstance(value, list):
            return [str(item) for item in value]
        try:
            parsed = json.loads(value)
        except Exception:
            return []
        return [str(item) for item in parsed] if isinstance(parsed, list) else []

    def can_rebuild_documents_for(user: Any) -> bool:
        if not user or not getattr(user, "is_authenticated", False):
            return False
        if hasattr(user, "is_owner") and user.is_owner():
            return True
        if not (hasattr(user, "is_admin") and user.is_admin()):
            return False
        permissions = normalize_permissions(getattr(user, "admin_permissions", None))
        if not permissions:
            return True
        return bool(LEGACY_REBUILD_PERMISSIONS.intersection(permissions))

    add_permission_option()
    app.jinja_env.globals["can_rebuild_documents"] = lambda: can_rebuild_documents_for(current_user)

    original_status_label = getattr(module, "document_status_label", None)

    def document_status_label_with_rebuild(kind, status):
        status_value = (status or "").strip()
        if status_value == REPLACED_STATUS:
            return "差替え済み"
        if status_value == "cancelled":
            return "取消済み"
        if callable(original_status_label):
            return original_status_label(kind, status)
        return status_value or "-"

    module.document_status_label = document_status_label_with_rebuild

    def current_actor_name() -> str:
        return (
            getattr(current_user, "display_name", None)
            or getattr(current_user, "username", None)
            or str(getattr(current_user, "id", "") or "")
        )

    def json_ids(values: Iterable[Any]) -> str:
        cleaned: list[int] = []
        for value in values or []:
            try:
                number = int(value)
            except (TypeError, ValueError):
                continue
            if number > 0 and number not in cleaned:
                cleaned.append(number)
        return json.dumps(cleaned, ensure_ascii=False)

    def clean_ids(values: Iterable[Any]) -> list[int]:
        result: list[int] = []
        for value in values or []:
            try:
                number = int(value)
            except (TypeError, ValueError):
                continue
            if number > 0 and number not in result:
                result.append(number)
        return result

    def log_final_document_event(cur, document_kind: str, document_id: int, action: str, reason: str, before_status: str | None, after_status: str | None, metadata: dict[str, Any] | None = None) -> None:
        metadata_text = json.dumps(metadata or {}, ensure_ascii=False)
        cur.execute(
            f"""
            INSERT INTO final_document_events
                (document_kind, document_id, action, actor_id, reason, before_status, after_status, metadata, created_at)
            VALUES ({mark()}, {mark()}, {mark()}, {mark()}, {mark()}, {mark()}, {mark()}, {mark()}, {mark()})
            """,
            (
                document_kind,
                document_id,
                action,
                getattr(current_user, "id", None),
                reason,
                before_status,
                after_status,
                metadata_text,
                get_jst_now(),
            ),
        )

    def insert_rebuild_event(cur, *, document_kind: str, old_document_id: int, target_user_id: int | None, request_item_ids: list[int], merchandise_ids: list[int], reason: str, before_status: str | None, target_step: str) -> None:
        cur.execute(
            f"""
            INSERT INTO document_rebuild_events
                (document_kind, old_document_id, new_document_id, target_user_id,
                 request_item_ids, merchandise_ids, reason, actor_id, actor_name,
                 before_status, after_status, target_step, created_at)
            VALUES ({mark()}, {mark()}, NULL, {mark()}, {mark()}, {mark()}, {mark()}, {mark()}, {mark()}, {mark()}, {mark()}, {mark()}, {mark()})
            """,
            (
                document_kind,
                old_document_id,
                target_user_id,
                json_ids(request_item_ids),
                json_ids(merchandise_ids),
                reason,
                getattr(current_user, "id", None),
                current_actor_name(),
                before_status,
                REPLACED_STATUS,
                target_step,
                get_jst_now(),
            ),
        )

    def update_document_status_to_replaced(cur, table_name: str, document_id: int, reason: str, before_status: str | None) -> None:
        now = get_jst_now()
        assignments = ["status = " + mark()]
        params: list[Any] = [REPLACED_STATUS]
        optional_values = {
            "cancelled_at": now,
            "cancelled_by": getattr(current_user, "id", None),
            "cancel_reason": reason,
            "rebuild_reason": reason,
            "rebuild_requested_at": now,
            "rebuild_requested_by": getattr(current_user, "id", None),
        }
        for column_name, value in optional_values.items():
            if column_exists(cur, table_name, column_name):
                assignments.append(f"{column_name} = {mark()}")
                params.append(value)
        params.append(document_id)
        cur.execute(
            f"UPDATE {table_name} SET {', '.join(assignments)} WHERE id = {mark()}",
            tuple(params),
        )

    def set_replacement_relation(cur, table_name: str, old_document_id: int, new_document_id: int) -> None:
        if column_exists(cur, table_name, "replacement_document_id"):
            cur.execute(
                f"UPDATE {table_name} SET replacement_document_id = {mark()} WHERE id = {mark()}",
                (new_document_id, old_document_id),
            )
        if column_exists(cur, table_name, "revision_of_document_id"):
            cur.execute(
                f"UPDATE {table_name} SET revision_of_document_id = {mark()} WHERE id = {mark()}",
                (old_document_id, new_document_id),
            )

    def update_rebuild_event_new_id(cur, document_kind: str, old_document_id: int, new_document_id: int) -> None:
        cur.execute(
            f"""
            UPDATE document_rebuild_events
            SET new_document_id = {mark()}
            WHERE document_kind = {mark()}
              AND old_document_id = {mark()}
              AND new_document_id IS NULL
            """,
            (new_document_id, document_kind, old_document_id),
        )

    def load_document_events(cur, document_kind: str, document_id: int) -> list[dict[str, Any]]:
        cur.execute(
            f"""
            SELECT e.*, u.username, u.display_name
            FROM final_document_events e
            LEFT JOIN users u ON e.actor_id = u.id
            WHERE e.document_kind = {mark()} AND e.document_id = {mark()}
            ORDER BY e.created_at DESC, e.id DESC
            """,
            (document_kind, document_id),
        )
        events = rows_to_dicts(cur.fetchall())
        for event in events:
            metadata = {}
            if event.get("metadata"):
                try:
                    metadata = json.loads(event.get("metadata") or "{}")
                except Exception:
                    metadata = {}
            event["metadata_obj"] = metadata
            event["changes"] = metadata.get("changes") if isinstance(metadata.get("changes"), list) else []
        return events

    def load_rebuild_events_for_template(document_kind: str, document_id: int) -> list[dict[str, Any]]:
        conn, cur = open_cursor()
        try:
            cur.execute(
                f"""
                SELECT e.*, u.username, u.display_name
                FROM document_rebuild_events e
                LEFT JOIN users u ON e.actor_id = u.id
                WHERE (e.document_kind = {mark()} AND e.old_document_id = {mark()})
                   OR (e.document_kind = {mark()} AND e.new_document_id = {mark()})
                ORDER BY e.created_at DESC, e.id DESC
                """,
                (document_kind, document_id, document_kind, document_id),
            )
            return rows_to_dicts(cur.fetchall())
        finally:
            cur.close()
            conn.close()

    app.jinja_env.globals["load_document_rebuild_events"] = load_rebuild_events_for_template

    def load_vendor_estimate(document_id: int) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        conn, cur = open_cursor()
        try:
            cur.execute(
                f"""
                SELECT m.*, u.display_name AS user_name, u.username
                FROM user_mitsumori m
                LEFT JOIN users u ON m.user_id = u.id
                WHERE m.id = {mark()}
                """,
                (document_id,),
            )
            document = row_to_dict(cur.fetchone())
            if not document:
                return None, []
            document_no = str(document.get("document_no") or "")
            document_scope = str(document.get("document_scope") or "")
            if not (document_no.startswith("MT-") or document_scope == "vendor_outgoing"):
                return None, []
            items = load_request_items_for_mitsumori(cur, document_id)
            return document, items
        finally:
            cur.close()
            conn.close()

    def load_invoice(document_id: int) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        conn, cur = open_cursor()
        try:
            cur.execute(
                f"""
                SELECT i.*, u.display_name AS user_name, u.username
                FROM invoices i
                LEFT JOIN users u ON i.sender_id = u.id
                WHERE i.id = {mark()}
                """,
                (document_id,),
            )
            document = row_to_dict(cur.fetchone())
            if not document:
                return None, []
            if str(document.get("source_workflow_step") or "") != "step4_client_outgoing":
                return None, []
            cur.execute(
                f"""
                SELECT ii.request_item_id,
                       ii.merchandise_id,
                       ii.product_name,
                       ii.brand_name,
                       ii.vendor_name,
                       ii.vendor_document_title,
                       sari.workflow_status,
                       sar.user_id
                FROM invoice_items ii
                LEFT JOIN sales_agency_request_items sari ON ii.request_item_id = sari.id
                LEFT JOIN sales_agency_requests sar ON sari.request_id = sar.id
                WHERE ii.invoice_id = {mark()}
                ORDER BY ii.item_no, ii.id
                """,
                (document_id,),
            )
            items = rows_to_dicts(cur.fetchall())
            return document, items
        finally:
            cur.close()
            conn.close()

    def load_request_items_for_mitsumori(cur, document_id: int) -> list[dict[str, Any]]:
        cur.execute(
            f"""
            SELECT sari.id AS request_item_id,
                   sari.merchandise_id,
                   COALESCE(m.product_name, sari.snapshot_product_name, umi.item_name, '') AS product_name,
                   COALESCE(m.brand_name, sari.snapshot_brand_name, '') AS brand_name,
                   sari.workflow_status,
                   sar.user_id
            FROM sales_agency_request_items sari
            JOIN sales_agency_requests sar ON sari.request_id = sar.id
            LEFT JOIN merchandise m ON sari.merchandise_id = m.id
            LEFT JOIN user_mitsumori_items umi
              ON umi.mitsumori_id = {mark()}
             AND umi.merchandise_id = sari.merchandise_id
            WHERE sari.vendor_mitsumori_id = {mark()}
            ORDER BY sari.id
            """,
            (document_id, document_id),
        )
        items = rows_to_dicts(cur.fetchall())
        if items:
            return items
        cur.execute(
            f"""
            SELECT sari.id AS request_item_id,
                   sari.merchandise_id,
                   COALESCE(m.product_name, sari.snapshot_product_name, umi.item_name, '') AS product_name,
                   COALESCE(m.brand_name, sari.snapshot_brand_name, '') AS brand_name,
                   sari.workflow_status,
                   sar.user_id
            FROM user_mitsumori_items umi
            JOIN sales_agency_request_items sari ON umi.merchandise_id = sari.merchandise_id
            JOIN sales_agency_requests sar ON sari.request_id = sar.id
            LEFT JOIN merchandise m ON sari.merchandise_id = m.id
            WHERE umi.mitsumori_id = {mark()}
            ORDER BY sari.id
            """,
            (document_id,),
        )
        return rows_to_dicts(cur.fetchall())

    def rebuild_vendor_estimate(document_id: int, reason: str) -> int:
        conn, cur = open_cursor()
        try:
            cur.execute(f"SELECT * FROM user_mitsumori WHERE id = {mark()}", (document_id,))
            document = row_to_dict(cur.fetchone())
            if not document:
                abort(404)
            if (document.get("status") or "") == REPLACED_STATUS:
                raise ValueError("この見積依頼書はすでに差替え済みです。")
            items = load_request_items_for_mitsumori(cur, document_id)
            request_item_ids = clean_ids([item.get("request_item_id") for item in items])
            merchandise_ids = clean_ids([item.get("merchandise_id") for item in items])
            if not request_item_ids:
                raise ValueError("ステップ2へ戻す対象商品が見つかりません。")
            placeholders = ", ".join([mark()] * len(request_item_ids))
            now = get_jst_now()
            cur.execute(
                f"""
                UPDATE sales_agency_request_items
                SET workflow_status = {mark()},
                    vendor_mitsumori_id = NULL,
                    moved_to_step3_at = NULL,
                    vendor_document_id = NULL,
                    moved_to_step4_at = NULL,
                    client_invoice_id = NULL,
                    client_invoice_sent_at = NULL,
                    redo_source_mitsumori_id = {mark()},
                    redo_requested_at = {mark()},
                    updated_at = {mark()}
                WHERE id IN ({placeholders})
                """,
                tuple(["step2_ready", document_id, now, now] + request_item_ids),
            )
            if table_exists(cur, "vendor_document_item_links"):
                cur.execute(
                    f"DELETE FROM vendor_document_item_links WHERE request_item_id IN ({placeholders})",
                    tuple(request_item_ids),
                )
            before_status = document.get("status")
            update_document_status_to_replaced(cur, "user_mitsumori", document_id, reason, before_status)
            insert_rebuild_event(
                cur,
                document_kind="user_mitsumori",
                old_document_id=document_id,
                target_user_id=document.get("user_id"),
                request_item_ids=request_item_ids,
                merchandise_ids=merchandise_ids,
                reason=reason,
                before_status=before_status,
                target_step="step2_ready",
            )
            log_final_document_event(
                cur,
                "user_mitsumori",
                document_id,
                "rebuild_to_step2",
                reason,
                before_status,
                REPLACED_STATUS,
                {
                    "target_step": "step2_ready",
                    "request_item_ids": request_item_ids,
                    "merchandise_ids": merchandise_ids,
                    "changes": [
                        {"field": "status", "label": "状態", "before": before_status or "-", "after": "差替え済み"},
                        {"field": "workflow_status", "label": "対象商品ステップ", "before": "作成済み", "after": "Step2"},
                    ],
                },
            )
            conn.commit()
            return len(request_item_ids)
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.close()
            conn.close()

    def rebuild_invoice(document_id: int, reason: str) -> int:
        conn, cur = open_cursor()
        try:
            cur.execute(f"SELECT * FROM invoices WHERE id = {mark()}", (document_id,))
            document = row_to_dict(cur.fetchone())
            if not document:
                abort(404)
            if (document.get("status") or "") == REPLACED_STATUS:
                raise ValueError("この買取明細書はすでに差替え済みです。")
            if str(document.get("source_workflow_step") or "") != "step4_client_outgoing":
                raise ValueError("Step4で作成した買取明細書だけ作り直しできます。")
            cur.execute(
                f"""
                SELECT request_item_id, merchandise_id, product_name
                FROM invoice_items
                WHERE invoice_id = {mark()}
                  AND request_item_id IS NOT NULL
                ORDER BY item_no, id
                """,
                (document_id,),
            )
            items = rows_to_dicts(cur.fetchall())
            request_item_ids = clean_ids([item.get("request_item_id") for item in items])
            merchandise_ids = clean_ids([item.get("merchandise_id") for item in items])
            if not request_item_ids:
                raise ValueError("ステップ4へ戻す対象商品が見つかりません。")
            placeholders = ", ".join([mark()] * len(request_item_ids))
            now = get_jst_now()
            cur.execute(
                f"""
                UPDATE sales_agency_request_items
                SET workflow_status = {mark()},
                    client_invoice_id = NULL,
                    client_invoice_sent_at = NULL,
                    redo_source_invoice_id = {mark()},
                    redo_requested_at = {mark()},
                    updated_at = {mark()}
                WHERE id IN ({placeholders})
                """,
                tuple(["step4_ready", document_id, now, now] + request_item_ids),
            )
            before_status = document.get("status")
            update_document_status_to_replaced(cur, "invoices", document_id, reason, before_status)
            insert_rebuild_event(
                cur,
                document_kind="invoices",
                old_document_id=document_id,
                target_user_id=document.get("sender_id"),
                request_item_ids=request_item_ids,
                merchandise_ids=merchandise_ids,
                reason=reason,
                before_status=before_status,
                target_step="step4_ready",
            )
            log_final_document_event(
                cur,
                "invoices",
                document_id,
                "rebuild_to_step4",
                reason,
                before_status,
                REPLACED_STATUS,
                {
                    "target_step": "step4_ready",
                    "request_item_ids": request_item_ids,
                    "merchandise_ids": merchandise_ids,
                    "changes": [
                        {"field": "status", "label": "状態", "before": before_status or "-", "after": "差替え済み"},
                        {"field": "workflow_status", "label": "対象商品ステップ", "before": "送付済み", "after": "Step4"},
                    ],
                },
            )
            conn.commit()
            return len(request_item_ids)
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.close()
            conn.close()

    REBUILD_CONFIG = {
        "user_mitsumori": {
            "title": "業者向け見積依頼書",
            "table": "user_mitsumori",
            "detail_endpoint": "admin_mitsumori_view",
            "target_step": "Step2",
            "loader": load_vendor_estimate,
            "executor": rebuild_vendor_estimate,
        },
        "invoices": {
            "title": "顧客向け買取明細書",
            "table": "invoices",
            "detail_endpoint": "admin_kaitori_view",
            "target_step": "Step4",
            "loader": load_invoice,
            "executor": rebuild_invoice,
        },
    }

    def normalize_document_kind(document_kind: str) -> str:
        if document_kind == "invoice":
            return "invoices"
        return document_kind

    def rebuild_detail_url(config: dict[str, Any], document_id: int):
        endpoint = config.get("detail_endpoint")
        if endpoint in app.view_functions:
            return url_for(endpoint, id=document_id)
        return url_for("admin_documents_history")

    def rebuild_block_reason(document: dict[str, Any]) -> str | None:
        status = str(document.get("status") or "").strip()
        if status == REPLACED_STATUS:
            return "この書類はすでに差替え済みです。"
        if status == "cancelled":
            return "取消済み書類は作り直しできません。"
        return None

    def admin_rebuild_final_document(document_kind: str, document_id: int):
        ensure_schema()
        if not can_rebuild_documents_for(current_user):
            abort(403)
        normalized_kind = normalize_document_kind(document_kind)
        config = REBUILD_CONFIG.get(normalized_kind)
        if not config:
            abort(404)
        document, items = config["loader"](document_id)
        if not document:
            abort(404)
        block_reason = rebuild_block_reason(document)
        if request.method == "POST":
            if block_reason:
                flash(block_reason, "error")
                return redirect(rebuild_detail_url(config, document_id))
            reason = (request.form.get("reason") or request.form.get("rebuild_reason") or "").strip()
            if not reason:
                return (
                    render_template(
                        "admin/final_document_rebuild.html",
                        config=config,
                        document_kind=normalized_kind,
                        document_id=document_id,
                        document=document,
                        items=items,
                        error="作り直し理由を入力してください。",
                        can_execute=True,
                        detail_url=rebuild_detail_url(config, document_id),
                    ),
                    400,
                )
            try:
                moved_count = config["executor"](document_id, reason)
            except ValueError as exc:
                flash(str(exc), "error")
                return redirect(rebuild_detail_url(config, document_id))
            flash(f"{config['title']}を差替え扱いにし、対象商品 {moved_count}点を{config['target_step']}へ戻しました。", "success")
            return redirect(rebuild_detail_url(config, document_id))
        return render_template(
            "admin/final_document_rebuild.html",
            config=config,
            document_kind=normalized_kind,
            document_id=document_id,
            document=document,
            items=items,
            error=block_reason,
            can_execute=not block_reason,
            detail_url=rebuild_detail_url(config, document_id),
        )

    def parse_redirect_id(response: Any, endpoint_name: str) -> int | None:
        location = None
        if hasattr(response, "location"):
            location = response.location
        if not location and hasattr(response, "headers"):
            location = response.headers.get("Location")
        if not location:
            return None
        patterns = {
            "admin_mitsumori_view": r"/admin/mitsumori/(\d+)",
            "admin_kaitori_view": r"/admin/kaitori/(\d+)",
        }
        match = re.search(patterns.get(endpoint_name, r"/(\d+)(?:\?|$)"), str(location))
        return int(match.group(1)) if match else None

    def source_document_ids_for_items(item_ids: list[int], column_name: str) -> list[int]:
        item_ids = clean_ids(item_ids)
        if not item_ids:
            return []
        conn, cur = open_cursor()
        try:
            placeholders = ", ".join([mark()] * len(item_ids))
            cur.execute(
                f"""
                SELECT DISTINCT {column_name} AS source_id
                FROM sales_agency_request_items
                WHERE id IN ({placeholders})
                  AND {column_name} IS NOT NULL
                """,
                tuple(item_ids),
            )
            return clean_ids([row.get("source_id") for row in rows_to_dicts(cur.fetchall())])
        finally:
            cur.close()
            conn.close()

    def link_replacement_document(table_name: str, document_kind: str, old_document_ids: list[int], new_document_id: int, item_ids: list[int]) -> None:
        if not old_document_ids or not new_document_id:
            return
        conn, cur = open_cursor()
        try:
            for old_document_id in old_document_ids:
                set_replacement_relation(cur, table_name, old_document_id, new_document_id)
                update_rebuild_event_new_id(cur, document_kind, old_document_id, new_document_id)
                log_final_document_event(
                    cur,
                    document_kind,
                    old_document_id,
                    "replacement_created",
                    "作り直し後の新しい書類を作成しました。",
                    REPLACED_STATUS,
                    REPLACED_STATUS,
                    {
                        "old_document_id": old_document_id,
                        "new_document_id": new_document_id,
                        "request_item_ids": clean_ids(item_ids),
                        "changes": [
                            {"field": "replacement_document_id", "label": "新書類ID", "before": "-", "after": str(new_document_id)},
                        ],
                    },
                )
                log_final_document_event(
                    cur,
                    document_kind,
                    new_document_id,
                    "replacement_of",
                    "作り直しにより作成された新しい書類です。",
                    None,
                    None,
                    {
                        "old_document_id": old_document_id,
                        "new_document_id": new_document_id,
                        "request_item_ids": clean_ids(item_ids),
                        "changes": [
                            {"field": "revision_of_document_id", "label": "旧書類ID", "before": "-", "after": str(old_document_id)},
                        ],
                    },
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.close()
            conn.close()

    original_vendor_create = app.view_functions.get("admin_vendor_mitsumori_create")
    if callable(original_vendor_create):
        @wraps(original_vendor_create)
        def admin_vendor_mitsumori_create_with_rebuild_link(*args, **kwargs):
            selected_item_ids = clean_ids(request.form.getlist("request_item_ids")) if request.method == "POST" else []
            old_ids = source_document_ids_for_items(selected_item_ids, "redo_source_mitsumori_id") if selected_item_ids else []
            response = original_vendor_create(*args, **kwargs)
            if request.method == "POST" and old_ids:
                new_id = parse_redirect_id(response, "admin_mitsumori_view")
                if new_id:
                    link_replacement_document("user_mitsumori", "user_mitsumori", old_ids, new_id, selected_item_ids)
            return response

        app.view_functions["admin_vendor_mitsumori_create"] = admin_vendor_mitsumori_create_with_rebuild_link

    original_step4_create = app.view_functions.get("admin_step4_client_invoice_create")
    if callable(original_step4_create):
        @wraps(original_step4_create)
        def admin_step4_client_invoice_create_with_rebuild_link(*args, **kwargs):
            selected_item_ids = clean_ids(request.form.getlist("request_item_ids")) if request.method == "POST" else []
            old_ids = source_document_ids_for_items(selected_item_ids, "redo_source_invoice_id") if selected_item_ids else []
            response = original_step4_create(*args, **kwargs)
            if request.method == "POST" and old_ids:
                new_id = parse_redirect_id(response, "admin_kaitori_view")
                if new_id:
                    link_replacement_document("invoices", "invoices", old_ids, new_id, selected_item_ids)
            return response

        app.view_functions["admin_step4_client_invoice_create"] = admin_step4_client_invoice_create_with_rebuild_link

    rebuild_view = module.login_required(module.admin_required(admin_rebuild_final_document))
    if "admin_rebuild_final_document" in app.view_functions:
        app.view_functions["admin_rebuild_final_document"] = rebuild_view
    else:
        app.add_url_rule(
            "/admin/documents/<document_kind>/<int:document_id>/rebuild",
            endpoint="admin_rebuild_final_document",
            view_func=rebuild_view,
            methods=["GET", "POST"],
        )

    module.can_rebuild_documents = can_rebuild_documents_for
    module.load_document_rebuild_events = load_rebuild_events_for_template
