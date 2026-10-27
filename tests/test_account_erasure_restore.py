import copy
import hashlib
import hmac
import json
import os
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch

from account_erasure_restore import (prepare_fixture_bindings, export_fixture_manifest,
    replay_fixture_manifest, manifest_digest, MARKER)
from account_lifecycle import LifecycleService, access_allowed_in_store, outbound_allowed_in_store
from item_registry import Actor, Registry, RegistryError
from recovery_runtime_guard import guard_new_connection, RecoveryQuarantineError
from test_account_lifecycle import LifecycleFixture
from werkzeug.security import check_password_hash


class ErasureRecoveryTests(LifecycleFixture):
    def setUp(self):
        super().setUp()
        self.environment = 'a' * 32
        self.key = b'fictional-recovery-signing-key-only-32-bytes'
        self.db.execute('CREATE TABLE push_outbox(id INTEGER,user_id INTEGER,state TEXT)')
        self.db.executemany('INSERT INTO push_outbox VALUES(?,?,?)', [(1,1,'queued'), (2,1,'sending'), (3,2,'queued')])
        self.db.commit()
        prepare_fixture_bindings(self.store,self.staff,fixture_database=self.path,environment_id=self.environment)
        self.db.commit()
        self.restored_path = Path(self.tmp.name)/'account-fixture-restored.sqlite3'
        self.restored_path.with_suffix('.fixture.json').write_text(json.dumps(MARKER),encoding='utf-8')
        self.restored_db = sqlite3.connect(self.restored_path)
        self.addCleanup(self.restored_db.close)
        self.db.backup(self.restored_db)
        self.restored_db.execute('CREATE TABLE kaika_recovery_quarantine(reason TEXT)')
        self.restored_db.execute("INSERT INTO kaika_recovery_quarantine VALUES('fixture')")
        self.restored_db.commit()
        self.restored = Registry(self.restored_db)

    def manifest(self, generation=1):
        return export_fixture_manifest(self.store,self.staff,fixture_database=self.path,
            environment_id=self.environment,generation=generation,signing_key=self.key)

    def apply(self, envelope, **overrides):
        args = dict(fixture_database=self.restored_path,envelope=envelope,
            environment_id=self.environment,expected_manifest_sha256=manifest_digest(envelope),signing_key=self.key)
        args.update(overrides)
        return replay_fixture_manifest(self.restored,self.staff,**args)

    def close_and_manifest(self):
        row=self.api.request(self.customer)
        self.close(row['request_id'])
        return self.manifest()

    def resign(self,envelope):
        payload=json.dumps(envelope['payload'],sort_keys=True,ensure_ascii=True,separators=(',',':'),allow_nan=False).encode()
        envelope['signature']=hmac.new(self.key,payload,hashlib.sha256).hexdigest()
        return envelope

    def test_replay_closes_old_auth_and_keeps_financial_and_other_customer_records(self):
        result=self.apply(self.close_and_manifest())
        self.assertFalse(result['erasure_complete'])
        self.assertTrue(result['quarantine_retained'])
        self.assertFalse(access_allowed_in_store(self.restored,1))
        self.assertFalse(outbound_allowed_in_store(self.restored,1))
        self.assertTrue(access_allowed_in_store(self.restored,2))
        user=self.restored.query('SELECT * FROM users WHERE id=1')[0]
        self.assertEqual('',user['email'])
        self.assertEqual('',user['address'])
        self.assertFalse(check_password_hash(user['password_hash'],self.password))
        self.assertEqual(30000,user['proxy_service_budget'])
        self.assertEqual(1155,self.restored.query('SELECT amount FROM invoices')[0]['amount'])
        self.assertEqual('fixture2@example.invalid',self.restored.query('SELECT email FROM users WHERE id=2')[0]['email'])
        self.assertEqual(['cancelled','unknown','queued'],[r['state'] for r in self.restored.query('SELECT state FROM push_outbox ORDER BY id')])
        self.assertEqual(0,self.restored.query('SELECT enabled FROM push_devices')[0]['enabled'])
        self.assertIsNotNone(self.restored.query('SELECT revoked_at FROM mobile_sessions')[0]['revoked_at'])

    def test_quarantine_is_retained_and_real_runtime_guard_still_rejects(self):
        self.apply(self.close_and_manifest())
        fresh=sqlite3.connect(self.restored_path)
        with self.assertRaises(RecoveryQuarantineError):guard_new_connection(fresh)
        with self.assertRaises(sqlite3.ProgrammingError):fresh.execute('SELECT 1')

    def test_without_marker_or_quarantine_cannot_apply(self):
        envelope=self.close_and_manifest()
        self.restored_path.with_suffix('.fixture.json').unlink()
        self.error('fixture_only',lambda:self.apply(envelope))
        self.restored_path.with_suffix('.fixture.json').write_text(json.dumps(MARKER),encoding='utf-8')
        self.restored.execute('DROP TABLE kaika_recovery_quarantine')
        self.error('deletion_restore_quarantine_required',lambda:self.apply(envelope))
        self.assertTrue(access_allowed_in_store(self.restored,1))

    def test_signature_and_latest_digest_both_required(self):
        envelope=self.close_and_manifest()
        self.error('deletion_restore_latest_digest_required',lambda:self.apply(envelope,expected_manifest_sha256=None))
        self.error('deletion_restore_signature_invalid',lambda:self.apply(envelope,signing_key=b'different-key-that-is-at-least-32-bytes'))
        corrupt=copy.deepcopy(envelope);corrupt['payload']['entries'][0]['request']['state']='withdrawn'
        self.error('deletion_restore_signature_invalid',lambda:self.apply(corrupt))
        self.assertTrue(access_allowed_in_store(self.restored,1))

    def test_valid_but_stale_signed_manifest_is_rejected_by_independent_latest_anchor(self):
        old=self.manifest()
        latest=self.close_and_manifest()
        self.error('deletion_restore_latest_manifest_mismatch',lambda:self.apply(old,expected_manifest_sha256=manifest_digest(latest)))

    def test_environment_and_customer_binding_prevent_cross_database_id_reuse(self):
        envelope=self.close_and_manifest()
        self.error('deletion_restore_environment_mismatch',lambda:self.apply(envelope,environment_id='b'*32))
        self.restored.execute('UPDATE account_restore_bindings SET binding=? WHERE customer_id=1',('c'*32,))
        self.error('deletion_restore_customer_mismatch',lambda:self.apply(envelope))
        self.assertEqual('fixture1@example.invalid',self.restored.query('SELECT email FROM users WHERE id=1')[0]['email'])

    def test_missing_binding_or_manifest_entry_fails_closed(self):
        envelope=self.close_and_manifest()
        omitted=copy.deepcopy(envelope);omitted['payload']['entries'].pop()
        self.error('deletion_restore_bindings_incomplete',lambda:self.apply(self.resign(omitted)))
        self.restored.execute('DELETE FROM account_restore_bindings WHERE customer_id=1')
        self.error('deletion_restore_bindings_incomplete',lambda:self.apply(envelope))

    def test_missing_manifest_cannot_be_replaced_by_empty_success(self):
        self.error('deletion_restore_verification_failed',lambda:self.apply(None))
        self.assertTrue(access_allowed_in_store(self.restored,1))

    def test_repeated_replay_scrubs_restored_profile_again_without_duplicate_events(self):
        envelope=self.close_and_manifest()
        self.apply(envelope)
        before=self.restored.query('SELECT COUNT(*) AS n FROM account_deletion_events')[0]['n']
        self.restored.execute("UPDATE users SET email='restored-again@example.invalid' WHERE id=1")
        self.restored.execute("UPDATE account_deletion_requests SET review_json='{\"old\":true}',customer_notice='old completion text' WHERE customer_id=1")
        result=self.apply(envelope)
        self.assertTrue(result['already_recorded'])
        self.assertEqual('',self.restored.query('SELECT email FROM users WHERE id=1')[0]['email'])
        self.assertEqual(before,self.restored.query('SELECT COUNT(*) AS n FROM account_deletion_events')[0]['n'])
        self.assertEqual({'review_json':'{}','customer_notice':''},self.restored.query('SELECT review_json,customer_notice FROM account_deletion_requests WHERE customer_id=1')[0])

    def test_pending_request_suppresses_jobs_but_does_not_close_or_scrub_profile(self):
        self.api.request(self.customer)
        result=self.apply(self.manifest())
        self.assertEqual(0,result['closed_accounts'])
        self.assertTrue(access_allowed_in_store(self.restored,1))
        self.assertFalse(outbound_allowed_in_store(self.restored,1))
        self.assertEqual('fixture1@example.invalid',self.restored.query('SELECT email FROM users WHERE id=1')[0]['email'])
        self.assertEqual('cancelled',self.restored.query('SELECT state FROM push_outbox WHERE id=1')[0]['state'])

    def test_withdrawal_restores_current_state_without_resending_previously_cancelled_jobs(self):
        row=self.api.request(self.customer)
        old=self.manifest()
        self.api.withdraw(self.customer,row['request_id'])
        latest=self.manifest(2)
        self.error('deletion_restore_latest_manifest_mismatch',lambda:self.apply(old,expected_manifest_sha256=manifest_digest(latest)))
        self.apply(latest)
        self.assertTrue(access_allowed_in_store(self.restored,1))
        self.assertTrue(outbound_allowed_in_store(self.restored,1))
        self.assertEqual('cancelled',self.restored.query('SELECT state FROM push_outbox WHERE id=1')[0]['state'])

    def test_rerequest_uses_new_id_and_late_old_manifest_cannot_override(self):
        first=self.api.request(self.customer)
        self.api.withdraw(self.customer,first['request_id'])
        old=self.manifest(1)
        self.apply(old)
        second=self.api.request(self.customer)
        latest=self.manifest(2)
        self.apply(latest)
        self.assertEqual(second['request_id'],self.restored.query('SELECT request_id FROM account_deletion_requests WHERE customer_id=1')[0]['request_id'])
        self.error('deletion_restore_state_conflict',lambda:self.apply(old))

    def test_completed_source_is_erasure_pending_after_restore(self):
        self.close_and_manifest()
        self.store.execute("UPDATE account_deletion_requests SET state='completed',version=version+1 WHERE customer_id=1")
        self.apply(self.manifest())
        self.assertEqual('erasure_pending',self.restored.query('SELECT state FROM account_deletion_requests WHERE customer_id=1')[0]['state'])

    def test_non_staff_cannot_export_or_replay(self):
        self.error('account_access_denied',lambda:export_fixture_manifest(self.store,self.customer,
            fixture_database=self.path,environment_id=self.environment,generation=1,signing_key=self.key))
        envelope=self.close_and_manifest()
        self.error('account_access_denied',lambda:replay_fixture_manifest(self.restored,Actor(4),
            fixture_database=self.restored_path,envelope=envelope,environment_id=self.environment,
            expected_manifest_sha256=manifest_digest(envelope),signing_key=self.key))

    def test_manifest_contains_no_raw_profile_or_authentication_values(self):
        envelope=self.close_and_manifest()
        value=json.dumps(envelope)
        for forbidden in ('example.invalid',self.password,'fictional address','password_hash','email','token_hash'):
            self.assertNotIn(forbidden,value)

    def test_rejected_manifest_rolls_back_all_customers(self):
        self.close_and_manifest()
        row=self.api.request(self.other);self.api.close_fixture(self.staff,2,row['request_id'],fixture_database=self.path,review_reference='fixture')
        envelope=self.manifest()
        self.restored.execute("UPDATE account_restore_bindings SET binding=? WHERE customer_id=2",('f'*32,))
        self.error('deletion_restore_customer_mismatch',lambda:self.apply(envelope))
        self.assertEqual('fixture1@example.invalid',self.restored.query('SELECT email FROM users WHERE id=1')[0]['email'])
        self.assertTrue(access_allowed_in_store(self.restored,1))

    def test_failure_during_replay_leaves_no_partial_request_or_profile_change(self):
        envelope=self.close_and_manifest()
        with patch.object(LifecycleService,'suppress',side_effect=RuntimeError('fixture outage')):
            with self.assertRaises(RuntimeError):self.apply(envelope)
        self.assertEqual([],self.restored.query('SELECT * FROM account_deletion_requests'))
        self.assertEqual('fixture1@example.invalid',self.restored.query('SELECT email FROM users WHERE id=1')[0]['email'])
        self.assertEqual('queued',self.restored.query('SELECT state FROM push_outbox WHERE id=1')[0]['state'])

    def test_generation_must_advance_for_a_different_manifest(self):
        self.api.request(self.customer)
        self.apply(self.manifest(1))
        row=self.api.get(self.customer,1)
        self.close(row['request_id'])
        self.error('deletion_restore_generation_conflict',lambda:self.apply(self.manifest(1)))
        self.apply(self.manifest(2))
        self.assertFalse(access_allowed_in_store(self.restored,1))

    def test_attached_database_is_rejected_before_any_replay(self):
        envelope=self.close_and_manifest()
        self.restored.execute("ATTACH DATABASE ':memory:' AS other")
        self.error('fixture_only',lambda:self.apply(envelope))
        self.assertTrue(access_allowed_in_store(self.restored,1))

    def test_temp_schema_shadow_is_rejected(self):
        envelope=self.close_and_manifest()
        self.restored.execute('CREATE TEMP VIEW users AS SELECT * FROM main.users')
        self.error('fixture_only',lambda:self.apply(envelope))

    def test_temp_trigger_is_rejected_before_it_can_change_other_records(self):
        envelope=self.close_and_manifest()
        self.restored.execute('CREATE TEMP TRIGGER sqliteXshadow AFTER UPDATE ON main.users BEGIN UPDATE invoices SET amount=0; END')
        self.error('fixture_only',lambda:self.apply(envelope))
        self.assertEqual(1155,self.restored.query('SELECT amount FROM invoices')[0]['amount'])

    def test_hardlinked_database_is_rejected(self):
        envelope=self.close_and_manifest()
        alias=self.restored_path.with_name('account-fixture-hardlink.sqlite3')
        os.link(self.restored_path,alias)
        self.addCleanup(lambda:alias.unlink(missing_ok=True))
        self.error('fixture_only',lambda:self.apply(envelope))

    def test_hardlinked_marker_is_rejected(self):
        envelope=self.close_and_manifest()
        marker=self.restored_path.with_suffix('.fixture.json')
        alias=marker.with_name('marker-hardlink.json')
        os.link(marker,alias)
        self.addCleanup(lambda:alias.unlink(missing_ok=True))
        self.error('fixture_only',lambda:self.apply(envelope))


if __name__=='__main__':unittest.main()
