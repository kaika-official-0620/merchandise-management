"""Home choices persist per customer without changing business facts or access."""
from pathlib import Path
import json
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest

from flask import Flask
from flask_login import LoginManager
from jinja2 import ChoiceLoader, DictLoader
from werkzeug.datastructures import MultiDict

from item_registry import Registry, RegistryError
from stage6_common import Stage6Store
from stage6_home_preferences import HomePreferences, DEFAULT_SECTIONS
from stage6_notifications_schema import migration_statements
from stage6_web import register_stage6_web
from test_inventory_registry_web import User, Forms
from test_stage6_home import fixture, CUSTOMER, OTHER, STAFF


ROOT = Path(__file__).resolve().parents[1]


class HomePreferencesTests(unittest.TestCase):
    def setUp(self):
        self.db = fixture()
        self.addCleanup(self.db.close)
        self.service = HomePreferences(Stage6Store(Registry(self.db)))

    def test_read_defaults_is_read_only_and_test_records_are_opt_in(self):
        before = self.db.total_changes
        result = self.service.get(CUSTOMER)
        self.assertEqual(result['selected'], list(DEFAULT_SECTIONS))
        self.assertTrue(result['available'])
        self.assertNotIn('test_records', result['selected'])
        self.assertEqual(self.db.total_changes, before)

    def test_choices_are_customer_owned_and_survive_new_service(self):
        self.service.save(CUSTOMER, ['inventory'], source_key='home-fixture-1')
        self.service.save(OTHER, ['sales'], source_key='home-fixture-2')
        restored = HomePreferences(Stage6Store(Registry(self.db)))
        self.assertEqual(restored.get(CUSTOMER)['selected'], ['inventory'])
        self.assertEqual(restored.get(OTHER)['selected'], ['sales'])

    def test_empty_selection_persists_and_replay_writes_once(self):
        result = self.service.save(CUSTOMER, [], source_key='home-fixture-empty')
        self.assertEqual(self.service.save(CUSTOMER, [], source_key='home-fixture-empty'), result)
        self.assertTrue(self.service.get(CUSTOMER)['all_hidden'])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM stage6_home_preferences').fetchone()[0], 1)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM stage4_operations').fetchone()[0], 1)

    def test_unrecognized_and_duplicate_sections_are_rejected(self):
        for value in (['admin'], ['next_actions'], ['inventory', 'inventory'], [1], None):
            with self.assertRaises(RegistryError) as error:
                self.service.save(CUSTOMER, value, source_key='bad')
            self.assertEqual(error.exception.code, 'home_preferences_invalid')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM stage6_home_preferences').fetchone()[0], 0)

    def test_staff_cannot_overwrite_customer_display_choices(self):
        with self.assertRaises(RegistryError) as error:
            self.service.save(STAFF, [], source_key='staff-attempt')
        self.assertEqual(error.exception.status, 403)

    def test_unapplied_schema_uses_defaults_without_ddl_or_failed_query(self):
        self.db.execute('DROP TABLE stage6_home_preferences')
        before = self.db.total_changes
        result = self.service.get(CUSTOMER)
        self.assertFalse(result['available'])
        self.assertEqual(result['selected'], list(DEFAULT_SECTIONS))
        with self.assertRaises(RegistryError) as error:
            self.service.save(CUSTOMER, [], source_key='unavailable')
        self.assertEqual(error.exception.code, 'home_preferences_not_ready')
        self.assertEqual(self.db.total_changes, before)

    def test_corrupt_saved_choices_fall_back_to_safe_defaults(self):
        for value in ('{bad json', json.dumps(['admin']), 'null', '{}'):
            self.db.execute('INSERT INTO stage6_home_preferences VALUES(?,?,?) ON CONFLICT(customer_id) DO UPDATE SET sections_json=excluded.sections_json', (1, value, 'fixture'))
            self.assertEqual(self.service.get(CUSTOMER)['selected'], list(DEFAULT_SECTIONS))


class HomePreferencesHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='kaika-home-display-')
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'home.sqlite'
        source = fixture()
        for sql in migration_statements():
            source.execute(sql)
        source.commit()
        target = sqlite3.connect(self.path)
        source.backup(target)
        target.close()
        source.close()
        self.app = Flask('home-display-fixture', template_folder=str(ROOT / 'templates'))
        self.app.config.update(TESTING=True, SECRET_KEY='home-display-fixture-only', STAGE2_INVENTORY_ENABLED=True,
            STAGE3_FINANCE_ENABLED=True, STAGE4_COMMERCE_ENABLED=True, STAGE5_PAYMENTS_ENABLED=True, STAGE6_ENABLED=True)
        self.app.jinja_loader = ChoiceLoader([DictLoader({'base.html': '{% block content %}{% endblock %}'}), self.app.jinja_loader])
        self.app.context_processor(lambda: {'customer_navigation_available': True})
        for endpoint in ('self_inventory_new', 'inventory_intake_new', 'index', 'finance_review.index'):
            self.app.add_url_rule('/fixture/' + endpoint, endpoint=endpoint, view_func=lambda: 'fixture')
        manager = LoginManager(self.app)
        manager.user_loader(lambda identifier: User(int(identifier)))
        register_stage6_web(SimpleNamespace(app=self.app, DATABASE_URL=None, get_db=self.connect))
        self.client = self.app.test_client()

    def connect(self):
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        return db

    def login(self, identifier):
        with self.client.session_transaction() as session:
            session.clear()
            session['_user_id'] = str(identifier)
            session['_fresh'] = True

    def form(self):
        response = self.client.get('/workspace-home')
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return Forms(response.get_data(as_text=True)).forms['/workspace-home/display']

    def test_authentication_csrf_and_cross_account_tokens(self):
        self.assertEqual(self.client.post('/workspace-home/display').status_code, 401)
        self.login(1)
        form = self.form()
        self.assertEqual(self.client.post('/workspace-home/display', data=dict(form, registry_csrf='wrong')).status_code, 400)
        self.login(2)
        self.assertEqual(self.client.post('/workspace-home/display', data=form).status_code, 400)
        own_form = self.form()
        self.assertEqual(self.client.post('/workspace-home/display', data=dict(
            own_form, operation_token=form['operation_token'])).status_code, 403)
        self.login(9)
        self.assertEqual(self.client.post('/workspace-home/display', data=form).status_code, 403)

    def test_save_reload_new_session_and_customer_isolation(self):
        self.login(1)
        form = self.form()
        payload = MultiDict(form)
        payload.add('sections', 'inventory')
        payload.add('customer_id', '2')
        self.assertEqual(self.client.post('/workspace-home/display', data=payload).status_code, 302)
        self.login(1)
        html = self.client.get('/workspace-home').get_data(as_text=True)
        self.assertIn('data-home-required="true"', html)
        self.assertIn('data-home-section="inventory"', html)
        self.assertNotIn('data-home-section="money"', html)
        self.login(2)
        html = self.client.get('/workspace-home').get_data(as_text=True)
        self.assertIn('data-home-section="money"', html)

    def test_all_hidden_still_has_required_actions_and_display_controls_and_reset(self):
        self.login(1)
        self.assertEqual(self.client.post('/workspace-home/display', data=self.form()).status_code, 302)
        html = self.client.get('/workspace-home').get_data(as_text=True)
        self.assertIn('data-home-required="true"', html)
        self.assertIn('id="home-display-settings"', html)
        self.assertNotIn('data-home-section=', html)
        self.assertEqual(self.client.post('/workspace-home/display', data=dict(self.form(), reset='default')).status_code, 302)
        html = self.client.get('/workspace-home').get_data(as_text=True)
        self.assertIn('data-home-section="money"', html)

    def test_missing_additive_schema_keeps_home_readable_and_save_disabled(self):
        db = self.connect()
        db.execute('DROP TABLE stage6_home_preferences')
        db.commit()
        db.close()
        self.login(1)
        response = self.client.get('/workspace-home')
        self.assertEqual(response.status_code, 200)
        self.assertIn('disabled', response.get_data(as_text=True))
        self.assertNotIn('表示を保存</button>', response.get_data(as_text=True))
        form = Forms(response.get_data(as_text=True)).forms['/workspace-home/display']
        self.assertEqual(self.client.post('/workspace-home/display', data=form).status_code, 409)

    def test_staff_home_keeps_operations_and_excludes_customer_preferences(self):
        self.login(9)
        response = self.client.get('/workspace-home')
        self.assertEqual(response.status_code, 200)
        self.assertIn('作業一覧', response.get_data(as_text=True))
        self.assertNotIn('home-display-settings', response.get_data(as_text=True))


if __name__ == '__main__':
    unittest.main()
