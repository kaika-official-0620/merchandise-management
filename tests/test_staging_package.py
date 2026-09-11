"""A deployment snapshot must exclude databases, uploads and credentials."""
import importlib.util
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('staging_package', Path(__file__).resolve().parents[1] / 'scripts/package_staging_release.py')
package = importlib.util.module_from_spec(spec)
spec.loader.exec_module(package)


class StagingPackageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='kaika-package-test-')
        self.root = Path(self.temporary.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        for folder in ('scripts', 'tests', 'templates', 'static/css', 'static/uploads', 'static/backups', 'docs'):
            (self.source / folder).mkdir(parents=True, exist_ok=True)
        for name, content in {
            'app.py': 'print("fictional source")\n', 'requirements.txt': 'Flask==2.3.3\n',
            'render.staging.yaml': 'services: [] # staging\n', 'render.yaml': 'production must not be copied',
            'templates/index.html': '<p>test</p>', 'static/css/style.css': 'p {color:black}',
            'scripts/start_staging.sh': 'echo fixture', 'tests/test_example.py': 'pass',
            'merchandise.db': 'DO NOT COPY DB', '.env': 'DO NOT COPY SECRET',
            'static/uploads/customer.jpg': 'DO NOT COPY CUSTOMER PHOTO',
            'static/backups/backup.json': 'DO NOT COPY BACKUP',
        }.items():
            (self.source / name).write_text(content, encoding='utf-8')

    def tearDown(self):
        self.temporary.cleanup()

    def test_only_source_and_staging_blueprint_are_packaged(self):
        target = self.root / 'release'
        report = package.package(self.source, target)
        self.assertEqual(report['included'], 7)
        self.assertEqual((target / 'render.yaml').read_text(), 'services: [] # staging\n')
        self.assertEqual((target / 'static/css/style.css').read_text(), 'p {color:black}')
        for name in ('merchandise.db', '.env', 'static/uploads/customer.jpg', 'static/backups/backup.json', '.git'):
            self.assertFalse((target / name).exists(), name)
        manifest = json.loads((target / 'staging-source-manifest.json').read_text())
        self.assertTrue(all(len(record['sha256']) == 64 for record in manifest['source_files']))

    def test_existing_output_is_never_overwritten(self):
        target = self.root / 'already'
        target.mkdir()
        (target / 'keep.txt').write_text('retain')
        with self.assertRaises(ValueError):
            package.package(self.source, target)
        self.assertEqual((target / 'keep.txt').read_text(), 'retain')

    def test_text_is_portable_and_binary_bytes_are_preserved(self):
        original = b'#!/bin/bash\r\necho fixture\r\n'
        binary = b'\x89PNG\r\n\x1a\n\x00'
        (self.source / 'scripts/start_staging.sh').write_bytes(original)
        (self.source / 'static/logo.png').write_bytes(binary)
        target = self.root / 'release'
        report = package.package(self.source, target)
        self.assertEqual((target / 'scripts/start_staging.sh').read_bytes(), original.replace(b'\r\n', b'\n'))
        self.assertEqual((target / 'static/logo.png').read_bytes(), binary)
        records = {record['path']: record for record in report['source_files']}
        self.assertEqual(records['scripts/start_staging.sh']['source_sha256'], hashlib.sha256(original).hexdigest())
        for record in records.values():
            self.assertEqual(record['sha256'], hashlib.sha256((target / record['path']).read_bytes()).hexdigest())
        self.assertEqual((target / '.gitattributes').read_bytes(), b'* text=auto eol=lf\n')

    def test_source_and_parent_targets_are_refused(self):
        for target in (self.source, self.source / 'release', self.root):
            with self.subTest(target=target), self.assertRaises(ValueError):
                package.package(self.source, target)

    def test_missing_staging_configuration_cannot_use_production(self):
        (self.source / 'render.staging.yaml').unlink()
        with self.assertRaises(ValueError):
            package.package(self.source, self.root / 'release')
        self.assertFalse((self.root / 'release').exists())


if __name__ == '__main__':
    unittest.main()
