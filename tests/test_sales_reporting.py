import sqlite3
import unittest

from sales_reporting import ordinary_merchandise_source


class OrdinarySalesReportingTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE merchandise(id INTEGER PRIMARY KEY, user_id INTEGER,
                purchase_price INTEGER, sale_date TEXT, sale_price INTEGER,
                shipping_cost INTEGER, is_shipped INTEGER);
            INSERT INTO merchandise VALUES (1, 2, 3500, '2026-11-22', 8500, 100, 1);
        """)

    def add_provenance(self):
        self.db.executescript("""
            CREATE TABLE business_item_links(merchandise_id INTEGER, physical_item_id TEXT);
            CREATE TABLE business_item_observations(observation_id TEXT, kind TEXT, source_key TEXT,
                physical_item_id TEXT, customer_id INTEGER, occurred_at TEXT, recorded_at TEXT);
            CREATE TABLE stage4_channel_orders(order_id TEXT, physical_item_id TEXT,
                customer_id INTEGER, initial_status TEXT, mode TEXT);
            INSERT INTO business_item_links VALUES (1, 'wallet');
            INSERT INTO stage4_channel_orders VALUES ('test-1', 'wallet', 2, 'accepted', 'test_only');
        """)

    def event(self, event_id, kind, source_key, recorded_at, customer_id=2):
        self.db.execute(
            "INSERT INTO business_item_observations VALUES (?, ?, ?, 'wallet', ?, '2026-11-22', ?)",
            (event_id, kind, source_key, customer_id, recorded_at))

    def projected(self, *, keep_stock_state=False):
        source = ordinary_merchandise_source(self.db, False, keep_stock_state=keep_stock_state)
        return dict(self.db.execute(f'SELECT * FROM {source} AS m WHERE id=1').fetchone())

    def test_older_installation_preserves_sales(self):
        self.assertEqual(self.projected()['sale_price'], 8500)

    def test_active_test_sale_is_hidden_without_changing_stock_or_history(self):
        self.add_provenance()
        self.event('s1', 'sold', 'channel-order:test-1', '01')
        self.event('s2', 'shipped', 'shipping', '02')
        row = self.projected()
        self.assertEqual(row['purchase_price'], 3500)
        self.assertTrue(row['reporting_sale_hidden'])
        for field in ('sale_price', 'sale_date', 'shipping_cost', 'is_shipped'):
            self.assertIsNone(row[field])
        self.assertEqual(self.projected(keep_stock_state=True)['sale_price'], 8500)
        self.assertEqual(self.db.execute('SELECT sale_price FROM merchandise').fetchone()[0], 8500)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM business_item_observations').fetchone()[0], 2)

    def test_later_self_sale_of_same_item_at_same_price_is_visible(self):
        self.add_provenance()
        self.event('s1', 'sold', 'channel-order:test-1', '01')
        self.event('r1', 'returned', 'return', '02')
        self.event('s2', 'sold', 'customer-self-sale-report:1', '03')
        self.assertEqual(self.projected()['sale_price'], 8500)
        self.assertFalse(self.projected()['reporting_sale_hidden'])
        self.db.execute("INSERT INTO stage4_channel_orders VALUES ('test-2', 'wallet', 2, 'accepted', 'test_only')")
        self.event('s3', 'sold', 'channel-order:test-2', '04')
        self.assertTrue(self.projected()['reporting_sale_hidden'])

    def test_order_must_match_active_item_owner_and_be_accepted(self):
        self.add_provenance()
        self.event('s1', 'sold', 'channel-order:test-1', '01', customer_id=3)
        self.assertEqual(self.projected()['sale_price'], 8500)
        self.event('s2', 'sold', 'channel-order:test-1', '02')
        self.db.execute("UPDATE stage4_channel_orders SET customer_id=3")
        self.assertFalse(self.projected()['reporting_sale_hidden'])
        self.db.execute("UPDATE stage4_channel_orders SET customer_id=2, initial_status='conflict'")
        self.assertFalse(self.projected()['reporting_sale_hidden'])
        self.db.execute("UPDATE stage4_channel_orders SET initial_status='accepted', mode='manual'")
        self.assertFalse(self.projected()['reporting_sale_hidden'])
