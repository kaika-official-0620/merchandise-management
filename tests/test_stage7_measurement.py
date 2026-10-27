"""Measurement tests use a disposable in-memory registry, without app import."""
import sqlite3
import unittest

from item_registry import Actor
from stage7_common import Stage7Store, migration_statements as common_schema
from stage7_measurement import MeasurementService, EVENT_TYPES, FINANCIAL_EVENTS, SAFETY_EVENTS
from stage7_measurement_schema import migration_statements as measurement_schema
from test_stage6_home import HomeFixture, STAFF, CUSTOMER, OTHER, INVENTORY_STAFF, time


class MeasurementTests(HomeFixture, unittest.TestCase):
    def setUp(self):
        self.init_home()
        for schema in (common_schema,measurement_schema):
            for statement in schema():self.db.execute(statement)
        self.store=Stage7Store(self.registry)
        self.now='2026-12-02T12:00:00+00:00'
        self.service=MeasurementService(self.store,clock=lambda:self.now)

    def campaign(self, key='campaign', **changes):
        values=dict(title='既存顧客の実証下書き',phase='pilot5',planned_start_at=time(),
            planned_end_at='2026-11-05T00:00:00+00:00',source_key=key)
        values.update(changes)
        return self.service.create_campaign(STAFF,**values)

    def enrolled(self, customer_id=1, **changes):
        campaign=self.campaign(**changes)
        self.service.enroll(STAFF,campaign['campaign_id'],customer_id,experience='existing_reseller',source_key='enroll-'+str(customer_id))
        return campaign['campaign_id']

    def observe(self, campaign_id, key, kind, customer_id=1, **changes):
        values=dict(event_type=kind,occurred_at=time(10),evidence_reference='fixture observation',source_key=key)
        values.update(changes)
        return self.service.observe(STAFF,campaign_id,customer_id,**values)

    def cohort(self, **changes):
        values=dict(start_at='2026-10-22T00:00:00+00:00',end_at='2026-10-23T00:00:00+00:00')
        values.update(changes)
        return self.service.receipt_cohort(STAFF,**values)

    def test_proposals_do_not_select_customers_or_start_campaign(self):
        result=self.service.overview(STAFF)
        self.assertEqual(result['campaigns'],[])
        self.assertFalse(result['participants_automatically_selected'])
        self.assertEqual(result['phase_proposals'][0]['target_min'],5)
        self.assertEqual(result['phase_proposals'][1]['target_max'],15)
        campaign=self.campaign()
        self.assertEqual(campaign['status'],'draft')
        self.assertFalse(campaign['live_execution_enabled'])

    def test_staff_permissions_are_checked_against_db(self):
        for actor in (CUSTOMER,OTHER,Actor(1,True)):
            self.assertCode('staff_permission_required',lambda:self.service.overview(actor))
        self.assertEqual(self.service.overview(INVENTORY_STAFF)['campaigns'],[])

    def test_paid_prerequisites_remain_review_only_even_all_references_present(self):
        missing=self.campaign(paid=True)
        self.assertIn('paid_consent_unconfirmed',missing['review_reasons'])
        refs={key:'fixture ref' for key in ('fee_terms','customer_scope','start_date','consent','features_available')}
        complete=self.campaign(key='complete',paid=True,prerequisite_references=refs)
        self.assertIn('paid_prerequisites_need_operator_review',complete['review_reasons'])
        self.assertFalse(complete['live_execution_enabled'])
        self.assertFalse(complete['charge_created'])

    def test_paid_campaign_and_financial_references_require_finance(self):
        self.assertCode('finance_permission_required',lambda:self.service.create_campaign(INVENTORY_STAFF,
            title='有料案',phase='pilot5',planned_start_at=time(),planned_end_at=time(59),source_key='bad',paid=True))
        campaign=self.campaign(paid=True,prerequisite_references={'fee_terms':'financial fixture ref'})
        visible=self.service.detail(INVENTORY_STAFF,campaign['campaign_id'])
        self.assertEqual(visible['prerequisite_references'],{})
        self.assertFalse(visible['financial_references_visible'])

    def test_create_idempotent_conflicting_key_and_period(self):
        original=self.campaign()
        self.assertEqual(self.campaign()['campaign_id'],original['campaign_id'])
        self.assertCode('source_key_conflict',lambda:self.campaign(title='変更'))
        self.assertCode('invalid_campaign_period',lambda:self.campaign(planned_end_at=time()))
        self.assertCode('invalid_pilot_prerequisites',lambda:self.campaign(prerequisite_references={'enabled':'yes'}))

    def test_enrollment_is_explicit_and_idempotent(self):
        campaign=self.campaign()['campaign_id']
        result=self.service.enroll(STAFF,campaign,1,experience='existing_reseller',source_key='enroll')
        again=self.service.enroll(STAFF,campaign,1,experience='existing_reseller',source_key='enroll')
        self.assertEqual(result,again)
        self.assertFalse(result['invitation_sent'])
        self.assertCode('participant_already_recorded',lambda:self.service.enroll(STAFF,campaign,1,experience='existing_reseller',source_key='another'))
        self.assertEqual(self.service.detail(STAFF,campaign)['participant_count'],1)

    def test_novice_and_existing_population_cannot_mix(self):
        campaign=self.campaign(phase='novice')['campaign_id']
        self.assertCode('pilot_population_mismatch',lambda:self.service.enroll(STAFF,campaign,1,experience='existing_reseller',source_key='wrong'))
        self.service.enroll(STAFF,campaign,1,experience='novice',source_key='novice')
        self.assertEqual(self.service.detail(STAFF,campaign)['participants'][0]['experience'],'novice')

    def test_observation_requires_explicit_participant(self):
        campaign=self.campaign()['campaign_id']
        self.assertCode('pilot_participant_required',lambda:self.observe(campaign,'obs','registration_started'))

    def test_modes_are_separate_and_future_actual_not_accepted(self):
        campaign=self.enrolled()
        for basis,mode in [('observed','record_only'),('assumption','record_only'),('test','test')]:
            self.observe(campaign,basis,'support',basis=basis,execution_mode=mode,duration_seconds=60)
        groups=self.service.report(STAFF,campaign)['groups']
        self.assertEqual([row['observation_count'] for row in groups],[1,1,1])
        self.assertCode('measurement_mode_mismatch',lambda:self.observe(campaign,'bad','support',basis='test'))
        self.assertCode('measurement_future_observation',lambda:self.observe(campaign,'future','support',occurred_at='2027-01-01T00:00:00Z'))
        self.observe(campaign,'forecast','support',basis='assumption',occurred_at='2027-01-01T00:00:00Z')

    def test_financial_evidence_requires_finance_and_is_hidden_from_inventory_staff(self):
        campaign=self.enrolled()
        for event in sorted(FINANCIAL_EVENTS):
            self.observe(campaign,event,event)
            self.assertCode('finance_permission_required',lambda:self.service.observe(INVENTORY_STAFF,campaign,1,
                event_type=event,occurred_at=time(10),evidence_reference='fixture',source_key='forbidden'+event))
        financial=self.service.report(STAFF,campaign)
        inventory=self.service.report(INVENTORY_STAFF,campaign)
        self.assertTrue(financial['financial_events_visible'])
        self.assertFalse(inventory['financial_events_visible'])
        self.assertEqual(inventory['groups'][0]['observation_count'],0)
        for event in FINANCIAL_EVENTS:self.assertNotIn(event,inventory['groups'][0]['event_counts'])

    def test_observation_deduplicated_and_conflict_rejected(self):
        campaign=self.enrolled()
        original=self.observe(campaign,'one','support',duration_seconds=60)
        self.assertEqual(self.observe(campaign,'one','support',duration_seconds=60),original)
        self.assertCode('source_key_conflict',lambda:self.observe(campaign,'one','support',duration_seconds=61))
        self.assertEqual(self.service.report(STAFF,campaign)['groups'][0]['support_duration']['total_seconds'],60)

    def test_correction_is_append_only_and_changes_only_current_total(self):
        campaign=self.enrolled()
        original=self.observe(campaign,'one','support',duration_seconds=60)
        fixed=self.observe(campaign,'fix','support',duration_seconds=90,
            correction_of=original['observation_id'],correction_reason='計測誤記の訂正')
        self.assertEqual(fixed['correction_of'],original['observation_id'])
        self.assertEqual(self.service.report(STAFF,campaign)['groups'][0]['support_duration']['total_seconds'],90)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM stage7_observations').fetchone()[0],2)
        history=self.service.report(STAFF,campaign)['observation_history']
        self.assertEqual(len(history),2)
        self.assertEqual(sum(row['is_current'] for row in history),1)
        with self.assertRaises(sqlite3.IntegrityError):self.db.execute('DELETE FROM stage7_observations')
        self.assertCode('measurement_already_corrected',lambda:self.observe(campaign,'second-fix','support',duration_seconds=100,
            correction_of=original['observation_id'],correction_reason='duplicate'))

    def test_correction_cannot_change_customer_type_or_basis(self):
        campaign=self.enrolled()
        self.service.enroll(STAFF,campaign,2,experience='existing_reseller',source_key='enroll2')
        original=self.observe(campaign,'one','support',duration_seconds=60)
        for changes in [dict(customer_id=2),dict(basis='assumption'),dict(kind='registration_started',duration_seconds=None)]:
            values=dict(campaign_id=campaign,key='bad',kind='support',duration_seconds=70,
                correction_of=original['observation_id'],correction_reason='wrong')
            values.update(changes)
            self.assertCode('measurement_correction_scope_mismatch',lambda:self.observe(**values))

    def test_unknown_support_duration_not_zero(self):
        campaign=self.enrolled()
        self.observe(campaign,'unknown','support')
        self.observe(campaign,'measured','support',duration_seconds=120)
        duration=self.service.report(STAFF,campaign)['groups'][0]['support_duration']
        self.assertIsNone(duration['total_seconds'])
        self.assertEqual(duration['known_seconds'],120)
        self.assertEqual(duration['unknown_count'],1)

    def test_measured_zero_is_distinct_from_no_measurements(self):
        campaign=self.enrolled()
        self.assertIsNone(self.service.report(STAFF,campaign)['groups'][0]['support_duration']['total_seconds'])
        self.observe(campaign,'zero','support',duration_seconds=0)
        self.assertEqual(self.service.report(STAFF,campaign)['groups'][0]['support_duration']['total_seconds'],0)

    def test_retention_insufficient_window_and_missing_are_not_zero(self):
        campaign=self.enrolled()
        self.observe(campaign,'completed','registration_completed')
        early=self.service.report(STAFF,campaign,as_of='2026-10-28T12:00:00Z')['groups'][0]
        self.assertIsNone(early['retention_7d']['percent'])
        self.assertEqual(early['retention_7d']['observation_window_incomplete_count'],1)
        late=self.service.report(STAFF,campaign)['groups'][0]
        self.assertEqual(late['retention_7d']['missing_count'],1)
        self.assertIsNone(late['retention_7d']['percent'])
        self.assertIsNone(late['retention_30d']['percent'])

    def test_retention_explicit_success_failure_and_no_immature_pollution(self):
        campaign=self.enrolled()
        self.service.enroll(STAFF,campaign,2,experience='existing_reseller',source_key='enroll2')
        for customer in (1,2):
            self.observe(campaign,'completed'+str(customer),'registration_completed',customer_id=customer)
            self.observe(campaign,'retained'+str(customer),'retention_7d',customer_id=customer,
                occurred_at='2026-10-29T01:11:00Z',outcome=customer==1)
        retention=self.service.report(STAFF,campaign)['groups'][0]['retention_7d']
        self.assertEqual(retention['percent'],'50.00')
        self.assertEqual(retention['status'],'observed')

    def test_retention_outside_day_window_not_counted(self):
        campaign=self.enrolled()
        self.observe(campaign,'completed','registration_completed')
        self.observe(campaign,'late','retention_7d',occurred_at='2026-11-01T00:00:00Z',outcome=True)
        retention=self.service.report(STAFF,campaign)['groups'][0]['retention_7d']
        self.assertEqual(retention['measured_count'],0)
        self.assertIsNone(retention['percent'])

    def test_retention_baselines_and_measures_never_mix_assumed_with_observed(self):
        campaign=self.enrolled()
        self.observe(campaign,'completed','registration_completed')
        self.observe(campaign,'assumed','retention_7d',basis='assumption',occurred_at='2026-10-29T01:11:00Z',outcome=True)
        groups=self.service.report(STAFF,campaign)['groups']
        self.assertEqual(groups[0]['retention_7d']['missing_count'],1)
        self.assertEqual(groups[1]['retention_7d']['status'],'no_baseline')

    def test_future_report_does_not_turn_unobserved_days_into_elapsed_days(self):
        campaign=self.enrolled()
        self.assertCode('measurement_future_report',lambda:self.service.report(STAFF,campaign,as_of='2027-01-01T00:00:00Z'))
        self.assertCode('invalid_cohort_period',lambda:self.cohort(as_of='2027-01-01T00:00:00Z'))

    def test_major_task_target_is_proposal_not_preawarded(self):
        campaign=self.enrolled()
        for index,success in enumerate((True,False,True)):
            self.observe(campaign,'task'+str(index),'major_task_attempt',outcome=success,duration_seconds=10)
        metric=self.service.report(STAFF,campaign)['groups'][0]['major_task_completion']
        self.assertEqual(metric['percent'],'66.67')
        self.assertEqual(metric['target_percent'],'80.00')
        self.assertTrue(metric['target_is_proposal'])

    def test_duration_outcome_and_event_validation(self):
        campaign=self.enrolled()
        self.assertCode('invalid_measured_duration',lambda:self.observe(campaign,'bad','support',duration_seconds=1.5))
        self.assertCode('invalid_measured_duration',lambda:self.observe(campaign,'bad','support',duration_seconds=True))
        self.assertCode('measurement_outcome_required',lambda:self.observe(campaign,'bad','retention_7d'))
        self.assertCode('measurement_outcome_required',lambda:self.observe(campaign,'bad','support',outcome=True))
        self.assertCode('invalid_measurement_event',lambda:self.observe(campaign,'bad','charge_customer'))

    def test_empty_safety_records_are_not_verified_zero_incidents(self):
        campaign=self.enrolled()
        for group in self.service.report(STAFF,campaign)['groups']:
            safety=group['safety_observations']
            self.assertEqual(safety['counts'],{kind:0 for kind in SAFETY_EVENTS})
            self.assertFalse(safety['zero_verified'])
            self.assertEqual(safety['coverage'],'unknown')

    def test_safety_evidence_is_separated_by_observed_assumption_and_test(self):
        campaign=self.enrolled()
        for index,(basis,mode) in enumerate((('observed','record_only'),('assumption','record_only'),('test','test')),1):
            for event in SAFETY_EVENTS:
                for number in range(index):
                    self.observe(campaign,basis+event+str(number),event,basis=basis,execution_mode=mode,
                        evidence_reference='case-fixture-'+str(index)+'-'+str(number))
        for index,group in enumerate(self.service.report(STAFF,campaign)['groups'],1):
            safety=group['safety_observations']
            self.assertTrue(all(count==index for count in safety['counts'].values()))
            self.assertEqual(safety['basis'],group['basis'])
            self.assertEqual(safety['execution_mode'],group['execution_mode'])
            self.assertFalse(safety['zero_verified'])

    def test_financial_safety_incidents_require_finance_and_are_not_exposed_to_inventory_staff(self):
        campaign=self.enrolled()
        for event in SAFETY_EVENTS:
            self.observe(campaign,'incident-'+event,event,evidence_reference='case-fixture')
        report=self.service.report(INVENTORY_STAFF,campaign)
        safety=report['groups'][0]['safety_observations']
        self.assertEqual(safety['counts'],{'cross_customer_access':1,'duplicate_allocation':1})
        self.assertFalse(safety['zero_verified'])
        for event in ('billing_error','duplicate_payment'):
            self.assertCode('finance_permission_required',lambda:self.service.observe(INVENTORY_STAFF,campaign,1,
                event_type=event,occurred_at=time(10),evidence_reference='case-fixture',source_key='forbidden-'+event))
            self.assertFalse(any(row['event_type']==event for row in report['observation_history']))
        for event in ('cross_customer_access','duplicate_allocation'):
            self.service.observe(INVENTORY_STAFF,campaign,1,event_type=event,occurred_at=time(10),
                evidence_reference='case-fixture',source_key='staff-'+event)
        for table in ('stage3_charge_drafts','stage5_payment_attempts','stage4_purchase_orders'):
            self.assertEqual(self.db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0],0)

    def test_cohort_empty_not_final_sale_rate_zero(self):
        result=self.cohort()['groups'][0]
        self.assertEqual(result['item_count'],0)
        self.assertIsNone(result['observed_sale_shipped_percent'])
        self.assertIsNone(result['final_sell_through_percent'])
        self.assertIsNone(result['measured_company_profit_yen'])

    def test_cohort_records_registration_before_shipping_and_unsold_time(self):
        self.register()
        self.registry.record_work(STAFF,self.item,'photography',time(5),'work','fixture',duration_seconds=60,measurement_source='timer')
        result=self.cohort(as_of=time(20))['groups'][0]
        self.assertEqual(result['item_count'],1)
        self.assertEqual(result['items'][0]['pre_registration_seconds'],120)
        self.assertEqual(result['items'][0]['elapsed_seconds'],19*60)
        self.assertEqual(result['registration_complete_item_count'],1)
        self.assertEqual(result['registration_event_count'],1)
        self.assertEqual(result['unsold_work_duration']['total_seconds'],60)
        self.assertEqual(result['observed_sale_shipped_percent'],'0.00')
        self.assertIsNone(result['final_sell_through_percent'])

    def test_cohort_missing_registration_and_duration_not_filled_with_estimates(self):
        self.event('received',1)
        self.registry.record_work(STAFF,self.item,'photography',time(5),'unknown','fixture')
        row=self.cohort()['groups'][0]['items'][0]
        self.assertIsNone(row['pre_registration_seconds'])
        self.assertIsNone(row['work_duration']['total_seconds'])
        self.assertEqual(row['work_duration']['unknown_count'],1)

    def test_cohort_return_stays_in_original_group_and_excludes_time_outside_custody(self):
        self.register()
        self.event('sold',10);self.event('shipped',11);self.event('returned',20,return_reason='fixture')
        result=self.cohort(as_of=time(30))['groups'][0]
        self.assertEqual(result['item_count'],1)
        self.assertEqual(result['return_count'],1)
        self.assertEqual(result['sale_shipped_count'],0)
        self.assertEqual(result['items'][0]['custody_seconds'],20*60)
        self.assertEqual(result['items'][0]['status'],'returned_after_sale')

    def test_cohort_self_work_not_counted_as_kaika_labor(self):
        self.registry.record_work(CUSTOMER,self.item,'photography',time(1),'self','fixture',duration_seconds=300,measurement_source='timer')
        self.event('received',2)
        row=self.cohort()['groups'][0]
        self.assertEqual(row['work_duration']['record_count'],0)
        self.assertIsNone(row['work_duration']['total_seconds'])

    def test_cohort_rework_is_separate_but_included_once_in_total(self):
        self.register()
        work=self.registry.record_work(STAFF,self.item,'photography',time(5),'first','fixture',duration_seconds=60,measurement_source='timer')
        self.registry.record_work(STAFF,self.item,'photography',time(6),'redo','fixture',duration_seconds=30,
            measurement_source='timer',rework_of=work['work_id'],rework_reason='撮影し直し')
        group=self.cohort()['groups'][0]
        self.assertEqual(group['work_duration']['total_seconds'],90)
        self.assertEqual(group['rework_duration']['total_seconds'],30)

    def test_unsold_return_to_customer_work_is_not_dropped_from_unsold_cost(self):
        self.register()
        self.registry.record_work(STAFF,self.item,'packing',time(5),'pack','fixture',duration_seconds=60,measurement_source='timer')
        self.event('return_shipped',10);self.event('return_received',11)
        group=self.cohort(as_of=time(20))['groups'][0]
        self.assertEqual(group['unsold_work_duration']['total_seconds'],60)
        self.assertEqual(group['unsold_or_unresolved_count'],1)
        self.assertEqual(group['items'][0]['custody_seconds'],9*60)

    def test_second_receipt_keeps_one_physical_item_and_measures_both_custody_intervals(self):
        self.register()
        self.event('return_shipped',10);self.event('return_received',11);self.event('received',12)
        group=self.cohort(as_of=time(20))['groups'][0]
        self.assertEqual(group['item_count'],1)
        self.assertEqual(group['receipt_event_count'],2)
        self.assertEqual(group['items'][0]['custody_seconds'],17*60)
        self.assertEqual(group['items'][0]['status'],'unsold')

    def test_custodian_changed_without_arrival_evidence_is_not_invented_custody_time(self):
        self.register()
        self.registry.change_assignment(STAFF,self.item,'custodian','customer',1,occurred_at=time(10),evidence_reference='fixture',source_key='custody-change')
        self.assertIsNone(self.cohort(as_of=time(20))['groups'][0]['items'][0]['custody_seconds'])

    def test_receipt_period_uses_normalized_timezone_and_exclusive_end(self):
        self.event('received',1)
        self.assertEqual(self.cohort(start_at='2026-10-22T10:01:00+09:00',end_at='2026-10-22T10:02:00+09:00')['groups'][0]['item_count'],1)
        self.assertEqual(self.cohort(end_at='2026-10-22T10:01:00+09:00')['groups'][0]['item_count'],0)

    def test_cohort_owner_transfer_does_not_include_new_owner_work_or_sale(self):
        self.register()
        self.registry.change_assignment(STAFF,self.item,'owner','customer',2,occurred_at=time(10),evidence_reference='fixture',source_key='transfer')
        self.registry.record_work(STAFF,self.item,'photography',time(15),'new-owner-work','fixture',duration_seconds=60,measurement_source='timer')
        self.event('sold',20);self.event('shipped',21)
        group=self.cohort(as_of=time(30),customer_id=1)['groups'][0]
        self.assertEqual(group['items'][0]['status'],'ownership_transferred')
        self.assertEqual(group['sale_shipped_count'],0)
        self.assertEqual(group['work_duration']['record_count'],0)
        self.assertEqual(group['items'][0]['elapsed_seconds'],9*60)

    def test_test_channel_order_is_separated_from_record_only_cohort(self):
        self.now=time(50)
        self.channel_order()
        result=self.cohort()
        self.assertEqual(result['groups'][0]['item_count'],0)
        self.assertEqual(result['groups'][1]['item_count'],1)

    def test_cohort_requires_staff_and_customer_filter(self):
        self.register()
        self.assertCode('staff_permission_required',lambda:self.service.receipt_cohort(CUSTOMER,start_at=time(),end_at=time(59)))
        self.assertEqual(self.cohort(customer_id=2)['groups'][0]['item_count'],0)

    def test_measurement_never_creates_charges_or_funds(self):
        campaign=self.enrolled(paid=True)
        self.observe(campaign,'payment','payment_success')
        self.register();self.cohort()
        for table in ('stage3_charge_drafts','stage5_payment_attempts','stage4_purchase_orders'):
            self.assertEqual(self.db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0],0)

    def test_schema_append_only_and_postgres_review_sql(self):
        self.enrolled()
        for table in ('stage7_campaigns','stage7_participants'):
            with self.assertRaises(sqlite3.IntegrityError):self.db.execute('DELETE FROM '+table)
        self.assertIn('stage7_observation_scope_guard','\n'.join(measurement_schema('postgres')))


if __name__=='__main__':unittest.main()
