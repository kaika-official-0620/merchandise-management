"""Explicitly issue one temporary link for the fixed fictional business tester.

Run only inside the existing Render staging service. Without --issue the
command prints non-sensitive readiness only; no link is created. --issue
prints a bearer link intended for the requesting reviewer, never a password.
The URL fragment is not sent in an HTTP request or ordinary server logs.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from staging_access import DEFAULT_TTL_SECONDS, MAX_TTL_SECONDS, mint_staging_access
from staging_environment import validate_environment


HOST = 'kaika-platform-staging.onrender.com'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--issue', action='store_true', help='Issue exactly one one-use link for the requester.')
    parser.add_argument('--ttl-seconds', type=int, default=DEFAULT_TTL_SECONDS,
                        help=f'Link lifetime in seconds (1 to {MAX_TTL_SECONDS}, default: %(default)s).')
    args = parser.parse_args(argv)
    if not 1 <= args.ttl_seconds <= MAX_TTL_SECONDS:
        parser.error(f'--ttl-seconds must be between 1 and {MAX_TTL_SECONDS}.')
    try:
        config = validate_environment()
        if config.host != HOST or os.environ.get('KAIKA_STAGING_ACCESS_ENABLED') != '1':
            raise ValueError('Dedicated staging access must be enabled.')
        if not args.issue:
            print(json.dumps({'staging_access_configured': True, 'issued': False}))
            return 0
        import psycopg2
        from urllib.parse import parse_qsl, urlsplit
        query = dict(parse_qsl(urlsplit(config.database_url).query))
        tls = {} if 'sslmode' in query else {'sslmode': 'require'}
        with closing(psycopg2.connect(config.database_url, connect_timeout=15, **tls)) as connection:
            token = mint_staging_access(config, connection=connection, ttl_seconds=args.ttl_seconds)
        print('STAGING_ACCESS_LINK=https://' + HOST + '/staging/access#token=' + token)
        print(f'EXPIRES_IN_SECONDS={args.ttl_seconds}; ACCOUNT=staging_business; SINGLE_USE=true')
        return 0
    except Exception:
        # Neither database diagnostics nor the token are included in failures.
        print(json.dumps({'issued': False, 'error': 'staging_access_not_ready'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
