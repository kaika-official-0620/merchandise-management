import json
import unittest
from unittest.mock import patch

from stage8_integrations_readiness import inspect_integrations


class ReadinessTests(unittest.TestCase):
    def test_empty_environment_does_not_claim_accounts_absent_or_ready(self):
        result = inspect_integrations({})
        self.assertEqual(result['external_accounts_and_deployed_settings'], 'unknown_not_queried')
        self.assertFalse(result['production_ready'])
        self.assertFalse(result['possible_live_execution_requires_review'])
        self.assertTrue(all(value == 'not_executed' for value in result['external_test_evidence'].values()))

    def test_no_network_sdk_database_or_env_loading(self):
        with patch('socket.socket', side_effect=AssertionError('network forbidden')), \
             patch('sqlite3.connect', side_effect=AssertionError('database forbidden')), \
             patch('pathlib.Path.read_text', side_effect=AssertionError('secret read forbidden')):
            self.assertFalse(inspect_integrations({})['production_ready'])

    def test_only_allowlisted_statuses_never_leak_secrets_addresses_ids_paths_or_urls(self):
        marker = 'PRIVATE_DO_NOT_OUTPUT'
        values = {name: marker for name in ('FEATURE_STRIPE_SECRET_KEY','FEATURE_STRIPE_WEBHOOK_SECRET',
            'FEATURE_APPLE_PRIVATE_KEY_PATH','FEATURE_GOOGLE_SERVICE_ACCOUNT_PATH','FEATURE_GOOGLE_PUBSUB_EMAIL',
            'FEATURE_APPLE_KEY_ID','FEATURE_APPLE_BUNDLE_ID','EXPO_ACCESS_TOKEN','EAS_PROJECT_ID','UNRELATED_SECRET')}
        values.update(FEATURE_STRIPE_SECRET_KEY='sk_live_' + marker,
            FEATURE_STORE_TERMS_URL='https://'+marker+'.invalid/terms',
            FEATURE_APPLE_ROOT_CERTIFICATES=json.dumps([marker]),
            FEATURE_STORE_PRODUCTS=json.dumps({'apple':[{'plan_code':'normal','product_id':marker},
                                                        {'plan_code':'business','product_id':marker}]}))
        result = inspect_integrations(values, file_exists=lambda _: True)
        self.assertNotIn(marker,json.dumps(result))
        self.assertNotIn('UNRELATED_SECRET',json.dumps(result))
        self.assertFalse(result['production_ready'])

    def test_flag_is_only_exact_one_and_never_enable_any_setting(self):
        env={'FEATURE_WEB_NEW_PURCHASES_ENABLED':'true','PUSH_NOTIFICATIONS_ENABLED':'1'}
        before=dict(env); result=inspect_integrations(env)
        self.assertEqual(result['flags']['FEATURE_WEB_NEW_PURCHASES_ENABLED'],'unrecognized')
        self.assertTrue(result['possible_live_execution_requires_review'])
        self.assertEqual(env,before)

    def test_production_write_flag_is_flagged_but_sandbox_never_called(self):
        for key, expected in [('sk_live_fixture',True),('rk_live_fixture',True),('sk_test_fixture',False),
                              ('rk_test_fixture',False),('sk_live_',False),('opaque',False)]:
            with self.subTest(key=key):
                result=inspect_integrations({'FEATURE_STRIPE_SECRET_KEY':key,'FEATURE_WEB_NEW_PURCHASES_ENABLED':'1'})
                self.assertEqual(result['possible_live_execution_requires_review'],expected)
                self.assertFalse(result['production_ready'])

    def test_store_production_flag_and_unknown_mode(self):
        for mode in ('production','test','bad'):
            result=inspect_integrations({'FEATURE_STORE_ENVIRONMENT':mode,'FEATURE_STORE_NEW_PURCHASES_ENABLED':'1'})
            self.assertEqual(result['possible_live_execution_requires_review'],mode=='production')
            self.assertEqual(result['store_environment'],mode if mode!='bad' else 'unrecognized')

    def test_malformed_products_do_not_leak_values_or_crash(self):
        for raw in ('secret-missing-brace', '[]', 'null','{"apple":42}', '{"apple":[{"plan_code":null}]}'):
            with self.subTest(raw=raw):
                result=inspect_integrations({'FEATURE_STORE_PRODUCTS':raw})
                self.assertFalse(result['production_ready'])
                self.assertNotIn('secret-missing-brace',json.dumps(result))

    def test_legacy_two_product_mapping_is_not_formal_four_plan_approval(self):
        raw=json.dumps({'google':[{'plan_code':'normal'},{'plan_code':'business'}]})
        result=inspect_integrations({'FEATURE_STORE_PRODUCTS':raw})
        self.assertEqual(result['store_products']['google'],'legacy_two_plan_mapping_not_store_verified')
        self.assertFalse(result['production_ready'])

    def test_only_file_presence_is_checked_and_errors_do_not_leak_path(self):
        def fail(_): raise OSError('PRIVATE_ERROR_PATH')
        result=inspect_integrations({'FEATURE_APPLE_PRIVATE_KEY_PATH':'private/path',
            'FEATURE_APPLE_ROOT_CERTIFICATES':'["private/path"]'},file_exists=fail)
        self.assertEqual(result['files']['FEATURE_APPLE_PRIVATE_KEY_PATH'],'file_check_failed')
        self.assertNotIn('private/path',json.dumps(result))
        self.assertNotIn('PRIVATE_ERROR_PATH',json.dumps(result))

    def test_invalid_certificate_list_and_document_urls_stay_unverified(self):
        for raw in ('secret', '{}','[]','[null]'):
            result=inspect_integrations({'FEATURE_APPLE_ROOT_CERTIFICATES':raw,
                'FEATURE_STORE_TERMS_URL':'http://localhost/terms',
                'FEATURE_STORE_PRIVACY_URL':'https://user:secret@example.invalid/privacy'})
            self.assertNotEqual(result['files']['FEATURE_APPLE_ROOT_CERTIFICATES'],'files_exist_not_validated')
            self.assertTrue(all(value=='not_present_or_invalid' for value in result['documents'].values()))


if __name__ == '__main__': unittest.main()
