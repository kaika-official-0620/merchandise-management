from pathlib import Path
from types import SimpleNamespace
import unittest

from flask import Flask, render_template_string
from flask_login import LoginManager

from public_release_pages import SETTING_KEYS, register_public_release_pages
from staging_environment import register_staging_boundary


class PublicReleasePagesTest(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[1]
        self.app = Flask(__name__, template_folder=str(root / 'templates'))
        self.app.config.update({key: '' for key in SETTING_KEYS})
        self.app.config['SECRET_KEY'] = 'test fixture for this Flask app only'
        LoginManager(self.app).user_loader(lambda _: None)
        self.app.add_url_rule('/login', 'login', lambda: render_template_string(
            '{{ public_support_available|default(false) }}'))
        self.app.add_url_rule('/account-deletion-info', 'account_lifecycle.information', lambda: 'deletion')
        self.app.add_url_rule('/member-only', 'member_only', lambda: 'private')
        register_public_release_pages(self.app)
        self.client = self.app.test_client()

    def test_default_and_incomplete_publication_leave_pages_unavailable(self):
        self.assertEqual(self.client.get('/support').status_code, 404)
        self.assertEqual(self.client.get('/login').text, 'False')
        self.app.config.update(APP_PUBLIC_PAGES_MODE='published', APP_PUBLIC_SUPPORT_EMAIL='help@kaika-fixture.com')
        self.assertEqual(self.client.get('/privacy').status_code, 404)
        support = self.client.get('/support')
        self.assertEqual(support.status_code, 200)
        self.assertNotIn('href="/privacy"', support.text)

    def test_staging_allows_anonymous_preview_pages_without_opening_member_pages(self):
        self.app.config.update(APP_PUBLIC_PAGES_MODE='preview', APP_PUBLIC_SUPPORT_EMAIL='help@kaika-fixture.com')
        register_staging_boundary(SimpleNamespace(app=self.app))
        support = self.client.get('/support')
        self.assertEqual(support.status_code, 200)
        self.assertEqual(self.client.head('/support').status_code, 200)
        self.assertIn('noindex', support.headers['X-Robots-Tag'])
        self.assertIn('公開前の確認用', support.text)
        self.assertIn('help@kaika-fixture.com', support.text)
        self.assertNotIn('mailto:', support.text)
        self.assertIn('href="/account-deletion-info"', self.client.get('/privacy').text)
        self.assertEqual(self.client.get('/member-only').status_code, 302)

    def test_published_pages_render_configured_contact_and_escape_policy_text(self):
        self.app.config.update(APP_PUBLIC_PAGES_MODE='published', APP_PUBLIC_SUPPORT_EMAIL='help@kaika-fixture.com',
                               APP_PRIVACY_EFFECTIVE_DATE='2026-11-25', APP_PRIVACY_RETENTION='保管期間＜検証条件＞',
                               APP_PRIVACY_SERVICE_PROVIDERS='<script>検証</script>')
        support = self.client.get('/support')
        privacy = self.client.get('/privacy')
        self.assertEqual((support.status_code, privacy.status_code), (200, 200))
        self.assertIn('mailto:help@kaika-fixture.com', support.text)
        self.assertIn('2026-11-25', privacy.text)
        self.assertIn('&lt;script&gt;検証&lt;/script&gt;', privacy.text)
        self.assertNotIn('公開前の確認用', privacy.text)
        self.assertEqual(self.client.get('/login').text, 'True')
        self.app.config['APP_PUBLIC_SUPPORT_EMAIL'] = 'hello@example.org'
        self.assertEqual(self.client.get('/support').status_code, 404)
