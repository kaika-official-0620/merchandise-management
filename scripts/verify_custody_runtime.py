"""Verify the effective proxy handler and custody boundary in a fresh sandbox."""
import importlib
import inspect
import json
import os
from pathlib import Path
import sys
import tempfile
import run_app_preview as sandbox


def run(module, users):
    app = module.app
    app.config['PROPAGATE_EXCEPTIONS'] = True
    base = 'http://127.0.0.1:18789'
    client = app.test_client()
    client.get('/__preview/start/admin', base_url=base)
    actual = inspect.unwrap(app.view_functions['admin_proxy_service_bulk_toggle'])
    checks = [{'name': 'effective handler is safe_bulk_toggle', 'pass': actual.__name__ == 'safe_bulk_toggle'}]
    with module.get_db() as conn:
        cursor = conn.execute("INSERT INTO proxy_service_settings (auction_name,is_public,sale_mode,start_datetime,end_datetime) VALUES ('架空保管検証',0,'auction','2026-01-01 00:00:00','2099-12-31 23:59:59')")
        auction_id = cursor.lastrowid
        item_id = conn.execute('SELECT id FROM merchandise WHERE user_id=? ORDER BY id LIMIT 1', (users['admin']['id'],)).fetchone()[0]
    path = f'/admin/proxy-service/{auction_id}/bulk-toggle'
    for custody in ('self', 'transit', 'kaika'):
        for encoding in ('json-items', 'raw-json-items', 'json-ids', 'form-ids'):
            with module.get_db() as conn:
                conn.execute('UPDATE merchandise SET custody_location=?,show_in_proxy_service=0,auction_id=NULL WHERE id=?', (custody, item_id))
            payload = {'action': 'add', 'items': [{'id': item_id}]} if encoding.endswith('items') else {'action': 'add', 'item_ids': [str(item_id)]}
            kwargs = ({'data': json.dumps(payload), 'content_type': 'text/plain'} if encoding == 'raw-json-items' else
                      {'data': payload} if encoding == 'form-ids' else {'json': payload})
            response = client.post(path, base_url=base, **kwargs)
            with module.get_db() as conn:
                after = dict(conn.execute('SELECT custody_location,show_in_proxy_service,auction_id FROM merchandise WHERE id=?', (item_id,)).fetchone())
            expected = 200 if custody == 'kaika' else 409
            changed = after['show_in_proxy_service'] == 1 and after['auction_id'] == auction_id
            checks.append({'name': custody + ' ' + encoding, 'status': response.status_code,
                           'pass': response.status_code == expected and changed == (custody == 'kaika') and after['custody_location'] == custody})
            response.close()
    result = {'scope': 'copied real render_app; disposable SQLite; network disabled', 'handler': actual.__qualname__,
              'file': Path(inspect.getsourcefile(actual)).name, 'checks': checks,
              'passed': sum(c['pass'] for c in checks), 'failed': sum(not c['pass'] for c in checks)}
    return result


def main():
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    original = Path.cwd()
    with tempfile.TemporaryDirectory(prefix='kaika-custody-runtime-') as temporary:
        preview = Path(temporary).resolve()
        runtime = sandbox.mirror_source(preview, None, None)
        sandbox.isolate_environment(preview, 18789)
        connections = sandbox.install_runtime_boundary(preview, 18789)
        sys.path[:] = [str(runtime)] + [p for p in sys.path if p and not sandbox.inside(Path(p).resolve(), sandbox.SOURCE)]
        os.chdir(runtime)
        try:
            loaded = importlib.import_module('render_app')
            users = sandbox.seed_preview(loaded.module, preview)
            sandbox.install_preview_routes(loaded.module, preview, users, 18789)
            result = run(loaded.module, users)
            print('CUSTODY_RUNTIME_JSON=' + json.dumps(result, ensure_ascii=False))
        finally:
            for connection in connections:
                connection.close()
            os.chdir(original)
    return 1 if result['failed'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
