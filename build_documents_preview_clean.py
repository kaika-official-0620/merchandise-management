from __future__ import annotations

from datetime import date, datetime
from html import escape
import json
from pathlib import Path
import shutil
import sqlite3
from textwrap import dedent


BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / ".preview-site-documents-clean"
STATIC_DIR = OUTPUT_DIR / "static"
PREVIEW_SOURCE_STATIC_DIR = BASE_DIR / ".preview-site-docs" / "static"


SERVICE_LABELS = {
    "all": "すべてのサービス",
    "wholesale": "業者卸販売",
    "auction": "業者オークション",
    "simultaneous": "同時出品",
    "proxy_purchase": "代行仕入れ",
}


CLIENTS = {
    "yamada": {"name": "山田 太郎", "client_no": "C-041", "request_id": "REQ-2026-041", "received_at": "2026/04/20"},
    "sato": {"name": "佐藤 花子", "client_no": "C-039", "request_id": "REQ-2026-039", "received_at": "2026/04/18"},
    "suzuki": {"name": "鈴木 一郎", "client_no": "C-038", "request_id": "REQ-2026-038", "received_at": "2026/04/17"},
    "takahashi": {"name": "高橋 愛", "client_no": "C-037", "request_id": "REQ-2026-037", "received_at": "2026/04/16"},
}


PRODUCTS = {
    "lv_alma": {
        "client": "yamada",
        "name": "ルイヴィトン アルマ BB",
        "brand": "Louis Vuitton",
        "service": "wholesale",
        "status": "査定中",
        "image": "static/img_lv_alma.svg",
        "category": "バッグ",
        "code": "KAIKA-101",
        "condition": "A",
        "detail_page": "documents_v2_product_detail_lv_alma.html",
        "request_detail": "documents_v2_request_detail_yamada.html",
        "model": "アルマ BB",
    },
    "chanel_wallet": {
        "client": "yamada",
        "name": "シャネル マトラッセ 長財布",
        "brand": "CHANEL",
        "service": "wholesale",
        "status": "認証待ち",
        "image": "static/img_chanel_wallet.svg",
        "category": "財布",
        "code": "KAIKA-102",
        "condition": "A",
        "proxyPurchase": True,
        "detail_page": "documents_v2_product_detail_chanel_wallet.html",
        "request_detail": "documents_v2_request_detail_yamada.html",
        "model": "マトラッセ 長財布",
    },
    "hermes_scarf": {
        "client": "sato",
        "name": "エルメス カレ90 スカーフ",
        "brand": "HERMES",
        "service": "wholesale",
        "status": "査定中",
        "image": "static/img_hermes_scarf.svg",
        "category": "スカーフ",
        "code": "KAIKA-201",
        "condition": "A",
        "detail_page": "documents_v2_product_detail_hermes_scarf.html",
        "request_detail": "documents_v2_request_detail_sato.html",
        "model": "カレ90",
    },
    "rolex_dj": {
        "client": "suzuki",
        "name": "ロレックス デイトジャスト 36",
        "brand": "ROLEX",
        "service": "auction",
        "status": "認証済み",
        "image": "static/img_rolex_dj.svg",
        "category": "時計",
        "code": "KAIKA-301",
        "condition": "A",
        "detail_page": "documents_v2_product_detail_rolex.html",
        "request_detail": "documents_v2_request_detail_suzuki.html",
        "model": "デイトジャスト 36",
    },
    "celine_luggage": {
        "client": "takahashi",
        "name": "セリーヌ ラゲージ ナノ",
        "brand": "CELINE",
        "service": "simultaneous",
        "status": "入金待ち",
        "image": "static/img_celine_luggage.svg",
        "category": "バッグ",
        "code": "KAIKA-401",
        "condition": "A",
        "detail_page": "documents_v2_product_detail_celine_luggage.html",
        "request_detail": "documents_v2_request_detail_takahashi.html",
        "model": "ラゲージ ナノ",
    },
}


RESPONSE_FILES = [
    {
        "slug": "documents_v2_vendor_response_file_yamada_alma.html",
        "download": "static/downloads/vendor_response_yamada_alma.txt",
        "label": "2026/04/08 ブランドセンター 回答書 No.041",
        "date": "2026-04-08",
        "month": "2026/04",
        "partner": "ブランドセンター",
        "service": "wholesale",
        "items": [
            {"product": "lv_alma", "price": 185000, "result": "成約", "assigned_client": "山田 太郎"},
        ],
    },
    {
        "slug": "documents_v2_vendor_response_file_yamada_wallet.html",
        "download": "static/downloads/vendor_response_yamada_wallet.txt",
        "label": "2026/04/21 Luxe Gate 回答書 No.053",
        "date": "2026-04-21",
        "month": "2026/04",
        "partner": "Luxe Gate",
        "service": "wholesale",
        "items": [
            {"product": "chanel_wallet", "price": 72000, "result": "成約", "assigned_client": "山田 太郎"},
        ],
    },
    {
        "slug": "documents_v2_vendor_response_file_sato_scarf.html",
        "download": "static/downloads/vendor_response_sato_scarf.txt",
        "label": "2026/04/15 ブランド市場 回答書 No.052",
        "date": "2026-04-15",
        "month": "2026/04",
        "partner": "ブランド市場",
        "service": "wholesale",
        "items": [
            {"product": "hermes_scarf", "price": 58000, "result": "成約", "assigned_client": "佐藤 花子"},
        ],
    },
    {
        "slug": "documents_v2_auction_response_file_suzuki_rolex.html",
        "download": "static/downloads/auction_response_suzuki_rolex.txt",
        "label": "2026/04/19 オークション市場 結果書 No.088",
        "date": "2026-04-19",
        "month": "2026/04",
        "partner": "オークション市場",
        "service": "auction",
        "items": [
            {"product": "rolex_dj", "price": 710000, "result": "落札", "assigned_client": "鈴木 一郎"},
        ],
    },
    {
        "slug": "documents_v2_simultaneous_response_file_takahashi_celine.html",
        "download": "static/downloads/simultaneous_response_takahashi_celine.txt",
        "label": "2026/04/20 同時出品 売却報告 No.014",
        "date": "2026-04-20",
        "month": "2026/04",
        "partner": "同時出品管理",
        "service": "simultaneous",
        "items": [
            {"product": "celine_luggage", "price": 148000, "result": "売却完了", "assigned_client": "高橋 愛"},
        ],
    },
]


RETURN_GROUPS = {
    "yamada": {
        "slug": "documents_v2_client_delivery_yamada.html",
        "template": "documents_v2_client_statement_template_yamada.html",
        "items": [
            {"product": "lv_alma", "price": 185000, "source": "ブランドセンター 回答書 No.041"},
            {"product": "chanel_wallet", "price": 72000, "source": "Luxe Gate 回答書 No.053"},
        ],
    },
    "sato": {
        "slug": "documents_v2_client_delivery_sato.html",
        "template": "documents_v2_client_statement_template_sato.html",
        "items": [
            {"product": "hermes_scarf", "price": 58000, "source": "ブランド市場 回答書 No.052"},
        ],
    },
    "suzuki": {
        "slug": "documents_v2_client_delivery_suzuki.html",
        "template": "documents_v2_client_statement_template_suzuki.html",
        "items": [
            {"product": "rolex_dj", "price": 710000, "source": "業者オークション 落札結果"},
        ],
    },
    "takahashi": {
        "slug": "documents_v2_client_delivery_takahashi.html",
        "template": "documents_v2_client_statement_template_takahashi.html",
        "items": [
            {"product": "celine_luggage", "price": 148000, "source": "同時出品 売却完了"},
        ],
    },
}


VENDORS = [
    {"name": "ブランドセンター", "mail": "center@example.jp", "phone": "03-1111-2222", "memo": "ルイヴィトン / シャネル中心"},
    {"name": "ブランド市場", "mail": "market@example.jp", "phone": "03-3333-4444", "memo": "スカーフ / 小物に強い"},
    {"name": "Luxe Gate", "mail": "luxegate@example.jp", "phone": "03-5555-6666", "memo": "財布 / バッグを少量で対応"},
]


TODAY = date(2026, 4, 24)


DEFAULT_MONTHLY_PLAN_SETTINGS = {
    "monthly_plan_20": "ライト",
    "monthly_fee_20": 2980,
    "monthly_plan_50": "スタンダード",
    "monthly_fee_50": 5980,
    "monthly_plan_100": "プロ",
    "monthly_fee_100": 9800,
    "monthly_plan_300": "ビジネス",
    "monthly_fee_300": 19800,
    "monthly_plan_over": "エンタープライズ",
    "monthly_fee_over": 0,
}


def load_monthly_plan_settings() -> dict:
    settings = dict(DEFAULT_MONTHLY_PLAN_SETTINGS)
    db_path = BASE_DIR / "merchandise.db"
    if not db_path.exists():
        return settings

    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT setting_key, setting_value
            FROM master_fee_settings
            WHERE setting_key LIKE 'monthly_%'
            """
        ).fetchall()
        conn.close()
    except Exception:
        return settings

    for row in rows:
        key = row["setting_key"]
        value = row["setting_value"]
        if key not in settings or value in (None, ""):
            continue
        if key.startswith("monthly_fee_"):
            try:
                settings[key] = int(value)
            except (TypeError, ValueError):
                continue
        else:
            settings[key] = str(value)
    return settings


MONTHLY_PLAN_SETTINGS = load_monthly_plan_settings()


def monthly_plan_for_count(item_count: int) -> dict:
    count = int(item_count or 0)
    if count <= 20:
        suffix, range_label = "20", "0〜20商品"
    elif count <= 50:
        suffix, range_label = "50", "21〜50商品"
    elif count <= 100:
        suffix, range_label = "100", "51〜100商品"
    elif count <= 300:
        suffix, range_label = "300", "101〜300商品"
    else:
        suffix, range_label = "over", "301商品以上"

    fee = int(MONTHLY_PLAN_SETTINGS.get(f"monthly_fee_{suffix}") or 0)
    plan = str(
        MONTHLY_PLAN_SETTINGS.get(f"monthly_plan_{suffix}")
        or DEFAULT_MONTHLY_PLAN_SETTINGS.get(f"monthly_plan_{suffix}")
        or "月額プラン"
    )
    return {
        "plan": plan,
        "monthly_fee": fee,
        "range": range_label,
        "item_count": count,
        "is_custom": suffix == "over" and fee <= 0,
    }


def _safe_slug(value: str) -> str:
    return "".join(ch.lower() if ch.isalnum() else "_" for ch in value).strip("_")


def _fallback_svg(brand_name: str | None, service: str) -> str:
    brand = (brand_name or "").lower()
    if "louis vuitton" in brand:
        return "static/img_lv_alma.svg"
    if "chanel" in brand:
        return "static/img_chanel_wallet.svg"
    if "hermes" in brand:
        return "static/img_hermes_scarf.svg"
    if "rolex" in brand:
        return "static/img_rolex_dj.svg"
    if "celine" in brand or "balenciaga" in brand or "prada" in brand or "gucci" in brand or "loewe" in brand or "fendi" in brand:
        return "static/img_celine_luggage.svg"
    if service == "auction":
        return "static/img_rolex_dj.svg"
    if service == "simultaneous":
        return "static/img_celine_luggage.svg"
    return "static/img_lv_alma.svg"


def _category_from_name(product_name: str) -> str:
    text = product_name or ""
    if any(word in text for word in ["財布", "ウォレット"]):
        return "財布"
    if any(word in text for word in ["ショルダー", "トート", "ハンドバッグ", "バッグ"]):
        return "バッグ"
    if "スカーフ" in text:
        return "スカーフ"
    if any(word in text for word in ["時計", "ロレックス"]):
        return "時計"
    return "バッグ"


def _service_for_item(merchandise_id: int) -> str:
    if merchandise_id in {145, 146, 151}:
        return "wholesale"
    if merchandise_id == 147:
        return "auction"
    return "simultaneous"


def _status_for_item(merchandise_id: int, service: str) -> str:
    if service == "wholesale":
        return "査定中" if merchandise_id in {145, 151} else "認証済み"
    if service == "auction":
        return "査定中"
    if service == "simultaneous":
        return "出品中"
    return "認証待ち"


def _price_for_item(merchandise_id: int, fallback: int) -> int:
    prices = {
        145: 185000,
        146: 72000,
        147: 710000,
        148: 148000,
        149: 128000,
        151: 58000,
    }
    return prices.get(merchandise_id, fallback)


def _copy_preview_image(photo_path: str | None, service: str, brand_name: str | None) -> str:
    if photo_path:
        normalized = photo_path.replace("\\", "/").lstrip("/")
        source = PREVIEW_SOURCE_STATIC_DIR / normalized
        if source.exists():
            destination = STATIC_DIR / normalized
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            return f"static/{normalized}"
    return _fallback_svg(brand_name, service)


def _load_preview_data():
    db_path = BASE_DIR / "merchandise.db"
    if not db_path.exists():
        return CLIENTS, PRODUCTS, RESPONSE_FILES, RETURN_GROUPS

    preferred_ids = [145, 146, 151, 147, 148, 149]
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    user_row = cur.execute(
        """
        SELECT id, COALESCE(NULLIF(display_name, ''), NULLIF(username, ''), 'テストユーザー') AS name
        FROM users
        WHERE id = 18
        """
    ).fetchone()

    if not user_row:
        conn.close()
        return CLIENTS, PRODUCTS, RESPONSE_FILES, RETURN_GROUPS

    placeholders = ",".join("?" for _ in preferred_ids)
    rows = cur.execute(
        f"""
        SELECT id, user_id, product_name, brand_name, item_condition, photo_path, purchase_price
               , purchase_date, model_number, notes, show_in_proxy_service, sale_type
        FROM merchandise
        WHERE user_id = ? AND id IN ({placeholders})
        ORDER BY CASE id
            WHEN 145 THEN 1
            WHEN 146 THEN 2
            WHEN 151 THEN 3
            WHEN 147 THEN 4
            WHEN 148 THEN 5
            WHEN 149 THEN 6
            ELSE 99
        END
        """,
        (user_row["id"], *preferred_ids),
    ).fetchall()

    if not rows:
        rows = cur.execute(
            """
            SELECT id, user_id, product_name, brand_name, item_condition, photo_path, purchase_price
                   , purchase_date, model_number, notes, show_in_proxy_service, sale_type
            FROM merchandise
            WHERE user_id = ? AND COALESCE(photo_path, '') != ''
            ORDER BY id DESC
            LIMIT 6
            """,
            (user_row["id"],),
        ).fetchall()

    conn.close()

    if not rows:
        return CLIENTS, PRODUCTS, RESPONSE_FILES, RETURN_GROUPS

    client_key = "test_client"
    clients = {
        client_key: {
            "name": user_row["name"],
            "client_no": f"C-{user_row['id']:03d}",
            "request_id": f"REQ-TEST-{user_row['id']:03d}",
            "received_at": "2026/04/23",
        }
    }

    products = {}
    service_items = {"wholesale": [], "auction": [], "simultaneous": []}

    for row in rows:
        service = _service_for_item(row["id"])
        status = _status_for_item(row["id"], service)
        slug = f"item_{row['id']}_{_safe_slug(row['product_name'] or '')}"[:64]
        detail_page = f"documents_v2_product_detail_{slug}.html"
        amount = _price_for_item(row["id"], int(row["purchase_price"] or 0) or 50000)
        product = {
            "client": client_key,
            "name": row["product_name"] or f"商品 {row['id']}",
            "brand": row["brand_name"] or "ブランド未設定",
            "service": service,
            "status": status,
            "image": _copy_preview_image(row["photo_path"], service, row["brand_name"]),
            "category": _category_from_name(row["product_name"] or ""),
            "code": f"KAIKA-{row['id']}",
            "condition": row["item_condition"] or "A",
            "detail_page": detail_page,
            "request_detail": f"documents_v2_request_detail_{client_key}.html",
            "model": row["product_name"] or f"商品 {row['id']}",
            "real_item_id": row["id"],
            "amount": amount,
            "proxyPurchase": bool(row["show_in_proxy_service"]),
            "proxyPurchaseType": row["sale_type"] or "proxy_purchase",
            "purchase_date": row["purchase_date"] or "未登録",
            "notes": row["notes"] or "特記事項はありません",
            "model_number": row["model_number"] or "未登録",
        }
        products[slug] = product
        service_items[service].append((slug, product))

    response_files = []
    sequence = 1
    for product_key, product in service_items["wholesale"]:
        vendor = VENDORS[(sequence - 1) % len(VENDORS)]
        response_files.append(
            {
                "slug": f"documents_v2_vendor_response_file_{product_key}.html",
                "download": f"static/downloads/vendor_response_{product_key}.txt",
                "label": f"2026/04/{7 + sequence:02d} {vendor['name']} 回答書 No.{40 + sequence:03d}",
                "date": f"2026-04-{7 + sequence:02d}",
                "month": "2026/04",
                "partner": vendor["name"],
                "service": "wholesale",
                "items": [
                    {
                        "product": product_key,
                        "price": product["amount"],
                        "result": "成約",
                        "assigned_client": clients[client_key]["name"],
                    }
                ],
            }
        )
        sequence += 1

    for product_key, product in service_items["auction"]:
        response_files.append(
            {
                "slug": f"documents_v2_auction_response_file_{product_key}.html",
                "download": f"static/downloads/auction_response_{product_key}.txt",
                "label": f"2026/04/{15 + sequence:02d} オークション市場 結果書 No.{80 + sequence:03d}",
                "date": f"2026-04-{15 + sequence:02d}",
                "month": "2026/04",
                "partner": "オークション市場",
                "service": "auction",
                "items": [
                    {
                        "product": product_key,
                        "price": product["amount"],
                        "result": "落札",
                        "assigned_client": clients[client_key]["name"],
                    }
                ],
            }
        )
        sequence += 1

    for product_key, product in service_items["simultaneous"]:
        response_files.append(
            {
                "slug": f"documents_v2_simultaneous_response_file_{product_key}.html",
                "download": f"static/downloads/simultaneous_response_{product_key}.txt",
                "label": f"2026/04/{18 + sequence:02d} 同時出品 売却報告 No.{10 + sequence:03d}",
                "date": f"2026-04-{18 + sequence:02d}",
                "month": "2026/04",
                "partner": "同時出品管理",
                "service": "simultaneous",
                "items": [
                    {
                        "product": product_key,
                        "price": product["amount"],
                        "result": "売却完了",
                        "assigned_client": clients[client_key]["name"],
                    }
                ],
            }
        )
        sequence += 1

    return_groups = {}

    def ensure_return_group(service: str) -> dict:
        group_key = f"{client_key}_{service}"
        if group_key not in return_groups:
            return_groups[group_key] = {
                "client": client_key,
                "service": service,
                "slug": f"documents_v2_client_delivery_{group_key}.html",
                "template": f"documents_v2_client_statement_template_{group_key}.html",
                "items": [],
            }
        return return_groups[group_key]

    for file_info in response_files:
        for item in file_info["items"]:
            ensure_return_group(file_info["service"])["items"].append(
                {
                    "product": item["product"],
                    "price": item["price"],
                    "source": file_info["label"],
                }
            )

    return clients, products, response_files, return_groups


CLIENTS, PRODUCTS, RESPONSE_FILES, RETURN_GROUPS = _load_preview_data()


def enrich_clients_with_monthly_plans() -> None:
    for client_id, client in CLIENTS.items():
        item_count = sum(1 for product in PRODUCTS.values() if product["client"] == client_id)
        plan_info = monthly_plan_for_count(item_count)
        client["monthly_plan"] = plan_info["plan"]
        client["monthly_fee"] = plan_info["monthly_fee"]
        client["monthly_plan_range"] = plan_info["range"]
        client["monthly_plan_item_count"] = plan_info["item_count"]
        client["monthly_plan_custom"] = plan_info["is_custom"]


enrich_clients_with_monthly_plans()


def parse_iso_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def is_recent_file(file_info: dict) -> bool:
    file_date = parse_iso_date(file_info.get("date"))
    if not file_date:
        return True
    return (TODAY - file_date).days <= 31


def historical_response_files() -> list[dict]:
    historical = []
    for index, file_info in enumerate(RESPONSE_FILES[:3], start=1):
        file_date = parse_iso_date(file_info.get("date")) or TODAY
        previous_month = file_date.replace(month=3, day=min(file_date.day, 28))
        clone = dict(file_info)
        clone["date"] = previous_month.isoformat()
        clone["month"] = previous_month.strftime("%Y/%m")
        clone["label"] = clone["label"].replace("2026/04", previous_month.strftime("%Y/%m"))
        clone["slug"] = clone["slug"].replace(".html", f"_history_{index}.html")
        clone["download"] = clone["download"].replace(".txt", f"_history_{index}.txt")
        historical.append(clone)
    return historical


HISTORY_RESPONSE_FILES = historical_response_files()


def money(value: int) -> str:
    return f"¥{value:,}"


def client_products(client_id: str, service_filter: str | None = None) -> list[dict]:
    items = []
    for product_id, product in PRODUCTS.items():
        if product["client"] != client_id:
            continue
        if service_filter and product["service"] != service_filter:
            continue
        item = dict(product)
        item["id"] = product_id
        items.append(item)
    return items


def products_for_service(service: str | None = None) -> list[dict]:
    items = []
    for product_id, product in PRODUCTS.items():
        if service and product["service"] != service:
            continue
        item = dict(product)
        item["id"] = product_id
        items.append(item)
    return items


def wholesale_products() -> list[dict]:
    return products_for_service("wholesale")


def status_class(status: str) -> str:
    return {
        "認証待ち": "pending",
        "認証済み": "approved",
        "査定中": "appraising",
        "出品中": "listing",
        "入金待ち": "payment",
        "完了": "completed",
        "受付不可": "unavailable",
    }.get(status, "approved")


def page(title: str, body: str, *, extra_head: str = "", extra_scripts: str = "") -> str:
    return dedent(
        f"""\
        <!DOCTYPE html>
        <html lang="ja">
        <head>
          <meta charset="UTF-8">
          <meta name="viewport" content="width=device-width, initial-scale=1.0">
          <title>{escape(title)}</title>
          <link rel="stylesheet" href="static/preview_v2.css?v=calculation-send-history-20260425">
          {extra_head}
        </head>
        <body>
        {body}
        <script src="static/preview_data.js?v=calculation-send-history-20260425"></script>
        <script src="https://cdn.jsdelivr.net/npm/html2canvas@1.4.1/dist/html2canvas.min.js"></script>
        <script src="https://cdn.jsdelivr.net/npm/jspdf@2.5.1/dist/jspdf.umd.min.js"></script>
        <script src="static/preview_v2.js?v=calculation-send-history-20260425"></script>
        {extra_scripts}
        </body>
        </html>
        """
    )


def back_bar() -> str:
    return """
    <div class="back-bar">
      <a class="btn btn-outline" href="documents_v2_index.html">書類一覧</a>
      <a class="btn btn-outline" href="javascript:history.back()">1つ前に戻る</a>
    </div>
    """


def history_back_bar() -> str:
    return """
    <div class="back-bar">
      <a class="btn btn-outline" href="documents_v2_history.html">書類履歴トップへ戻る</a>
      <a class="btn btn-outline" href="javascript:history.back()">1つ前に戻る</a>
    </div>
    """


def top_cards() -> str:
    stage1_count = sum(len(client_products(client_id)) for client_id in CLIENTS)
    stage2_count = sum(len(products_for_outgoing(service)) for service in ("wholesale", "auction", "simultaneous"))
    stage3_count = len(RESPONSE_FILES)
    stage4_count = sum(1 for _ in RETURN_GROUPS)
    settlement_count = len(CLIENTS)
    cards = [
        ("1", "クライアントから受付", "どのクライアントから何の商品が届いたかを、名前単位で確認します。", "documents_v2_client_incoming.html"),
        ("2", "開花から業者へ依頼", "査定中・出品中に進めた商品を、業者販売・オークション・同時出品に分けて書類化します。", "documents_v2_vendor_outgoing.html"),
        ("3", "業者から回答受領", "業者から届いた回答ファイルを1件ずつ確認し、商品ごとに返送先を振り分けます。", "documents_v2_vendor_incoming.html"),
        ("4", "買取明細書", "クライアントごとに買取明細書を作成し、送付後にユーザー書類・商品ページへ反映します。", "documents_v2_client_outgoing.html"),
        ("5", "クライアント返送見積依頼書", "買取明細書送付後に、ユーザーから見積依頼書として返送された書類を確認します。", "documents_v2_client_estimate_requests.html"),
        ("6", "仕切書", "月額利用料と撮影・梱包・発送代行サポート費用を月締めで確認・送付します。", "documents_v2_settlement_statements.html"),
        ("7", "計算書", "代行仕入れを行った取引だけを計算書として確認します。", "documents_v2_calculation_statements.html"),
    ]
    inner = []
    for step, heading, desc, href in cards:
        count = {
            "1": f"{stage1_count}点受付中",
            "2": f"{stage2_count}点準備中",
            "3": f"{stage3_count}ファイル受領",
            "4": f"{stage4_count}名へ返送準備",
            "5": "返送待ちを確認",
            "6": f"{settlement_count}名分",
            "7": "代行仕入れのみ",
        }.get(step, "")
        inner.append(
            f"""
            <a class="flow-card" href="{href}">
              <span class="flow-step">{step}</span>
              <h2>{heading}</h2>
              <p>{desc}</p>
              <span class="flow-count">{count}</span>
            </a>
            """
        )
    return f'<div class="flow-grid">{"".join(inner)}</div>'


def stage_counts(stage: str) -> dict[str, int]:
    counts = {
        "all": 0,
        "wholesale": 0,
        "auction": 0,
        "simultaneous": 0,
    }
    if stage == "stage1":
        counts["wholesale"] = len(products_for_service("wholesale"))
        counts["auction"] = len(products_for_service("auction"))
        counts["simultaneous"] = len(products_for_service("simultaneous"))
    elif stage == "stage2":
        counts["wholesale"] = sum(1 for item in products_for_service("wholesale") if item["status"] == outgoing_expected_status("wholesale"))
        counts["auction"] = sum(1 for item in products_for_service("auction") if item["status"] == outgoing_expected_status("auction"))
        counts["simultaneous"] = sum(1 for item in products_for_service("simultaneous") if item["status"] == outgoing_expected_status("simultaneous"))
    elif stage == "stage3":
        counts["wholesale"] = sum(1 for item in RESPONSE_FILES if item["service"] == "wholesale" and is_recent_file(item))
        counts["auction"] = sum(1 for item in RESPONSE_FILES if item["service"] == "auction" and is_recent_file(item))
        counts["simultaneous"] = sum(1 for item in RESPONSE_FILES if item["service"] == "simultaneous" and is_recent_file(item))
    elif stage == "stage4":
        for config in RETURN_GROUPS.values():
            counts[config["service"]] += 1
    counts["all"] = counts["wholesale"] + counts["auction"] + counts["simultaneous"]
    return counts


def service_tabs(stage: str, current: str) -> str:
    tabs = {
        "stage1": [
            ("documents_v2_client_incoming.html", "すべて", "all"),
            ("documents_v2_client_incoming_wholesale.html", "業者卸販売", "wholesale"),
            ("documents_v2_client_incoming_auction.html", "業者オークション", "auction"),
            ("documents_v2_client_incoming_simultaneous.html", "同時出品", "simultaneous"),
        ],
        "stage2": [
            ("documents_v2_vendor_outgoing.html", "すべて", "all"),
            ("documents_v2_vendor_outgoing_wholesale.html", "業者卸販売", "wholesale"),
            ("documents_v2_vendor_outgoing_auction.html", "業者オークション", "auction"),
            ("documents_v2_vendor_outgoing_simultaneous.html", "同時出品", "simultaneous"),
        ],
        "stage3": [
            ("documents_v2_vendor_incoming.html", "すべて", "all"),
            ("documents_v2_vendor_incoming_wholesale.html", "業者卸販売", "wholesale"),
            ("documents_v2_vendor_incoming_auction.html", "業者オークション", "auction"),
            ("documents_v2_vendor_incoming_simultaneous.html", "同時出品", "simultaneous"),
        ],
        "stage4": [
            ("documents_v2_client_outgoing.html", "すべて", "all"),
            ("documents_v2_client_outgoing_wholesale.html", "業者卸販売", "wholesale"),
            ("documents_v2_client_outgoing_auction.html", "業者オークション", "auction"),
            ("documents_v2_client_outgoing_simultaneous.html", "同時出品", "simultaneous"),
        ],
    }[stage]
    counts = stage_counts(stage)
    parts = []
    for href, label, key in tabs:
        cls = "service-tab is-active" if current == key else "service-tab"
        parts.append(
            f'<a class="{cls}" href="{href}" data-stage-tab="{stage}" data-service-key="{key}">{label}<span class="service-tab-count">{counts[key]}</span></a>'
        )
    return f'<div class="service-tabs">{"".join(parts)}</div>'


def request_detail_filename(client_id: str, service: str | None = None) -> str:
    if service and service != "all":
        return f"documents_v2_request_detail_{client_id}_{service}.html"
    return f"documents_v2_request_detail_{client_id}.html"


def product_detail_href(product: dict) -> str:
    return product["detail_page"]


def service_summary_label(products: list[dict], service_filter: str | None) -> str:
    if service_filter and service_filter != "all":
        return SERVICE_LABELS[service_filter]

    summary = []
    for service_key in ("wholesale", "auction", "simultaneous"):
        count = sum(1 for product in products if product["service"] == service_key)
        if count:
            summary.append(f"{SERVICE_LABELS[service_key]} {count}点")
    return " / ".join(summary) or "認証申請一式"


def service_chips(products: list[dict]) -> str:
    chips = []
    for service_key in ("wholesale", "auction", "simultaneous"):
        count = sum(1 for product in products if product["service"] == service_key)
        if not count:
            continue
        chips.append(
            f'<span class="service-chip service-chip-{service_key}">{SERVICE_LABELS[service_key]} {count}件</span>'
        )
    return "".join(chips)


def compact_product_names(products: list[dict], limit: int = 3) -> str:
    names = [product["name"] for product in products]
    if len(names) <= limit:
        return " / ".join(names)
    remaining = len(names) - limit
    return f"{' / '.join(names[:limit])} / ほか {remaining}件"


def client_overview_status(products: list[dict]) -> str:
    statuses = [product["status"] for product in products]
    if any(status == "認証待ち" for status in statuses):
        return "認証待ち"
    if any(status in {"査定中", "出品中"} for status in statuses):
        return "査定中"
    if any(status == "入金待ち" for status in statuses):
        return "入金待ち"
    if all(status == "完了" for status in statuses):
        return "完了"
    return statuses[0] if statuses else "認証待ち"


def client_summary_card(client_id: str, products: list[dict], *, service_filter: str | None = None) -> str:
    client = CLIENTS[client_id]
    product_names = compact_product_names(products)
    request_detail = request_detail_filename(client_id, service_filter)
    status_badge = client_overview_status(products)
    request_kind = service_summary_label(products, service_filter)
    search_blob = " ".join(
        [client["name"], client.get("client_no", ""), client["request_id"], request_kind, product_names]
        + [SERVICE_LABELS[item["service"]] for item in products]
    )
    return f"""
    <div class="summary-card js-filter-card" data-search="{escape(search_blob)}">
      <div class="summary-head">
        <div>
          <div class="summary-client">{client["name"]}</div>
          <div class="summary-meta">{client.get("client_no", client["request_id"])} / {client["request_id"]} / 受付日 {client["received_at"]}</div>
        </div>
        <span class="pill {status_class(status_badge)}">{status_badge}</span>
      </div>
      <div class="summary-card-grid">
        <div class="field-block"><div class="field-label">申請商品数</div><div class="field-value">{len(products)}点</div></div>
        <div class="field-block"><div class="field-label">依頼形式</div><div class="field-value">{request_kind}</div></div>
        <div class="field-block"><div class="field-label">代表商品</div><div class="field-value">{product_names}</div></div>
        <div class="field-block"><div class="field-label">次の流れ</div><div class="field-value">詳細で確認後、査定中にした商品だけ2番へ送ります</div></div>
      </div>
      <div class="summary-footer">
        <div class="service-chip-row summary-chip-row">{service_chips(products)}</div>
        <div class="card-actions summary-actions summary-action-row">
          <a class="btn btn-soft" href="{request_detail}">詳細を確認する</a>
        </div>
      </div>
    </div>
    """


def stage1_page(service: str | None) -> str:
    filtered_clients = []
    for client_id in CLIENTS:
        items = client_products(client_id, service if service not in (None, "all") else None)
        if items:
            filtered_clients.append(client_summary_card(client_id, items, service_filter=service or "all"))
    label = SERVICE_LABELS.get(service or "all", "すべてのサービス")
    return page(
        f"1. クライアントから受付 - {label}",
        f"""
        <div class="page">
          {back_bar()}
          <div class="page-head">
            <div>
              <h1>1. クライアントから受付</h1>
              <p>最初の画面ではクライアント名ごとに、何の商品が何件届いているかを一覧で確認します。クライアント名を開くと、商品ごとの詳細・状態更新・受付不可通知が行えます。</p>
            </div>
          </div>
          <div class="section">
            <div class="section-head">
              <div>
                <h3>クライアント別の受付一覧</h3>
                <p class="section-note">商品一覧を縦に並べず、まずはクライアント単位で確認できる形にしています。サービスを切り替えても同じ見え方で確認できます。</p>
              </div>
            </div>
            {service_tabs("stage1", service or "all")}
            <div class="filter-row">
              <label class="field search-field">
                <span>クライアント検索</span>
                <input id="client-incoming-search" type="search" placeholder="クライアント名・クライアント番号・商品名で検索">
              </label>
            </div>
            <div class="summary-grid">
              {''.join(filtered_clients)}
            </div>
            <p id="client-incoming-empty" class="section-note" hidden>条件に合う受付はありません。</p>
          </div>
        </div>
        """,
    )


def status_controls(product_id: str, status: str, product_name: str, service: str) -> str:
    select_id = f"status-{product_id}"
    badge_id = f"badge-{product_id}"
    state_id = f"state-{product_id}"
    notice_id = f"notice-{product_id}"
    memo_id = f"memo-{product_id}"
    if service == "simultaneous":
        status_options = ["認証待ち", "認証済み", "出品中", "入金待ち", "完了"]
    else:
        status_options = ["認証待ち", "認証済み", "査定中", "入金待ち", "完了"]
    options = "".join(
        f'<option value="{label}" {"selected" if label == status else ""}>{label}</option>'
        for label in status_options
    )
    return f"""
    <div class="status-box">
      <div class="status-row">
        <span class="field-label">現在の状態</span>
        <span id="{state_id}" class="field-value">{status}</span>
      </div>
      <div class="status-row status-row-actions">
        <select id="{select_id}" class="status-select">
          {options}
        </select>
        <button class="btn btn-primary btn-compact" type="button" onclick="applyStatus('{select_id}','{badge_id}','{state_id}','{notice_id}','{escape(product_name)}','{service}')">通知して更新</button>
        <button class="btn btn-outline btn-compact" type="button" onclick="notifyUnavailable('{notice_id}','{badge_id}','{state_id}','card-{product_id}','{escape(product_name)}')">受付不可を通知</button>
      </div>
      <div class="field field-wide" style="margin-top:10px;">
        <span>クライアントへ送るメモ</span>
        <textarea id="{memo_id}" rows="3" placeholder="例：状態変更の理由や補足内容を入力してください"></textarea>
      </div>
      <div class="status-row memo-action-row">
        <button class="btn btn-soft btn-compact" type="button" onclick="sendMemo('{memo_id}','{notice_id}','{product_id}','{escape(product_name)}')">メモを送信</button>
      </div>
      <p id="{notice_id}" class="inline-notice" hidden></p>
    </div>
    """


def product_card(product_id: str) -> str:
    product = PRODUCTS[product_id]
    client = CLIENTS[product["client"]]
    detail_href = product_detail_href(product)
    return f"""
    <div class="product-card js-product-card" id="card-{product_id}" data-product-id="{product_id}" data-service="{product['service']}" data-default-status="{product['status']}">
      <div class="product-thumb">
        <img src="{product['image']}" alt="{escape(product['name'])}">
      </div>
      <div class="product-body">
        <div class="product-top">
          <div>
            <div class="product-title"><a href="{detail_href}">{product['name']}</a></div>
            <div class="product-meta">{client['name']} / {SERVICE_LABELS[product['service']]} / {product['brand']} / 商品ID {product['code']}</div>
          </div>
          <span id="badge-{product_id}" class="pill {status_class(product['status'])}">{product['status']}</span>
        </div>
        <div class="detail-grid">
          <div class="field-block"><div class="field-label">申請形式</div><div class="field-value">{SERVICE_LABELS[product['service']]}</div></div>
          <div class="field-block"><div class="field-label">カテゴリ</div><div class="field-value">{product['category']}</div></div>
          <div class="field-block"><div class="field-label">ブランド</div><div class="field-value">{product['brand']}</div></div>
          <div class="field-block"><div class="field-label">モデル</div><div class="field-value">{product['model']}</div></div>
          <div class="field-block"><div class="field-label">状態</div><div class="field-value">{product['condition']}</div></div>
        </div>
        <div class="card-actions">
          <a class="btn btn-soft" href="{detail_href}">{product['name']} の詳細を見る</a>
        </div>
        {status_controls(product_id, product['status'], product['name'], product['service'])}
      </div>
    </div>
    """


def request_detail_page(client_id: str, service: str | None = None) -> str:
    client = CLIENTS[client_id]
    products = client_products(client_id, service if service not in (None, "all") else None)
    service_summary = service_summary_label(products, service)
    sections = []
    for service_key in ("wholesale", "auction", "simultaneous"):
        service_items = [item for item in products if item["service"] == service_key]
        if not service_items:
            continue
        guidance = {
            "wholesale": "業者卸販売の申請です。認証後に査定中へ進めた商品だけが 2番 の業者依頼へ反映されます。",
            "auction": "業者オークションの申請です。認証後に査定中へ進めた商品だけが 2番 のオークション依頼へ反映されます。",
            "simultaneous": "同時出品の申請です。認証後は出品中へ進め、売却履歴が出たら 3番 と 4番 へ進みます。",
        }[service_key]
        sections.append(
            f"""
            <div class="service-panel">
              <div class="service-panel-head">
                <div>
                  <h4>{SERVICE_LABELS[service_key]}</h4>
                  <p>{guidance}</p>
                </div>
                <span class="service-panel-count">{len(service_items)}件</span>
              </div>
              <div class="stack">
                {''.join(product_card(item['id']) for item in service_items)}
              </div>
            </div>
            """
        )
    return page(
        f"{client['name']} さんの受付詳細",
        f"""
        <div class="page">
          {back_bar()}
          <div class="page-head">
            <div>
              <h1>{client['name']} さんの受付詳細</h1>
              <p>{client['request_id']} / 受付日 {client['received_at']} / {service_summary}。ここで商品ごとに、認証済み・査定中・入金待ち・受付不可通知を切り替えます。査定中にした業者卸販売の商品だけが 2番へ進みます。</p>
            </div>
          </div>
          {service_tabs("stage1", service or "all")}
          <div class="section">
            <div class="section-head">
              <div>
                <h3>商品ごとの確認</h3>
                <p class="section-note">商品名を押すと、商品一覧で登録している内容をそのまま確認できます。商品ごとに申請形式と現在状態を見ながら、認証・査定・入金待ち・受付不可通知を行います。</p>
              </div>
            </div>
            {''.join(sections)}
          </div>
        </div>
        """,
    )


def product_detail_page(product_id: str) -> str:
    product = PRODUCTS[product_id]
    return page(
        f"{product['name']} の商品詳細",
        f"""
        <div class="page">
          {back_bar()}
          <div class="page-head">
            <div>
              <h1>{product['name']} の商品詳細</h1>
              <p>{product['name']} の登録内容を、商品一覧で確認するイメージに近い形で preview しています。必要であれば実際の商品詳細画面も開けます。</p>
            </div>
          </div>
          <div class="section">
            <div class="detail-layout">
              <div class="detail-image-card">
                <img src="{product['image']}" alt="{escape(product['name'])}">
              </div>
              <div class="detail-list">
                <div class="detail-row"><strong>商品名</strong><span>{product['name']}</span></div>
                <div class="detail-row"><strong>申請形式</strong><span>{SERVICE_LABELS[product['service']]}</span></div>
                <div class="detail-row"><strong>ブランド</strong><span>{product['brand']}</span></div>
                <div class="detail-row"><strong>カテゴリ</strong><span>{product['category']}</span></div>
                <div class="detail-row"><strong>型番 / モデル</strong><span>{product['model_number']}</span></div>
                <div class="detail-row"><strong>状態</strong><span>{product['condition']}</span></div>
                <div class="detail-row"><strong>管理コード</strong><span>{product['code']}</span></div>
                <div class="detail-row"><strong>仕入参考額</strong><span>{money(product['amount'])}</span></div>
                <div class="detail-row"><strong>登録日</strong><span>{product['purchase_date']}</span></div>
                <div class="detail-row"><strong>メモ</strong><span>{product['notes']}</span></div>
              </div>
            </div>
          </div>
        </div>
        """,
    )


def products_for_outgoing(service: str) -> list[dict]:
    return products_for_service(service)


def outgoing_expected_status(service: str) -> str:
    return "出品中" if service == "simultaneous" else "査定中"


def outgoing_button_label(service: str) -> str:
    return {
        "wholesale": "業者向け見積依頼書を作成する",
        "auction": "オークション依頼書を作成する",
        "simultaneous": "出品管理シートを作成する",
    }[service]


def outgoing_section(service: str) -> str:
    descriptions = {
        "wholesale": "査定中にした業者卸販売の商品を、複数商品まとめて業者へ流すための見積依頼書に差し込みます。",
        "auction": "査定中にした業者オークションの商品をまとめて選択し、オークション出品用の依頼書を作成します。",
        "simultaneous": "出品中にした同時出品の商品を選択し、開花側で使う出品管理シートを作成します。",
    }
    cards = []
    for product in products_for_outgoing(service):
        client = CLIENTS[product["client"]]
        detail_href = product_detail_href(product)
        cards.append(
            f"""
            <div class="product-card js-product-card js-filter-card" data-product-id="{product['id']}" data-service="{product['service']}" data-default-status="{product['status']}" data-stage="outgoing-{service}" data-expected-status="{outgoing_expected_status(service)}" data-search="{escape(f"{client['name']} {product['name']} {product['brand']} {SERVICE_LABELS[product['service']]} {product['code']}")}">
              <div class="product-thumb">
                <img src="{product['image']}" alt="{escape(product['name'])}">
              </div>
              <div class="product-body">
                <div class="product-top">
                  <div>
                    <div class="product-title">{product['name']}</div>
                    <div class="product-meta">{client['name']} / {product['brand']} / {SERVICE_LABELS[product['service']]} / 商品ID {product['code']}</div>
                  </div>
                  <span class="pill {status_class(product['status'])}">{product['status']}</span>
                </div>
                <div class="detail-grid">
                  <div class="field-block"><div class="field-label">画像確認</div><div class="field-value"><a href="{detail_href}">{product['name']} の詳細を見る</a></div></div>
                  <div class="field-block"><div class="field-label">申請元クライアント</div><div class="field-value">{client['name']}</div></div>
                  <div class="field-block"><div class="field-label">処理部門</div><div class="field-value">{SERVICE_LABELS[service]}</div></div>
                  <div class="field-block"><div class="field-label">次の流れ</div><div class="field-value">{descriptions[service]}</div></div>
                </div>
              </div>
            </div>
            """
        )
    return f"""
    <div class="section">
      <div class="section-head">
        <div>
          <h3>{SERVICE_LABELS[service]} へ進める商品</h3>
          <p class="section-note">{descriptions[service]}</p>
        </div>
        <div class="card-actions stage-action-top">
          <a class="btn btn-outline" href="vendor_partner_registry.html">送付先業者を登録・編集する</a>
          <a class="btn btn-primary" href="vendor_estimate_batch_create.html?service={service}">{outgoing_button_label(service)}</a>
        </div>
      </div>
      <div class="stack">
        {''.join(cards)}
      </div>
      <p id="outgoing-empty-{service}" class="section-note" hidden>この部門で進行できる商品はありません。1番で状態を更新するとここに表示されます。</p>
    </div>
    """


def stage2_page(service: str = "all") -> str:
    sections = []
    for service_key in ("wholesale", "auction", "simultaneous"):
        if service != "all" and service_key != service:
            continue
        sections.append(outgoing_section(service_key))
    return page(
        "2. 開花から業者へ依頼",
        f"""
        <div class="page">
          {back_bar()}
          <div class="page-head">
            <div>
              <h1>2. 開花から業者へ依頼</h1>
              <p>ここでは、1番で進行可能にした商品を 3部門に分けて整理し、まとめて書類を作成します。商品単位で確認しながら、業者卸販売・業者オークション・同時出品の流れを分けて扱います。</p>
            </div>
          </div>
          {service_tabs("stage2", service)}
          <div class="filter-row">
            <label class="field search-field">
              <span>商品検索</span>
              <input id="vendor-outgoing-search" type="search" placeholder="クライアント名・商品名・ブランドで検索">
            </label>
          </div>
          {''.join(sections)}
          <p id="vendor-outgoing-search-empty" class="section-note" hidden>条件に合う商品はありません。</p>
        </div>
        """,
    )


def vendor_registry_page() -> str:
    existing = "".join(
        f"""
        <div class="compact-item">
          <strong>{vendor['name']}</strong>
          <span>{vendor['mail']} / {vendor['phone']}</span>
          <span>{vendor['memo']}</span>
        </div>
        """
        for vendor in VENDORS
    )
    return page(
        "送付先業者を登録・編集する",
        f"""
        <div class="page">
          {back_bar()}
          <div class="page-head">
            <div>
              <h1>送付先業者を登録・編集する</h1>
              <p>新しい取引先を追加するときは、ここで会社名・担当者名・メールアドレス・電話番号を登録します。既存の送付先も一覧で確認できます。</p>
            </div>
          </div>
          <div class="two-col">
            <div class="section">
              <div class="section-head"><h3>登録済みの送付先業者</h3></div>
              <div class="compact-list">{existing}</div>
            </div>
            <div class="section">
              <div class="section-head"><h3>新規業者を登録</h3></div>
              <div class="form-grid">
                <label class="field"><span>会社名</span><input type="text" placeholder="例：ブランドセンター"></label>
                <label class="field"><span>担当者名</span><input type="text" placeholder="例：田中 太郎"></label>
                <label class="field"><span>メールアドレス</span><input type="email" placeholder="example@vendor.jp"></label>
                <label class="field"><span>電話番号</span><input type="text" placeholder="03-0000-0000"></label>
                <label class="field field-wide"><span>メモ</span><textarea rows="5" placeholder="取り扱いブランドや連絡時の注意点を入力"></textarea></label>
              </div>
              <div class="card-actions" style="margin-top: 16px;">
                <button class="btn btn-primary" type="button" onclick="showInlineNotice('vendor-registry-notice','新規業者の入力欄を整えた preview です。実装時はこの内容をそのまま保存できるようにつなぎます。')">登録内容を確認する</button>
              </div>
              <p id="vendor-registry-notice" class="inline-notice" hidden></p>
            </div>
          </div>
        </div>
        """,
    )


def batch_create_page() -> str:
    rows = []
    for product in products_for_service():
        client = CLIENTS[product["client"]]
        detail_href = product_detail_href(product)
        rows.append(
            f"""
            <label class="select-row js-vendor-select-row" data-product-id="{product['id']}" data-service="{product['service']}" data-default-status="{product['status']}">
              <input class="vendor-check" type="checkbox" data-product-id="{product['id']}" data-product="{escape(product['name'])}" data-summary="{client['name']} / {product['name']} / {product['brand']}" data-brand="{product['brand']}">
              <span class="select-main">{client['name']} / {product['name']}</span>
              <span class="select-sub">{product['brand']} / {SERVICE_LABELS[product['service']]} / 商品ID {product['code']} / <a href="{detail_href}">商品詳細を見る</a></span>
            </label>
            """
        )
    vendor_options = "".join(f'<option>{vendor["name"]}</option>' for vendor in VENDORS)
    return page(
        "書類を作成する",
        f"""
        <div class="page">
          {back_bar()}
          <div class="page-head">
            <div>
              <h1 id="batch-create-title">書類を作成する</h1>
              <p id="batch-create-copy">ここでは、部門ごとに送る商品をチェックして既存テンプレートへ差し込みます。件名や本文ではなく、商品・送付先・テンプレートだけを整える形です。</p>
            </div>
          </div>
          <div class="two-col">
            <div class="section">
              <div class="section-head"><h3 id="batch-create-list-title">書類に入れる商品</h3></div>
              <div class="select-grid">{''.join(rows)}</div>
              <p id="vendor-selection-empty" class="section-note" hidden>査定中の業者卸販売商品がないため、選択できる商品はありません。</p>
            </div>
            <div class="section">
              <div class="section-head"><h3 id="batch-create-config-title">書類の設定</h3></div>
              <div class="form-grid">
                <label class="field"><span id="batch-target-label">送付先業者</span><select id="vendor-target"><option value="">選択してください</option>{vendor_options}</select></label>
                <label class="field"><span>書類種別</span><select id="document-type"><option>見積依頼書</option><option>出品依頼書</option><option>出品管理シート</option></select></label>
                <label class="field field-wide"><span>備考</span><textarea rows="4" placeholder="必要な場合だけ備考を入力"></textarea></label>
              </div>
              <div class="mini-panel">
                <strong>選択中の商品</strong>
                <div id="vendor-selected-summary" style="margin-top:8px;color:#475569;">まだ商品を選択していません</div>
              </div>
              <p id="vendor-draft-notice" class="inline-notice" hidden></p>
              <div class="card-actions" style="margin-top: 16px;">
                <a class="btn btn-outline" href="vendor_partner_registry.html">送付先業者を登録・編集する</a>
                <button class="btn btn-primary" type="button" onclick="prepareEstimateDraft('documents_v2_vendor_estimate_template.html')">書類を作成する</button>
              </div>
            </div>
          </div>
        </div>
        """,
    )


def doc_rows(items: list[dict], *, price_key: str = "price") -> str:
    rows = []
    for idx in range(max(15, len(items))):
        item = items[idx] if idx < len(items) else None
        if item:
            product = PRODUCTS[item["product"]]
            amount = item.get(price_key, product.get("amount", 0))
            rows.append(
                f"""
                <tr>
                  <td class="doc-col-no">{idx + 1}</td>
                  <td class="doc-col-name">{product['name']}</td>
                  <td class="doc-col-brand">{product['brand']}</td>
                  <td class="doc-col-condition">{product['condition']}</td>
                  <td class="doc-col-qty">1</td>
                  <td class="doc-col-price doc-cell-price" data-amount="{amount}">{money(amount)}</td>
                  <td class="doc-col-price doc-cell-amount" data-amount="{amount}">{money(amount)}</td>
                </tr>
                """
            )
        else:
            rows.append(
                """
                <tr>
                  <td class="doc-col-no">&nbsp;</td>
                  <td class="doc-col-name"></td>
                  <td class="doc-col-brand"></td>
                  <td class="doc-col-condition"></td>
                  <td class="doc-col-qty"></td>
                  <td class="doc-col-price"></td>
                  <td class="doc-col-price"></td>
                </tr>
                """
            )
    return "".join(rows)


def blank_doc_rows(count: int = 15) -> str:
    return "".join(
        """
        <tr>
          <td class="doc-col-no">&nbsp;</td>
          <td class="doc-col-name"></td>
          <td class="doc-col-brand"></td>
          <td class="doc-col-condition"></td>
          <td class="doc-col-qty"></td>
          <td class="doc-col-price"></td>
          <td class="doc-col-price"></td>
        </tr>
        """
        for _ in range(count)
    )


def estimate_template_page() -> str:
    return page(
        "見積依頼書",
        f"""
        <div class="page page-narrow">
          <div class="doc-toolbar">
            <div class="doc-toolbar-copy">2番で選んだ商品と送付先業者を、既存の見積依頼書テンプレートへ差し込む preview です。</div>
            <div class="doc-toolbar-actions">
              <a class="btn btn-outline" href="documents_v2_vendor_outgoing.html">一覧へ戻る</a>
              <button class="btn btn-soft" type="button" data-action="toggle-edit">書類を編集する</button>
              <button class="btn btn-outline" type="button" data-action="reset-doc">入力を元に戻す</button>
              <button class="btn btn-primary" type="button" onclick="saveCurrentDocumentAsPdf('vendor','kaika_vendor_request.pdf')">PDFとして保存</button>
              <button class="btn btn-soft" type="button" onclick="saveCurrentDocumentAsWord('vendor','kaika_vendor_request.doc')">Word形式で保存</button>
            </div>
          </div>
          <div class="doc-page" data-doc-editor>
            <div class="doc-title">見積依頼書</div>
            <div class="doc-meta">
              <div><strong>発行日</strong> 2026年4月23日</div>
              <div><strong>送付先</strong> <span data-vendor-name>取引先業者 御中</span></div>
              <div><strong>差出人</strong> 株式会社開花</div>
              <div><strong>担当者</strong> 田中 花子</div>
              <div><strong>連絡先</strong> 03-0000-0000 / kaika@example.jp</div>
              <div><strong>回答希望日</strong> 2026年4月30日</div>
            </div>
            <div class="doc-message">下記の商品について、見積のご確認をお願いいたします。</div>
            <table class="doc-table">
              <thead>
                <tr>
                  <th class="doc-col-no">No.</th>
                  <th class="doc-col-name">商品名</th>
                  <th class="doc-col-brand">ブランド</th>
                  <th class="doc-col-condition">状態</th>
                  <th class="doc-col-qty">数量</th>
                  <th class="doc-col-price">単価</th>
                  <th class="doc-col-price">金額</th>
                </tr>
              </thead>
              <tbody id="vendor-estimate-rows">{blank_doc_rows()}</tbody>
            </table>
            <div class="doc-total">
              <span>合計金額</span>
              <strong data-total-output>{money(0)}</strong>
            </div>
            <div class="doc-note">
              <strong>備考</strong>
              <p>選択した商品をもとに、必要に応じて書類内容を編集してから業者へ送付します。査定条件、返送方法、回答期限など、重要事項はこの欄へ追記してください。</p>
            </div>
            <p id="doc-save-notice" class="inline-notice" hidden></p>
          </div>
        </div>
        """,
        extra_scripts='<script src="static/doc_editor.js"></script>',
    )


def stage3_page(service: str = "all") -> str:
    sections = []
    current_files = [item for item in RESPONSE_FILES if is_recent_file(item)]
    vendor_options = "".join(f'<option value="{escape(vendor["name"])}"></option>' for vendor in VENDORS)
    for service_key in ("wholesale", "auction", "simultaneous"):
        if service != "all" and service_key != service:
            continue
        service_note = {
            "wholesale": "業者卸販売の回答書類です。ファイルを1件ずつ開いて、商品ごとの売却額と返送先を確認します。",
            "auction": "業者オークションの回答書類です。落札結果を確認し、商品ごとに返送先を整理します。",
            "simultaneous": "同時出品の売却履歴やスクリーンショットを登録し、どの商品が売れたかを確認します。",
        }[service_key]
        cards = []
        for file_info in [item for item in current_files if item["service"] == service_key]:
            products = " / ".join(PRODUCTS[item["product"]]["name"] for item in file_info["items"])
            cards.append(
                f"""
                <div class="file-card js-filter-card" data-search="{escape(f"{file_info['label']} {file_info['partner']} {products} {SERVICE_LABELS[file_info['service']]}")}">
                  <div class="file-head">
                    <div>
                      <div class="file-title">{file_info['label']}</div>
                      <div class="file-meta">{file_info['partner']} / {file_info['month']} / {SERVICE_LABELS[file_info['service']]} / 商品 {len(file_info['items'])}点</div>
                    </div>
                    <span class="pill approved">回答受領</span>
                  </div>
                  <p class="section-note">{products}</p>
                  <div class="card-actions">
                    <a class="btn btn-soft" href="{file_info['slug']}">書類を確認する</a>
                  </div>
                </div>
                """
            )
        sections.append(
            f"""
            <div class="section">
              <div class="section-head">
                <div>
                  <h3>{SERVICE_LABELS[service_key]} の回答書類</h3>
                  <p class="section-note">{service_note}</p>
                </div>
              </div>
              <div class="stack">{''.join(cards)}</div>
            </div>
            """
        )
    return page(
        "3. 業者から回答受領",
        f"""
        <div class="page">
          {back_bar()}
          <div class="page-head">
            <div>
              <h1>3. 業者から回答受領</h1>
              <p>月に1件ではなく、取引ごとの回答ファイルを1件ずつ登録して確認します。直近1か月以内の書類をここで扱い、それ以前のものは書類履歴で確認する前提です。</p>
            </div>
          </div>
          {service_tabs("stage3", service)}
          <div class="filter-row">
            <label class="field search-field">
              <span>回答ファイル検索</span>
              <input id="vendor-incoming-search" type="search" placeholder="取引先・商品名・書類名で検索">
            </label>
            <a class="btn btn-outline" href="documents_v2_history.html">書類履歴を開く</a>
          </div>
          <div class="section">
              <div class="section-head">
                <div>
                  <h3>回答ファイルを登録する</h3>
                  <p class="section-note">届いたファイルをサービス別に登録し、下の一覧から書類の中身を開いて確認する想定です。</p>
                </div>
              </div>
              <div class="form-inline">
                <label class="field file-field"><span>回答ファイル</span><input id="vendor-file" type="file"></label>
                <label class="field"><span>題名</span><input id="vendor-file-title" type="text" placeholder="例：2026/04/24 ブランドセンター 回答書 No.054"></label>
                <label class="field"><span>業者名</span><input id="vendor-file-company" list="vendor-company-options" type="text" placeholder="例：ブランドセンター"><datalist id="vendor-company-options">{vendor_options}</datalist></label>
                <label class="field"><span>書類区分</span><select id="vendor-file-service"><option value="wholesale">業者卸販売</option><option value="auction">業者オークション</option><option value="simultaneous">同時出品</option></select></label>
                <label class="field"><span>取引日</span><input id="vendor-file-date" type="date" value="2026-04-23"></label>
                <button class="btn btn-primary btn-compact" type="button" onclick="registerVendorFile()">ファイルを登録</button>
            </div>
            <p id="vendor-file-notice" class="inline-notice" hidden></p>
          </div>
          {''.join(sections)}
          <p id="vendor-incoming-search-empty" class="section-note" hidden>条件に合う回答書類はありません。</p>
        </div>
        """,
    )


def history_category_configs() -> list[dict]:
    return [
        {
            "key": "client_requests",
            "title": "クライアントから届いた依頼書",
            "copy": "1番で受け付けた、最初の申請依頼書・買取依頼書の履歴です。",
            "slug": "documents_v2_history_client_requests.html",
        },
        {
            "key": "vendor_requests",
            "title": "開花から業者への見積依頼書",
            "copy": "2番でPDF/Word保存して完了にした、業者向け見積依頼書・オークション依頼書の履歴です。",
            "slug": "documents_v2_history_vendor_requests.html",
        },
        {
            "key": "vendor_responses",
            "title": "業者から届いた回答書類",
            "copy": "3番で登録した、業者卸販売・業者オークション・同時出品の回答ファイル履歴です。",
            "slug": "documents_v2_history_vendor_responses.html",
        },
        {
            "key": "client_returns",
            "title": "クライアントへ返送した書類",
            "copy": "4番で送付完了にした、ユーザー向け買取明細書などの返送履歴です。",
            "slug": "documents_v2_history_client_returns.html",
        },
        {
            "key": "client_estimate_requests",
            "title": "クライアント返送見積依頼書",
            "copy": "買取明細書を送った後、ユーザーから見積依頼書として返送された書類の履歴です。",
            "slug": "documents_v2_history_client_estimate_requests.html",
        },
        {
            "key": "settlement_statements",
            "title": "仕切書",
            "copy": "月額利用料や撮影・梱包・発送代行サポート費用をまとめた、利用明細書型の仕切書履歴です。",
            "slug": "documents_v2_history_settlement_statements.html",
        },
        {
            "key": "calculation_statements",
            "title": "計算書",
            "copy": "代行仕入れの計算書を送付した履歴です。",
            "slug": "documents_v2_history_calculation_statements.html",
        },
    ]


def history_config(category: str) -> dict:
    configs = {item["key"]: item for item in history_category_configs()}
    return configs.get(category, history_category_configs()[0])


def history_static_entries() -> list[dict]:
    return [dict(item, category="vendor_responses") for item in HISTORY_RESPONSE_FILES] + additional_history_entries()


def history_rows_for_category(category: str) -> str:
    rows = []
    entries = [item for item in history_static_entries() if item.get("category") == category]
    for file_info in sorted(entries, key=lambda item: item["date"], reverse=True):
        products = " / ".join(PRODUCTS[item["product"]]["name"] for item in file_info["items"])
        search_text = f"{file_info['label']} {file_info['partner']} {products} {SERVICE_LABELS[file_info['service']]} {file_info['month']} {file_info['date']}"
        rows.append(
            f"""
            <div class="file-card js-filter-card js-history-card" data-search="{escape(search_text)}" data-title="{escape(file_info['label'])}" data-date="{file_info['date']}" data-month="{file_info['month'].replace('/', '-')}">
              <div class="file-head">
                <div>
                  <div class="file-title">{file_info['label']}</div>
                  <div class="file-meta">取引日 {file_info['date']} / {file_info['partner']} / {file_info['month']} / {SERVICE_LABELS[file_info['service']]} / {products}</div>
                </div>
                <span class="pill completed">履歴</span>
              </div>
              <div class="card-actions">
                <a class="btn btn-soft" href="{file_info['slug']}">履歴を開く</a>
              </div>
            </div>
            """
        )
    return "".join(rows)


def history_page() -> str:
    cards = []
    for config in history_category_configs():
        dynamic_note = ""
        if config["key"] == "vendor_requests":
            dynamic_note = "保存完了した見積依頼書もここへ追加表示されます。"
        elif config["key"] == "client_returns":
            dynamic_note = "送付完了した買取明細書もここへ追加表示されます。"
        elif config["key"] == "client_estimate_requests":
            dynamic_note = "ユーザーから返送された見積依頼書を認証するとここへ追加表示されます。"
        cards.append(
            f"""
            <a class="flow-card history-category-card" href="{config['slug']}">
              <h2>{config['title']}</h2>
              <p>{config['copy']}</p>
              <span class="flow-count">この書類履歴を開く</span>
              {f'<p class="section-note">{dynamic_note}</p>' if dynamic_note else ''}
            </a>
            """
        )
    return page(
        "書類履歴",
        f"""
        <div class="page">
          <div class="page-head">
            <div>
              <h1>書類履歴</h1>
              <p>書類の種類ごとに履歴を分けて確認します。トップには全部を並べず、必要な書類カテゴリを開いて中身を確認する形です。</p>
            </div>
          </div>
          <div class="flow-grid">{''.join(cards)}</div>
        </div>
        """,
    )


def history_category_page(category: str) -> str:
    config = history_config(category)
    dynamic_filter = ""
    dynamic_title = "完了済み書類"
    dynamic_note = "PDF保存・Word保存・送付で完了にした書類がここに追加されます。履歴からPDF/Word/印刷もできます。"
    static_title = "保存済み履歴"
    static_note = "過去の書類を種類別に確認します。必要な書類だけをこのページ内で探せます。"
    if category == "client_requests":
        static_title = "依頼書履歴"
        static_note = "最初にクライアントから届いた、調査・査定のための申請依頼書です。"
    elif category == "client_estimate_requests":
        dynamic_filter = "client_estimate_request"
        dynamic_title = "認証済みの見積依頼書"
        dynamic_note = "4番で買取明細書を送付した後、ユーザー画面から見積依頼書として返送され、管理側で認証した書類がここに追加されます。"
        static_title = "過去の見積依頼書"
        static_note = "ユーザーから返送された見積依頼書の過去履歴です。"
    elif category == "vendor_requests":
        dynamic_filter = "vendor_outgoing"
    elif category == "client_returns":
        dynamic_filter = "client_outgoing"
    elif category == "settlement_statements":
        dynamic_filter = "settlement_statement"
        dynamic_title = "送付済みの仕切書"
        dynamic_note = "書類一覧の仕切書で月締め送付した書類がここに追加されます。"
    elif category == "calculation_statements":
        dynamic_filter = "calculation_statement"
        dynamic_title = "送付済みの計算書"
        dynamic_note = "代行仕入れの計算書を作成・送付した場合にここへ追加されます。"
    rows = history_rows_for_category(category)
    dynamic_block = ""
    if dynamic_filter:
        dynamic_block = f"""
          <div class="section">
            <div class="section-head">
              <div>
                <h3>{dynamic_title}</h3>
                <details class="compact-help">
                  <summary>説明を見る</summary>
                  <p>{dynamic_note}</p>
                </details>
              </div>
            </div>
            <div id="completed-doc-history" class="stack" data-completed-filter="{dynamic_filter}"></div>
            <p id="completed-doc-empty" class="section-note" hidden>完了済みの書類はまだありません。</p>
          </div>
        """
    return page(
        config["title"],
        f"""
        <div class="page">
          {history_back_bar()}
          <div class="page-head">
            <div>
              <h1>{config['title']}</h1>
              <p>{config['copy']}</p>
            </div>
          </div>
          {dynamic_block}
          <div class="section">
            <div class="section-head">
              <div>
                <h3>{static_title}</h3>
                <details class="compact-help">
                  <summary>説明を見る</summary>
                  <p>{static_note}</p>
                </details>
              </div>
            </div>
            <div class="filter-row">
              <label class="field search-field">
                <span>題名検索</span>
                <input id="history-title-search" type="search" placeholder="例：仕切書、見積依頼書、ブランドセンター">
              </label>
              <label class="field history-filter-field">
                <span>対象月</span>
                <input id="history-month-filter" type="month">
              </label>
              <label class="field history-filter-field">
                <span>開始日</span>
                <input id="history-date-from" type="date">
              </label>
              <label class="field history-filter-field">
                <span>終了日</span>
                <input id="history-date-to" type="date">
              </label>
              <button class="btn btn-outline btn-compact history-clear-btn" type="button" onclick="clearHistoryFilters()">絞り込みを解除</button>
            </div>
            <div class="stack">{rows}</div>
            <p id="history-empty" class="section-note" hidden>条件に合う履歴はありません。</p>
          </div>
        </div>
        """,
    )


def additional_history_entries() -> list[dict]:
    fallback_products = list(PRODUCTS.keys())
    stage1_product = fallback_products[0] if fallback_products else ""
    stage4_product = fallback_products[1] if len(fallback_products) > 1 else stage1_product
    vendor_product = fallback_products[2] if len(fallback_products) > 2 else stage1_product
    settlement_product = fallback_products[3] if len(fallback_products) > 3 else stage4_product
    return [
        {
            "slug": "documents_v2_history_stage1_client_incoming.html",
            "label": "2026/03 クライアント受付履歴",
            "date": "2026-03-28",
            "month": "2026/03",
            "partner": "クライアント受付",
            "service": "wholesale",
            "category": "client_requests",
            "items": [{"product": stage1_product, "price": 0, "result": "受付済み", "assigned_client": "テスト"}] if stage1_product else [],
        },
        {
            "slug": "documents_v2_history_stage2_vendor_request.html",
            "label": "2026/03 開花から業者への見積依頼書",
            "date": "2026-03-27",
            "month": "2026/03",
            "partner": "ブランドセンター",
            "service": "wholesale",
            "category": "vendor_requests",
            "items": [{"product": vendor_product, "price": PRODUCTS[vendor_product]["amount"], "result": "依頼済み", "assigned_client": "テスト"}] if vendor_product else [],
        },
        {
            "slug": "documents_v2_history_stage4_client_outgoing.html",
            "label": "2026/03 クライアント返送履歴",
            "date": "2026-03-26",
            "month": "2026/03",
            "partner": "クライアント返送",
            "service": "auction",
            "category": "client_returns",
            "items": [{"product": stage4_product, "price": PRODUCTS[stage4_product]["amount"], "result": "返送済み", "assigned_client": "テスト"}] if stage4_product else [],
        },
        {
            "slug": "documents_v2_history_client_estimate_request.html",
            "label": "2026/03 クライアント返送見積依頼書",
            "date": "2026-03-25",
            "month": "2026/03",
            "partner": "クライアント返送",
            "service": "wholesale",
            "category": "client_estimate_requests",
            "items": [{"product": stage4_product, "price": PRODUCTS[stage4_product]["amount"], "result": "見積依頼書受領", "assigned_client": "テスト"}] if stage4_product else [],
        },
        {
            "slug": "documents_v2_history_settlement_statement.html",
            "label": "2026/03 仕切書履歴",
            "date": "2026-03-24",
            "month": "2026/03",
            "partner": "クライアント仕切書",
            "service": "wholesale",
            "category": "settlement_statements",
            "statement_type": "settlement",
            "statement_lines": [
                {"name": "月額利用料", "qty": 1, "unit": 30000},
                {"name": "撮影・梱包・発送代行サポート費用", "qty": 1, "unit": 18000},
            ],
            "items": [{"product": settlement_product, "price": PRODUCTS[settlement_product]["amount"], "result": "発行済み", "assigned_client": "テスト"}] if settlement_product else [],
        },
        {
            "slug": "documents_v2_history_proxy_purchase_calculation.html",
            "label": "2026/03 代行仕入れ計算書履歴",
            "date": "2026-03-22",
            "month": "2026/03",
            "partner": "代行仕入れ計算書",
            "service": "proxy_purchase",
            "category": "calculation_statements",
            "items": [{"product": stage4_product, "price": PRODUCTS[stage4_product]["amount"], "result": "計算済み", "assigned_client": "テスト"}] if stage4_product else [],
        },
    ]


def vendor_file_page(file_info: dict) -> str:
    client_options = "".join(f'<option value="{escape(client["name"])}"></option>' for client in CLIENTS.values())
    nav = history_back_bar() if "history" in file_info.get("slug", "") else back_bar()
    preview_rows = "".join(
        f"""
        <tr>
          <td>{idx}</td>
          <td>{PRODUCTS[item['product']]['name']}</td>
          <td>{money(item['price'])}</td>
          <td>{item['result']}</td>
          <td>
            <input class="assign-input" id="assign-{item['product']}" list="client-options-{file_info['service']}" value="{escape(item['assigned_client'])}" placeholder="クライアント名で検索">
            <button class="btn btn-soft btn-mini" type="button" onclick="assignClient('assign-{item['product']}','assign-note-{item['product']}','{item['product']}','{escape(PRODUCTS[item['product']]['name'])}',{item['price']},'{escape(file_info['label'])}','{file_info['service']}')">4番へ反映</button>
            <p id="assign-note-{item['product']}" class="inline-note" hidden></p>
          </td>
        </tr>
        """
        for idx, item in enumerate(file_info["items"], start=1)
    )
    return page(
        file_info["label"],
        f"""
        <div class="page">
          {nav}
          <div class="page-head">
            <div>
              <h1>{file_info['label']}</h1>
              <p>{SERVICE_LABELS[file_info['service']]} として届いた書類を確認し、商品ごとにどのクライアントへ返送するかを決める画面です。</p>
            </div>
          </div>
          <div class="two-col">
            <div class="section">
              <div class="section-head">
                <div>
                  <h3>ファイルの中身を確認</h3>
                  <p class="section-note">登録したファイルを確認し、商品名・売却額・結果を見ながら、4番で振り分けるための元データを確認します。</p>
                </div>
              </div>
              <div class="file-preview">
                <div class="file-preview-head">回答ファイル プレビュー</div>
                <table class="simple-table">
                  <thead><tr><th>No.</th><th>商品名</th><th>売却額</th><th>結果</th><th>4番への反映先</th></tr></thead>
                  <tbody>{preview_rows}</tbody>
                </table>
                <datalist id="client-options-{file_info['service']}">{client_options}</datalist>
              </div>
            </div>
            <div class="section">
              <div class="section-head"><h3>次の作業</h3></div>
              <div class="compact-list">
                <div class="compact-item">
                  <strong>4番でクライアントへ返送する</strong>
                  <span>この書類の各商品をここで反映先へ登録すると、4番のクライアント返送候補に表示されます。</span>
                  <div class="card-actions">
                    <a class="btn btn-primary" href="documents_v2_client_outgoing_{file_info['service']}.html">4番の {SERVICE_LABELS[file_info['service']]} 一覧を開く</a>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </div>
        """,
    )


def return_group_title(config: dict) -> str:
    client = CLIENTS[config["client"]]
    return f"{client['name']} / {SERVICE_LABELS[config['service']]}"


def stage4_page(service: str = "all") -> str:
    return page(
        "4. クライアントへ返送",
        f"""
        <div class="page">
          {back_bar()}
          <div class="page-head">
            <div>
              <h1>4. クライアントへ返送</h1>
              <p>3番で振り分けた回答内容と、完了した案件をもとに、クライアント名ごと・サービス別に返送書類を作成します。1人に対してもサービスごとに別書類で返送する前提です。</p>
            </div>
          </div>
          {service_tabs("stage4", service)}
          <div class="filter-row">
            <label class="field search-field">
              <span>クライアント検索</span>
              <input id="client-outgoing-search" type="search" placeholder="クライアント名・クライアント番号・商品名で検索">
            </label>
          </div>
          <div class="section">
            <div class="section-head">
              <div>
                <h3>返送対象のクライアント一覧</h3>
                <p class="section-note">最初はクライアント名だけを一覧表示し、名前を開くと業者卸販売・業者オークション・同時出品の返送候補をサービス別に確認できる形です。</p>
              </div>
            </div>
            <div id="client-outgoing-groups" class="summary-grid" data-service-filter="{service}"></div>
            <p id="client-outgoing-empty" class="section-note" hidden>返送対象の商品はまだありません。3番で振り分けるか、完了したオークション・同時出品の商品があるとここへ表示されます。</p>
          </div>
        </div>
        """,
    )


def client_delivery_page() -> str:
    return page(
        "クライアント返送内容",
        f"""
        <div class="page">
          {back_bar()}
          <div class="page-head">
            <div>
              <h1 id="client-delivery-title">クライアント返送内容</h1>
              <p id="client-delivery-description">ここでどの商品を返送書類に含めるかを確認し、サービス別の買取明細書へ進みます。</p>
            </div>
          </div>
          <div class="section">
            <div class="section-head">
              <div>
                <h3>返送する商品</h3>
                <p class="section-note">キャンセル済みの商品は含めず、3番で反映済みの商品と完了済みの商品だけを確認して、サービス別の買取明細書を作成します。</p>
              </div>
            </div>
            <div id="client-delivery-items" class="stack"></div>
            <p id="client-delivery-empty" class="section-note" hidden>このクライアント / サービスで返送対象の商品はありません。</p>
            <div class="card-actions" style="margin-top: 18px;">
              <a id="client-delivery-template-link" class="btn btn-primary" href="documents_v2_client_statement_template.html">買取明細書を作成する</a>
            </div>
          </div>
        </div>
        """,
    )


def statement_template_page() -> str:
    return page(
        "クライアント向け買取明細書",
        f"""
        <div class="page page-narrow">
          <div class="doc-toolbar">
            <div class="doc-toolbar-copy">既存の買取明細書テンプレートに、クライアント返送対象の商品をサービス別で差し込んだ preview です。</div>
            <div class="doc-toolbar-actions">
              <a id="statement-back-link" class="btn btn-outline" href="documents_v2_client_delivery.html">返送一覧へ戻る</a>
              <button class="btn btn-soft" type="button" data-action="toggle-edit">書類を編集する</button>
              <button class="btn btn-outline" type="button" data-action="reset-doc">入力を元に戻す</button>
              <a id="statement-send-link" class="btn btn-primary" href="documents_v2_user_item_editor.html" onclick="completeCurrentDocument('client')">送付をする</a>
            </div>
          </div>
          <div class="doc-page" data-doc-editor>
            <div class="doc-title">買取明細書</div>
            <div class="doc-meta">
              <div><strong>発行日</strong> 2026年4月23日</div>
              <div><strong>宛名</strong> <span data-client-name>クライアント名</span> 様</div>
              <div><strong>発行者</strong> 株式会社開花</div>
              <div><strong>対象サービス</strong> <span data-client-service>サービス名</span></div>
            </div>
            <div class="doc-message">下記の通り、買取明細をご案内いたします。</div>
            <table class="doc-table">
              <thead>
                <tr>
                  <th class="doc-col-no">No.</th>
                  <th class="doc-col-name">商品名</th>
                  <th class="doc-col-brand">ブランド</th>
                  <th class="doc-col-condition">状態</th>
                  <th class="doc-col-qty">数量</th>
                  <th class="doc-col-price">単価</th>
                  <th class="doc-col-price">金額</th>
                </tr>
              </thead>
              <tbody id="client-statement-rows">{blank_doc_rows()}</tbody>
            </table>
            <div class="doc-total">
              <span>合計金額</span>
              <strong data-total-output>{money(0)}</strong>
            </div>
            <div class="doc-note">
              <strong>備考</strong>
              <p>必要に応じて、ここで備考や金額を修正してからクライアントへ返送する想定です。</p>
            </div>
            <p id="doc-save-notice" class="inline-notice" hidden></p>
          </div>
        </div>
        """,
        extra_scripts='<script src="static/doc_editor.js"></script>',
    )


def user_item_editor_page() -> str:
    return page(
        "ユーザー商品を編集する",
        """
        <div class="page">
          <div class="back-bar">
            <a class="btn btn-outline" id="user-item-back-link" href="documents_v2_client_statement_template.html">買取明細書へ戻る</a>
            <a class="btn btn-outline" href="documents_v2_index.html">書類一覧</a>
          </div>
          <div class="page-head">
            <div>
              <h1 id="user-item-editor-title">ユーザー商品を編集する</h1>
              <p id="user-item-editor-copy">送付後に、ユーザー商品一覧へ反映する内容をここで整えます。販売状況だけでなく、売却額・送料・メモも確認してから更新する前提です。</p>
            </div>
          </div>
          <div id="user-reflection-panel" class="section user-reflection-panel" hidden></div>
          <div class="section">
            <div class="section-head">
              <div>
                <h3>対象商品の状態編集</h3>
                <p class="section-note">送付した書類に含めた商品だけを表示します。ここで状態・売却額・送料・管理メモを更新し、そのままユーザー商品一覧を編集する想定です。</p>
              </div>
            </div>
            <div id="user-item-editor-items" class="stack"></div>
            <p id="user-item-editor-empty" class="section-note" hidden>編集対象の商品はありません。</p>
          </div>
        </div>
        """,
    )


def user_documents_page() -> str:
    return page(
        "ユーザー画面プレビュー",
        """
        <div class="page">
          <div class="back-bar">
            <a class="btn btn-outline" href="documents_v2_user_item_editor.html">ユーザー商品編集へ戻る</a>
            <a class="btn btn-outline" href="documents_v2_index.html">書類一覧</a>
          </div>
          <div class="page-head">
            <div>
              <h1 id="user-documents-title">ユーザー画面プレビュー</h1>
              <p id="user-documents-copy">買取明細書を送付した後、ユーザー側の書類一覧・通知・商品ページに反映される状態を確認します。</p>
            </div>
            <span id="user-documents-notification" class="notification-badge">通知 0</span>
          </div>
          <div class="section">
            <div class="section-head">
              <div>
                <h3>ユーザー書類一覧</h3>
                <p class="section-note">送付済みの買取明細書がここに表示される想定です。</p>
              </div>
            </div>
            <div id="user-documents-list" class="stack"></div>
            <p id="user-documents-empty" class="section-note" hidden>ユーザー側に届いている書類はまだありません。</p>
          </div>
          <div class="section">
            <div class="section-head">
              <div>
                <h3>商品ページへの反映</h3>
                <p class="section-note">送付対象の商品に、送付済み・完了・販売金額などが反映される想定です。</p>
              </div>
            </div>
            <div id="user-documents-products" class="stack"></div>
          </div>
        </div>
        """,
    )


def client_estimate_requests_page() -> str:
    return page(
        "クライアント返送見積依頼書",
        f"""
        <div class="page">
          {back_bar()}
          <div class="page-head">
            <div>
              <h1>クライアント返送見積依頼書</h1>
              <p>買取明細書を送付した後、ユーザー画面から見積依頼書として返送された書類を確認します。管理側で認証すると、書類履歴へ移動します。</p>
            </div>
          </div>
          <div class="section">
            <div class="section-head">
              <div>
                <h3>認証待ちの見積依頼書</h3>
                <p class="section-note">ユーザーが「見積依頼書として返送する」を押した書類がここに届く想定です。</p>
              </div>
            </div>
            <div id="pending-client-estimate-list" class="stack"></div>
            <p id="pending-client-estimate-empty" class="section-note" hidden>認証待ちの見積依頼書はありません。</p>
          </div>
        </div>
        """,
    )


def settlement_statements_page() -> str:
    return page(
        "仕切書",
        f"""
        <div class="page">
          {back_bar()}
          <div class="page-head">
            <div>
              <h1>仕切書</h1>
            </div>
            <div class="card-actions">
              <button class="btn btn-primary" type="button" onclick="sendAllSettlementStatements('settlement-bulk-notice')">全クライアントへ一括送付</button>
            </div>
          </div>
          <p id="settlement-bulk-notice" class="inline-notice" hidden></p>
          <div class="section">
            <div class="section-head">
              <h3>月締め対象</h3>
              <label class="field history-filter-field">
                <span>対象月</span>
                <input id="settlement-month-filter" type="month" value="2026-04">
              </label>
            </div>
            <div id="settlement-current-list" class="stack"></div>
          </div>
        </div>
        """,
    )


def calculation_statements_page() -> str:
    return page(
        "計算書",
        f"""
        <div class="page">
          {back_bar()}
          <div class="page-head">
            <div>
              <h1>計算書</h1>
              <p>代行仕入れを行った取引だけを計算書として表示します。通常の業者販売・オークション・同時出品はここには混ぜません。</p>
            </div>
          </div>
          <div class="section">
            <div class="section-head">
              <div>
                <h3>代行仕入れ履歴から送付する書類</h3>
                <p class="section-note">代行仕入れ対象の商品だけをクライアント別にまとめ、計算書を確認して送付する場所です。送付後はこの一覧から外れ、下の履歴へ移動します。</p>
              </div>
            </div>
            <div id="calculation-current-list" class="stack"></div>
            <p id="calculation-current-empty" class="section-note" hidden>現在、代行仕入れの書類送付対象はありません。</p>
          </div>
          <div class="section">
            <div class="section-head">
              <div>
                <h3>送付済み計算書履歴</h3>
                <p class="section-note">計算書を送付すると、商品一覧側の送付対象から消えて、ここに履歴として残ります。</p>
              </div>
            </div>
            <div id="completed-doc-history" class="stack" data-completed-filter="calculation_statement"></div>
            <p id="completed-doc-empty" class="section-note" hidden>送付済みの計算書はまだありません。</p>
          </div>
        </div>
        """,
    )


def generic_history_detail_page(file_info: dict) -> str:
    if file_info.get("statement_type") == "settlement":
        return settlement_statement_history_page(file_info)
    preview_rows = "".join(
        f"<tr><td>{idx}</td><td>{PRODUCTS[item['product']]['name']}</td><td>{money(item.get('price', 0))}</td><td>{item['result']}</td></tr>"
        for idx, item in enumerate(file_info["items"], start=1)
    )
    return page(
        file_info["label"],
        f"""
        <div class="page">
          {history_back_bar()}
          <div class="page-head">
            <div>
              <h1>{file_info['label']}</h1>
              <p>{file_info['partner']} の履歴書類です。書類履歴から中身を確認するための preview です。</p>
            </div>
          </div>
          <div class="section">
            <div class="section-head">
              <div>
                <h3>履歴書類の中身</h3>
                <p class="section-note">{file_info['month']} / {SERVICE_LABELS[file_info['service']]} / 過去履歴</p>
              </div>
              <div class="card-actions">
                <button class="btn btn-primary btn-compact" type="button" onclick="downloadElementAsPdf(document.querySelector('.file-preview'),'history_document.pdf')">PDF保存</button>
                <button class="btn btn-soft btn-compact" type="button" onclick="downloadElementAsWord(document.querySelector('.file-preview'),'history_document.doc')">Word保存</button>
                <button class="btn btn-outline btn-compact" type="button" onclick="printElement(document.querySelector('.file-preview'))">印刷</button>
              </div>
            </div>
            <div class="file-preview">
              <div class="file-preview-head">履歴書類プレビュー</div>
              <table class="simple-table">
                <thead><tr><th>No.</th><th>商品名</th><th>金額</th><th>結果</th></tr></thead>
                <tbody>{preview_rows}</tbody>
              </table>
            </div>
          </div>
        </div>
        """,
    )


def settlement_statement_history_page(file_info: dict) -> str:
    lines = file_info.get("statement_lines") or []
    subtotal = sum(int(line.get("qty", 1)) * int(line.get("unit", 0)) for line in lines)
    tax = int(round(subtotal * 0.1))
    total = subtotal + tax
    row_count = max(10, len(lines))
    rows = []
    for idx in range(row_count):
        line = lines[idx] if idx < len(lines) else None
        if line:
            qty = int(line.get("qty", 1))
            unit = int(line.get("unit", 0))
            amount = qty * unit
            rows.append(
                f"""
                <tr>
                  <td class="statement-col-no">{idx + 1}</td>
                  <td class="statement-col-desc">{line['name']}</td>
                  <td class="statement-col-qty">{qty}</td>
                  <td class="statement-col-price">{money(unit)}</td>
                  <td class="statement-col-price">{money(amount)}</td>
                </tr>
                """
            )
        else:
            rows.append(
                """
                <tr>
                  <td class="statement-col-no">&nbsp;</td>
                  <td class="statement-col-desc"></td>
                  <td class="statement-col-qty"></td>
                  <td class="statement-col-price"></td>
                  <td class="statement-col-price"></td>
                </tr>
                """
            )
    return page(
        file_info["label"],
        f"""
        <div class="page page-narrow">
          {history_back_bar()}
          <div class="doc-toolbar">
            <div class="doc-toolbar-copy">仕切書は、月額利用料と撮影・梱包・発送代行サポート費用をまとめた利用明細書型の書類として保存します。</div>
            <div class="doc-toolbar-actions">
              <button class="btn btn-primary" type="button" onclick="downloadElementAsPdf(document.querySelector('.doc-page'),'kaika_settlement_statement.pdf')">PDF保存</button>
              <button class="btn btn-soft" type="button" onclick="downloadElementAsWord(document.querySelector('.doc-page'),'kaika_settlement_statement.doc')">Word保存</button>
              <button class="btn btn-outline" type="button" onclick="printElement(document.querySelector('.doc-page'))">印刷</button>
            </div>
          </div>
          <div class="doc-page statement-doc">
            <div class="doc-title">仕切書</div>
            <div class="doc-subtitle">利用明細書</div>
            <div class="statement-header">
              <div>
                <div class="statement-recipient">テスト 様</div>
                <p>下記の通り、サービス利用料およびサポート費用をご請求いたします。</p>
              </div>
              <div class="statement-issuer">
                <strong>株式会社開花</strong>
                <span>〒000-0000 東京都中央区サンプル1-1-1</span>
                <span>TEL: 03-0000-0000</span>
                <span>登録番号: T0000000000000</span>
              </div>
            </div>
            <div class="doc-meta">
              <div><strong>発行日</strong> 2026年3月24日</div>
              <div><strong>対象月</strong> {file_info['month']}</div>
              <div><strong>書類番号</strong> ST-202603-001</div>
              <div><strong>支払期日</strong> 2026年4月30日</div>
              <div><strong>取引区分</strong> 利用明細・サポート費用</div>
              <div><strong>支払方法</strong> 指定口座振込</div>
            </div>
            <div class="statement-total-box">
              <span>ご請求金額</span>
              <strong>{money(total)}</strong>
            </div>
            <table class="doc-table statement-table">
              <thead>
                <tr>
                  <th class="statement-col-no">No.</th>
                  <th class="statement-col-desc">内容</th>
                  <th class="statement-col-qty">数量</th>
                  <th class="statement-col-price">単価</th>
                  <th class="statement-col-price">金額</th>
                </tr>
              </thead>
              <tbody>{''.join(rows)}</tbody>
            </table>
            <div class="statement-summary">
              <div><span>小計</span><strong>{money(subtotal)}</strong></div>
              <div><span>消費税 10%</span><strong>{money(tax)}</strong></div>
              <div class="statement-grand-total"><span>合計</span><strong>{money(total)}</strong></div>
            </div>
            <div class="doc-note">
              <strong>備考</strong>
              <p>月額利用料と、開花が行った撮影・梱包・発送代行サポート費用をまとめた仕切書です。実運用では税区分・登録番号・振込先を正式情報に差し替えて利用します。</p>
            </div>
          </div>
        </div>
        """,
    )


PREVIEW_CSS = """
*{box-sizing:border-box}
body{margin:0;font-family:"Noto Sans JP","Segoe UI",sans-serif;background:#f4f7fb;color:#1e293b}
a{color:inherit;text-decoration:none}
img{max-width:100%;display:block}
button,input,select,textarea{font:inherit}
.page{max-width:1240px;margin:0 auto;padding:24px 18px 40px}
.page.page-narrow{max-width:1000px}
.back-bar,.card-actions,.doc-toolbar-actions,.form-inline,.service-tabs,.history-tabs,.status-row,.filter-row{display:flex;gap:10px;flex-wrap:wrap}
.back-bar{margin-bottom:16px}
.page-head{display:flex;justify-content:space-between;align-items:flex-start;gap:16px;flex-wrap:wrap;margin-bottom:20px}
.page-head h1{margin:0;font-size:28px;line-height:1.35}
.page-head p{margin:8px 0 0;color:#64748b;line-height:1.8}
.flow-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}
.flow-card,.section,.summary-card,.product-card,.file-card{background:#fff;border:1px solid #dbe3ef;border-radius:20px;box-shadow:0 12px 28px rgba(15,23,42,.05)}
.flow-card{padding:18px}
.flow-step{display:inline-flex;align-items:center;justify-content:center;width:36px;height:36px;border-radius:999px;background:#dbeafe;color:#1d4ed8;font-weight:800;font-size:14px;margin-bottom:12px}
.flow-card h2{margin:0;font-size:18px;line-height:1.45}
.flow-card p{margin:8px 0 12px;color:#64748b;font-size:14px;line-height:1.8}
.flow-count,.pill{display:inline-flex;align-items:center;justify-content:center;border-radius:999px;font-size:12px;font-weight:800;white-space:nowrap}
.flow-count{padding:7px 12px;background:#eff6ff;color:#1d4ed8}
.section{padding:20px;margin-bottom:18px}
.section-head{display:flex;justify-content:space-between;align-items:center;gap:14px;flex-wrap:wrap;margin-bottom:16px}
.section-head h3{margin:0;font-size:20px}
.section-note{margin:6px 0 0;color:#64748b;font-size:14px;line-height:1.8}
.summary-grid,.stack{display:grid;gap:16px}
.summary-card{padding:18px}
.summary-head,.origin-head,.product-top,.file-head{display:flex;justify-content:space-between;gap:14px;align-items:flex-start;flex-wrap:wrap}
.summary-client,.client-title{font-size:20px;font-weight:800;line-height:1.45}
.file-title{font-size:15px;font-weight:800;line-height:1.5;word-break:break-word}
.summary-meta,.product-meta,.origin-meta,.file-meta{margin-top:4px;color:#64748b;font-size:13px;line-height:1.7}
.summary-card-grid,.detail-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px;margin-top:12px}
.summary-footer{display:grid;grid-template-columns:minmax(0,1fr) auto;align-items:center;gap:12px;margin-top:14px}
.summary-card details{margin-top:14px}
.summary-card details summary{display:inline-flex;justify-content:space-between;align-items:center;gap:12px;cursor:pointer;list-style:none;padding:9px 12px;border-radius:14px;background:#f8fafc;border:1px solid #e2e8f0;font-weight:700;max-width:100%;font-size:13px}
.summary-card details summary::-webkit-details-marker{display:none}
.summary-card details[open] summary{background:#eff6ff;border-color:#bfdbfe;color:#1d4ed8}
.summary-card .service-detail-body{padding-top:12px}
.service-chip-row{display:flex;gap:8px;flex-wrap:wrap;margin-top:14px}
.summary-footer .service-chip-row{margin-top:0}
.summary-actions{margin-top:0}
.summary-chip-row{min-width:0}
.summary-action-row{justify-content:flex-end}
.summary-action-row .btn{white-space:nowrap;padding:8px 12px;font-size:13px}
.stage-action-top{justify-content:flex-end;align-items:center}
.stage-action-top .btn{white-space:nowrap}
.service-chip{display:inline-flex;align-items:center;justify-content:center;padding:6px 10px;border-radius:999px;font-size:12px;font-weight:800;white-space:nowrap}
.service-chip-wholesale{background:#eff6ff;color:#1d4ed8}
.service-chip-auction{background:#fef3c7;color:#92400e}
.service-chip-simultaneous{background:#ecfdf5;color:#047857}
.service-panel{display:grid;gap:14px;margin-bottom:18px}
.service-panel:last-child{margin-bottom:0}
.service-panel-head{display:flex;justify-content:space-between;align-items:flex-start;gap:12px;flex-wrap:wrap}
.service-panel-head h4{margin:0;font-size:18px;line-height:1.45}
.service-panel-head p{margin:6px 0 0;color:#64748b;font-size:14px;line-height:1.8}
.service-panel-count{display:inline-flex;align-items:center;justify-content:center;padding:7px 12px;border-radius:999px;background:#eef2ff;color:#4338ca;font-size:12px;font-weight:800;white-space:nowrap}
.field-block,.field{display:grid;gap:6px}
.field-label{font-size:12px;font-weight:700;color:#64748b;letter-spacing:.02em}
.field-value{font-size:14px;line-height:1.8;color:#0f172a}
.btn{display:inline-flex;align-items:center;justify-content:center;gap:8px;border-radius:12px;padding:10px 14px;font-size:14px;font-weight:700;border:1px solid transparent;cursor:pointer;line-height:1.35;max-width:100%}
.btn-compact{padding:8px 12px;font-size:13px;min-height:38px}
.btn-mini{padding:6px 9px;font-size:12px;border-radius:10px;margin-top:7px}
.btn-primary{background:#2563eb;color:#fff}
.btn-soft{background:#eff6ff;color:#1d4ed8;border-color:#bfdbfe}
.btn-outline{background:#fff;color:#334155;border-color:#cbd5e1}
.pill.pending{background:#e2e8f0;color:#334155;padding:6px 10px}
.pill.approved{background:#dcfce7;color:#166534;padding:6px 10px}
.pill.appraising{background:#fef3c7;color:#92400e;padding:6px 10px}
.pill.listing{background:#dbeafe;color:#1d4ed8;padding:6px 10px}
.pill.payment{background:#dbeafe;color:#1d4ed8;padding:6px 10px}
.pill.completed{background:#ede9fe;color:#6d28d9;padding:6px 10px}
.pill.unavailable{background:#fee2e2;color:#b91c1c;padding:6px 10px}
.service-tabs{margin-bottom:18px}
.service-tab{display:inline-flex;align-items:center;padding:10px 14px;border-radius:999px;border:1px solid #cbd5e1;background:#fff;color:#334155;font-size:13px;font-weight:700}
.service-tab.is-active{background:#1d4ed8;color:#fff;border-color:#1d4ed8}
.service-tab-count{display:inline-flex;align-items:center;justify-content:center;min-width:24px;height:24px;padding:0 8px;margin-left:8px;border-radius:999px;background:rgba(255,255,255,.75);color:inherit;font-size:11px;font-weight:800}
.service-tab.is-active .service-tab-count{background:rgba(255,255,255,.22);color:#fff}
.history-tabs{margin-bottom:14px}
.history-tab{display:inline-flex;align-items:center;justify-content:center;padding:9px 12px;border-radius:999px;border:1px solid #cbd5e1;background:#fff;color:#334155;font-size:13px;font-weight:700;cursor:pointer}
.history-tab.is-active{background:#0f172a;color:#fff;border-color:#0f172a}
.notification-badge{display:inline-flex;align-items:center;justify-content:center;border-radius:999px;background:#ef4444;color:#fff;padding:8px 12px;font-size:13px;font-weight:800;white-space:nowrap}
.user-reflection-panel{border-color:#bfdbfe;background:linear-gradient(135deg,#ffffff 0%,#eff6ff 100%)}
.owner-cancel-box{display:flex;align-items:flex-end;gap:10px;flex-wrap:wrap;margin-top:12px;padding:12px;border-radius:14px;background:#fff7ed;border:1px solid #fed7aa}
.owner-cancel-box .field{min-width:160px}
.product-card{display:grid;grid-template-columns:156px minmax(0,1fr);gap:16px;padding:14px}
.file-card{padding:16px}
.settlement-client-card{display:block;padding:0;overflow:hidden}
.settlement-client-card>summary{list-style:none;cursor:pointer;padding:16px;display:grid;grid-template-columns:minmax(0,1fr) auto;gap:14px;align-items:center}
.settlement-client-card>summary::-webkit-details-marker{display:none}
.settlement-client-card[open]>summary{border-bottom:1px solid #e2e8f0;background:#f8fafc}
.settlement-client-summary-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px;margin-top:10px}
.settlement-client-detail{padding:16px;display:grid;gap:14px}
.settlement-doc-actions{justify-content:flex-end;margin-top:0}
.settlement-detail-overview{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px}
.settlement-product-list{display:grid;gap:8px}
.settlement-product-line{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:10px;align-items:center;padding:10px 12px;border-radius:12px;background:#f8fafc;border:1px solid #e2e8f0}
.settlement-product-line strong{font-size:14px;line-height:1.5}
.settlement-product-line span{color:#64748b;font-size:12px;line-height:1.6}
.document-toggle{border:1px solid #e2e8f0;border-radius:18px;background:#fff;overflow:hidden}
.document-toggle>summary{list-style:none;display:flex;align-items:center;justify-content:space-between;gap:12px;cursor:pointer;padding:12px 14px;font-weight:800;background:#f8fafc}
.document-toggle>summary::-webkit-details-marker{display:none}
.document-toggle[open]>summary{border-bottom:1px solid #e2e8f0;background:#eff6ff;color:#1d4ed8}
.product-thumb{height:126px;border-radius:16px;background:#f8fafc;border:1px solid #e2e8f0;display:flex;align-items:center;justify-content:center;overflow:hidden}
.product-thumb img{width:100%;height:100%;object-fit:contain}
.status-box{margin-top:14px;padding:14px;border-radius:16px;background:#f8fbff;border:1px solid #dbeafe}
.status-row-actions{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.status-row-actions .btn{white-space:nowrap;flex:0 0 auto}
.status-select{min-width:160px;padding:10px 12px;border-radius:10px;border:1px solid #cbd5e1;background:#fff}
.memo-action-row{justify-content:flex-end;margin-top:8px}
.inline-notice,.inline-note{margin:8px 0 0;font-size:13px;line-height:1.8;color:#475569}
.filter-row{margin-bottom:16px;align-items:flex-end}
.search-field{min-width:280px;max-width:420px}
.history-filter-field{min-width:150px;max-width:190px}
.history-clear-btn{align-self:flex-end;min-height:40px;height:40px;padding:8px 12px}
.compact-help{margin-top:6px}
.compact-help summary{display:inline-flex;align-items:center;cursor:pointer;border:1px solid #cbd5e1;border-radius:999px;background:#fff;padding:5px 10px;font-size:12px;font-weight:800;color:#475569;list-style:none}
.compact-help summary::-webkit-details-marker{display:none}
.compact-help p{margin:8px 0 0;color:#64748b;font-size:13px;line-height:1.7}
.two-col{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:18px}
.compact-list{display:grid;gap:10px}
.compact-item{display:grid;gap:4px;padding:12px 14px;border-radius:14px;background:#f8fafc;border:1px solid #e2e8f0;min-width:0}
.form-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}
.field input,.field select,.field textarea{width:100%;padding:11px 12px;border-radius:12px;border:1px solid #cbd5e1;background:#fff}
.field-wide{grid-column:1 / -1}
.select-grid,.origin-items{display:grid;gap:12px}
.select-row{display:grid;grid-template-columns:auto minmax(0,1fr);gap:10px;align-items:flex-start;padding:12px 14px;border-radius:16px;border:1px solid #e2e8f0;background:#fff}
.select-main{font-weight:700}
.select-sub{grid-column:2;color:#64748b;font-size:13px}
.mini-panel{margin-top:14px;padding:12px 14px;border-radius:16px;background:#f8fbff;border:1px solid #dbeafe}
.doc-toolbar{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;padding:12px 14px;margin:0 0 18px;border:1px solid #dbeafe;border-radius:16px;background:linear-gradient(135deg,#f8fbff 0%,#eef5ff 100%)}
.doc-toolbar-copy{color:#475569;font-size:13px;line-height:1.7;max-width:560px}
.doc-page{width:210mm;min-height:297mm;margin:0 auto;background:#fff;padding:14mm 12mm;box-shadow:0 16px 30px rgba(15,23,42,.08);border:1px solid #e2e8f0}
.doc-title{text-align:center;font-size:26px;font-weight:800;letter-spacing:.12em;margin-bottom:14px}
.doc-subtitle{text-align:center;font-size:13px;color:#475569;letter-spacing:.28em;margin:-6px 0 16px}
.doc-meta{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;margin-bottom:14px;font-size:13px;line-height:1.8}
.doc-message{margin-bottom:14px;font-size:14px;line-height:1.9}
.doc-table{width:100%;border-collapse:collapse;table-layout:fixed;font-size:12px}
.doc-table th,.doc-table td{border:1px solid #334155;padding:6px 6px;vertical-align:top;line-height:1.55}
.doc-table thead th{background:#f8fafc}
.doc-col-no{width:5%}
.doc-col-name{width:34%}
.doc-col-brand{width:16%}
.doc-col-condition{width:10%}
.doc-col-qty{width:8%;text-align:center}
.doc-col-price{width:13%;text-align:right}
.doc-total{margin-top:14px;display:flex;justify-content:flex-end;align-items:center;gap:16px;font-size:16px;font-weight:700}
.doc-total strong{font-size:28px;line-height:1.2}
.doc-note{margin-top:16px;padding-top:10px;border-top:1px solid #cbd5e1;font-size:13px;line-height:1.8}
.doc-editable{outline:none}
.is-edit-mode .doc-editable{outline:2px dashed #93c5fd;background:#eff6ff}
.document-scroll{margin-top:10px;padding:12px;border:1px solid #e2e8f0;border-radius:18px;background:#f8fafc;overflow:auto}
.settlement-doc-page{box-shadow:none;border-color:#cbd5e1}
.settlement-print-actions{margin-top:10px;justify-content:flex-end}
.statement-detail-sheet{margin-top:18mm;padding-top:10mm;border-top:1px dashed #94a3b8}
.statement-doc{padding:15mm 13mm}
.statement-header{display:grid;grid-template-columns:minmax(0,1fr) 72mm;gap:18px;margin-bottom:14px;align-items:start}
.statement-recipient{display:inline-block;min-width:72mm;border-bottom:1px solid #334155;font-size:20px;font-weight:800;padding-bottom:8px;margin-bottom:12px}
.statement-header p{margin:0;color:#475569;line-height:1.8;font-size:13px}
.statement-issuer{display:grid;gap:4px;padding:12px;border:1px solid #cbd5e1;border-radius:10px;background:#f8fafc;font-size:12px;line-height:1.55}
.statement-total-box{display:flex;justify-content:space-between;align-items:flex-end;gap:14px;margin:14px 0;padding:12px 16px;border:2px solid #0f172a;background:#f8fafc}
.statement-total-box span{font-weight:800;font-size:15px}
.statement-total-box strong{font-size:30px;line-height:1.1}
.statement-table{font-size:12px}
.statement-col-no{width:7%;text-align:center}
.statement-col-desc{width:49%}
.statement-col-qty{width:10%;text-align:center}
.statement-col-price{width:17%;text-align:right}
.statement-summary{width:72mm;margin:12px 0 0 auto;display:grid;border:1px solid #334155;border-bottom:0;font-size:13px}
.statement-summary div{display:flex;justify-content:space-between;gap:10px;border-bottom:1px solid #334155;padding:8px 10px}
.statement-grand-total{background:#f8fafc;font-size:15px}
.file-preview{border:1px solid #e2e8f0;border-radius:18px;background:#fff;overflow:hidden}
.file-preview-head{padding:12px 14px;background:#0f172a;color:#fff;font-weight:700}
.simple-table{width:100%;border-collapse:collapse;font-size:13px}
.simple-table th,.simple-table td{border:1px solid #e2e8f0;padding:10px 12px;vertical-align:top}
.assign-input{width:100%;padding:9px 10px;border-radius:10px;border:1px solid #cbd5e1}
.form-inline{align-items:flex-end}
.form-inline .field{min-width:180px;flex:1 1 190px}
.form-inline .file-field{flex:1 1 220px}
.form-inline .btn{flex:0 0 auto}
.filter-row>.btn{flex:0 0 auto;padding:9px 13px;font-size:13px}
.filter-row>.btn.history-clear-btn{height:40px;min-height:40px;padding:8px 12px}
.user-edit-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px;margin-top:12px}
.user-edit-grid .field{min-width:0}
.user-edit-grid .field input,.user-edit-grid .field textarea,.user-edit-grid .field select{width:100%;padding:10px 12px;border-radius:10px;border:1px solid #cbd5e1;background:#fff}
.user-edit-grid .field textarea{min-height:88px;resize:vertical}
.detail-layout{display:grid;grid-template-columns:320px minmax(0,1fr);gap:18px}
.detail-image-card{background:#fff;border:1px solid #e2e8f0;border-radius:20px;padding:18px;display:flex;align-items:center;justify-content:center}
.detail-image-card img{max-height:320px;object-fit:contain}
.detail-list{display:grid;gap:10px}
.detail-row{display:grid;grid-template-columns:150px minmax(0,1fr);gap:12px;padding:10px 0;border-bottom:1px solid #e2e8f0}
.note-row td{background:#f8fafc}
@media (max-width: 1024px){
  .two-col,.detail-layout,.product-card,.summary-card-grid,.detail-grid,.form-grid,.user-edit-grid,.settlement-detail-overview,.settlement-client-summary-grid{grid-template-columns:1fr}
  .doc-page{width:100%;min-height:auto;padding:24px}
  .summary-footer{grid-template-columns:1fr}
  .status-row-actions{display:flex;grid-template-columns:none}
  .stage-action-top{justify-content:flex-start}
}
@media (max-width: 720px){
  .page{padding:20px 14px 36px}
  .flow-grid{grid-template-columns:1fr}
  .summary-client,.client-title,.file-title{font-size:18px}
  .product-thumb{height:120px}
  .doc-meta{grid-template-columns:1fr}
}
"""


PREVIEW_JS = """
const PREVIEW_STORAGE_KEY = "documentsPreviewStateV6";

function previewData() {
  return window.DOCUMENTS_PREVIEW_DATA || { clients: {}, products: {}, vendors: [], responseFiles: [], serviceLabels: {}, monthlyPlanSettings: {} };
}

function normalizeState(raw) {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
    return { items: {}, assignments: {}, vendorDraft: null, completedDocs: {} };
  }
  if (raw.items || raw.assignments || Object.prototype.hasOwnProperty.call(raw, "vendorDraft") || raw.completedDocs) {
    return {
      items: raw.items || {},
      assignments: raw.assignments || {},
      vendorDraft: raw.vendorDraft || null,
      completedDocs: raw.completedDocs || {},
    };
  }
  return { items: raw, assignments: {}, vendorDraft: null, completedDocs: {} };
}

function loadPreviewState() {
  try {
    return normalizeState(JSON.parse(window.localStorage.getItem(PREVIEW_STORAGE_KEY) || "{}"));
  } catch (error) {
    return { items: {}, assignments: {}, vendorDraft: null, completedDocs: {} };
  }
}

function savePreviewState(state) {
  window.localStorage.setItem(PREVIEW_STORAGE_KEY, JSON.stringify(state));
}

function getItemState(productId, fallbackStatus = "認証待ち") {
  const state = loadPreviewState();
  return state.items[productId] || { status: fallbackStatus, unavailable: false };
}

function setItemState(productId, patch) {
  const state = loadPreviewState();
  const current = state.items[productId] || {};
  state.items[productId] = { ...current, ...patch };
  savePreviewState(state);
  return state.items[productId];
}

function setVendorDraft(vendorDraft) {
  const state = loadPreviewState();
  state.vendorDraft = vendorDraft;
  savePreviewState(state);
}

function getVendorDraft() {
  return loadPreviewState().vendorDraft || null;
}

function setAssignment(productId, assignment) {
  const state = loadPreviewState();
  state.assignments[productId] = assignment;
  savePreviewState(state);
}

function getAssignments() {
  return loadPreviewState().assignments || {};
}

function getCompletedDocs() {
  return loadPreviewState().completedDocs || {};
}

function setCompletedDoc(doc) {
  const state = loadPreviewState();
  state.completedDocs = state.completedDocs || {};
  state.completedDocs[doc.id] = doc;
  savePreviewState(state);
  return doc;
}

function removeCompletedDoc(docId) {
  const state = loadPreviewState();
  const doc = state.completedDocs?.[docId];
  if (!doc) return null;
  delete state.completedDocs[docId];
  savePreviewState(state);
  return doc;
}

function defaultAssignments() {
  const assignments = {};
  (previewData().responseFiles || []).forEach((fileInfo) => {
    (fileInfo.items || []).forEach((item) => {
      const selected = findClientByName(item.assigned_client || "");
      if (!selected) return;
      const [clientId, client] = selected;
      assignments[item.product] = {
        clientId,
        clientName: client.name,
        service: fileInfo.service,
        source: fileInfo.label,
        price: item.price,
      };
    });
  });
  return assignments;
}

function getQueryParam(name) {
  const params = new URLSearchParams(window.location.search);
  return params.get(name) || "";
}

function showInlineNotice(id, message) {
  const target = document.getElementById(id);
  if (!target) return;
  target.textContent = message;
  target.hidden = false;
}

function applySearchFilter(inputId, selector, emptyId) {
  const input = document.getElementById(inputId);
  const query = (input?.value || "").trim().toLowerCase();
  const cards = Array.from(document.querySelectorAll(selector));
  let visibleCount = 0;
  cards.forEach((card) => {
    const baseVisible = (card.dataset.baseVisible || "true") === "true";
    const haystack = (card.dataset.search || card.textContent || "").toLowerCase();
    const matches = !query || haystack.includes(query);
    const visible = baseVisible && matches;
    card.style.display = visible ? "" : "none";
    if (visible) visibleCount += 1;
  });
  const empty = document.getElementById(emptyId);
  if (empty) empty.hidden = visibleCount > 0;
}

function applyHistoryFilters() {
  const query = (document.getElementById("history-title-search")?.value || "").trim().toLowerCase();
  const month = document.getElementById("history-month-filter")?.value || "";
  const from = document.getElementById("history-date-from")?.value || "";
  const to = document.getElementById("history-date-to")?.value || "";
  const cards = Array.from(document.querySelectorAll(".js-history-card"));
  let visibleCount = 0;
  cards.forEach((card) => {
    const haystack = (card.dataset.search || card.textContent || "").toLowerCase();
    const date = card.dataset.date || "";
    const cardMonth = card.dataset.month || "";
    const matchesText = !query || haystack.includes(query);
    const matchesMonth = !month || cardMonth === month;
    const matchesFrom = !from || date >= from;
    const matchesTo = !to || date <= to;
    const visible = matchesText && matchesMonth && matchesFrom && matchesTo;
    card.style.display = visible ? "" : "none";
    if (visible) visibleCount += 1;
  });
  const empty = document.getElementById("history-empty");
  if (empty) empty.hidden = visibleCount > 0;
}

function clearHistoryFilters() {
  ["history-title-search", "history-month-filter", "history-date-from", "history-date-to"].forEach((id) => {
    const input = document.getElementById(id);
    if (input) input.value = "";
  });
  applyHistoryFilters();
}

function statusClass(label) {
  switch (label) {
    case "認証待ち": return "pending";
    case "認証済み": return "approved";
    case "査定中": return "appraising";
    case "出品中": return "listing";
    case "入金待ち": return "payment";
    case "完了": return "completed";
    case "受付不可": return "unavailable";
    default: return "approved";
  }
}

function serviceLabel(service) {
  return previewData().serviceLabels?.[service] || service;
}

function computeStageCounts(stage) {
  const counts = { all: 0, wholesale: 0, auction: 0, simultaneous: 0 };
  const products = Object.entries(previewData().products || {});
  if (stage === "stage1") {
    products.forEach(([productId, product]) => {
      const current = getItemState(productId, product.status || "認証待ち");
      if (current.unavailable) return;
      counts[product.service] += 1;
    });
  } else if (stage === "stage2") {
    products.forEach(([productId, product]) => {
      const current = getItemState(productId, product.status || "認証待ち");
      if (current.unavailable || current.stage2Completed) return;
      const expected = product.service === "simultaneous" ? "出品中" : "査定中";
      if (current.status === expected) {
        counts[product.service] += 1;
      }
    });
  } else if (stage === "stage3") {
    (previewData().responseFiles || []).forEach((fileInfo) => {
      counts[fileInfo.service] += 1;
    });
  } else if (stage === "stage4") {
    buildReturnGroups().forEach((group) => {
      counts[group.service] += 1;
    });
  }
  counts.all = counts.wholesale + counts.auction + counts.simultaneous;
  return counts;
}

function updateStageTabCounts() {
  const tabs = Array.from(document.querySelectorAll("[data-stage-tab][data-service-key]"));
  if (!tabs.length) return;
  const cache = {};
  tabs.forEach((tab) => {
    const stage = tab.dataset.stageTab;
    const key = tab.dataset.serviceKey;
    if (!cache[stage]) cache[stage] = computeStageCounts(stage);
    const countNode = tab.querySelector(".service-tab-count");
    if (countNode) countNode.textContent = cache[stage][key] ?? 0;
  });
}

function formatYen(value) {
  const amount = Number(value) || 0;
  return `¥${amount.toLocaleString("ja-JP")}`;
}

function monthlyPlanFromItemCount(itemCount) {
  const settings = previewData().monthlyPlanSettings || {};
  const count = Number(itemCount) || 0;
  const planFor = (suffix, fallbackPlan, fallbackFee, rangeLabel) => {
    const feeValue = settings[`monthly_fee_${suffix}`];
    const fee = Number(feeValue ?? fallbackFee) || 0;
    return {
      plan: settings[`monthly_plan_${suffix}`] || fallbackPlan,
      fee,
      range: rangeLabel,
      itemCount: count,
      isCustom: suffix === "over" && fee <= 0,
    };
  };
  if (count <= 20) return planFor("20", "ライト", 2980, "0〜20商品");
  if (count <= 50) return planFor("50", "スタンダード", 5980, "21〜50商品");
  if (count <= 100) return planFor("100", "プロ", 9800, "51〜100商品");
  if (count <= 300) return planFor("300", "ビジネス", 19800, "101〜300商品");
  return planFor("over", "エンタープライズ", 0, "301商品以上");
}

function formatMonthlyPlanFee(data) {
  return data?.monthlyPlanCustom ? "個別相談" : formatYen(data?.monthlyFee || data?.fee || 0);
}

function shortProductName(value, limit = 18) {
  const clean = String(value || "").trim();
  if (!clean) return "商品名未設定";
  return clean.length > limit ? `${clean.slice(0, limit)}…` : clean;
}

function settlementSupportFeeFromSale(saleAmount) {
  const amount = Number(saleAmount) || 0;
  if (amount <= 0) return 0;
  if (amount <= 19999) return 500;
  if (amount <= 49999) return 650;
  if (amount <= 99999) return 800;
  return 1000;
}

function settlementSupportTierLabel(saleAmount) {
  const amount = Number(saleAmount) || 0;
  if (amount <= 0) return "売上未入力";
  if (amount <= 19999) return "2万円以下";
  if (amount <= 49999) return "5万円以下";
  if (amount <= 99999) return "10万円以下";
  return "10万円超";
}

function sanitizeFilename(value, fallback = "document") {
  const base = String(value || fallback).replace(/[\\/:*?"<>|]/g, "_").replace(/\\s+/g, "_").slice(0, 80);
  return base || fallback;
}

async function downloadElementAsPdf(element, filename) {
  if (!element) return false;
  if (!window.html2canvas || !window.jspdf?.jsPDF) {
    alert("PDF作成ライブラリの読み込み中です。数秒待ってからもう一度押してください。");
    return false;
  }
  const canvas = await window.html2canvas(element, {
    scale: 2,
    useCORS: true,
    backgroundColor: "#ffffff",
    scrollX: 0,
    scrollY: 0,
  });
  const pdf = new window.jspdf.jsPDF("p", "mm", "a4");
  const pageWidth = 210;
  const pageHeight = 297;
  const imgWidth = pageWidth;
  let imgHeight = (canvas.height * imgWidth) / canvas.width;
  const imgData = canvas.toDataURL("image/jpeg", 0.96);
  if (imgHeight <= pageHeight * 1.12) {
    pdf.addImage(imgData, "JPEG", 0, 0, imgWidth, pageHeight);
  } else {
    let heightLeft = imgHeight;
    let position = 0;
    pdf.addImage(imgData, "JPEG", 0, position, imgWidth, imgHeight);
    heightLeft -= pageHeight;
    while (heightLeft > 2) {
      position = heightLeft - imgHeight;
      pdf.addPage();
      pdf.addImage(imgData, "JPEG", 0, position, imgWidth, imgHeight);
      heightLeft -= pageHeight;
    }
  }
  const blob = pdf.output("blob");
  await saveBlobWithPicker(blob, filename, "application/pdf", "PDFファイル");
  return true;
}

async function saveBlobWithPicker(blob, filename, mimeType, description) {
  if (window.showSaveFilePicker) {
    const extension = filename.includes(".") ? filename.split(".").pop() : "";
    const handle = await window.showSaveFilePicker({
      suggestedName: filename,
      types: [{ description, accept: { [mimeType]: extension ? [`.${extension}`] : [] } }],
    });
    const writable = await handle.createWritable();
    await writable.write(blob);
    await writable.close();
    return true;
  }
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
  return true;
}

async function downloadElementAsWord(element, filename) {
  if (!element) return false;
  const html = `<!DOCTYPE html><html><head><meta charset="UTF-8"><title>${filename}</title><style>
    body{font-family:"Noto Sans JP","Yu Gothic",sans-serif;color:#111827}
    table{width:100%;border-collapse:collapse;font-size:12px}
    th,td{border:1px solid #111827;padding:6px;vertical-align:top}
    .doc-title{text-align:center;font-size:24px;font-weight:700;margin-bottom:16px}
    .doc-meta{display:grid;grid-template-columns:repeat(2,1fr);gap:8px;margin-bottom:14px}
    .doc-total{text-align:right;font-size:18px;font-weight:700;margin-top:16px}
  </style></head><body>${element.outerHTML}</body></html>`;
  const blob = new Blob(["\ufeff", html], { type: "application/msword" });
  await saveBlobWithPicker(blob, filename, "application/msword", "Wordファイル");
  return true;
}

async function downloadDocumentAsWord(filename) {
  return downloadElementAsWord(document.querySelector(".doc-page"), filename);
}

function printElement(element) {
  if (!element) return false;
  const popup = window.open("", "_blank");
  if (!popup) {
    alert("印刷用のウィンドウを開けませんでした。ポップアップ許可を確認してください。");
    return false;
  }
  popup.document.write(`<!DOCTYPE html><html><head><meta charset="UTF-8"><title>印刷</title><link rel="stylesheet" href="static/preview_v2.css"></head><body>${element.outerHTML}</body></html>`);
  popup.document.close();
  popup.focus();
  window.setTimeout(() => popup.print(), 300);
  return true;
}

function prepareSettlementEditableNodes(clientId) {
  const roots = Array.from(document.querySelectorAll(`[data-settlement-doc-group="${clientId}"]`));
  const nodes = roots.flatMap((root) => Array.from(root.querySelectorAll(".doc-title, .doc-meta div, .doc-table tbody td, .doc-total span, .doc-total strong, .doc-note p")));
  nodes.forEach((node) => {
    if (!node.dataset.initialHtml) node.dataset.initialHtml = node.innerHTML;
    node.classList.add("doc-editable");
    if (!node.hasAttribute("contenteditable")) node.setAttribute("contenteditable", "false");
  });
  return { roots, nodes };
}

function toggleSettlementDocumentEdit(clientId, buttonId) {
  const { roots, nodes } = prepareSettlementEditableNodes(clientId);
  if (!roots.length) return;
  const editing = !roots.some((root) => root.classList.contains("is-edit-mode"));
  roots.forEach((root) => root.classList.toggle("is-edit-mode", editing));
  nodes.forEach((node) => node.setAttribute("contenteditable", editing ? "true" : "false"));
  const button = document.getElementById(buttonId);
  if (button) button.textContent = editing ? "訂正を終了する" : "2書類を訂正する";
}

function resetSettlementDocumentEdit(clientId) {
  const { roots, nodes } = prepareSettlementEditableNodes(clientId);
  nodes.forEach((node) => {
    node.innerHTML = node.dataset.initialHtml || "";
    node.setAttribute("contenteditable", "false");
  });
  roots.forEach((root) => root.classList.remove("is-edit-mode"));
  const button = document.getElementById(`settlement-edit-${clientId}`);
  if (button) button.textContent = "2書類を訂正する";
}

function captureSettlementDocumentHtml(clientId) {
  const { roots, nodes } = prepareSettlementEditableNodes(clientId);
  nodes.forEach((node) => node.setAttribute("contenteditable", "false"));
  roots.forEach((root) => root.classList.remove("is-edit-mode"));
  const main = document.getElementById(`settlement-main-${clientId}`);
  const detail = document.getElementById(`settlement-detail-${clientId}`);
  const clean = (element) => {
    if (!element) return "";
    const clone = element.cloneNode(true);
    clone.classList.remove("is-edit-mode");
    clone.querySelectorAll("[contenteditable]").forEach((node) => node.setAttribute("contenteditable", "false"));
    return clone.outerHTML;
  };
  return {
    mainDocumentHtml: clean(main),
    detailDocumentHtml: clean(detail),
  };
}

async function downloadElementIdAsPdf(elementId) {
  const element = document.getElementById(elementId);
  if (!element) {
    alert("PDF化する書類が見つかりません。");
    return false;
  }
  const filename = element.dataset.filename || "document.pdf";
  return downloadElementAsPdf(element, filename);
}

function findClientByName(name) {
  const normalized = (name || "").trim();
  return Object.entries(previewData().clients || {}).find(([, client]) => client.name === normalized) || null;
}

function buildDocRows(items) {
  const products = previewData().products || {};
  const rows = [];
  const rowCount = Math.max(15, items.length);
  for (let index = 0; index < rowCount; index += 1) {
    const item = items[index];
    if (!item) {
      rows.push(`
        <tr>
          <td class="doc-col-no">&nbsp;</td>
          <td class="doc-col-name"></td>
          <td class="doc-col-brand"></td>
          <td class="doc-col-condition"></td>
          <td class="doc-col-qty"></td>
          <td class="doc-col-price"></td>
          <td class="doc-col-price"></td>
        </tr>
      `);
      continue;
    }
    const product = products[item.product];
    if (!product) continue;
    rows.push(`
      <tr>
        <td class="doc-col-no">${index + 1}</td>
        <td class="doc-col-name">${product.name}</td>
        <td class="doc-col-brand">${product.brand}</td>
        <td class="doc-col-condition">${product.condition}</td>
        <td class="doc-col-qty">1</td>
        <td class="doc-col-price doc-cell-price" data-amount="${item.price}">${formatYen(item.price)}</td>
        <td class="doc-col-price doc-cell-amount" data-amount="${item.price}">${formatYen(item.price)}</td>
      </tr>
    `);
  }
  return rows.join("");
}

function buildReturnGroups() {
  const assignments = { ...defaultAssignments(), ...getAssignments() };
  const groups = {};
  Object.entries(assignments).forEach(([productId, assignment]) => {
    const product = previewData().products?.[productId];
    if (!product) return;
    const current = getItemState(productId, product.status || "認証待ち");
    if (current.unavailable || current.clientReturned) return;
    const clientId = assignment.clientId || product.client;
    const service = assignment.service || product.service;
    const key = `${clientId}__${service}`;
    if (!groups[key]) {
      groups[key] = {
        key,
        client: clientId,
        service,
        items: [],
      };
    }
    groups[key].items.push({
      product: productId,
      price: Number(assignment.price) || Number(product.amount) || 0,
      source: assignment.source || "回答書類",
    });
  });

  return Object.values(groups).sort((left, right) => {
    const a = `${previewData().clients[left.client]?.name || ""}-${left.service}`;
    const b = `${previewData().clients[right.client]?.name || ""}-${right.service}`;
    return a.localeCompare(b, "ja");
  });
}

function applyStatus(selectId, badgeId, stateId, noticeId, productName, service) {
  const select = document.getElementById(selectId);
  const badge = document.getElementById(badgeId);
  const state = document.getElementById(stateId);
  if (!select || !badge || !state) return;

  const next = select.value;
  const card = select.closest("[data-product-id]");
  const productId = card ? card.dataset.productId : "";
  badge.textContent = next;
  badge.className = `pill ${statusClass(next)}`;
  state.textContent = next;
  if (productId) {
    setItemState(productId, { status: next, unavailable: false });
  }
  showInlineNotice(noticeId, `${productName}（${serviceLabel(service)}）の状態を「${next}」へ更新し、ユーザー画面のお知らせと商品状態へ反映する想定です。`);
  updateStageTabCounts();
}

function notifyUnavailable(noticeId, badgeId, stateId, cardId, productName) {
  const confirmed = window.confirm(`${productName} を受付不可にして、クライアントへ通知します。よろしいですか？`);
  if (!confirmed) return;
  const badge = document.getElementById(badgeId);
  const state = document.getElementById(stateId);
  if (badge) {
    badge.textContent = "受付不可";
    badge.className = "pill unavailable";
  }
  if (state) {
    state.textContent = "受付不可";
  }
  const card = document.getElementById(cardId);
  const productId = card ? card.dataset.productId : "";
  if (productId) {
    setItemState(productId, { status: "受付不可", unavailable: true });
  }
  showInlineNotice(noticeId, `${productName} は受付不可として、ユーザー画面のお知らせへ通知し、この確認一覧から外す想定です。`);
  updateStageTabCounts();
  if (card) {
    window.setTimeout(() => {
      card.style.display = "none";
    }, 250);
  }
}

function sendMemo(textareaId, noticeId, productId, productName) {
  const textarea = document.getElementById(textareaId);
  const message = (textarea?.value || "").trim();
  if (!message) {
    showInlineNotice(noticeId, "送信するメモを入力してください。");
    return;
  }
  setItemState(productId, { memo: message });
  showInlineNotice(noticeId, `${productName} について、ユーザー画面のお知らせへメモを送信する想定です。内容: ${message}`);
}

function registerVendorFile() {
  const fileInput = document.getElementById("vendor-file");
  const dateInput = document.getElementById("vendor-file-date");
  const serviceInput = document.getElementById("vendor-file-service");
  const titleInput = document.getElementById("vendor-file-title");
  const companyInput = document.getElementById("vendor-file-company");
  if (!fileInput) return;
  const fileName = (fileInput.value || "").split("\\\\").pop();
  if (!fileName) {
    showInlineNotice("vendor-file-notice", "登録する回答ファイルを選択してください。");
    return;
  }
  const dateLabel = dateInput && dateInput.value ? dateInput.value : "未設定";
  const service = serviceInput ? serviceLabel(serviceInput.value) : "業者卸販売";
  const title = (titleInput?.value || "").trim() || fileName;
  const company = (companyInput?.value || "").trim() || "業者名未設定";
  showInlineNotice("vendor-file-notice", `${dateLabel} / ${company} / ${service} の回答ファイル「${title}」を登録する想定です。`);
}

function assignClient(inputId, noticeId, productId, productName, price, source, service) {
  const input = document.getElementById(inputId);
  if (!input) return;
  const selected = findClientByName(input.value);
  if (!selected) {
    showInlineNotice(noticeId, "返送先クライアント名を一覧から選択してください。");
    return;
  }
  const [clientId, client] = selected;
  setAssignment(productId, {
    clientId,
    clientName: client.name,
    service,
    source,
    price,
  });
  showInlineNotice(noticeId, `${productName} を「${client.name}」の ${serviceLabel(service)} 返送候補へ振り分けました。4番で確認できます。`);
  updateStageTabCounts();
}

function syncVendorSelectionUI() {
  const rows = Array.from(document.querySelectorAll(".js-vendor-select-row[data-product-id]"));
  const summary = document.getElementById("vendor-selected-summary");
  const empty = document.getElementById("vendor-selection-empty");
  const service = getQueryParam("service") || "wholesale";
  const expectedStatus = service === "simultaneous" ? "出品中" : "査定中";

  rows.forEach((row) => {
    const productId = row.dataset.productId;
    const product = previewData().products?.[productId];
    const current = getItemState(productId, row.dataset.defaultStatus || product?.status || "認証待ち");
    const visible = !current.unavailable && !current.stage2Completed && row.dataset.service === service && current.status === expectedStatus;
    row.dataset.baseVisible = visible ? "true" : "false";
    row.style.display = visible ? "" : "none";
    const checkbox = row.querySelector(".vendor-check");
    if (!visible && checkbox) checkbox.checked = false;
  });

  if (empty) {
    const visibleRows = rows.filter((row) => row.style.display !== "none");
    empty.hidden = visibleRows.length > 0;
  }

  if (summary) {
    const names = Array.from(document.querySelectorAll(".vendor-check:checked"))
      .map((node) => node.dataset.summary || node.dataset.product)
      .filter(Boolean);
    summary.textContent = names.length ? names.join(" / ") : "まだ商品を選択していません";
  }
  applySearchFilter("vendor-outgoing-search", ".js-vendor-select-row", "vendor-selection-empty");
}

function prepareEstimateDraft(targetHref) {
  const service = getQueryParam("service") || "wholesale";
  let selectedProducts = Array.from(document.querySelectorAll(".vendor-check:checked"))
    .map((node) => node.dataset.productId)
    .filter(Boolean);
  if (!selectedProducts.length) {
    selectedProducts = Array.from(document.querySelectorAll(`.js-vendor-select-row[data-service="${service}"]`))
      .filter((row) => row.style.display !== "none")
      .map((row) => row.dataset.productId)
      .filter(Boolean);
  }
  if (!selectedProducts.length) {
    selectedProducts = Array.from(document.querySelectorAll(`.js-vendor-select-row[data-service="${service}"]`))
      .map((row) => row.dataset.productId)
      .filter(Boolean);
  }
  const vendorSelect = document.getElementById("vendor-target");
  let vendorName = vendorSelect ? vendorSelect.value : "";
  if (!vendorName && vendorSelect && vendorSelect.options.length > 1) {
    vendorSelect.selectedIndex = 1;
    vendorName = vendorSelect.value;
  }
  if (!selectedProducts.length) {
    showInlineNotice("vendor-draft-notice", "書類に入れる商品がありません。1番で状態を進めると、ここに表示されます。");
    return;
  }
  if (!vendorName) {
    showInlineNotice("vendor-draft-notice", "送付先業者を選択してください。");
    return;
  }
  setVendorDraft({
    vendorName,
    selectedProducts,
    service,
    documentType: document.getElementById("document-type")?.value || "",
    createdAt: new Date().toISOString(),
  });
  window.location.href = targetHref;
}

function renderVendorEstimateTemplate() {
  const rowsTarget = document.getElementById("vendor-estimate-rows");
  if (!rowsTarget) return;
  const draft = getVendorDraft();
  const vendorName = document.querySelector("[data-vendor-name]");
  const title = document.querySelector(".doc-title");
  const message = document.querySelector(".doc-message");
  if (!draft || !draft.selectedProducts?.length) {
    rowsTarget.innerHTML = buildDocRows([]);
    if (vendorName) vendorName.textContent = "取引先業者 御中";
    return;
  }
  const items = draft.selectedProducts.map((productId) => {
    const product = previewData().products?.[productId];
    return { product: productId, price: Number(product?.amount) || 0 };
  });
  rowsTarget.innerHTML = buildDocRows(items);
  if (vendorName) vendorName.textContent = `${draft.vendorName} 御中`;
  if (title) {
    title.textContent = draft.service === "auction" ? "オークション依頼書" : (draft.service === "simultaneous" ? "出品管理シート" : "見積依頼書");
  }
  if (message) {
    message.textContent = draft.service === "auction"
      ? "下記の商品について、オークション出品のご確認をお願いいたします。"
      : (draft.service === "simultaneous"
          ? "下記の商品について、同時出品の管理内容をご確認ください。"
          : "下記の商品について、見積のご確認をお願いいたします。");
  }
  const total = items.reduce((sum, item) => sum + item.price, 0);
  document.querySelectorAll("[data-total-output]").forEach((node) => {
    node.textContent = formatYen(total);
  });
}

function renderStage4Summary() {
  const container = document.getElementById("client-outgoing-groups");
  if (!container) return;
  const serviceFilter = container.dataset.serviceFilter || "all";
  const groups = buildReturnGroups().filter((group) => serviceFilter === "all" || group.service === serviceFilter);
  const empty = document.getElementById("client-outgoing-empty");
  if (!groups.length) {
    container.innerHTML = "";
    if (empty) empty.hidden = false;
    return;
  }
  if (empty) empty.hidden = true;
  const byClient = {};
  groups.forEach((group) => {
    if (!byClient[group.client]) byClient[group.client] = [];
    byClient[group.client].push(group);
  });
  container.innerHTML = Object.entries(byClient).map(([clientId, clientGroups]) => {
    const client = previewData().clients?.[clientId];
    const serviceCards = clientGroups.map((group) => {
      const total = group.items.reduce((sum, item) => sum + item.price, 0);
      const names = group.items.map((item) => previewData().products?.[item.product]?.name).filter(Boolean).join(" / ");
      const query = encodeURIComponent(group.key);
      return `
        <div class="compact-item">
          <strong>${serviceLabel(group.service)}</strong>
          <span>${group.items.length}点 / ${names}</span>
          <span>合計予定金額 ${formatYen(total)}</span>
          <div class="card-actions">
            <a class="btn btn-soft" href="documents_v2_client_delivery.html?group=${query}">返送内容を確認する</a>
            <a class="btn btn-primary" href="documents_v2_client_statement_template.html?group=${query}">買取明細書を作成する</a>
          </div>
        </div>
      `;
    }).join("");
    const totalCount = clientGroups.reduce((sum, group) => sum + group.items.length, 0);
    const services = clientGroups.map((group) => serviceLabel(group.service)).join(" / ");
    return `
      <div class="summary-card js-client-group js-filter-card" data-base-visible="true" data-search="${client?.name || ""} ${client?.client_no || ""} ${client?.request_id || ""} ${clientGroups.map((group) => serviceLabel(group.service)).join(" ")} ${clientGroups.map((group) => group.items.map((item) => previewData().products?.[item.product]?.name || '').join(' ')).join(' ')}">
        <div class="summary-head">
          <div>
            <div class="summary-client">${client?.name || "クライアント"}</div>
            <div class="summary-meta">${client?.client_no || client?.request_id || ""} / 返送対象 ${totalCount}点 / ${services}</div>
          </div>
          <span class="pill payment">返送準備中</span>
        </div>
        <details>
          <summary>
            <span>サービス別に確認</span>
            <span>${clientGroups.length}区分</span>
          </summary>
          <div class="compact-list service-detail-body">${serviceCards}</div>
        </details>
      </div>
    `;
  }).join("");
  applySearchFilter("client-outgoing-search", ".js-client-group", "client-outgoing-empty");
}

function getGroupFromQuery() {
  const params = new URLSearchParams(window.location.search);
  const key = params.get("group");
  if (!key) return null;
  return buildReturnGroups().find((group) => group.key === key) || null;
}

function renderClientDelivery() {
  const container = document.getElementById("client-delivery-items");
  if (!container) return;
  const group = getGroupFromQuery();
  const empty = document.getElementById("client-delivery-empty");
  const title = document.getElementById("client-delivery-title");
  const description = document.getElementById("client-delivery-description");
  const templateLink = document.getElementById("client-delivery-template-link");
  if (!group) {
    container.innerHTML = "";
    if (empty) empty.hidden = false;
    return;
  }
  const client = previewData().clients?.[group.client];
  if (title) title.textContent = `${client?.name || "クライアント"} / ${serviceLabel(group.service)} の返送内容`;
  if (description) description.textContent = `ここでどの商品を返送書類に含めるかを確認し、${serviceLabel(group.service)} 用の買取明細書へ進みます。`;
  if (templateLink) templateLink.href = `documents_v2_client_statement_template.html?group=${encodeURIComponent(group.key)}`;
  if (empty) empty.hidden = group.items.length > 0;
  container.innerHTML = group.items.map((item) => {
    const product = previewData().products?.[item.product];
    if (!product) return "";
    const detailHref = product.detail_page;
    const assignment = getAssignments()[item.product];
    return `
      <div class="product-card js-product-card" data-product-id="${item.product}" data-service="${product.service}" data-default-status="${product.status}" data-stage="client-outgoing">
        <div class="product-thumb">
          <img src="${product.image}" alt="${product.name}">
        </div>
        <div class="product-body">
          <div class="product-top">
            <div>
              <div class="product-title">${product.name}</div>
              <div class="product-meta">${serviceLabel(product.service)} / ${item.source}</div>
            </div>
            <span class="pill payment">返送予定</span>
          </div>
          <div class="detail-grid">
            <div class="field-block"><div class="field-label">売却額</div><div class="field-value">${formatYen(item.price)}</div></div>
            <div class="field-block"><div class="field-label">商品詳細</div><div class="field-value"><a href="${detailHref}">${product.name} の詳細を見る</a></div></div>
            <div class="field-block"><div class="field-label">返送先クライアント</div><div class="field-value">${assignment?.clientName || (previewData().clients?.[group.client]?.name || "")}</div></div>
            <div class="field-block"><div class="field-label">返送書類</div><div class="field-value">買取明細書へ反映</div></div>
          </div>
          <p id="delivery-note-${item.product}" class="inline-note">3番で反映された返送先です。内容を確認してから買取明細書へ進みます。</p>
        </div>
      </div>
    `;
  }).join("");
}

function renderStatementTemplate() {
  const rowsTarget = document.getElementById("client-statement-rows");
  if (!rowsTarget) return;
  const group = getGroupFromQuery();
  const clientName = document.querySelector("[data-client-name]");
  const clientService = document.querySelector("[data-client-service]");
  const backLink = document.getElementById("statement-back-link");
  if (!group) {
    rowsTarget.innerHTML = buildDocRows([]);
    return;
  }
  const client = previewData().clients?.[group.client];
  if (clientName) clientName.textContent = client?.name || "クライアント名";
  if (clientService) clientService.textContent = serviceLabel(group.service);
  if (backLink) backLink.href = `documents_v2_client_delivery.html?group=${encodeURIComponent(group.key)}`;
  const sendLink = document.getElementById("statement-send-link");
  if (sendLink) sendLink.href = `documents_v2_user_item_editor.html?group=${encodeURIComponent(group.key)}`;
  rowsTarget.innerHTML = buildDocRows(group.items);
  const total = group.items.reduce((sum, item) => sum + item.price, 0);
  document.querySelectorAll("[data-total-output]").forEach((node) => {
    node.textContent = formatYen(total);
  });
}

function completedDocKindLabel(kind) {
  if (kind === "client_estimate_request_pending") return "クライアント返送見積依頼書";
  if (kind === "client_estimate_request") return "クライアントから届いた見積依頼書";
  if (kind === "settlement_statement") return "仕切書";
  if (kind === "calculation_statement") return "計算書";
  if (kind === "vendor_outgoing") return "開花から業者への見積依頼書";
  if (kind === "client_outgoing") return "クライアント返送書類";
  return "書類";
}

function completedDocTitle(doc) {
  if (doc.kind === "client_estimate_request_pending") return "見積依頼書";
  if (doc.kind === "client_estimate_request") return "見積依頼書";
  if (doc.kind === "settlement_statement") return "仕切書";
  if (doc.kind === "calculation_statement") return "代行仕入れ計算書";
  if (doc.kind === "client_outgoing") return "買取明細書";
  if (doc.service === "auction") return "オークション依頼書";
  if (doc.service === "simultaneous") return "出品管理シート";
  return "見積依頼書";
}

function completedDocItems(doc) {
  if (Array.isArray(doc.items) && doc.items.length) return doc.items;
  return (doc.products || []).map((productId) => {
    const product = previewData().products?.[productId];
    return { product: productId, price: Number(product?.amount) || 0, source: doc.partner || "履歴書類" };
  });
}

function buildSettlementCompletedDocHtml(doc) {
  if (doc.mainDocumentHtml || doc.detailDocumentHtml) {
    return `
      <div class="settlement-history-docs">
        ${doc.mainDocumentHtml || ""}
        ${doc.detailDocumentHtml || ""}
      </div>
    `;
  }
  const supportItems = Array.isArray(doc.settlementItems) && doc.settlementItems.length
    ? doc.settlementItems
    : completedDocItems(doc).map((item) => {
        const product = previewData().products?.[item.product] || {};
        const saleAmount = Number(item.saleAmount || product.amount || item.price || 0);
        return {
          product: item.product,
          productName: product.name || item.source || "商品名未設定",
          brand: product.brand || "-",
          saleAmount,
          amount: Number(item.price || item.amount || 0),
          tierLabel: settlementSupportTierLabel(saleAmount),
          status: item.status || "完了",
        };
      });
  const supportTotal = Number(doc.supportFee || supportItems.reduce((sum, item) => sum + (Number(item.amount) || 0), 0));
  const monthlyFee = Number(doc.monthlyFee || 0);
  const monthlyFeeLabel = formatMonthlyPlanFee({
    monthlyFee,
    monthlyPlanCustom: Boolean(doc.monthlyPlanCustom),
  });
  const monthlyPlanLabel = [doc.monthlyPlan || "月額プラン", doc.monthlyPlanRange].filter(Boolean).join(" / ");
  const subtotal = monthlyFee + supportTotal;
  const detailRows = supportItems.length
    ? supportItems.map((item, index) => `
        <tr>
          <td class="statement-col-no">${index + 1}</td>
          <td>${item.productName || "商品名未設定"}</td>
          <td>${item.brand || "-"}</td>
          <td class="statement-col-price">${formatYen(item.saleAmount || 0)}</td>
          <td>${item.tierLabel || settlementSupportTierLabel(item.saleAmount)}</td>
          <td class="statement-col-price">${formatYen(item.amount || 0)}</td>
        </tr>
      `).join("")
    : `<tr><td colspan="6">対象の商品明細はありません。</td></tr>`;
  const dateLabel = doc.createdAt ? new Date(doc.createdAt).toLocaleDateString("ja-JP") : new Date().toLocaleDateString("ja-JP");
  return `
    <div class="doc-page statement-doc">
      <div class="doc-title">仕切書</div>
      <div class="doc-meta">
        <div><strong>発行日</strong> ${dateLabel}</div>
        <div><strong>宛先</strong> ${doc.partner || "クライアント"} 様</div>
        <div><strong>発行者</strong> 株式会社開花</div>
        <div><strong>対象月</strong> ${doc.month || "-"}</div>
        <div><strong>書類区分</strong> 利用明細・仕切書</div>
        <div><strong>支払方法</strong> 指定口座振込</div>
      </div>
      <table class="doc-table statement-table">
        <thead><tr><th class="statement-col-no">No.</th><th class="statement-col-desc">項目</th><th class="statement-col-qty">数量</th><th class="statement-col-price">金額</th></tr></thead>
        <tbody>
          <tr><td class="statement-col-no">1</td><td>月額利用料（${monthlyPlanLabel}）</td><td class="statement-col-qty">1式</td><td class="statement-col-price">${monthlyFeeLabel}</td></tr>
          <tr><td class="statement-col-no">2</td><td>撮影・梱包・発送代行サポート費用</td><td class="statement-col-qty">${supportItems.length}点</td><td class="statement-col-price">${formatYen(supportTotal)}</td></tr>
        </tbody>
      </table>
      <div class="doc-total"><span>合計金額</span><strong>${formatYen(subtotal)}</strong></div>
      <div class="doc-note"><strong>備考</strong><p>商品別の対象内容は、別紙「撮影・梱包・発送代行 商品明細」に記載しています。</p></div>
      <div class="statement-detail-sheet">
        <div class="doc-title">撮影・梱包・発送代行 商品明細</div>
        <table class="doc-table statement-table">
          <thead><tr><th class="statement-col-no">No.</th><th>商品名</th><th>ブランド</th><th class="statement-col-price">売上金額</th><th>料金帯</th><th class="statement-col-price">サポート費用</th></tr></thead>
          <tbody>${detailRows}</tbody>
        </table>
      </div>
    </div>
  `;
}

function buildCompletedDocHtml(doc) {
  if (doc.kind === "settlement_statement") return buildSettlementCompletedDocHtml(doc);
  const items = completedDocItems(doc);
  const total = items.reduce((sum, item) => sum + (Number(item.price) || 0), 0);
  const dateLabel = doc.createdAt ? new Date(doc.createdAt).toLocaleDateString("ja-JP") : new Date().toLocaleDateString("ja-JP");
  const recipient = doc.kind === "client_outgoing" || doc.kind === "calculation_statement"
    ? `${doc.partner || "クライアント"} 様`
    : (doc.kind === "client_estimate_request" || doc.kind === "client_estimate_request_pending")
      ? "株式会社開花 御中"
      : `${doc.partner || "取引先業者"} 御中`;
  const message = doc.kind === "calculation_statement"
    ? "下記の通り、代行仕入れに関する計算書をご案内いたします。"
    : doc.kind === "client_outgoing"
    ? "下記の通り、買取明細をご案内いたします。"
    : (doc.kind === "client_estimate_request" || doc.kind === "client_estimate_request_pending")
      ? "下記の商品について、見積依頼書として返送いたします。"
      : "下記の商品について、見積のご確認をお願いいたします。";
  const issuer = (doc.kind === "client_estimate_request" || doc.kind === "client_estimate_request_pending") ? (doc.partner || "クライアント") : "株式会社開花";
  return `
    <div class="doc-page">
      <div class="doc-title">${completedDocTitle(doc)}</div>
      <div class="doc-meta">
        <div><strong>発行日</strong> ${dateLabel}</div>
        <div><strong>宛先</strong> ${recipient}</div>
        <div><strong>発行者</strong> ${issuer}</div>
        <div><strong>対象サービス</strong> ${serviceLabel(doc.service)}</div>
      </div>
      <div class="doc-message">${message}</div>
      <table class="doc-table">
        <thead>
          <tr>
            <th class="doc-col-no">No.</th>
            <th class="doc-col-name">商品名</th>
            <th class="doc-col-brand">ブランド</th>
            <th class="doc-col-condition">状態</th>
            <th class="doc-col-qty">数量</th>
            <th class="doc-col-price">単価</th>
            <th class="doc-col-price">金額</th>
          </tr>
        </thead>
        <tbody>${buildDocRows(items)}</tbody>
      </table>
      <div class="doc-total"><span>合計金額</span><strong>${formatYen(total)}</strong></div>
      <div class="doc-note"><strong>備考</strong><p>書類履歴から再出力した控えです。</p></div>
    </div>
  `;
}

async function withTemporaryCompletedDoc(docId, callback) {
  const doc = getCompletedDocs()[docId];
  if (!doc) {
    alert("履歴書類が見つかりません。");
    return false;
  }
  const wrapper = document.createElement("div");
  wrapper.style.position = "fixed";
  wrapper.style.left = "-10000px";
  wrapper.style.top = "0";
  wrapper.style.background = "#ffffff";
  wrapper.innerHTML = buildCompletedDocHtml(doc);
  document.body.appendChild(wrapper);
  const element = doc.kind === "settlement_statement"
    ? (wrapper.querySelector(".settlement-history-docs") || wrapper.querySelector(".doc-page"))
    : wrapper.querySelector(".doc-page");
  try {
    await callback(element, doc);
  } finally {
    wrapper.remove();
  }
  return true;
}

async function downloadCompletedDocPdf(docId) {
  await withTemporaryCompletedDoc(docId, async (element, doc) => {
    await downloadElementAsPdf(element, `${sanitizeFilename(doc.title || completedDocTitle(doc))}.pdf`);
  });
}

async function downloadCompletedDocWord(docId) {
  await withTemporaryCompletedDoc(docId, async (element, doc) => {
    await downloadElementAsWord(element, `${sanitizeFilename(doc.title || completedDocTitle(doc))}.doc`);
  });
}

function printCompletedDoc(docId) {
  withTemporaryCompletedDoc(docId, async (element) => {
    printElement(element);
  });
}

function renderCompletedDocumentHistory(filter = "all") {
  const container = document.getElementById("completed-doc-history");
  if (!container) return;
  if (filter === "all" && container.dataset.completedFilter) {
    filter = container.dataset.completedFilter;
  }
  const empty = document.getElementById("completed-doc-empty");
  const docs = Object.values(getCompletedDocs())
    .filter((doc) => filter === "all" || doc.kind === filter)
    .sort((a, b) => String(b.createdAt || "").localeCompare(String(a.createdAt || "")));
  if (!docs.length) {
    container.innerHTML = "";
    if (empty) empty.hidden = false;
    return;
  }
  if (empty) empty.hidden = true;
  container.innerHTML = docs.map((doc) => {
    const productNames = (doc.products || []).map((productId) => previewData().products?.[productId]?.name).filter(Boolean);
    const dateLabel = doc.createdAt ? new Date(doc.createdAt).toLocaleString("ja-JP") : "保存日時未設定";
    const roleId = `role-${doc.id}`;
    const noticeId = `cancel-notice-${doc.id}`;
    return `
      <div class="file-card">
        <div class="file-head">
          <div>
            <div class="file-title">${doc.title || completedDocKindLabel(doc.kind)}</div>
            <div class="file-meta">${completedDocKindLabel(doc.kind)} / ${doc.partner || "相手先未設定"} / ${serviceLabel(doc.service)} / 完了日 ${dateLabel}</div>
          </div>
          <span class="pill completed">完了</span>
        </div>
        <p class="section-note">${productNames.join(" / ") || "商品未設定"}</p>
        <div class="card-actions" style="margin-top:12px;">
          <button class="btn btn-primary btn-compact" type="button" onclick="downloadCompletedDocPdf('${doc.id}')">PDF保存</button>
          <button class="btn btn-soft btn-compact" type="button" onclick="downloadCompletedDocWord('${doc.id}')">Word保存</button>
          <button class="btn btn-outline btn-compact" type="button" onclick="printCompletedDoc('${doc.id}')">印刷</button>
        </div>
        <div class="owner-cancel-box">
          <label class="field">
            <span>取消権限</span>
            <select id="${roleId}">
              <option value="admin">管理者</option>
              <option value="owner">オーナー</option>
              <option value="staff">一般スタッフ</option>
            </select>
          </label>
          <button class="btn btn-outline btn-compact" type="button" onclick="cancelCompletedDocument('${doc.id}','${roleId}','${noticeId}')">権限者として取消</button>
          <p id="${noticeId}" class="inline-notice" hidden></p>
        </div>
      </div>
    `;
  }).join("");
}

function cancelCompletedDocument(docId, roleSelectId, noticeId) {
  const role = document.getElementById(roleSelectId)?.value || "";
  if (!["owner", "admin"].includes(role)) {
    showInlineNotice(noticeId, "取消はオーナー、または権限を付与された管理者のみ実行できる想定です。");
    return;
  }
  const confirmed = window.confirm("この完了書類を取り消し、対象商品を再度処理中へ戻します。よろしいですか？");
  if (!confirmed) return;
  const doc = removeCompletedDoc(docId);
  if (!doc) {
    showInlineNotice(noticeId, "取消対象の書類が見つかりません。");
    return;
  }
  (doc.products || []).forEach((productId) => {
    if (doc.kind === "vendor_outgoing") {
      setItemState(productId, { stage2Completed: false, vendorDocId: null });
    } else if (doc.kind === "client_outgoing") {
      setItemState(productId, { clientReturned: false, clientDocId: null, status: "入金待ち" });
    } else if (doc.kind === "calculation_statement") {
      setItemState(productId, { calculationStatementSent: false, userDocumentDelivered: false, userNotificationUnread: false });
    }
  });
  renderCalculationCurrentPage();
  renderCompletedDocumentHistory(document.querySelector(".history-tab.is-active")?.dataset.historyFilter || "all");
  updateStageTabCounts();
}

function completeCurrentDocument(kind) {
  const createdAt = new Date().toISOString();
  if (kind === "vendor") {
    const draft = getVendorDraft();
    if (!draft || !draft.selectedProducts?.length) {
      showInlineNotice("doc-save-notice", "保存する書類の商品がありません。2番の書類作成画面から商品を選んで作成してください。");
      return;
    }
    const docId = `vendor-${Date.now()}`;
    const doc = {
      id: docId,
      kind: "vendor_outgoing",
      title: `${draft.documentType || "見積依頼書"} / ${draft.vendorName || "送付先未設定"}`,
      partner: draft.vendorName || "送付先未設定",
      service: draft.service || "wholesale",
      products: draft.selectedProducts,
      items: draft.selectedProducts.map((productId) => {
        const product = previewData().products?.[productId];
        return { product: productId, price: Number(product?.amount) || 0, source: draft.vendorName || "業者依頼書" };
      }),
      createdAt,
      status: "完了",
    };
    setCompletedDoc(doc);
    doc.products.forEach((productId) => {
      setItemState(productId, { stage2Completed: true, vendorDocId: docId });
    });
    showInlineNotice("doc-save-notice", "この業者向け書類を完了にしました。2番の進行中一覧から外れ、書類履歴へ保存されます。取消は履歴側で権限者のみ行う想定です。");
    updateStageTabCounts();
    return;
  }

  if (kind === "client") {
    const group = getGroupFromQuery();
    if (!group || !group.items?.length) {
      showInlineNotice("doc-save-notice", "保存する返送書類の商品がありません。4番の返送候補から作成してください。");
      return;
    }
    const client = previewData().clients?.[group.client];
    const docId = `client-${Date.now()}`;
    const doc = {
      id: docId,
      kind: "client_outgoing",
      title: `買取明細書 / ${client?.name || "クライアント"} / ${serviceLabel(group.service)}`,
      partner: client?.name || "クライアント",
      clientId: group.client,
      service: group.service,
      products: group.items.map((item) => item.product),
      items: group.items,
      createdAt,
      status: "完了",
    };
    setCompletedDoc(doc);
    group.items.forEach((item) => {
      setItemState(item.product, {
        clientReturned: true,
        clientDocId: docId,
        status: "完了",
        userDocumentDelivered: true,
        userNotificationUnread: true,
        userNotificationTitle: "買取明細書が届きました",
      });
    });
    showInlineNotice("doc-save-notice", "このクライアント返送書類を完了にしました。4番の返送候補から外れ、書類履歴へ保存されます。取消は履歴側で権限者のみ行う想定です。");
    updateStageTabCounts();
  }
}

async function saveCurrentDocumentAsPdf(kind, filename) {
  const element = document.querySelector(".doc-page");
  try {
    const ok = await downloadElementAsPdf(element, filename);
    if (ok) {
      completeCurrentDocument(kind);
    }
  } catch (error) {
    if (error?.name !== "AbortError") {
      alert("PDF保存に失敗しました。もう一度お試しください。");
    }
  }
}

async function saveCurrentDocumentAsWord(kind, filename) {
  try {
    const ok = await downloadElementAsWord(document.querySelector(".doc-page"), filename);
    if (ok) {
      completeCurrentDocument(kind);
    }
  } catch (error) {
    if (error?.name !== "AbortError") {
      alert("Word保存に失敗しました。もう一度お試しください。");
    }
  }
}

function assignDeliveryProduct(inputId, noticeId, productId, productName, price, source, service) {
  const input = document.getElementById(inputId);
  if (!input) return;
  const selected = findClientByName(input.value);
  if (!selected) {
    showInlineNotice(noticeId, "返送先クライアント名を一覧から選択してください。");
    return;
  }
  const [clientId, client] = selected;
  setAssignment(productId, {
    clientId,
    clientName: client.name,
    service,
    source,
    price,
  });
  showInlineNotice(noticeId, `${productName} を「${client.name}」の ${serviceLabel(service)} 返送書類へ振り分けました。`);
  updateStageTabCounts();
}

function sendClientEstimateRequestFromUser(noticeId) {
  const group = getGroupFromQuery();
  if (!group || !group.items?.length) return;
  const client = previewData().clients?.[group.client];
  const docId = `client-estimate-${group.key}`;
  const doc = {
    id: docId,
    kind: "client_estimate_request_pending",
    title: `見積依頼書 / ${client?.name || "クライアント"} / ${serviceLabel(group.service)}`,
    partner: client?.name || "クライアント",
    clientId: group.client,
    service: group.service,
    products: group.items.map((item) => item.product),
    items: group.items,
    createdAt: new Date().toISOString(),
    status: "認証待ち",
  };
  setCompletedDoc(doc);
  group.items.forEach((item) => {
    setItemState(item.product, {
      estimateRequestReturned: true,
      userEstimateRequestSent: true,
      userNotificationUnread: false,
    });
  });
  showInlineNotice(noticeId, "ユーザーから見積依頼書として返送され、管理側の書類一覧「クライアント返送見積依頼書」に反映されました。管理側で認証すると履歴へ移動します。");
  renderUserDocumentsPreview();
  renderPendingClientEstimateRequests();
}

function approveClientEstimateRequest(docId, noticeId) {
  const state = loadPreviewState();
  const doc = state.completedDocs?.[docId];
  if (!doc) {
    showInlineNotice(noticeId, "対象の見積依頼書が見つかりません。");
    return;
  }
  doc.kind = "client_estimate_request";
  doc.status = "認証済み";
  doc.approvedAt = new Date().toISOString();
  state.completedDocs[docId] = doc;
  savePreviewState(state);
  showInlineNotice(noticeId, "見積依頼書を認証しました。書類一覧から外れ、書類履歴の「クライアント返送見積依頼書」へ保存されます。");
  renderPendingClientEstimateRequests();
  renderCompletedDocumentHistory("client_estimate_request");
}

function renderPendingClientEstimateRequests() {
  const container = document.getElementById("pending-client-estimate-list");
  if (!container) return;
  const empty = document.getElementById("pending-client-estimate-empty");
  const docs = Object.values(getCompletedDocs())
    .filter((doc) => doc.kind === "client_estimate_request_pending")
    .sort((a, b) => String(b.createdAt || "").localeCompare(String(a.createdAt || "")));
  if (!docs.length) {
    container.innerHTML = "";
    if (empty) empty.hidden = false;
    return;
  }
  if (empty) empty.hidden = true;
  container.innerHTML = docs.map((doc) => {
    const productNames = completedDocItems(doc).map((item) => previewData().products?.[item.product]?.name).filter(Boolean);
    const dateLabel = doc.createdAt ? new Date(doc.createdAt).toLocaleString("ja-JP") : "返送日時未設定";
    const noticeId = `approve-estimate-${doc.id}`;
    return `
      <div class="file-card">
        <div class="file-head">
          <div>
            <div class="file-title">${doc.title || "見積依頼書"}</div>
            <div class="file-meta">${doc.partner || "クライアント"} / ${serviceLabel(doc.service)} / 返送日時 ${dateLabel}</div>
          </div>
          <span class="pill pending">認証待ち</span>
        </div>
        <p class="section-note">${productNames.join(" / ") || "商品未設定"}</p>
        <div class="card-actions" style="margin-top:12px;">
          <button class="btn btn-primary btn-compact" type="button" onclick="approveClientEstimateRequest('${doc.id}','${noticeId}')">認証して履歴へ移動</button>
          <button class="btn btn-soft btn-compact" type="button" onclick="downloadCompletedDocPdf('${doc.id}')">PDF確認</button>
          <button class="btn btn-outline btn-compact" type="button" onclick="printCompletedDoc('${doc.id}')">印刷</button>
        </div>
        <p id="${noticeId}" class="inline-notice" hidden></p>
      </div>
    `;
  }).join("");
}

function settlementDataForClient(clientId, month) {
  const client = previewData().clients?.[clientId];
  if (!client) return null;
  const products = Object.entries(previewData().products || {}).filter(([, product]) => product.client === clientId);
  const completedProducts = products.filter(([productId, product]) => {
    const current = getItemState(productId, product.status || "認証待ち");
    return current.clientReturned || ["販売済み", "完了"].includes(current.status);
  });
  const planInfo = client.monthly_plan
    ? {
        plan: client.monthly_plan,
        fee: Number(client.monthly_fee || 0),
        range: client.monthly_plan_range || "",
        itemCount: Number(client.monthly_plan_item_count || products.length),
        isCustom: Boolean(client.monthly_plan_custom),
      }
    : monthlyPlanFromItemCount(products.length);
  const monthlyFee = Number(planInfo.fee || 0);
  const supportItems = completedProducts.map(([productId, product]) => {
    const current = getItemState(productId, product.status || "認証待ち");
    const saleAmount = Number(current.soldPrice || product.amount || current.amount || 0);
    const amount = settlementSupportFeeFromSale(saleAmount);
    const shortName = shortProductName(product.name);
    return {
      productId,
      product,
      productName: product.name,
      shortName,
      saleAmount,
      amount,
      label: `撮影・梱包・発送代行サポート費用（${shortName}）`,
      tierLabel: settlementSupportTierLabel(saleAmount),
      status: current.status || product.status || "完了",
      note: current.settlementNote || "",
    };
  });
  const supportFee = supportItems.reduce((sum, item) => sum + item.amount, 0);
  return {
    client,
    clientId,
    month,
    monthlyPlan: planInfo.plan,
    monthlyPlanRange: planInfo.range,
    monthlyPlanItemCount: planInfo.itemCount,
    monthlyPlanCustom: planInfo.isCustom,
    products,
    completedProducts,
    monthlyFee,
    supportItems,
    supportFee,
    total: monthlyFee + supportFee,
  };
}

function createSettlementDocument(clientId, month, renderedDocs = null) {
  const data = settlementDataForClient(clientId, month);
  if (!data) return null;
  const docId = `settlement-${clientId}-${month}`;
  return setCompletedDoc({
    id: docId,
    kind: "settlement_statement",
    title: `仕切書 / ${data.client.name} / ${month}`,
    partner: data.client.name,
    clientId,
    service: "wholesale",
    products: data.supportItems.map((item) => item.productId),
    items: data.supportItems.map((item) => ({ product: item.productId, price: item.amount, source: item.label, saleAmount: item.saleAmount, status: item.status })),
    settlementItems: data.supportItems.map((item) => ({
      product: item.productId,
      productName: item.productName,
      shortName: item.shortName,
      brand: item.product.brand,
      saleAmount: item.saleAmount,
      amount: item.amount,
      tierLabel: item.tierLabel,
      status: item.status,
      note: item.note,
    })),
    createdAt: new Date().toISOString(),
    status: "送付済み",
    month,
    monthlyPlan: data.monthlyPlan,
    monthlyPlanRange: data.monthlyPlanRange,
    monthlyPlanItemCount: data.monthlyPlanItemCount,
    monthlyPlanCustom: data.monthlyPlanCustom,
    monthlyFee: data.monthlyFee,
    supportFee: data.supportFee,
    total: data.total,
    mainDocumentHtml: renderedDocs?.mainDocumentHtml || "",
    detailDocumentHtml: renderedDocs?.detailDocumentHtml || "",
  });
}

function sendSettlementStatement(clientId, noticeId) {
  const month = document.getElementById("settlement-month-filter")?.value || "2026-04";
  const data = settlementDataForClient(clientId, month);
  if (!data) return;
  const firstConfirm = window.confirm(`${data.client.name} 様の「仕切書」と「商品明細」の2書類を確認しましたか？`);
  if (!firstConfirm) return;
  const secondConfirm = window.confirm(`最終確認です。${data.client.name} 様へ ${month} の仕切書・商品明細を送付して、履歴へ保存します。よろしいですか？`);
  if (!secondConfirm) return;
  const renderedDocs = captureSettlementDocumentHtml(clientId);
  const doc = createSettlementDocument(clientId, month, renderedDocs);
  if (!doc) return;
  showInlineNotice(noticeId, `${doc.partner} の ${month} 仕切書と商品明細を送付済みにしました。訂正後の内容も履歴の「仕切書」へ保存されます。`);
  renderCompletedDocumentHistory("settlement_statement");
}

function sendAllSettlementStatements(noticeId) {
  const month = document.getElementById("settlement-month-filter")?.value || "2026-04";
  const clients = Object.keys(previewData().clients || {});
  const firstConfirm = window.confirm(`${month} の仕切書・商品明細を全クライアント ${clients.length} 名分まとめて送付します。各書類の確認は完了していますか？`);
  if (!firstConfirm) return;
  const secondConfirm = window.confirm(`最終確認です。全クライアントへ2書類ずつ送付して履歴へ保存します。よろしいですか？`);
  if (!secondConfirm) return;
  clients.forEach((clientId) => createSettlementDocument(clientId, month, captureSettlementDocumentHtml(clientId)));
  showInlineNotice(noticeId, `${month} の仕切書と商品明細を全クライアント ${clients.length} 名分、一括送付済みにしました。履歴の「仕切書」へ保存されます。`);
  renderSettlementCurrentPage();
  renderCompletedDocumentHistory("settlement_statement");
}

function renderSettlementCurrentPage() {
  const container = document.getElementById("settlement-current-list");
  if (!container) return;
  const month = document.getElementById("settlement-month-filter")?.value || "2026-04";
  container.innerHTML = Object.entries(previewData().clients || {}).map(([clientId, client]) => {
    const data = settlementDataForClient(clientId, month);
    if (!data) return "";
    const noticeId = `settlement-notice-${clientId}`;
    const mainDocId = `settlement-main-${clientId}`;
    const detailDocId = `settlement-detail-${clientId}`;
    const editButtonId = `settlement-edit-${clientId}`;
    const fileBase = sanitizeFilename(`${client.name}_${month}`);
    const monthlyFeeLabel = formatMonthlyPlanFee(data);
    const monthlyPlanLabel = [data.monthlyPlan, data.monthlyPlanRange].filter(Boolean).join(" / ");
    const statementRows = [
      `<tr><td class="statement-col-no">1</td><td>月額利用料（${monthlyPlanLabel}）</td><td class="statement-col-qty">1式</td><td class="statement-col-price">${monthlyFeeLabel}</td></tr>`,
      `<tr><td class="statement-col-no">2</td><td>撮影・梱包・発送代行サポート費用</td><td class="statement-col-qty">${data.supportItems.length}点</td><td class="statement-col-price">${formatYen(data.supportFee)}</td></tr>`,
    ].join("");
    const detailRows = data.supportItems.length ? data.supportItems.map((item, index) => `
      <tr>
        <td class="statement-col-no">${index + 1}</td>
        <td>${item.product.name}</td>
        <td>${item.product.brand || "-"}</td>
        <td class="statement-col-price">${formatYen(item.saleAmount)}</td>
        <td>${item.tierLabel}</td>
        <td class="statement-col-price">${formatYen(item.amount)}</td>
      </tr>
    `).join("") : `<tr><td colspan="6">当月の商品別サポート費用はありません。</td></tr>`;
    const productOverview = data.supportItems.length ? data.supportItems.map((item) => `
      <div class="settlement-product-line">
        <div>
          <strong>${item.product.name}</strong>
          <span>${item.product.brand || "-"} / ${item.product.category || "商品"} / ${item.status}</span>
        </div>
        <span>${formatYen(item.saleAmount)}</span>
      </div>
    `).join("") : `<div class="compact-item"><strong>当月の対象商品なし</strong><span>取引完了した商品が発生したらここに表示されます。</span></div>`;
    return `
      <details class="file-card settlement-client-card">
        <summary>
          <div>
            <div class="file-head">
              <div>
                <div class="file-title">${client.name} / ${month} 仕切書</div>
                <div class="file-meta">クライアント番号 ${client.client_no} / 登録商品 ${data.products.length}点 / 取引完了 ${data.completedProducts.length}点</div>
              </div>
              <span class="pill payment">月締め前</span>
            </div>
            <div class="settlement-client-summary-grid">
              <div class="field-block"><div class="field-label">月額利用料</div><div class="field-value">${monthlyFeeLabel}</div></div>
              <div class="field-block"><div class="field-label">撮影・梱包・発送代行</div><div class="field-value">${data.supportItems.length}点 / ${formatYen(data.supportFee)}</div></div>
              <div class="field-block"><div class="field-label">請求合計</div><div class="field-value">${formatYen(data.total)}</div></div>
            </div>
          </div>
          <span class="btn btn-soft btn-compact">詳細を確認</span>
        </summary>
        <div class="settlement-client-detail">
          <div class="settlement-detail-overview">
            <div class="field-block"><div class="field-label">月額プラン</div><div class="field-value">${monthlyPlanLabel} / ${monthlyFeeLabel}</div></div>
            <div class="field-block"><div class="field-label">登録商品数</div><div class="field-value">${data.monthlyPlanItemCount}点（判定対象）</div></div>
            <div class="field-block"><div class="field-label">仕切対象商品</div><div class="field-value">${data.supportItems.length}点 / ${formatYen(data.supportFee)}</div></div>
          </div>
          <div class="mini-panel">
            <div class="field-label">今回仕入れ・取引した商品</div>
            <div class="settlement-product-list" style="margin-top:8px;">${productOverview}</div>
          </div>
          <div class="card-actions settlement-doc-actions">
            <button id="${editButtonId}" class="btn btn-soft btn-compact" type="button" onclick="toggleSettlementDocumentEdit('${clientId}','${editButtonId}')">2書類を訂正する</button>
            <button class="btn btn-outline btn-compact" type="button" onclick="resetSettlementDocumentEdit('${clientId}')">訂正を元に戻す</button>
            <button class="btn btn-primary btn-compact" type="button" onclick="sendSettlementStatement('${clientId}','${noticeId}')">2書類を確認して送付する</button>
          </div>
          <details class="document-toggle">
            <summary><span>1. 仕切書</span><span class="btn btn-soft btn-compact">仕切書を開く</span></summary>
            <div class="card-actions settlement-print-actions">
              <button class="btn btn-primary btn-compact" type="button" onclick="downloadElementIdAsPdf('${mainDocId}')">仕切書PDF保存</button>
            </div>
            <div class="document-scroll">
              <div id="${mainDocId}" class="doc-page statement-doc settlement-doc-page" data-settlement-doc-group="${clientId}" data-filename="${fileBase}_仕切書.pdf">
              <div class="doc-title">仕切書</div>
              <div class="doc-meta">
                <div><strong>対象月</strong> ${month}</div>
                <div><strong>宛先</strong> ${client.name} 様</div>
                <div><strong>発行者</strong> 株式会社開花</div>
                <div><strong>クライアント番号</strong> ${client.client_no}</div>
                <div><strong>書類区分</strong> 利用明細・仕切書</div>
                <div><strong>支払方法</strong> 指定口座振込</div>
              </div>
              <table class="doc-table statement-table">
                <thead><tr><th class="statement-col-no">No.</th><th class="statement-col-desc">項目</th><th class="statement-col-qty">数量</th><th class="statement-col-price">金額</th></tr></thead>
                <tbody>${statementRows}</tbody>
              </table>
              <div class="doc-total"><span>合計金額</span><strong>${formatYen(data.total)}</strong></div>
              <div class="doc-note"><strong>備考</strong><p>撮影・梱包・発送代行の対象商品は、別紙「商品明細」に記載しています。</p></div>
              </div>
            </div>
          </details>
          <details class="document-toggle">
            <summary><span>2. 商品明細</span><span class="btn btn-soft btn-compact">商品明細を開く</span></summary>
            <div class="card-actions settlement-print-actions">
              <button class="btn btn-soft btn-compact" type="button" onclick="downloadElementIdAsPdf('${detailDocId}')">商品明細PDF保存</button>
            </div>
            <div class="document-scroll">
              <div id="${detailDocId}" class="doc-page statement-doc settlement-doc-page" data-settlement-doc-group="${clientId}" data-filename="${fileBase}_撮影梱包発送商品明細.pdf">
              <div class="doc-title">撮影・梱包・発送代行 商品明細</div>
              <div class="doc-meta">
                <div><strong>対象月</strong> ${month}</div>
                <div><strong>宛先</strong> ${client.name} 様</div>
                <div><strong>対象件数</strong> ${data.supportItems.length}点</div>
                <div><strong>サポート費用合計</strong> ${formatYen(data.supportFee)}</div>
                <div><strong>発行者</strong> 株式会社開花</div>
                <div><strong>書類区分</strong> 商品別明細</div>
              </div>
              <table class="doc-table statement-table">
                <thead><tr><th class="statement-col-no">No.</th><th>商品名</th><th>ブランド</th><th class="statement-col-price">売上金額</th><th>料金帯</th><th class="statement-col-price">サポート費用</th></tr></thead>
                <tbody>${detailRows}</tbody>
              </table>
              <div class="doc-total"><span>商品別サポート費用合計</span><strong>${formatYen(data.supportFee)}</strong></div>
              </div>
            </div>
          </details>
          <p id="${noticeId}" class="inline-notice" hidden></p>
        </div>
      </details>
    `;
  }).join("");
}

function calculationStatementGroups() {
  const groups = {};
  Object.entries(previewData().products || {}).forEach(([productId, product]) => {
    if (!(product.proxyPurchase || product.service === "proxy_purchase")) return;
    const current = getItemState(productId, product.status || "認証待ち");
    if (current.calculationStatementSent) return;
    const clientId = product.client;
    const client = previewData().clients?.[clientId];
    if (!client) return;
    if (!groups[clientId]) {
      groups[clientId] = {
        clientId,
        client,
        items: [],
      };
    }
    groups[clientId].items.push({
      product: productId,
      price: Number(current.proxyPurchaseAmount || product.purchasePrice || product.amount || 0),
      source: "代行仕入れ",
      status: current.status || product.status || "計算書候補",
    });
  });
  return groups;
}

function buildCalculationStatementDoc(group, docId) {
  const total = group.items.reduce((sum, item) => sum + Number(item.price || 0), 0);
  const dateLabel = new Date().toLocaleDateString("ja-JP");
  const rows = buildDocRows(group.items);
  return `
    <div id="${docId}" class="doc-page" data-filename="${sanitizeFilename(group.client.name)}_代行仕入れ計算書.pdf">
      <div class="doc-title">代行仕入れ計算書</div>
      <div class="doc-meta">
        <div><strong>発行日</strong> ${dateLabel}</div>
        <div><strong>宛先</strong> ${group.client.name} 様</div>
        <div><strong>クライアント番号</strong> ${group.client.client_no || "-"}</div>
        <div><strong>発行者</strong> 株式会社開花</div>
        <div><strong>書類区分</strong> 代行仕入れ計算書</div>
        <div><strong>対象件数</strong> ${group.items.length}点</div>
      </div>
      <div class="doc-message">下記の通り、代行仕入れに関する計算内容をご案内いたします。</div>
      <table class="doc-table">
        <thead>
          <tr>
            <th class="doc-col-no">No.</th>
            <th class="doc-col-name">商品名</th>
            <th class="doc-col-brand">ブランド</th>
            <th class="doc-col-condition">状態</th>
            <th class="doc-col-qty">数量</th>
            <th class="doc-col-price">単価</th>
            <th class="doc-col-price">金額</th>
          </tr>
        </thead>
        <tbody>${rows}</tbody>
      </table>
      <div class="doc-total"><span>代行仕入れ計算額</span><strong>${formatYen(total)}</strong></div>
      <div class="doc-note"><strong>備考</strong><p>必要に応じて、代行仕入れに関する手数料・送料・調整額を確認してから送付してください。</p></div>
    </div>
  `;
}

function sendCalculationStatement(clientId, noticeId) {
  const group = calculationStatementGroups()[clientId];
  if (!group || !group.items.length) {
    showInlineNotice(noticeId, "送付対象の代行仕入れ商品がありません。");
    return;
  }
  const confirmed = window.confirm(`${group.client.name} 様へ代行仕入れ計算書を送付し、書類履歴へ保存します。よろしいですか？`);
  if (!confirmed) return;
  const docId = `calculation-${clientId}-${Date.now()}`;
  const doc = {
    id: docId,
    kind: "calculation_statement",
    title: `代行仕入れ計算書 / ${group.client.name}`,
    partner: group.client.name,
    clientId,
    service: "proxy_purchase",
    products: group.items.map((item) => item.product),
    items: group.items,
    createdAt: new Date().toISOString(),
    status: "送付済み",
  };
  setCompletedDoc(doc);
  group.items.forEach((item) => {
    setItemState(item.product, {
      calculationStatementSent: true,
      userDocumentDelivered: true,
      userNotificationUnread: true,
      userNotificationTitle: "代行仕入れ計算書が届きました",
    });
  });
  showInlineNotice(noticeId, `${group.client.name} 様へ代行仕入れ計算書を送付しました。商品一覧側の送付対象から外れ、下の送付済み計算書履歴と書類履歴の「計算書」へ保存されます。`);
  renderCalculationCurrentPage();
  renderCompletedDocumentHistory("calculation_statement");
}

function renderCalculationCurrentPage() {
  const container = document.getElementById("calculation-current-list");
  if (!container) return;
  const empty = document.getElementById("calculation-current-empty");
  const groups = Object.values(calculationStatementGroups());
  if (!groups.length) {
    container.innerHTML = "";
    if (empty) empty.hidden = false;
    return;
  }
  if (empty) empty.hidden = true;
  container.innerHTML = groups.map((group) => {
    const total = group.items.reduce((sum, item) => sum + Number(item.price || 0), 0);
    const noticeId = `calculation-notice-${group.clientId}`;
    const docId = `calculation-doc-${group.clientId}`;
    const itemRows = group.items.map((item) => {
      const product = previewData().products?.[item.product];
      return `
        <div class="settlement-product-line">
          <div>
            <strong>${product?.name || "商品名未設定"}</strong>
            <span>${product?.brand || "-"} / ${product?.category || "商品"} / ${item.status}</span>
          </div>
          <span>${formatYen(item.price)}</span>
        </div>
      `;
    }).join("");
    return `
      <details class="file-card settlement-client-card">
        <summary>
          <div>
            <div class="file-head">
              <div>
                <div class="file-title">${group.client.name} / 代行仕入れ計算書</div>
                <div class="file-meta">クライアント番号 ${group.client.client_no || "-"} / 対象 ${group.items.length}点 / 合計 ${formatYen(total)}</div>
              </div>
              <span class="pill payment">書類送付待ち</span>
            </div>
          </div>
          <span class="btn btn-soft btn-compact">送付内容を確認</span>
        </summary>
        <div class="settlement-client-detail">
          <div class="settlement-detail-overview">
            <div class="field-block"><div class="field-label">書類区分</div><div class="field-value">代行仕入れ計算書</div></div>
            <div class="field-block"><div class="field-label">対象商品</div><div class="field-value">${group.items.length}点</div></div>
            <div class="field-block"><div class="field-label">計算額</div><div class="field-value">${formatYen(total)}</div></div>
          </div>
          <div class="mini-panel">
            <div class="field-label">送付対象の商品</div>
            <div class="settlement-product-list" style="margin-top:8px;">${itemRows}</div>
          </div>
          <div class="card-actions settlement-doc-actions">
            <button class="btn btn-primary btn-compact" type="button" onclick="sendCalculationStatement('${group.clientId}','${noticeId}')">計算書を送付する</button>
            <button class="btn btn-soft btn-compact" type="button" onclick="downloadElementIdAsPdf('${docId}')">PDF保存</button>
          </div>
          <details class="document-toggle">
            <summary><span>計算書テンプレート</span><span class="btn btn-soft btn-compact">書類を開く</span></summary>
            <div class="document-scroll">
              ${buildCalculationStatementDoc(group, docId)}
            </div>
          </details>
          <p id="${noticeId}" class="inline-notice" hidden></p>
        </div>
      </details>
    `;
  }).join("");
}

function assignCurrentGroupToClient() {
  const group = getGroupFromQuery();
  if (!group) return;
  const client = previewData().clients?.[group.client];
  if (!client) return;
  group.items.forEach((item) => {
    setAssignment(item.product, {
      clientId: group.client,
      clientName: client.name,
      service: group.service,
      source: item.source,
      price: item.price,
    });
  });
  showInlineNotice("client-delivery-notice", `${client.name} の ${serviceLabel(group.service)} 商品を一括で振り分けました。`);
  renderStage4Summary();
}

function renderUserItemEditor() {
  const container = document.getElementById("user-item-editor-items");
  if (!container) return;
  const group = getGroupFromQuery();
  const empty = document.getElementById("user-item-editor-empty");
  const title = document.getElementById("user-item-editor-title");
  const copy = document.getElementById("user-item-editor-copy");
  const backLink = document.getElementById("user-item-back-link");
  const reflectionPanel = document.getElementById("user-reflection-panel");
  if (!group) {
    container.innerHTML = "";
    if (empty) empty.hidden = false;
    return;
  }
  const client = previewData().clients?.[group.client];
  if (title) title.textContent = `${client?.name || "クライアント"} の商品状態を編集する`;
  if (copy) copy.textContent = `${serviceLabel(group.service)} の返送書類を送付した後、このクライアントの商品状態を編集する想定です。`;
  if (backLink) backLink.href = `documents_v2_client_statement_template.html?group=${encodeURIComponent(group.key)}`;
  if (empty) empty.hidden = group.items.length > 0;
  if (reflectionPanel) {
    const unreadCount = group.items.filter((item) => {
      const product = previewData().products?.[item.product];
      const current = getItemState(item.product, product?.status || "認証待ち");
      return current.userNotificationUnread || current.clientReturned;
    }).length;
    reflectionPanel.hidden = false;
    reflectionPanel.innerHTML = `
      <div class="section-head">
        <div>
          <h3>ユーザー画面への反映確認</h3>
          <p class="section-note">買取明細書を送付すると、ユーザー側の書類一覧に反映され、商品ページにも送付済み・完了状態が反映されます。</p>
        </div>
        <span class="notification-badge">通知 ${unreadCount || 1}</span>
      </div>
      <div class="summary-card-grid">
        <div class="field-block"><div class="field-label">ユーザー書類</div><div class="field-value">買取明細書が書類一覧に届く想定</div></div>
        <div class="field-block"><div class="field-label">商品ページ</div><div class="field-value">対象商品に送付済み・完了状態を反映</div></div>
        <div class="field-block"><div class="field-label">通知表示</div><div class="field-value">お知らせに未読 ${unreadCount || 1} 件として表示</div></div>
        <div class="field-block"><div class="field-label">次の作業</div><div class="field-value">販売金額・送料・管理メモを確認して更新</div></div>
      </div>
      <div class="card-actions" style="margin-top:14px;">
        <a class="btn btn-soft btn-compact" href="documents_v2_user_documents.html?group=${encodeURIComponent(group.key)}">ユーザー画面の反映を見る</a>
      </div>
    `;
  }
  container.innerHTML = group.items.map((item) => {
    const product = previewData().products?.[item.product];
    if (!product) return "";
    const current = getItemState(item.product, product.status || "認証待ち");
    const selectId = `user-item-status-${item.product}`;
    const soldId = `user-item-sold-${item.product}`;
    const shippingId = `user-item-shipping-${item.product}`;
    const noteId = `user-item-note-${item.product}`;
    const noticeId = `user-item-notice-${item.product}`;
    const options = ["入金待ち", "販売済み", "完了"].map((label) => `<option value="${label}" ${current.status === label ? "selected" : ""}>${label}</option>`).join("");
    return `
      <div class="product-card">
        <div class="product-thumb"><img src="${product.image}" alt="${product.name}"></div>
        <div class="product-body">
          <div class="product-top">
            <div>
              <div class="product-title">${product.name}</div>
              <div class="product-meta">${client?.name || ""} / ${serviceLabel(group.service)} / ${formatYen(item.price)}</div>
            </div>
            <span class="pill ${statusClass(current.status)}">${current.status}</span>
          </div>
          <div class="status-row status-row-actions" style="margin-top:12px;">
            <select id="${selectId}" class="status-select">${options}</select>
            <button class="btn btn-primary" type="button" onclick="updateUserItemStatus('${item.product}','${selectId}','${soldId}','${shippingId}','${noteId}','${noticeId}')">ユーザー商品を更新する</button>
          </div>
          <div class="user-edit-grid">
            <label class="field">
              <span>販売金額</span>
              <input id="${soldId}" type="number" min="0" step="1" value="${current.soldPrice || item.price || 0}" placeholder="売却額を入力">
            </label>
            <label class="field">
              <span>送料</span>
              <input id="${shippingId}" type="number" min="0" step="1" value="${current.shipping || 0}" placeholder="送料を入力">
            </label>
            <label class="field field-wide">
              <span>管理メモ</span>
              <textarea id="${noteId}" rows="3" placeholder="販売済み・入金・発送メモなどを入力">${current.settlementNote || ""}</textarea>
            </label>
          </div>
          <p id="${noticeId}" class="inline-notice" hidden></p>
        </div>
      </div>
    `;
  }).join("");
}

function renderUserDocumentsPreview() {
  const list = document.getElementById("user-documents-list");
  const productList = document.getElementById("user-documents-products");
  if (!list || !productList) return;
  const group = getGroupFromQuery();
  const empty = document.getElementById("user-documents-empty");
  const title = document.getElementById("user-documents-title");
  const copy = document.getElementById("user-documents-copy");
  const badge = document.getElementById("user-documents-notification");
  if (!group) {
    list.innerHTML = "";
    productList.innerHTML = "";
    if (empty) empty.hidden = false;
    if (badge) badge.textContent = "通知 0";
    return;
  }
  const client = previewData().clients?.[group.client];
  if (title) title.textContent = `${client?.name || "クライアント"} のユーザー画面`;
  if (copy) copy.textContent = `${serviceLabel(group.service)} の買取明細書送付後に、書類一覧・通知・商品ページへ反映される内容です。`;
  const docs = Object.values(getCompletedDocs())
    .filter((doc) => doc.kind === "client_outgoing" && doc.clientId === group.client && doc.service === group.service)
    .sort((a, b) => String(b.createdAt || "").localeCompare(String(a.createdAt || "")));
  const estimateDocs = Object.values(getCompletedDocs())
    .filter((doc) => ["client_estimate_request_pending", "client_estimate_request"].includes(doc.kind) && doc.clientId === group.client && doc.service === group.service)
    .sort((a, b) => String(b.createdAt || "").localeCompare(String(a.createdAt || "")));
  const estimateSent = estimateDocs.length > 0;
  const deliveredItems = group.items.filter((item) => {
    const product = previewData().products?.[item.product];
    const current = getItemState(item.product, product?.status || "認証待ち");
    return current.userDocumentDelivered || current.clientReturned;
  });
  const unreadCount = group.items.filter((item) => {
    const product = previewData().products?.[item.product];
    const current = getItemState(item.product, product?.status || "認証待ち");
    return current.userNotificationUnread || current.clientReturned;
  }).length;
  if (badge) badge.textContent = `通知 ${unreadCount}`;
  if (!docs.length && !deliveredItems.length) {
    list.innerHTML = "";
    if (empty) empty.hidden = false;
  } else {
    if (empty) empty.hidden = true;
    const fallbackCard = deliveredItems.length ? `
      <div class="file-card">
        <div class="file-head">
          <div>
            <div class="file-title">買取明細書 / ${client?.name || "クライアント"} / ${serviceLabel(group.service)}</div>
            <div class="file-meta">ユーザー書類一覧へ反映済み / 未読通知 ${unreadCount} 件</div>
          </div>
          <span class="pill completed">届いた書類</span>
        </div>
        <div class="card-actions" style="margin-top:12px;">
          <button class="btn btn-primary btn-compact" type="button" onclick="sendClientEstimateRequestFromUser('user-estimate-request-notice')">${estimateSent ? "見積依頼書を再送する" : "見積依頼書として返送する"}</button>
          <span class="pill ${estimateSent ? "completed" : "pending"}">${estimateSent ? "見積依頼書 返送済み" : "返送待ち"}</span>
        </div>
        <p id="user-estimate-request-notice" class="inline-notice" hidden></p>
      </div>
    ` : "";
    list.innerHTML = docs.length ? docs.map((doc) => {
      const dateLabel = doc.createdAt ? new Date(doc.createdAt).toLocaleString("ja-JP") : "送付日時未設定";
      return `
        <div class="file-card">
          <div class="file-head">
            <div>
              <div class="file-title">${doc.title || "買取明細書"}</div>
              <div class="file-meta">送付日時 ${dateLabel} / ${serviceLabel(doc.service)} / ${client?.name || ""}</div>
            </div>
            <span class="pill completed">届いた書類</span>
          </div>
          <div class="card-actions" style="margin-top:12px;">
            <button class="btn btn-primary btn-compact" type="button" onclick="sendClientEstimateRequestFromUser('user-estimate-request-notice-${doc.id}')">${estimateSent ? "見積依頼書を再送する" : "見積依頼書として返送する"}</button>
            <span class="pill ${estimateSent ? "completed" : "pending"}">${estimateSent ? "見積依頼書 返送済み" : "返送待ち"}</span>
          </div>
          <p id="user-estimate-request-notice-${doc.id}" class="inline-notice" hidden></p>
        </div>
      `;
    }).join("") : fallbackCard;
  }
  productList.innerHTML = group.items.map((item) => {
    const product = previewData().products?.[item.product];
    if (!product) return "";
    const current = getItemState(item.product, product.status || "認証待ち");
    const delivered = current.userDocumentDelivered || current.clientReturned;
    return `
      <div class="product-card">
        <div class="product-thumb"><img src="${product.image}" alt="${product.name}"></div>
        <div class="product-body">
          <div class="product-top">
            <div>
              <div class="product-title">${product.name}</div>
              <div class="product-meta">${serviceLabel(group.service)} / ${formatYen(current.soldPrice || item.price || 0)} / 送料 ${formatYen(current.shipping || 0)}</div>
            </div>
            <span class="pill ${statusClass(current.status || product.status)}">${current.status || product.status}</span>
          </div>
          <div class="detail-grid">
            <div class="field-block"><div class="field-label">書類反映</div><div class="field-value">${delivered ? "買取明細書に紐づき済み" : "未送付"}</div></div>
            <div class="field-block"><div class="field-label">通知</div><div class="field-value">${current.userNotificationUnread ? "未読通知あり" : "通知なし"}</div></div>
            <div class="field-block"><div class="field-label">商品詳細</div><div class="field-value"><a href="${product.detail_page}">${product.name} の詳細を見る</a></div></div>
            <div class="field-block"><div class="field-label">管理メモ</div><div class="field-value">${current.settlementNote || "未入力"}</div></div>
          </div>
        </div>
      </div>
    `;
  }).join("");
}

function updateUserItemStatus(productId, selectId, soldId, shippingId, noteId, noticeId) {
  const select = document.getElementById(selectId);
  const soldInput = document.getElementById(soldId);
  const shippingInput = document.getElementById(shippingId);
  const noteInput = document.getElementById(noteId);
  const product = previewData().products?.[productId];
  if (!select || !product) return;
  const soldPrice = Number(soldInput?.value || 0);
  const shipping = Number(shippingInput?.value || 0);
  const settlementNote = noteInput?.value || "";
  setItemState(productId, {
    status: select.value,
    unavailable: false,
    soldPrice,
    shipping,
    settlementNote,
  });
  showInlineNotice(noticeId, `${product.name} を「${select.value}」へ変更し、販売金額 ${formatYen(soldPrice)} / 送料 ${formatYen(shipping)} をユーザー商品一覧へ反映する想定です。`);
  updateStageTabCounts();
}

function renderBatchCreateMeta() {
  const title = document.getElementById("batch-create-title");
  if (!title) return;
  const copy = document.getElementById("batch-create-copy");
  const listTitle = document.getElementById("batch-create-list-title");
  const configTitle = document.getElementById("batch-create-config-title");
  const targetLabel = document.getElementById("batch-target-label");
  const docType = document.getElementById("document-type");
  const service = getQueryParam("service") || "wholesale";
  const config = {
    wholesale: {
      title: "業者販売の書類を作成する",
      copy: "業者販売へ流す商品を複数選択し、見積依頼書テンプレートへまとめて差し込みます。",
      listTitle: "業者販売へ流す商品",
      configTitle: "見積依頼書の設定",
      targetLabel: "送付先業者",
      docType: "見積依頼書",
    },
    auction: {
      title: "オークション依頼書を作成する",
      copy: "オークションへ出す商品を複数選択し、オークション依頼書テンプレートへ差し込みます。",
      listTitle: "オークションへ出す商品",
      configTitle: "オークション依頼書の設定",
      targetLabel: "送付先業者",
      docType: "オークション依頼書",
    },
    simultaneous: {
      title: "同時出品の管理書類を作成する",
      copy: "同時出品の商品を選択し、出品管理シートへ差し込みます。",
      listTitle: "同時出品で扱う商品",
      configTitle: "出品管理シートの設定",
      targetLabel: "出品先",
      docType: "出品管理シート",
    },
  }[service];
  if (!config) return;
  title.textContent = config.title;
  if (copy) copy.textContent = config.copy;
  if (listTitle) listTitle.textContent = config.listTitle;
  if (configTitle) configTitle.textContent = config.configTitle;
  if (targetLabel) targetLabel.textContent = config.targetLabel;
  if (docType) docType.value = config.docType;
}

document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll(".js-product-card[data-product-id]").forEach((card) => {
    const productId = card.dataset.productId;
    const product = previewData().products?.[productId];
    const fallbackStatus = card.dataset.defaultStatus || product?.status || "認証待ち";
    const current = getItemState(productId, fallbackStatus);
    const badge = card.querySelector(`[id^="badge-"]`);
    const stateTarget = card.querySelector(`[id^="state-"]`);
    const select = card.querySelector("select.status-select");

    if (badge) {
      badge.textContent = current.status || fallbackStatus;
      badge.className = `pill ${statusClass(current.status || fallbackStatus)}`;
    }
    if (stateTarget) {
      stateTarget.textContent = current.status || fallbackStatus;
    }
    if (select) {
      select.value = current.status || fallbackStatus;
    }

    if (current.unavailable) {
      card.style.display = "none";
    }

    if (card.dataset.stage && card.dataset.stage.startsWith("outgoing-")) {
      const expected = card.dataset.expectedStatus || "査定中";
      if ((current.status || fallbackStatus) !== expected || current.unavailable || current.stage2Completed) {
        card.style.display = "none";
      }
    }
  });

  ["wholesale", "auction", "simultaneous"].forEach((service) => {
    const cards = Array.from(document.querySelectorAll(`.js-product-card[data-stage="outgoing-${service}"]`)).filter((card) => card.style.display !== "none");
    const empty = document.getElementById(`outgoing-empty-${service}`);
    if (empty) empty.hidden = cards.length > 0;
  });

  document.querySelectorAll(".vendor-check").forEach((checkbox) => {
    checkbox.addEventListener("change", syncVendorSelectionUI);
  });

  const filters = [
    ["client-incoming-search", ".summary-card.js-filter-card", "client-incoming-empty"],
    ["vendor-outgoing-search", ".product-card.js-filter-card", "vendor-outgoing-search-empty"],
    ["vendor-incoming-search", ".file-card.js-filter-card", "vendor-incoming-search-empty"],
    ["client-outgoing-search", ".js-client-group", "client-outgoing-empty"],
  ];
  filters.forEach(([inputId, selector, emptyId]) => {
    const input = document.getElementById(inputId);
    if (!input) return;
    input.addEventListener("input", () => applySearchFilter(inputId, selector, emptyId));
  });

  ["history-title-search", "history-month-filter", "history-date-from", "history-date-to"].forEach((inputId) => {
    const input = document.getElementById(inputId);
    if (!input) return;
    input.addEventListener("input", applyHistoryFilters);
    input.addEventListener("change", applyHistoryFilters);
  });

  const settlementMonth = document.getElementById("settlement-month-filter");
  if (settlementMonth) {
    settlementMonth.addEventListener("input", renderSettlementCurrentPage);
    settlementMonth.addEventListener("change", renderSettlementCurrentPage);
  }

  document.querySelectorAll(".history-tab[data-history-filter]").forEach((tab) => {
    tab.addEventListener("click", () => {
      document.querySelectorAll(".history-tab").forEach((node) => node.classList.remove("is-active"));
      tab.classList.add("is-active");
      renderCompletedDocumentHistory(tab.dataset.historyFilter || "all");
    });
  });

  document.querySelectorAll(".assign-input[data-product-id]").forEach((input) => {
    const assignment = getAssignments()[input.dataset.productId];
    if (assignment?.clientName) {
      input.value = assignment.clientName;
    }
  });

  syncVendorSelectionUI();
  updateStageTabCounts();
  renderBatchCreateMeta();
  renderVendorEstimateTemplate();
  renderStage4Summary();
  renderClientDelivery();
  renderStatementTemplate();
  renderUserItemEditor();
  renderUserDocumentsPreview();
  renderPendingClientEstimateRequests();
  renderSettlementCurrentPage();
  renderCalculationCurrentPage();
  renderCompletedDocumentHistory();
  applySearchFilter("client-incoming-search", ".summary-card.js-filter-card", "client-incoming-empty");
  applySearchFilter("vendor-outgoing-search", ".product-card.js-filter-card", "vendor-outgoing-search-empty");
  applySearchFilter("vendor-incoming-search", ".file-card.js-filter-card", "vendor-incoming-search-empty");
  applyHistoryFilters();
});
"""


DOC_EDITOR_JS = """
async function downloadDocumentAsWord(filename) {
  if (typeof downloadElementAsWord !== "function") return false;
  return downloadElementAsWord(document.querySelector(".doc-page"), filename);
}

document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll("[data-doc-editor]").forEach((root) => {
    const editableNodes = Array.from(root.querySelectorAll(".doc-meta strong, .doc-meta div, .doc-message, .doc-table tbody td, .doc-note p"));
    editableNodes.forEach((node) => {
      node.classList.add("doc-editable");
      node.dataset.initialHtml = node.innerHTML;
      node.setAttribute("contenteditable", "false");
    });

    const totalOutputs = Array.from(root.querySelectorAll("[data-total-output]"));
    const editButton = root.parentElement.querySelector("[data-action='toggle-edit']");
    const resetButton = root.parentElement.querySelector("[data-action='reset-doc']");

    const formatYen = (value) => `¥${value.toLocaleString("ja-JP")}`;

    function recalcTotal() {
      const amountCells = Array.from(root.querySelectorAll(".doc-cell-amount"));
      const total = amountCells.reduce((sum, cell) => {
        const text = (cell.textContent || "").replace(/[^\\d.-]/g, "");
        const value = Number(text);
        return Number.isFinite(value) ? sum + value : sum;
      }, 0);
      totalOutputs.forEach((output) => {
        output.textContent = formatYen(total);
      });
    }

    editableNodes.forEach((node) => {
      node.addEventListener("input", recalcTotal);
      node.addEventListener("blur", recalcTotal);
    });

    if (editButton) {
      editButton.addEventListener("click", () => {
        const editing = !root.classList.contains("is-edit-mode");
        root.classList.toggle("is-edit-mode", editing);
        editableNodes.forEach((node) => {
          node.setAttribute("contenteditable", editing ? "true" : "false");
        });
        editButton.textContent = editing ? "編集を終了する" : "書類を編集する";
      });
    }

    if (resetButton) {
      resetButton.addEventListener("click", () => {
        editableNodes.forEach((node) => {
          node.innerHTML = node.dataset.initialHtml || "";
        });
        recalcTotal();
      });
    }

    recalcTotal();
  });
});
"""


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content.strip() + "\n", encoding="utf-8")


def redirect_html(url: str) -> str:
    return f'<!DOCTYPE html><meta http-equiv="refresh" content="0; url={url}">'


def response_file_download_content(file_info: dict) -> str:
    lines = [
        file_info["label"],
        f"区分: {SERVICE_LABELS[file_info['service']]}",
        f"取引先: {file_info['partner']}",
        f"対象月: {file_info['month']}",
        "",
    ]
    for idx, item in enumerate(file_info["items"], start=1):
        product = PRODUCTS[item["product"]]
        lines.extend(
            [
                f"No.{idx}",
                f"商品名: {product['name']}",
                f"ブランド: {product['brand']}",
                f"売却額: {money(item['price'])}",
                f"結果: {item['result']}",
                f"初期振り分け先: {item['assigned_client']}",
                "",
            ]
        )
    return "\n".join(lines)


def build() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    write_text(STATIC_DIR / "preview_v2.css", PREVIEW_CSS)
    preview_payload = {
        "clients": CLIENTS,
        "products": PRODUCTS,
        "vendors": VENDORS,
        "responseFiles": RESPONSE_FILES,
        "serviceLabels": SERVICE_LABELS,
        "monthlyPlanSettings": MONTHLY_PLAN_SETTINGS,
    }
    write_text(STATIC_DIR / "preview_data.js", f"window.DOCUMENTS_PREVIEW_DATA = {json.dumps(preview_payload, ensure_ascii=False)};")
    write_text(STATIC_DIR / "preview_v2.js", PREVIEW_JS)
    write_text(STATIC_DIR / "doc_editor.js", DOC_EDITOR_JS)

    write_text(OUTPUT_DIR / "documents_v2_index.html", page(
        "書類管理",
        f"""
        <div class="page">
          <div class="page-head">
            <div>
              <h1>書類管理</h1>
              <p>現状処理中の書類を種類ごとに確認します。履歴ではなく、いま対応が必要な書類だけをここから開く形です。</p>
            </div>
          </div>
          {top_cards()}
        </div>
        """,
    ))
    write_text(OUTPUT_DIR / "index.html", '<!DOCTYPE html><meta http-equiv="refresh" content="0; url=documents_v2_index.html">')

    write_text(OUTPUT_DIR / "documents_v2_client_incoming.html", stage1_page("all"))
    write_text(OUTPUT_DIR / "documents_v2_client_incoming_wholesale.html", stage1_page("wholesale"))
    write_text(OUTPUT_DIR / "documents_v2_client_incoming_auction.html", stage1_page("auction"))
    write_text(OUTPUT_DIR / "documents_v2_client_incoming_simultaneous.html", stage1_page("simultaneous"))

    for client_id in CLIENTS:
        write_text(OUTPUT_DIR / request_detail_filename(client_id), request_detail_page(client_id))
        write_text(OUTPUT_DIR / request_detail_filename(client_id, "wholesale"), request_detail_page(client_id, "wholesale"))
        write_text(OUTPUT_DIR / request_detail_filename(client_id, "auction"), request_detail_page(client_id, "auction"))
        write_text(OUTPUT_DIR / request_detail_filename(client_id, "simultaneous"), request_detail_page(client_id, "simultaneous"))

    for product_id in PRODUCTS:
        write_text(OUTPUT_DIR / PRODUCTS[product_id]["detail_page"], product_detail_page(product_id))

    write_text(OUTPUT_DIR / "documents_v2_vendor_outgoing.html", stage2_page("all"))
    write_text(OUTPUT_DIR / "documents_v2_vendor_outgoing_wholesale.html", stage2_page("wholesale"))
    write_text(OUTPUT_DIR / "documents_v2_vendor_outgoing_auction.html", stage2_page("auction"))
    write_text(OUTPUT_DIR / "documents_v2_vendor_outgoing_simultaneous.html", stage2_page("simultaneous"))
    write_text(OUTPUT_DIR / "vendor_partner_registry.html", vendor_registry_page())
    write_text(OUTPUT_DIR / "vendor_estimate_batch_create.html", batch_create_page())
    write_text(OUTPUT_DIR / "documents_v2_vendor_estimate_template.html", estimate_template_page())
    write_text(OUTPUT_DIR / "documents_v2_vendor_incoming.html", stage3_page("all"))
    write_text(OUTPUT_DIR / "documents_v2_vendor_incoming_wholesale.html", stage3_page("wholesale"))
    write_text(OUTPUT_DIR / "documents_v2_vendor_incoming_auction.html", stage3_page("auction"))
    write_text(OUTPUT_DIR / "documents_v2_vendor_incoming_simultaneous.html", stage3_page("simultaneous"))
    write_text(OUTPUT_DIR / "documents_v2_history.html", history_page())
    for config in history_category_configs():
        write_text(OUTPUT_DIR / config["slug"], history_category_page(config["key"]))
    for file_info in RESPONSE_FILES:
        write_text(OUTPUT_DIR / file_info["slug"], vendor_file_page(file_info))
        write_text(OUTPUT_DIR / file_info["download"], response_file_download_content(file_info))
    for file_info in HISTORY_RESPONSE_FILES:
        write_text(OUTPUT_DIR / file_info["slug"], vendor_file_page(file_info))
        write_text(OUTPUT_DIR / file_info["download"], response_file_download_content(file_info))
    for file_info in additional_history_entries():
        write_text(OUTPUT_DIR / file_info["slug"], generic_history_detail_page(file_info))

    write_text(OUTPUT_DIR / "documents_v2_client_outgoing.html", stage4_page("all"))
    write_text(OUTPUT_DIR / "documents_v2_client_outgoing_wholesale.html", stage4_page("wholesale"))
    write_text(OUTPUT_DIR / "documents_v2_client_outgoing_auction.html", stage4_page("auction"))
    write_text(OUTPUT_DIR / "documents_v2_client_outgoing_simultaneous.html", stage4_page("simultaneous"))
    write_text(OUTPUT_DIR / "documents_v2_client_delivery.html", client_delivery_page())
    write_text(OUTPUT_DIR / "documents_v2_client_statement_template.html", statement_template_page())
    write_text(OUTPUT_DIR / "documents_v2_user_item_editor.html", user_item_editor_page())
    write_text(OUTPUT_DIR / "documents_v2_user_documents.html", user_documents_page())
    write_text(OUTPUT_DIR / "documents_v2_client_estimate_requests.html", client_estimate_requests_page())
    write_text(OUTPUT_DIR / "documents_v2_settlement_statements.html", settlement_statements_page())
    write_text(OUTPUT_DIR / "documents_v2_calculation_statements.html", calculation_statements_page())
    for client_id, config in RETURN_GROUPS.items():
        group_key = f"{config['client']}__{config['service']}"
        write_text(OUTPUT_DIR / config["slug"], redirect_html(f"documents_v2_client_delivery.html?group={group_key}"))
        write_text(OUTPUT_DIR / config["template"], redirect_html(f"documents_v2_client_statement_template.html?group={group_key}"))


if __name__ == "__main__":
    build()
