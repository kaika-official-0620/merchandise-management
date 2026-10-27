"""Explicit hosted-staging schema bootstrap, never used by production startup.

Only additive schema objects and their version markers are created. No customer,
stock, custody, price, work, payment, entitlement or notification rows are seeded.
Existing functions/triggers are verified, never dropped or replaced.
"""
import os
from pathlib import Path
import re

from staging_environment import StagingConfigurationError, check_upload_disk, validate_environment


REVIEW_FLAGS = ('STAGE2_INVENTORY_ENABLED', 'STAGE3_FINANCE_ENABLED',
                'STAGE4_COMMERCE_ENABLED', 'STAGE5_PAYMENTS_ENABLED',
                'STAGE6_ENABLED', 'STAGE7_ENABLED')
DISABLED_REVIEW_FLAGS = (
    'STAGE3_TEST_LEDGER_ENABLED', 'STAGE9_PAYMENTS_ENABLED',
    'STAGE9_STRIPE_NETWORK_ENABLED', 'STAGE9_STRIPE_WRITES_ENABLED',
    'ACCOUNT_LIFECYCLE_ENABLED', 'STAGE6_TEST_DELIVERY_ENABLED',
    'STAGE6_EXPO_TEST_ENABLED', 'STAGE6_LINE_TEST_ENABLED',
    'STAGE6_SMTP_TEST_ENABLED', 'STAGE6_LINE_LINK_ENABLED',
    'PUSH_NOTIFICATIONS_ENABLED', 'FEATURE_APPLE_BILLING_ENABLED',
    'FEATURE_GOOGLE_BILLING_ENABLED', 'FEATURE_STORE_NEW_PURCHASES_ENABLED',
)
_IDENTIFIER = r'[a-z][a-z0-9_]*'


def schema_statements():
    """Return the existing PostgreSQL definitions, without fixture executors."""
    from item_registry_schema import migration_statements as registry
    from inventory_reconciliation import import_schema_statements
    from stage2_self_management import PHOTO_RECORDS_DDL, SELF_INVENTORY_DDL, custody_ddl
    from stage3_common import migration_statements as finance
    from stage3_profit_schema import migration_statements as profit
    from stage3_billing_schema import migration_statements as billing
    from stage3_funds_schema import migration_statements as funds
    from stage4_common import migration_statements as commerce
    from stage4_catalog_schema import migration_statements as catalog
    from stage4_listings_schema import migration_statements as listings
    from stage4_channels_schema import migration_statements as channels
    from stage5_common import migration_statements as payments
    from stage5_payments_schema import migration_statements as payment_records
    from stage6_common import migration_statements as home
    from stage6_notifications_schema import migration_statements as notifications
    from stage6_actions_schema import migration_statements as actions
    from stage7_common import migration_statements as pilot
    from stage7_measurement_schema import migration_statements as measurement
    from stage7_economics_schema import migration_statements as economics
    statements = list(registry('postgres')) + list(import_schema_statements())
    statements += list(SELF_INVENTORY_DDL) + list(custody_ddl('postgres')) + [PHOTO_RECORDS_DDL]
    for provider in (finance, profit, billing, funds, commerce, catalog, listings,
                     channels, payments, payment_records, home, notifications,
                     actions, pilot, measurement, economics):
        statements += list(provider('postgres'))
    return tuple(statements)


def _row_values(row, names):
    return tuple(row[name] for name in names) if hasattr(row, 'keys') else tuple(row)


def _normalized(value):
    return re.sub(r'\s+', ' ', value or '').strip()


def _column_names(statement):
    """Extract names from our fixed CREATE TABLE definitions, not user SQL."""
    body = statement[statement.index('(') + 1:statement.rfind(')')]
    parts, start, depth, quoted = [], 0, 0, False
    index = 0
    while index < len(body):
        char = body[index]
        if char == "'":
            if quoted and index + 1 < len(body) and body[index + 1] == "'":
                index += 2
                continue
            quoted = not quoted
        elif not quoted:
            if char == '(':
                depth += 1
            elif char == ')':
                depth -= 1
            elif char == ',' and depth == 0:
                parts.append(body[start:index])
                start = index + 1
        index += 1
    parts.append(body[start:])
    names = set()
    for part in parts:
        match = re.match(r'\s*(' + _IDENTIFIER + r')\b', part, re.I)
        if match and match.group(1).upper() not in {'PRIMARY', 'FOREIGN', 'CHECK', 'UNIQUE', 'CONSTRAINT'}:
            names.add(match.group(1))
    return names


def _check_columns(cursor, table, required):
    cursor.execute("SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name=%s", (table,))
    actual = {_row_values(row, ('column_name',))[0] for row in cursor.fetchall()}
    if not required <= actual:
        raise StagingConfigurationError('Staging review requires an explicit schema review for table ' + table + '.')


def _function(cursor, statement, name):
    body = re.search(r'\$\$(.*?)\$\$', statement, re.S)
    if not body or not re.search(r'RETURNS\s+trigger\b', statement, re.I) or not re.search(r'LANGUAGE\s+plpgsql\b', statement, re.I):
        raise StagingConfigurationError('Unrecognized staging review function definition.')
    cursor.execute("""SELECT p.prosrc, l.lanname,
        p.prorettype='pg_catalog.trigger'::regtype AS returns_trigger,
        p.prosecdef, p.proconfig
        FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
        JOIN pg_language l ON l.oid=p.prolang
        WHERE n.nspname='public' AND p.proname=%s AND p.pronargs=0""", (name,))
    rows = cursor.fetchall()
    if rows:
        if len(rows) != 1:
            raise StagingConfigurationError('Ambiguous staging review function ' + name + '.')
        source, language, returns_trigger, privileged, settings = _row_values(
            rows[0], ('prosrc', 'lanname', 'returns_trigger', 'prosecdef', 'proconfig'))
        if (_normalized(source) != _normalized(body.group(1)) or language != 'plpgsql'
                or not returns_trigger or privileged or settings):
            raise StagingConfigurationError('Existing staging review function differs: ' + name + '.')
        return
    cursor.execute(re.sub(r'^CREATE OR REPLACE FUNCTION', 'CREATE FUNCTION', statement, count=1, flags=re.I))


def _trigger(cursor, statement, name):
    match = re.fullmatch(r'CREATE TRIGGER ' + re.escape(name) + r' BEFORE (UPDATE OR DELETE|INSERT) ON (' + _IDENTIFIER +
                         r') FOR EACH ROW EXECUTE FUNCTION (' + _IDENTIFIER + r')\(\)', _normalized(statement), re.I)
    if not match:
        raise StagingConfigurationError('Unrecognized staging review trigger definition.')
    event, table, function = match.groups()
    cursor.execute("""SELECT t.tgtype, t.tgenabled, t.tgnargs, t.tgqual,
        t.tgattr::text AS attributes, t.tgconstraint, p.proname, fn.nspname AS function_schema
        FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid
        JOIN pg_namespace n ON n.oid=c.relnamespace
        JOIN pg_proc p ON p.oid=t.tgfoid JOIN pg_namespace fn ON fn.oid=p.pronamespace
        WHERE n.nspname='public' AND c.relname=%s AND t.tgname=%s AND NOT t.tgisinternal""", (table, name))
    rows = cursor.fetchall()
    if rows:
        expected = (7 if event.upper() == 'INSERT' else 27, 'O', 0, None, '', 0, function, 'public')
        if len(rows) != 1 or _row_values(rows[0], ('tgtype', 'tgenabled', 'tgnargs', 'tgqual',
                                                 'attributes', 'tgconstraint', 'proname', 'function_schema')) != expected:
            raise StagingConfigurationError('Existing staging review trigger differs: ' + name + '.')
        return
    cursor.execute(statement)


def apply_additive_schema(cursor):
    """Apply known schema definitions; the caller owns transaction and scope."""
    statements = schema_statements()
    for statement in statements:
        statement = statement.strip()
        match = re.match(r'^CREATE TABLE IF NOT EXISTS (' + _IDENTIFIER + r')\b', statement, re.I)
        if match:
            cursor.execute(statement)
            _check_columns(cursor, match.group(1), _column_names(statement))
            continue
        if re.match(r'^CREATE (?:UNIQUE )?INDEX IF NOT EXISTS ' + _IDENTIFIER + r'\b', statement, re.I):
            cursor.execute(statement)
            continue
        match = re.match(r'^CREATE OR REPLACE FUNCTION (' + _IDENTIFIER + r')\(\)', statement, re.I)
        if match:
            _function(cursor, statement, match.group(1))
            continue
        if re.fullmatch(r'DROP TRIGGER IF EXISTS ' + _IDENTIFIER + r' ON ' + _IDENTIFIER, statement, re.I):
            continue  # The following CREATE verifies or adds the guard; no DROP is executed.
        match = re.match(r'^CREATE TRIGGER (' + _IDENTIFIER + r')\b', statement, re.I)
        if match:
            _trigger(cursor, statement, match.group(1))
            continue
        if re.fullmatch(r"INSERT INTO (?:business_registry_schema|stage[3-7]_schema)\(version\) VALUES \('[a-z0-9-]+'\) ON CONFLICT\(version\) DO NOTHING", statement, re.I):
            cursor.execute(statement)
            continue
        raise StagingConfigurationError('Staging review refuses an unrecognized or non-additive schema statement.')
    return len(statements)


def bootstrap_staging_review(runtime, config, *, connection):
    """After staging boundary/seed/volume checks, prepare the opted-in review UI.

    The connection must be the PostgreSQL session held by staging_bootstrap.
    Ordinary render_app startup does not import or call this function.
    """
    app = runtime.app
    app.config.update({name: False for name in REVIEW_FLAGS + DISABLED_REVIEW_FLAGS})
    app.config['STAGE2_FREE_LIMIT_PROPOSAL_ENABLED'] = False
    value = os.environ.get('KAIKA_STAGING_REVIEW_ENABLED', '0')
    if value not in ('0', '1'):
        raise StagingConfigurationError('KAIKA_STAGING_REVIEW_ENABLED must be 0 or 1.')
    if value == '0':
        return {'enabled': False}
    checked = validate_environment()
    if (checked != config or getattr(runtime, 'DATABASE_URL', None) != config.database_url
            or app.extensions.get('kaika_staging') is not True):
        raise StagingConfigurationError('Staging review requires the validated dedicated staging runtime.')
    check_upload_disk(runtime)
    volume = Path(app.config['UPLOAD_FOLDER']).resolve()
    if (volume / '.kaika-staging-volume').read_text(encoding='utf-8').strip() != 'staging':
        raise StagingConfigurationError('Staging review requires the verified staging volume.')
    cursor = connection.cursor()
    try:
        cursor.execute('SELECT current_database()')
        if _row_values(cursor.fetchone(), ('current_database',))[0] != config.database_name:
            raise StagingConfigurationError('Staging review database identity does not match.')
        cursor.execute("SELECT name FROM public.kaika_environment")
        if [_row_values(row, ('name',))[0] for row in cursor.fetchall()] != ['staging']:
            raise StagingConfigurationError('Staging review requires the existing staging database marker.')
        cursor.execute("SELECT tablename FROM pg_tables WHERE schemaname='public' AND tablename='kaika_recovery_quarantine'")
        if cursor.fetchone():
            raise StagingConfigurationError('A quarantined database cannot enable staging review.')
        cursor.execute('SELECT pg_advisory_xact_lock(574920270)')
        cursor.execute('SET LOCAL search_path=public,pg_catalog')
        _check_columns(cursor, 'merchandise', {'id', 'user_id', 'custody_location', 'custody_version', 'custody_received_at'})
        count = apply_additive_schema(cursor)
        connection.commit()
    except StagingConfigurationError:
        connection.rollback()
        raise
    except Exception as error:
        connection.rollback()
        # PostgreSQL diagnostics can include row values. Keep this public startup
        # error limited to the exception class, never DSNs, rows or statements.
        raise StagingConfigurationError('Staging review schema preparation failed (' + type(error).__name__ + ').') from None
    finally:
        cursor.close()
    app.config.update({name: True for name in REVIEW_FLAGS})
    app.config['STAGE2_FREE_LIMIT_PROPOSAL_ENABLED'] = True
    app.extensions['kaika_staging_review'] = {'schema_only': True, 'legacy_reconciliation': 'manual'}
    return {'enabled': True, 'schema_statements': count, 'legacy_reconciliation': 'manual'}
