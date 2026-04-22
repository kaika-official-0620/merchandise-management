from __future__ import annotations

from dataclasses import dataclass
from html import escape
from pathlib import Path
from textwrap import dedent


BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / ".preview-site-documents-clean"
STATIC_DIR = OUTPUT_DIR / "static"


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


VENDOR_FILES = [
    {
        "slug": "documents_v2_vendor_response_file_yamada_alma.html",
        "label": "2026/04/08 ブランドセンター 回答書 No.041",
        "month": "2026/04",
        "partner": "ブランドセンター",
        "items": [
            {"product": "lv_alma", "price": 185000, "result": "成約", "assigned_client": "山田 太郎"},
        ],
    },
    {
        "slug": "documents_v2_vendor_response_file_yamada_wallet.html",
        "label": "2026/04/21 Luxe Gate 回答書 No.053",
        "month": "2026/04",
        "partner": "Luxe Gate",
        "items": [
            {"product": "chanel_wallet", "price": 72000, "result": "成約", "assigned_client": "山田 太郎"},
        ],
    },
    {
        "slug": "documents_v2_vendor_response_file_sato_scarf.html",
        "label": "2026/04/15 ブランド市場 回答書 No.052",
        "month": "2026/04",
        "partner": "ブランド市場",
        "items": [
            {"product": "hermes_scarf", "price": 58000, "result": "成約", "assigned_client": "佐藤 花子"},
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


def status_class(status: str) -> str:
    return {
        "認証待ち": "pending",
        "認証済み": "approved",
        "査定中": "appraising",
        "入金待ち": "payment",
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
    cards = [
        ("1", "クライアントから受付", "どのクライアントから何の商品が届いたかを、名前単位で確認します。", "documents_v2_client_incoming.html"),
        ("2", "開花から業者へ依頼", "査定中に切り替えた業者卸販売の商品だけをまとめて業者へ流します。", "documents_v2_vendor_outgoing.html"),
        ("3", "業者から回答受領", "業者から届いた回答ファイルを1件ずつ確認し、商品ごとに返送先を振り分けます。", "documents_v2_vendor_incoming.html"),
        ("4", "クライアントへ返送", "クライアントごとに書類をまとめて作成し、返送内容を確認します。", "documents_v2_client_outgoing.html"),
    ]
    inner = []
    for step, heading, desc, href in cards:
        count = {"1": "4件受付中", "2": "2件準備中", "3": "3ファイル受領", "4": "4名へ返送準備"}.get(step, "")
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


def client_summary_card(client_id: str, products: list[dict], *, service_label: str) -> str:
    client = CLIENTS[client_id]
    status_summary = " / ".join(f"{p['status']} 1" for p in products)
    product_names = " / ".join(p["name"] for p in products)
    request_detail = products[0]["request_detail"] if products else "documents_v2_client_incoming.html"
    return f"""
    <div class="summary-card">
      <div class="summary-head">
        <div>
          <div class="summary-client">{client["name"]}</div>
          <div class="summary-meta">{client["request_id"]} / 受付日 {client["received_at"]} / {service_label}</div>
        </div>
        <span class="pill {status_class(products[0]["status"])}">{products[0]["status"]}</span>
      </div>
      <div class="summary-card-grid">
        <div class="field-block"><div class="field-label">申請商品数</div><div class="field-value">{len(products)}点</div></div>
        <div class="field-block"><div class="field-label">依頼書</div><div class="field-value">認証申請一式</div></div>
        <div class="field-block"><div class="field-label">商品名</div><div class="field-value">{product_names}</div></div>
        <div class="field-block"><div class="field-label">次の流れ</div><div class="field-value">詳細で確認後、査定中にした商品だけ2番へ送ります</div></div>
      </div>
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
            filtered_clients.append(client_summary_card(client_id, items, service_label=SERVICE_LABELS.get(service or "all", "すべてのサービス")))
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


def status_controls(product_id: str, status: str, product_name: str) -> str:
    select_id = f"status-{product_id}"
    badge_id = f"badge-{product_id}"
    state_id = f"state-{product_id}"
    notice_id = f"notice-{product_id}"
    options = "".join(
        f'<option value="{label}" {"selected" if label == status else ""}>{label}</option>'
        for label in ["認証済み", "査定中", "入金待ち"]
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
        <button class="btn btn-primary" type="button" onclick="applyStatus('{select_id}','{badge_id}','{state_id}','{notice_id}','{escape(product_name)}')">状態を更新して通知</button>
      </div>
      <div class="status-row">
        <button class="btn btn-outline" type="button" onclick="notifyUnavailable('{notice_id}','{badge_id}','{state_id}','{escape(product_name)}')">受付不可を通知する</button>
      </div>
      <p id="{notice_id}" class="inline-notice" hidden></p>
    </div>
    """


def product_card(product_id: str) -> str:
    product = PRODUCTS[product_id]
    client = CLIENTS[product["client"]]
    return f"""
    <div class="product-card">
      <div class="product-thumb">
        <img src="{product['image']}" alt="{escape(product['name'])}">
      </div>
      <div class="product-body">
        <div class="product-top">
          <div>
            <div class="product-title"><a href="{product['detail_page']}">{product['name']}</a></div>
            <div class="product-meta">{client['name']} / {SERVICE_LABELS[product['service']]} / {product['brand']} / 商品ID {product['code']}</div>
          </div>
          <span id="badge-{product_id}" class="pill {status_class(product['status'])}">{product['status']}</span>
        </div>
        <div class="detail-grid">
          <div class="field-block"><div class="field-label">カテゴリ</div><div class="field-value">{product['category']}</div></div>
          <div class="field-block"><div class="field-label">ブランド</div><div class="field-value">{product['brand']}</div></div>
          <div class="field-block"><div class="field-label">モデル</div><div class="field-value">{product['model']}</div></div>
          <div class="field-block"><div class="field-label">状態</div><div class="field-value">{product['condition']}</div></div>
        </div>
        <div class="card-actions">
          <a class="btn btn-soft" href="{product['detail_page']}">商品詳細を見る</a>
        </div>
        {status_controls(product_id, product['status'], product['name'])}
      </div>
    </div>
    """


def request_detail_page(client_id: str) -> str:
    client = CLIENTS[client_id]
    products = client_products(client_id)
    service_summary = " / ".join(sorted({SERVICE_LABELS[p["service"]] for p in products}))
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
                <p class="section-note">商品名を押すと、商品一覧で登録している内容をそのまま確認できます。</p>
              </div>
            </div>
            <div class="stack">
              {''.join(product_card(item['id']) for item in products)}
            </div>
          </div>
        </div>
        """,
    )


def product_detail_page(product_id: str) -> str:
    product = PRODUCTS[product_id]
    client = CLIENTS[product["client"]]
    detail_rows = [
        ("クライアント名", client["name"]),
        ("サービス", SERVICE_LABELS[product["service"]]),
        ("商品ID", product["code"]),
        ("ブランド", product["brand"]),
        ("商品名", product["name"]),
        ("カテゴリ", product["category"]),
        ("状態", product["condition"]),
        ("現在の進行状態", product["status"]),
        ("メモ", "商品一覧に登録済みの内容を、そのまま受付画面から確認できる想定です。"),
    ]
    rows_html = "".join(
        f'<div class="detail-row"><div class="field-label">{label}</div><div class="field-value">{value}</div></div>'
        for label, value in detail_rows
    )
    return page(
        f"{product['name']} の商品詳細",
        f"""
        <div class="page">
          {back_bar()}
          <div class="page-head">
            <div>
              <h1>商品詳細</h1>
              <p>{product['name']} の登録内容を、そのまま確認するための画面です。</p>
            </div>
          </div>
          <div class="detail-layout">
            <div class="detail-image-card">
              <img src="{product['image']}" alt="{escape(product['name'])}">
            </div>
            <div class="section detail-section">
              <div class="section-head">
                <div>
                  <h3>{product['name']}</h3>
                  <p class="section-note">クライアント申請と商品一覧を照らし合わせるための詳細です。</p>
                </div>
              </div>
              <div class="detail-list">
                {rows_html}
              </div>
            </div>
          </div>
        </div>
        """,
    )


def outgoing_products() -> list[dict]:
    items = []
    for product_id in ["lv_alma", "hermes_scarf"]:
        product = dict(PRODUCTS[product_id])
        product["id"] = product_id
        items.append(product)
    return items


def stage2_page() -> str:
    cards = []
    for product in outgoing_products():
        client = CLIENTS[product["client"]]
        cards.append(
            f"""
            <div class="product-card">
              <div class="product-thumb">
                <img src="{product['image']}" alt="{escape(product['name'])}">
              </div>
              <div class="product-body">
                <div class="product-top">
                  <div>
                    <div class="product-title">{product['name']}</div>
                    <div class="product-meta">{client['name']} / {product['brand']} / {SERVICE_LABELS[product['service']]} / 商品ID {product['code']}</div>
                  </div>
                  <span class="pill appraising">査定中</span>
                </div>
                <div class="detail-grid">
                  <div class="field-block"><div class="field-label">画像確認</div><div class="field-value"><a href="{product['detail_page']}">商品詳細を見る</a></div></div>
                  <div class="field-block"><div class="field-label">送付先候補</div><div class="field-value">ブランドセンター / ブランド市場</div></div>
                  <div class="field-block"><div class="field-label">次の流れ</div><div class="field-value">チェックした商品をまとめて見積依頼書へ差し込みます</div></div>
                </div>
              </div>
            </div>
            """
        )
    return page(
        "2. 開花から業者へ依頼",
        f"""
        <div class="page">
          {back_bar()}
          <div class="page-head">
            <div>
              <h1>2. 開花から業者へ依頼</h1>
              <p>1番で査定中に切り替えた業者卸販売の商品だけがここへ集まります。複数クライアント分の商品を選んで、まとめて業者向け見積依頼書を作成します。</p>
            </div>
          </div>
          <div class="section">
            <div class="section-head">
              <div>
                <h3>業者へ流す商品一覧</h3>
                <p class="section-note">誰の商品か、どの商品か、どの画像かをここで確認してから書類を作成します。</p>
              </div>
            </div>
            <div class="stack">
              {''.join(cards)}
            </div>
            <div class="card-actions" style="margin-top: 18px;">
              <a class="btn btn-outline" href="vendor_partner_registry.html">送付先業者を登録・編集する</a>
              <a class="btn btn-primary" href="vendor_estimate_batch_create.html">見積依頼書を作成する</a>
            </div>
          </div>
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
    for product in outgoing_products():
        client = CLIENTS[product["client"]]
        rows.append(
            f"""
            <label class="select-row">
              <input class="vendor-check" type="checkbox" data-product="{escape(product['name'])}" data-summary="{client['name']} / {product['name']} / {product['brand']}">
              <span class="select-main">{client['name']} / {product['name']}</span>
              <span class="select-sub">{product['brand']} / 商品ID {product['code']}</span>
            </label>
            """
        )
    vendor_options = "".join(f'<option>{vendor["name"]}</option>' for vendor in VENDORS)
    return page(
        "見積依頼書を作成する",
        f"""
        <div class="page">
          {back_bar()}
          <div class="page-head">
            <div>
              <h1>見積依頼書を作成する</h1>
              <p>ここでは、業者へ流したい商品をチェックして既存の見積依頼書テンプレートへ差し込みます。件名や本文ではなく、商品・送付先・テンプレートだけを整える形です。</p>
            </div>
          </div>
          <div class="two-col">
            <div class="section">
              <div class="section-head"><h3>見積依頼書に入れる商品</h3></div>
              <div class="select-grid">{''.join(rows)}</div>
            </div>
            <div class="section">
              <div class="section-head"><h3>見積依頼書の設定</h3></div>
              <div class="form-grid">
                <label class="field"><span>送付先業者</span><select><option>選択してください</option>{vendor_options}</select></label>
                <label class="field"><span>書類種別</span><select><option>見積依頼書</option></select></label>
                <label class="field field-wide"><span>備考</span><textarea rows="4" placeholder="必要な場合だけ備考を入力"></textarea></label>
              </div>
              <div class="mini-panel">
                <strong>選択中の商品</strong>
                <div id="vendor-selected-summary" style="margin-top:8px;color:#475569;">まだ商品を選択していません</div>
              </div>
              <div class="card-actions" style="margin-top: 16px;">
                <a class="btn btn-outline" href="vendor_partner_registry.html">送付先業者を登録・編集する</a>
                <a class="btn btn-primary" href="documents_v2_vendor_estimate_template.html">テンプレートへ差し込む</a>
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
            rows.append(
                f"""
                <tr>
                  <td class="doc-col-no">{idx + 1}</td>
                  <td class="doc-col-name">{product['name']}</td>
                  <td class="doc-col-brand">{product['brand']}</td>
                  <td class="doc-col-condition">{product['condition']}</td>
                  <td class="doc-col-qty">1</td>
                  <td class="doc-col-price doc-cell-price" data-amount="{item[price_key]}">{money(item[price_key])}</td>
                  <td class="doc-col-price doc-cell-amount" data-amount="{item[price_key]}">{money(item[price_key])}</td>
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


def estimate_template_page() -> str:
    items = [
        {"product": "lv_alma", "price": 185000},
        {"product": "hermes_scarf", "price": 58000},
        {"product": "chanel_wallet", "price": 72000},
    ]
    total = sum(item["price"] for item in items)
    return page(
        "見積依頼書",
        f"""
        <div class="page page-narrow">
          <div class="doc-toolbar">
            <div class="doc-toolbar-copy">既存の見積依頼書テンプレートに、選択した商品を差し込む preview です。</div>
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
              <div><strong>送付先</strong> ブランドセンター 御中</div>
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
              <tbody>
                {doc_rows(items)}
              </tbody>
            </table>
            <div class="doc-total">
              <span>合計金額</span>
              <strong data-total-output>{money(total)}</strong>
            </div>
            <div class="doc-note">
              <strong>備考</strong>
              <p>送付先業者はテンプレート外で管理し、書類自体は必要に応じて編集できるようにしています。</p>
            </div>
          </div>
        </div>
        """,
        extra_scripts='<script src="static/doc_editor.js"></script>',
    )


def stage3_page() -> str:
    file_cards = []
    for file_info in VENDOR_FILES:
        products = " / ".join(PRODUCTS[item["product"]]["name"] for item in file_info["items"])
        file_cards.append(
            f"""
            <div class="file-card">
              <div class="file-head">
                <div>
                  <div class="file-title">{file_info['label']}</div>
                  <div class="file-meta">{file_info['partner']} / {file_info['month']} / 商品 {len(file_info['items'])}点</div>
                </div>
                <span class="pill approved">回答受領</span>
              </div>
              <p class="section-note">{products}</p>
              <div class="card-actions">
                <a class="btn btn-soft" href="{file_info['slug']}">ファイルを開く</a>
              </div>
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
                <p class="section-note">ファイル選択と登録ボタンは枠内に収まるようにし、登録後は同じ一覧に増えていくイメージです。</p>
              </div>
            </div>
            <div class="form-inline">
              <label class="field file-field"><span>回答ファイル</span><input id="vendor-file" type="file"></label>
              <label class="field"><span>取引日</span><input id="vendor-file-date" type="date" value="2026-04-23"></label>
              <button class="btn btn-primary" type="button" onclick="registerVendorFile()">ファイルを登録</button>
            </div>
            <p id="vendor-file-notice" class="inline-notice" hidden></p>
          </div>
          <div class="section">
            <div class="section-head">
              <div>
                <h3>登録済みの回答ファイル</h3>
                <p class="section-note">ファイルごとに開き、商品ごとの売却額とクライアント振り分けを確認します。</p>
              </div>
            </div>
            <div class="stack">{''.join(file_cards)}</div>
          </div>
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
                <input class="assign-input" list="{datalist_id}" id="assign-{item['product']}" value="{item['assigned_client']}" placeholder="クライアント名を検索">
                <datalist id="{datalist_id}">{options}</datalist>
              </td>
              <td>
                <button class="btn btn-soft" type="button" onclick="assignClient('assign-{item['product']}','assign-note-{item['product']}','{escape(product['name'])}')">振り分ける</button>
              </td>
            </tr>
            <tr class="note-row">
              <td colspan="6"><p id="assign-note-{item['product']}" class="inline-note">この商品は 4番でクライアント返送書類を作るための候補です。</p></td>
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
              <p>{file_info['partner']} から届いた回答ファイルを開き、商品ごとにどのクライアントへ返送するかを決める画面です。</p>
            </div>
          </div>
          <div class="two-col">
            <div class="section">
              <div class="section-head"><h3>ファイルの中身を確認</h3></div>
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


def stage4_page() -> str:
    cards = []
    for client_id, config in RETURN_GROUPS.items():
        client = CLIENTS[client_id]
        total = sum(item["price"] for item in config["items"])
        service_labels = " / ".join(sorted({SERVICE_LABELS[PRODUCTS[item["product"]]["service"]] for item in config["items"]}))
        cards.append(
            f"""
            <div class="summary-card">
              <div class="summary-head">
                <div>
                  <div class="summary-client">{client['name']}</div>
                  <div class="summary-meta">{len(config['items'])}点 / {service_labels}</div>
                </div>
                <span class="pill payment">返送準備中</span>
              </div>
              <div class="summary-card-grid">
                <div class="field-block"><div class="field-label">対象商品</div><div class="field-value">{' / '.join(PRODUCTS[item['product']]['name'] for item in config['items'])}</div></div>
                <div class="field-block"><div class="field-label">合計予定金額</div><div class="field-value">{money(total)}</div></div>
                <div class="field-block"><div class="field-label">返送書類</div><div class="field-value">買取明細書</div></div>
                <div class="field-block"><div class="field-label">元データ</div><div class="field-value">{' / '.join(item['source'] for item in config['items'])}</div></div>
              </div>
              <div class="card-actions">
                <a class="btn btn-soft" href="{config['slug']}">返送内容を確認する</a>
                <a class="btn btn-primary" href="{config['template']}">買取明細書を作成する</a>
              </div>
            </div>
            """
        )
    return page(
        "4. クライアントへ返送",
        f"""
        <div class="page">
          {back_bar()}
          <div class="page-head">
            <div>
              <h1>4. クライアントへ返送</h1>
              <p>クライアント名ごとに返送対象をまとめ、売却済みの商品だけを選んで買取明細書を作成します。商品名ではなくクライアント名単位で表示します。</p>
            </div>
          </div>
          <div class="section">
            <div class="section-head">
              <div>
                <h3>返送対象のクライアント一覧</h3>
                <p class="section-note">まずクライアント名で見て、そのあと詳細画面でどの商品を返送書類へ乗せるか確認します。</p>
              </div>
            </div>
            <div class="summary-grid">
              {''.join(cards)}
            </div>
          </div>
        </div>
        """,
    )


def client_delivery_page(client_id: str) -> str:
    client = CLIENTS[client_id]
    config = RETURN_GROUPS[client_id]
    cards = []
    for item in config["items"]:
        product = PRODUCTS[item["product"]]
        cards.append(
            f"""
            <div class="product-card">
              <div class="product-thumb">
                <img src="{product['image']}" alt="{escape(product['name'])}">
              </div>
              <div class="product-body">
                <div class="product-top">
                  <div>
                    <div class="product-title">{product['name']}</div>
                    <div class="product-meta">{SERVICE_LABELS[product['service']]} / {item['source']}</div>
                  </div>
                  <span class="pill payment">返送予定</span>
                </div>
                <div class="detail-grid">
                  <div class="field-block"><div class="field-label">売却額</div><div class="field-value">{money(item['price'])}</div></div>
                  <div class="field-block"><div class="field-label">商品詳細</div><div class="field-value"><a href="{product['detail_page']}">商品詳細を見る</a></div></div>
                  <div class="field-block"><div class="field-label">返送書類</div><div class="field-value">買取明細書へ反映</div></div>
                </div>
              </div>
            </div>
            """
        )
    return page(
        f"{client['name']} さんへの返送内容",
        f"""
        <div class="page">
          {back_bar()}
          <div class="page-head">
            <div>
              <h1>{client['name']} さんへの返送内容</h1>
              <p>ここでどの商品を返送書類に含めるかを確認し、買取明細書へ進みます。</p>
            </div>
          </div>
          <div class="section">
            <div class="section-head">
              <div>
                <h3>返送する商品</h3>
                <p class="section-note">キャンセル済みの商品は含めず、売却済みの商品のみを返送対象にします。</p>
              </div>
            </div>
            <div class="stack">{''.join(cards)}</div>
            <div class="card-actions" style="margin-top: 18px;">
              <a class="btn btn-primary" href="{config['template']}">買取明細書を作成する</a>
            </div>
          </div>
        </div>
        """,
    )


def statement_template_page(client_id: str) -> str:
    client = CLIENTS[client_id]
    config = RETURN_GROUPS[client_id]
    total = sum(item["price"] for item in config["items"])
    return page(
        f"{client['name']} さん向け買取明細書",
        f"""
        <div class="page page-narrow">
          <div class="doc-toolbar">
            <div class="doc-toolbar-copy">既存の買取明細書テンプレートに、売却済み商品を差し込んだ preview です。</div>
            <div class="doc-toolbar-actions">
              <a class="btn btn-outline" href="{config['slug']}">返送一覧へ戻る</a>
              <button class="btn btn-soft" type="button" data-action="toggle-edit">書類を編集する</button>
              <button class="btn btn-outline" type="button" data-action="reset-doc">入力を元に戻す</button>
            </div>
          </div>
          <div class="doc-page" data-doc-editor>
            <div class="doc-title">買取明細書</div>
            <div class="doc-meta">
              <div><strong>発行日</strong> 2026年4月23日</div>
              <div><strong>宛名</strong> {client['name']} 様</div>
              <div><strong>発行者</strong> 株式会社開花</div>
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
              <tbody>
                {doc_rows(config['items'])}
              </tbody>
            </table>
            <div class="doc-total">
              <span>合計金額</span>
              <strong data-total-output>{money(total)}</strong>
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
.pill.payment{background:#dbeafe;color:#1d4ed8;padding:6px 10px}
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
function togglePanel(id) {
  const panel = document.getElementById(id);
  if (!panel) return;
  panel.hidden = !panel.hidden;
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
    case "入金待ち": return "payment";
    case "受付不可": return "unavailable";
    default: return "approved";
  }
}

function applyStatus(selectId, badgeId, stateId, noticeId, productName) {
  const select = document.getElementById(selectId);
  const badge = document.getElementById(badgeId);
  const state = document.getElementById(stateId);
  if (!select || !badge || !state) return;
  const next = select.value;
  badge.textContent = next;
  badge.className = `pill ${statusClass(next)}`;
  state.textContent = next;
  showInlineNotice(noticeId, `${productName} の状態を「${next}」へ更新し、クライアントへ通知する想定です。`);
}

function notifyUnavailable(noticeId, badgeId, stateId, productName) {
  const badge = document.getElementById(badgeId);
  const state = document.getElementById(stateId);
  if (badge) {
    badge.textContent = "受付不可";
    badge.className = "pill unavailable";
  }
  if (state) {
    state.textContent = "受付不可";
  }
  showInlineNotice(noticeId, `${productName} は受付不可としてクライアントへ通知する想定です。`);
}

function registerVendorFile() {
  const fileInput = document.getElementById("vendor-file");
  const dateInput = document.getElementById("vendor-file-date");
  if (!fileInput) return;
  const fileName = (fileInput.value || "").split("\\\\").pop();
  if (!fileName) {
    showInlineNotice("vendor-file-notice", "登録する回答ファイルを選択してください。");
    return;
  }
  const dateLabel = dateInput && dateInput.value ? dateInput.value : "未設定";
  showInlineNotice("vendor-file-notice", `${dateLabel} の回答ファイル「${fileName}」を登録する想定です。`);
}

function assignClient(inputId, noticeId, productName) {
  const input = document.getElementById(inputId);
  if (!input) return;
  const client = input.value || "未選択";
  showInlineNotice(noticeId, `${productName} を「${client}」の返送候補へ振り分ける想定です。`);
}

document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll(".vendor-check").forEach((checkbox) => {
    checkbox.addEventListener("change", () => {
      const names = Array.from(document.querySelectorAll(".vendor-check:checked")).map((node) => node.dataset.product);
      const summary = document.getElementById("vendor-selected-summary");
      if (summary) {
        summary.textContent = names.length ? names.join(" / ") : "まだ商品を選択していません";
      }
    });
  });
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


def build() -> None:
    write_text(STATIC_DIR / "preview_v2.css", PREVIEW_CSS)
    write_text(STATIC_DIR / "preview_v2.js", PREVIEW_JS)
    write_text(STATIC_DIR / "doc_editor.js", DOC_EDITOR_JS)

    write_text(OUTPUT_DIR / "documents_v2_index.html", page(
        "書類管理 preview",
        f"""
        <div class="page">
          <div class="page-head">
            <div>
              <h1>書類管理 preview</h1>
              <p>以前の使用感に近い形で、1番から4番までの流れをまとめ直した preview です。トップでは 2×2 の導線だけを見せ、各段階の中身は個別ページで確認します。</p>
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
        write_text(OUTPUT_DIR / f"documents_v2_request_detail_{client_id}.html", request_detail_page(client_id))

    for product_id in PRODUCTS:
        write_text(OUTPUT_DIR / PRODUCTS[product_id]["detail_page"], product_detail_page(product_id))

    write_text(OUTPUT_DIR / "documents_v2_vendor_outgoing.html", stage2_page())
    write_text(OUTPUT_DIR / "vendor_partner_registry.html", vendor_registry_page())
    write_text(OUTPUT_DIR / "vendor_estimate_batch_create.html", batch_create_page())
    write_text(OUTPUT_DIR / "documents_v2_vendor_estimate_template.html", estimate_template_page())
    write_text(OUTPUT_DIR / "documents_v2_vendor_incoming.html", stage3_page())
    for file_info in VENDOR_FILES:
        write_text(OUTPUT_DIR / file_info["slug"], vendor_file_page(file_info))

    write_text(OUTPUT_DIR / "documents_v2_client_outgoing.html", stage4_page())
    for client_id in RETURN_GROUPS:
        write_text(OUTPUT_DIR / RETURN_GROUPS[client_id]["slug"], client_delivery_page(client_id))
        write_text(OUTPUT_DIR / RETURN_GROUPS[client_id]["template"], statement_template_page(client_id))


if __name__ == "__main__":
    build()
