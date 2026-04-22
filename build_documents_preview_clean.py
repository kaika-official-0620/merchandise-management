from __future__ import annotations

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
}


CLIENTS = {
    "yamada": {"name": "山田 太郎", "request_id": "REQ-2026-041", "received_at": "2026/04/20"},
    "sato": {"name": "佐藤 花子", "request_id": "REQ-2026-039", "received_at": "2026/04/18"},
    "suzuki": {"name": "鈴木 一郎", "request_id": "REQ-2026-038", "received_at": "2026/04/17"},
    "takahashi": {"name": "高橋 愛", "request_id": "REQ-2026-037", "received_at": "2026/04/16"},
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
               , purchase_date, model_number, notes
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
                   , purchase_date, model_number, notes
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
          <link rel="stylesheet" href="static/preview_v2.css">
          {extra_head}
        </head>
        <body>
        {body}
        <script src="static/preview_data.js"></script>
        <script src="static/preview_v2.js"></script>
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


def top_cards() -> str:
    stage1_count = sum(len(client_products(client_id)) for client_id in CLIENTS)
    stage2_count = sum(len(products_for_outgoing(service)) for service in ("wholesale", "auction", "simultaneous"))
    stage3_count = len(RESPONSE_FILES)
    stage4_count = sum(1 for _ in RETURN_GROUPS)
    cards = [
        ("1", "クライアントから受付", "どのクライアントから何の商品が届いたかを、名前単位で確認します。", "documents_v2_client_incoming.html"),
        ("2", "開花から業者へ依頼", "査定中に切り替えた業者卸販売の商品だけをまとめて業者へ流します。", "documents_v2_vendor_outgoing.html"),
        ("3", "業者から回答受領", "業者から届いた回答ファイルを1件ずつ確認し、商品ごとに返送先を振り分けます。", "documents_v2_vendor_incoming.html"),
        ("4", "クライアントへ返送", "クライアントごとに書類をまとめて作成し、返送内容を確認します。", "documents_v2_client_outgoing.html"),
    ]
    inner = []
    for step, heading, desc, href in cards:
        count = {
            "1": f"{stage1_count}点受付中",
            "2": f"{stage2_count}点準備中",
            "3": f"{stage3_count}ファイル受領",
            "4": f"{stage4_count}名へ返送準備",
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


def service_tabs(current: str) -> str:
    tabs = [
        ("documents_v2_client_incoming.html", "すべて", "all"),
        ("documents_v2_client_incoming_wholesale.html", "業者卸販売", "wholesale"),
        ("documents_v2_client_incoming_auction.html", "業者オークション", "auction"),
        ("documents_v2_client_incoming_simultaneous.html", "同時出品", "simultaneous"),
    ]
    parts = []
    for href, label, key in tabs:
        cls = "service-tab is-active" if current == key else "service-tab"
        parts.append(f'<a class="{cls}" href="{href}">{label}</a>')
    return f'<div class="service-tabs">{"".join(parts)}</div>'


def request_detail_filename(client_id: str, service: str | None = None) -> str:
    if service and service != "all":
        return f"documents_v2_request_detail_{client_id}_{service}.html"
    return f"documents_v2_request_detail_{client_id}.html"


def product_detail_href(product: dict) -> str:
    real_item_id = product.get("real_item_id")
    if real_item_id:
        return f"/view/{real_item_id}"
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
    return f"""
    <div class="summary-card">
      <div class="summary-head">
        <div>
          <div class="summary-client">{client["name"]}</div>
          <div class="summary-meta">{client["request_id"]} / 受付日 {client["received_at"]}</div>
        </div>
        <span class="pill {status_class(status_badge)}">{status_badge}</span>
      </div>
      <div class="summary-card-grid">
        <div class="field-block"><div class="field-label">申請商品数</div><div class="field-value">{len(products)}点</div></div>
        <div class="field-block"><div class="field-label">依頼形式</div><div class="field-value">{request_kind}</div></div>
        <div class="field-block"><div class="field-label">代表商品</div><div class="field-value">{product_names}</div></div>
        <div class="field-block"><div class="field-label">次の流れ</div><div class="field-value">詳細で確認後、査定中にした商品だけ2番へ送ります</div></div>
      </div>
      <div class="service-chip-row">{service_chips(products)}</div>
      <div class="card-actions">
        <a class="btn btn-soft" href="{request_detail}">詳細を確認する</a>
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
            {service_tabs(service or "all")}
            <div class="summary-grid">
              {''.join(filtered_clients)}
            </div>
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
      <div class="status-row">
        <select id="{select_id}" class="status-select">
          {options}
        </select>
        <button class="btn btn-primary" type="button" onclick="applyStatus('{select_id}','{badge_id}','{state_id}','{notice_id}','{escape(product_name)}','{service}')">状態を更新して通知</button>
      </div>
      <div class="status-row">
        <button class="btn btn-outline" type="button" onclick="notifyUnavailable('{notice_id}','{badge_id}','{state_id}','card-{product_id}','{escape(product_name)}')">受付不可を通知する</button>
      </div>
      <div class="field field-wide" style="margin-top:10px;">
        <span>クライアントへ送るメモ</span>
        <textarea id="{memo_id}" rows="3" placeholder="例：状態変更の理由や補足内容を入力してください"></textarea>
      </div>
      <div class="status-row">
        <button class="btn btn-soft" type="button" onclick="sendMemo('{memo_id}','{notice_id}','{product_id}','{escape(product_name)}')">メモを送信する</button>
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
          <a class="btn btn-soft" href="{detail_href}">商品詳細を見る</a>
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
    detail_href = product_detail_href(product)
    return page(
        f"{product['name']} の商品詳細",
        f"""
        <div class="page">
          {back_bar()}
          <div class="page-head">
            <div>
              <h1>商品詳細</h1>
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
            <div class="card-actions" style="margin-top:18px;">
              <a class="btn btn-primary" href="{detail_href}">実際の商品詳細を開く</a>
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
            <div class="product-card js-product-card" data-product-id="{product['id']}" data-service="{product['service']}" data-default-status="{product['status']}" data-stage="outgoing-{service}" data-expected-status="{outgoing_expected_status(service)}">
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
                  <div class="field-block"><div class="field-label">画像確認</div><div class="field-value"><a href="{detail_href}">商品詳細を見る</a></div></div>
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
      </div>
      <div class="stack">
        {''.join(cards)}
      </div>
      <p id="outgoing-empty-{service}" class="section-note" hidden>この部門で進行できる商品はありません。1番で状態を更新するとここに表示されます。</p>
      <div class="card-actions" style="margin-top:18px;">
        <a class="btn btn-outline" href="vendor_partner_registry.html">送付先業者を登録・編集する</a>
        <a class="btn btn-primary" href="vendor_estimate_batch_create.html?service={service}">{outgoing_button_label(service)}</a>
      </div>
    </div>
    """


def stage2_page() -> str:
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
          {outgoing_section("wholesale")}
          {outgoing_section("auction")}
          {outgoing_section("simultaneous")}
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
                <button class="btn btn-primary" type="button" onclick="prepareEstimateDraft('documents_v2_vendor_estimate_template.html')">テンプレートへ差し込む</button>
              </div>
            </div>
          </div>
        </div>
        """,
    )


def doc_rows(items: list[dict], *, price_key: str = "price") -> str:
    rows = []
    for idx in range(15):
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
            </div>
          </div>
          <div class="doc-page" data-doc-editor>
            <div class="doc-title">見積依頼書</div>
            <div class="doc-meta">
              <div><strong>発行日</strong> 2026年4月23日</div>
              <div><strong>送付先</strong> <span data-vendor-name>取引先業者 御中</span></div>
              <div><strong>差出人</strong> 株式会社開花</div>
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
              <p>選択した商品をもとに、必要に応じて書類内容を編集してから業者へ送付します。</p>
            </div>
          </div>
        </div>
        """,
        extra_scripts='<script src="static/doc_editor.js"></script>',
    )


def stage3_page() -> str:
    sections = []
    for service in ("wholesale", "auction", "simultaneous"):
        service_note = {
            "wholesale": "業者卸販売の回答書類です。ファイルを1件ずつ開いて、商品ごとの売却額と返送先を確認します。",
            "auction": "業者オークションの回答書類です。落札結果を確認し、商品ごとに返送先を整理します。",
            "simultaneous": "同時出品の売却履歴やスクリーンショットを登録し、どの商品が売れたかを確認します。",
        }[service]
        cards = []
        for file_info in [item for item in RESPONSE_FILES if item["service"] == service]:
            products = " / ".join(PRODUCTS[item["product"]]["name"] for item in file_info["items"])
            cards.append(
                f"""
                <div class="file-card">
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
                    <a class="btn btn-outline" href="{file_info['download']}" download>ダウンロード</a>
                  </div>
                </div>
                """
            )
        sections.append(
            f"""
            <div class="section">
              <div class="section-head">
                <div>
                  <h3>{SERVICE_LABELS[service]} の回答書類</h3>
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
              <p>月に1件ではなく、取引ごとの回答ファイルを1件ずつ登録して確認します。ファイルを開くと、商品ごとにどのクライアントへ返送するかを振り分けられます。</p>
            </div>
          </div>
          <div class="section">
              <div class="section-head">
                <div>
                  <h3>回答ファイルを登録する</h3>
                  <p class="section-note">届いたファイルをサービス別に登録し、下の一覧から開く・ダウンロードして中身を確認する想定です。</p>
                </div>
              </div>
              <div class="form-inline">
                <label class="field file-field"><span>回答ファイル</span><input id="vendor-file" type="file"></label>
              <label class="field"><span>書類区分</span><select id="vendor-file-service"><option value="wholesale">業者卸販売</option><option value="auction">業者オークション</option><option value="simultaneous">同時出品</option></select></label>
              <label class="field"><span>取引日</span><input id="vendor-file-date" type="date" value="2026-04-23"></label>
              <button class="btn btn-primary" type="button" onclick="registerVendorFile()">ファイルを登録</button>
            </div>
            <p id="vendor-file-notice" class="inline-notice" hidden></p>
          </div>
          {''.join(sections)}
        </div>
        """,
    )


def vendor_file_page(file_info: dict) -> str:
    item_cards = []
    for idx, item in enumerate(file_info["items"], start=1):
        product = PRODUCTS[item["product"]]
        datalist_id = f"client-list-{item['product']}"
        options = "".join(f'<option value="{client["name"]}"></option>' for client in CLIENTS.values())
        item_cards.append(
            f"""
            <tr>
              <td>{idx}</td>
              <td>{product['name']}</td>
              <td>{money(item['price'])}</td>
              <td>{item['result']}</td>
              <td>
                <input class="assign-input" data-product-id="{item['product']}" list="{datalist_id}" id="assign-{item['product']}" value="{item['assigned_client']}" placeholder="クライアント名を検索">
                <datalist id="{datalist_id}">{options}</datalist>
              </td>
              <td>
                <button class="btn btn-soft" type="button" onclick="assignClient('assign-{item['product']}','assign-note-{item['product']}','{item['product']}','{escape(product['name'])}',{item['price']},'{escape(file_info['label'])}','{file_info['service']}')">振り分ける</button>
              </td>
            </tr>
            <tr class="note-row">
              <td colspan="6"><p id="assign-note-{item['product']}" class="inline-note">この商品をどのクライアントの {SERVICE_LABELS[file_info['service']]} 返送へつなぐか、ここで決めます。</p></td>
            </tr>
            """
        )
    preview_rows = "".join(
        f"<tr><td>{idx}</td><td>{PRODUCTS[item['product']]['name']}</td><td>{money(item['price'])}</td><td>{item['result']}</td></tr>"
        for idx, item in enumerate(file_info["items"], start=1)
    )
    return page(
        file_info["label"],
        f"""
        <div class="page">
          {back_bar()}
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
                  <p class="section-note">登録したファイルを確認し、商品名・売却額・結果を見ながら、4番へ渡す準備をします。</p>
                </div>
                <a class="btn btn-outline" href="{file_info['download']}" download>ダウンロード</a>
              </div>
              <div class="file-preview">
                <div class="file-preview-head">回答ファイル プレビュー</div>
                <table class="simple-table">
                  <thead><tr><th>No.</th><th>商品名</th><th>売却額</th><th>結果</th></tr></thead>
                  <tbody>{preview_rows}</tbody>
                </table>
              </div>
            </div>
            <div class="section">
              <div class="section-head"><h3>4番へ振り分ける</h3></div>
              <table class="simple-table">
                <thead><tr><th>No.</th><th>商品名</th><th>売却額</th><th>結果</th><th>返送先クライアント</th><th>操作</th></tr></thead>
                <tbody>{''.join(item_cards)}</tbody>
              </table>
            </div>
          </div>
        </div>
        """,
    )


def return_group_title(config: dict) -> str:
    client = CLIENTS[config["client"]]
    return f"{client['name']} / {SERVICE_LABELS[config['service']]}"


def stage4_page() -> str:
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
          <div class="section">
            <div class="section-head">
              <div>
                <h3>返送対象のクライアント一覧</h3>
                <p class="section-note">業者から回答が来て振り分けた商品と、完了したオークション・同時出品の商品を、クライアント名 × サービス別に分けて表示します。</p>
              </div>
            </div>
            <div id="client-outgoing-groups" class="summary-grid"></div>
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
                <p class="section-note">キャンセル済みの商品は含めず、売却済みの商品のみを返送対象にします。</p>
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
          </div>
        </div>
        """,
        extra_scripts='<script src="static/doc_editor.js"></script>',
    )


PREVIEW_CSS = """
*{box-sizing:border-box}
body{margin:0;font-family:"Noto Sans JP","Segoe UI",sans-serif;background:#f4f7fb;color:#1e293b}
a{color:inherit;text-decoration:none}
img{max-width:100%;display:block}
button,input,select,textarea{font:inherit}
.page{max-width:1280px;margin:0 auto;padding:28px 20px 48px}
.page.page-narrow{max-width:1000px}
.back-bar,.card-actions,.doc-toolbar-actions,.form-inline,.service-tabs,.status-row{display:flex;gap:10px;flex-wrap:wrap}
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
.summary-client,.client-title,.file-title{font-size:20px;font-weight:800;line-height:1.45}
.summary-meta,.product-meta,.origin-meta,.file-meta{margin-top:4px;color:#64748b;font-size:13px;line-height:1.7}
.summary-card-grid,.detail-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px;margin-top:14px}
.service-chip-row{display:flex;gap:8px;flex-wrap:wrap;margin-top:14px}
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
.btn{display:inline-flex;align-items:center;justify-content:center;gap:8px;border-radius:12px;padding:10px 14px;font-size:14px;font-weight:700;border:1px solid transparent;cursor:pointer}
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
.product-card{display:grid;grid-template-columns:180px minmax(0,1fr);gap:18px;padding:16px}
.product-thumb{height:140px;border-radius:16px;background:#f8fafc;border:1px solid #e2e8f0;display:flex;align-items:center;justify-content:center;overflow:hidden}
.product-thumb img{width:100%;height:100%;object-fit:contain}
.status-box{margin-top:14px;padding:14px;border-radius:16px;background:#f8fbff;border:1px solid #dbeafe}
.status-select{min-width:160px;padding:10px 12px;border-radius:10px;border:1px solid #cbd5e1;background:#fff}
.inline-notice,.inline-note{margin:8px 0 0;font-size:13px;line-height:1.8;color:#475569}
.two-col{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:18px}
.compact-list{display:grid;gap:10px}
.compact-item{display:grid;gap:4px;padding:12px 14px;border-radius:14px;background:#f8fafc;border:1px solid #e2e8f0}
.form-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}
.field input,.field select,.field textarea{width:100%;padding:11px 12px;border-radius:12px;border:1px solid #cbd5e1;background:#fff}
.field-wide{grid-column:1 / -1}
.select-grid,.origin-items{display:grid;gap:12px}
.select-row{display:grid;grid-template-columns:auto minmax(0,1fr);gap:10px;align-items:flex-start;padding:12px 14px;border-radius:16px;border:1px solid #e2e8f0;background:#fff}
.select-main{font-weight:700}
.select-sub{grid-column:2;color:#64748b;font-size:13px}
.mini-panel{margin-top:14px;padding:12px 14px;border-radius:16px;background:#f8fbff;border:1px solid #dbeafe}
.doc-toolbar{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;padding:12px 14px;margin:0 0 18px;border:1px solid #dbeafe;border-radius:16px;background:linear-gradient(135deg,#f8fbff 0%,#eef5ff 100%)}
.doc-toolbar-copy{color:#475569;font-size:13px;line-height:1.7}
.doc-page{width:210mm;min-height:297mm;margin:0 auto;background:#fff;padding:14mm 12mm;box-shadow:0 16px 30px rgba(15,23,42,.08);border:1px solid #e2e8f0}
.doc-title{text-align:center;font-size:26px;font-weight:800;letter-spacing:.12em;margin-bottom:14px}
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
.file-preview{border:1px solid #e2e8f0;border-radius:18px;background:#fff;overflow:hidden}
.file-preview-head{padding:12px 14px;background:#0f172a;color:#fff;font-weight:700}
.simple-table{width:100%;border-collapse:collapse;font-size:13px}
.simple-table th,.simple-table td{border:1px solid #e2e8f0;padding:10px 12px;vertical-align:top}
.assign-input{width:100%;padding:9px 10px;border-radius:10px;border:1px solid #cbd5e1}
.detail-layout{display:grid;grid-template-columns:320px minmax(0,1fr);gap:18px}
.detail-image-card{background:#fff;border:1px solid #e2e8f0;border-radius:20px;padding:18px;display:flex;align-items:center;justify-content:center}
.detail-image-card img{max-height:320px;object-fit:contain}
.detail-list{display:grid;gap:10px}
.detail-row{display:grid;grid-template-columns:150px minmax(0,1fr);gap:12px;padding:10px 0;border-bottom:1px solid #e2e8f0}
.note-row td{background:#f8fafc}
@media (max-width: 1024px){
  .two-col,.detail-layout,.product-card,.summary-card-grid,.detail-grid,.form-grid{grid-template-columns:1fr}
  .doc-page{width:100%;min-height:auto;padding:24px}
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
const PREVIEW_STORAGE_KEY = "documentsPreviewStateV4";

function previewData() {
  return window.DOCUMENTS_PREVIEW_DATA || { clients: {}, products: {}, vendors: [], responseFiles: [], serviceLabels: {} };
}

function normalizeState(raw) {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
    return { items: {}, assignments: {}, vendorDraft: null };
  }
  if (raw.items || raw.assignments || Object.prototype.hasOwnProperty.call(raw, "vendorDraft")) {
    return {
      items: raw.items || {},
      assignments: raw.assignments || {},
      vendorDraft: raw.vendorDraft || null,
    };
  }
  return { items: raw, assignments: {}, vendorDraft: null };
}

function loadPreviewState() {
  try {
    return normalizeState(JSON.parse(window.localStorage.getItem(PREVIEW_STORAGE_KEY) || "{}"));
  } catch (error) {
    return { items: {}, assignments: {}, vendorDraft: null };
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

function formatYen(value) {
  const amount = Number(value) || 0;
  return `¥${amount.toLocaleString("ja-JP")}`;
}

function findClientByName(name) {
  const normalized = (name || "").trim();
  return Object.entries(previewData().clients || {}).find(([, client]) => client.name === normalized) || null;
}

function buildDocRows(items) {
  const products = previewData().products || {};
  const rows = [];
  for (let index = 0; index < 15; index += 1) {
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
    if (current.unavailable) return;
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
  showInlineNotice(noticeId, `${productName}（${serviceLabel(service)}）の状態を「${next}」へ更新し、クライアントへ通知する想定です。`);
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
  showInlineNotice(noticeId, `${productName} は受付不可としてクライアントへ通知する想定です。`);
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
  showInlineNotice(noticeId, `${productName} についてクライアントへメモを送信する想定です。内容: ${message}`);
}

function registerVendorFile() {
  const fileInput = document.getElementById("vendor-file");
  const dateInput = document.getElementById("vendor-file-date");
  const serviceInput = document.getElementById("vendor-file-service");
  if (!fileInput) return;
  const fileName = (fileInput.value || "").split("\\\\").pop();
  if (!fileName) {
    showInlineNotice("vendor-file-notice", "登録する回答ファイルを選択してください。");
    return;
  }
  const dateLabel = dateInput && dateInput.value ? dateInput.value : "未設定";
  const service = serviceInput ? serviceLabel(serviceInput.value) : "業者卸販売";
  showInlineNotice("vendor-file-notice", `${dateLabel} の ${service} 回答ファイル「${fileName}」を登録する想定です。`);
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
    const visible = !current.unavailable && row.dataset.service === service && current.status === expectedStatus;
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
}

function prepareEstimateDraft(targetHref) {
  const service = getQueryParam("service") || "wholesale";
  const selectedProducts = Array.from(document.querySelectorAll(".vendor-check:checked"))
    .map((node) => node.dataset.productId)
    .filter(Boolean);
  const vendorSelect = document.getElementById("vendor-target");
  const vendorName = vendorSelect ? vendorSelect.value : "";
  if (!selectedProducts.length) {
    showInlineNotice("vendor-draft-notice", "見積依頼書に入れる商品を1点以上選択してください。");
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
  const groups = buildReturnGroups();
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
    return `
      <div class="summary-card">
        <div class="summary-head">
          <div>
            <div class="summary-client">${client?.name || "クライアント"}</div>
            <div class="summary-meta">クライアントごとに、業者卸販売 / 業者オークション / 同時出品を分けて返送書類を作成します。</div>
          </div>
          <span class="pill payment">返送準備中</span>
        </div>
        <div class="compact-list" style="margin-top:14px;">${serviceCards}</div>
      </div>
    `;
  }).join("");
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
    const detailHref = product.real_item_id ? `/view/${product.real_item_id}` : product.detail_page;
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
            <div class="field-block"><div class="field-label">商品詳細</div><div class="field-value"><a href="${detailHref}">商品詳細を見る</a></div></div>
            <div class="field-block"><div class="field-label">返送書類</div><div class="field-value">買取明細書へ反映</div></div>
          </div>
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
  rowsTarget.innerHTML = buildDocRows(group.items);
  const total = group.items.reduce((sum, item) => sum + item.price, 0);
  document.querySelectorAll("[data-total-output]").forEach((node) => {
    node.textContent = formatYen(total);
  });
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
      if ((current.status || fallbackStatus) !== expected || current.unavailable) {
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

  document.querySelectorAll(".assign-input[data-product-id]").forEach((input) => {
    const assignment = getAssignments()[input.dataset.productId];
    if (assignment?.clientName) {
      input.value = assignment.clientName;
    }
  });

  syncVendorSelectionUI();
  renderBatchCreateMeta();
  renderVendorEstimateTemplate();
  renderStage4Summary();
  renderClientDelivery();
  renderStatementTemplate();
});
"""


DOC_EDITOR_JS = """
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
              <p>1番から4番までの流れを管理者向けにまとめています。トップでは 2×2 の導線だけを見せ、各段階の中身は個別ページで確認します。</p>
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

    write_text(OUTPUT_DIR / "documents_v2_vendor_outgoing.html", stage2_page())
    write_text(OUTPUT_DIR / "vendor_partner_registry.html", vendor_registry_page())
    write_text(OUTPUT_DIR / "vendor_estimate_batch_create.html", batch_create_page())
    write_text(OUTPUT_DIR / "documents_v2_vendor_estimate_template.html", estimate_template_page())
    write_text(OUTPUT_DIR / "documents_v2_vendor_incoming.html", stage3_page())
    for file_info in RESPONSE_FILES:
        write_text(OUTPUT_DIR / file_info["slug"], vendor_file_page(file_info))
        write_text(OUTPUT_DIR / file_info["download"], response_file_download_content(file_info))

    write_text(OUTPUT_DIR / "documents_v2_client_outgoing.html", stage4_page())
    write_text(OUTPUT_DIR / "documents_v2_client_delivery.html", client_delivery_page())
    write_text(OUTPUT_DIR / "documents_v2_client_statement_template.html", statement_template_page())
    for client_id, config in RETURN_GROUPS.items():
        group_key = f"{config['client']}__{config['service']}"
        write_text(OUTPUT_DIR / config["slug"], redirect_html(f"documents_v2_client_delivery.html?group={group_key}"))
        write_text(OUTPUT_DIR / config["template"], redirect_html(f"documents_v2_client_statement_template.html?group={group_key}"))


if __name__ == "__main__":
    build()
