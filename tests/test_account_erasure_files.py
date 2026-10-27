import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from account_erasure_files import MARKER, MARKER_NAME, plan_file_cleanup, execute_fixture_cleanup
from item_registry import Registry, RegistryError


class FileCleanupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='account-erasure-files-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / MARKER_NAME).write_text(json.dumps(MARKER), encoding='utf-8')
        self.db_path = self.root/'account-fixture-files.sqlite3'
        self.db = sqlite3.connect(self.db_path)
        self.addCleanup(self.db.close)
        self.store = Registry(self.db)
        self.db.executescript('''
            CREATE TABLE merchandise(id INTEGER PRIMARY KEY,user_id INTEGER,photo_path TEXT,additional_photos TEXT,amount INTEGER);
            CREATE TABLE stage2_photo_records(merchandise_id INTEGER,owner_user_id INTEGER,photo_path TEXT,removed_from_item_at TEXT);
            CREATE TABLE business_item_links(merchandise_id INTEGER,physical_item_id TEXT,link_status TEXT);
            CREATE TABLE business_parties(party_id TEXT,kind TEXT,user_id INTEGER);
            CREATE TABLE business_item_assignments(physical_item_id TEXT,role TEXT,party_id TEXT,ended_at TEXT);
            INSERT INTO business_parties VALUES ('owner1','customer',1),('owner2','customer',2);
        ''')
        self.photo = self.root/'static/uploads/fixture.jpg'
        self.photo.parent.mkdir(parents=True)
        self.photo.write_bytes(b'Fictional photo content, no actual customer photograph')
        self.add_item(1, 1, 'uploads/fixture.jpg')

    def add_item(self, item, owner, reference):
        self.db.execute('INSERT INTO merchandise VALUES(?,?,?,NULL,30000)', (item, owner, reference))
        self.db.execute('INSERT INTO stage2_photo_records VALUES(?,?,?,NULL)', (item, owner, reference))
        self.db.execute("INSERT INTO business_item_links VALUES(?,?,'verified')", (item, 'item'+str(item)))
        self.db.execute("INSERT INTO business_item_assignments VALUES(?,'owner',?,NULL)", ('item'+str(item), 'owner'+str(owner)))
        self.db.commit()

    def plan(self, decisions=None):
        if decisions is None:
            decisions = {'uploads/fixture.jpg':{'action':'delete','synthetic_only':True,'reference':'fictional-policy-only'}}
        return plan_file_cleanup(self.store, 1, fixture_root=self.root, decisions=decisions)

    def execute(self, plan):
        return execute_fixture_cleanup(self.store, plan, fixture_root=self.root)

    def code(self, code, action):
        with self.assertRaises(RegistryError) as error:
            action()
        self.assertEqual(code, error.exception.code)

    def test_reviewed_owned_photo_removed_once_without_database_mutation(self):
        before = list(self.db.iterdump())
        plan = self.plan()
        self.assertEqual('ready', plan['entries'][0]['status'])
        result = self.execute(plan)
        self.assertFalse(self.photo.exists())
        self.assertEqual(1, result['deleted'])
        self.assertEqual(0, result['blocked'])
        self.assertFalse(result['account_deletion_complete'])
        self.assertEqual(before, list(self.db.iterdump()))
        self.assertEqual(1, self.execute(plan)['deleted'])
        self.assertNotIn('uploads/fixture.jpg', json.dumps(result))

    def test_missing_policy_keeps_photo_and_marks_pending(self):
        plan = self.plan({})
        self.assertIn('retention_policy_pending', plan['entries'][0]['reasons'])
        self.assertEqual(1, self.execute(plan)['blocked'])
        self.assertTrue(self.photo.exists())

    def test_policy_must_be_explicitly_fictional(self):
        plan = self.plan({'uploads/fixture.jpg':{'action':'delete','reference':'approval'}})
        self.assertIn('retention_policy_pending', plan['entries'][0]['reasons'])

    def test_other_customer_shared_reference_blocks_removal(self):
        self.add_item(2, 2, 'uploads/fixture.jpg')
        plan = self.plan()
        self.assertIn('shared_or_unverified_owner', plan['entries'][0]['reasons'])
        self.assertEqual(1, self.execute(plan)['blocked'])
        self.assertTrue(self.photo.exists())

    def test_same_customer_two_items_sharing_photo_also_hold(self):
        self.add_item(2, 1, 'uploads/fixture.jpg')
        self.assertIn('shared_between_items', self.plan()['entries'][0]['reasons'])

    def test_current_owner_differs_from_legacy_customer(self):
        self.db.execute("UPDATE business_item_assignments SET party_id='owner2'")
        self.db.commit()
        self.assertIn('shared_or_unverified_owner', self.plan()['entries'][0]['reasons'])

    def test_unverified_registry_link_is_not_ownership(self):
        self.db.execute("UPDATE business_item_links SET link_status='unresolved'")
        self.db.commit()
        self.assertIn('shared_or_unverified_owner', self.plan()['entries'][0]['reasons'])

    def test_retired_photo_with_other_owner_still_blocks(self):
        self.db.execute("INSERT INTO stage2_photo_records VALUES(1,2,'uploads/fixture.jpg','2026-01-01')")
        self.db.commit()
        self.assertIn('shared_or_unverified_owner', self.plan()['entries'][0]['reasons'])

    def test_json_listing_or_unknown_attachment_reference_is_retained(self):
        self.db.execute('CREATE TABLE old_evidence(snapshot_json TEXT)')
        self.db.execute('INSERT INTO old_evidence VALUES(?)', (json.dumps({'photo':['uploads/fixture.jpg']}),))
        self.db.commit()
        self.assertIn('retained_or_unknown_reference', self.plan()['entries'][0]['reasons'])

    def test_malformed_additional_photo_json_stops_delete(self):
        self.db.execute("UPDATE merchandise SET additional_photos='[broken'")
        self.db.commit()
        self.assertIn('malformed_additional_photos', self.plan()['entries'][0]['reasons'])

    def test_changed_content_is_not_deleted(self):
        plan = self.plan()
        self.photo.write_bytes(b'replaced content')
        result = self.execute(plan)
        self.assertEqual('file_changed', next(iter(result['entries'].values()))['reason'])
        self.assertTrue(self.photo.exists())

    def test_ownership_change_after_plan_is_not_deleted(self):
        plan = self.plan()
        self.db.execute("UPDATE business_item_assignments SET party_id='owner2'")
        self.db.commit()
        result = self.execute(plan)
        self.assertEqual('references_changed', next(iter(result['entries'].values()))['reason'])
        self.assertTrue(self.photo.exists())

    def test_reappeared_same_content_never_deleted_by_old_plan(self):
        contents = self.photo.read_bytes()
        plan = self.plan()
        self.execute(plan)
        self.photo.write_bytes(contents)
        for _ in range(2):
            result = self.execute(plan)
            self.assertEqual('file_reappeared', next(iter(result['entries'].values()))['reason'])
            self.assertTrue(self.photo.exists())

    def test_interruption_after_unlink_has_durable_intent_and_retry(self):
        plan = self.plan()
        from account_erasure_files import _save
        calls = 0
        def interrupted(path, value):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError('fictional process interruption after unlink')
            return _save(path, value)
        with patch('account_erasure_files._save', side_effect=interrupted):
            with self.assertRaises(RuntimeError):
                self.execute(plan)
        self.assertFalse(self.photo.exists())
        result = self.execute(plan)
        self.assertEqual(1, result['absent_after_intent'])
        self.assertEqual(0, result['deleted'])

    def test_missing_without_intent_is_not_claimed_deleted(self):
        plan = self.plan()
        self.photo.unlink()
        result = self.execute(plan)
        self.assertEqual(0, result['deleted'])
        self.assertEqual('missing_before_execution', next(iter(result['entries'].values()))['reason'])

    def test_changed_plan_rejected(self):
        plan = self.plan()
        plan['entries'][0]['sha256'] = '0' * 64
        self.code('file_cleanup_plan_changed', lambda:self.execute(plan))
        self.assertTrue(self.photo.exists())

    def test_missing_marker_rejected(self):
        (self.root/MARKER_NAME).unlink()
        self.code('file_cleanup_path_missing', self.plan)
        self.assertTrue(self.photo.exists())

    def test_database_outside_marked_root_rejected(self):
        with tempfile.TemporaryDirectory(prefix='account-erasure-files-') as path:
            alternate = Path(path)
            (alternate/MARKER_NAME).write_text(json.dumps(MARKER), encoding='utf-8')
            self.code('file_cleanup_fixture_only', lambda:plan_file_cleanup(self.store,1,fixture_root=alternate))

    def test_path_traversal_external_and_document_are_never_removed(self):
        for reference in ('uploads/../fixture.jpg', 'https://example.invalid/fixture.jpg',
                          'uploads/fixture.pdf', 'uploads/%2e%2e/fixture.jpg', 'uploads\\fixture.jpg'):
            with self.subTest(reference=reference):
                self.db.execute('UPDATE merchandise SET photo_path=?', (reference,))
                self.db.execute('UPDATE stage2_photo_records SET photo_path=?', (reference,))
                self.db.commit()
                plan = self.plan({reference:{'action':'delete','synthetic_only':True,'reference':'fixture'}})
                self.assertEqual('blocked', plan['entries'][0]['status'])
                self.assertEqual(1, self.execute(plan)['blocked'])
                self.assertTrue(self.photo.exists())

    def test_hardlinked_file_is_not_removed(self):
        alias = self.photo.with_name('alias.jpg')
        os.link(self.photo, alias)
        self.assertIn('file_cleanup_shared_file', self.plan()['entries'][0]['reasons'])
        self.assertTrue(alias.exists())

    @unittest.skipUnless(os.name == 'nt', 'Windows case-insensitive file reference regression')
    def test_windows_case_alias_owned_by_other_customer_is_shared(self):
        self.add_item(2, 2, 'uploads/FIXTURE.jpg')
        plan = self.plan()
        self.assertEqual(1, len(plan['entries']))
        self.assertIn('shared_or_unverified_owner', plan['entries'][0]['reasons'])
        self.assertEqual(1, self.execute(plan)['blocked'])
        self.assertTrue(self.photo.exists())

    def test_reparse_detection_is_fail_closed(self):
        from account_erasure_files import _plain
        original = Path.lstat
        class Reparse:
            st_mode = 0o100600
            st_nlink = 1
            st_file_attributes = 0x400
        with patch.object(Path, 'lstat', lambda path:Reparse() if path == self.photo else original(path)):
            self.code('file_cleanup_unsafe_path', lambda:_plain(self.photo))

    def test_private_mobile_reference_maps_only_inside_fixture(self):
        private = self.root/'static/uploads/.mobile-private/private.jpg'
        private.parent.mkdir()
        private.write_bytes(b'fictional private image')
        reference = 'uploads/mobile/private.jpg'
        self.db.execute('UPDATE merchandise SET photo_path=?',(reference,))
        self.db.execute('UPDATE stage2_photo_records SET photo_path=?',(reference,))
        self.db.commit()
        plan = self.plan({reference:{'action':'delete','synthetic_only':True,'reference':'fixture'}})
        self.assertEqual(1, self.execute(plan)['deleted'])
        self.assertFalse(private.exists())
        self.assertTrue(self.photo.exists())

    def test_stale_or_active_executor_lock_stops_retry(self):
        plan = self.plan()
        (self.root/'.file-cleanup.lock').write_text('fixture')
        self.code('file_cleanup_busy_or_interrupted',lambda:self.execute(plan))
        self.assertTrue(self.photo.exists())

    def test_open_database_transaction_is_rejected(self):
        plan = self.plan()
        self.db.execute('BEGIN')
        self.code('file_cleanup_open_transaction',lambda:self.execute(plan))
        self.db.rollback()
        self.assertTrue(self.photo.exists())

    def test_temporary_table_cannot_shadow_actual_ownership(self):
        self.db.execute('CREATE TEMP TABLE merchandise(id INTEGER,user_id INTEGER,photo_path TEXT,additional_photos TEXT)')
        self.code('file_cleanup_temporary_schema_unverified', self.plan)
        self.assertTrue(self.photo.exists())

    def test_temporary_trigger_including_sqlite_like_name_is_rejected(self):
        # '_' in LIKE is a wildcard; sqliteXshadow is a legal user object,
        # unlike SQLite's reserved literal sqlite_ prefix.
        self.db.execute('''CREATE TEMP TRIGGER sqliteXshadow AFTER UPDATE ON main.merchandise
            BEGIN UPDATE merchandise SET amount=0 WHERE id=1; END''')
        self.code('file_cleanup_temporary_schema_unverified', self.plan)
        self.assertEqual(30000, self.db.execute('SELECT amount FROM merchandise').fetchone()[0])
        self.assertTrue(self.photo.exists())


if __name__ == '__main__':
    unittest.main()
