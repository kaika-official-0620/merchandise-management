# -*- coding: utf-8 -*-
from __future__ import annotations

import difflib
import mimetypes
import os
import re
import shutil
import sqlite3
import subprocess
import unicodedata
from pathlib import Path
from typing import Any, Iterable

from flask import abort, flash, redirect, render_template, request, send_file, url_for
from werkzeug.utils import secure_filename


ALLOWED_VENDOR_EXTENSIONS = {"pdf", "png", "jpg", "jpeg"}


def apply(module: Any) -> None:
    if getattr(module, "_step3_vendor_workflow_patch_applied", False):
        return
    module._step3_vendor_workflow_patch_applied = True

    app = module.app
    get_db = module.get_db
    DATABASE_URL = getattr(module, "DATABASE_URL", None)
    RealDictCursor = getattr(module, "RealDictCursor", None)
    login_required = module.login_required
    admin_required = module.admin_required
    current_user = module.current_user
    get_jst_now = getattr(module, "get_jst_now")

    def mark() -> str:
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

    def column_exists(cur, table_name: str, column_name: str) -> bool:
        if DATABASE_URL:
            cur.execute(
                """
                SELECT 1
                FROM information_schema.columns
                WHERE table_name = %s AND column_name = %s
                LIMIT 1
                """,
                (table_name, column_name),
            )
            return cur.fetchone() is not None
        cur.execute(f"PRAGMA table_info({table_name})")
        for row in cur.fetchall():
            name = row["name"] if isinstance(row, sqlite3.Row) else row[1]
            if name == column_name:
                return True
        return False

    def table_exists(cur, table_name: str) -> bool:
        if DATABASE_URL:
            cur.execute(
                """
                SELECT 1
                FROM information_schema.tables
                WHERE table_name = %s
                LIMIT 1
                """,
                (table_name,),
            )
            return cur.fetchone() is not None
        cur.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?", (table_name,))
        return cur.fetchone() is not None

    def add_column_if_missing(cur, table: str, name: str, pg_def: str, sqlite_def: str) -> None:
        if not table_exists(cur, table) or column_exists(cur, table, name):
            return
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {name} {pg_def if DATABASE_URL else sqlite_def}")

    def ensure_schema() -> None:
        conn, cur = open_cursor()
        try:
            if DATABASE_URL:
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
                    CREATE TABLE IF NOT EXISTS vendor_document_item_links (
                        id SERIAL PRIMARY KEY,
                        vendor_document_id INTEGER REFERENCES vendor_documents(id) ON DELETE CASCADE,
                        request_item_id INTEGER REFERENCES sales_agency_request_items(id),
                        merchandise_id INTEGER REFERENCES merchandise(id),
                        user_id INTEGER REFERENCES users(id),
                        vendor_mitsumori_id INTEGER,
                        extracted_name TEXT,
                        match_source VARCHAR(40),
                        linked_by INTEGER REFERENCES users(id),
                        linked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
            else:
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
                    CREATE TABLE IF NOT EXISTS vendor_document_item_links (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        vendor_document_id INTEGER,
                        request_item_id INTEGER,
                        merchandise_id INTEGER,
                        user_id INTEGER,
                        vendor_mitsumori_id INTEGER,
                        extracted_name TEXT,
                        match_source TEXT,
                        linked_by INTEGER,
                        linked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )

            vendor_doc_columns = {
                "vendor_id": ("INTEGER REFERENCES vendors(id)", "INTEGER"),
                "vendor_name": ("VARCHAR(200)", "TEXT"),
                "extracted_item_name": ("VARCHAR(255)", "TEXT"),
                "extracted_text": ("TEXT", "TEXT"),
                "extraction_status": ("VARCHAR(30) DEFAULT 'pending'", "TEXT DEFAULT 'pending'"),
                "extraction_error": ("TEXT", "TEXT"),
                "match_status": ("VARCHAR(30) DEFAULT 'unlinked'", "TEXT DEFAULT 'unlinked'"),
                "linked_by": ("INTEGER REFERENCES users(id)", "INTEGER"),
                "linked_at": ("TIMESTAMP", "TIMESTAMP"),
                "vendor_amount": ("INTEGER DEFAULT 0", "INTEGER DEFAULT 0"),
                "customer_amount": ("INTEGER DEFAULT 0", "INTEGER DEFAULT 0"),
                "amount_difference": ("INTEGER DEFAULT 0", "INTEGER DEFAULT 0"),
                "difference_rate": ("NUMERIC(10,2) DEFAULT 0", "REAL DEFAULT 0"),
                "edited_by": ("INTEGER REFERENCES users(id)", "INTEGER"),
                "edited_at": ("TIMESTAMP", "TIMESTAMP"),
                "reception_number": ("VARCHAR(20)", "TEXT"),
            }
            for name, (pg_def, sqlite_def) in vendor_doc_columns.items():
                add_column_if_missing(cur, "vendor_documents", name, pg_def, sqlite_def)

            request_item_columns = {
                "item_status": ("VARCHAR(30) DEFAULT 'active'", "TEXT DEFAULT 'active'"),
                "snapshot_product_name": ("TEXT", "TEXT"),
                "snapshot_brand_name": ("TEXT", "TEXT"),
                "snapshot_model_number": ("TEXT", "TEXT"),
                "snapshot_kaika_product_code": ("TEXT", "TEXT"),
                "snapshot_photo_path": ("TEXT", "TEXT"),
                "workflow_status": ("VARCHAR(40) DEFAULT 'step1_pending'", "TEXT DEFAULT 'step1_pending'"),
                "vendor_mitsumori_id": ("INTEGER", "INTEGER"),
                "moved_to_step3_at": ("TIMESTAMP", "TEXT"),
                "vendor_document_id": ("INTEGER", "INTEGER"),
                "moved_to_step4_at": ("TIMESTAMP", "TEXT"),
                "updated_at": ("TIMESTAMP", "TEXT"),
            }
            for name, (pg_def, sqlite_def) in request_item_columns.items():
                add_column_if_missing(cur, "sales_agency_request_items", name, pg_def, sqlite_def)

            user_mitsumori_columns = {
                "vendor_id": ("INTEGER", "INTEGER"),
                "created_by_admin_id": ("INTEGER", "INTEGER"),
            }
            for name, (pg_def, sqlite_def) in user_mitsumori_columns.items():
                add_column_if_missing(cur, "user_mitsumori", name, pg_def, sqlite_def)

            cur.execute("CREATE INDEX IF NOT EXISTS idx_vendor_documents_scope_registered ON vendor_documents (document_scope, registered_at)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_vendor_documents_vendor ON vendor_documents (vendor_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_vendor_document_links_doc ON vendor_document_item_links (vendor_document_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_vendor_document_links_item ON vendor_document_item_links (request_item_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_sales_agency_request_items_step3 ON sales_agency_request_items (workflow_status, vendor_mitsumori_id)")
            conn.commit()
        finally:
            cur.close()
            conn.close()

    def safe_int(value, default: int = 0) -> int:
        try:
            if value is None or str(value).strip() == "":
                return default
            return int(float(str(value).replace(",", "")))
        except (TypeError, ValueError):
            return default

    def format_dt(value) -> str:
        if value is None:
            return "-"
        if hasattr(value, "strftime"):
            return value.strftime("%Y-%m-%d %H:%M")
        return str(value)[:16].replace("T", " ")

    def normalize_text(value: Any) -> str:
        text = unicodedata.normalize("NFKC", str(value or "")).lower()
        return re.sub(r"[\s\u3000\-_‐ー・/\\|,.，。、:：;；()（）\[\]【】]+", "", text)

    def compact_text(value: Any, max_len: int = 1200) -> str:
        text = re.sub(r"\s+", " ", str(value or "")).strip()
        return text[:max_len]

    def load_vendors() -> list[dict[str, Any]]:
        ensure_schema()
        conn, cur = open_cursor()
        try:
            cur.execute("SELECT * FROM vendors ORDER BY name, id")
            vendors = rows_to_dicts(cur.fetchall())
            for vendor in vendors:
                vendor["display_name"] = (vendor.get("name") or f"ID:{vendor.get('id')}").strip()
            return vendors
        finally:
            cur.close()
            conn.close()

    def save_vendor_upload(file_storage) -> tuple[str, str, str, int, str]:
        original_filename = secure_filename(file_storage.filename or "")
        if "." not in original_filename:
            raise ValueError("拡張子のないファイルは登録できません。")
        ext = original_filename.rsplit(".", 1)[1].lower()
        if ext not in ALLOWED_VENDOR_EXTENSIONS:
            raise ValueError("PDF / png / jpg / jpeg のみ登録できます。")
        upload_dir = Path(app.static_folder) / "uploads" / "vendor_documents"
        upload_dir.mkdir(parents=True, exist_ok=True)
        stamp = get_jst_now().strftime("%Y%m%d%H%M%S%f")
        stored_filename = f"{stamp}_{original_filename}"
        absolute_path = upload_dir / stored_filename
        file_storage.save(str(absolute_path))
        stored_path = f"uploads/vendor_documents/{stored_filename}"
        mime_type = file_storage.mimetype or mimetypes.guess_type(original_filename)[0] or "application/octet-stream"
        file_size = absolute_path.stat().st_size
        return stored_path, original_filename, mime_type, file_size, ext

    def decode_pdf_literal(value: str) -> str:
        value = value.replace(r"\(", "(").replace(r"\)", ")").replace(r"\\", "\\")
        value = re.sub(r"\\([nrtbf])", " ", value)
        return value

    def fallback_pdf_text(path: Path) -> str:
        raw = path.read_bytes()
        payload = raw.decode("latin-1", errors="ignore")
        chunks = []
        for match in re.findall(r"\((.*?)\)\s*Tj", payload, flags=re.S):
            chunks.append(decode_pdf_literal(match))
        for array_match in re.findall(r"\[(.*?)\]\s*TJ", payload, flags=re.S):
            for literal in re.findall(r"\((.*?)\)", array_match, flags=re.S):
                chunks.append(decode_pdf_literal(literal))
        return "\n".join(part.strip() for part in chunks if part.strip())

    def extract_pdf_text(path: Path) -> tuple[str, str]:
        errors = []
        try:
            from pypdf import PdfReader

            reader = PdfReader(str(path))
            pages = []
            for page in reader.pages:
                pages.append(page.extract_text() or "")
            text = "\n".join(pages).strip()
            if text:
                return text, ""
        except Exception as exc:  # pragma: no cover - depends on optional runtime package.
            errors.append(f"pypdf: {exc}")
        try:
            text = fallback_pdf_text(path).strip()
            if text:
                return text, "; ".join(errors)
        except Exception as exc:
            errors.append(f"pdf-fallback: {exc}")
        return "", "; ".join(errors)

    def extract_image_text(path: Path) -> tuple[str, str]:
        errors = []
        metadata_texts = []
        image = None
        try:
            from PIL import Image

            image = Image.open(str(path))
            for key, value in (image.info or {}).items():
                if isinstance(value, str) and value.strip():
                    metadata_texts.append(value.strip())
            try:
                exif = image.getexif()
                for tag_id in (270, 40091, 40092, 40093, 40094, 40095):
                    value = exif.get(tag_id)
                    if isinstance(value, bytes):
                        value = value.decode("utf-16le", errors="ignore") or value.decode("utf-8", errors="ignore")
                    if value:
                        metadata_texts.append(str(value).strip())
            except Exception as exc:
                errors.append(f"exif: {exc}")
        except Exception as exc:
            errors.append(f"pillow: {exc}")

        if image is not None:
            try:
                import pytesseract

                tesseract_cmd = os.environ.get("TESSERACT_CMD") or shutil.which("tesseract")
                if not tesseract_cmd:
                    project_root = Path.cwd()
                    local_cmd = project_root / ".render" / "tesseract" / "usr" / "bin" / "tesseract"
                    if local_cmd.exists():
                        tesseract_cmd = str(local_cmd)
                        local_bin = str(local_cmd.parent)
                        os.environ["PATH"] = f"{local_bin}{os.pathsep}{os.environ.get('PATH', '')}"
                        lib_paths = [
                            str(project_root / ".render" / "tesseract" / "usr" / "lib" / "x86_64-linux-gnu"),
                            str(project_root / ".render" / "tesseract" / "lib" / "x86_64-linux-gnu"),
                        ]
                        os.environ["LD_LIBRARY_PATH"] = os.pathsep.join(lib_paths + [os.environ.get("LD_LIBRARY_PATH", "")])
                    for tessdata_dir in [
                        project_root / ".render" / "tesseract" / "usr" / "share" / "tesseract-ocr" / "5" / "tessdata",
                        project_root / ".render" / "tesseract" / "usr" / "share" / "tesseract-ocr" / "4.00" / "tessdata",
                        project_root / ".render" / "tesseract" / "usr" / "share" / "tessdata",
                    ]:
                        if tessdata_dir.exists() and not os.environ.get("TESSDATA_PREFIX"):
                            os.environ["TESSDATA_PREFIX"] = str(tessdata_dir)
                            break
                if tesseract_cmd:
                    pytesseract.pytesseract.tesseract_cmd = str(tesseract_cmd)
                ocr_text = pytesseract.image_to_string(image, lang="jpn+eng").strip()
                if ocr_text:
                    return "\n".join(metadata_texts + [ocr_text]).strip(), "; ".join(errors)
            except Exception as exc:  # pragma: no cover - tesseract binary availability varies.
                errors.append(f"tesseract: {exc}")

        if metadata_texts:
            return "\n".join(metadata_texts), "; ".join(errors)
        return "", "; ".join(errors)

    def extract_vendor_document_text(path: Path, ext: str, manual_text: str = "") -> tuple[str, str, str]:
        text = ""
        error = ""
        if ext == "pdf":
            text, error = extract_pdf_text(path)
        else:
            text, error = extract_image_text(path)
        if manual_text.strip():
            text = "\n".join(part for part in [text, manual_text.strip()] if part)
        if text.strip():
            return text.strip(), "extracted", error
        return "", "empty", error or "text_not_found"

    def fetch_step_items(status: str) -> list[dict[str, Any]]:
        ensure_schema()
        conn, cur = open_cursor()
        try:
            cur.execute(
                f"""
                SELECT
                    sari.id AS request_item_id,
                    sari.request_id,
                    sari.merchandise_id,
                    sari.vendor_mitsumori_id,
                    sari.vendor_document_id,
                    sari.updated_at,
                    sari.moved_to_step4_at,
                    COALESCE(m.id, sari.merchandise_id) AS actual_merchandise_id,
                    COALESCE(m.product_name, sari.snapshot_product_name, '') AS product_name,
                    COALESCE(m.brand_name, sari.snapshot_brand_name, '') AS brand_name,
                    COALESCE(m.model_number, sari.snapshot_model_number, '') AS model_number,
                    COALESCE(m.kaika_product_code, sari.snapshot_kaika_product_code, '') AS kaika_product_code,
                    COALESCE(m.photo_path, sari.snapshot_photo_path, '') AS photo_path,
                    COALESCE(m.item_condition, '') AS item_condition,
                    sar.service_type,
                    sar.created_at AS requested_at,
                    u.id AS user_id,
                    u.display_name AS user_display_name,
                    u.username,
                    um.document_no AS vendor_document_no,
                    um.id AS vendor_estimate_id,
                    v.id AS vendor_id,
                    COALESCE(v.name, um.company_name, '') AS vendor_name,
                    vd.title AS linked_vendor_doc_title,
                    vd.original_filename AS linked_vendor_filename
                FROM sales_agency_request_items sari
                JOIN sales_agency_requests sar ON sari.request_id = sar.id
                JOIN users u ON sar.user_id = u.id
                LEFT JOIN merchandise m ON sari.merchandise_id = m.id
                LEFT JOIN user_mitsumori um ON sari.vendor_mitsumori_id = um.id
                LEFT JOIN vendors v ON um.vendor_id = v.id
                LEFT JOIN vendor_documents vd ON sari.vendor_document_id = vd.id
                WHERE COALESCE(NULLIF(sari.workflow_status, ''), 'step1_pending') = {mark()}
                  AND COALESCE(sari.item_status, 'active') NOT IN ('cancelled', 'canceled')
                ORDER BY COALESCE(sari.updated_at, sar.created_at) DESC, sari.id DESC
                """,
                (status,),
            )
            rows = rows_to_dicts(cur.fetchall())
            for row in rows:
                user_name = row.get("user_display_name") or row.get("username") or f"ID:{row.get('user_id')}"
                row["user_name"] = user_name
                row["service_name"] = getattr(module, "get_sales_agency_service_name", lambda key: key)(row.get("service_type"))
                row["estimate_label"] = row.get("vendor_document_no") or f"見積ID {row.get('vendor_estimate_id') or '-'}"
                row["vendor_name"] = row.get("vendor_name") or "未設定"
                row["photo_path"] = str(row.get("photo_path") or "").replace("\\", "/").lstrip("/")
                row["updated_label"] = format_dt(row.get("updated_at") or row.get("requested_at"))
            return rows
        finally:
            cur.close()
            conn.close()

    def item_similarity_score(doc: dict[str, Any], item: dict[str, Any], manual_query: str = "") -> tuple[int, list[str]]:
        source = " ".join(
            part
            for part in [
                manual_query,
                doc.get("extracted_text"),
                doc.get("extracted_item_name"),
                doc.get("title"),
                doc.get("original_filename"),
            ]
            if part
        )
        source_norm = normalize_text(source)
        product_norm = normalize_text(item.get("product_name"))
        brand_norm = normalize_text(item.get("brand_name"))
        model_norm = normalize_text(item.get("model_number"))
        reasons = []
        score = 0

        if source_norm and product_norm:
            if product_norm in source_norm:
                score += 100
                reasons.append("商品名を含む")
            elif source_norm in product_norm:
                score += 75
                reasons.append("商品名の部分一致")
            else:
                ratio = difflib.SequenceMatcher(None, product_norm, source_norm).ratio()
                ratio_score = int(ratio * 60)
                if ratio_score >= 18:
                    score += ratio_score
                    reasons.append("類似候補")
        if source_norm and brand_norm and brand_norm in source_norm:
            score += 24
            reasons.append("ブランド一致")
        if source_norm and model_norm and model_norm in source_norm:
            score += 18
            reasons.append("型番一致")
        if doc.get("vendor_id") and item.get("vendor_id") and int(doc.get("vendor_id")) == int(item.get("vendor_id")):
            score += 15
            reasons.append("業者一致")
        if manual_query and score == 0:
            search_target = normalize_text(" ".join([
                item.get("user_name"),
                str(item.get("user_id") or ""),
                item.get("product_name"),
                item.get("brand_name"),
                item.get("vendor_name"),
                item.get("estimate_label"),
            ]))
            if normalize_text(manual_query) in search_target:
                score += 55
                reasons.append("手動検索一致")
        return score, reasons

    def build_candidates(doc: dict[str, Any], pending_items: list[dict[str, Any]], manual_query: str = "") -> list[dict[str, Any]]:
        candidates = []
        has_source_text = bool((manual_query or doc.get("extracted_text") or doc.get("extracted_item_name") or "").strip())
        for item in pending_items:
            candidate = dict(item)
            score, reasons = item_similarity_score(doc, item, manual_query)
            candidate["match_score"] = score
            candidate["match_reasons"] = " / ".join(reasons) if reasons else ("手動選択可" if not has_source_text else "低一致")
            if score > 0 or not has_source_text:
                candidates.append(candidate)
        candidates.sort(
            key=lambda row: (
                int(row.get("match_score") or 0),
                1 if doc.get("vendor_id") and row.get("vendor_id") == doc.get("vendor_id") else 0,
                str(row.get("updated_at") or ""),
            ),
            reverse=True,
        )
        return candidates[:60]

    def load_linked_items(document_id: int) -> list[dict[str, Any]]:
        conn, cur = open_cursor()
        try:
            cur.execute(
                f"""
                SELECT l.*,
                       COALESCE(m.product_name, sari.snapshot_product_name, '') AS product_name,
                       COALESCE(m.brand_name, sari.snapshot_brand_name, '') AS brand_name,
                       COALESCE(m.photo_path, sari.snapshot_photo_path, '') AS photo_path,
                       u.display_name AS user_display_name,
                       u.username,
                       um.document_no AS vendor_document_no,
                       COALESCE(v.name, um.company_name, '') AS vendor_name
                FROM vendor_document_item_links l
                JOIN sales_agency_request_items sari ON l.request_item_id = sari.id
                JOIN users u ON l.user_id = u.id
                LEFT JOIN merchandise m ON l.merchandise_id = m.id
                LEFT JOIN user_mitsumori um ON l.vendor_mitsumori_id = um.id
                LEFT JOIN vendors v ON um.vendor_id = v.id
                WHERE l.vendor_document_id = {mark()}
                ORDER BY l.linked_at DESC, l.id DESC
                """,
                (document_id,),
            )
            rows = rows_to_dicts(cur.fetchall())
            for row in rows:
                row["user_name"] = row.get("user_display_name") or row.get("username") or f"ID:{row.get('user_id')}"
                row["linked_label"] = format_dt(row.get("linked_at"))
                row["estimate_label"] = row.get("vendor_document_no") or f"見積ID {row.get('vendor_mitsumori_id') or '-'}"
                row["photo_path"] = str(row.get("photo_path") or "").replace("\\", "/").lstrip("/")
            return rows
        finally:
            cur.close()
            conn.close()

    def fetch_vendor_documents(limit: int = 100) -> list[dict[str, Any]]:
        ensure_schema()
        conn, cur = open_cursor()
        try:
            cur.execute(
                f"""
                SELECT vd.*,
                       v.name AS master_vendor_name,
                       creator.display_name AS creator_display_name,
                       creator.username AS creator_username
                FROM vendor_documents vd
                LEFT JOIN vendors v ON vd.vendor_id = v.id
                LEFT JOIN users creator ON vd.created_by = creator.id
                WHERE COALESCE(vd.document_scope, 'user_flow') = 'user_flow'
                ORDER BY vd.registered_at DESC, vd.id DESC
                LIMIT {int(limit)}
                """
            )
            docs = rows_to_dicts(cur.fetchall())
            for doc in docs:
                doc["vendor_display_name"] = doc.get("master_vendor_name") or doc.get("vendor_name") or "未設定"
                doc["registered_label"] = format_dt(doc.get("registered_at"))
                doc["status_label"] = {
                    "received": "受領済み",
                    "registered": "登録済み",
                    "shared": "共有済み",
                    "sent": "送信済み",
                }.get(doc.get("status"), doc.get("status") or "受領済み")
                doc["match_status_label"] = {
                    "unlinked": "未紐づけ",
                    "partial": "一部紐づけ",
                    "linked": "紐づけ済み",
                }.get(doc.get("match_status"), doc.get("match_status") or "未紐づけ")
                doc["extraction_status_label"] = {
                    "extracted": "抽出済み",
                    "empty": "抽出テキストなし",
                    "error": "抽出エラー",
                    "pending": "未抽出",
                }.get(doc.get("extraction_status"), doc.get("extraction_status") or "未抽出")
                doc["extracted_preview"] = compact_text(doc.get("extracted_text"), 900)
                doc["download_url"] = url_for("admin_vendor_document_download", document_id=doc["id"])
                doc["delete_url"] = url_for("admin_vendor_document_delete", document_id=doc["id"])
                doc["link_url"] = url_for("admin_vendor_document_link_items", document_id=doc["id"])
                doc["linked_items"] = load_linked_items(doc["id"])
            return docs
        finally:
            cur.close()
            conn.close()

    def build_step3_context():
        manual_query = (request.args.get("q") or "").strip()
        focus_doc_id = request.args.get("vendor_doc_id", type=int) or request.args.get("focus_doc_id", type=int)
        pending_items = fetch_step_items("step3_vendor_wait")
        step4_items = fetch_step_items("step4_ready")
        docs = fetch_vendor_documents()
        for doc in docs:
            doc["is_focused"] = bool(focus_doc_id and int(doc.get("id") or 0) == int(focus_doc_id))
            doc["manual_query"] = manual_query if doc["is_focused"] or not focus_doc_id else ""
            doc["candidates"] = build_candidates(doc, pending_items, doc["manual_query"])
        if focus_doc_id:
            docs.sort(key=lambda doc: 0 if int(doc.get("id") or 0) == int(focus_doc_id) else 1)
        return {
            "vendors": load_vendors(),
            "documents": docs,
            "pending_items": pending_items,
            "step4_items": step4_items,
            "manual_query": manual_query,
            "focus_doc_id": focus_doc_id,
            "allowed_extensions": ", ".join(sorted(ALLOWED_VENDOR_EXTENSIONS)),
        }

    def admin_vendor_documents():
        ensure_schema()
        selected_scope = (request.values.get("scope") or "user_flow").strip()
        if selected_scope == "kaika":
            previous = getattr(module, "_step3_previous_admin_vendor_documents", None)
            if callable(previous) and previous is not admin_vendor_documents:
                return previous()

        if request.method == "POST":
            try:
                vendor_id = request.form.get("vendor_id", type=int)
                title = (request.form.get("title") or "").strip()
                notes = (request.form.get("notes") or "").strip()
                manual_text = (request.form.get("manual_text") or "").strip()
                status = (request.form.get("status") or "received").strip()
                file_storage = request.files.get("file")
                vendors = load_vendors()
                vendor_row = next((vendor for vendor in vendors if int(vendor.get("id") or 0) == int(vendor_id or 0)), None)
                if not vendor_row:
                    raise ValueError("流し先業者を1社選択してください。")
                if not file_storage or not file_storage.filename:
                    raise ValueError("登録するPDF/png/jpg/jpegを選択してください。")
                stored_path, original_filename, mime_type, file_size, ext = save_vendor_upload(file_storage)
                absolute_path = Path(app.static_folder) / stored_path
                extracted_text, extraction_status, extraction_error = extract_vendor_document_text(absolute_path, ext, manual_text)
                first_line = next((line.strip() for line in extracted_text.splitlines() if line.strip()), "")
                now_value = get_jst_now()
                conn, cur = open_cursor()
                try:
                    cur.execute(
                        f"""
                        INSERT INTO vendor_documents
                        (document_scope, vendor_id, vendor_name, title, original_filename, stored_path,
                         mime_type, file_size, status, notes, extracted_item_name, extracted_text,
                         extraction_status, extraction_error, match_status, created_by, registered_at,
                         created_at, updated_at)
                        VALUES ({mark()}, {mark()}, {mark()}, {mark()}, {mark()}, {mark()},
                                {mark()}, {mark()}, {mark()}, {mark()}, {mark()}, {mark()},
                                {mark()}, {mark()}, {mark()}, {mark()}, {mark()}, {mark()}, {mark()})
                        """
                        + (" RETURNING id" if DATABASE_URL else ""),
                        (
                            "user_flow",
                            vendor_id,
                            vendor_row.get("name"),
                            title or original_filename,
                            original_filename,
                            stored_path,
                            mime_type,
                            file_size,
                            status,
                            notes,
                            first_line[:255],
                            extracted_text,
                            extraction_status,
                            extraction_error,
                            "unlinked",
                            getattr(current_user, "id", None),
                            now_value,
                            now_value,
                            now_value,
                        ),
                    )
                    document_id = row_to_dict(cur.fetchone()).get("id") if DATABASE_URL else cur.lastrowid
                    conn.commit()
                finally:
                    cur.close()
                    conn.close()
                if extraction_status == "extracted":
                    flash("業者書類を登録し、テキスト抽出を行いました。候補を確認して紐づけてください。", "success")
                else:
                    flash("業者書類を登録しました。抽出テキストがないため、手動検索で紐づけてください。", "warning")
                return redirect(url_for("admin_documents_dashboard", group="vendor_incoming", vendor_doc_id=document_id))
            except Exception as exc:
                flash(str(exc), "error")

        return render_template("admin/vendor_documents_step3.html", **build_step3_context())

    def admin_vendor_document_link_items(document_id: int):
        ensure_schema()
        selected_ids = [safe_int(value) for value in request.form.getlist("request_item_ids")]
        selected_ids = [value for value in selected_ids if value > 0]
        if not selected_ids:
            flash("紐づける商品候補を選択してください。", "error")
            return redirect(url_for("admin_documents_dashboard", group="vendor_incoming", vendor_doc_id=document_id))

        pending_items = fetch_step_items("step3_vendor_wait")
        pending_map = {int(item["request_item_id"]): item for item in pending_items}
        valid_items = [pending_map[item_id] for item_id in selected_ids if item_id in pending_map]
        if not valid_items:
            flash("選択した商品はステップ3の業者書類待ちにありません。", "error")
            return redirect(url_for("admin_documents_dashboard", group="vendor_incoming", vendor_doc_id=document_id))

        now_value = get_jst_now()
        conn, cur = open_cursor()
        try:
            cur.execute(f"SELECT id FROM vendor_documents WHERE id = {mark()}", (document_id,))
            if not cur.fetchone():
                flash("対象の業者書類が見つかりません。", "error")
                return redirect(url_for("admin_documents_dashboard", group="vendor_incoming"))
            for item in valid_items:
                cur.execute(
                    f"""
                    INSERT INTO vendor_document_item_links
                    (vendor_document_id, request_item_id, merchandise_id, user_id, vendor_mitsumori_id,
                     extracted_name, match_source, linked_by, linked_at)
                    VALUES ({mark()}, {mark()}, {mark()}, {mark()}, {mark()}, {mark()}, {mark()}, {mark()}, {mark()})
                    """,
                    (
                        document_id,
                        item.get("request_item_id"),
                        item.get("actual_merchandise_id") or item.get("merchandise_id"),
                        item.get("user_id"),
                        item.get("vendor_mitsumori_id"),
                        item.get("product_name"),
                        "admin_confirmed",
                        getattr(current_user, "id", None),
                        now_value,
                    ),
                )
                cur.execute(
                    f"""
                    UPDATE sales_agency_request_items
                    SET workflow_status = {mark()},
                        vendor_document_id = {mark()},
                        moved_to_step4_at = {mark()},
                        updated_at = {mark()}
                    WHERE id = {mark()}
                    """,
                    ("step4_ready", document_id, now_value, now_value, item.get("request_item_id")),
                )
            cur.execute(
                f"""
                UPDATE vendor_documents
                SET match_status = {mark()},
                    linked_by = {mark()},
                    linked_at = {mark()},
                    updated_at = {mark()}
                WHERE id = {mark()}
                """,
                ("linked", getattr(current_user, "id", None), now_value, now_value, document_id),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.close()
            conn.close()
        flash(f"{len(valid_items)}点を業者書類に紐づけ、ステップ4へ移動しました。", "success")
        return redirect(url_for("admin_documents_dashboard", group="vendor_incoming", vendor_doc_id=document_id))

    def admin_vendor_document_download(document_id: int):
        ensure_schema()
        conn, cur = open_cursor()
        try:
            cur.execute(f"SELECT * FROM vendor_documents WHERE id = {mark()}", (document_id,))
            doc = row_to_dict(cur.fetchone())
        finally:
            cur.close()
            conn.close()
        if not doc:
            abort(404)
        stored_path = str(doc.get("stored_path") or "").replace("\\", "/").lstrip("/")
        if stored_path.startswith("static/"):
            stored_path = stored_path[len("static/") :]
        absolute_path = Path(app.static_folder) / stored_path
        static_root = Path(app.static_folder).resolve()
        try:
            resolved = absolute_path.resolve()
        except FileNotFoundError:
            abort(404)
        if static_root not in resolved.parents and resolved != static_root:
            abort(404)
        if not resolved.exists():
            abort(404)
        return send_file(str(resolved), as_attachment=True, download_name=doc.get("original_filename") or resolved.name)

    def admin_vendor_document_delete(document_id: int):
        ensure_schema()
        conn, cur = open_cursor()
        doc = None
        linked_ids = []
        try:
            cur.execute(f"SELECT * FROM vendor_documents WHERE id = {mark()}", (document_id,))
            doc = row_to_dict(cur.fetchone())
            if not doc:
                flash("削除対象の業者書類が見つかりません。", "error")
                return redirect(url_for("admin_documents_dashboard", group="vendor_incoming"))
            cur.execute(f"SELECT request_item_id FROM vendor_document_item_links WHERE vendor_document_id = {mark()}", (document_id,))
            linked_ids = [row_to_dict(row).get("request_item_id") for row in cur.fetchall()]
            if linked_ids:
                placeholders = ", ".join([mark()] * len(linked_ids))
                cur.execute(
                    f"""
                    UPDATE sales_agency_request_items
                    SET workflow_status = {mark()},
                        vendor_document_id = NULL,
                        moved_to_step4_at = NULL,
                        updated_at = {mark()}
                    WHERE id IN ({placeholders})
                      AND COALESCE(workflow_status, '') = {mark()}
                    """,
                    tuple(["step3_vendor_wait", get_jst_now()] + linked_ids + ["step4_ready"]),
                )
            cur.execute(f"DELETE FROM vendor_document_item_links WHERE vendor_document_id = {mark()}", (document_id,))
            cur.execute(f"DELETE FROM vendor_documents WHERE id = {mark()}", (document_id,))
            conn.commit()
        finally:
            cur.close()
            conn.close()

        stored_path = str(doc.get("stored_path") or "").replace("\\", "/").lstrip("/") if doc else ""
        if stored_path.startswith("static/"):
            stored_path = stored_path[len("static/") :]
        absolute_path = Path(app.static_folder) / stored_path
        try:
            resolved = absolute_path.resolve()
            static_root = Path(app.static_folder).resolve()
            if (static_root in resolved.parents or resolved == static_root) and resolved.exists():
                resolved.unlink()
        except OSError:
            flash("書類データは削除しましたが、ファイル削除は後処理が必要です。", "warning")
            return redirect(url_for("admin_documents_dashboard", group="vendor_incoming"))
        flash("業者書類を削除しました。紐づけ済み商品はステップ3へ戻しました。", "success")
        return redirect(url_for("admin_documents_dashboard", group="vendor_incoming"))

    def register(endpoint: str, rule: str, view_func, methods: list[str]) -> None:
        wrapped = login_required(admin_required(view_func))
        if endpoint in app.view_functions:
            if endpoint == "admin_vendor_documents":
                previous = app.view_functions.get(endpoint)
                if previous is not wrapped:
                    module._step3_previous_admin_vendor_documents = previous
            app.view_functions[endpoint] = wrapped
            return
        app.add_url_rule(rule, endpoint=endpoint, view_func=wrapped, methods=methods)

    ensure_schema()
    register("admin_vendor_documents", "/admin/vendor-documents", admin_vendor_documents, ["GET", "POST"])
    register("admin_vendor_document_link_items", "/admin/vendor-documents/<int:document_id>/link-items", admin_vendor_document_link_items, ["POST"])
    register("admin_vendor_document_download", "/admin/vendor-documents/<int:document_id>/download", admin_vendor_document_download, ["GET"])
    register("admin_vendor_document_delete", "/admin/vendor-documents/<int:document_id>/delete", admin_vendor_document_delete, ["POST"])

    module.admin_vendor_documents = admin_vendor_documents
    module.admin_vendor_document_link_items = admin_vendor_document_link_items
    module.fetch_step3_vendor_pending_items = lambda: fetch_step_items("step3_vendor_wait")
    module.fetch_step4_ready_items = lambda: fetch_step_items("step4_ready")
