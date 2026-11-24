"""Public App Store support and privacy pages. Registration does not publish them."""
from datetime import date
import os
import re

from flask import Blueprint, abort, render_template, request


PUBLIC_PATHS = frozenset({'/support', '/privacy'})
SUPPORT_EMAIL = 'info@kaika-potential.co.jp'
SETTING_KEYS = (
    'APP_PUBLIC_PAGES_MODE',
    'APP_PUBLIC_SUPPORT_EMAIL',
    'APP_PRIVACY_EFFECTIVE_DATE',
    'APP_PRIVACY_RETENTION',
    'APP_PRIVACY_SERVICE_PROVIDERS',
)


def page_details(config):
    mode = config.get('APP_PUBLIC_PAGES_MODE')
    if mode not in {'preview', 'published'}:
        return None

    email, effective_date, retention, providers = (
        config.get(key, '').strip() for key in SETTING_KEYS[1:]
    )
    public_email = re.fullmatch(r'[A-Za-z0-9._+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+', email) and not email.lower().endswith(
        ('.invalid', '.example', '.test', '@example.com', '@example.net', '@example.org'))
    if mode == 'preview':
        return {'preview': True, 'support_email': email if public_email else '', 'privacy_available': True}
    if not public_email:
        return None
    try:
        effective_date = date.fromisoformat(effective_date).isoformat()
    except ValueError:
        effective_date = None
    return {'preview': False, 'support_email': email, 'effective_date': effective_date,
            'retention': retention, 'providers': providers,
            'privacy_available': bool(effective_date and retention and providers)}


def register_public_release_pages(app):
    for key in SETTING_KEYS:
        default = SUPPORT_EMAIL if key == 'APP_PUBLIC_SUPPORT_EMAIL' else ''
        app.config.setdefault(key, os.environ.get(key, default))

    bp = Blueprint('public_release_pages', __name__)

    def render_page(page):
        details = page_details(app.config)
        if details is None or (page == 'privacy' and not details['privacy_available']):
            abort(404)
        return render_template(f'public_release/{page}.html', **details)

    @bp.get('/support')
    def support():
        return render_page('support')

    @bp.get('/privacy')
    def privacy():
        return render_page('privacy')

    @bp.after_request
    def public_page_headers(response):
        response.headers['Cache-Control'] = 'no-store'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Content-Security-Policy'] = "default-src 'none'; style-src 'self'; img-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'"
        if app.config.get('APP_PUBLIC_PAGES_MODE') != 'published':
            response.headers['X-Robots-Tag'] = 'noindex, nofollow'
        return response

    @app.context_processor
    def public_release_page_context():
        if request.endpoint not in {'login', 'customer_navigation.guide', 'account_lifecycle.information'}:
            return {}
        details = page_details(app.config)
        return {'public_support_available': details is not None,
                'public_privacy_available': bool(details and details['privacy_available'])}

    app.register_blueprint(bp)
