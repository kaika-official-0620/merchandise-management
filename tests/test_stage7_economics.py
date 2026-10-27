"""In-memory costing/permissions tests; no app import or external side effects."""
import sqlite3
import unittest

from item_registry import Registry, RegistryError
from stage7_common import Stage7Store, migration_statements as common_schema
from stage7_economics import EconomicsService, calculate_scenario, COST_CATEGORIES
from stage7_economics_schema import migration_statements as economics_schema
from test_stage6_home import fixture, STAFF, CUSTOMER, INVENTORY_STAFF


def sample(**changes):
    value = dict(free_accounts=10, paid_accounts=5, paid_monthly_price_yen=3000,
        receipts=20, sellthrough_bps=5000, physical_fee_per_sale_yen=1000,
        free_digital_per_account_yen=20, paid_digital_per_account_yen=100,
        physical_other_per_received_yen=30, physical_other_per_sold_yen=40,
        fixed_expenses_yen=1000, labor_mode='time_allocated', hourly_labor_yen=1200,
        fixed_salary_yen=None, work_minutes_per_received=3, work_minutes_per_sold=5,
        other_staff_minutes=0,
        digital_payment_fee_bps=300, physical_payment_fee_bps=300,
        available_staff_minutes=120, opening_inventory=200, shelf_capacity=1000,
        working_capital_yen=700000, purchase_cash_per_received_yen=30000,
        operating_cash_outflow_yen=10000,
        tax_basis='exclusive', timing_basis='steady_state_assumption', basis='assumption', reference='fictitious scenario, not observed data')
    value.update(changes)
    return value


class ErrorAssertions:
    def assertCode(self, code, callback):
        with self.assertRaises(RegistryError) as error:
            callback()
        self.assertEqual(error.exception.code, code)


class CalculationTests(ErrorAssertions, unittest.TestCase):
    def test_all_receipts_cost_but_only_sold_generate_revenue(self):
        result = calculate_scenario(sample())
        metrics = result['metrics']
        self.assertEqual(metrics['expected_sold']['value'], 10)
        self.assertEqual(metrics['receipt_work_minutes']['value'], 60)
        self.assertEqual(metrics['sale_work_minutes']['value'], 50)
        self.assertEqual(metrics['physical_revenue_yen']['value'], 10000)
        self.assertEqual(metrics['total_cost_yen']['value'], 5650)
        self.assertEqual(metrics['expected_profit_yen']['value'], 19350)
        self.assertEqual(metrics['projected_inventory']['value'], 210)
        self.assertEqual(metrics['required_working_capital_yen']['value'], 610000)
        self.assertTrue(all(result['checks'].values()))

    def test_unsold_inventory_still_costs_receipt_work(self):
        result = calculate_scenario(sample(sellthrough_bps=0))['metrics']
        self.assertEqual(result['physical_revenue_yen']['value'], 0)
        self.assertEqual(result['labor_cost_yen']['value'], 1200)
        self.assertEqual(result['physical_nonlabor_cost_yen']['value'], 600)
        self.assertEqual(result['projected_inventory']['value'], 220)

    def test_fractional_expected_sale_not_rounded_to_one_sale(self):
        result = calculate_scenario(sample(receipts=1, sellthrough_bps=3333))['metrics']
        self.assertEqual(result['expected_sold'], {'value': '0.33', 'numerator': 3333, 'denominator': 10000})
        self.assertEqual(result['physical_revenue_yen'], {'value': '333.30', 'numerator': 3333, 'denominator': 10})

    def test_money_display_half_up_does_not_change_exact_fraction(self):
        result = calculate_scenario(sample(receipts=1, sellthrough_bps=1, physical_fee_per_sale_yen=50))
        self.assertEqual(result['metrics']['physical_revenue_yen'], {'value': '0.01', 'numerator': 1, 'denominator': 200})

    def test_unknown_work_does_not_become_experience_based_default(self):
        result = calculate_scenario(sample(work_minutes_per_received=None))
        self.assertIsNone(result['metrics']['labor_cost_yen']['value'])
        self.assertIsNone(result['metrics']['expected_profit_yen']['value'])
        self.assertIsNone(result['checks']['staff_capacity'])
        self.assertIn('work_minutes_per_received', result['unknown_fields'])

    def test_unknown_service_time_capacity_is_not_four_hours(self):
        result = calculate_scenario(sample(available_staff_minutes=None))
        self.assertIsNone(result['checks']['staff_capacity'])
        self.assertEqual(result['metrics']['required_staff_minutes']['value'], 110)

    def test_unknown_procurement_travel_and_support_time_blocks_completeness(self):
        result = calculate_scenario(sample(other_staff_minutes=None))
        self.assertIsNone(result['checks']['staff_capacity'])
        self.assertIsNone(result['metrics']['expected_profit_yen']['value'])
        self.assertIn('other_staff_minutes', result['unknown_fields'])
        with_other = calculate_scenario(sample(other_staff_minutes=60))
        self.assertEqual(with_other['metrics']['required_staff_minutes']['value'], 170)
        self.assertEqual(with_other['metrics']['labor_cost_yen']['value'], 3400)
        self.assertFalse(with_other['checks']['staff_capacity'])

    def test_unknown_payment_rate_blocks_profit(self):
        result = calculate_scenario(sample(digital_payment_fee_bps=None))
        self.assertIsNone(result['metrics']['payment_fees_yen']['value'])
        self.assertIsNone(result['checks']['profit_positive'])

    def test_unknown_tax_blocks_combined_profit(self):
        result = calculate_scenario(sample(tax_basis='unknown'))
        self.assertIsNone(result['metrics']['expected_profit_yen']['value'])
        self.assertIsNone(result['checks']['profit_positive'])
        self.assertTrue(result['checks']['shelf_capacity'])

    def test_inclusive_tax_not_silently_converted(self):
        result = calculate_scenario(sample(tax_basis='inclusive'))
        self.assertEqual(result['tax_basis'], 'inclusive')
        self.assertEqual(result['metrics']['expected_profit_yen']['value'], 19350)

    def test_zero_population_has_zero_variable_cost_not_divide_by_zero(self):
        result = calculate_scenario(sample(free_accounts=0, paid_accounts=0, receipts=0,
            paid_monthly_price_yen=None, free_digital_per_account_yen=None,
            paid_digital_per_account_yen=None, physical_fee_per_sale_yen=None))
        self.assertEqual(result['metrics']['total_revenue_yen']['value'], 0)
        self.assertEqual(result['metrics']['expected_profit_yen']['value'], -1000)

    def test_unknown_population_is_not_zero(self):
        result = calculate_scenario(sample(paid_accounts=None))
        self.assertIsNone(result['metrics']['digital_revenue_yen']['value'])
        self.assertIsNone(result['checks']['profit_positive'])

    def test_fixed_salary_excludes_time_allocated_salary(self):
        result = calculate_scenario(sample(labor_mode='fixed_salary', hourly_labor_yen=None, fixed_salary_yen=440000))
        self.assertEqual(result['metrics']['labor_cost_yen']['value'], 440000)
        self.assertFalse(result['checks']['profit_positive'])
        self.assertEqual(result['metrics']['required_staff_minutes']['value'], 110)

    def test_two_salary_methods_rejected_even_explicit_zero(self):
        for data in (sample(fixed_salary_yen=0), sample(labor_mode='fixed_salary', fixed_salary_yen=440000)):
            self.assertCode('economic_salary_double_count', lambda: calculate_scenario(data))

    def test_known_capacity_boundaries_and_funding_separate(self):
        exact = calculate_scenario(sample(available_staff_minutes=110, shelf_capacity=220, working_capital_yen=610000))
        self.assertTrue(exact['checks']['staff_capacity'])
        self.assertTrue(exact['checks']['shelf_capacity'])
        self.assertTrue(exact['checks']['funding_capacity'])
        below = calculate_scenario(sample(available_staff_minutes=109, shelf_capacity=219, working_capital_yen=609999))
        self.assertFalse(below['checks']['staff_capacity'])
        self.assertFalse(below['checks']['shelf_capacity'])
        self.assertFalse(below['checks']['funding_capacity'])
        self.assertTrue(below['checks']['profit_positive'])

    def test_missing_operating_payments_does_not_claim_funding_sufficient(self):
        result = calculate_scenario(sample(operating_cash_outflow_yen=None))
        self.assertIsNone(result['checks']['funding_capacity'])
        self.assertIsNone(result['metrics']['required_working_capital_yen']['value'])

    def test_final_sellthrough_cannot_hide_inventory_peak(self):
        result = calculate_scenario(sample(opening_inventory=200,receipts=1000,sellthrough_bps=10000,shelf_capacity=1000))
        self.assertEqual(result['metrics']['projected_inventory']['value'],200)
        self.assertEqual(result['metrics']['peak_inventory']['value'],1200)
        self.assertFalse(result['checks']['shelf_capacity'])

    def test_unknown_sale_timing_does_not_claim_monthly_profit_or_capacity(self):
        data=sample()
        data.pop('timing_basis')
        result=calculate_scenario(data)
        self.assertIsNone(result['metrics']['expected_profit_yen']['value'])
        self.assertIsNone(result['metrics']['total_revenue_yen']['value'])
        self.assertIsNone(result['metrics']['required_staff_minutes']['value'])
        self.assertIsNone(result['checks']['staff_capacity'])
        self.assertIn('timing_basis',result['unknown_fields'])

    def test_sales_ratio_boundaries_and_outside_range(self):
        self.assertEqual(calculate_scenario(sample(sellthrough_bps=10000))['metrics']['expected_sold']['value'], 20)
        self.assertCode('economic_rate_out_of_range', lambda: calculate_scenario(sample(sellthrough_bps=10001)))
        self.assertCode('economic_integer_required', lambda: calculate_scenario(sample(sellthrough_bps=-1)))

    def test_floats_booleans_negative_money_rejected(self):
        for changes in ({'paid_monthly_price_yen': 1.1}, {'paid_monthly_price_yen': True}, {'paid_monthly_price_yen': -1}):
            self.assertCode('integer_yen_required', lambda: calculate_scenario(sample(**changes)))
        self.assertCode('economic_integer_required', lambda: calculate_scenario(sample(receipts=True)))

    def test_customer_funds_cannot_be_scenario_revenue_input(self):
        self.assertCode('economic_scenario_fields_invalid', lambda: calculate_scenario(sample(customer_purchase_balances=100000)))
        self.assertIn('customer_unsettled_sales', calculate_scenario(sample())['excluded'])

    def test_scenario_cannot_be_labelled_observed_actual_profit(self):
        self.assertCode('economic_scenario_is_not_actual_profit', lambda: calculate_scenario(sample(basis='measured')))


class EconomicsEvidenceTests(ErrorAssertions, unittest.TestCase):
    def setUp(self):
        self.db = fixture()
        self.addCleanup(self.db.close)
        for schema in (common_schema, economics_schema):
            for sql in schema():
                self.db.execute(sql)
        self.service = EconomicsService(Stage7Store(Registry(self.db)))

    def cost(self, **changes):
        fields = dict(period='2026-10', scope='free_digital', category='hosting', amount_yen=100,
                      basis='measured', tax_basis='exclusive', reference='fixture-invoice/line-1', source_key='cost-1')
        fields.update(changes)
        return self.service.record_cost(STAFF, **fields)

    def test_only_inventory_and_finance_staff_can_read_write(self):
        for actor in (CUSTOMER, INVENTORY_STAFF):
            for action in (lambda: self.service.cost_summary(actor, '2026-10'),
                           lambda: self.service.scenarios(actor),
                           lambda: self.service.save_scenario(actor, name='x', period='2026-10', inputs=sample(), source_key='unauthorized')):
                with self.assertRaises(RegistryError):
                    action()
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM stage7_economic_scenarios').fetchone()[0], 0)

    def test_identical_retry_returns_same_record_no_new_cost(self):
        first = self.cost()
        self.assertEqual(first, self.cost())
        self.assertEqual(self.service.cost_summary(STAFF, '2026-10')['scopes']['free_digital']['total_yen'], 100)

    def test_duplicate_evidence_with_different_operation_is_rejected(self):
        self.cost()
        self.assertCode('economic_duplicate_evidence', lambda: self.cost(source_key='different-operation'))
        self.assertCode('economic_duplicate_evidence', lambda: self.cost(source_key='other-scope', scope='paid_digital'))

    def test_same_operation_cannot_change_amount(self):
        self.cost()
        self.assertCode('source_key_conflict', lambda: self.cost(amount_yen=101))

    def test_append_correction_replaces_current_amount_but_preserves_original(self):
        old = self.cost()
        new = self.service.correct_cost(STAFF, old['entry_id'], amount_yen=150, basis='measured',
            tax_basis='exclusive', reference='fixture-credit-note', reason='receipt corrected', source_key='correction')
        self.assertEqual(new['revision'], 2)
        self.assertEqual(self.service.cost_summary(STAFF, '2026-10')['scopes']['free_digital']['total_yen'], 150)
        history = self.service.history(STAFF, old['entry_id'])['history']
        self.assertEqual([r['amount_yen'] for r in history], [100, 150])
        self.assertEqual(history[1]['previous_revision_id'], history[0]['revision_id'])

    def test_correction_replay_does_not_append_again(self):
        old = self.cost()
        args = dict(amount_yen=None, basis='reference', tax_basis='unknown', reference='new evidence', reason='not established', source_key='correction')
        self.assertEqual(self.service.correct_cost(STAFF, old['entry_id'], **args), self.service.correct_cost(STAFF, old['entry_id'], **args))
        self.assertEqual(len(self.service.history(STAFF, old['entry_id'])['history']), 2)

    def test_no_entries_is_unknown_not_zero(self):
        result = self.service.cost_summary(STAFF, '2026-10')
        self.assertTrue(all(value['total_yen'] is None for value in result['scopes'].values()))
        self.assertFalse(result['complete_operating_cost'])

    def test_unknown_amount_retains_known_subtotal_and_blocks_total(self):
        self.cost()
        self.cost(amount_yen=None, reference='invoice/line-2', source_key='unknown')
        result = self.service.cost_summary(STAFF, '2026-10')['scopes']['free_digital']
        self.assertIsNone(result['total_yen'])
        self.assertEqual(result['known_subtotal_by_tax'], {'exclusive': 100})
        self.assertEqual(result['unknown_count'], 1)

    def test_tax_mix_not_summed(self):
        self.cost()
        self.cost(amount_yen=110, tax_basis='inclusive', reference='invoice/line-2', source_key='gross')
        result = self.service.cost_summary(STAFF, '2026-10')['scopes']['free_digital']
        self.assertIsNone(result['total_yen'])
        self.assertEqual(result['tax_basis'], 'mixed')
        self.assertEqual(result['known_subtotal_by_tax'], {'exclusive': 100, 'inclusive': 110})

    def test_unknown_tax_never_assumed_zero_tax(self):
        self.cost(tax_basis='unknown')
        self.assertIsNone(self.service.cost_summary(STAFF, '2026-10')['scopes']['free_digital']['total_yen'])

    def test_measured_assumed_reference_costs_have_separate_subtotals(self):
        self.cost()
        self.cost(amount_yen=500, basis='assumption', reference='assumption-line', source_key='assumed')
        self.cost(amount_yen=300, basis='reference', reference='reference-line', source_key='reference')
        result = self.service.cost_summary(STAFF, '2026-10')['scopes']['free_digital']
        self.assertIsNone(result['total_yen'])
        self.assertEqual(result['known_subtotal_by_tax'], {})
        self.assertEqual(result['by_basis']['measured']['total_yen'], 100)
        self.assertEqual(result['by_basis']['assumption']['total_yen'], 500)
        self.assertEqual(result['by_basis']['reference']['total_yen'], 300)

    def test_unit_cost_denominators_zero_unknown_and_exact_fraction(self):
        self.cost()
        for count in (None, 0):
            result = self.service.cost_summary(STAFF, '2026-10', {'free_digital': count})
            self.assertIsNone(result['scopes']['free_digital']['per_unit_yen']['value'])
        result = self.service.cost_summary(STAFF, '2026-10', {'free_digital': 3})
        self.assertEqual(result['scopes']['free_digital']['per_unit_yen'], {'value': '33.33', 'numerator': 100, 'denominator': 3})

    def test_fixed_and_time_salary_evidence_cannot_be_double_counted(self):
        self.cost(scope='fixed', category='staff_salary_fixed')
        self.assertCode('economic_salary_double_count', lambda: self.cost(category='staff_time_allocated', reference='other', source_key='allocated'))

    def test_void_salary_then_record_other_method_keeps_history(self):
        old = self.cost(scope='fixed', category='staff_salary_fixed')
        self.service.correct_cost(STAFF, old['entry_id'], amount_yen=100, basis='reference', tax_basis='exclusive',
            reference='void-method', reason='switch method', source_key='void', voided=True)
        self.cost(category='staff_time_allocated', reference='allocation', source_key='allocate')
        self.assertEqual(self.service.cost_summary(STAFF, '2026-10')['scopes']['free_digital']['total_yen'], 100)
        self.assertEqual(len(self.service.history(STAFF, old['entry_id'])['history']), 2)

    def test_voided_salary_cannot_restore_and_double_count_other_mode(self):
        old = self.cost(scope='fixed', category='staff_salary_fixed')
        args = dict(amount_yen=100, basis='reference', tax_basis='exclusive', reference='correction', reason='change', source_key='void', voided=True)
        self.service.correct_cost(STAFF, old['entry_id'], **args)
        self.cost(category='staff_time_allocated', reference='allocation', source_key='allocate')
        args.update(source_key='restore', voided=False)
        self.assertCode('economic_salary_double_count', lambda: self.service.correct_cost(STAFF, old['entry_id'], **args))

    def test_cost_and_resource_tables_are_immutable(self):
        self.cost()
        self.service.save_scenario(STAFF, name='sample', period='2026-10', inputs=sample(), source_key='scenario')
        for table in ('stage7_economic_entries', 'stage7_economic_revisions', 'stage7_economic_scenarios'):
            with self.assertRaises(sqlite3.IntegrityError):
                self.db.execute('DELETE FROM ' + table)

    def test_resource_count_unknown_correction_not_added_twice(self):
        old = self.service.record_resource(STAFF, period='2026-10', scope='free_digital', metric='image_bytes',
            quantity=None, basis='reference', reference='storage report', source_key='bytes')
        self.service.correct_resource(STAFF, old['entry_id'], quantity=1000, basis='measured',
            reference='confirmed report', reason='measured later', source_key='measured')
        result = self.service.cost_summary(STAFF, '2026-10')['resources']
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['quantity'], 1000)
        self.assertEqual(result[0]['unit'], 'bytes')

    def test_resource_dimension_can_only_be_corrected(self):
        args = dict(period='2026-10', scope='fixed', metric='database_bytes', quantity=1000, basis='measured', reference='report1', source_key='resource1')
        self.service.record_resource(STAFF, **args)
        args.update(reference='report2', source_key='resource2')
        self.assertCode('economic_duplicate_resource', lambda: self.service.record_resource(STAFF, **args))

    def test_customer_purchase_funds_are_not_allowed_cost_or_resource(self):
        self.assertCode('economic_category_invalid', lambda: self.cost(category='customer_purchase_balance'))
        self.assertNotIn('customer_unsettled_sales', COST_CATEGORIES)

    def test_scenario_snapshot_immutable_idempotent_and_labelled_comparison(self):
        args = dict(name='fictitious', period='2026-10', inputs=sample(), source_key='scenario')
        saved = self.service.save_scenario(STAFF, **args)
        self.assertEqual(saved, self.service.save_scenario(STAFF, **args))
        result = self.service.scenarios(STAFF, '2026-10')
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['result']['model'], 'comparison_only')
        self.assertEqual(result[0]['inputs'], sample())

    def test_invalid_period_and_empty_correction_reason_rejected(self):
        self.assertCode('economic_period_required', lambda: self.cost(period='2026-13'))
        old = self.cost()
        with self.assertRaises(RegistryError):
            self.service.correct_cost(STAFF, old['entry_id'], amount_yen=100, basis='measured', tax_basis='exclusive', reference='new', reason='', source_key='bad')


if __name__ == '__main__':
    unittest.main()
