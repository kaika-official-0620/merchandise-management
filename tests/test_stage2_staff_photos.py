"""Photo-only receipt workflow: temporary DB, synthetic images, no app import."""
import html
import io
import json
import re
import unittest
from unittest.mock import patch

from flask import Flask
from types import SimpleNamespace

import test_stage2_self_management as fixtures
from stage2_staff_photos import register_stage2_staff_photos


class Stage2StaffPhotosTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures.Stage2SelfManagementTests.setUpClass.__func__(cls)

    db = fixtures.Stage2SelfManagementTests.db
    login = fixtures.Stage2SelfManagementTests.login
    form = fixtures.Stage2SelfManagementTests.form
    create = fixtures.Stage2SelfManagementTests.create
    record = fixtures.Stage2SelfManagementTests.record
    png = staticmethod(fixtures.Stage2SelfManagementTests.png)
    tearDown = fixtures.Stage2SelfManagementTests.tearDown
    row = fixtures.Stage2SelfManagementTests.row
    operation = fixtures.Stage2SelfManagementTests.operation
    action = fixtures.Stage2SelfManagementTests.action
    new_intake = fixtures.Stage2SelfManagementTests.new_intake
    receive = fixtures.Stage2SelfManagementTests.receive

    def setUp(self):
        fixtures.Stage2SelfManagementTests.setUp(self)
        self.app.add_url_rule('/fixture/individual/<item_id>', 'inventory_registry.detail', lambda item_id: 'fixture')
        self.photos = register_stage2_staff_photos(self.runtime)
        self.plans.snapshot = lambda user=None: {'plan_code': 'normal'}
        self.legacy_id = self.create({'photo': self.png()})
        intake_id, _ = self.new_intake(self.legacy_id)
        self.receive(intake_id)
        self.item_id = self.row('SELECT physical_item_id FROM business_item_links WHERE merchandise_id=?', (self.legacy_id,))['physical_item_id']
        self.path_url = f'/inventory/items/{self.item_id}/photos'
        self.login(3)

    def photo_form(self):
        response = self.client.get(self.path_url)
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return {key: html.unescape(re.search(r'name="' + key + r'" value="([^"]+)"', response.get_data(as_text=True))[1])
                for key in ('staff_photo_csrf', 'operation_token')}

    def submit(self, data=None, files=None):
        data = dict(data if data is not None else self.photo_form())
        data['photos'] = files if files is not None else [self.png('added.png')]
        return self.client.post(self.path_url, data=data, content_type='multipart/form-data')

    def snapshot(self):
        with self.db() as conn:
            return {'item': self.record(self.legacy_id),
                    'events': conn.execute('SELECT COUNT(*) FROM business_item_observations').fetchone()[0],
                    'work': conn.execute('SELECT COUNT(*) FROM business_work_records').fetchone()[0],
                    'ops': conn.execute("SELECT COUNT(*) FROM business_registry_operations WHERE source_key LIKE 'staff-photo:%'").fetchone()[0],
                    'files': set(path.name for path in self.uploads.iterdir())}

    def test_only_photos_are_appended_original_kept_no_work_or_status_invented(self):
        before = self.snapshot()
        response = self.submit()
        self.assertEqual(response.status_code, 302, response.get_data(as_text=True))
        after = self.snapshot()
        old, new = before['item'], after['item']
        self.assertEqual(old['photo_path'], new['photo_path'])
        self.assertTrue((self.static / old['photo_path']).is_file())
        self.assertEqual(len(json.loads(new['additional_photos'])), 1)
        for key in old:
            if key not in ('additional_photos', 'custody_version', 'updated_by', 'updated_at'):
                self.assertEqual(old[key], new[key], key)
        self.assertEqual((before['events'], before['work']), (after['events'], after['work']))
        self.assertEqual(after['ops'], 1)
        usage = self.row('SELECT COUNT(*) AS n,SUM(byte_size) AS bytes FROM stage2_photo_records WHERE merchandise_id=? AND measurement_status=?', (self.legacy_id,'measured'))
        self.assertEqual(usage['n'], 2)
        self.assertGreater(usage['bytes'], 0)
        self.assertIsNone(self.row('SELECT registration_completed_at FROM business_item_profiles WHERE physical_item_id=?', (self.item_id,))['registration_completed_at'])

    def test_same_operation_replays_once_but_different_content_rejected(self):
        data = self.photo_form()
        self.assertEqual(self.submit(data).status_code, 302)
        before = self.snapshot()
        self.assertEqual(self.submit(data).status_code, 302)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.submit(data, [(io.BytesIO(b'not-the-same-payload'),'other.png')]).status_code, 409)
        self.assertEqual(self.snapshot(), before)

    def test_stale_other_screen_does_not_overwrite(self):
        first, second = self.photo_form(), self.photo_form()
        self.assertEqual(self.submit(first).status_code, 302)
        before = self.snapshot()
        self.assertEqual(self.submit(second).status_code, 409)
        self.assertEqual(self.snapshot(), before)

    def test_csrf_wrong_target_and_modified_fields_are_rejected(self):
        before = self.snapshot()
        data = self.photo_form()
        self.assertEqual(self.submit(dict(data, staff_photo_csrf='wrong')).status_code, 400)
        for name in ('user_id','custody_location','purchase_price','photo_path','registration_completed_at'):
            self.assertEqual(self.submit(dict(data, **{name:'tampered'})).status_code, 400)
        token = self.photos.signer.dumps({'actor':'3','item':'wrong-id','snapshot':'n/a'})
        self.assertEqual(self.submit(dict(data, operation_token=token)).status_code, 403)
        self.assertEqual(self.snapshot(), before)

    def test_customers_and_restricted_staff_cannot_manage_photos(self):
        data = self.photo_form()
        for user_id in (1, 2):
            self.login(user_id)
            self.assertEqual(self.client.get(self.path_url).status_code, 403)
            self.assertEqual(self.submit(data).status_code, 403)
        self.login(3)
        with self.db() as conn:
            conn.execute('ALTER TABLE users ADD COLUMN admin_permissions TEXT')
            conn.execute('UPDATE users SET admin_permissions=? WHERE id=3', ('["payments"]',))
        self.assertEqual(self.client.get(self.path_url).status_code, 403)
        self.assertEqual(self.submit(data).status_code, 403)

    def test_status_owner_and_hold_rechecked_before_upload(self):
        data = self.photo_form()
        cases = [('user_id',2),('scope','admin'),('custody_location','self'),('is_shipped',1),('sale_date','2026-10-22')]
        for key, changed in cases:
            old = self.record(self.legacy_id)[key]
            with self.db() as conn:
                conn.execute('UPDATE merchandise SET '+key+'=? WHERE id=?',(changed,self.legacy_id))
            before = self.snapshot()
            self.assertEqual(self.submit(data).status_code, 409, key)
            self.assertEqual(self.snapshot(), before)
            with self.db() as conn:
                conn.execute('UPDATE merchandise SET '+key+'=? WHERE id=?',(old,self.legacy_id))
        with self.db() as conn:
            conn.execute('UPDATE business_item_profiles SET held=1 WHERE physical_item_id=?',(self.item_id,))
        self.assertEqual(self.submit(data).status_code, 409)

    def test_missing_or_ambiguous_link_requires_reconciliation(self):
        with self.db() as conn:
            original = tuple(conn.execute('SELECT * FROM business_item_links WHERE merchandise_id=?',(self.legacy_id,)).fetchone())
            # Link storage is append-only in real operation; fixture removes its
            # local protection solely to exercise a corrupted/missing state.
            triggers = conn.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='business_item_links'").fetchall()
            for trigger in triggers:
                conn.execute('DROP TRIGGER "'+trigger[0]+'"')
            conn.execute('DELETE FROM business_item_links WHERE merchandise_id=?',(self.legacy_id,))
        self.assertEqual(self.client.get(self.path_url).status_code, 409)
        with self.db() as conn:
            conn.execute('INSERT INTO business_item_links VALUES(?,?,?,?)',original)
            conn.execute('INSERT INTO business_item_links VALUES(?,?,?,?)',(100,self.item_id,'unresolved','fixture-duplicate'))
        self.assertEqual(self.client.get(self.path_url).status_code, 409)

    def test_size_count_and_bad_image_leave_no_partial_files_or_rows(self):
        before = self.snapshot()
        data = self.photo_form()
        self.assertEqual(self.submit(data,[self.png(),(io.BytesIO(b'invalid'),'bad.png')]).status_code, 400)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.submit(data,[self.png() for _ in range(20)]).status_code,409)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.submit(data,[(io.BytesIO(b'x'*(1024*1024+1)),'oversized.png')]).status_code,400)
        self.assertEqual(self.snapshot(), before)
        with patch('stage2_staff_photos.MAX_REQUEST_BYTES',10):
            self.assertEqual(self.submit(data).status_code,413)
        self.assertEqual(self.snapshot(), before)

    def test_metadata_failure_rolls_back_upload_and_legacy_edit(self):
        before = self.snapshot()
        with patch('stage2_staff_photos.record_item_photos',side_effect=RuntimeError('synthetic metadata failure')):
            with self.assertLogs(self.app.logger,level='ERROR'):
                response = self.submit()
        self.assertEqual(response.status_code,503)
        self.assertEqual(self.snapshot(),before)

    def test_empty_main_gets_first_photo_without_other_mutation(self):
        with self.db() as conn:
            conn.execute('UPDATE merchandise SET photo_path=NULL WHERE id=?',(self.legacy_id,))
        self.assertEqual(self.submit().status_code,302)
        item = self.record(self.legacy_id)
        self.assertTrue(item['photo_path'].startswith('uploads/staff_'))
        self.assertIsNone(item['additional_photos'])

    def test_flag_off_no_route_use_and_registration_never_opens_database(self):
        app = Flask('staff-photo-registration')
        app.secret_key='fixture-only-secret'
        service = register_stage2_staff_photos(SimpleNamespace(app=app,DATABASE_URL=None,
            get_db=lambda: self.fail('registration opened database')))
        self.assertIs(service,app.extensions['stage2_staff_photos'])
        self.app.config['STAGE2_INVENTORY_ENABLED']=False
        self.assertEqual(self.client.get(self.path_url).status_code,404)


if __name__ == '__main__':
    unittest.main()
