from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
from unittest.mock import patch

from account_erasure import ErasureService, migration_statements
from account_erasure_copies import FixtureCopies, verify_fixture_copies
from item_registry import Registry
from test_account_lifecycle import LifecycleFixture


class CopyErasureTests(LifecycleFixture):
    def setUp(self):
        super().setUp()
        self.now = '2026-11-25T12:00:00+09:00'
        self.clock = lambda: self.now
        for statement in migration_statements():
            self.db.execute(statement)
        self.db.commit()
        self.erasure = ErasureService(self.store, self.clock)
        self.copies = FixtureCopies(self.store, self.path, self.clock)
        self.copies.initialize(self.staff)
        self.private_photo = b'before-deletion synthesized photograph'
        self.private_provider = 'synthetic-provider-record-only-9d7bf0'
        self.other_backup = self.copies.seed_backup(self.staff, 2, payload=b'another customer synthetic photograph')
        self.other_provider = self.copies.seed_provider(self.staff, 2, service='email', payload='fixture-b@example.invalid')

    def start(self, *, backup='erase', provider='erase', expiry=None):
        request = self.api.request(self.customer)
        self.request_id = request['request_id']
        plan = self.erasure.prepare(self.staff, 1, self.request_id)
        self.policies = {}
        for task, action in (('backups', backup), ('providers', provider)):
            policy = {'action': action, 'reference': 'fictional-approval-' + task,
                      'retain_until': expiry if action == 'retain' else None}
            self.policies[task] = policy
            plan = self.erasure.decide(self.staff, 1, self.request_id, revision=plan['revision'],
                                       task_key=task, reason='Synthetic proof only', **policy)
        self.close(self.request_id)

    def reconcile(self, task):
        return self.copies.reconcile(self.staff, 1, self.request_id, task=task, **self.policies[task])

    def verify(self, **options):
        scope = {'fixture_database': self.path, 'backup': self.copies, 'providers': self.copies}
        scope.update(options)
        return verify_fixture_copies(self.store, self.staff, 1, self.request_id, **scope)

    def archives(self, copy_id):
        return self.copies.directory / f'backup-{copy_id}.copy'

    def provider(self, copy_id):
        with closing(sqlite3.connect(self.copies.catalogue)) as connection:
            return connection.execute('SELECT state,payload FROM copies WHERE copy_id=?', (copy_id,)).fetchone()

    def test_actual_owned_archive_removed_provider_scrubbed_and_both_idempotent(self):
        photo = self.copies.seed_backup(self.staff, 1, payload=self.private_photo)
        provider = self.copies.seed_provider(self.staff, 1, service='email', payload=self.private_provider)
        self.assertEqual(self.private_photo, self.archives(photo).read_bytes())
        self.assertIn(self.private_provider.encode(), self.copies.catalogue.read_bytes())
        self.start()
        with patch('socket.create_connection', side_effect=AssertionError('must remain offline')):
            proof = {task: self.reconcile(task) for task in self.policies}
            self.assertEqual(proof, self.verify())
            self.assertEqual(proof, {task: self.reconcile(task) for task in self.policies})
        self.assertEqual('erasure_pending', self.api.get(self.staff, 1)['state'])
        self.assertFalse(self.archives(photo).exists())
        self.assertEqual(('erased', None), self.provider(provider))
        self.assertNotIn(self.private_provider.encode(), self.copies.catalogue.read_bytes())
        self.assertNotIn(self.private_provider, json.dumps(proof))
        self.assertEqual(b'another customer synthetic photograph', self.archives(self.other_backup).read_bytes())
        self.assertEqual(('active', 'fixture-b@example.invalid'), self.provider(self.other_provider))
        for evidence in proof.values():
            self.assertRegex(evidence['digest'], r'^[a-f0-9]{64}$')

    def test_missing_provider_result_blocks_even_with_backup_completed(self):
        self.copies.seed_backup(self.staff, 1, payload=self.private_photo)
        provider = self.copies.seed_provider(self.staff, 1, service='email', payload=self.private_provider)
        self.start()
        self.reconcile('backups')
        self.error('erasure_copy_evidence_required', self.verify)
        self.error('erasure_copy_evidence_required', lambda: self.verify(providers=None))
        self.assertEqual(('active', self.private_provider), self.provider(provider))
        self.assertEqual(('active', 'fixture-b@example.invalid'), self.provider(self.other_provider))

    def test_unknown_provider_outcome_cannot_be_reconciled_as_success(self):
        provider = self.copies.seed_provider(self.staff, 1, service='email', payload=self.private_provider)
        self.start(backup='not_applicable')
        self.reconcile('backups')
        with closing(sqlite3.connect(self.copies.catalogue)) as connection, connection:
            connection.execute("UPDATE copies SET state='unknown' WHERE copy_id=?", (provider,))
        self.error('erasure_copy_evidence_required', lambda: self.reconcile('providers'))
        self.error('erasure_copy_evidence_required', self.verify)
        self.assertEqual(('unknown', self.private_provider), self.provider(provider))

    def test_shared_backup_cannot_be_erased(self):
        archive = self.copies.seed_backup(self.staff, 1, payload=self.private_photo, shared=True)
        self.start(provider='not_applicable')
        self.error('erasure_copy_evidence_required', lambda: self.reconcile('backups'))
        self.assertEqual(self.private_photo, self.archives(archive).read_bytes())

    def test_shared_backup_finite_retention_requires_recorded_policy_and_fresh_expiry(self):
        archive = self.copies.seed_backup(self.staff, 1, payload=self.private_photo, shared=True)
        self.start(backup='retain', provider='not_applicable', expiry='2026-12-15')
        self.error('erasure_copy_policy_invalid', lambda: self.copies.reconcile(self.staff, 1, self.request_id,
            task='backups', action='retain', reference='unmatched', retain_until='2026-12-15'))
        self.reconcile('backups')
        self.reconcile('providers')
        self.assertEqual('2026-12-15', self.verify()['backups']['retain_until'])
        self.assertEqual(self.private_photo, self.archives(archive).read_bytes())
        self.now = '2026-12-15T00:00:00+09:00'
        self.error('erasure_copy_policy_invalid', self.verify)
        self.assertEqual(self.private_photo, self.archives(archive).read_bytes())

    def test_scoped_provider_retention_is_not_erasable_without_a_new_decision(self):
        provider = self.copies.seed_provider(self.staff, 1, service='payments', payload=self.private_provider)
        self.start(backup='not_applicable', provider='retain', expiry='2026-12-15')
        self.reconcile('backups')
        self.reconcile('providers')
        self.assertEqual('retain', self.verify()['providers']['action'])
        self.error('erasure_copy_policy_invalid', lambda: self.copies.reconcile(self.staff, 1, self.request_id,
            task='providers', action='erase', reference='fictional-approval-providers'))
        self.assertEqual(('active', self.private_provider), self.provider(provider))

    def test_wrong_customer_request_or_wrong_role_does_not_touch_archives(self):
        archive = self.copies.seed_backup(self.staff, 1, payload=self.private_photo)
        self.start(provider='not_applicable')
        for customer_id, request_id in ((2, self.request_id), (1, 'another-request')):
            self.error('erasure_copy_scope_mismatch', lambda: self.copies.reconcile(self.staff, customer_id, request_id,
                task='backups', **self.policies['backups']))
        self.error('account_access_denied', lambda: self.copies.reconcile(self.customer, 1, self.request_id,
            task='backups', **self.policies['backups']))
        self.assertEqual(self.private_photo, self.archives(archive).read_bytes())

    def test_unmarked_database_rejected_before_any_artifact_created(self):
        unmanaged = self.path.parent / 'account-fixture-unmarked.sqlite3'
        with closing(sqlite3.connect(unmanaged)) as connection:
            self.error('fixture_only', lambda: FixtureCopies(Registry(connection), unmanaged, self.clock))
        self.assertFalse(unmanaged.with_suffix('.copies').exists())

    def test_other_database_catalogue_cannot_be_used_for_verification(self):
        self.start(backup='not_applicable', provider='not_applicable')
        self.reconcile('backups')
        self.reconcile('providers')
        foreign = self.path.parent / 'account-fixture-foreign.sqlite3'
        foreign.with_suffix('.fixture.json').write_bytes(self.path.with_suffix('.fixture.json').read_bytes())
        with closing(sqlite3.connect(foreign)) as connection:
            self.db.backup(connection)
            different = FixtureCopies(Registry(connection), foreign, self.clock)
            self.error('erasure_copy_scope_mismatch', lambda: self.verify(backup=different))

    def test_reappearing_backup_file_invalidates_completed_proof(self):
        archive = self.copies.seed_backup(self.staff, 1, payload=self.private_photo)
        self.start(provider='not_applicable')
        self.reconcile('backups')
        self.reconcile('providers')
        self.archives(archive).write_bytes(self.private_photo)
        self.error('erasure_copy_evidence_required', self.verify)
        self.error('erasure_copy_evidence_required', lambda: self.reconcile('backups'))
        self.assertTrue(self.archives(archive).exists())

    def test_hardlinked_archive_rejected_without_removing_either_name(self):
        archive = self.copies.seed_backup(self.staff, 1, payload=self.private_photo)
        self.start(provider='not_applicable')
        alternate = Path(self.tmp.name) / 'another-fixture-copy.jpg'
        try:
            os.link(self.archives(archive), alternate)
        except OSError:
            self.skipTest('Hard links unavailable on this filesystem.')
        self.error('erasure_copy_scope_mismatch', lambda: self.reconcile('backups'))
        self.assertTrue(self.archives(archive).exists())
        self.assertTrue(alternate.exists())

    def test_missing_backup_before_intent_never_treated_as_completed(self):
        archive = self.copies.seed_backup(self.staff, 1, payload=self.private_photo)
        self.start(provider='not_applicable')
        self.archives(archive).unlink()
        self.error('erasure_copy_evidence_required', lambda: self.reconcile('backups'))

    def test_recovery_of_known_interruption_after_intent_is_idempotent(self):
        archive = self.copies.seed_backup(self.staff, 1, payload=self.private_photo)
        self.start(provider='not_applicable')
        self.copies._record_erasure_intent(1, self.request_id, 'backups')
        self.archives(archive).unlink()
        self.reconcile('providers')
        expected = self.reconcile('backups')
        self.assertEqual(expected, self.verify()['backups'])

    def test_not_applicable_cannot_hide_existing_copy(self):
        archive = self.copies.seed_backup(self.staff, 1, payload=self.private_photo)
        self.start(backup='not_applicable', provider='not_applicable')
        self.error('erasure_copy_evidence_required', lambda: self.reconcile('backups'))
        self.assertTrue(self.archives(archive).exists())

    def test_unregistered_artifact_blocks_a_previously_empty_backup_inventory(self):
        self.start(backup='not_applicable', provider='not_applicable')
        self.reconcile('backups')
        self.reconcile('providers')
        unmanaged = self.copies.directory / 'unexpected-backup.copy'
        unmanaged.write_bytes(self.private_photo)
        self.error('erasure_copy_evidence_required', self.verify)
        self.assertEqual(self.private_photo, unmanaged.read_bytes())
