"""Read-only preflight for the legacy fixed-column backup importers.

The old restore format cannot rebuild custody/workflow/account bindings. Reject
before it extracts images or writes any row; do not silently drop new data.
"""
import json
import zipfile

UNSUPPORTED = ('旧形式の復元は、自己保管・配送中の商品、開花の受付・受領履歴、'
               'アプリの契約・通知情報の完全復元に対応していません。'
               '現在のデータを守るため、このインポートは実行していません。'
               '保存したファイルは保管し、復元方法を管理者へご相談ください。')
UNREADABLE = 'バックアップまたは現在のデータを安全に確認できないため、復元を実行していません。ファイルと復元方法を管理者へご確認ください。'

PROTECTED_TABLES = (
    'self_inventory_items', 'inventory_intakes', 'inventory_intake_items', 'inventory_custody_events',
    'feature_billing_accounts', 'feature_subscriptions', 'feature_manual_grants',
    'feature_store_accounts', 'feature_store_receipts', 'feature_store_replacements',
    'feature_store_refunds', 'feature_plan_changes', 'push_devices', 'push_outbox',
)


def unsupported_payload(data):
    if not isinstance(data, dict) or not isinstance(data.get('merchandise', []), list):
        return True
    for item in data.get('merchandise', []):
        if not isinstance(item, dict):
            return True
        if item.get('custody_location') not in (None, '', 'kaika') or item.get('custody_received_at') not in (None, ''):
            return True
    return any(value for key, value in data.items() if key in PROTECTED_TABLES or
               key.startswith(('inventory_intake', 'inventory_custody', 'self_inventory_', 'feature_', 'push_')))


def current_data_requires_new_restore(get_db, postgres=False, user_id=None):
    conn = get_db()
    cur = conn.cursor()
    mark = '%s' if postgres else '?'
    try:
        if postgres:
            cur.execute('SELECT table_name FROM information_schema.tables WHERE table_schema=current_schema()')
        else:
            cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
        names = {next(iter(row.values())) if isinstance(row, dict) else row[0] for row in cur.fetchall()}

        def columns(table):
            if postgres:
                cur.execute('SELECT column_name FROM information_schema.columns WHERE table_schema=current_schema() AND table_name=%s', (table,))
                return {next(iter(row.values())) if isinstance(row, dict) else row[0] for row in cur.fetchall()}
            cur.execute('PRAGMA table_info(' + table + ')')
            return {row['name'] if hasattr(row, 'keys') else row[1] for row in cur.fetchall()}

        if 'merchandise' in names:
            fields = columns('merchandise')
            conditions = []
            if 'custody_location' in fields:
                conditions.append("COALESCE(custody_location, 'kaika') NOT IN ('kaika', '')")
            if 'custody_received_at' in fields:
                conditions.append('custody_received_at IS NOT NULL')
            if conditions:
                scope = '' if user_id is None else ' AND user_id=' + mark
                cur.execute('SELECT 1 FROM merchandise WHERE (' + ' OR '.join(conditions) + ')' + scope + ' LIMIT 1',
                            () if user_id is None else (int(user_id),))
                if cur.fetchone():
                    return True
        for table in PROTECTED_TABLES:
            if table not in names:
                continue
            scope, args = '', ()
            if user_id is not None:
                fields = columns(table)
                if 'user_id' in fields:
                    scope, args = ' WHERE user_id=' + mark, (int(user_id),)
                elif table == 'inventory_intake_items' and 'inventory_intakes' in names:
                    scope, args = ' WHERE intake_id IN (SELECT id FROM inventory_intakes WHERE user_id=' + mark + ')', (int(user_id),)
                elif 'subscription_id' in fields and 'feature_subscriptions' in names:
                    scope, args = ' WHERE subscription_id IN (SELECT subscription_id FROM feature_subscriptions WHERE user_id=' + mark + ')', (int(user_id),)
                # Unknown association is checked conservatively, never ignored.
            cur.execute('SELECT 1 FROM ' + table + scope + ' LIMIT 1', args)
            if cur.fetchone():
                return True
        return False
    finally:
        cur.close()
        conn.close()


def legacy_restore_block_reason(get_db, postgres, uploaded_file, user_id=None):
    if uploaded_file is None or not uploaded_file.filename:
        return None  # Keep the original missing-file message.
    stream = uploaded_file.stream
    position = stream.tell()
    try:
        if current_data_requires_new_restore(get_db, postgres, user_id):
            return UNSUPPORTED
        stream.seek(0)
        if uploaded_file.filename.lower().endswith('.zip'):
            with zipfile.ZipFile(stream) as archive:
                with archive.open('backup_data.json') as source:
                    payload = json.load(source)
        elif uploaded_file.filename.lower().endswith('.json'):
            payload = json.load(stream)
        else:
            return None  # Original handler rejects unsupported extensions.
        return UNSUPPORTED if unsupported_payload(payload) else None
    except Exception:
        return UNREADABLE
    finally:
        stream.seek(position)
