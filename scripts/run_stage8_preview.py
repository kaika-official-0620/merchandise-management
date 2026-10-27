"""Loopback-only Stage2-7 review with fictional data and no external traffic.

Reuses run_app_preview's copied-source, database, filesystem and network guards.
Never import the original application. No credentials are printed or inherited.
This is not a release server, and performs no production migrations.
"""
import argparse
from contextlib import redirect_stdout, redirect_stderr
import importlib
import io
import json
import logging
import os
from pathlib import Path
import socket
import sys
import tempfile

import run_app_preview as sandbox

FLAGS = ('STAGE2_INVENTORY_ENABLED', 'STAGE3_FINANCE_ENABLED',
         'STAGE4_COMMERCE_ENABLED', 'STAGE5_PAYMENTS_ENABLED',
         'STAGE6_ENABLED', 'STAGE7_ENABLED')

SHELL = '''<!doctype html><html lang="ja"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>開花・公開前の確認</title>
<style>body{font:16px/1.8 system-ui,sans-serif;max-width:840px;margin:40px auto;padding:20px;color:#25344a;background:#f7f8fb}h1{font-size:28px}section{background:white;border:1px solid #dbe2eb;border-radius:14px;padding:20px;margin:20px 0}a{color:#314c92}nav{display:flex;gap:18px;flex-wrap:wrap}.notice{padding:14px;background:#fff2cd;border-radius:8px}</style>
<h1>開花・公開前の確認</h1><p class="notice">このPC内だけの確認用です。すべて架空データで、決済・外部通知は送信しません。再起動すると初期状態に戻ります。実際のお客様の情報は入力しないでください。</p>
<section><h2>1. 確認する立場を選ぶ</h2><nav><a href="/__preview/start/normal">お客様Aで確認</a><a href="/__preview/start/business">お客様Bで確認</a><a href="/__preview/start/admin">開花スタッフで確認</a></nav><p>同じブラウザのタブはログイン状態を共有します。立場を切り替えると、ほかのタブにも反映されます。</p></section>
<section><h2>2. 操作する画面を開く</h2><nav><a href="/workspace-home">ホーム</a><a href="/inventory/items">個品管理</a><a href="/inventory/self/new">自己登録</a><a href="/inventory/intakes">開花への依頼</a><a href="/commerce">商品販売・承認</a><a href="/finance">料金・利益</a><a href="/payments">現物の支払検証</a><a href="/notifications">通知履歴</a><a href="/notifications/settings">通知設定</a><a href="/pilot-review">実証と採算（スタッフ）</a></nav><p>商品の所有者以外や、権限のない立場では開けない画面があります。</p></section>
<section><h2>確認の順番</h2><ol><li>お客様で自己登録し、ホームと個品一覧に同じ商品が載るか確認。</li><li>開花への依頼と、スタッフ側での受領・作業記録を確認。</li><li>商品販売、両者の承認、発送、料金・利益の内訳を確認。</li><li>スタッフで実証の観察記録と採算の条件比較を確認。</li></ol><p>料金案・契約・販路の許可が未確認の場合は処理を保留します。空欄や未確認は0円・完了を意味しません。実ストア購入、通知の到着、実機のカメラ等はこの確認とは別です。</p></section></html>'''


def prepare(module, preview, users):
    for stage in range(2, 8):
        migration = importlib.import_module('stage%d_migration' % stage)
        getattr(migration, 'prepare_stage%d_fixture' % stage)(module, allowed_test_path=preview / 'preview.sqlite3')
    module.app.config.update({name: True for name in FLAGS})
    module.app.config['STAGE2_FREE_LIMIT_PROPOSAL_ENABLED'] = True
    from item_registry import Actor, Registry
    conn = module.get_db()
    try:
        conn.execute('PRAGMA foreign_keys=ON')
        conn.execute('BEGIN IMMEDIATE')
        registry = Registry(conn)
        # These six rows were just generated as self-held fictional stock.
        # Never infer this custody value for existing customer inventory.
        conn.execute("UPDATE merchandise SET custody_location='self' WHERE scope='user'")
        for row in registry.query("SELECT id,user_id,product_name FROM merchandise WHERE scope='user' ORDER BY id"):
            actor = Actor(row['user_id'])
            item = registry.create_item(actor, row['product_name'], custodian='self',
                occurred_at='2026-10-23T09:00:00+09:00', evidence_reference='fictional-stage8-preview',
                source_key='stage8-preview-item-' + str(row['id']))
            registry.attach_legacy(actor, row['id'], physical_item_id=item['physical_item_id'],
                occurred_at='2026-10-23T09:00:00+09:00', evidence_reference='fictional-stage8-preview',
                source_key='stage8-preview-link-' + str(row['id']))
        staff = Actor(users['admin']['id'], True)
        item = registry.create_item(staff, '【架空】開花で受領したバッグ', owner_user_id=users['normal']['id'], custodian='unknown',
            occurred_at='2026-10-23T09:00:00+09:00', evidence_reference='fictional-stage8-preview', source_key='stage8-preview-receipt')
        registry.record_event(staff, item['physical_item_id'], 'received', '2026-10-23T10:00:00+09:00',
            'stage8-preview-received', 'fictional-stage8-preview')
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def check_routes(module, users, port):
    base = 'http://127.0.0.1:' + str(port)
    checks = []
    for persona in users:
        client = module.app.test_client()
        response = client.get('/__preview/start/' + persona, base_url=base, follow_redirects=True)
        checks.append(dict(persona=persona, path='/__preview/start', status=response.status_code, passed=response.status_code == 200))
        paths = ['/workspace-home', '/inventory/items', '/commerce', '/finance', '/payments', '/notifications', '/notifications/settings']
        if persona == 'admin':
            paths += ['/pilot-review', '/pilot-review/costs', '/pilot-review/scenarios']
        for path in paths:
            response = client.get(path, base_url=base)
            expected = 403 if persona == 'admin' and path == '/notifications/settings' else 200
            if path=='/notifications/settings' and module.app.config.get('ACCOUNT_LIFECYCLE_ENABLED') is True:
                if not module.app.extensions['account_lifecycle'].outbound_allowed(users[persona]['id']):
                    expected=403  # Deletion requests also stop new destination verification.
            checks.append(dict(persona=persona, path=path, status=response.status_code, expected=expected,
                               passed=response.status_code == expected))
        for path in ['/billing/checkout', '/stripe/webhook']:
            response = client.post(path, base_url=base)
            checks.append(dict(persona=persona, path=path, status=response.status_code, passed=response.status_code == 403))
    return checks


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=18808)
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--account-review', action='store_true', help='Include fictional deletion requests and disconnected Stripe sandbox screens')
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error('Choose a non-privileged port.')
    if not args.check:
        with socket.socket() as probe:
            probe.settimeout(1)
            if probe.connect_ex(('127.0.0.1', args.port)) == 0:
                parser.error('Port already in use; select a different --port.')
    if any(key in sys.modules for key in ('app', 'render_app')):
        raise RuntimeError('A fresh Python process is required.')
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    original = Path.cwd()
    preview = Path(tempfile.mkdtemp(prefix='kaika-stage8-preview-')).resolve()
    runtime = sandbox.mirror_source(preview, None, None)
    (preview / 'shell.html').write_text(SHELL, encoding='utf-8')
    sandbox.isolate_environment(preview, args.port)
    connections = sandbox.install_runtime_boundary(preview, args.port)
    sys.path[:] = [str(runtime)] + [p for p in sys.path if p and not sandbox.inside(Path(p).resolve(), sandbox.SOURCE)]
    os.chdir(runtime)
    diagnostics = io.StringIO()
    try:
        with redirect_stdout(diagnostics), redirect_stderr(diagnostics):
            loaded = importlib.import_module('render_app')
            if loaded.RUNTIME_SOURCE != 'source' or not sandbox.inside(Path(loaded.module.__file__).resolve(), preview):
                raise RuntimeError('Only a copied source runtime is allowed.')
            users = sandbox.seed_preview(loaded.module, preview)
            prepare(loaded.module, preview, users)
            if args.account_review:
                from account_lifecycle import migration_statements
                from account_erasure import migration_statements as erasure_statements
                from stage9_payment_schema import prepare_stage9_payments_fixture
                from stage9_stripe import SandboxScope
                with loaded.module.get_db() as connection:
                    for statement in migration_statements()+erasure_statements():
                        connection.execute(statement)
                    from item_registry import Actor, Registry
                    from account_lifecycle import LifecycleService
                    from account_erasure import ErasureService
                    review_store=Registry(connection)
                    deletion=LifecycleService(review_store).request(Actor(users['normal']['id']),source_key='fictional-preview-deletion')
                    ErasureService(review_store).prepare(Actor(users['admin']['id']),users['normal']['id'],deletion['request_id'])
                fixture_customers=tuple(users[name]['id'] for name in ('normal','business'))
                sandbox_id='local-account-review-20261025'
                prepare_stage9_payments_fixture(loaded.module,allowed_test_path=preview/'preview.sqlite3',
                    sandbox_id=sandbox_id,customer_ids=fixture_customers)
                loaded.app.config.update(ACCOUNT_LIFECYCLE_ENABLED=True,STAGE9_PAYMENTS_ENABLED=True)
                loaded.app.extensions['stage9_payment_scope']=SandboxScope(sandbox_id,
                    tuple((uid,'cus_fixture'+str(uid)) for uid in fixture_customers),'https://offline-preview.invalid')
                shell=(preview/'shell.html').read_text(encoding='utf-8').replace('</nav><p>商品の所有者',
                    '<a href="/account-deletion-info">アカウント削除の案内</a><a href="/account/deletion">削除申請</a><a href="/admin/account-deletions">申請確認（スタッフ）</a><a href="/payments/sandbox">Stripe接続準備</a></nav><p>商品の所有者')
                (preview/'shell.html').write_text(shell,encoding='utf-8')
            sandbox.install_preview_routes(loaded.module, preview, users, args.port)
            from flask import redirect
            start = loaded.app.view_functions['preview_start']
            def start_at_home(persona):
                response = start(persona)
                return redirect('/workspace-home') if response.status_code == 302 else response
            loaded.app.view_functions['preview_start'] = start_at_home
            checks = check_routes(loaded.module, users, args.port)
        failed = [row for row in checks if not row['passed']]
        report = dict(scope='fictional local Stage2-7 preview; no outgoing connections', checks=checks, failed=failed,
                      passed=len(checks)-len(failed), production_modified=False, real_payments=False)
        (preview / 'verification.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(dict(preview_url='http://127.0.0.1:%d/__preview/shell' % args.port,
                             report=str(preview / 'verification.json'), passed=report['passed'], failed=failed), ensure_ascii=False), flush=True)
        if failed:
            return 1
        if args.check:
            return 0
        logging.getLogger('werkzeug').setLevel(logging.ERROR)
        loaded.app.run(host='127.0.0.1', port=args.port, debug=False, use_reloader=False, threaded=True)
    except Exception as error:
        # The isolated fixture contains no user credentials. Do not echo arbitrary runtime logs.
        print(json.dumps(dict(error_type=type(error).__name__, error=str(error)), ensure_ascii=False), flush=True)
        return 1
    finally:
        for conn in connections:
            conn.close()
        os.chdir(original)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
