"""Startup requirements and permanently retired financial/custody operations.

No database, provider, scheduler, or credentials are accessed on import. Retired
operations have no environment switch: any replacement needs a reviewed flow.
"""
import hashlib
import os

from flask import jsonify, request
from werkzeug.exceptions import ServiceUnavailable


class StartupConfigurationError(RuntimeError):
    """Configuration failure whose text never contains a secret value."""


# Filled with the digest of the removed public fallback, never with a password.
_RETIRED_SESSION_KEY_DIGESTS = frozenset({'6c95fe23688bc0434d838fc5ff10e8f5e93ad894bba23904c43eac6c7185660b'})


def require_session_secret(environ=None):
    env = os.environ if environ is None else environ
    value = env.get('SECRET_KEY')
    if (not isinstance(value, str) or len(value) < 32 or value != value.strip()
            or len(set(value)) < 8
            or hashlib.sha256(value.encode('utf-8')).hexdigest() in _RETIRED_SESSION_KEY_DIGESTS):
        raise StartupConfigurationError(
            'SECRET_KEY must be explicitly configured with a unique, randomly generated value of at least 32 characters.')
    return value


class LegacyOperationDisabled(ServiceUnavailable):
    description = 'この旧操作は停止しています。顧客の資産・契約・請求は変更していません。'


def legacy_operation_disabled(operation):
    """Fail before any DB/provider work, including direct or stale job calls."""
    raise LegacyOperationDisabled()


LEGACY_BILLING_PATHS = frozenset({
    '/stripe/webhook', '/api/stripe/batch-update',
    '/admin/stripe/batch-update', '/admin/stripe/success',
})
LEGACY_BILLING_PREFIXES = (
    '/admin/stripe/subscribe/', '/admin/stripe/change-plan/',
    '/admin/stripe/cancel-subscription/',
)


def install_runtime_security(app):
    """Deny retired HTTP paths independently of later view-function patches."""
    if app.extensions.get('kaika_runtime_security'):
        return
    app.extensions['kaika_runtime_security'] = True

    @app.before_request
    def retired_billing_boundary():
        path = request.path.rstrip('/')
        if path in LEGACY_BILLING_PATHS or path.startswith(LEGACY_BILLING_PREFIXES):
            return jsonify(error={'code': 'legacy_operation_disabled',
                                  'message': LegacyOperationDisabled.description}), 503
