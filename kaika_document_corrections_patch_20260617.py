# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import sqlite3
from html import escape
from datetime import datetime

from flask import abort, flash, make_response, redirect, render_template, request, url_for
from flask_login import current_user


TERMINAL_SALES_STATUSES = {"cancelled", "rejected", "deal_failed"}
FINAL_DOCUMENT_STATUS = "cancelled"


def apply(module):
    if getattr(module, "_kaika_document_corrections_patch_20260617_applied", False):
        return
    setattr(module, "_kaika_document_corrections_patch_20260617_applied", True)

    app = module.app
    get_db = module.get_db
    get_jst_now = getattr(module, "get_jst_now", lambda: datetime.now())
    DATABASE_URL = getattr(module, "DATABASE_URL", None)
    RealDictCursor = getattr(module, "RealDictCursor", None)

    original_views = {
        name: app.view_functions.get(name)
        for name in (
            "documents",
            "user_invoice_view",
            "user_mitsumori_add",
            "user_mitsumori_list",
            "user_mitsumori_view",
            "admin_user_mitsumori_view",
            "admin_kaitori_view",
            "admin_kaitori_delete",
            "admin_mitsumori_delete",
            "admin_auction_keisan_edit",
            "admin_shikiriosho_edit",
            "admin_kaitori_edit",
            "admin_seisan_edit",
            "admin_mitsumori_edit",
            "view_item",
        )
    }
    original_fetch_sales_agency_request_details = getattr(module, "fetch_sales_agency_request_details", None)

    def mark():
        return "%s" if DATABASE_URL else "?"

    def open_cursor(dict_cursor=True):
        conn = get_db()
        if DATABASE_URL:
            if dict_cursor and RealDictCursor:
                return conn, conn.cursor(cursor_factory=RealDictCursor)
            return conn, conn.cursor()
        conn.row_factory = sqlite3.Row
        return conn, conn.cursor()

    def rows_to_dicts(rows):
        result = []
        for row in rows:
            if row is None:
                continue
            if isinstance(row, dict):
                result.append(dict(row))
            elif hasattr(row, "keys"):
                result.append({key: row[key] for key in row.keys()})
            else:
                result.append(dict(row))
        return result

    def fetch_one_dict(cur):
        row = cur.fetchone()
        if not row:
            return None
        return rows_to_dicts([row])[0]

    def table_exists(cur, table):
        if DATABASE_URL:
            cur.execute(
                "SELECT 1 FROM information_schema.tables WHERE table_schema = 'public' AND table_name = %s",
                (table,),
            )
        else:
            cur.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,))
        return cur.fetchone() is not None

    def column_exists(cur, table, column):
        if DATABASE_URL:
            cur.execute(
                """
                SELECT 1
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = %s
                  AND column_name = %s
                """,
                (table, column),
            )
            return cur.fetchone() is not None
        cur.execute(f"PRAGMA table_info({table})")
        return any(row["name"] == column for row in cur.fetchall())

    def add_column_if_missing(cur, table, column, pg_type, sqlite_type):
        if not table_exists(cur, table) or column_exists(cur, table, column):
            return
        if DATABASE_URL:
            cur.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {pg_type}")
        else:
            cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {sqlite_type}")

    def ensure_schema():
        specs = {
            "user_mitsumori": [
                ("source_invoice_id", "INTEGER", "INTEGER"),
                ("source_document_id", "INTEGER", "INTEGER"),
                ("source_document_type", "VARCHAR(40)", "TEXT"),
                ("cancelled_at", "TIMESTAMP", "TEXT"),
                ("cancelled_by", "INTEGER", "INTEGER"),
                ("cancel_reason", "TEXT", "TEXT"),
                ("revision_of_document_id", "INTEGER", "INTEGER"),
                ("replacement_document_id", "INTEGER", "INTEGER"),
            ],
            "invoices": [
                ("cancelled_at", "TIMESTAMP", "TEXT"),
                ("cancelled_by", "INTEGER", "INTEGER"),
                ("cancel_reason", "TEXT", "TEXT"),
                ("revision_of_document_id", "INTEGER", "INTEGER"),
                ("replacement_document_id", "INTEGER", "INTEGER"),
            ],
            "shikiriosho": [
                ("cancelled_at", "TIMESTAMP", "TEXT"),
                ("cancelled_by", "INTEGER", "INTEGER"),
                ("cancel_reason", "TEXT", "TEXT"),
                ("revision_of_document_id", "INTEGER", "INTEGER"),
                ("replacement_document_id", "INTEGER", "INTEGER"),
            ],
            "user_keisan": [
                ("cancelled_at", "TIMESTAMP", "TEXT"),
                ("cancelled_by", "INTEGER", "INTEGER"),
                ("cancel_reason", "TEXT", "TEXT"),
                ("revision_of_document_id", "INTEGER", "INTEGER"),
                ("replacement_document_id", "INTEGER", "INTEGER"),
            ],
            "user_kaitori_shoudaku": [
                ("cancelled_at", "TIMESTAMP", "TEXT"),
                ("cancelled_by", "INTEGER", "INTEGER"),
                ("cancel_reason", "TEXT", "TEXT"),
                ("revision_of_document_id", "INTEGER", "INTEGER"),
                ("replacement_document_id", "INTEGER", "INTEGER"),
            ],
            "admin_kaitori_shoudaku": [
                ("cancelled_at", "TIMESTAMP", "TEXT"),
                ("cancelled_by", "INTEGER", "INTEGER"),
                ("cancel_reason", "TEXT", "TEXT"),
                ("revision_of_document_id", "INTEGER", "INTEGER"),
                ("replacement_document_id", "INTEGER", "INTEGER"),
            ],
            "sales_agency_requests": [
                ("admin_cancel_reason", "TEXT", "TEXT"),
                ("admin_cancel_note", "TEXT", "TEXT"),
                ("cancelled_at", "TIMESTAMP", "TEXT"),
                ("cancelled_by", "INTEGER", "INTEGER"),
            ],
            "sales_agency_request_items": [
                ("item_status", "VARCHAR(40) DEFAULT 'active'", "TEXT DEFAULT 'active'"),
                ("cancelled_at", "TIMESTAMP", "TEXT"),
                ("cancelled_by", "INTEGER", "INTEGER"),
                ("cancel_reason", "TEXT", "TEXT"),
            ],
        }
        conn, cur = open_cursor()
        try:
            for table, columns in specs.items():
                for column, pg_type, sqlite_type in columns:
                    add_column_if_missing(cur, table, column, pg_type, sqlite_type)
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
            conn.commit()
        finally:
            cur.close()
            conn.close()

    ensure_schema()

    def add_document_permissions():
        permission_options = getattr(module.User, "ADMIN_PERMISSION_OPTIONS", {})
        permission_options.setdefault("can_edit_final_documents", "交付済み書類の修正")
        permission_options.setdefault("can_cancel_documents", "交付済み書類の取消")
        module.User.ADMIN_PERMISSION_OPTIONS = permission_options

        def can_edit_final_documents(self):
            return is_document_privileged_admin(self, "can_edit_final_documents")

        def can_cancel_documents(self):
            return is_document_privileged_admin(self, "can_cancel_documents")

        module.User.can_edit_final_documents = can_edit_final_documents
        module.User.can_cancel_documents = can_cancel_documents

    def is_document_privileged_admin(user, permission_key):
        if not user or not getattr(user, "is_authenticated", False):
            return False
        if hasattr(user, "is_owner") and user.is_owner():
            return True
        if not (hasattr(user, "is_admin") and user.is_admin()):
            return False
        permissions = getattr(user, "admin_permissions", None) or []
        return permission_key in permissions

    add_document_permissions()

    app.jinja_env.globals["can_edit_final_documents"] = lambda: is_document_privileged_admin(current_user, "can_edit_final_documents")
    app.jinja_env.globals["can_cancel_documents"] = lambda: is_document_privileged_admin(current_user, "can_cancel_documents")

    login_required = getattr(module, "login_required")
    permission_required = getattr(module, "permission_required")
    generate_password_hash = getattr(module, "generate_password_hash")
    normalize_submitted_admin_permissions = getattr(module, "normalize_submitted_admin_permissions")
    get_all_admin_permission_keys = getattr(module, "get_all_admin_permission_keys")
    sync_users_id_sequence = getattr(module, "sync_users_id_sequence", lambda _cur: None)

    def parse_permissions_summary(raw_permissions):
        if not raw_permissions:
            return ""
        try:
            permissions = json.loads(raw_permissions) if isinstance(raw_permissions, str) else list(raw_permissions)
        except Exception:
            permissions = []
        labels = getattr(module.User, "ADMIN_PERMISSION_OPTIONS", {})
        return "、".join(labels.get(permission, permission) for permission in permissions)

    def load_operator_user(cur, user_id):
        cur.execute(
            f"""
            SELECT *
            FROM users
            WHERE id = {mark()}
              AND role IN ('admin', 'owner')
            """,
            (user_id,),
        )
        return fetch_one_dict(cur)

    @login_required
    @permission_required("users")
    def admin_operator_users_view():
        search_query = (request.args.get("search") or "").strip()
        conn, cur = open_cursor()
        try:
            params = []
            where = "WHERE role IN ('admin', 'owner')"
            if search_query:
                like = f"%{search_query}%"
                where += f" AND (username LIKE {mark()} OR display_name LIKE {mark()} OR email LIKE {mark()})"
                params.extend([like, like, like])
            cur.execute(
                f"""
                SELECT id, username, email, role, display_name, admin_permissions, last_login
                FROM users
                {where}
                ORDER BY CASE WHEN role = 'owner' THEN 0 ELSE 1 END, id DESC
                """,
                tuple(params),
            )
            users = rows_to_dicts(cur.fetchall())
        finally:
            cur.close()
            conn.close()
        for user in users:
            user["permissions_summary"] = "" if user.get("role") == "owner" else parse_permissions_summary(user.get("admin_permissions"))
        return render_template("admin/operators.html", users=users, search_query=search_query)

    @login_required
    @permission_required("users")
    def admin_operator_add_view():
        if request.method == "POST":
            username = (request.form.get("username") or "").strip()
            email = (request.form.get("email") or "").strip().lower()
            password = request.form.get("password") or ""
            display_name = (request.form.get("display_name") or "").strip()
            role = request.form.get("role") or "admin"
            if role == "owner" and not current_user.is_owner():
                role = "admin"
            if role not in {"admin", "owner"}:
                role = "admin"
            if not username or not email or not password:
                flash("必須項目を入力してください", "error")
                return render_template("admin/operator_form.html", user=None, permission_options=module.User.ADMIN_PERMISSION_OPTIONS)
            if len(password) < 6:
                flash("パスワードは6文字以上で入力してください", "error")
                return render_template("admin/operator_form.html", user=None, permission_options=module.User.ADMIN_PERMISSION_OPTIONS)

            admin_permissions = normalize_submitted_admin_permissions(role, request.form.getlist("admin_permissions"))
            conn, cur = open_cursor(dict_cursor=False)
            try:
                cur.execute(
                    f"""
                    SELECT id
                    FROM users
                    WHERE LOWER(username) = LOWER({mark()}) OR LOWER(email) = LOWER({mark()})
                    LIMIT 1
                    """,
                    (username, email),
                )
                if cur.fetchone():
                    flash("同じユーザー名またはメールアドレスが既に存在します", "error")
                    return render_template("admin/operator_form.html", user=None, permission_options=module.User.ADMIN_PERMISSION_OPTIONS)
                if DATABASE_URL:
                    sync_users_id_sequence(cur)
                cur.execute(
                    f"""
                    INSERT INTO users
                        (username, email, password_hash, role, display_name, created_at, admin_permissions)
                    VALUES ({mark()}, {mark()}, {mark()}, {mark()}, {mark()}, {mark()}, {mark()})
                    """,
                    (
                        username,
                        email,
                        generate_password_hash(password),
                        role,
                        display_name or username,
                        get_jst_now(),
                        admin_permissions,
                    ),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                cur.close()
                conn.close()
            flash("運営アカウントを追加しました", "success")
            return redirect(url_for("admin_operator_users"))
        return render_template("admin/operator_form.html", user=None, permission_options=module.User.ADMIN_PERMISSION_OPTIONS)

    @login_required
    @permission_required("users")
    def admin_operator_edit_view(id):
        conn, cur = open_cursor()
        try:
            user = load_operator_user(cur, id)
            if not user:
                flash("運営アカウントが見つかりません", "error")
                return redirect(url_for("admin_operator_users"))
            if request.method == "POST":
                display_name = (request.form.get("display_name") or "").strip()
                email = (request.form.get("email") or "").strip().lower()
                role = request.form.get("role") or user.get("role") or "admin"
                if role == "owner" and not current_user.is_owner():
                    role = "admin"
                if role not in {"admin", "owner"}:
                    role = "admin"
                permissions_json = normalize_submitted_admin_permissions(role, request.form.getlist("admin_permissions"))
                new_password = request.form.get("new_password") or ""
                assignments = [
                    f"display_name = {mark()}",
                    f"email = {mark()}",
                    f"role = {mark()}",
                    f"admin_permissions = {mark()}",
                ]
                params = [display_name or user.get("username"), email, role, permissions_json]
                if new_password:
                    assignments.append(f"password_hash = {mark()}")
                    params.append(generate_password_hash(new_password))
                params.append(id)
                cur.execute(
                    f"""
                    UPDATE users
                    SET {", ".join(assignments)}
                    WHERE id = {mark()}
                    """,
                    tuple(params),
                )
                conn.commit()
                flash("運営アカウントを更新しました", "success")
                return redirect(url_for("admin_operator_users"))
            user["admin_permissions_list"] = []
            if user.get("role") == "admin":
                try:
                    user["admin_permissions_list"] = json.loads(user.get("admin_permissions") or "[]")
                except Exception:
                    user["admin_permissions_list"] = get_all_admin_permission_keys()
        finally:
            cur.close()
            conn.close()
        return render_template("admin/operator_form.html", user=user, permission_options=module.User.ADMIN_PERMISSION_OPTIONS)

    @login_required
    @permission_required("users")
    def admin_operator_delete_view(id):
        if id == getattr(current_user, "id", None):
            flash("自分自身は削除できません", "error")
            return redirect(url_for("admin_operator_users"))
        conn, cur = open_cursor()
        try:
            user = load_operator_user(cur, id)
            if not user:
                flash("運営アカウントが見つかりません", "error")
                return redirect(url_for("admin_operator_users"))
            if user.get("role") == "owner" and not current_user.is_owner():
                flash("オーナーは削除できません", "error")
                return redirect(url_for("admin_operator_users"))
            cur.execute(f"DELETE FROM users WHERE id = {mark()}", (id,))
            conn.commit()
        finally:
            cur.close()
            conn.close()
        flash("運営アカウントを削除しました", "success")
        return redirect(url_for("admin_operator_users"))

    app.view_functions["admin_operator_users"] = admin_operator_users_view
    app.view_functions["admin_operator_add_user"] = admin_operator_add_view
    app.view_functions["admin_operator_edit_user"] = admin_operator_edit_view
    if "admin_delete_operator" in app.view_functions:
        app.view_functions["admin_delete_operator"] = admin_operator_delete_view
    else:
        app.add_url_rule(
            "/admin/operators/<int:id>/delete",
            endpoint="admin_delete_operator",
            view_func=admin_operator_delete_view,
            methods=["GET", "POST"],
        )

    def log_document_event(cur, document_kind, document_id, action, reason="", before_status=None, after_status=None, metadata=None):
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

    def load_document_events(cur, document_kind, document_id):
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
            metadata_text = event.get("metadata")
            if metadata_text:
                try:
                    metadata = json.loads(metadata_text)
                except (TypeError, ValueError, json.JSONDecodeError):
                    metadata = {}
            event["metadata_obj"] = metadata
            event["changes"] = metadata.get("changes") if isinstance(metadata.get("changes"), list) else []
        return events

    def load_final_document_events_for_template(document_kind, document_id):
        if not document_kind or not document_id:
            return []
        conn, cur = open_cursor()
        try:
            return load_document_events(cur, document_kind, document_id)
        except Exception:
            return []
        finally:
            cur.close()
            conn.close()

    app.jinja_env.globals["load_final_document_events"] = load_final_document_events_for_template

    def generate_user_mitsumori_no(cur, user_id):
        now = get_jst_now()
        cur.execute(
            f"""
            SELECT COUNT(*) AS count
            FROM user_mitsumori
            WHERE user_id = {mark()} AND issue_date >= {mark()}
            """,
            (user_id, now.strftime("%Y-%m-01")),
        )
        result = fetch_one_dict(cur) or {}
        return f"UM-{now.strftime('%Y%m')}-{user_id}-{int(result.get('count') or 0) + 1:04d}"

    def load_invoice_for_current_user(invoice_id):
        conn, cur = open_cursor()
        try:
            cur.execute(
                f"SELECT * FROM invoices WHERE id = {mark()} AND sender_id = {mark()}",
                (invoice_id, current_user.id),
            )
            invoice = fetch_one_dict(cur)
            if invoice:
                cur.execute(f"UPDATE invoices SET is_read = 1 WHERE id = {mark()}", (invoice_id,))
                cur.execute(f"SELECT * FROM invoice_items WHERE invoice_id = {mark()} ORDER BY item_no, id", (invoice_id,))
                items = rows_to_dicts(cur.fetchall())
                conn.commit()
            else:
                items = []
            return invoice, items
        finally:
            cur.close()
            conn.close()

    def source_invoice_is_actionable(invoice):
        if not invoice:
            return False
        status = (invoice.get("status") or "sent").strip()
        scope = (invoice.get("document_scope") or "client_outgoing").strip()
        return scope == "client_outgoing" and status not in {"draft", "in_progress", FINAL_DOCUMENT_STATUS}

    def linked_mitsumori_id_for_invoice(cur, invoice_id, user_id):
        cur.execute(
            f"""
            SELECT id
            FROM user_mitsumori
            WHERE source_invoice_id = {mark()}
              AND user_id = {mark()}
              AND COALESCE(status, '') <> {mark()}
            ORDER BY id DESC
            LIMIT 1
            """,
            (invoice_id, user_id, FINAL_DOCUMENT_STATUS),
        )
        row = fetch_one_dict(cur)
        return row.get("id") if row else None

    def annotate_invoice_mitsumori_state(invoice, cur=None):
        if not invoice:
            return invoice
        owns_cursor = cur is None
        conn = None
        if owns_cursor:
            conn, cur = open_cursor()
        try:
            linked_id = linked_mitsumori_id_for_invoice(cur, invoice.get("id"), current_user.id) if invoice.get("id") else None
            invoice["linked_mitsumori_id"] = linked_id
            invoice["can_create_mitsumori_from_invoice"] = source_invoice_is_actionable(invoice) and not linked_id
            return invoice
        finally:
            if owns_cursor:
                cur.close()
                conn.close()

    def pending_source_invoices(user_id):
        conn, cur = open_cursor()
        try:
            cur.execute(
                f"""
                SELECT i.*
                FROM invoices i
                WHERE i.sender_id = {mark()}
                  AND COALESCE(i.document_scope, 'client_outgoing') = 'client_outgoing'
                  AND COALESCE(i.status, 'sent') NOT IN ('draft', 'in_progress', {mark()})
                  AND NOT EXISTS (
                      SELECT 1
                      FROM user_mitsumori m
                      WHERE m.user_id = i.sender_id
                        AND m.source_invoice_id = i.id
                        AND COALESCE(m.status, '') <> {mark()}
                  )
                ORDER BY COALESCE(i.issue_date, i.created_at) DESC, i.id DESC
                """,
                (user_id, FINAL_DOCUMENT_STATUS, FINAL_DOCUMENT_STATUS),
            )
            return rows_to_dicts(cur.fetchall())
        finally:
            cur.close()
            conn.close()

    def annotate_document_lists(invoices, user_mitsumori_list, user_kaitori_shoudaku_list, user_keisan_list):
        is_user_deletable_invoice_document = getattr(module, "is_user_deletable_invoice_document", lambda _doc: False)
        is_user_created_invoice_document = getattr(module, "is_user_created_invoice_document", lambda _doc: False)
        is_deletable_document_status = getattr(module, "is_deletable_document_status", lambda status: status in {"draft", "in_progress"})
        is_user_deletable_mitsumori_document = getattr(module, "is_user_deletable_mitsumori_document", lambda _doc: False)
        is_user_deletable_keisan_document = getattr(module, "is_user_deletable_keisan_document", lambda _doc: False)

        conn, cur = open_cursor()
        try:
            for invoice in invoices:
                invoice["can_user_delete"] = is_user_deletable_invoice_document(invoice)
                invoice["can_user_edit"] = is_user_created_invoice_document(invoice) and is_deletable_document_status(invoice.get("status"))
                annotate_invoice_mitsumori_state(invoice, cur)
            for doc in user_mitsumori_list:
                doc["can_user_delete"] = is_user_deletable_mitsumori_document(doc)
                doc["can_user_edit"] = doc["can_user_delete"] and not doc.get("source_invoice_id")
            for doc in user_kaitori_shoudaku_list:
                doc["can_user_delete"] = is_deletable_document_status(doc.get("status"))
                doc["can_user_edit"] = doc["can_user_delete"]
            for doc in user_keisan_list:
                doc["can_user_delete"] = is_user_deletable_keisan_document(doc)
                doc["can_user_edit"] = doc["can_user_delete"]
        finally:
            cur.close()
            conn.close()

    def documents_v3():
        active_tab = (request.args.get("tab") or "kaitori").strip()
        if active_tab not in {"kaitori", "mitsumori", "shoudaku", "shikiri", "keisan"}:
            active_tab = "kaitori"
        conn, cur = open_cursor()
        try:
            cur.execute(
                f"""
                SELECT *
                FROM invoices
                WHERE sender_id = {mark()}
                  AND COALESCE(document_scope, 'client_outgoing') = 'client_outgoing'
                  AND COALESCE(status, 'draft') NOT IN ('draft', 'in_progress')
                ORDER BY created_at DESC
                """,
                (current_user.id,),
            )
            invoices = rows_to_dicts(cur.fetchall())

            cur.execute(
                f"""
                SELECT *
                FROM user_mitsumori
                WHERE user_id = {mark()}
                  AND COALESCE(document_scope, 'client_incoming') = 'client_incoming'
                  AND COALESCE(status, 'draft') NOT IN ('draft', 'in_progress')
                ORDER BY created_at DESC
                """,
                (current_user.id,),
            )
            user_mitsumori_list = rows_to_dicts(cur.fetchall())

            cur.execute(
                f"""
                SELECT *
                FROM user_kaitori_shoudaku
                WHERE user_id = {mark()}
                  AND COALESCE(document_scope, 'client_incoming') = 'client_incoming'
                ORDER BY created_at DESC
                """,
                (current_user.id,),
            )
            user_kaitori_shoudaku_list = rows_to_dicts(cur.fetchall())

            cur.execute(
                f"""
                SELECT *
                FROM user_keisan
                WHERE user_id = {mark()}
                ORDER BY created_at DESC
                """,
                (current_user.id,),
            )
            user_keisan_list = rows_to_dicts(cur.fetchall())

            cur.execute(
                f"""
                SELECT s.*, u.display_name AS sender_display_name
                FROM shikiriosho s
                LEFT JOIN users u ON s.sender_id = u.id
                WHERE s.recipient_id = {mark()}
                ORDER BY s.issue_date DESC, s.id DESC
                """,
                (current_user.id,),
            )
            shikiriosho_list = rows_to_dicts(cur.fetchall())

            admin_invoice_read_ids = [invoice.get("id") for invoice in invoices if invoice.get("id")]
            if admin_invoice_read_ids:
                placeholders = ",".join([mark()] * len(admin_invoice_read_ids))
                cur.execute(f"UPDATE invoices SET is_read = 1 WHERE id IN ({placeholders})", tuple(admin_invoice_read_ids))
            conn.commit()
        finally:
            cur.close()
            conn.close()

        annotate_document_lists(invoices, user_mitsumori_list, user_kaitori_shoudaku_list, user_keisan_list)
        pending = pending_source_invoices(current_user.id)
        return render_template(
            "documents.html",
            invoices=invoices,
            user_mitsumori_list=user_mitsumori_list,
            user_kaitori_shoudaku_list=user_kaitori_shoudaku_list,
            user_keisan_list=user_keisan_list,
            shikiriosho_list=shikiriosho_list,
            pending_mitsumori_from_invoices=pending,
            active_tab=active_tab,
        )

    def user_invoice_view_v3(id):
        invoice, items = load_invoice_for_current_user(id)
        if not invoice:
            flash("買取明細書が見つかりません。", "error")
            return redirect(url_for("user_invoice_list"))
        is_user_visible_invoice = getattr(module, "is_user_visible_invoice", lambda _doc: True)
        if not is_user_visible_invoice(invoice):
            flash("この買取明細書は現在表示できません。", "error")
            return redirect(url_for("user_invoice_list"))
        annotate_document_lists([invoice], [], [], [])
        return render_template("invoice_view.html", invoice=invoice, items=items)

    def load_source_invoice(invoice_id):
        conn, cur = open_cursor()
        try:
            cur.execute(
                f"""
                SELECT *
                FROM invoices
                WHERE id = {mark()} AND sender_id = {mark()}
                """,
                (invoice_id, current_user.id),
            )
            invoice = fetch_one_dict(cur)
            if not source_invoice_is_actionable(invoice):
                return None, []
            cur.execute(f"SELECT * FROM invoice_items WHERE invoice_id = {mark()} ORDER BY item_no, id", (invoice_id,))
            return invoice, rows_to_dicts(cur.fetchall())
        finally:
            cur.close()
            conn.close()

    def normalize_source_items(rows):
        normalized = []
        for index, row in enumerate(rows, 1):
            quantity = int(row.get("quantity") or 1)
            amount = int(row.get("amount") or 0)
            unit_price = int(row.get("unit_price") or 0) if row.get("unit_price") is not None else 0
            if not unit_price and quantity:
                unit_price = amount // quantity
            normalized.append(
                {
                    "id": row.get("id"),
                    "item_no": row.get("item_no") or index,
                    "product_name": row.get("product_name") or row.get("item_name") or f"Item {index}",
                    "merchandise_id": row.get("merchandise_id"),
                    "quantity": quantity,
                    "unit": row.get("unit") or "点",
                    "unit_price": unit_price,
                    "amount": amount,
                }
            )
        return normalized

    def user_mitsumori_list_v3():
        conn, cur = open_cursor()
        try:
            cur.execute(
                f"""
                SELECT *
                FROM user_mitsumori
                WHERE user_id = {mark()}
                  AND COALESCE(document_scope, 'client_incoming') = 'client_incoming'
                  AND COALESCE(status, 'draft') IN ('draft', 'in_progress')
                ORDER BY created_at DESC
                """,
                (current_user.id,),
            )
            mitsumori_list = rows_to_dicts(cur.fetchall())
        finally:
            cur.close()
            conn.close()
        return render_template(
            "mitsumori_list.html",
            mitsumori_list=mitsumori_list,
            pending_mitsumori_from_invoices=pending_source_invoices(current_user.id),
        )

    def user_mitsumori_add_v3():
        source_invoice_id = request.args.get("source_invoice_id", type=int) or request.form.get("source_invoice_id", type=int)
        if not source_invoice_id:
            original = original_views.get("user_mitsumori_add")
            if callable(original):
                return original()
            flash("見積依頼書作成画面を表示できません。", "error")
            return redirect(url_for("user_document_list"))

        source_invoice, source_invoice_items = load_source_invoice(source_invoice_id)
        source_items = normalize_source_items(source_invoice_items)
        if not source_invoice:
            flash("元の買取明細書が見つからないか、見積依頼書を作成できない状態です。", "error")
            return redirect(url_for("documents", tab="kaitori") + "#document-history-tabs")

        conn, cur = open_cursor()
        try:
            existing_id = linked_mitsumori_id_for_invoice(cur, source_invoice_id, current_user.id)
            if existing_id:
                return redirect(url_for("user_mitsumori_view", id=existing_id))
            document_no = generate_user_mitsumori_no(cur, current_user.id)

            if request.method == "POST":
                selected_ids = request.form.getlist("source_invoice_item_id[]") or request.form.getlist("source_invoice_item_choice[]")
                selected_ids = [int(value) for value in selected_ids if str(value).isdigit()]
                if not selected_ids:
                    flash("見積依頼書に反映する商品を選択してください。", "error")
                    return render_template(
                        "mitsumori_form.html",
                        mitsumori=None,
                        items=[],
                        today=source_invoice.get("issue_date"),
                        document_no=document_no,
                        my_merchandise=[],
                        source_invoice=source_invoice,
                        source_items=source_items,
                    )

                allowed = {int(item["id"]): item for item in source_items if item.get("id") is not None}
                selected_items = [allowed[item_id] for item_id in selected_ids if item_id in allowed]
                if not selected_items:
                    flash("元の買取明細書の商品だけを選択してください。", "error")
                    return redirect(url_for("user_mitsumori_add", source_invoice_id=source_invoice_id))

                total_amount = sum(int(item.get("amount") or 0) for item in selected_items)
                issue_date = source_invoice.get("issue_date") or get_jst_now().strftime("%Y-%m-%d")
                valid_until = request.form.get("valid_until") or None
                notes = (request.form.get("notes") or "").strip()
                raw_status = request.form.get("status") or "draft"
                status = "completed" if raw_status == "completed" else "draft"
                subject = (request.form.get("subject") or "").strip() or "買取明細書に基づく見積依頼書"
                company_name = (request.form.get("company_name") or "").strip() or "株式会社 開花"
                department = (request.form.get("department") or "").strip()
                contact_person = (request.form.get("contact_person") or "").strip()
                address = (request.form.get("address") or "").strip()

                cur.execute(
                    f"""
                    INSERT INTO user_mitsumori
                        (document_no, user_id, issue_date, valid_until, company_name, department,
                         contact_person, address, subject, total_amount, notes, status,
                         document_scope, source_invoice_id, source_document_id, source_document_type)
                    VALUES
                        ({mark()}, {mark()}, {mark()}, {mark()}, {mark()}, {mark()},
                         {mark()}, {mark()}, {mark()}, {mark()}, {mark()}, {mark()},
                         {mark()}, {mark()}, {mark()}, {mark()})
                    """
                    + (" RETURNING id" if DATABASE_URL else ""),
                    (
                        document_no,
                        current_user.id,
                        issue_date,
                        valid_until,
                        company_name,
                        department,
                        contact_person,
                        address,
                        subject,
                        total_amount,
                        notes,
                        status,
                        "client_incoming",
                        source_invoice_id,
                        source_invoice_id,
                        "invoice",
                    ),
                )
                if DATABASE_URL:
                    mitsumori_id = fetch_one_dict(cur)["id"]
                else:
                    mitsumori_id = cur.lastrowid

                for index, item in enumerate(selected_items, 1):
                    cur.execute(
                        f"""
                        INSERT INTO user_mitsumori_items
                            (mitsumori_id, item_no, item_name, merchandise_id, quantity, unit, unit_price, amount)
                        VALUES ({mark()}, {mark()}, {mark()}, {mark()}, {mark()}, {mark()}, {mark()}, {mark()})
                        """,
                        (
                            mitsumori_id,
                            index,
                            item.get("product_name"),
                            item.get("merchandise_id"),
                            item.get("quantity") or 1,
                            item.get("unit") or "点",
                            int(item.get("unit_price") or 0),
                            int(item.get("amount") or 0),
                        ),
                    )
                log_document_event(
                    cur,
                    "user_mitsumori",
                    mitsumori_id,
                    "create_from_invoice",
                    "買取明細書から見積依頼書を作成",
                    None,
                    status,
                    {"source_invoice_id": source_invoice_id},
                )
                conn.commit()
                flash("見積依頼書を作成しました。", "success")
                if status == "draft":
                    return redirect(url_for("user_mitsumori_list"))
                return redirect(url_for("user_mitsumori_view", id=mitsumori_id))
        finally:
            cur.close()
            conn.close()

        return render_template(
            "mitsumori_form.html",
            mitsumori=None,
            items=[],
            today=source_invoice.get("issue_date"),
            document_no=document_no,
            my_merchandise=[],
            source_invoice=source_invoice,
            source_items=source_items,
        )

    def user_mitsumori_view_v3(id):
        original = original_views.get("user_mitsumori_view")
        if callable(original):
            return original(id)
        flash("見積依頼書を表示できません。", "error")
        return redirect(url_for("documents", tab="mitsumori") + "#document-history-tabs")

    DOCUMENT_CANCEL_TABLES = {
        "invoices": ("invoices", "admin_kaitori_view"),
        "user_mitsumori": ("user_mitsumori", "admin_user_mitsumori_view"),
        "shikiriosho": ("shikiriosho", "admin_shikiriosho_view"),
        "user_keisan": ("user_keisan", "admin_auction_keisan_view"),
        "user_kaitori_shoudaku": ("user_kaitori_shoudaku", "admin_user_kaitori_shoudaku_view"),
        "admin_kaitori_shoudaku": ("admin_kaitori_shoudaku", "admin_kaitori_shoudaku_view"),
    }

    DOCUMENT_EDIT_CONFIG = {
        "invoices": {
            "table": "invoices",
            "detail_endpoint": "admin_kaitori_view",
            "title": "\u8cb7\u53d6\u660e\u7d30\u66f8",
            "fields": [
                {"name": "notes", "label": "\u5099\u8003", "type": "textarea"},
            ],
        },
        "user_mitsumori": {
            "table": "user_mitsumori",
            "detail_endpoint": "admin_user_mitsumori_view",
            "title": "\u898b\u7a4d\u4f9d\u983c\u66f8",
            "fields": [
                {"name": "subject", "label": "\u4ef6\u540d", "type": "text"},
                {"name": "notes", "label": "\u5099\u8003\u30fb\u6761\u4ef6", "type": "textarea"},
            ],
        },
        "shikiriosho": {
            "table": "shikiriosho",
            "detail_endpoint": "admin_shikiriosho_view",
            "title": "\u7cbe\u7b97\u66f8",
            "fields": [
                {"name": "notes", "label": "\u5099\u8003", "type": "textarea"},
            ],
        },
        "user_keisan": {
            "table": "user_keisan",
            "detail_endpoint": "admin_auction_keisan_view",
            "title": "\u8a08\u7b97\u66f8",
            "fields": [
                {"name": "subject", "label": "\u4ef6\u540d", "type": "text"},
                {"name": "notes", "label": "\u5099\u8003", "type": "textarea"},
            ],
        },
        "user_kaitori_shoudaku": {
            "table": "user_kaitori_shoudaku",
            "detail_endpoint": "admin_user_kaitori_shoudaku_view",
            "title": "\u30e6\u30fc\u30b6\u30fc\u8cb7\u53d6\u627f\u8afe\u66f8",
            "fields": [
                {"name": "notes", "label": "\u5099\u8003", "type": "textarea"},
            ],
        },
    }

    def get_editable_document_fields(cur, config, document):
        table = config["table"]
        fields = []
        for field in config["fields"]:
            name = field["name"]
            if column_exists(cur, table, name):
                prepared = dict(field)
                prepared["value"] = document.get(name) or ""
                fields.append(prepared)
        return fields

    def final_document_detail_redirect(config, document_id):
        endpoint = config.get("detail_endpoint")
        if endpoint in app.view_functions:
            return redirect(url_for(endpoint, id=document_id))
        return redirect(url_for("admin_documents_history"))

    def admin_final_document_edit(document_kind, document_id):
        if not is_document_privileged_admin(current_user, "can_edit_final_documents"):
            abort(403)
        config = DOCUMENT_EDIT_CONFIG.get(document_kind)
        if not config:
            abort(404)

        conn, cur = open_cursor()
        try:
            table = config["table"]
            if not table_exists(cur, table):
                abort(404)
            cur.execute(f"SELECT * FROM {table} WHERE id = {mark()}", (document_id,))
            document = fetch_one_dict(cur)
            if not document:
                abort(404)

            fields = get_editable_document_fields(cur, config, document)
            if not fields:
                flash("\u4fee\u6b63\u53ef\u80fd\u306a\u9805\u76ee\u304c\u3042\u308a\u307e\u305b\u3093\u3002", "error")
                return final_document_detail_redirect(config, document_id)

            error = None
            if request.method == "POST":
                reason = (request.form.get("reason") or request.form.get("correction_reason") or "").strip()
                if not reason:
                    error = "\u4fee\u6b63\u7406\u7531\u3092\u5165\u529b\u3057\u3066\u304f\u3060\u3055\u3044\u3002"
                changes = []
                update_names = []
                update_values = []
                for field in fields:
                    name = field["name"]
                    before = "" if document.get(name) is None else str(document.get(name))
                    after = request.form.get(name)
                    after = "" if after is None else after.strip()
                    if before != after:
                        changes.append(
                            {
                                "field": name,
                                "label": field["label"],
                                "before": before,
                                "after": after,
                            }
                        )
                        update_names.append(name)
                        update_values.append(after)

                if not error and not changes:
                    error = "\u4fee\u6b63\u3059\u308b\u9805\u76ee\u304c\u5909\u66f4\u3055\u308c\u3066\u3044\u307e\u305b\u3093\u3002"

                if not error:
                    set_clauses = [f"{name} = {mark()}" for name in update_names]
                    if column_exists(cur, table, "updated_at"):
                        set_clauses.append(f"updated_at = {mark()}")
                        update_values.append(get_jst_now())
                    if column_exists(cur, table, "updated_by"):
                        set_clauses.append(f"updated_by = {mark()}")
                        update_values.append(getattr(current_user, "id", None))
                    update_values.append(document_id)
                    cur.execute(
                        f"UPDATE {table} SET {', '.join(set_clauses)} WHERE id = {mark()}",
                        tuple(update_values),
                    )
                    log_document_event(
                        cur,
                        document_kind,
                        document_id,
                        "edit",
                        reason,
                        document.get("status"),
                        document.get("status"),
                        {"changes": changes, "document_kind": document_kind, "document_id": document_id},
                    )
                    conn.commit()
                    flash("\u66f8\u985e\u3092\u4fee\u6b63\u3057\u307e\u3057\u305f\u3002", "success")
                    return final_document_detail_redirect(config, document_id)

                for field in fields:
                    field["value"] = request.form.get(field["name"], field.get("value", ""))
                return (
                    render_template(
                        "admin/final_document_edit.html",
                        document=document,
                        document_kind=document_kind,
                        document_id=document_id,
                        config=config,
                        fields=fields,
                        error=error,
                    ),
                    400,
                )

            return render_template(
                "admin/final_document_edit.html",
                document=document,
                document_kind=document_kind,
                document_id=document_id,
                config=config,
                fields=fields,
                error=None,
            )
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.close()
            conn.close()

    def admin_cancel_final_document(document_kind, document_id):
        if not is_document_privileged_admin(current_user, "can_cancel_documents"):
            flash("この書類を取り消す権限がありません。", "error")
            return redirect(request.referrer or url_for("admin_documents_history"))
        config = DOCUMENT_CANCEL_TABLES.get(document_kind)
        if not config:
            flash("取消対象の書類種別が正しくありません。", "error")
            return redirect(request.referrer or url_for("admin_documents_history"))
        reason = (request.form.get("reason") or request.form.get("cancel_reason") or "").strip()
        if not reason:
            flash("取消理由を入力してください。", "error")
            return redirect(request.referrer or url_for("admin_documents_history"))
        table, detail_endpoint = config
        conn, cur = open_cursor()
        try:
            cur.execute(f"SELECT * FROM {table} WHERE id = {mark()}", (document_id,))
            document = fetch_one_dict(cur)
            if not document:
                flash("書類が見つかりません。", "error")
                return redirect(request.referrer or url_for("admin_documents_history"))
            before_status = document.get("status")
            cur.execute(
                f"""
                UPDATE {table}
                SET status = {mark()},
                    cancelled_at = {mark()},
                    cancelled_by = {mark()},
                    cancel_reason = {mark()}
                WHERE id = {mark()}
                """,
                (FINAL_DOCUMENT_STATUS, get_jst_now(), current_user.id, reason, document_id),
            )
            log_document_event(
                cur,
                document_kind,
                document_id,
                "cancel",
                reason,
                before_status,
                FINAL_DOCUMENT_STATUS,
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.close()
            conn.close()
        flash("書類を取消済みにしました。", "success")
        if detail_endpoint in app.view_functions:
            return redirect(url_for(detail_endpoint, id=document_id))
        return redirect(request.referrer or url_for("admin_documents_history"))

    def admin_kaitori_view_v3(id):
        if not current_user.is_authenticated or not current_user.is_admin():
            flash("アクセス権限がありません。", "error")
            return redirect(url_for("index"))
        conn, cur = open_cursor()
        try:
            cur.execute(
                f"""
                SELECT i.*, u.display_name AS sender_display_name, u.username AS sender_username
                FROM invoices i
                LEFT JOIN users u ON i.sender_id = u.id
                WHERE i.id = {mark()}
                """,
                (id,),
            )
            invoice = fetch_one_dict(cur)
            if not invoice:
                flash("買取明細書が見つかりません。", "error")
                return redirect(url_for("admin_documents_history"))
            cur.execute(f"SELECT * FROM invoice_items WHERE invoice_id = {mark()} ORDER BY item_no, id", (id,))
            items = rows_to_dicts(cur.fetchall())
            cur.execute(
                f"""
                SELECT id, document_no, status, issue_date
                FROM user_mitsumori
                WHERE source_invoice_id = {mark()}
                ORDER BY id DESC
                """,
                (id,),
            )
            linked_mitsumori = rows_to_dicts(cur.fetchall())
            events = load_document_events(cur, "invoices", id)
        finally:
            cur.close()
            conn.close()
        return render_template(
            "admin/invoice_view.html",
            invoice=invoice,
            items=items,
            linked_mitsumori=linked_mitsumori,
            document_events=events,
        )

    def admin_user_mitsumori_view_v3(id):
        if not current_user.is_authenticated or not current_user.is_admin():
            flash("アクセス権限がありません。", "error")
            return redirect(url_for("index"))
        conn, cur = open_cursor()
        try:
            cur.execute(
                f"""
                SELECT m.*, u.display_name AS user_name, u.username
                FROM user_mitsumori m
                LEFT JOIN users u ON m.user_id = u.id
                WHERE m.id = {mark()}
                """,
                (id,),
            )
            mitsumori = fetch_one_dict(cur)
            if not mitsumori:
                flash("見積依頼書が見つかりません。", "error")
                return redirect(url_for("admin_documents_history"))
            cur.execute(f"SELECT * FROM user_mitsumori_items WHERE mitsumori_id = {mark()} ORDER BY item_no, id", (id,))
            items = rows_to_dicts(cur.fetchall())
            source_invoice = None
            if mitsumori.get("source_invoice_id"):
                cur.execute(
                    f"""
                    SELECT i.*, u.display_name AS sender_display_name, u.username AS sender_username
                    FROM invoices i
                    LEFT JOIN users u ON i.sender_id = u.id
                    WHERE i.id = {mark()}
                    """,
                    (mitsumori.get("source_invoice_id"),),
                )
                source_invoice = fetch_one_dict(cur)
            events = load_document_events(cur, "user_mitsumori", id)
        finally:
            cur.close()
            conn.close()
        return render_template(
            "admin/user_mitsumori_view.html",
            mitsumori=mitsumori,
            items=items,
            source_invoice=source_invoice,
            document_events=events,
        )

    def protected_admin_delete(original_endpoint, document_kind):
        original = original_views.get(original_endpoint)

        def wrapped(id):
            if not is_document_privileged_admin(current_user, "can_cancel_documents"):
                flash("交付済み書類の削除/取消権限がありません。", "error")
                return redirect(request.referrer or url_for("admin_documents_history"))
            if callable(original):
                return original(id)
            return redirect(url_for("admin_documents_history"))

        wrapped.__name__ = f"{original_endpoint}_protected"
        return wrapped

    def protected_admin_edit(original_endpoint):
        original = original_views.get(original_endpoint)

        def wrapped(id):
            if not is_document_privileged_admin(current_user, "can_edit_final_documents"):
                flash("交付済み書類の修正権限がありません。", "error")
                return redirect(request.referrer or url_for("admin_documents_history"))
            if callable(original):
                return original(id)
            return redirect(request.referrer or url_for("admin_documents_history"))

        wrapped.__name__ = f"{original_endpoint}_privileged_edit"
        return wrapped

    def sales_agency_label(status, viewer="admin"):
        if status == "deal_failed":
            return "取引不成立"
        if status == "cancelled":
            return "キャンセル済み"
        if status == "rejected":
            return "受付不可"
        return None

    def fetch_sales_agency_request_details_v3(request_id, viewer="admin"):
        if callable(original_fetch_sales_agency_request_details):
            request_row, items = original_fetch_sales_agency_request_details(request_id, viewer=viewer)
        else:
            request_row, items = None, []
        if not request_row:
            return request_row, items
        status = (request_row.get("status") or "").strip()
        label = sales_agency_label(status, viewer)
        if label:
            request_row["status_label"] = label
            request_row["client_status_label"] = label
            request_row["document_flow_label"] = label
        request_row["is_past"] = status in TERMINAL_SALES_STATUSES or bool(request_row.get("is_past"))
        if status != "completed":
            request_row["request_can_create_client_invoice"] = False
        if status in TERMINAL_SALES_STATUSES:
            for key in (
                "request_can_create_vendor_estimate",
                "request_can_register_vendor_kaitori",
                "request_can_create_client_invoice",
                "request_can_create_shikiriosho",
                "request_can_create_auction_keisan",
                "request_can_create_documents",
            ):
                request_row[key] = False
        return request_row, items

    def sales_agency_my_requests_v3(service_slug=None):
        period_filter = (request.args.get("period") or "current").strip()
        if period_filter not in {"current", "past", "all"}:
            period_filter = "current"

        service_types = getattr(module, "SALES_AGENCY_SERVICE_TYPES", {})
        slug_to_service = getattr(module, "SALES_AGENCY_SLUG_TO_SERVICE", {})
        sales_agency_service_slug = getattr(module, "sales_agency_service_slug", lambda value: value)

        if service_slug:
            service_filter = slug_to_service.get((service_slug or "").strip(), "")
            if service_filter not in {"wholesale", "auction", "simultaneous"}:
                return redirect(url_for("sales_agency_my_requests"))
        else:
            requested_service = (request.args.get("service") or "all").strip()
            if requested_service in {"wholesale", "auction", "simultaneous"}:
                return redirect(
                    url_for(
                        "sales_agency_my_requests",
                        service_slug=sales_agency_service_slug(requested_service),
                        period=period_filter,
                    )
                )
            service_filter = "all"

        show_service_top = service_filter == "all" and not service_slug
        requests_list = []
        conn, cur = open_cursor()
        try:
            cur.execute(
                f"""
                SELECT id
                FROM sales_agency_requests
                WHERE user_id = {mark()}
                ORDER BY created_at DESC, id DESC
                """,
                (current_user.id,),
            )
            request_ids = [row["id"] if isinstance(row, dict) else row[0] for row in cur.fetchall()]
        finally:
            cur.close()
            conn.close()

        for request_id in request_ids:
            request_row, request_items = fetch_sales_agency_request_details_v3(request_id, viewer="client")
            if not request_row:
                continue
            request_row["request_items"] = request_items
            status = (request_row.get("status") or "").strip()
            request_row["is_past"] = status in ({"completed"} | TERMINAL_SALES_STATUSES)
            request_row["period_label"] = "過去" if request_row["is_past"] else "現行"
            requests_list.append(request_row)

        service_counts = {"all": len(requests_list), "wholesale": 0, "auction": 0, "simultaneous": 0}
        service_period_counts = {
            "wholesale": {"all": 0, "current": 0, "past": 0},
            "auction": {"all": 0, "current": 0, "past": 0},
            "simultaneous": {"all": 0, "current": 0, "past": 0},
        }
        period_counts = {"all": len(requests_list), "current": 0, "past": 0}
        for request_row in requests_list:
            service_type = (request_row.get("service_type") or "").strip()
            is_past = bool(request_row.get("is_past"))
            if service_type in service_counts:
                service_counts[service_type] += 1
                service_period_counts[service_type]["all"] += 1
                service_period_counts[service_type]["past" if is_past else "current"] += 1
            period_counts["past" if is_past else "current"] += 1

        service_cards = []
        for key in ("wholesale", "auction", "simultaneous"):
            counts = service_period_counts.get(key, {})
            service_cards.append(
                {
                    "key": key,
                    "slug": sales_agency_service_slug(key),
                    "title": service_types.get(key, key),
                    "count": service_counts.get(key, 0),
                    "current_count": counts.get("current", 0),
                    "past_count": counts.get("past", 0),
                    "url": url_for("sales_agency_my_requests", service_slug=sales_agency_service_slug(key)),
                }
            )

        filtered_requests = []
        if not show_service_top:
            for request_row in requests_list:
                if service_filter != "all" and (request_row.get("service_type") or "").strip() != service_filter:
                    continue
                if period_filter == "current" and request_row.get("is_past"):
                    continue
                if period_filter == "past" and not request_row.get("is_past"):
                    continue
                filtered_requests.append(request_row)

        display_period_counts = (
            service_period_counts.get(service_filter, period_counts)
            if service_filter in service_period_counts
            else period_counts
        )
        return render_template(
            "sales_agency_requests.html",
            requests=filtered_requests,
            service_types=service_types,
            statuses=getattr(module, "SALES_AGENCY_STATUS_CLIENT", {}),
            service_filter=service_filter,
            period_filter=period_filter,
            service_counts=service_counts,
            service_cards=service_cards,
            show_service_top=show_service_top,
            selected_service_slug=sales_agency_service_slug(service_filter),
            selected_service_name=service_types.get(service_filter, ""),
            period_counts=display_period_counts,
        )

    def admin_sales_agency_admin_cancel(id):
        if not current_user.is_authenticated or not current_user.is_admin():
            flash("アクセス権限がありません。", "error")
            return redirect(url_for("index"))
        target_status = (request.form.get("status") or "").strip()
        if target_status not in TERMINAL_SALES_STATUSES:
            flash("キャンセル区分が正しくありません。", "error")
            return redirect(request.referrer or url_for("admin_sales_agency_requests"))
        reason = (request.form.get("reason") or request.form.get("admin_note") or "").strip()
        if not reason:
            flash("理由を入力してください。", "error")
            return redirect(request.referrer or url_for("admin_sales_agency_requests"))
        conn, cur = open_cursor()
        try:
            cur.execute(f"SELECT status FROM sales_agency_requests WHERE id = {mark()}", (id,))
            row = fetch_one_dict(cur)
            if not row:
                flash("申請が見つかりません。", "error")
                return redirect(request.referrer or url_for("admin_sales_agency_requests"))
            before_status = row.get("status")
            cur.execute(
                f"""
                UPDATE sales_agency_requests
                SET status = {mark()},
                    admin_note = {mark()},
                    admin_cancel_reason = {mark()},
                    admin_cancel_note = {mark()},
                    cancelled_at = {mark()},
                    cancelled_by = {mark()},
                    processed_at = {mark()},
                    processed_by = {mark()}
                WHERE id = {mark()}
                """,
                (
                    target_status,
                    reason,
                    reason,
                    reason,
                    get_jst_now(),
                    current_user.id,
                    get_jst_now(),
                    current_user.id,
                    id,
                ),
            )
            cur.execute(
                f"""
                UPDATE sales_agency_request_items
                SET item_status = {mark()},
                    cancelled_at = {mark()},
                    cancelled_by = {mark()},
                    cancel_reason = COALESCE(NULLIF(cancel_reason, ''), {mark()})
                WHERE request_id = {mark()}
                  AND COALESCE(item_status, 'active') NOT IN ('cancelled', 'canceled')
                """,
                ("cancelled", get_jst_now(), current_user.id, reason, id),
            )
            log_document_event(
                cur,
                "sales_agency_requests",
                id,
                "terminal_status",
                reason,
                before_status,
                target_status,
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.close()
            conn.close()
        flash("業者系申請の状態を更新しました。", "success")
        return redirect(request.referrer or url_for("admin_sales_agency_requests"))

    def load_terminal_sales_agency_request_for_item(item_id):
        if not current_user.is_authenticated or current_user.is_admin() or current_user.is_owner():
            return None
        conn, cur = open_cursor()
        try:
            cur.execute(
                f"""
                SELECT sar.id AS request_id,
                       sar.service_type,
                       sar.status,
                       sar.admin_note,
                       sari.cancel_reason AS item_cancel_reason
                FROM sales_agency_request_items sari
                JOIN sales_agency_requests sar ON sari.request_id = sar.id
                WHERE sari.merchandise_id = {mark()}
                  AND sar.user_id = {mark()}
                  AND sar.status IN ('cancelled', 'rejected', 'deal_failed')
                ORDER BY COALESCE(sar.processed_at, sar.created_at) DESC, sar.id DESC
                LIMIT 1
                """,
                (item_id, current_user.id),
            )
            return fetch_one_dict(cur)
        finally:
            cur.close()
            conn.close()

    def inject_terminal_sales_agency_status(html, terminal_request):
        if not terminal_request or "直近の販売代行申請" in html:
            return html
        marker = "<p>この商品の詳細を確認したまま、業者卸販売・同時出品・業者オークション出品の申請ができます。</p>"
        if marker not in html:
            return html
        service_types = getattr(module, "SALES_AGENCY_SERVICE_TYPES", {})
        status_label_func = getattr(module, "get_sales_agency_status_label", None)
        service_name = service_types.get(terminal_request.get("service_type"), terminal_request.get("service_type") or "")
        if callable(status_label_func):
            status_label = status_label_func(
                terminal_request.get("status"),
                viewer="client",
                service_type=terminal_request.get("service_type"),
            )
        else:
            status_label = sales_agency_label(terminal_request.get("status"), viewer="client") or terminal_request.get("status") or ""
        terminal_status_labels = {
            "cancelled": "キャンセル済み",
            "rejected": "受付不可",
            "deal_failed": "取引不成立",
        }
        if terminal_request.get("status") in terminal_status_labels and status_label == terminal_request.get("status"):
            status_label = terminal_status_labels[terminal_request.get("status")]
        reason = (
            terminal_request.get("admin_note")
            or terminal_request.get("item_cancel_reason")
            or ""
        )
        status_class = terminal_request.get("status") or ""
        block = (
            '<p class="sales-agency-status-note">'
            "<span>直近の販売代行申請</span>"
            f'<span class="request-pill type">{escape(str(service_name))}</span>'
            f'<span class="request-pill status {escape(str(status_class))}">{escape(str(status_label))}</span>'
            "</p>"
        )
        if reason:
            block += f'<p class="detail-action-note">{escape(str(reason))}</p>'
        return html.replace(marker, block + marker, 1)

    def view_item_with_terminal_sales_agency_status(id):
        original = original_views.get("view_item")
        if not callable(original):
            return redirect(url_for("index"))
        response = make_response(original(id))
        if response.status_code != 200 or not response.content_type.startswith("text/html"):
            return response
        terminal_request = load_terminal_sales_agency_request_for_item(id)
        if not terminal_request:
            return response
        html = response.get_data(as_text=True)
        patched = inject_terminal_sales_agency_status(html, terminal_request)
        if patched != html:
            response.set_data(patched)
        return response

    if hasattr(module, "SALES_AGENCY_STATUS"):
        module.SALES_AGENCY_STATUS["deal_failed"] = "取引不成立"
        module.SALES_AGENCY_STATUS["rejected"] = "受付不可"
    if hasattr(module, "SALES_AGENCY_STATUS_CLIENT"):
        module.SALES_AGENCY_STATUS_CLIENT["deal_failed"] = "取引不成立"
        module.SALES_AGENCY_STATUS_CLIENT["rejected"] = "受付不可"

    module.fetch_sales_agency_request_details = fetch_sales_agency_request_details_v3
    module.documents = documents_v3
    module.user_invoice_view = user_invoice_view_v3
    module.user_mitsumori_list = user_mitsumori_list_v3
    module.user_mitsumori_add = user_mitsumori_add_v3
    module.user_mitsumori_view = user_mitsumori_view_v3
    module.admin_kaitori_view = admin_kaitori_view_v3
    module.admin_user_mitsumori_view = admin_user_mitsumori_view_v3
    module.sales_agency_my_requests = sales_agency_my_requests_v3
    module.view_item = view_item_with_terminal_sales_agency_status

    login_required = getattr(module, "login_required", None)
    require_login = login_required if callable(login_required) else (lambda view_func: view_func)

    app.view_functions["documents"] = require_login(documents_v3)
    app.view_functions["user_invoice_view"] = require_login(user_invoice_view_v3)
    app.view_functions["user_mitsumori_list"] = require_login(user_mitsumori_list_v3)
    app.view_functions["user_mitsumori_add"] = require_login(user_mitsumori_add_v3)
    app.view_functions["user_mitsumori_view"] = require_login(user_mitsumori_view_v3)
    app.view_functions["admin_kaitori_view"] = admin_kaitori_view_v3
    app.view_functions["admin_user_mitsumori_view"] = admin_user_mitsumori_view_v3
    app.view_functions["sales_agency_my_requests"] = require_login(sales_agency_my_requests_v3)
    if "view_item" in app.view_functions:
        app.view_functions["view_item"] = view_item_with_terminal_sales_agency_status
    if "admin_kaitori_delete" in app.view_functions:
        app.view_functions["admin_kaitori_delete"] = protected_admin_delete("admin_kaitori_delete", "invoices")
    if "admin_mitsumori_delete" in app.view_functions:
        app.view_functions["admin_mitsumori_delete"] = protected_admin_delete("admin_mitsumori_delete", "user_mitsumori")
    for edit_endpoint in (
        "admin_auction_keisan_edit",
        "admin_shikiriosho_edit",
        "admin_kaitori_edit",
        "admin_seisan_edit",
        "admin_mitsumori_edit",
    ):
        if edit_endpoint in app.view_functions:
            app.view_functions[edit_endpoint] = protected_admin_edit(edit_endpoint)

    if "admin_cancel_final_document" not in app.view_functions:
        app.add_url_rule(
            "/admin/documents/<document_kind>/<int:document_id>/cancel",
            endpoint="admin_cancel_final_document",
            view_func=admin_cancel_final_document,
            methods=["POST"],
        )
    if "admin_sales_agency_admin_cancel" not in app.view_functions:
        app.add_url_rule(
            "/admin/sales-agency-requests/<int:id>/admin-cancel",
            endpoint="admin_sales_agency_admin_cancel",
            view_func=admin_sales_agency_admin_cancel,
            methods=["POST"],
        )
    if "admin_final_document_edit" not in app.view_functions:
        app.add_url_rule(
            "/admin/documents/<document_kind>/<int:document_id>/edit",
            endpoint="admin_final_document_edit",
            view_func=admin_final_document_edit,
            methods=["GET", "POST"],
        )
    if "admin_kaitori_edit" in app.view_functions:
        def admin_kaitori_edit_final_document(id):
            return admin_final_document_edit("invoices", id)

        app.view_functions["admin_kaitori_edit"] = require_login(admin_kaitori_edit_final_document)
