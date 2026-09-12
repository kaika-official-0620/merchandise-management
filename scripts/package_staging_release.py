"""Copy only application source into a NEW staging checkout, never user data.

The resulting directory intentionally has no Git history. Publish it only to a
dedicated staging branch; do not merge this source-only tree into production.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil

SOURCE = Path(__file__).resolve().parents[1]
PUBLIC_EXTENSIONS = {'.css', '.js', '.json', '.png', '.jpg', '.jpeg', '.svg', '.webp', '.ico', '.woff', '.woff2', '.ttf'}
TEXT_EXTENSIONS = {'.py', '.txt', '.yaml', '.html', '.sh', '.ps1', '.cjs', '.css', '.js', '.json', '.svg', '.md'}


def source_files(source):
    candidates = list(source.glob('*.py')) + [source / 'requirements.txt', source / 'render.staging.yaml']
    candidates.extend(source.joinpath('templates').rglob('*.html'))
    candidates.extend(p for p in source.joinpath('scripts').iterdir() if p.suffix in {'.sh', '.py', '.ps1', '.cjs'})
    candidates.extend(source.joinpath('tests').glob('test_*.py'))
    for path in source.joinpath('static').rglob('*'):
        relative = path.relative_to(source)
        if any(part.startswith('.') or part.lower() in {'uploads', 'backups', '__pycache__'} for part in relative.parts):
            continue
        if path.is_file() and path.suffix.lower() in PUBLIC_EXTENSIONS:
            candidates.append(path)
    for name in ('staging-environment.md', 'platform-acceptance.md', 'recovery-runbook.md',
                 'deployed-staging-verification.md', 'tesseract-render-install.md'):
        path = source / 'docs' / name
        if path.is_file():
            candidates.append(path)
    for path in sorted(set(candidates)):
        if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(source.resolve()):
            raise ValueError('Source candidate must be an ordinary file within the application source.')
        yield path


def package(source, destination):
    source, destination = source.resolve(), destination.resolve()
    if destination == source or destination.is_relative_to(source) or source.is_relative_to(destination):
        raise ValueError('Output must be a separate, new directory outside the source checkout.')
    if destination.exists():
        raise ValueError('Output already exists. Choose a new directory; nothing is overwritten or deleted.')
    if not (source / 'render.staging.yaml').is_file():
        raise ValueError('The dedicated staging Blueprint is required.')
    paths = list(source_files(source))
    destination.mkdir(parents=True)
    records = []
    for path in paths:
        relative = path.relative_to(source)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        source_bytes = path.read_bytes()
        deployed_bytes = source_bytes.replace(b'\r\n', b'\n') if path.suffix.lower() in TEXT_EXTENSIONS else source_bytes
        target.write_bytes(deployed_bytes)
        records.append({'path': relative.as_posix(), 'sha256': hashlib.sha256(deployed_bytes).hexdigest(),
            'source_sha256': hashlib.sha256(source_bytes).hexdigest()})
    # Render's default Blueprint in this separate branch must be staging-only.
    shutil.copyfile(destination / 'render.staging.yaml', destination / 'render.yaml')
    (destination / '.gitignore').write_text(
        '.env\n.env.*\n*.db\n*.sqlite*\n*.log\n__pycache__/\n*.py[cod]\n'
        'static/uploads/\nbackups/\n*.age\n.render/\nnode_modules/\n', encoding='utf-8', newline='\n')
    # Keep the reviewed bytes and manifest stable across Windows and Linux Git checkouts.
    (destination / '.gitattributes').write_text('* text=auto eol=lf\n', encoding='utf-8', newline='\n')
    (destination / 'README.md').write_text(
        '# 開花 検証用サーバー\n\n'
        'PCとスマホアプリで共通の業務を確認するための、ソースだけを収録した検証用ブランチです。\n'
        '既存DB・商品写真・秘密設定・Gitの過去履歴は含みません。検証用の架空データだけを使います。\n\n'
        '`render.yaml` は検証専用です。本番ブランチへのマージや本番サービスの接続先変更には使わないでください。\n'
        '元の開発コピーの履歴はそのまま保持しています。本番へは別途確認した変更のみを反映します。\n\n'
        '- [検証環境の準備](docs/staging-environment.md)\n'
        '- [業務の確認表](docs/platform-acceptance.md)\n'
        '- [保存・復旧手順](docs/recovery-runbook.md)\n', encoding='utf-8', newline='\n')
    manifest = {'created_at': datetime.now(timezone.utc).isoformat(), 'source_files': records,
        'included': len(records), 'data_policy': 'source only; no database, uploaded business file, credential or Git history',
        'line_endings': 'Text files normalized to LF; source_sha256 records the original working-copy bytes.',
        'generated_files': ['render.yaml (copy of render.staging.yaml)', '.gitignore', '.gitattributes', 'README.md']}
    (destination / 'staging-source-manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8', newline='\n')
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = package(SOURCE, args.output)
    print(json.dumps({'output': str(args.output.resolve()), 'source_files': result['included'], 'data_policy': result['data_policy']}))


if __name__ == '__main__':
    main()
