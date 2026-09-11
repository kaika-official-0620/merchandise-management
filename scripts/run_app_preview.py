"""Run the real website in a disposable, loopback-only development sandbox.

Only source, templates and public static assets are copied. Existing databases,
uploads, environment files and bytecode are never copied or opened. All runtime
writes stay in the temporary workspace; outgoing networking, payment SDKs,
schedulers and subprocesses are disabled before importing render_app.

    python scripts/run_app_preview.py --port 8769 --shell /path/to/pc-preview.html
    python scripts/run_app_preview.py --check

Ctrl+C stops the server and removes its disposable data. Never deploy this script.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import importlib
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import sqlite3
import sys
import tempfile
import time
from urllib.parse import urlsplit


SOURCE = Path(__file__).resolve().parents[1]
PERSONAS = {'normal': 'ノーマル・確認用', 'business': 'ビジネス・確認用', 'admin': '管理者・Web確認用'}


def inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def copy_public_tree(source: Path, target: Path, allowed_root: Path | None = None) -> None:
    """Copy public files, including hydrated OneDrive files, within one root.

    FILE_ATTRIBUTE_REPARSE_POINT also describes ordinary cloud files; treating
    every such file as a link silently drops CSS, scripts and images on Windows.
    Check actual link types and resolved containment instead of that broad flag.
    """
    allowed_root = allowed_root or source.resolve()
    if not inside(source.resolve(), allowed_root):
        raise RuntimeError('Public assets must resolve inside their source directory.')
    target.mkdir(parents=True, exist_ok=True)
    with os.scandir(source) as entries:
        for entry in entries:
            if entry.name.startswith('.') or entry.name.lower() in {'uploads', 'backups', '__pycache__'}:
                continue
            item = Path(entry.path)
            attributes = entry.stat(follow_symlinks=False)
            # Mount points/junctions and symlinks are actual path redirection.
            # Other reparse tags (e.g. OneDrive cloud placeholders) are allowed.
            if entry.is_symlink() or getattr(attributes, 'st_reparse_tag', 0) in {0xA0000003, 0xA000000C}:
                continue
            if not inside(item.resolve(), allowed_root):
                continue
            if entry.is_dir(follow_symlinks=False):
                copy_public_tree(item, target / entry.name, allowed_root)
            elif item.suffix.lower() in {'.html', '.css', '.js', '.json', '.png', '.jpg', '.jpeg', '.svg', '.webp', '.ico', '.woff', '.woff2', '.ttf'}:
                shutil.copyfile(item, target / entry.name)


def mirror_source(preview: Path, shell: Path | None, guide: Path | None) -> Path:
    runtime = preview / 'site'
    runtime.mkdir()
    for source in SOURCE.glob('*.py'):
        if source.is_symlink():
            raise RuntimeError('Preview does not follow source symlinks.')
        shutil.copyfile(source, runtime / source.name)
    for directory in ('templates', 'static'):
        public = SOURCE / directory
        if not inside(public.resolve(), SOURCE):
            raise RuntimeError('Public asset directories cannot redirect outside the source workspace.')
        copy_public_tree(public, runtime / directory)
    if shell:
        if shell.suffix.lower() != '.html' or not shell.is_file():
            raise RuntimeError('--shell must identify the development preview HTML file.')
        shutil.copyfile(shell, preview / 'shell.html')
    if guide:
        if guide.suffix.lower() != '.md' or not guide.is_file():
            raise RuntimeError('--guide must identify the development preview Markdown guide.')
        shutil.copyfile(guide, preview / 'guide.md')
    return runtime


def isolate_environment(preview: Path, port: int) -> None:
    # Keep only OS/runtime variables required to run Python. No inherited cloud,
    # database, mail, OAuth, payment, proxy or application credentials survive.
    keep = {'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'PATH', 'PATHEXT', 'TEMP', 'TMP',
            'LOCALAPPDATA', 'APPDATA', 'USERPROFILE', 'HOMEDRIVE', 'HOMEPATH',
            'LANG', 'LC_ALL', 'PYTHONPATH', 'PYTHONHOME', 'PYTHONUTF8'}
    for key in list(os.environ):
        if key.upper() not in keep:
            del os.environ[key]
    os.environ.update({
        'MERCHANDISE_DB_PATH': str(preview / 'preview.sqlite3'),
        'SECRET_KEY': secrets.token_urlsafe(48),
        'FEATURE_PLANS_ENABLED': '1', 'MOBILE_API_ENABLED': '0',
        'GOOGLE_DRIVE_ENABLED': '0', 'AUTO_BACKUP_ENABLED': '0',
        'BACKUP_STORAGE_DIR': str(preview / 'backups'),
        'PRIMARY_DOMAIN_REDIRECT': '0', 'PRIMARY_DOMAIN': f'127.0.0.1:{port}',
        'PRIMARY_SCHEME': 'http', 'FLASK_DEBUG': '0',
        'TZ': 'Asia/Tokyo', 'FLASK_SKIP_DOTENV': '1',
    })
    # The existing scheduler's enable switch is import availability, not an env
    # flag. Disable the import before the application can initialize any jobs.
    for prefix in ('stripe', 'apscheduler', 'psycopg2', 'dotenv'):
        for name in list(sys.modules):
            if name == prefix or name.startswith(prefix + '.'):
                del sys.modules[name]
        sys.modules[prefix] = None
    sys.dont_write_bytecode = True


def install_runtime_boundary(preview: Path, port: int) -> list:
    counters = {'network_denied': 0, 'database_denied': 0, 'write_denied': 0}
    connections = []
    original_connect = sqlite3.connect

    def tracked_connect(*args, **kwargs):
        # A few legacy views leave handles open. Track every disposable handle
        # so Ctrl+C can close it before Windows removes the temporary directory.
        kwargs.setdefault('check_same_thread', False)
        connection = original_connect(*args, **kwargs)
        connections.append(connection)
        return connection

    sqlite3.connect = tracked_connect

    def resolved(value) -> Path | None:
        if not isinstance(value, (str, bytes, os.PathLike)):
            return None
        return Path(os.fsdecode(value)).resolve()

    def check_write(value):
        path = resolved(value)
        if path and not inside(path, preview):
            counters['write_denied'] += 1
            raise PermissionError('Preview writes must stay inside its temporary workspace.')

    def audit(event, args):
        if event in {'socket.connect', 'socket.sendto', 'socket.sendmsg'}:
            counters['network_denied'] += 1
            raise PermissionError('Outgoing connections are disabled in the preview.')
        if event in {'socket.getaddrinfo', 'socket.gethostbyname', 'socket.gethostbyaddr'}:
            host = str(args[0] or '')
            if host not in {'127.0.0.1', 'localhost', '::1', 'None', ''}:
                counters['network_denied'] += 1
                raise PermissionError('External DNS is disabled in the preview.')
        if event == 'socket.bind':
            address = args[1]
            if not isinstance(address, tuple) or address[0] != '127.0.0.1' or address[1] != port:
                raise PermissionError('Preview may listen only on its selected loopback port.')
        if event in {'subprocess.Popen', 'os.system', 'os.exec', 'os.spawn', 'os.posix_spawn'}:
            raise PermissionError('External processes are disabled in the preview.')
        if event == 'sqlite3.connect':
            path = resolved(args[0])
            if not path or not inside(path, preview):
                counters['database_denied'] += 1
                raise PermissionError('Preview can open only its disposable SQLite files.')
        if event == 'open':
            path, mode, flags = args
            target = resolved(path)
            if target and (target.name in {'.env', '.flaskenv'} or
                           (not inside(target, preview) and (target.suffix.lower() in {'.db', '.sqlite', '.sqlite3'} or
                            (inside(target, SOURCE) and 'uploads' in target.parts)))):
                raise PermissionError('Existing database, upload and environment files are excluded from preview.')
            if (isinstance(mode, str) and any(letter in mode for letter in 'wax+')) or (
                    isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)):
                check_write(path)
        if event in {'os.mkdir', 'os.remove', 'os.rmdir', 'os.truncate', 'os.chmod', 'os.utime'}:
            check_write(args[0])
        if event in {'os.rename', 'os.link', 'os.symlink'}:
            check_write(args[0])
            check_write(args[1])

    sys.addaudithook(audit)
    return connections


def placeholder_image(path: Path, index: int) -> None:
    from PIL import Image, ImageDraw
    colors = ['#b8997b', '#849eaa', '#ba95a2']
    canvas = Image.new('RGB', (480, 360), '#f4f1f8')
    draw = ImageDraw.Draw(canvas)
    draw.rounded_rectangle((110, 110, 370, 280), radius=24, fill=colors[index % 3])
    draw.arc((170, 48, 310, 192), 180, 360, fill='#615466', width=12)
    draw.text((190, 309), 'PREVIEW ONLY', fill='#6c6179')
    canvas.save(path, 'PNG')


def seed_preview(module, preview: Path) -> dict:
    from werkzeug.security import generate_password_hash
    app = module.app
    database = Path(module.DATABASE).resolve()
    if module.DATABASE_URL or database != preview / 'preview.sqlite3':
        raise RuntimeError('Preview refused a non-preview database configuration.')
    if module.SCHEDULER_ENABLED or module.STRIPE_ENABLED or getattr(module, 'scheduler', None):
        raise RuntimeError('A scheduler or payment SDK remained enabled.')
    module.ensure_user_profile_columns()
    users = {}
    with module.get_db() as conn:
        # init_db creates a default admin on fresh databases. Remove every such
        # fixture account before exposing the server; no default password remains.
        conn.execute('DELETE FROM users')
        for alias, display_name in PERSONAS.items():
            password = secrets.token_urlsafe(18)
            cursor = conn.execute('''INSERT INTO users
                (username,email,password_hash,role,display_name,subscription_status,
                 tuition_exempt,admin_permissions,address,phone,postal_code)
                VALUES (?,?,?,?,?,'active',1,'[]',?,?,?)''',
                ('preview_' + alias, f'{alias}@preview.invalid', generate_password_hash(password),
                 'owner' if alias == 'admin' else 'user', display_name,
                 '確認用の架空住所', '00000000000', '0000000'))
            users[alias] = {'id': cursor.lastrowid, 'username': 'preview_' + alias, 'password': password}
    service = app.extensions['kaika_feature_plans']
    with app.app_context(), service.db(write=True) as cur:
        for alias in ('normal', 'business'):
            service.execute(cur, 'INSERT INTO feature_manual_grants VALUES (?,?,?)',
                            (users[alias]['id'], alias, int(time.time()) + 7 * 86400))
    upload = Path(app.config['UPLOAD_FOLDER']).resolve()
    if not inside(upload, preview):
        raise RuntimeError('Preview refused an upload path outside its workspace.')
    images = upload / 'preview'
    images.mkdir(parents=True, exist_ok=True)
    names = ['トートバッグ', 'ハンドバッグ', 'レザーポーチ']
    for index in range(3):
        placeholder_image(images / f'sample-{index}.png', index)
    self_inventory = app.extensions['kaika_self_inventory']
    with self_inventory.connection(write=True) as (_conn, cur):
        for alias, account in users.items():
            for index, name in enumerate(names):
                now = module.get_jst_now().strftime('%Y-%m-%d')
                values = {
                    'user_id': account['id'], 'scope': 'admin' if alias == 'admin' else 'user',
                    'product_name': f'【確認用】{name}', 'brand_name': 'SAMPLE',
                    'photo_path': f'uploads/preview/sample-{index}.png', 'additional_photos': '[]',
                    'kaika_product_code': f'PREVIEW-{alias.upper()}-{index + 1:03}',
                    'purchase_date': now, 'storage_start_date': now,
                    'item_condition': 'B', 'store_name': '確認用仕入先',
                    'purchase_price': (index + 1) * 3000, 'wholesale_price': (index + 1) * 3000,
                    'listing_price': (index + 1) * 5500,
                    'notes': 'プレビュー専用の架空商品です。再起動すると元に戻ります。',
                    'is_listed': 0, 'is_shipped': 0,
                }
                cur.execute(f"INSERT INTO merchandise ({','.join(values)}) VALUES ({','.join('?' for _ in values)})", tuple(values.values()))
                if alias != 'admin':
                    cur.execute('INSERT INTO self_inventory_items (merchandise_id,user_id,submission_hash) VALUES (?,?,?)',
                                (cur.lastrowid, account['id'], hashlib.sha256(secrets.token_bytes(32)).hexdigest()))
    return users


def install_preview_routes(module, preview: Path, users: dict, port: int) -> None:
    from flask import abort, jsonify, redirect, request, send_file, session, url_for
    from flask_login import login_user
    from werkzeug.middleware.proxy_fix import ProxyFix
    app = module.app
    # A local preview must not trust forwarded headers from outside proxies.
    if isinstance(app.wsgi_app, ProxyFix):
        app.wsgi_app = app.wsgi_app.app
    app.config.update(SESSION_COOKIE_NAME='kaika_preview_' + secrets.token_hex(5),
                      SESSION_COOKIE_SECURE=False, REMEMBER_COOKIE_SECURE=False,
                      SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax',
                      MAX_CONTENT_LENGTH=40 * 1024 * 1024, TESTING=False)
    allowed_hosts = {f'127.0.0.1:{port}', f'localhost:{port}'}

    def boundary():
        if request.host not in allowed_hosts or request.remote_addr not in {'127.0.0.1', '::1'}:
            abort(403)
        if request.headers.get('Sec-Fetch-Site') == 'cross-site' and (
                request.path.startswith('/__preview/start/') or request.method not in {'GET', 'HEAD', 'OPTIONS'}):
            abort(403)
        persona = session.get('_preview_persona')
        if persona in {'normal', 'business'}:
            request.environ['HTTP_USER_AGENT'] = 'KaikaApp/0.2 PreviewOnly'
        elif persona == 'admin':
            request.environ['HTTP_USER_AGENT'] = 'KaikaPreview/1 WebAdmin'
        if request.path.startswith(('/billing/', '/stripe/', '/admin/stripe', '/api/stripe/',
                                    '/line/', '/admin/line', '/admin/backup', '/backup/import')):
            return 'プレビューでは決済・外部連携・バックアップ復元を実行できません。', 403

    app.before_request_funcs.setdefault(None, []).insert(0, boundary)

    @app.get('/__preview/start/<persona>')
    def preview_start(persona):
        if persona not in users:
            abort(404)
        account = users[persona]
        conn = module.get_db()
        try:
            row = conn.execute('SELECT * FROM users WHERE id=? AND username=?',
                               (account['id'], account['username'])).fetchone()
        finally:
            conn.close()
        if not row:
            abort(404)
        session.clear()
        session['_preview_persona'] = persona
        login_user(module.build_user_from_record(row), remember=False, fresh=True)
        endpoint = 'feature_plans.admin' if persona == 'admin' else 'index'
        return redirect(url_for(endpoint))

    @app.get('/__preview/shell')
    def preview_shell():
        path = preview / 'shell.html'
        if path.is_file():
            return send_file(path, mimetype='text/html')
        return '''<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1">
            <title>開花・確認環境</title><h1>開花・確認環境</h1><p>架空データのみ。決済・外部送信なし。</p>
            <p><a href="/__preview/start/normal">ノーマル</a> |
            <a href="/__preview/start/business">ビジネス</a> |
            <a href="/__preview/start/admin">管理者（Web）</a></p>'''

    @app.get('/__preview/health')
    def preview_health():
        return jsonify(preview=True, database='disposable-sqlite', outgoing_network=False,
                       payments=False, scheduler=False, personas=list(users))

    @app.get('/__preview/guide')
    def preview_guide():
        path = preview / 'guide.md'
        if not path.is_file():
            abort(404)
        escaped = html.escape(path.read_text(encoding='utf-8'))
        return ('<!doctype html><html lang="ja"><meta name="viewport" content="width=device-width,initial-scale=1">'
                '<title>確認・公開の手順</title><body style="max-width:920px;margin:32px auto;padding:16px;font-family:sans-serif">'
                '<p><a href="/__preview/shell">確認画面へ戻る</a></p>'
                '<pre style="white-space:pre-wrap;overflow-wrap:anywhere;font:15px/1.9 sans-serif">' + escaped + '</pre></body></html>')

    @app.after_request
    def preview_response(response):
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Kaika-Preview'] = 'disposable-local-only'
        response.headers['Content-Security-Policy'] = (
            "default-src 'self' data: blob:; script-src 'self' 'unsafe-inline' 'unsafe-eval'; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; font-src 'self' data:; "
            "connect-src 'self'; frame-src 'self' blob:; object-src 'none'; base-uri 'self'; "
            "form-action 'self'; frame-ancestors 'self'")
        response.headers['Referrer-Policy'] = 'no-referrer'
        location = response.headers.get('Location')
        if location and urlsplit(location).netloc and urlsplit(location).netloc not in allowed_hosts:
            return app.make_response(('外部のページへの移動はプレビューでは停止しています。', 403))
        if response.mimetype == 'text/html' and not response.direct_passthrough and request.path != '/__preview/shell':
            html = response.get_data(as_text=True)
            banner = '<div data-kaika-preview style="position:relative;z-index:10;padding:5px 10px;background:#fff4d6;color:#674d00;font:12px/1.5 sans-serif;text-align:center">確認用・架空データのみ／決済・外部送信なし／再起動でリセット</div>'
            guard = '''<script>document.addEventListener('click',function(e){var a=e.target.closest&&e.target.closest('a[href]');if(!a)return;try{var u=new URL(a.href,location.href);if(u.origin!==location.origin){e.preventDefault();e.stopImmediatePropagation();alert('外部のページへの移動はプレビューでは停止しています。');}}catch(_){}},true);</script>'''
            html = re.sub(r'(<body\b[^>]*>)', lambda match: match.group(1) + banner + guard, html, count=1, flags=re.I)
            response.set_data(html)
        return response


def verify_preview(module, users: dict, preview: Path, port: int) -> None:
    base = f'http://127.0.0.1:{port}'
    app = module.app
    asset_client = app.test_client()
    assets = {
        '/static/css/style.css': ({'text/css'}, b'{'),
        '/static/brand/kaika-header-logo-transparent.png': ({'image/png'}, b'\x89PNG\r\n\x1a\n'),
        '/static/js/sale_request_image_picker.js': ({'text/javascript', 'application/javascript'}, b'KaikaSaleRequestImagePicker'),
    }
    connection = module.get_db()
    try:
        # Build URLs from the stored seed values, just as the site templates do.
        # Checking only files on disk misses a broken photo_path prefix.
        photo_paths = [row[0] for row in connection.execute('SELECT DISTINCT photo_path FROM merchandise')]
    finally:
        connection.close()
    if len(photo_paths) != 3:
        raise RuntimeError('Expected three distinct generated preview photos.')
    with app.test_request_context(base_url=base):
        from flask import url_for
        for photo_path in photo_paths:
            assets[url_for('static', filename=photo_path)] = ({'image/png'}, b'\x89PNG\r\n\x1a\n')
    for asset_path, (mime_types, marker) in assets.items():
        response = asset_client.get(asset_path, base_url=base)
        data = response.get_data()
        if response.status_code != 200 or response.mimetype not in mime_types or len(data) < 64 or marker not in data:
            raise RuntimeError(f'Preview public asset failed: {asset_path}; status={response.status_code}; '
                               f'type={response.mimetype}; bytes={len(data)}; static_folder={app.static_folder}; '
                               f'diagnostic={data[:4000].decode("utf-8", errors="replace") if response.status_code >= 400 else "unexpected asset format"}')
    def forbidden_network():
        with socket.socket() as connection:
            connection.connect(('192.0.2.1', 443))

    for operation in (
        lambda: sqlite3.connect(str(preview.parent / 'preview-forbidden.sqlite3')),
        forbidden_network,
        lambda: sys.audit('subprocess.Popen', 'forbidden', [], None, None),
    ):
        try:
            operation()
        except PermissionError:
            pass
        else:
            raise RuntimeError('A preview isolation boundary did not reject its check.')
    for alias in users:
        client = app.test_client()
        response = client.get('/__preview/start/' + alias, base_url=base, follow_redirects=True)
        if response.status_code != 200:
            raise RuntimeError(f'Preview {alias} initial page failed: {response.status_code}: {response.get_data(as_text=True)[:600]}')
        paths = ['/plans', '/profile']
        if alias == 'admin':
            paths += ['/admin/feature-plans']
        else:
            with app.test_request_context(base_url=base):
                from flask import url_for
                paths += [url_for('self_inventory_new')]
        for path in paths:
            response = client.get(path, base_url=base)
            if response.status_code != 200:
                raise RuntimeError(f'Preview {alias} {path} failed: {response.status_code}: {response.get_data(as_text=True)[:600]}')
        if alias != 'admin':
            html = client.get('/plans', base_url=base).get_data(as_text=True)
            if 'action="/billing/' in html or '税込／月' in html:
                raise RuntimeError('Native preview unexpectedly displayed web payment controls.')
        if client.post('/billing/checkout', base_url=base).status_code != 403:
            raise RuntimeError('Preview did not block payment checkout.')
    print('PREVIEW_CHECK_OK: real render_app, CSS/logo/JS/seed photos, three personas, templates, native plans, outbound/DB/process guards', flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default='127.0.0.1', choices=['127.0.0.1'])
    parser.add_argument('--port', type=int, default=8769)
    parser.add_argument('--shell', type=Path)
    parser.add_argument('--guide', type=Path)
    parser.add_argument('--check', action='store_true', help='Run isolated smoke checks and exit without listening.')
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error('--port must be between 1024 and 65535.')
    if not args.check:
        # Windows can let two development servers reuse the same address.
        # Refuse a second launch so the browser cannot keep reaching old code.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(1)
            if probe.connect_ex(('127.0.0.1', args.port)) == 0:
                parser.exit(1, f'Port {args.port} is already in use. Stop the existing preview before restarting, or choose another --port.\n')
    if any(name in sys.modules for name in ('app', 'render_app')):
        raise RuntimeError('Launch the preview in a fresh Python process.')
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    shell = args.shell.resolve() if args.shell else None
    guide = args.guide.resolve() if args.guide else None
    original_cwd = Path.cwd()
    with tempfile.TemporaryDirectory(prefix='kaika-app-preview-') as temporary:
        preview = Path(temporary).resolve()
        runtime = mirror_source(preview, shell, guide)
        isolate_environment(preview, args.port)
        connections = install_runtime_boundary(preview, args.port)
        sys.path[:] = [str(runtime)] + [path for path in sys.path if path and not inside(Path(path).resolve(), SOURCE)]
        os.chdir(runtime)
        try:
            loaded = importlib.import_module('render_app')
            if loaded.RUNTIME_SOURCE != 'source' or not inside(Path(loaded.module.__file__).resolve(), preview):
                raise RuntimeError('Preview requires the copied source runtime.')
            users = seed_preview(loaded.module, preview)
            install_preview_routes(loaded.module, preview, users, args.port)
            verify_preview(loaded.module, users, preview, args.port)
            if args.check:
                return 0
            print(f'\nPREVIEW_URL=http://127.0.0.1:{args.port}/__preview/shell', flush=True)
            print('Temporary demo accounts only. Passwords are generated per launch; no existing account is used.', flush=True)
            for alias, account in users.items():
                print(f"  {alias}: username={account['username']} password={account['password']}", flush=True)
            print('Stop with Ctrl+C. Restart creates fresh demo data. Do not enter real customer data.', flush=True)
            loaded.app.run(host='127.0.0.1', port=args.port, debug=False, use_reloader=False, threaded=True)
        finally:
            for connection in connections:
                connection.close()
            os.chdir(original_cwd)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
