import unittest
from invoice_tax import inclusive_breakdown, invoice_tax_context, invoice_tax_category_label, normalize_tax_category, STEP4_SOURCE


class InvoiceTaxTests(unittest.TestCase):
    def context(self, items, **changes):
        totals = inclusive_breakdown(items)
        invoice = dict(source_workflow_step=STEP4_SOURCE, tax_breakdown_version=1,
                       tax_rounding_method='floor', total_amount=totals['total'],
                       tax_amount_8=totals['tax_8'], tax_amount_10=totals['tax_10'])
        invoice.update(changes)
        return invoice_tax_context(invoice, items)

    def test_standard_gross_is_preserved(self):
        result = inclusive_breakdown([{'amount': 1000, 'tax_category': '10'}])
        self.assertEqual((result['total'], result['tax_10'], result['tax_8']), (1000, 90, 0))

    def test_reduced_gross_is_preserved(self):
        result = inclusive_breakdown([{'amount': 1080, 'tax_category': '8'}])
        self.assertEqual((result['total'], result['tax_8']), (1080, 80))

    def test_round_once_per_rate_not_per_item(self):
        result = inclusive_breakdown([{'amount': 6, 'tax_category': '10'}] * 2)
        self.assertEqual(result['tax_10'], 1)

    def test_mixed_rates_and_non_taxable_amounts(self):
        result = inclusive_breakdown([{'amount': 1100, 'tax_category': '10'},
                                     {'amount': 1080, 'tax_category': '8'},
                                     {'amount': 50, 'tax_category': 'exempt'},
                                     {'amount': 70, 'tax_category': 'outside'}])
        self.assertEqual((result['total'], result['tax_10'], result['tax_8']), (2300, 100, 80))
        self.assertTrue(result['confirmed'])

    def test_missing_category_remains_unknown(self):
        result = self.context([{'amount': 1000}])
        self.assertFalse(result['confirmed'])
        self.assertEqual(result['rows'], [{'label': '未確認の金額', 'amount': 1000}])

    def test_unknown_mixed_does_not_hide_known_breakdown(self):
        result = self.context([{'amount': 1100, 'tax_category': '10'}, {'amount': 500}])
        self.assertFalse(result['confirmed'])
        self.assertIn({'label': 'うち消費税等（10%）', 'amount': 100}, result['rows'])
        self.assertEqual(result['total'], 1600)

    def test_zero_known_is_different_from_unknown(self):
        self.assertTrue(self.context([{'amount': 0, 'tax_category': '10'}])['confirmed'])
        self.assertFalse(self.context([{'amount': 0}])['confirmed'])

    def test_invalid_category_is_rejected(self):
        with self.assertRaises(ValueError):
            normalize_tax_category('0')

    def test_negative_amount_is_rejected(self):
        with self.assertRaises(ValueError):
            inclusive_breakdown([{'amount': -1, 'tax_category': '10'}])

    def test_legacy_zero_does_not_become_tax_exemption(self):
        invoice = {'source_workflow_step': STEP4_SOURCE, 'total_amount': 1000, 'tax_amount_10': 0}
        original = dict(invoice)
        result = invoice_tax_context(invoice, [{'amount': 1000, 'tax_category': '0'}])
        self.assertTrue(result['legacy'])
        self.assertEqual(result['rows'][0]['amount'], None)
        self.assertEqual(invoice, original)
        self.assertEqual(invoice_tax_category_label(invoice, {'tax_category': '0'}), '未確認')

    def test_corrupt_saved_breakdown_is_not_presented_as_confirmed(self):
        result = self.context([{'amount': 1000, 'tax_category': '10'}], tax_amount_10=0)
        self.assertFalse(result['confirmed'])
        self.assertIn('保存内容に確認', result['message'])

    def test_stored_total_is_not_recalculated_for_display(self):
        result = self.context([{'amount': 1000, 'tax_category': '10'}], total_amount=999)
        self.assertEqual(result['total'], 999)
        self.assertFalse(result['confirmed'])

    def test_self_created_invoice_convention_is_unchanged(self):
        invoice = {'total_amount': 2640, 'subtotal': 2400, 'tax_amount_10': 240}
        self.assertIsNone(invoice_tax_context(invoice, []))
        self.assertEqual(invoice_tax_category_label(invoice, {'tax_category': '10'}), '10%')


if __name__ == '__main__':
    unittest.main()
