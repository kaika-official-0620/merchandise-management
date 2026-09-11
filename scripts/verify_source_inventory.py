"""Inventory application source and public assets without importing the website.

Databases, uploaded business files, credentials, and historical preview copies
are deliberately outside the application-source inventory. This is a source
and format check, not a substitute for the HTTP/workflow and device checks.
"""
from __future__ import annotations

import argparse
import ast
from collections import Counter
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET


SOURCE = Path(__file__).resolve().parents[1]
OMIT = {'uploads', 'backups', '__pycache__', 'node_modules'}
CONFIG_FILES = {'requirements.txt', 'render.yaml'}


def selected(name: str) -> bool:
    path = Path(name)
    if any(part.startswith('.') or part in OMIT for part in path.parts):
        return False
    return name in CONFIG_FILES or (len(path.parts) == 1 and path.suffix == '.py') or path.parts[0] in {'templates', 'static', 'scripts'}


def git(*args: str, binary: bool = False):
    result = subprocess.run(['git', '-C', str(SOURCE), *args], check=True, capture_output=True)
    return result.stdout if binary else result.stdout.decode('utf-8').strip()


def check_file(path: Path, node: str) -> tuple[str, str | None]:
    suffix = path.suffix.lower()
    try:
        if suffix == '.py':
            ast.parse(path.read_text(encoding='utf-8-sig'), filename=str(path))
            return 'python_syntax', None
        if suffix == '.js':
            result = subprocess.run([node, '--check', str(path)], capture_output=True, text=True, encoding='utf-8', errors='replace')
            return 'javascript_syntax', None if result.returncode == 0 else result.stderr[-1800:]
        if suffix == '.json':
            json.loads(path.read_text(encoding='utf-8-sig'))
            return 'json_syntax', None
        if suffix in {'.png', '.jpg', '.jpeg', '.webp', '.gif', '.ico'}:
            from PIL import Image
            with Image.open(path) as picture:
                picture.verify()
            return 'image_decode', None
        if suffix == '.svg':
            ET.fromstring(path.read_bytes())
            return 'svg_xml', None
        if suffix == '.html':
            # Actual application-environment template compilation is performed
            # by verify_site_parity.py, including the site's custom filters.
            return 'template_inventory_http_suite', None
        return 'inventory_only', None
    except Exception as error:
        return 'format_check', f'{type(error).__name__}: {error}'


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=SOURCE / 'docs' / 'source-inventory.json')
    parser.add_argument('--node', default='node')
    args = parser.parse_args()
    baseline = git('rev-parse', 'HEAD')
    tracked = {name for name in git('ls-tree', '-rz', '--name-only', 'HEAD', binary=True).decode('utf-8').split('\0') if name and selected(name)}
    current = {}
    for directory in [SOURCE, SOURCE / 'templates', SOURCE / 'static', SOURCE / 'scripts']:
        paths = directory.iterdir() if directory == SOURCE else directory.rglob('*')
        for path in paths:
            if path.is_file() and not path.is_symlink() and selected(path.relative_to(SOURCE).as_posix()):
                resolved = path.resolve()
                if SOURCE.resolve() not in resolved.parents:
                    raise RuntimeError('Source inventory refuses a path outside the application root.')
                current[path.relative_to(SOURCE).as_posix()] = path
    records = []
    for name in sorted(tracked | current.keys()):
        original = git('show', f'HEAD:{name}', binary=True) if name in tracked else None
        path = current.get(name)
        data = path.read_bytes() if path else None
        original_hash = hashlib.sha256(original).hexdigest() if original is not None else None
        current_hash = hashlib.sha256(data).hexdigest() if data is not None else None
        # Checkout line endings do not represent a change in site behavior.
        same_text = original is not None and data is not None and original.replace(b'\r\n', b'\n') == data.replace(b'\r\n', b'\n')
        status = 'deleted' if path is None else 'added' if original is None else 'unchanged' if same_text else 'modified'
        kind, error = check_file(path, args.node) if path else ('missing_source', 'Baseline file is missing')
        records.append({'path': name, 'status': status, 'bytes': len(data) if data is not None else None,
                        'sha256': current_hash, 'baseline_sha256': original_hash, 'check': kind, 'error': error})
    report = {'baseline_commit': baseline, 'scope': 'Root Python modules, scripts, templates, public static assets, requirements.txt, and render.yaml',
              'exclusions': 'Existing databases, uploaded business data, credentials, backups, historical preview copies, and native dependencies',
              'note': 'Syntax/format/inventory only. HTTP, writes, external services and native-device behavior require separate checks.',
              'status_counts': dict(Counter(record['status'] for record in records)),
              'check_counts': dict(Counter(record['check'] for record in records)),
              'failures': sum(record['error'] is not None for record in records), 'files': records}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({key: value for key, value in report.items() if key != 'files'}, ensure_ascii=False))
    for record in records:
        if record['error']:
            print(json.dumps({'path': record['path'], 'error': record['error']}, ensure_ascii=False))
    return 1 if report['failures'] else 0


if __name__ == '__main__':
    sys.exit(main())
