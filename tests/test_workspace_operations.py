"""Business routing and read-only queue projections with fictional data."""
import unittest
from types import SimpleNamespace
from stage6_presentation import organize_tasks, home_view, local_time
from test_stage6_home import HomeFixture, CUSTOMER, STAFF, OTHER, time


class OperationsHomeTests(HomeFixture, unittest.TestCase):
    def setUp(self):
        self.init_home()

    def test_self_sale_does_not_request_kaika_shipping(self):
        self.event('sold')
        self.assertFalse(any(task['kind'] == 'shipping' for task in self.home.staff(STAFF)['tasks']))
        self.assertEqual(self.home.customer(CUSTOMER)['sold_history_count'], 1)

    def test_self_return_hold_does_not_request_kaika_work(self):
        self.event('sold', 1)
        self.event('shipped', 2)
        self.event('returned', 3, return_reason='fictional')
        self.assertFalse(any(task['kind'] == 'hold' for task in self.home.staff(STAFF)['tasks']))
        self.assertTrue(any('保留理由' in action.get('body', '') for action in self.home.customer(CUSTOMER)['next_actions']))

    def test_kaika_sale_requires_shipping_and_identifies_item(self):
        self.register()
        self.event('sold', 5)
        tasks = [row for row in self.home.staff(STAFF)['tasks'] if row['kind'] == 'shipping']
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]['item_title'], '自分の商品')
        self.assertTrue(tasks[0]['item_label'])

    def test_stop_failures_are_visible_until_confirmed(self):
        service, order = self.channel_order()
        # The sold listing itself must also be confirmed stopped in manual mode.
        stop = service.overview(STAFF)['stop_tasks'][0]
        self.assertTrue(any(row['kind'] == 'channel_stop' for row in self.home.staff(STAFF)['tasks']))
        service.stop_task(STAFF, stop['task_id'], status='confirmed_stopped', evidence_reference='fictional-confirmation',
                          occurred_at=time(20), source_key='confirm-stop')
        self.assertFalse(any(row.get('task_id') == stop['task_id'] for row in self.home.staff(STAFF)['tasks']))

    def test_customer_filter_never_expands_authorized_items(self):
        result = home_view(self.home.customer(OTHER), {'q': self.item, 'view': 'all'}, False)
        self.assertEqual(result['items'], [])

    def test_inventory_filter_does_not_change_total_or_financial_projection(self):
        self.registry.create_item(CUSTOMER, 'second', occurred_at=time(), evidence_reference='fixture', source_key='second')
        dashboard = self.home.customer(CUSTOMER)
        result = home_view(dashboard, {'q': self.item}, False)
        self.assertEqual(result['total_count'], 2)
        self.assertEqual(result['filtered_count'], 1)
        self.assertEqual(len(dashboard['items']), 2)
        self.assertIsNone(dashboard['money']['actual_purchase_available_yen'])


class QueuePresentationTests(unittest.TestCase):
    def test_disabled_or_unconfigured_payment_route_never_gets_home_links(self):
        from stage6_web import HomeWeb
        web = HomeWeb.__new__(HomeWeb)
        web.app = SimpleNamespace(config={}, extensions={})
        self.assertFalse(web.sandbox_payments_enabled())
        web.app.config['STAGE9_PAYMENTS_ENABLED'] = True
        self.assertFalse(web.sandbox_payments_enabled())
        web.app.extensions['stage9_payment_scope'] = object()
        self.assertTrue(web.sandbox_payments_enabled())
        web.app.config['STAGE9_PAYMENTS_ENABLED'] = False
        self.assertFalse(web.sandbox_payments_enabled())

    def task(self, name, deadline=None, **kwargs):
        return dict(kind=name, deadline_at=deadline, **kwargs)

    def test_timezone_sorting_and_unknown_deadline(self):
        rows = organize_tasks([self.task('future', '2026-10-27T00:00:00+09:00'),
            self.task('unknown'), self.task('past', '2026-10-26T07:00:00+09:00'),
            self.task('conflict_resolution')], [], '2026-10-26T00:00:00+00:00')
        self.assertEqual([row['kind'] for row in rows], ['conflict_resolution', 'past', 'future', 'unknown'])
        self.assertEqual(rows[-1]['deadline_label'], '未確認')
        self.assertEqual(rows[1]['deadline_label'], '2026/10/26 07:00')

    def test_invalid_or_naive_timestamp_remains_unknown(self):
        for value in (None, 'yesterday', '2026-10-26T00:00:00'):
            self.assertEqual(local_time(value), '未確認')

    def test_bad_filter_falls_back_to_authorized_all(self):
        row = self.task('shipping', assignee_user_id=None, attention='normal')
        result = home_view({'tasks': [row]}, {'view': '../admin', 'q': ''}, True)
        self.assertEqual(result['selected'], 'all')
        self.assertEqual(result['tasks'], [row])

    def test_unassigned_and_attention_filters_are_independent(self):
        rows = [self.task('shipping', assignee_user_id=9, attention='overdue'),
                self.task('registration', assignee_user_id=None, attention='normal')]
        self.assertEqual(home_view({'tasks': rows}, {'view':'attention'}, True)['tasks'], [rows[0]])
        self.assertEqual(home_view({'tasks': rows}, {'view':'unassigned'}, True)['tasks'], [rows[1]])


if __name__ == '__main__':
    unittest.main()
