"""Only disposable DB/uploads; no production application import or backup reads."""
import ast
from copy import deepcopy
from datetime import datetime
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
import zipfile

from flask import Flask, flash, redirect, request, url_for
from werkzeug.datastructures import FileStorage

from backup_restore_guard import legacy_restore_block_reason, unsupported_payload

ROOT = Path(__file__).resolve().parents[1]
TREE = ast.parse((ROOT / 'app.py').read_text(encoding='utf-8-sig'))


def existing_function(name, globals):
    node = deepcopy(next(node for node in TREE.body if isinstance(node, ast.FunctionDef) and node.name == name))
    node.decorator_list = []
    namespace = dict(globals)
    exec(compile(ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[])), '<existing-backup-handler>', 'exec'), namespace)
    return namespace[name]


def archive(payload):
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w') as zipped:
        zipped.writestr('backup_data.json', json.dumps(payload))
        zipped.writestr('images/existing-photo.jpg', b'must-not-overwrite')
        zipped.writestr('images/new-photo.jpg', b'must-not-create')
    output.seek(0)
    return output


class BackupRestoreGuardTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='kaika-restore-guard-fixture-')
        self.addCleanup(self.temp.cleanup)
        self.connections = []
        self.addCleanup(lambda: [conn.close() for conn in self.connections])
        self.root = Path(self.temp.name); self.path = self.root / 'fixture.sqlite3'
        self.uploads = self.root / 'uploads'; self.uploads.mkdir()
        (self.uploads / 'existing-photo.jpg').write_bytes(b'original-fictional-photo')
        with self.db() as conn:
            conn.execute('CREATE TABLE users(id INTEGER PRIMARY KEY, username TEXT)')
            conn.execute("INSERT INTO users VALUES (1, 'fixture-owner')")
            conn.execute('CREATE TABLE merchandise(id INTEGER PRIMARY KEY, user_id INTEGER, custody_location TEXT, custody_received_at TEXT, product_name TEXT)')
            conn.execute("INSERT INTO merchandise VALUES (1, 1, 'kaika', NULL, 'existing-fictional-item')")
            conn.execute('CREATE TABLE customers(id INTEGER PRIMARY KEY, user_id INTEGER, name TEXT, email TEXT, phone TEXT, address TEXT, total_purchase INTEGER, purchase_count INTEGER, notes TEXT, created_at TEXT)')
            conn.execute('CREATE TABLE inventory_intakes(id INTEGER PRIMARY KEY, user_id INTEGER)')
            conn.execute('CREATE TABLE proxy_service_users(user_id INTEGER)')
        self.app = Flask('restore-guard-fixture')
        self.app.config.update(TESTING=True, SECRET_KEY='fixture', UPLOAD_FOLDER=str(self.uploads))
        environment = dict(get_db=self.db, DATABASE_URL=None, app=self.app, request=request, flash=flash,
            redirect=redirect, url_for=url_for, current_user=SimpleNamespace(id=1, username='fixture-owner'),
            legacy_restore_block_reason=legacy_restore_block_reason, zipfile=zipfile, json=json, os=os)
        self.app.add_url_rule('/admin/backup', endpoint='admin_backup', view_func=lambda: 'backup')
        self.app.add_url_rule('/', endpoint='index', view_func=lambda: 'inventory')
        for name in ('import_backup', 'import_user_backup'):
            self.app.add_url_rule('/' + name, endpoint=name, methods=['POST'], view_func=existing_function(name, environment))
        self.client = self.app.test_client()

    def db(self):
        conn = sqlite3.connect(self.path); conn.row_factory = sqlite3.Row
        self.connections.append(conn)
        return conn

    def post(self, route, payload, zipped=False, mode='merge'):
        body = archive(payload) if zipped else io.BytesIO(json.dumps(payload).encode())
        return self.client.post('/' + route, data={'backup_file': (body, 'fixture.zip' if zipped else 'fixture.json'), 'import_mode': mode}, content_type='multipart/form-data')

    def assert_unchanged(self):
        with self.db() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM merchandise').fetchone()[0], 1)
            self.assertEqual(conn.execute('SELECT product_name FROM merchandise WHERE id=1').fetchone()[0], 'existing-fictional-item')
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM users').fetchone()[0], 1)
        self.assertEqual((self.uploads / 'existing-photo.jpg').read_bytes(), b'original-fictional-photo')
        self.assertFalse((self.uploads / 'new-photo.jpg').exists())
        with self.client.session_transaction() as session:
            self.assertIn('実行していません', ' '.join(message for category, message in session.get('_flashes', [])))

    def test_self_and_transit_payloads_are_rejected_before_json_or_zip_writes(self):
        for route in ('import_backup', 'import_user_backup'):
            for custody in ('self', 'transit'):
                for zipped in (False, True):
                    for mode in ('merge', 'replace'):
                        with self.subTest(route=route, custody=custody, zipped=zipped, mode=mode):
                            response = self.post(route, {'merchandise': [{'custody_location': custody}]}, zipped, mode)
                            self.assertEqual(response.status_code, 302); self.assert_unchanged()

    def test_existing_new_inventory_blocks_old_payload_in_both_importers(self):
        payload = {'merchandise': [], 'customers': []}
        for custody in ('self', 'transit'):
            with self.db() as conn:
                conn.execute('UPDATE merchandise SET custody_location=? WHERE id=1', (custody,))
            for route in ('import_backup', 'import_user_backup'):
                for zipped in (False, True):
                    response = self.post(route, payload, zipped, 'replace')
                    self.assertEqual(response.status_code, 302); self.assert_unchanged()

    def test_intake_history_and_account_bindings_cannot_be_lost_by_legacy_restore(self):
        payload = {'merchandise': [], 'customers': []}
        for table in ('inventory_intakes', 'feature_subscriptions', 'push_devices'):
            with self.db() as conn:
                if table != 'inventory_intakes':
                    conn.execute('CREATE TABLE ' + table + ' (id INTEGER, user_id INTEGER)')
                conn.execute('INSERT INTO ' + table + ' VALUES (1,1)')
            for route in ('import_backup', 'import_user_backup'):
                self.assertEqual(self.post(route, payload, True, 'replace').status_code, 302)
                self.assert_unchanged()
            with self.db() as conn:
                conn.execute('DELETE FROM ' + table)

    def test_foreign_user_history_does_not_block_own_legacy_preflight(self):
        with self.db() as conn:
            conn.execute('INSERT INTO inventory_intakes VALUES (2,2)')
        incoming = FileStorage(stream=io.BytesIO(b'{"merchandise": []}'), filename='legacy.json')
        self.assertIsNone(legacy_restore_block_reason(self.db, False, incoming, 1))
        self.assertIsNotNone(legacy_restore_block_reason(self.db, False, incoming, None))

    def test_received_dates_or_related_payloads_are_rejected_and_stream_position_is_restored(self):
        for payload in ({'merchandise': [{'custody_location': 'kaika', 'custody_received_at': '2026-09-11'}]},
                        {'merchandise': [], 'inventory_intakes': [{'id': 1}]},
                        {'merchandise': [], 'feature_store_accounts': [{'user_id': 1}]},
                        {'merchandise': [], 'push_devices': [{'user_id': 1}]}):
            self.assertTrue(unsupported_payload(payload))
            incoming = FileStorage(stream=archive(payload), filename='fixture.zip')
            self.assertIsNotNone(legacy_restore_block_reason(self.db, False, incoming))
            self.assertEqual(incoming.stream.tell(), 0)

    def test_legacy_imports_remain_allowed_and_database_read_failure_fails_closed(self):
        for count, route in enumerate(('import_backup', 'import_user_backup'), 1):
            with self.client.session_transaction() as session:
                session.clear()
            response = self.post(route, {'merchandise': [], 'customers': [{'user_id': 1, 'name': 'fictional-customer'}]})
            self.assertEqual(response.status_code, 302)
            with self.client.session_transaction() as session:
                self.assertIn('インポート完了', ' '.join(message for category, message in session.get('_flashes', [])))
            with self.db() as conn:
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM customers').fetchone()[0], count)
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM merchandise').fetchone()[0], 1)
        incoming = FileStorage(stream=io.BytesIO(b'{"merchandise": []}'), filename='legacy.json')
        self.assertIsNotNone(legacy_restore_block_reason(lambda: (_ for _ in ()).throw(RuntimeError('fixture error')), False, incoming))
        self.assertEqual(incoming.stream.tell(), 0)

    def test_existing_export_keeps_custody_columns_in_saved_json(self):
        with self.db() as conn:
            conn.execute("UPDATE merchandise SET custody_location='self' WHERE id=1")
        exporter = existing_function('build_admin_backup_data', {
            'get_db': self.db, 'DATABASE_URL': None, 'ADMIN_BACKUP_TABLES': ['users', 'merchandise'],
            'get_jst_now': lambda: datetime(2026, 9, 11), 'convert_backup_dates': lambda value: value})
        payload = exporter()
        self.assertEqual(payload['merchandise'][0]['custody_location'], 'self')
        self.assertIn('custody_received_at', payload['merchandise'][0])
        self.assertTrue(unsupported_payload(payload))
        with zipfile.ZipFile(archive(payload)) as zipped:
            self.assertEqual(json.load(zipped.open('backup_data.json'))['merchandise'][0]['custody_location'], 'self')


if __name__ == '__main__':
    unittest.main()
