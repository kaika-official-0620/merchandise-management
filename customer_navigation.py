"""Customer navigation only: no migrations, balances, records or external calls.

The destinations keep their existing authentication, ownership and plan guards.
This module groups them; a visible link never grants access to its destination.
"""
from flask import Blueprint, abort, current_app, g, render_template, request, url_for
from flask_login import current_user, login_required


SECTIONS = (
    ('home', 'ホーム', '今の状況と、次に必要な操作を確認します。'),
    ('products', '商品・在庫', '自分の商品を登録し、保管場所や販売の記録を確認します。'),
    ('requests', '開花への依頼', '預け入れから販売まで、開花への依頼と進み具合を確認します。'),
    ('money', '売上・支払い', '売上、費用、支払いと利用プランを確認します。'),
    ('documents', '書類', '必要な書類を作り、届いた書類や過去の履歴を確認します。'),
    ('support', 'お知らせ・サポート', '開花からの連絡、問い合わせ、通知とアカウントの設定をまとめています。'),
)
SECTION_KEYS = {section[0] for section in SECTIONS}
STAGE_FLAGS = ('STAGE2_INVENTORY_ENABLED', 'STAGE3_FINANCE_ENABLED',
               'STAGE4_COMMERCE_ENABLED', 'STAGE5_PAYMENTS_ENABLED', 'STAGE6_ENABLED')


def is_customer():
    return bool(current_user.is_authenticated and getattr(current_user, 'role', None) == 'user'
                and not current_user.is_admin())


def stage_available(stage):
    return all(current_app.config.get(flag) is True for flag in STAGE_FLAGS[:stage - 1])


def destination(endpoint, **values):
    if endpoint not in current_app.view_functions:
        return None
    return url_for(endpoint, **values)


def current_section(endpoint, args=None):
    """Use server-resolved endpoints, never a user-supplied return URL."""
    endpoint = endpoint or ''
    if endpoint == 'customer_navigation.hub':
        value = (args or {}).get('section_key')
        return value if value in SECTION_KEYS else None
    if endpoint == 'customer_navigation.guide':
        return 'support'
    if endpoint in ('user_home', 'workspace_home.home'):
        return 'home'
    if endpoint.startswith(('workspace_home.', 'account_lifecycle.', 'inquiry_')) or endpoint in (
            'profile', 'user_announcements_page', 'user_announcement_detail_page'):
        return 'support'
    if endpoint.startswith(('finance_review.', 'payment_review.', 'sandbox_payments.', 'feature_plans.', 'report_')) or endpoint in (
            'user_analytics', 'reports'):
        return 'money'
    if endpoint.startswith(('user_document', 'user_mitsumori', 'user_kaitori_shoudaku', 'user_shikiriosho', 'user_invoice', 'documents')) or endpoint == 'service_document_view':
        return 'documents'
    if endpoint.startswith(('inventory_intake', 'public_proxy_service', 'sales_agency_')) or endpoint in (
            'commerce.listing_index', 'commerce.listing_item', 'commerce.listing_action'):
        return 'requests'
    if endpoint.startswith(('inventory_registry.', 'self_inventory', 'commerce.')) or endpoint in (
            'index', 'view_item', 'edit_item', 'add_item', 'long_term_items'):
        return 'products'
    return None


def navigation():
    if not is_customer():
        return [], None
    if hasattr(g, '_customer_navigation'):
        return g._customer_navigation
    key = current_section(request.endpoint, request.view_args)
    entries = []
    for section_key, title, description in SECTIONS:
        if section_key == 'home':
            href = destination('workspace_home.home') if stage_available(6) else None
            href = href or destination('user_home') or destination('index')
        else:
            href = destination('customer_navigation.hub', section_key=section_key)
        if href:
            entries.append(dict(key=section_key, title=title, description=description,
                                url=href, active=section_key == key))
    selected = next((entry for entry in entries if entry['active']), None)
    g._customer_navigation = (entries, selected)
    return entries, selected


def section_cards(key):
    """Only route metadata and the existing capability helper; no new data scan."""
    cards = []
    plans = current_app.extensions.get('kaika_feature_plans')
    plan_enabled = bool(plans and plans.enabled)
    intake_available = plan_enabled or stage_available(2)

    def add(endpoint, title, description, *, enabled=True, secondary=False, group='', badge=None, **values):
        href = destination(endpoint, **values) if enabled else None
        if href:
            cards.append(dict(title=title, description=description, url=href,
                              secondary=secondary, group=group, badge=badge))

    can_register = False
    can_request_intake = False
    if key in ('products', 'requests') and intake_available and plans:
        try:
            can_register = plans.has_feature('inventory_manage') is True
            # Free self-management may remain available while new Kaika work
            # is restricted. Match InventoryCustody.require_user exactly here.
            if key == 'requests' and can_register:
                can_request_intake = bool(current_user.can_edit_merchandise())
        except Exception:
            # An unavailable capability check must not advertise an actionable
            # write or turn a read-only menu into an error page.
            current_app.logger.warning('Customer navigation capability check unavailable')

    if key == 'products':
        add('index', '商品一覧', '自分で保管する商品と、開花へ預けた商品を一覧で確認します。')
        add('self_inventory_new', '自分の商品を登録', '自分で保管・販売する商品の写真と情報を登録します。', enabled=can_register)
        add('inventory_registry.index', '保管場所・作業履歴', '商品の保管先、契約ごとの預かり期限、受領・撮影・発送の記録を確認します。', enabled=stage_available(2))
        add('commerce.index', '開花の商品を買う', '開花の商品選びから購入までの確認画面です。現在はテスト資金を使い、実購入は発生しません。', enabled=stage_available(4), badge='検証中')
        add('commerce.orders', '購入・予約の履歴', '選んだ商品の予約と購入の検証記録を確認します。', enabled=stage_available(4), secondary=True, badge='検証中')
        add('long_term_items', '以前の預かり記録', '以前の在庫記録を参照します。経過日数だけで預かり期限や処分は決まりません。', secondary=True)
    elif key == 'requests':
        add('inventory_intake_new', '開花へ預ける', '商品を開花へ送り、撮影・登録などを依頼します。', enabled=can_request_intake)
        add('inventory_intakes', '依頼・受付状況', '依頼した商品の受付、受領と登録の進み具合を確認します。', enabled=intake_available)
        add('commerce.listing_index', '出品内容を確認', '写真、説明、販売価格を確認し、出品案を承認するための確認画面です。外部への出品は未接続です。', enabled=stage_available(4), badge='検証中')
        add('sales_agency_my_requests', '販売依頼の履歴', 'これまでの業者販売などの申請と対応状況を確認します。', secondary=True)
        add('public_proxy_service_history', '以前の仕入れ履歴', '以前の代行仕入れ・オークションの取引記録を確認します。', secondary=True)
    elif key == 'money':
        add('finance_review.index', '商品ごとの料金・利益', '売上から費用を差し引いた手残りと、料金の根拠を確認します。未確認の金額は確定額ではありません。', enabled=stage_available(3))
        add('finance_review.customer', '月ごとの請求・資金記録', '月別の請求下書きと資金の記録を確認します。下書きはまだ実請求ではありません。', enabled=stage_available(3), customer_id=int(current_user.get_id()), badge='検証中')
        add('payment_review.index', '支払い状況を確認', '月額と現物料金の支払いを確認する検証画面です。実際の引落しは行いません。', enabled=stage_available(5), badge='検証中')
        add('feature_plans.plans', '利用プランを確認', '現在の利用プランと、使える機能を確認します。', enabled=plan_enabled)
        add('user_analytics', '売上の分析', 'これまでの売上や商品の動きを確認します。')
        add('reports', 'レポート・CSV', '期間別の集計を確認し、必要なデータを書き出します。', secondary=True)
    elif key == 'documents':
        add('user_document_list', '書類を作る', '見積依頼書や買取承諾書など、必要な書類を作成します。')
        add('documents', '届いた書類・履歴', '請求書や精算書、これまでに作成・受領した書類を確認します。')
    elif key == 'support':
        add('customer_navigation.guide', '使い方・よくある質問', '商品の登録、書類の送信、アプリの確認方法を目的別に確認できます。')
        add('workspace_home.inbox', '自分宛てのお知らせ', '商品の確認依頼など、あなた宛てのお知らせを確認します。', enabled=stage_available(6))
        add('user_announcements_page', '開花からのお知らせ', 'サービスの案内や、開花からのお知らせを確認します。')
        add('inquiry_list', '問い合わせ・返信', '送った問い合わせと、開花からの返信を確認します。')
        add('inquiry_new', '問い合わせを送る', '操作や商品について分からないことを相談します。')
        add('workspace_home.settings', '通知設定', 'プッシュ・メール・LINEの通知先と受取設定を確認します。', enabled=stage_available(6), secondary=True)
        add('profile', 'アカウント設定', '登録情報とアカウントの設定を確認します。', secondary=True)
        add('account_lifecycle.page', '退会・アカウント削除', '削除申請の内容と、商品や必要な取引記録の扱いを確認します。', enabled=current_app.config.get('ACCOUNT_LIFECYCLE_ENABLED') is True, secondary=True)
    return cards


def register_customer_navigation(runtime):
    app = runtime.app
    if 'customer_navigation' in app.blueprints:
        return app.extensions['customer_navigation']
    bp = Blueprint('customer_navigation', __name__)

    @bp.get('/my/support/guide')
    @login_required
    def guide():
        if not is_customer():
            abort(403)
        sections = [dict(key=key, title=title, cards=section_cards(key))
                    for key, title, _ in SECTIONS if key not in ('home', 'support')]
        return render_template('customer_navigation/guide.html', guide_sections=sections,
                               inquiry_url=destination('inquiry_new'),
                               history_url=destination('inquiry_list'),
                               private_preview=bool(current_app.extensions.get('kaika_staging_review')),
                               home_url=destination('workspace_home.home') if stage_available(6) else destination('user_home'))

    @bp.get('/my/<section_key>')
    @login_required
    def hub(section_key):
        if not is_customer():
            abort(403)
        if section_key not in SECTION_KEYS - {'home'}:
            abort(404)
        entries, selected = navigation()
        section = dict(selected, cards=section_cards(section_key))
        return render_template('customer_navigation/hub.html', section=section, navigation=entries)

    @bp.after_request
    def private(response):
        response.headers.update({'Cache-Control': 'private, no-store', 'Referrer-Policy': 'same-origin',
                                 'X-Content-Type-Options': 'nosniff'})
        return response

    @app.context_processor
    def context():
        entries, selected = navigation()
        return dict(customer_navigation_available=is_customer(), customer_nav_sections=entries,
                    customer_nav_current=selected)

    app.register_blueprint(bp)
    app.extensions['customer_navigation'] = bp
    return bp
