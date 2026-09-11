"""Execute real billing SQL/functions against fictional mixed-custody inventory.

AST extraction avoids importing app.py, its database, scheduler or payment SDK.
"""
import ast
from copy import deepcopy
from datetime import date, datetime
from pathlib import Path
import re
import sqlite3
from types import SimpleNamespace
import unittest

from kaika_fee_counts import is_kaika_fee_client, is_kaika_fee_item

ROOT = Path(__file__).resolve().parents[1]
TREES = {name: ast.parse((ROOT / name).read_text(encoding='utf-8-sig')) for name in (
    'app.py', 'preview_runtime_patches.py', 'kaika_business_flow_patch_20260611.py')}


def functions(filename, name):
    return [node for node in ast.walk(TREES[filename]) if isinstance(node, ast.FunctionDef) and node.name == name]


def load_function(node, environment):
    node = deepcopy(node); node.decorator_list = []
    namespace = dict(environment)
    exec(compile(ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[])), '<isolated-existing-function>', 'exec'), namespace)
    return namespace[node.name]


def queries(filename, name, environment=None):
    environment = environment or {}
    found = []
    for function in functions(filename, name):
        for node in ast.walk(function):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute) or node.func.attr != 'execute' or not node.args:
                continue
            if not isinstance(node.args[0], (ast.Constant, ast.JoinedStr)):
                continue
            try:
                value = eval(compile(ast.Expression(node.args[0]), '<existing-sql>', 'eval'), environment)
            except NameError:
                continue
            if isinstance(value, str) and 'merchandise' in value and re.search(r'COUNT\(', value, re.I):
                found.append(value)
    return found


def normalize(value):
    return datetime.fromisoformat(str(value)).date() if value else None


class KaikaFeeCountsTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:'); self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        names = set(re.findall(r'\bu\.([a-z_]+)', (ROOT / 'app.py').read_text(encoding='utf-8-sig')))
        names |= {'username', 'display_name', 'role', 'subscription_status', 'stripe_subscription_id'}
        names.discard('id')
        self.conn.execute('CREATE TABLE users (id INTEGER PRIMARY KEY,' + ','.join(name + ' TEXT' for name in sorted(names)) + ')')
        for user_id, role, subscription in ((1, 'user', None), (2, 'user', None), (3, 'user', 'existing-contract'), (4, 'admin', None)):
            self.conn.execute('INSERT INTO users(id,username,display_name,role,stripe_subscription_id,subscription_status) VALUES (?,?,?,?,?,?)',
                (user_id, 'Fixture', '架空データ', role, subscription, 'active' if subscription else 'inactive'))
        self.conn.execute('''CREATE TABLE merchandise (id INTEGER PRIMARY KEY, user_id INTEGER,
            custody_location TEXT, custody_received_at TEXT, created_at TEXT, purchase_date TEXT,
            product_name TEXT, purchase_price INTEGER DEFAULT 0, sale_price INTEGER DEFAULT 0,
            shipping_cost INTEGER DEFAULT 0, commission INTEGER DEFAULT 0, sale_date TEXT,
            sales_destination TEXT, sale_type TEXT)''')
        # The incoming item was registered at home in August, received in September.
        self.add(1, 'kaika', '2026-08-01', '2026-09-03')
        self.add(1, 'kaika', '2026-09-02')
        self.add(1, 'kaika', '2026-09-02', '2026-08-03')
        self.add(1, 'kaika', '2026-08-01')
        self.add(1, None, '2026-09-02')  # Legacy NULL retains original treatment.
        for _ in range(120):
            self.add(1, 'self', '2026-09-02')
        self.add(1, 'transit', '2026-09-02')
        self.add(2, 'self', '2026-09-02')
        self.add(4, 'kaika', '2026-09-02'); self.add(4, 'self', '2026-09-02')

    def add(self, user, custody, created, received=None):
        self.conn.execute('INSERT INTO merchandise(user_id,custody_location,created_at,purchase_date,custody_received_at) VALUES (?,?,?,?,?)',
            (user, custody, created, created, received))

    def run_sql(self, sql, user_id=1):
        # Freeze only the database clock; all filters are the actual source SQL.
        sql = sql.replace("'now'", "'2026-09-11'").replace('CURRENT_DATE', "'2026-09-11'").replace('%s', '?')
        return [dict(row) for row in self.conn.execute(sql, (user_id,) * sql.count('?')).fetchall()]

    def test_monthly_queries_exclude_self_transit_and_count_receipt_month(self):
        checked = 0
        for name in ('profile', 'admin_stripe_dashboard', 'admin_stripe_subscribe', 'admin_users'):
            for sql in queries('app.py', name):
                if not ('strftime' in sql or 'DATE_TRUNC' in sql):
                    continue
                with self.subTest(function=name, postgres='DATE_TRUNC' in sql):
                    self.assertIn('custody_location', sql)
                    self.assertIn('custody_received_at', sql)
                    if 'DATE_TRUNC' in sql:
                        continue  # PostgreSQL execution is a separate integration check.
                    rows = self.run_sql(sql)
                    row = next((row for row in rows if row.get('id', 1) == 1), None)
                    self.assertEqual(row.get('item_count', row.get('count')), 3)
                    if 'u.id = ?' in sql or 'user_id = ?' in sql:
                        empty = self.run_sql(sql, 2)[0]
                        self.assertEqual(empty.get('item_count', empty.get('count')), 0)
                    checked += 1
        self.assertEqual(checked, 4)

    def test_all_legacy_batch_entrypoints_keep_total_period_and_filter_custody(self):
        checked = 0
        for name in ('admin_stripe_change_plan', 'admin_stripe_batch_update', 'api_stripe_batch_update', 'run_monthly_batch_update'):
            for sql in queries('app.py', name):
                with self.subTest(function=name):
                    self.assertIn('custody_location', sql)
                    # Existing batches count all periods, not only this month.
                    self.assertNotIn('strftime', sql)
                    self.assertNotIn('DATE_TRUNC', sql)
                    if 'subscription_status' in sql:
                        self.conn.execute("UPDATE users SET stripe_subscription_id='fixture', subscription_status='active' WHERE id=1")
                    rows = self.run_sql(sql)
                    row = next(row for row in rows if row.get('id', 1) == 1)
                    self.assertEqual(row['item_count'], 5)
                    checked += 1
        self.assertEqual(checked, 8)

    def test_active_runtime_patch_and_monthly_settlement_default_use_only_kaika(self):
        sqls = queries('preview_runtime_patches.py', 'fetch_client_rows', {'placeholder': '?'})
        monthly = [sql for sql in sqls if 'strftime' in sql]
        self.assertEqual(len(monthly), 1)
        self.assertEqual(self.run_sql(monthly[0])[0]['count'], 3)
        tariff = load_function(functions('app.py', 'get_monthly_fee')[0], {'get_fee_settings': lambda: {}})
        self.assertEqual(tariff(0), 2980)  # Do not silently remove the legacy minimum.
        seen_counts = []
        def fee(count):
            seen_counts.append(count); return tariff(count)
        function = load_function(functions('kaika_business_flow_patch_20260611.py', 'default_monthly_fee_for_user')[0], {
            'get_monthly_fee': fee, 'safe_int': int, 'placeholder': lambda: '?',
            'fetch_scalar': lambda sql, args: self.conn.execute(sql, args).fetchone()[0]})
        self.assertEqual(function(1), 2980); self.assertEqual(seen_counts, [5])
        self.assertEqual(function(2), 0)  # No Kaika custody, no existing monthly agreement.
        self.assertEqual(function(3), 2980)  # Existing zero-item agreement retains the tariff.
        self.assertEqual(seen_counts, [5, 0])

    def test_revenue_projection_does_not_invent_legacy_fee_for_self_only_user(self):
        items = [dict(row) for row in self.conn.execute('SELECT * FROM merchandise WHERE user_id IN (1,2)')]
        users = [dict(row) for row in self.conn.execute('SELECT * FROM users WHERE role=\'user\'')]
        common = {'get_jst_now': lambda: datetime(2026, 9, 11), 'normalize_item_date': normalize,
                  'is_kaika_fee_item': is_kaika_fee_item, 'is_kaika_fee_client': is_kaika_fee_client}
        for node in functions('app.py', 'build_monthly_subscription_summary'):
            counts = []
            def fee(count):
                counts.append(count); return 2980
            function = load_function(node, dict(common, get_monthly_fee=fee))
            result = function(users, items)
            self.assertEqual(counts, [3, 0])  # User 2 has self stock only; user 3 retains an existing zero-item contract.
            self.assertEqual(result['current_month_fee_revenue'], 5960)
            self.assertEqual(result['client_count'], 2)

    def test_user_analytics_payment_query_excludes_personal_costs(self):
        self.conn.execute("UPDATE merchandise SET sale_date='2026-09-05', shipping_cost=100, commission=200 WHERE user_id=1")
        selected = [sql for sql in queries('app.py', 'user_analytics', {
            'user_condition': 'user_id = ?', 'sale_date_filter': " AND sale_date >= '2026-09-01' AND sale_date <= '2026-09-30'"})
                    if 'AS kaika_fee_total' in sql]
        self.assertEqual(len(selected), 1)
        result = self.run_sql(selected[0])[0]
        self.assertEqual(result['item_count'], 5)
        self.assertEqual(result['kaika_fee_total'], 1500)
        # Personal records/costs are still intact in their own analytics source.
        self.assertGreater(self.conn.execute('SELECT SUM(shipping_cost+commission) FROM merchandise WHERE user_id=1').fetchone()[0], 1500)

    def test_active_company_analytics_keeps_personal_sales_but_excludes_them_from_kaika_revenue(self):
        self.conn.execute("UPDATE merchandise SET sale_date='2026-09-05', sale_price=1000, commission=200 WHERE user_id IN (1,2)")
        counts = []
        def fee(count):
            counts.append(count); return 2980
        function = load_function(functions('preview_runtime_patches.py', 'build_company_sales_analytics_context')[0], {
            'datetime': SimpleNamespace(now=lambda: datetime(2026, 9, 11)), 'date': date,
            'get_fee_settings': lambda: {}, 'get_monthly_fee': fee,
            'open_cursor': lambda: (SimpleNamespace(close=lambda: None), self.conn.cursor()),
            'rows_to_dicts': lambda rows: [dict(row) for row in rows],
            'normalize_item_date': normalize, 'to_int_local': lambda value: int(value or 0),
            'is_kaika_fee_item': is_kaika_fee_item, 'is_kaika_fee_client': is_kaika_fee_client,
            'classify_company_scope': lambda item, roles: 'user' if roles.get(item.get('user_id')) == 'user' else 'kaika',
            'build_user_fee_components': lambda item, settings: {'kaika_revenue_total': item['commission']},
            'clean_display_text': lambda value, fallback='': value or fallback,
            'format_destination_label': lambda item: '',
        })
        result, recent = function()
        self.assertEqual(counts, [3, 0])
        self.assertEqual(result['support']['service_fee_revenue'], 1000)
        self.assertEqual(result['support']['subscription_revenue'], 5960)
        self.assertEqual(result['client']['sold_count'], 127)
        self.assertEqual(len(recent), 10)
        self.assertFalse(is_kaika_fee_item({'custody_location': 'unknown'}))
        self.assertFalse(is_kaika_fee_item({'custody_location': ''}))


if __name__ == '__main__':
    unittest.main()
