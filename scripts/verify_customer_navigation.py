"""Check the copied real Flask customer menu and its GET destinations offline.

Uses only fictional accounts and a disposable SQLite DB. Source credentials,
existing databases/uploads, outbound network and production writes are blocked.
"""
from contextlib import redirect_stderr, redirect_stdout
from html.parser import HTMLParser
import importlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import traceback
from urllib.parse import urlsplit

import run_app_preview as sandbox
from run_stage8_preview import prepare


class Links(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.menu, self.cards = [], []
        self.feed(html)

    def handle_starttag(self, tag, attributes):
        attributes = dict(attributes)
        classes = attributes.get('class', '').split()
        if tag == 'a' and 'customer-menu-link' in classes:
            self.menu.append(attributes)
        if tag == 'a' and 'customer-action-card' in classes:
            self.cards.append(attributes)


def verify(module, users, port):
    app = module.app
    checks = []
    requests = []
    base = 'http://127.0.0.1:' + str(port)

    def check(name, passed):
        checks.append(dict(name=name, passed=bool(passed)))

    def get(client, persona, path, follow=False):
        response = client.get(path, base_url=base, follow_redirects=follow)
        requests.append(dict(persona=persona, path=path, status=response.status_code))
        return response

    for persona in ('normal', 'business'):
        client = app.test_client()
        get(client, persona, '/__preview/start/' + persona)
        destinations = set()
        for section in ('products', 'requests', 'money', 'documents', 'support'):
            path = '/my/' + section
            response = get(client, persona, path)
            check(persona + ' renders ' + path, response.status_code == 200)
            parsed = Links(response.get_data(as_text=True))
            check(persona + ' six menu entries ' + section, len(parsed.menu) == 6)
            active = [link['href'] for link in parsed.menu if link.get('aria-current') == 'page']
            check(persona + ' one current menu ' + section, active == [path])
            check(persona + ' available cards ' + section, bool(parsed.cards))
            check(persona + ' no private page caching ' + section,
                  'no-store' in {part.strip() for part in response.headers.get('Cache-Control', '').split(',')})
            for link in parsed.cards:
                target = urlsplit(link['href'])
                check(persona + ' internal card ' + target.path, not target.netloc and target.path.startswith('/'))
                destinations.add(link['href'])
        owner = users[persona]['id']
        other = users['business' if persona == 'normal' else 'normal']['id']
        check(persona + ' monthly finance links only own customer',
              '/finance/customers/' + str(owner) in destinations and
              '/finance/customers/' + str(other) not in destinations)
        for path in sorted(destinations):
            response = get(client, persona, path, follow=True)
            check(persona + ' destination opens ' + path, response.status_code == 200)
        check(persona + ' other customer finance remains inaccessible',
              get(client, persona, '/finance/customers/' + str(other)).status_code in (403, 404))
    client = app.test_client()
    check('anonymous cannot open customer hub', get(client, 'anonymous', '/my/products').status_code == 302)
    get(client, 'admin', '/__preview/start/admin')
    check('staff cannot open customer hub', get(client, 'admin', '/my/products').status_code == 403)
    staff = get(client, 'admin', '/workspace-home')
    check('staff home remains accessible', staff.status_code == 200)
    check('staff keeps existing menu', not Links(staff.get_data(as_text=True)).menu)
    check('real charges and scheduler remain disabled', not module.STRIPE_ENABLED and not module.SCHEDULER_ENABLED)
    return dict(scope='copied real Flask; synthetic customers; offline GET destinations', checks=checks,
                requests=requests, passed=sum(row['passed'] for row in checks),
                failed=sum(not row['passed'] for row in checks),
                server_errors=sum(row['status'] >= 500 for row in requests),
                production_modified=False, real_payments=False)


def main():
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    original = Path.cwd()
    preview = Path(tempfile.mkdtemp(prefix='kaika-navigation-verification-')).resolve()
    runtime = sandbox.mirror_source(preview, None, None)
    port = 18816
    sandbox.isolate_environment(preview, port)
    connections = sandbox.install_runtime_boundary(preview, port)
    sys.path[:] = [str(runtime)] + [path for path in sys.path if path and not sandbox.inside(Path(path).resolve(), sandbox.SOURCE)]
    os.chdir(runtime)
    diagnostics = io.StringIO()
    try:
        with redirect_stdout(diagnostics), redirect_stderr(diagnostics):
            loaded = importlib.import_module('render_app')
            if loaded.RUNTIME_SOURCE != 'source' or not sandbox.inside(Path(loaded.module.__file__).resolve(), preview):
                raise RuntimeError('Only a copied source runtime is allowed.')
            users = sandbox.seed_preview(loaded.module, preview)
            prepare(loaded.module, preview, users)
            from account_lifecycle import migration_statements
            from account_erasure import migration_statements as erasure_schema
            with loaded.module.get_db() as connection:
                for statement in migration_statements() + erasure_schema():
                    connection.execute(statement)
            loaded.app.config['ACCOUNT_LIFECYCLE_ENABLED'] = True
            sandbox.install_preview_routes(loaded.module, preview, users, port)
            report = verify(loaded.module, users, port)
    except Exception as error:
        report = dict(passed=0, failed=1, server_errors=1, error_type=type(error).__name__,
                      error=str(error), traceback=traceback.format_exc())
    finally:
        for connection in connections:
            connection.close()
        os.chdir(original)
    if report['failed'] or report['server_errors']:
        report['isolated_runtime_diagnostics'] = diagnostics.getvalue()[-14000:]
    report_path = preview / 'verification.json'
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    summary = {key: report.get(key) for key in ('passed', 'failed', 'server_errors', 'error_type', 'error')}
    summary.update(report_path=str(report_path), requests=len(report.get('requests', [])),
                   failed_checks=[row['name'] for row in report.get('checks', []) if not row['passed']])
    print(json.dumps(summary, ensure_ascii=False))
    return 1 if report['failed'] or report['server_errors'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
