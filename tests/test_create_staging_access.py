"""Issuance is explicit, fixed-host, and safe on configuration/provider failure."""
import io
import json
import os
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
from contextlib import redirect_stderr, redirect_stdout
from urllib.parse import urlsplit, parse_qs

from scripts import create_staging_access as command


class StagingAccessCommandTests(unittest.TestCase):
    def run_command(self, args, *, host=command.HOST, enabled='1', failure=False):
        config = SimpleNamespace(host=host, database_url='postgresql://fixture:fixture@db.invalid/kaika_staging')
        connection = MagicMock()
        driver = SimpleNamespace(connect=MagicMock(return_value=connection))
        output = io.StringIO()
        with patch.dict(os.environ, {'KAIKA_STAGING_ACCESS_ENABLED': enabled}, clear=True), \
                patch.object(command, 'validate_environment', return_value=config), \
                patch.object(command, 'mint_staging_access', side_effect=RuntimeError('sensitive diagnostics') if failure else None,
                             return_value='a'*43) as mint, \
                patch.dict('sys.modules', {'psycopg2': driver}), redirect_stdout(output):
            result = command.main(args)
        return result, output.getvalue(), mint, driver, connection

    def test_no_implicit_issue_or_connection(self):
        for args in ([], ['--ttl-seconds', '86400', '--max-uses', '5']):
            with self.subTest(args=args):
                result, text, mint, driver, _ = self.run_command(args)
                self.assertEqual(result, 0)
                self.assertFalse(json.loads(text)['issued'])
                mint.assert_not_called()
                driver.connect.assert_not_called()

    def test_explicit_issue_matches_browser_fragment_contract_and_tls(self):
        result, text, mint, driver, connection = self.run_command(['--issue'])
        self.assertEqual(result, 0)
        link = text.splitlines()[0].split('=', 1)[1]
        parsed = urlsplit(link)
        self.assertEqual((parsed.scheme, parsed.hostname, parsed.path, parsed.query),
                         ('https', command.HOST, '/staging/access', ''))
        self.assertEqual(parse_qs(parsed.fragment), {'token': ['a'*43]})
        mint.assert_called_once()
        self.assertEqual(mint.call_args.kwargs['ttl_seconds'], 900)
        self.assertEqual(mint.call_args.kwargs['max_uses'], 1)
        self.assertIn('EXPIRES_IN_SECONDS=900;', text)
        self.assertEqual(driver.connect.call_args.kwargs['sslmode'], 'require')
        connection.close.assert_called_once()

    def test_explicit_one_day_link_keeps_single_use(self):
        result, text, mint, _, _ = self.run_command(['--issue', '--ttl-seconds', '86400'])
        self.assertEqual(result, 0)
        mint.assert_called_once()
        self.assertEqual(mint.call_args.kwargs['ttl_seconds'], 86400)
        self.assertEqual(mint.call_args.kwargs['max_uses'], 1)
        self.assertIn('EXPIRES_IN_SECONDS=86400; ACCOUNT=staging_business; SINGLE_USE=true; MAX_USES=1', text)

    def test_explicit_one_day_link_with_multiple_logins(self):
        result, text, mint, _, _ = self.run_command(['--issue', '--ttl-seconds', '86400', '--max-uses', '5'])
        self.assertEqual(result, 0)
        mint.assert_called_once()
        self.assertEqual(mint.call_args.kwargs['ttl_seconds'], 86400)
        self.assertEqual(mint.call_args.kwargs['max_uses'], 5)
        self.assertIn('EXPIRES_IN_SECONDS=86400; ACCOUNT=staging_business; SINGLE_USE=false; MAX_USES=5', text)

    def test_invalid_max_uses_is_rejected_without_issuance(self):
        for value in ('0', '6', '1.5'):
            with self.subTest(value=value), redirect_stderr(io.StringIO()), \
                    patch.object(command, 'validate_environment') as validate, \
                    self.assertRaises(SystemExit) as error:
                command.main(['--issue', '--max-uses', value])
            self.assertEqual(error.exception.code, 2)
            validate.assert_not_called()

    def test_invalid_lifetime_is_rejected_without_issuance(self):
        for value in ('0', '86401', 'one day'):
            with self.subTest(value=value), redirect_stderr(io.StringIO()), \
                    patch.object(command, 'validate_environment') as validate, \
                    self.assertRaises(SystemExit) as error:
                command.main(['--issue', '--ttl-seconds', value])
            self.assertEqual(error.exception.code, 2)
            validate.assert_not_called()

    def test_disabled_or_different_host_refuses_before_issuance(self):
        for options in ({'host': 'stock.kaika-potential.co.jp'}, {'enabled': '0'}):
            with self.subTest(options=options):
                result, text, mint, driver, _ = self.run_command(['--issue', '--max-uses', '5'], **options)
                self.assertEqual(result, 1)
                self.assertFalse(json.loads(text)['issued'])
                mint.assert_not_called()
                driver.connect.assert_not_called()

    def test_provider_failure_does_not_echo_diagnostics_or_credentials(self):
        result, text, _, _, connection = self.run_command(['--issue'], failure=True)
        self.assertEqual(result, 1)
        self.assertNotIn('sensitive', text)
        self.assertNotIn('postgres', text)
        self.assertNotIn('STAGING_ACCESS_LINK', text)
        connection.close.assert_called_once()


if __name__ == '__main__':
    unittest.main()
