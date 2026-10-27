import json
import unittest

from item_registry import Actor, RegistryError
from stage3_common import FinanceStore, migration_statements, yen
from test_item_registry import fixture


class CommonTests(unittest.TestCase):
    def setUp(self):
        self.conn, registry = fixture()
        self.addCleanup(self.conn.close)
        self.store = FinanceStore(registry)
        for sql in migration_statements():
            self.conn.execute(sql)
        self.staff, self.customer = Actor(9, True), Actor(1)

    def test_only_finance_and_inventory_staff_can_write(self):
        self.assertTrue(self.store.authorize(self.staff, 1, write=True))
        for actor in (Actor(8, True), Actor(7, True), Actor(6, True), Actor(1, True), Actor(1)):
            with self.assertRaises(RegistryError):
                self.store.authorize(actor, 1, write=True)
        self.conn.execute('UPDATE users SET admin_permissions=? WHERE id=8', (json.dumps(['merchandise','invoices']),))
        self.assertTrue(self.store.authorize(Actor(8, True), 1, write=True))

    def test_own_customer_reads_only(self):
        self.assertFalse(self.store.authorize(self.customer, 1))
        with self.assertRaises(RegistryError):
            self.store.authorize(self.customer, 2)

    def test_customer_target_existence_is_not_disclosed(self):
        for customer_id in (2, 9, 99999):
            with self.assertRaises(RegistryError) as result:
                self.store.authorize(self.customer, customer_id)
            self.assertEqual(result.exception.status, 403)
            self.assertEqual(result.exception.code, 'customer_access_denied')

    def test_operation_repeat_and_mismatched_replay(self):
        calls = []
        def change():
            calls.append(True)
            return {'record_id':'fixture'}
        for _ in range(2):
            self.assertEqual(self.store.operation(self.staff, 1, 'event', {'amount':5}, change), {'record_id':'fixture'})
        self.assertEqual(len(calls), 1)
        with self.assertRaises(RegistryError):
            self.store.operation(self.staff, 1, 'event', {'amount':6}, change)
        with self.assertRaises(RegistryError):
            self.store.operation(self.staff, 2, 'event', {'amount':5}, change)

    def test_exception_rolls_back_entire_operation(self):
        def failed():
            self.conn.execute('UPDATE stage3_customer_locks SET lock_version=7 WHERE customer_id=1')
            raise ValueError('fixture')
        with self.assertRaises(ValueError):
            self.store.operation(self.staff, 1, 'failure', {}, failed)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM stage3_operations').fetchone()[0], 0)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM stage3_customer_locks').fetchone()[0], 0)

    def test_operations_append_only(self):
        self.store.operation(self.staff, 1, 'immutable', {}, lambda: {})
        for sql in ('DELETE FROM stage3_operations', "UPDATE stage3_operations SET result_json='{}'"):
            with self.assertRaises(Exception):
                self.conn.execute(sql)

    def test_money_rejects_float_boolean_unknown_and_overflow(self):
        for value in (None, True, 1.0, '1', -1, 9_000_000_000_001):
            with self.assertRaises(RegistryError):
                yen(value)
        self.assertEqual(yen(0), 0)
        self.assertEqual(yen(-5, signed=True), -5)
        self.assertIsNone(yen(None, optional=True))

    def test_missing_schema_is_not_automatically_created(self):
        self.conn.execute('DROP TABLE stage3_schema')
        with self.assertRaises(RegistryError) as result:
            self.store.authorize(self.customer, 1)
        self.assertEqual(result.exception.code, 'stage3_migration_required')


if __name__ == '__main__':
    unittest.main()
