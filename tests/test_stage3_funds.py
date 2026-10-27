"""Isolated ledger tests: no application import, network, or real database."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest

from item_registry import Actor, Registry, RegistryError
from item_registry_schema import migrate_registry
from stage3_common import FinanceStore, migration_statements as common_statements
from stage3_funds import FundsService
from stage3_funds_schema import MAX_YEN, migration_statements


TIME = '2026-10-22T01:00:00+00:00'
LATER = '2026-10-22T02:00:00+00:00'
STAFF = Actor(9, True)


def fixture(path=':memory:'):
    db = sqlite3.connect(path, timeout=5)
    db.execute('PRAGMA foreign_keys=ON')
    db.execute('CREATE TABLE users(id INTEGER PRIMARY KEY,role TEXT,admin_permissions TEXT)')
    db.execute('CREATE TABLE merchandise(id INTEGER PRIMARY KEY,user_id INTEGER,product_name TEXT)')
    db.executemany('INSERT INTO users VALUES (?,?,?)', [(1, 'user', None), (2, 'user', None), (9, 'owner', None),
        (8, 'admin', '["merchandise"]'), (7, 'admin', '["merchandise","invoices"]'), (6, 'admin', '["invoices"]')])
    migrate_registry(db, allowed_test_path=path if path != ':memory:' else None)
    for sql in (*common_statements(), *migration_statements()):
        db.execute(sql)
    registry = Registry(db)
    item = registry.create_item(STAFF, '架空商品', owner_user_id=1, custodian='self', occurred_at=TIME,
        source_key='fixture-item', evidence_reference='fixture')
    item2 = registry.create_item(STAFF, '架空商品2', owner_user_id=1, custodian='self', occurred_at=TIME,
        source_key='fixture-item2', evidence_reference='fixture')
    db.commit()
    return db, FundsService(FinanceStore(registry)), item['physical_item_id'], item2['physical_item_id']


class FundsTests(unittest.TestCase):
    def setUp(self):
        self.db, self.service, self.item, self.item2 = fixture()
        self.addCleanup(self.db.close)
        self.sequence = 0

    def post(self, kind, amount=1000, *, customer=1, actor=STAFF, origin=None, key=None, **kwargs):
        self.sequence += 1
        result = self.service.post(actor, customer, kind=kind, amount_yen=amount, occurred_at=LATER,
            source_key=key or 'fixture-' + str(self.sequence), evidence_reference='fixture-evidence',
            original_record_id=origin['record_id'] if origin else None, **kwargs)
        return result['record']

    def balance(self, bucket, customer=1, mode='test'):
        return self.service.summary(Actor(customer), customer, mode)['balances_yen'][bucket]

    def assertCode(self, expected, call):
        with self.assertRaises(RegistryError) as raised:
            call()
        self.assertEqual(expected, raised.exception.code)

    def test_empty_balance_is_only_test_record_balance(self):
        result = self.service.summary(Actor(1), 1)
        self.assertEqual(result['record_count'], 0)
        self.assertFalse(result['provider_verified'])
        self.assertFalse(result['live_execution_enabled'])
        self.assertFalse(result['sales_to_purchase_credit_enabled'])

    def test_deposit_reserve_partial_purchase_and_release(self):
        self.post('deposit', 10000)
        held = self.post('reserve', 7000, physical_item_id=self.item)
        self.assertEqual(self.balance('purchase_available'), 3000)
        self.assertEqual(self.balance('purchase_reserved'), 7000)
        self.post('purchase', 6000, origin=held)
        self.post('release', 1000, origin=held)
        self.assertEqual(self.balance('purchase_available'), 4000)
        self.assertEqual(self.balance('purchase_reserved'), 0)

    def test_insufficient_funds_rolls_back_record_and_idempotency(self):
        self.post('deposit', 500)
        before = self.db.execute('SELECT COUNT(*) FROM stage3_operations').fetchone()[0]
        self.assertCode('fund_insufficient_purchase_available', lambda: self.post('reserve', 501, physical_item_id=self.item))
        self.assertEqual(before, self.db.execute('SELECT COUNT(*) FROM stage3_operations').fetchone()[0])
        self.assertEqual(self.balance('purchase_available'), 500)
        self.assertEqual(len(self.service.history(Actor(1), 1)), 1)

    def test_reserved_funds_cannot_be_refunded_as_unused(self):
        deposit = self.post('deposit', 1000)
        self.post('reserve', 800, physical_item_id=self.item)
        self.assertCode('fund_insufficient_purchase_available', lambda: self.post('refund', 201, origin=deposit))
        self.post('refund', 200, origin=deposit)
        self.assertEqual(self.balance('purchase_available'), 0)

    def test_deposit_refund_cannot_exceed_its_origin_even_with_other_funds(self):
        deposit = self.post('deposit', 1000)
        self.post('deposit', 5000)
        self.post('refund', 600, origin=deposit)
        self.assertCode('fund_origin_amount_exceeded', lambda: self.post('refund', 401, origin=deposit))
        self.post('refund', 400, origin=deposit)
        self.assertEqual(self.balance('purchase_available'), 5000)

    def test_release_and_purchase_share_same_reservation_cap(self):
        self.post('deposit', 2000)
        held = self.post('reserve', 1000, physical_item_id=self.item)
        self.post('release', 600, origin=held)
        self.assertCode('fund_origin_amount_exceeded', lambda: self.post('purchase', 401, origin=held))
        self.post('purchase', 400, origin=held)

    def test_purchase_refund_restores_available_not_reservation(self):
        self.post('deposit', 1000)
        reservation = self.post('reserve', 1000, physical_item_id=self.item)
        purchase = self.post('purchase', 1000, origin=reservation)
        self.post('refund', 1000, origin=purchase)
        self.assertEqual(self.balance('purchase_available'), 1000)
        self.assertEqual(self.balance('purchase_reserved'), 0)
        self.assertCode('fund_origin_amount_exceeded', lambda: self.post('purchase', 1, origin=reservation))
        self.post('reserve', 1000, physical_item_id=self.item)

    def test_same_item_cannot_be_allocated_twice_even_other_customer(self):
        self.post('deposit', 1000)
        self.post('deposit', 1000, customer=2)
        self.post('reserve', 500, physical_item_id=self.item)
        self.assertCode('fund_item_already_reserved', lambda: self.post('reserve', 500, physical_item_id=self.item))
        self.assertCode('fund_item_already_reserved', lambda: self.post('reserve', 500, customer=2, physical_item_id=self.item))

    def test_completed_purchase_still_claims_item_until_full_refund(self):
        self.post('deposit', 2000)
        reserved = self.post('reserve', 1000, physical_item_id=self.item)
        bought = self.post('purchase', 1000, origin=reserved)
        self.post('refund', 999, origin=bought)
        self.assertCode('fund_item_already_reserved', lambda: self.post('reserve', 1, physical_item_id=self.item))
        self.post('refund', 1, origin=bought)
        self.post('reserve', 1, physical_item_id=self.item)

    def test_complete_release_reopens_same_item(self):
        self.post('deposit', 1000)
        held = self.post('reserve', 1000, physical_item_id=self.item)
        self.post('release', 1000, origin=held)
        self.post('reserve', 1000, physical_item_id=self.item)

    def test_modes_are_separate_and_live_mode_rejected(self):
        self.post('deposit', 1000, mode='record_only')
        self.assertEqual(self.balance('purchase_available'), 0)
        self.assertEqual(self.balance('purchase_available', mode='record_only'), 1000)
        self.assertCode('fund_insufficient_purchase_available', lambda: self.post('reserve', 1, physical_item_id=self.item))
        self.assertCode('funds_mode_invalid', lambda: self.post('deposit', 1000, mode='live'))

    def test_same_item_test_claim_does_not_consume_record_only_funds(self):
        for mode in ('test', 'record_only'):
            self.post('deposit', 1000, mode=mode)
            self.post('reserve', 1000, mode=mode, physical_item_id=self.item)
        self.assertEqual(self.balance('purchase_reserved'), 1000)
        self.assertEqual(self.balance('purchase_reserved', mode='record_only'), 1000)

    def test_invoice_payment_and_repayment_after_payment_correction(self):
        invoice = self.post('invoice', 1155)
        paid = self.post('payment', 1155, origin=invoice)
        self.assertEqual(self.balance('fees_payable'), 0)
        self.assertCode('fund_origin_amount_exceeded', lambda: self.post('payment', 1, origin=invoice))
        self.post('correction', 1155, origin=paid)
        self.assertEqual(self.balance('fees_payable'), 1155)
        self.post('payment', 1155, origin=invoice)
        self.assertEqual(self.balance('fees_payable'), 0)

    def test_invoice_refunded_payment_reopens_only_returned_amount(self):
        invoice = self.post('invoice', 1000)
        payment = self.post('payment', 1000, origin=invoice)
        self.post('refund', 400, origin=payment)
        self.assertEqual(self.balance('fees_payable'), 400)
        self.assertCode('fund_origin_amount_exceeded', lambda: self.post('payment', 401, origin=invoice))
        self.post('payment', 400, origin=invoice)

    def test_sales_do_not_credit_purchase_funds(self):
        sale = self.post('sales_record', 10000, physical_item_id=self.item)
        receipt = self.post('sales_receipt', 10000, origin=sale)
        hold = self.post('sales_hold', 2000, origin=receipt)
        ready = self.post('sales_ready', 8000, origin=receipt)
        self.post('sales_settlement', 8000, origin=ready)
        self.assertEqual(self.balance('purchase_available'), 0)
        self.assertEqual(self.balance('sales_unpaid'), 0)
        self.assertEqual(self.balance('sales_unsettled'), 0)
        self.assertEqual(self.balance('sales_return_hold'), 2000)
        self.assertEqual(self.balance('sales_settlement_ready'), 0)
        released = self.post('sales_release', 2000, origin=hold)
        self.assertEqual(self.balance('sales_unsettled'), 2000)
        self.assertEqual(self.balance('sales_settlement_ready'), 0)
        self.post('sales_ready', 2000, origin=released)

    def test_unpaid_or_return_hold_cannot_settle(self):
        sale = self.post('sales_record', 1000)
        self.assertCode('fund_origin_kind_invalid', lambda: self.post('sales_settlement', 1000, origin=sale))
        receipt = self.post('sales_receipt', 1000, origin=sale)
        hold = self.post('sales_hold', 1000, origin=receipt)
        self.assertCode('fund_origin_kind_invalid', lambda: self.post('sales_settlement', 1, origin=hold))
        self.post('refund', 1000, origin=hold)
        self.assertEqual(self.balance('sales_return_hold'), 0)
        self.assertEqual(self.balance('purchase_available'), 0)

    def test_settlement_ready_can_be_held_but_not_twice(self):
        sale = self.post('sales_record', 1000)
        receipt = self.post('sales_receipt', 1000, origin=sale)
        ready = self.post('sales_ready', 1000, origin=receipt)
        self.post('sales_settlement', 600, origin=ready)
        self.post('sales_hold', 400, origin=ready)
        self.assertCode('fund_origin_amount_exceeded', lambda: self.post('sales_hold', 1, origin=ready))

    def test_receipt_correction_allows_reentering_receipt(self):
        sale = self.post('sales_record', 1000)
        receipt = self.post('sales_receipt', 1000, origin=sale)
        self.post('correction', 1000, origin=receipt)
        self.assertEqual(self.balance('sales_unpaid'), 1000)
        self.post('sales_receipt', 1000, origin=sale)

    def test_correction_is_terminal_full_reversal_with_trace(self):
        deposit = self.post('deposit', 1000)
        reversed_entry = self.post('correction', 1000, origin=deposit)
        self.assertEqual(reversed_entry['original_record_id'], deposit['record_id'])
        self.assertEqual(self.balance('purchase_available'), 0)
        self.assertEqual(self.service.get(Actor(1), deposit['record_id'])['amount_yen'], 1000)
        self.assertCode('fund_origin_reversed', lambda: self.post('refund', 1, origin=deposit))
        self.assertCode('fund_origin_reversed', lambda: self.post('correction', 1000, origin=deposit))

    def test_partial_and_downstream_corrections_rejected(self):
        deposit = self.post('deposit', 1000)
        self.assertCode('fund_correction_requires_unconsumed_full_origin', lambda: self.post('correction', 999, origin=deposit))
        self.post('refund', 1, origin=deposit)
        self.assertCode('fund_correction_requires_unconsumed_full_origin', lambda: self.post('correction', 1000, origin=deposit))

    def test_correction_cannot_create_negative_available(self):
        deposit = self.post('deposit', 1000)
        self.post('reserve', 1000, physical_item_id=self.item)
        self.assertCode('fund_insufficient_purchase_available', lambda: self.post('correction', 1000, origin=deposit))

    def test_idempotent_same_request_and_mismatched_payload(self):
        first = self.post('deposit', 1000, key='once')
        second = self.post('deposit', 1000, key='once')
        self.assertEqual(first, second)
        self.assertEqual(self.balance('purchase_available'), 1000)
        self.assertCode('source_key_conflict', lambda: self.post('deposit', 1001, key='once'))
        self.assertCode('source_key_conflict', lambda: self.post('deposit', 1000, key='once', customer=2))

    def test_same_source_different_staff_not_accepted_as_new_event(self):
        self.post('deposit', 1000, key='single-source')
        self.assertCode('source_key_conflict', lambda: self.post('deposit', 1000, key='single-source', actor=Actor(7, True)))

    def test_origin_customer_mode_and_item_enforced(self):
        deposit = self.post('deposit', 1000)
        self.assertCode('fund_origin_scope_mismatch', lambda: self.post('refund', 1, origin=deposit, customer=2))
        self.assertCode('fund_origin_scope_mismatch', lambda: self.post('refund', 1, origin=deposit, mode='record_only'))
        held = self.post('reserve', 1000, physical_item_id=self.item)
        self.assertCode('fund_origin_item_mismatch', lambda: self.post('purchase', 1, origin=held, physical_item_id=self.item2))

    def test_sales_item_owner_must_match_customer(self):
        self.assertCode('fund_sale_customer_mismatch', lambda: self.post('sales_record', 1000, customer=2, physical_item_id=self.item))

    def test_customer_read_isolation_and_no_customer_mutations(self):
        record = self.post('deposit', 1000)
        self.assertEqual(self.service.get(Actor(1), record['record_id'])['customer_id'], 1)
        self.assertCode('customer_access_denied', lambda: self.service.get(Actor(2), record['record_id']))
        self.assertCode('customer_access_denied', lambda: self.service.history(Actor(2), 1))
        self.assertCode('customer_access_denied', lambda: self.service.summary(Actor(2), 1))
        self.assertCode('finance_permission_required', lambda: self.post('deposit', actor=Actor(1)))

    def test_staff_requires_inventory_and_invoice_rights(self):
        self.post('deposit', actor=Actor(7, True))
        with self.assertRaises(RegistryError):
            self.post('deposit', actor=Actor(8, True))
        with self.assertRaises(RegistryError):
            self.post('deposit', actor=Actor(6, True))
        with self.assertRaises(RegistryError):
            self.post('deposit', actor=Actor(1, True))
        with self.assertRaises(RegistryError):
            self.service.summary(Actor(8, False), 1)
        with self.assertRaises(RegistryError):
            self.service.summary(Actor(6, False), 1)

    def test_missing_origin_and_unexpected_origin_rejected(self):
        self.assertCode('fund_origin_required_or_unexpected', lambda: self.post('payment', 1))
        deposit = self.post('deposit', 1000)
        self.assertCode('fund_origin_required_or_unexpected', lambda: self.post('deposit', 1, origin=deposit))
        self.assertCode('fund_reservation_item_required', lambda: self.post('reserve', 1))

    def test_purchase_records_do_not_change_item_or_generate_charges(self):
        before = list(self.db.execute('SELECT * FROM business_item_assignments'))
        self.post('deposit', 1000)
        held = self.post('reserve', 1000, physical_item_id=self.item)
        self.post('purchase', 1000, origin=held)
        self.assertEqual(before, list(self.db.execute('SELECT * FROM business_item_assignments')))
        self.assertEqual(self.balance('fees_payable'), 0)

    def test_migration_missing_fails_closed(self):
        self.db.execute('DELETE FROM stage3_schema')
        self.assertCode('stage3_migration_required', lambda: self.post('deposit', 1000))

    def test_unknown_float_bool_negative_zero_overflow_rejected(self):
        for value in (None, 1.0, True, '1000', -1, 0, MAX_YEN + 1):
            with self.subTest(value=value):
                self.assertCode('positive_integer_yen_required', lambda: self.post('deposit', value))
        self.post('deposit', MAX_YEN)
        self.assertCode('fund_balance_limit_exceeded', lambda: self.post('deposit', 1))

    def test_missing_and_naive_datetime_rejected(self):
        self.assertCode('timezone_required', lambda: self.service.post(STAFF, 1, kind='deposit', amount_yen=1000,
            occurred_at='2026-10-22', source_key='naive', evidence_reference='fixture'))
        self.assertCode('invalid_evidence_reference', lambda: self.service.post(STAFF, 1, kind='deposit', amount_yen=1000,
            occurred_at=TIME, source_key='missing-evidence', evidence_reference=''))

    def test_backdated_child_rejected(self):
        deposit = self.post('deposit', 1000)
        self.assertCode('fund_event_precedes_origin', lambda: self.service.post(STAFF, 1, kind='refund', amount_yen=1,
            occurred_at=TIME, source_key='before-origin', evidence_reference='fixture', original_record_id=deposit['record_id']))

    def test_database_rows_append_only_and_non_live(self):
        record = self.post('deposit', 1000)
        for statement in ('UPDATE stage3_fund_records SET amount_yen=1001 WHERE record_id=?',
                          'DELETE FROM stage3_fund_records WHERE record_id=?'):
            with self.assertRaises(sqlite3.IntegrityError):
                self.db.execute(statement, (record['record_id'],))
        self.assertFalse(record['provider_verified'])
        self.assertFalse(record['live_execution_enabled'])
        self.assertEqual(self.balance('purchase_available'), 1000)

    def test_database_rejects_wrong_delta_and_live_record_shape(self):
        original = self.post('deposit', 1000)
        cursor = self.db.execute('SELECT * FROM stage3_fund_records WHERE record_id=?', (original['record_id'],))
        row = dict(zip([column[0] for column in cursor.description], cursor.fetchone()))
        for index, changes in enumerate(({'delta_purchase_available': 2000}, {'live_execution_enabled': 1},
                                        {'provider_verified': 1}, {'execution_mode': 'live'})):
            invalid = dict(row, record_id='invalid-' + str(index), source_key='invalid-' + str(index), **changes)
            with self.subTest(changes=changes), self.assertRaises(sqlite3.IntegrityError):
                self.db.execute('INSERT INTO stage3_fund_records(' + ','.join(invalid) + ') VALUES (' +
                                ','.join('?' for _ in invalid) + ')', tuple(invalid.values()))

    def test_database_origin_foreign_key_is_customer_and_mode_scoped(self):
        original = self.post('deposit', 1000)
        refund = self.post('refund', 10, origin=original)
        cursor = self.db.execute('SELECT * FROM stage3_fund_records WHERE record_id=?', (refund['record_id'],))
        row = dict(zip([column[0] for column in cursor.description], cursor.fetchone()))
        for index, changes in enumerate(({'customer_id': 2}, {'execution_mode': 'record_only'})):
            invalid = dict(row, record_id='cross-scope-' + str(index), source_key='cross-scope-' + str(index), **changes)
            with self.subTest(changes=changes), self.assertRaises(sqlite3.IntegrityError):
                self.db.execute('INSERT INTO stage3_fund_records(' + ','.join(invalid) + ') VALUES (' +
                                ','.join('?' for _ in invalid) + ')', tuple(invalid.values()))

    def test_postgres_review_uses_bigint_no_sqlite_types(self):
        sql = '\n'.join(migration_statements('postgres'))
        self.assertIn('amount_yen BIGINT', sql)
        self.assertNotIn('typeof(', sql)
        self.assertNotIn('RAISE(ABORT', sql)
        self.assertIn('FOREIGN KEY(original_record_id,customer_id,execution_mode)', sql)

    def test_record_only_does_not_change_test_or_legacy_tables(self):
        before = list(self.db.execute('SELECT * FROM merchandise'))
        self.post('deposit', 1000, mode='record_only')
        self.post('invoice', 500, mode='record_only')
        self.assertEqual(before, list(self.db.execute('SELECT * FROM merchandise')))
        self.assertEqual(self.service.summary(Actor(1), 1)['record_count'], 0)


class ConcurrentFundsTests(unittest.TestCase):
    def run_race(self, *, same_item, same_customer, same_key=False):
        with tempfile.TemporaryDirectory(prefix='stage3-funds-race-') as folder:
            path = str(Path(folder) / 'fixture.sqlite')
            db, service, item, item2 = fixture(path)
            for customer in (1, 2):
                service.post(STAFF, customer, kind='deposit', amount_yen=1000, occurred_at=TIME,
                    source_key='deposit-' + str(customer), evidence_reference='fixture')
            db.commit()
            db.close()
            barrier = threading.Barrier(2)

            def contender(index):
                connection = sqlite3.connect(path, timeout=3)
                connection.execute('PRAGMA foreign_keys=ON')
                ledger = FundsService(FinanceStore(Registry(connection)))
                barrier.wait(timeout=10)
                try:
                    for retry in range(20):
                        try:
                            result = ledger.post(STAFF, 1 if same_customer else index + 1, kind='reserve', amount_yen=700,
                                physical_item_id=item if same_item or index == 0 else item2, occurred_at=LATER,
                                source_key='same-reservation' if same_key else 'reservation-' + str(index), evidence_reference='fixture')
                            connection.commit()
                            return ('ok', result['record']['record_id'])
                        except sqlite3.OperationalError as error:
                            connection.rollback()
                            if 'locked' not in str(error).lower() or retry == 19:
                                raise
                            time.sleep(0.01)
                        except RegistryError as error:
                            connection.rollback()
                            return ('rejected', error.code)
                finally:
                    connection.close()

            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(contender, (0, 1)))
            connection = sqlite3.connect(path)
            try:
                counts = connection.execute("SELECT COUNT(*) FROM stage3_fund_records WHERE kind='reserve'").fetchone()[0]
                minimum = connection.execute('SELECT MIN(available) FROM (SELECT SUM(delta_purchase_available) available FROM stage3_fund_records GROUP BY customer_id)').fetchone()[0]
            finally:
                connection.close()
            self.assertGreaterEqual(minimum, 0)
            self.assertEqual(counts, 1)
            return results

    def test_concurrent_two_items_cannot_overspend_same_customer(self):
        results = self.run_race(same_item=False, same_customer=True)
        self.assertEqual(sum(result[0] == 'ok' for result in results), 1)
        self.assertIn(('rejected', 'fund_insufficient_purchase_available'), results)

    def test_concurrent_two_customers_cannot_claim_same_item(self):
        results = self.run_race(same_item=True, same_customer=False)
        self.assertEqual(sum(result[0] == 'ok' for result in results), 1)
        self.assertIn(('rejected', 'fund_item_already_reserved'), results)

    def test_concurrent_retry_same_source_returns_same_reservation(self):
        results = self.run_race(same_item=True, same_customer=True, same_key=True)
        self.assertEqual(results[0], results[1])
        self.assertEqual(results[0][0], 'ok')


if __name__ == '__main__':
    unittest.main()
