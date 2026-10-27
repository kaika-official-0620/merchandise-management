"""Read-only local configuration inventory. No app, DB, SDK, socket or secret output.

Presence is not validity, and local absence says nothing about Render/console.
This inspector cannot authorize billing, notification delivery or publication.
"""
import json
from pathlib import Path
from urllib.parse import urlsplit


FIELDS = {
    'digital_web': ('FEATURE_STRIPE_SECRET_KEY', 'FEATURE_STRIPE_WEBHOOK_SECRET',
        'FEATURE_BILLING_ORIGIN', 'FEATURE_STRIPE_PORTAL_CONFIG_ID'),
    'apple': ('FEATURE_APPLE_BUNDLE_ID', 'FEATURE_APPLE_SUBSCRIPTION_GROUP_ID',
        'FEATURE_APPLE_ROOT_CERTIFICATES', 'FEATURE_APPLE_PRIVATE_KEY_PATH',
        'FEATURE_APPLE_KEY_ID', 'FEATURE_APPLE_ISSUER_ID'),
    'google': ('FEATURE_GOOGLE_PACKAGE_NAME', 'FEATURE_GOOGLE_SERVICE_ACCOUNT_PATH',
        'FEATURE_GOOGLE_PUBSUB_AUDIENCE', 'FEATURE_GOOGLE_PUBSUB_EMAIL',
        'FEATURE_GOOGLE_PUBSUB_SUBSCRIPTION'),
    'legacy_push': ('EXPO_ACCESS_TOKEN', 'EAS_PROJECT_ID'),
    'store_documents': ('FEATURE_STORE_TERMS_URL', 'FEATURE_STORE_PRIVACY_URL'),
}
FLAGS = ('FEATURE_PLANS_ENABLED', 'FEATURE_WEB_NEW_PURCHASES_ENABLED',
    'FEATURE_APPLE_BILLING_ENABLED', 'FEATURE_GOOGLE_BILLING_ENABLED',
    'FEATURE_STORE_NEW_PURCHASES_ENABLED', 'PUSH_NOTIFICATIONS_ENABLED',
    'STAGE5_PAYMENTS_ENABLED', 'STAGE6_ENABLED')


def _mode(key):
    if not isinstance(key, str) or not key:
        return 'not_present_locally'
    for prefix, value in (('sk_test_', 'test'), ('rk_test_', 'test'),
                          ('sk_live_', 'production'), ('rk_live_', 'production')):
        if key.startswith(prefix) and len(key) > len(prefix):
            return value
    return 'unrecognized'


def _https(value):
    try:
        parsed = urlsplit(value)
        return bool(parsed.scheme == 'https' and parsed.hostname and not parsed.username and
            not parsed.password and parsed.port in (None, 443) and
            not any(ord(char) < 33 for char in value))
    except (ValueError, TypeError):
        return False


def inspect_integrations(environ, *, file_exists=None):
    """Return allowlisted names/statuses only, including on malformed inputs."""
    if file_exists is None:
        file_exists = lambda value: Path(value).is_file()
    def present(name):
        return isinstance(environ.get(name), str) and bool(environ[name].strip())
    def file_status(name):
        if not present(name):
            return 'not_present_locally'
        try:
            return 'file_exists_not_validated' if file_exists(environ[name]) else 'file_not_found'
        except (ValueError, OSError, TypeError):
            return 'file_check_failed'
    flags = {name: 'enabled' if environ.get(name) == '1' else
        'disabled' if environ.get(name) in ('0', None, '') else 'unrecognized' for name in FLAGS}
    groups = {group: {name: 'present_not_validated' if present(name) else 'not_present_locally'
        for name in fields} for group, fields in FIELDS.items()}
    stripe_mode = _mode(environ.get('FEATURE_STRIPE_SECRET_KEY'))
    store_mode = environ.get('FEATURE_STORE_ENVIRONMENT')
    store_mode = store_mode if store_mode in ('test', 'production') else (
        'not_present_locally' if not store_mode else 'unrecognized')
    products = {'apple': 'not_present_locally', 'google': 'not_present_locally'}
    try:
        raw = json.loads(environ.get('FEATURE_STORE_PRODUCTS', '{}'))
        for provider in products:
            values = raw.get(provider) if isinstance(raw, dict) else None
            if values is None:
                continue
            if not isinstance(values, list) or not values:
                products[provider] = 'malformed_or_empty'
                continue
            codes = [value.get('plan_code') for value in values if isinstance(value, dict)]
            products[provider] = 'legacy_two_plan_mapping_not_store_verified' if (
                len(values) == 2 and sorted(codes) == ['business', 'normal']) else 'unsupported_mapping'
    except (ValueError, TypeError):
        products = dict.fromkeys(products, 'malformed')
    certificates = 'not_present_locally'
    if present('FEATURE_APPLE_ROOT_CERTIFICATES'):
        try:
            roots = json.loads(environ['FEATURE_APPLE_ROOT_CERTIFICATES'])
            certificates = 'files_exist_not_validated' if isinstance(roots, list) and roots and all(
                isinstance(value, str) and bool(value) and file_exists(value) for value in roots) else 'invalid_or_missing_file'
        except (ValueError, TypeError, OSError):
            certificates = 'invalid_or_unreadable_list'
    return {
        'scope': 'current_process_environment_only',
        'external_accounts_and_deployed_settings': 'unknown_not_queried',
        'flags': flags, 'settings': groups,
        'stripe_mode_from_key_prefix': stripe_mode, 'store_environment': store_mode,
        'store_products': products,
        'files': {'FEATURE_APPLE_PRIVATE_KEY_PATH': file_status('FEATURE_APPLE_PRIVATE_KEY_PATH'),
            'FEATURE_APPLE_ROOT_CERTIFICATES': certificates,
            'FEATURE_GOOGLE_SERVICE_ACCOUNT_PATH': file_status('FEATURE_GOOGLE_SERVICE_ACCOUNT_PATH')},
        'documents': {name: 'https_shape_only_not_fetched' if _https(environ.get(name, ''))
            else 'not_present_or_invalid' for name in FIELDS['store_documents']},
        'physical_card_payments': 'memory_test_only_external_writes_disabled',
        'stage6_delivery': {'email': 'unwired_sender_undecided',
            'line': 'unwired_official_account_reported_exists_channel_unknown',
            'push': 'unwired_legacy_registration_bridge_only'},
        'external_test_evidence': {name: 'not_executed' for name in
            ('stripe', 'apple', 'google', 'email', 'line', 'expo_device_delivery')},
        'production_ready': False,
        'possible_live_execution_requires_review': bool(
            (stripe_mode == 'production' and flags['FEATURE_WEB_NEW_PURCHASES_ENABLED'] == 'enabled') or
            (store_mode == 'production' and flags['FEATURE_STORE_NEW_PURCHASES_ENABLED'] == 'enabled') or
            flags['PUSH_NOTIFICATIONS_ENABLED'] == 'enabled'),
        'next_required': ['review_formal_prices_contracts_and_service_scope',
            'wire_physical_outbound_queue_and_verified_webhook_reconciliation',
            'wire_stage6_external_transports_and_account_link_entry',
            'confirm_company_email_and_authorized_test_destinations',
            'verify_store_accounts_products_signed_builds_and_real_devices',
            'perform_external_sandbox_and_delivery_checks_with_evidence'],
    }
