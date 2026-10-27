"""Read-only HTTPS checks for the current dedicated Render staging review UI.

Run: python scripts/verify_deployed_staging_review.py

Run only in that service's Render Shell. Uses its existing environment secrets;
never reads .env, accepts an alternate host, prints credentials or prints pages.
Only POST /login is sent (login history/session state may change). All other
requests are fixed GETs; no inventory, finance, notification or deletion actions
are submitted. This does not verify real payments, delivery or native devices.
"""
from __future__ import annotations

import argparse
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from staging_environment import validate_environment
from scripts.verify_deployed_staging import (
    DEPLOYED_HOST, GuardedSession, VerificationStopped, same_origin_url,
)


HUBS = (
    ('products', '商品・在庫'), ('requests', '開花への依頼'),
    ('money', '売上・支払い'), ('documents', '書類'),
    ('support', 'お知らせ・サポート'),
)
COMMON_PAGES = (
    ('stage2_inventory', '/inventory/items', '保管場所・作業履歴'),
    ('stage3_finance', '/finance', '利益・請求下書き'),
    ('stage4_catalog', '/commerce', '開花の商品'),
    ('stage4_orders', '/commerce/orders', '購入・予約の記録'),
    ('stage4_approvals', '/commerce/listings', '出品内容・両者承認'),
    ('stage5_payments', '/payments', '月額・現物料金のお支払い'),
)
STAFF_PAGES = (
    ('stage2_reconciliation', '/inventory/reconcile', '既存在庫の照合'),
    ('stage2_import_preview', '/inventory/imports', 'Excel・CSVの確認'),
    ('stage4_channels', '/commerce/channels', '販路・受注・発送'),
    ('stage7_pilot', '/pilot-review', '小規模実証の準備'),
    ('stage7_cohorts', '/pilot-review/cohorts', '受付した商品の経過'),
    ('stage7_costs', '/pilot-review/costs', '原価・使用量の記録'),
    ('stage7_scenarios', '/pilot-review/scenarios', '採算を条件ごとに比較'),
)
# Even callback/action paths are probed with GET only. A 403 with this exact
# boundary code confirms refusal before dispatch; it does not execute a POST.
BLOCKED_PAGES = (
    ('legacy_billing', '/billing/checkout'),
    ('store_purchase', '/api/plans/store-intent'),
    ('store_verification', '/api/plans/store-verify'),
    ('stripe_sandbox', '/payments/sandbox'),
    ('stripe_callback', '/webhooks/stripe/physical-sandbox'),
    ('line_link', '/notifications/line/link'),
    ('line_callback', '/notifications/line/webhook'),
    ('notification_send', '/notifications/deliveries/staging-review-probe/send_test'),
    ('account_deletion', '/account/deletion'),
    ('staff_account_deletion', '/admin/account-deletions'),
)


class PageEvidence(HTMLParser):
    def __init__(self, body):
        super().__init__(convert_charrefs=True)
        self.headings, self.links, self.classes = [], set(), set()
        self.heading = None
        self.feed(body)

    def handle_starttag(self, tag, attributes):
        values = dict(attributes)
        self.classes.update(values.get('class', '').split())
        if tag == 'h1':
            self.heading = []
        if tag == 'a':
            self.links.add(values.get('href', ''))

    def handle_data(self, data):
        if self.heading is not None:
            self.heading.append(data)

    def handle_endtag(self, tag):
        if tag == 'h1' and self.heading is not None:
            self.headings.append(' '.join(''.join(self.heading).split()))
            self.heading = None


def verify_review(environ=None, *, session_factory=None):
    """Only fixed staging accounts/routes; injectable transport is for tests."""
    environ = os.environ if environ is None else environ
    checks, clients = {}, []
    stage = 'environment_validated'

    def require(name, condition):
        checks[name] = bool(condition)
        if not condition:
            raise VerificationStopped(name)

    def page(client, path, heading):
        response = client.request('GET', path, expected={200})
        body = response.content.decode('utf-8', errors='strict')
        parsed = PageEvidence(body)
        if b'data-kaika-staging="true"' not in response.content or heading not in parsed.headings:
            raise VerificationStopped('expected_review_page_required')
        return parsed

    try:
        config = validate_environment(environ)
        require(stage, config.host == DEPLOYED_HOST and environ.get('KAIKA_STAGING_REVIEW_ENABLED') == '1')
        origin = 'https://' + DEPLOYED_HOST
        for role in ('normal', 'business', 'admin'):
            stage = role + '_anonymous_staging_confirmed'
            client = GuardedSession(origin, 'Mozilla/5.0 KaikaReadOnlyStagingReview/1.0', session_factory)
            clients.append(client)
            client.confirm(DEPLOYED_HOST)
            require(stage, True)
            stage = role + '_anonymous_review_denied'
            response = client.request('GET', '/workspace-home', expected={302, 303, 401, 403})
            require(stage, not response.headers.get('Location') or
                    urlsplit(same_origin_url(origin, response.headers['Location'])).path == '/login')
            stage = role + '_login_authenticated'
            client.request('POST', '/login', expected={302, 303}, data={
                'username': 'staging_' + role,
                'password': environ['STAGING_' + role.upper() + '_PASSWORD'],
            })
            page(client, '/workspace-home', '今日の業務' if role == 'admin' else '商品とお金の状況')
            require(stage, True)
            if role != 'admin':
                for key, title in HUBS:
                    stage = role + '_hub_' + key
                    parsed = page(client, '/my/' + key, title)
                    require(stage, 'customer-hub' in parsed.classes and
                            all('/my/' + value in parsed.links for value, _ in HUBS) and
                            '/workspace-home' in parsed.links)
                for key, path, heading in (
                    ('document_list', '/documents/list', '書類一覧'),
                    ('estimate_form', '/mitsumori/add', '見積り依頼書作成'),
                    ('consent_form', '/kaitori-shoudaku/add', '買取承諾書作成'),
                ):
                    stage = role + '_' + key
                    page(client, path, heading)
                    require(stage, True)
                stage = role + '_document_candidates'
                response = client.request('GET', '/api/document-products', expected={200})
                require(stage, isinstance(response.json(), list))
            for key, path, heading in COMMON_PAGES:
                stage = role + '_' + key
                page(client, path, heading)
                require(stage, True)
            stage = role + '_stage6_inbox'
            page(client, '/notifications', '通知の配信状況' if role == 'admin' else 'お知らせ')
            require(stage, True)
            stage = role + '_stage6_settings'
            if role == 'admin':
                client.request('GET', '/notifications/settings', expected={403})
            else:
                page(client, '/notifications/settings', '通知設定')
            require(stage, True)
            if role == 'admin':
                stage = 'admin_customer_hub_denied'
                client.request('GET', '/my/products', expected={403})
                require(stage, True)
                for key, path, heading in STAFF_PAGES:
                    stage = 'admin_' + key
                    page(client, path, heading)
                    require(stage, True)
                stage = 'admin_customer_document_inbox'
                page(client, '/admin/customer-submissions', 'お客様から届いた書類')
                require(stage, True)
                for key, path in BLOCKED_PAGES:
                    stage = 'admin_' + key + '_disabled'
                    response = client.request('GET', path, expected={403})
                    body = response.json()
                    require(stage, isinstance(body, dict) and isinstance(body.get('error'), dict)
                            and body['error'].get('code') == 'staging_disabled')
            else:
                stage = role + '_staff_only_work_denied'
                for path in ('/inventory/reconcile', '/commerce/channels', '/pilot-review'):
                    client.request('GET', path, expected={403})
                require(stage, True)
    except Exception:
        # Neither HTTP exception text nor response pages/URLs/identifiers leave
        # this process. The current named check is enough to locate the failure.
        checks[stage] = False
    finally:
        for client in clients:
            try:
                client.close()
            except Exception:
                checks['session_cleanup'] = False
    return {'checks': checks, 'all_checks_passed': bool(checks) and all(checks.values())}


def main(argv=None):
    argparse.ArgumentParser(description=__doc__).parse_args(argv)
    report = verify_review()
    print('DEPLOYED_STAGING_REVIEW_JSON=' + json.dumps(report, ensure_ascii=True, separators=(',', ':')))
    return 0 if report['all_checks_passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
