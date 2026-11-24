import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from account_erasure import ErasureService
from account_erasure_copies import FixtureCopies
from account_erasure_files import execute_fixture_cleanup, plan_file_cleanup
from account_lifecycle import LifecycleService
from item_registry import Actor, Registry, RegistryError
from scripts.verify_account_erasure_workflow import fixture


class FixtureCompletionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='account-erasure-files-completion-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        db, self.database, _ = fixture(self.root)
        self.addCleanup(db.close)
        self.store = Registry(db)
        self.customer, self.staff = Actor(1), Actor(3)
        self.now = '2026-11-25T12:00:00+09:00'
        self.erasure = ErasureService(self.store, clock=lambda: self.now)
        self.lifecycle = LifecycleService(self.store, clock=lambda: self.now)
        self.copies = FixtureCopies(self.store, self.database, clock=lambda: self.now)
        self.backup = self.copies.seed_backup(self.staff, 1, payload=b'Disposable photo copy')
        self.copies.seed_provider(self.staff, 1, service='email', payload='fake data for account 1')
        self.request_id = self.lifecycle.request(self.customer)['request_id']
        self.plan = self.erasure.prepare(self.staff, 1, self.request_id)
        self.photo = self.root / 'static/uploads/fictional-1.jpg'
        self.other_photo = self.root / 'static/uploads/fictional-2.jpg'

    def decide(self, task, action, *, retain_until=None):
        self.plan = self.erasure.decide(self.staff, 1, self.request_id, revision=self.plan['revision'], task_key=task,
                                        action=action, reason='Explicit synthetic review', reference='review-' + task,
                                        retain_until=retain_until)

    def review_all(self, *, backup_action='erase'):
        for task in ('profile', 'notifications', 'photos', 'providers'):
            self.decide(task, 'erase')
        for task in ('goods', 'money', 'records'):
            self.decide(task, 'retain', retain_until='2026-12-25')
        for task in ('unclassified', 'completion_notice'):
            self.decide(task, 'not_applicable')
        self.decide('backups', backup_action, retain_until='2026-12-25' if backup_action == 'retain' else None)
        self.erasure.rehearse_local(self.staff, 1, self.request_id, fixture_database=self.database,
                                   revision=self.plan['revision'])
        self.plan = self.erasure.get(self.staff, 1, self.request_id)

    def prove_copies(self):
        for task in ('backups', 'providers'):
            decision = self.plan['decisions'][task]
            self.copies.reconcile(self.staff, 1, self.request_id, task=task, action=decision['action'],
                                  reference=decision['reference'], retain_until=decision['retain_until'])

    def prove_photo(self):
        plan = plan_file_cleanup(self.store, 1, fixture_root=self.root, decisions={
            'uploads/fictional-1.jpg': {'action': 'delete', 'synthetic_only': True, 'reference': 'review-photos'},
        })
        self.assertEqual(1, execute_fixture_cleanup(self.store, plan, fixture_root=self.root)['deleted'])
        return plan['plan_id']

    def complete(self, **changes):
        options = {'fixture_database': self.database, 'fixture_root': self.root, 'revision': self.plan['revision'],
                   'backup': self.copies, 'providers': self.copies, **changes}
        return self.erasure.complete_fixture(self.staff, 1, self.request_id, **options)

    def state(self):
        return self.lifecycle.get(self.staff, 1)['state']

    def test_remains_pending_until_review_and_real_copy_evidence(self):
        self.assertEqual({'state': 'requested', 'complete': False, 'blockers': ['local_erasure_required']}, self.complete())
        self.review_all()
        self.assertEqual(['backups', 'providers'], self.complete()['blockers'])
        self.assertFalse(self.complete(backup={'digest': 'a' * 64}, providers={'complete': True})['complete'])
        self.assertEqual('erasure_pending', self.state())
        self.assertTrue(self.photo.is_file())

    def test_complete_clears_proven_references_and_preserves_other_customer(self):
        other_before = self.store.query('SELECT * FROM users WHERE id=2')
        money_before = self.store.query('SELECT * FROM invoices ORDER BY id')
        ownership_before = self.store.query('SELECT * FROM business_item_assignments ORDER BY physical_item_id')
        self.review_all(backup_action='retain')
        self.prove_copies()
        self.assertEqual(['photos'], self.complete()['blockers'])
        file_plan = self.prove_photo()
        result = self.complete(file_plan_id=file_plan)
        self.assertTrue(result['complete'])
        self.assertEqual('completed', self.state())
        self.assertEqual(result, self.complete())
        self.assertEqual(result, self.erasure.fixture_completion(self.staff, 1, self.request_id))
        self.assertEqual(1, result['deleted_photos'])
        self.assertEqual(2, result['photo_references_removed'])
        self.assertRegex(result['evidence_sha256'], r'^[a-f0-9]{64}$')
        self.assertFalse(self.photo.exists())
        self.assertTrue(self.other_photo.is_file())
        self.assertIsNone(self.store.query('SELECT photo_path FROM merchandise WHERE id=1')[0]['photo_path'])
        self.assertEqual('uploads/fictional-2.jpg', self.store.query('SELECT photo_path FROM merchandise WHERE id=2')[0]['photo_path'])
        self.assertEqual([], self.store.query('SELECT * FROM stage2_photo_records WHERE owner_user_id=1'))
        self.assertTrue((self.copies.directory / f'backup-{self.backup}.copy').is_file())
        self.assertEqual(other_before, self.store.query('SELECT * FROM users WHERE id=2'))
        self.assertEqual(money_before, self.store.query('SELECT * FROM invoices ORDER BY id'))
        self.assertEqual(ownership_before, self.store.query('SELECT * FROM business_item_assignments ORDER BY physical_item_id'))
        self.assertEqual(1, len(self.store.query("SELECT event_id FROM account_erasure_events WHERE kind='fixture_completed'")))
        self.store.execute('CREATE TABLE kaika_recovery_quarantine(reason TEXT)')
        self.store.connection.commit()
        self.assertIsNone(self.erasure.fixture_completion(self.staff, 1, self.request_id))

    def test_reappearing_file_and_unclassified_rows_block_completion(self):
        self.review_all()
        self.prove_copies()
        file_plan = self.prove_photo()
        self.photo.write_bytes(b'Disposable file reappeared')
        self.assertEqual(['photos'], self.complete(file_plan_id=file_plan)['blockers'])
        self.photo.unlink()
        self.store.execute('CREATE TABLE unresolved_fixture_copy(note TEXT)')
        self.store.execute('INSERT INTO unresolved_fixture_copy VALUES (?)', ('Must be reviewed',))
        self.store.connection.commit()
        self.assertEqual(['unclassified'], self.complete(file_plan_id=file_plan)['blockers'])
        self.assertEqual('erasure_pending', self.state())
        self.assertIsNotNone(self.store.query('SELECT photo_path FROM merchandise WHERE id=1')[0]['photo_path'])

    def test_abort_rolls_back_photo_references_and_completion_together(self):
        self.review_all()
        self.prove_copies()
        file_plan = self.prove_photo()
        with patch.object(self.erasure, '_event', side_effect=RuntimeError('Fictional recording failure')):
            with self.assertRaises(RuntimeError):
                self.complete(file_plan_id=file_plan)
        self.assertEqual('erasure_pending', self.state())
        self.assertIsNotNone(self.store.query('SELECT photo_path FROM merchandise WHERE id=1')[0]['photo_path'])
        self.assertTrue(self.complete(file_plan_id=file_plan)['complete'])

    def test_expired_retention_reintroduced_profile_and_invalid_receipt_block(self):
        self.review_all()
        self.prove_copies()
        file_plan = self.prove_photo()
        self.now = '2026-12-26T12:00:00+09:00'
        self.assertEqual(['goods', 'money', 'records'], self.complete(file_plan_id=file_plan)['blockers'])
        self.now = '2026-11-25T12:00:00+09:00'
        self.store.execute('UPDATE users SET address=? WHERE id=1', ('Synthetic reintroduced address',))
        self.store.connection.commit()
        self.assertEqual(['local_erasure_required'], self.complete(file_plan_id=file_plan)['blockers'])
        self.store.execute("UPDATE users SET address='' WHERE id=1")
        self.store.execute("UPDATE stage6_notification_outbox SET state='queued' WHERE customer_id=1")
        self.store.connection.commit()
        self.assertEqual(['local_erasure_required'], self.complete(file_plan_id=file_plan)['blockers'])
        self.store.execute("UPDATE stage6_notification_outbox SET state='suppressed' WHERE customer_id=1")
        self.store.execute("UPDATE account_deletion_requests SET state='completed' WHERE customer_id=1")
        self.store.connection.commit()
        self.assertIsNone(self.erasure.fixture_completion(self.staff, 1, self.request_id))
        with self.assertRaises(RegistryError) as error:
            self.complete(file_plan_id=file_plan)
        self.assertEqual('erasure_completion_evidence_required', error.exception.code)

    def test_denies_non_staff_wrong_fixture_stale_revision_and_quarantine(self):
        self.review_all()
        cases = [
            ('account_access_denied', lambda: self.erasure.complete_fixture(self.customer, 1, self.request_id,
                fixture_database=self.database, revision=self.plan['revision'])),
            ('fixture_only', lambda: self.complete(fixture_database=self.root / 'wrong.sqlite3')),
            ('erasure_plan_stale', lambda: self.complete(revision=self.plan['revision'] - 1)),
        ]
        for code, call in cases:
            with self.subTest(code=code), self.assertRaises(RegistryError) as error:
                call()
            self.assertEqual(code, error.exception.code)
        self.store.execute('CREATE TABLE kaika_recovery_quarantine(reason TEXT)')
        self.store.connection.commit()
        with self.assertRaises(RegistryError) as error:
            self.complete()
        self.assertEqual('erasure_fixture_restore_quarantined', error.exception.code)

    def test_completion_requires_exact_typed_audit_pair(self):
        self.review_all()
        recorded = {'plan_id': self.plan['plan_id'], 'request_id': self.request_id,
                    'state': 'completed', 'complete': True, 'customer_id': 1, 'evidence_sha256': 'a' * 64}
        boolean_customer = {**recorded, 'customer_id': True}
        for completion, event in ((boolean_customer, json.dumps(boolean_customer, sort_keys=True)),
                                  (recorded, json.dumps(boolean_customer, sort_keys=True)),
                                  (recorded, '{bad JSON}')):
            self.store.execute('SAVEPOINT fake_completion')
            try:
                self.store.execute('UPDATE account_erasure_plans SET fixture_result_json=? WHERE plan_id=?',
                                   (json.dumps({'completion': completion}), self.plan['plan_id']))
                self.store.execute("UPDATE account_deletion_requests SET state='completed' WHERE customer_id=1")
                self.store.execute("INSERT INTO account_erasure_events VALUES (?,?,3,'fixture_completed',?,?)",
                                   ('fictional-invalid-completion', self.plan['plan_id'], event, self.now))
                self.assertIsNone(self.erasure.fixture_completion(self.staff, 1, self.request_id))
            finally:
                self.store.execute('ROLLBACK TO SAVEPOINT fake_completion')
                self.store.execute('RELEASE SAVEPOINT fake_completion')
