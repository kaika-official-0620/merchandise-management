"""Offline browser fixture for the actual notification proof templates.

Copies public source into OS temp before importing Flask. All outbound sockets,
subprocesses, original data reads and writes outside that temp tree are denied.
Only fictional credentials/proofs are shown on the loopback-only fixture guide.
Run with --check for local HTTP checks, or without arguments to serve port 18812.
"""
import base64
import hashlib
import hmac
import importlib
import io
import json
import logging
import os
from pathlib import Path
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from urllib.parse import parse_qs, urlsplit

import run_app_preview as sandbox
from verify_stage5 import Forms

PORT = 18812
ORIGIN = f'http://127.0.0.1:{PORT}'


def install_fixture(module, users):
    from flask import jsonify, redirect, render_template_string, session
    from flask_login import current_user, logout_user
    from stage6_external_transports import TestDeliveryPolicy, LineTestTransport, SmtpTestTransport

    app = module.app
    app.config.update(PROPAGATE_EXCEPTIONS=True, STAGE6_ENABLED=True,
        STAGE6_LINE_LINK_ENABLED=True, STAGE6_CONFIRMATION_ORIGIN='https://notification-preview.invalid',
        STAGE6_LINE_CHANNEL_SECRET='fictional-browser-channel-secret',
        STAGE6_LINE_STAFF_USER_ID=users['admin']['id'],
        STAGE6_TEST_MONTHLY_LIMITS={'email':20, 'line':20, 'push':20})
    customer = users['normal']['id']
    email = 'browser-proof@example.invalid'
    line_user = 'U' + 'c' * 32
    mails, lines, checks = [], [], []

    def policy(channel, address):
        return TestDeliveryPolicy(True, 'test', ({'channel':channel, 'customer_id':customer,
            'recipient_sha256':hashlib.sha256(address.encode()).hexdigest(),
            'approval_reference':'fictional-browser-fixture-only'},))

    def capture_line(plan, token):
        lines.append(plan)
        if plan['url'].endswith('/linkToken'):
            return 200, {}, {'linkToken':'fixture-browser-link-token-123456789'}
        return 200, {'X-Line-Request-Id':'fixture-browser-request'}, {}

    class CapturedSMTP:
        def __init__(self, **kwargs): pass
        def ehlo(self): pass
        def starttls(self, **kwargs): pass
        def send_message(self, message, **kwargs):
            mails.append(message)
            return {}
        def quit(self): pass

    app.extensions['stage6_test_transports'] = {
        'line':LineTestTransport(policy('line', line_user), access_token='fictional-token', http=capture_line),
        'email':SmtpTestTransport(policy('email', email), host='smtp.example.invalid', port=587,
            sender='fixture@example.invalid', smtp_factory=CapturedSMTP)}
    clients = {key:app.test_client() for key in ('normal', 'business', 'admin', 'anonymous')}

    def check(name, condition):
        checks.append({'name':name, 'pass':bool(condition)})
        if not condition:
            raise AssertionError(name)

    def http(role, path, data=None, **kwargs):
        response = clients[role].open(path, method='GET' if data is None else 'POST',
            data=data, base_url=ORIGIN, headers=kwargs.pop('headers', {'User-Agent':'Mozilla/5.0 OfflineBrowserFixture'}), **kwargs)
        response.get_data()
        return response

    def fields(role, page, action):
        response = http(role, page)
        check('actual form ' + action, response.status_code == 200)
        values = Forms(response).fields(action)
        check('CSRF ' + action, bool(values.get('registry_csrf')))
        return values

    def query(sql, params=()):
        db = module.get_db()
        try:
            return [dict(row) for row in db.execute(sql, params)]
        finally:
            db.close()

    guide = '''<!doctype html><html lang="ja"><meta charset="utf-8"><title>開花 通知ブラウザ検証</title>
    <style>body{font:17px sans-serif;max-width:900px;margin:32px auto;line-height:1.7}td,th{border:1px solid #aaa;padding:8px}a{margin-right:12px}code{background:#eee}</style>
    <h1>開花・通知ブラウザ検証（架空データ）</h1><p>外部通信を遮断しています。メールとLINEはローカル捕捉のみ。以下はこの一時環境だけの架空ログイン情報です。</p>
    <table><tr><th>使い方</th><th>ユーザー名</th><th>パスワード</th></tr>
    {% for role,label in [('normal','確認リンクの持ち主'),('business','別のお客様（拒否を確認）')] %}<tr><td>{{label}}</td><td>{{users[role].username}}</td><td>{{users[role].password}}</td></tr>{% endfor %}</table>
    <p>現在：{{identity}}。<a href="/__notification_fixture/logout">ログアウトしてガイドへ</a><a href="/login?next=/__notification_fixture">通常のログイン</a><a href="/__notification_fixture/result">サーバ側の結果</a></p>
    <p>ログアウトはブラウザの一時proofを消しません。独立した試験を始める時だけ下のリセットを使います。</p>
    <button onclick="sessionStorage.removeItem('kaika-email-proof-test');sessionStorage.removeItem('kaika-line-link-test');document.getElementById('reset-result').textContent='ブラウザのproofを消しました'">ブラウザのproofだけリセット</button><span id="reset-result"></span>
    <h2>メール</h2><ul><li><a href="{{valid}}">有効なメール確認リンク</a>匿名→ログイン→確認ボタン→通知設定</li>
    <li><a href="/notifications/email/confirm#token=bad">明示的に不正なメールリンク</a>古いproofが残っている状態でも誤って有効にならないこと</li>
    <li><a href="{{expired}}">期限切れメール確認リンク</a>実サーバの確認処理が拒否すること</li>
    <li><a href="/notifications/email/confirm">proofなしで直接開く</a>リセット後は確認ボタンが無効であること</li></ul>
    <h2>LINE</h2><ul><li><a href="{{line}}">有効なLINE連携リンク</a>匿名→ログイン→フォーム有効まで。ボタンは押さないでください。</li>
    <li><a href="/notifications/line/link#invitation=bad&amp;linkToken=bad">明示的に不正なLINEリンク</a></li>
    <li><a href="/notifications/line/link">LINE proofなしで直接開く</a></li></ul>
    <p>メールの正常確認はproofを一度だけ消費します。再実行する場合はこのfixtureプロセスを再起動してください。LINE外部転送はfixture側でも遮断しています。</p></html>'''

    @app.get('/__notification_fixture')
    def fixture_guide():
        identity = current_user.username if current_user.is_authenticated else '未ログイン'
        return render_template_string(guide, users=users, identity=identity, valid=proof_links['valid'], expired=proof_links['expired'], line=line_link)

    @app.get('/__notification_fixture/logout')
    def fixture_logout():
        logout_user()
        session.clear()
        return redirect('/__notification_fixture')

    def result():
        states = {key:query('SELECT consumed_at,expires_at FROM stage6_destination_proofs WHERE proof_id=?', (value,))[0] for key,value in proof_ids.items()}
        destinations = query('SELECT customer_id,channel FROM stage6_destinations WHERE revoked_at IS NULL')
        return {'fictional_only':True, 'outbound_network_blocked':True,
            'valid_email_consumed':states['valid']['consumed_at'] is not None,
            'expired_email_consumed':states['expired']['consumed_at'] is not None,
            'expired_email_is_expired':states['expired']['expires_at'] == 0,
            'owner_email_verified':any(row['customer_id'] == customer and row['channel'] == 'email' for row in destinations),
            'other_customer_email_verified':any(row['customer_id'] == users['business']['id'] and row['channel'] == 'email' for row in destinations),
            'line_verified':any(row['channel'] == 'line' for row in destinations),
            'captured_smtp_messages':len(mails), 'captured_line_requests':len(lines),
            'real_provider_calls':0, 'setup_checks_passed':len(checks),
            'actual_browser_checks':'performed separately by browser operator'}

    @app.get('/__notification_fixture/result')
    def fixture_result():
        return jsonify(result())

    @app.after_request
    def fixture_browser_boundary(response):
        # Browser resources must be local too; the real inline templates remain unchanged.
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; connect-src 'self'; form-action 'self'; object-src 'none'; base-uri 'self'"
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Cache-Control'] = 'no-store'
        location = response.headers.get('Location', '')
        if urlsplit(location).netloc and urlsplit(location).netloc != f'127.0.0.1:{PORT}':
            return app.make_response(('Fixture: external redirect blocked. Return to /__notification_fixture.', 409))
        return response

    for role in ('normal', 'business', 'admin'):
        response = http(role, '/login', {'username':users[role]['username'], 'password':users[role]['password']})
        check('actual login ' + role, response.status_code == 302 and '/login' not in (response.location or ''))

    proof_links = {}
    proof_ids = {}
    for case in ('expired', 'valid'):
        action = '/notifications/email'
        response = http('normal', action, {**fields('normal', '/notifications/settings', action), 'email':email})
        check('email queued ' + case, response.status_code == 302)
        row = query("SELECT delivery_id,proof_id FROM stage6_notification_outbox WHERE channel='email' AND state='queued'")[-1]
        action = '/notifications/deliveries/' + row['delivery_id'] + '/send_test'
        response = http('admin', action, fields('admin', '/notifications', action))
        check('captured email ' + case, response.status_code == 302 and len(mails) == len(proof_links) + 1)
        proof_links[case] = next(line.strip() for line in mails[-1].get_content().splitlines() if '/notifications/email/confirm#' in line)
        proof_ids[case] = row['proof_id']

    # Deliberate fictional expiry; preserve the original generated proof and JS.
    db = module.get_db()
    try:
        db.execute('UPDATE stage6_destination_proofs SET expires_at=0,consumed_at=NULL WHERE proof_id=?', (proof_ids['expired'],))
        db.commit()
    finally:
        db.close()
    token = parse_qs(urlsplit(proof_links['valid']).fragment)['token'][0]
    confirm = '/notifications/email/confirm'
    response = http('business', confirm, {**fields('business', confirm, confirm), 'token':token})
    check('other customer cannot consume valid proof', response.status_code == 409)
    response = http('normal', confirm, {**fields('normal', confirm, confirm), 'token':'bad'})
    check('invalid proof rejected', response.status_code == 409)
    expired = parse_qs(urlsplit(proof_links['expired']).fragment)['token'][0]
    response = http('normal', confirm, {**fields('normal', confirm, confirm), 'token':expired})
    check('expired proof rejected', response.status_code == 409)
    check('valid proof remains unconsumed', query('SELECT consumed_at FROM stage6_destination_proofs WHERE proof_id=?', (proof_ids['valid'],))[0]['consumed_at'] is None)

    raw = json.dumps({'events':[{'type':'follow', 'webhookEventId':'fixture-browser-follow', 'source':{'userId':line_user}}]}, separators=(',', ':')).encode()
    signature = base64.b64encode(hmac.new(app.config['STAGE6_LINE_CHANNEL_SECRET'].encode(), raw, hashlib.sha256).digest()).decode()
    response = http('anonymous', '/notifications/line/webhook', raw, content_type='application/json', headers={'x-line-signature':signature})
    check('captured LINE invite', response.status_code == 200 and len(lines) == 2)
    line_link = lines[-1]['json']['messages'][0]['text'].splitlines()[-1]
    check('generated links contain fragment only', all(urlsplit(link).netloc == 'notification-preview.invalid' and not urlsplit(link).query for link in (*proof_links.values(), line_link)))
    # Only this fixture guide changes the origin; generated bearer fragments stay exact.
    proof_links = {key:ORIGIN + urlsplit(link).path + '#' + urlsplit(link).fragment for key,link in proof_links.items()}
    line_link = ORIGIN + urlsplit(line_link).path + '#' + urlsplit(line_link).fragment
    for path, marker in ((confirm, 'kaika-email-proof-test'), ('/notifications/line/link', 'kaika-line-link-test')):
        response = http('anonymous', path)
        check('actual anonymous template ' + path, response.status_code == 200 and marker in response.get_data(as_text=True))
    check('real charges and scheduler disabled', not module.STRIPE_ENABLED and not module.SCHEDULER_ENABLED)


    check('guide route loads', http('anonymous', '/__notification_fixture').status_code == 200)
    report = result()
    report['checks'] = checks
    report['not_verified'] = ['browser JavaScript execution (root agent checks separately)', 'real email delivery',
        'real LINE channel/accountLink callback', 'push delivery', 'native devices', 'production settings or charges']
    return report


def main():
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    preview = Path(tempfile.mkdtemp(prefix='kaika-notification-browser-')).resolve()
    runtime = sandbox.mirror_source(preview, None, None)
    sandbox.isolate_environment(preview, PORT)
    connections = sandbox.install_runtime_boundary(preview, PORT)
    sys.path[:] = [str(runtime)] + [path for path in sys.path if path and not sandbox.inside(Path(path).resolve(), sandbox.SOURCE)]
    os.chdir(runtime)
    diagnostics = io.StringIO()
    try:
        with redirect_stdout(diagnostics), redirect_stderr(diagnostics):
            loaded = importlib.import_module('render_app')
            users = sandbox.seed_preview(loaded.module, preview)
            for number in range(2, 7):
                migration = importlib.import_module(f'stage{number}_migration')
                getattr(migration, f'prepare_stage{number}_fixture')(loaded.module, allowed_test_path=preview/'preview.sqlite3')
            loaded.module.app.config.update(STAGE2_INVENTORY_ENABLED=True, STAGE3_FINANCE_ENABLED=True,
                STAGE4_COMMERCE_ENABLED=True, STAGE5_PAYMENTS_ENABLED=True)
            report = install_fixture(loaded.module, users)
        report_path = preview/'browser-fixture-report.json'
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print('NOTIFICATION_BROWSER_FIXTURE=' + json.dumps({'url':ORIGIN+'/__notification_fixture',
            'report_path':str(report_path), 'setup_checks_passed':report['setup_checks_passed'],
            'pid':os.getpid(), 'real_provider_calls':0}), flush=True)
        if '--check' not in sys.argv:
            logging.getLogger('werkzeug').setLevel(logging.ERROR)
            loaded.module.app.run(host='127.0.0.1', port=PORT, debug=False, use_reloader=False, threaded=True)
        return 0
    except Exception as error:
        print('NOTIFICATION_BROWSER_FIXTURE_ERROR=' + json.dumps({'type':type(error).__name__, 'message':str(error)}, ensure_ascii=False), flush=True)
        return 1
    finally:
        for connection in connections:
            connection.close()


if __name__ == '__main__':
    raise SystemExit(main())
