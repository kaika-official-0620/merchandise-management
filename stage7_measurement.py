"""Staff-only pilot evidence and receipt-cohort measurement.

All writes are records only. No billing, messaging, participant invitation,
provider access, inferred actual cost or production activation is performed.
"""
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
import json
import uuid

from item_registry import RegistryError, _json, _now, _text, _time


PHASES = {
    'pilot5': dict(label='既存顧客の第1実証案', target_min=5, target_max=5, days_min=14, days_max=14, experience='existing_reseller'),
    'pilot15': dict(label='既存顧客の第2実証案', target_min=10, target_max=15, days_min=28, days_max=42, experience='existing_reseller'),
    'novice': dict(label='新規未経験者の操作観察', target_min=None, target_max=None, days_min=None, days_max=None, experience='novice'),
}
FINANCIAL_EVENTS = frozenset(('paid_offer', 'paid_consent', 'payment_success', 'renewal', 'cancellation',
    'billing_error', 'duplicate_payment'))
SAFETY_EVENTS = frozenset(('billing_error', 'cross_customer_access', 'duplicate_allocation', 'duplicate_payment'))
EVENT_TYPES = frozenset(('registration_started', 'registration_completed', 'retention_7d', 'retention_30d',
    'service_requested', 'item_received', 'item_registered', 'item_shipped', 'item_returned',
    'unsold_observed', 'support', 'major_task_attempt')) | FINANCIAL_EVENTS | SAFETY_EVENTS
PREREQUISITES = frozenset(('fee_terms', 'customer_scope', 'start_date', 'consent', 'features_available'))


def _dt(value):
    return datetime.fromisoformat(_time(value))


def _percentage(numerator, denominator):
    if not denominator:
        return None
    return str((Decimal(numerator) * 100 / Decimal(denominator)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))


def _duration(values):
    known = [value for value in values if value is not None]
    return dict(record_count=len(values), measured_count=len(known), unknown_count=len(values)-len(known),
        known_seconds=sum(known), total_seconds=sum(known) if values and len(known)==len(values) else None)


class MeasurementService:
    def __init__(self, store, clock=None):
        self.store = store
        self.clock = clock or _now

    def _staff(self, actor, finance=False):
        return self.store.authorize(actor, staff_required=True, finance=finance)

    def _has_finance(self, actor):
        try:
            self.store.authorize(actor, staff_required=True, finance=True)
            return True
        except RegistryError as error:
            if error.code != 'finance_permission_required':
                raise
            return False

    def _campaign(self, campaign_id):
        rows = self.store.query('SELECT * FROM stage7_campaigns WHERE campaign_id=?', (campaign_id,))
        if not rows:
            raise RegistryError('campaign_not_found', status=404)
        result = rows[0]
        result['paid'] = bool(result['paid'])
        result['prerequisite_references'] = json.loads(result.pop('prerequisite_references_json'))
        result['proposal'] = dict(PHASES[result['phase']])
        result['review_reasons'] = ['pilot_is_draft_only', 'real_participation_not_started']
        if result['paid']:
            result['review_reasons'] += ['paid_' + key + '_unconfirmed' for key in sorted(PREREQUISITES)
                if not result['prerequisite_references'].get(key)]
            result['review_reasons'].append('paid_prerequisites_need_operator_review')
        result['live_execution_enabled'] = False
        result['charge_created'] = False
        return result

    def create_campaign(self, actor, *, title, phase, planned_start_at, planned_end_at,
                        source_key, paid=False, prerequisite_references=None):
        self._staff(actor,finance=paid is True)
        if phase not in PHASES or type(paid) is not bool:
            raise RegistryError('invalid_campaign')
        title = _text(title, 'campaign_title', 120)
        start, end = _time(planned_start_at), _time(planned_end_at)
        if start >= end:
            raise RegistryError('invalid_campaign_period')
        references = prerequisite_references or {}
        if not isinstance(references, dict) or set(references)-PREREQUISITES:
            raise RegistryError('invalid_pilot_prerequisites')
        references = {key: _text(value, 'evidence_reference', 300) for key, value in references.items()}
        payload = dict(action='pilot_create', title=title, phase=phase, start=start, end=end, paid=paid, references=references)
        def write():
            campaign_id = uuid.uuid4().hex
            self.store.execute('''INSERT INTO stage7_campaigns(campaign_id,title,phase,planned_start_at,planned_end_at,
                paid,prerequisite_references_json,status,actor_user_id,recorded_at,source_key) VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
                (campaign_id,title,phase,start,end,int(paid),_json(references),'draft',actor.user_id,self.clock(),source_key))
            return self._campaign(campaign_id)
        return self.store.operation(actor,source_key,payload,write,staff_required=True,finance=paid)

    def enroll(self, actor, campaign_id, customer_id, *, experience, source_key):
        self._staff(actor)
        if experience not in ('existing_reseller','novice'):
            raise RegistryError('invalid_participant_experience')
        self.store.authorize(actor,customer_id,staff_required=True)
        payload = dict(action='pilot_enroll',campaign_id=campaign_id,customer_id=customer_id,experience=experience)
        def write():
            campaign=self._campaign(campaign_id)
            if experience != PHASES[campaign['phase']]['experience']:
                raise RegistryError('pilot_population_mismatch','既存顧客の実証と未経験者の操作観察は別に記録してください。')
            if self.store.query('SELECT participant_id FROM stage7_participants WHERE campaign_id=? AND customer_id=?',(campaign_id,customer_id)):
                raise RegistryError('participant_already_recorded',status=409)
            participant_id=uuid.uuid4().hex
            self.store.execute('''INSERT INTO stage7_participants(participant_id,campaign_id,customer_id,experience,
                actor_user_id,recorded_at,source_key) VALUES(?,?,?,?,?,?,?)''',
                (participant_id,campaign_id,customer_id,experience,actor.user_id,self.clock(),source_key))
            return dict(participant_id=participant_id,campaign_id=campaign_id,customer_id=customer_id,
                experience=experience,participation_status='candidate_only',invitation_sent=False)
        return self.store.operation(actor,source_key,payload,write,customer_id=customer_id,staff_required=True)

    def observe(self, actor, campaign_id, customer_id, *, event_type, occurred_at, evidence_reference,
                source_key, basis='observed', execution_mode='record_only', outcome=None, duration_seconds=None,
                correction_of=None, correction_reason=None):
        self._staff(actor,finance=event_type in FINANCIAL_EVENTS)
        if event_type not in EVENT_TYPES or basis not in ('observed','assumption','test'):
            raise RegistryError('invalid_measurement_event')
        if execution_mode != ('test' if basis=='test' else 'record_only'):
            raise RegistryError('measurement_mode_mismatch')
        if outcome is not None and type(outcome) is not bool:
            raise RegistryError('measurement_boolean_required')
        outcome_events=('retention_7d','retention_30d','major_task_attempt')
        if event_type in outcome_events and outcome is None or event_type not in outcome_events and outcome is not None:
            raise RegistryError('measurement_outcome_required')
        if duration_seconds is not None and (type(duration_seconds) is not int or not 0 <= duration_seconds <= 86400):
            raise RegistryError('invalid_measured_duration')
        if duration_seconds is not None and event_type not in ('support','major_task_attempt'):
            raise RegistryError('measurement_duration_not_applicable')
        when=_time(occurred_at)
        if basis != 'assumption' and when > _time(self.clock()):
            raise RegistryError('measurement_future_observation')
        evidence=_text(evidence_reference,'evidence_reference',300)
        if correction_of:
            correction_reason=_text(correction_reason,'correction_reason',500)
        elif correction_reason is not None:
            raise RegistryError('measurement_correction_target_required')
        payload=dict(action='pilot_observe',campaign_id=campaign_id,customer_id=customer_id,event_type=event_type,
            occurred_at=when,evidence_reference=evidence,basis=basis,execution_mode=execution_mode,outcome=outcome,
            duration_seconds=duration_seconds,correction_of=correction_of,correction_reason=correction_reason)
        def write():
            self._campaign(campaign_id)
            participants=self.store.query('SELECT * FROM stage7_participants WHERE campaign_id=? AND customer_id=?',(campaign_id,customer_id))
            if not participants:
                raise RegistryError('pilot_participant_required',status=409)
            participant=participants[0]
            if correction_of:
                previous=self.store.query('SELECT * FROM stage7_observations WHERE observation_id=?',(correction_of,))
                if not previous or any(previous[0][key]!=value for key,value in
                        [('participant_id',participant['participant_id']),('event_type',event_type),('basis',basis),('execution_mode',execution_mode)]):
                    raise RegistryError('measurement_correction_scope_mismatch',status=409)
                if self.store.query('SELECT observation_id FROM stage7_observations WHERE correction_of=?',(correction_of,)):
                    raise RegistryError('measurement_already_corrected',status=409)
            observation_id=uuid.uuid4().hex
            self.store.execute('''INSERT INTO stage7_observations(observation_id,participant_id,event_type,occurred_at,
                basis,execution_mode,outcome,duration_seconds,evidence_reference,correction_of,correction_reason,
                actor_user_id,recorded_at,source_key) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (observation_id,participant['participant_id'],event_type,when,basis,execution_mode,
                 None if outcome is None else int(outcome),duration_seconds,evidence,correction_of,correction_reason,
                 actor.user_id,self.clock(),source_key))
            return dict(observation_id=observation_id,campaign_id=campaign_id,customer_id=customer_id,
                event_type=event_type,basis=basis,execution_mode=execution_mode,charge_created=False,
                provider_verified=False,correction_of=correction_of)
        return self.store.operation(actor,source_key,payload,write,customer_id=customer_id,staff_required=True,
            finance=event_type in FINANCIAL_EVENTS)

    def detail(self, actor, campaign_id):
        self._staff(actor)
        result=self._campaign(campaign_id)
        result['participants']=self.store.query('''SELECT participant_id,customer_id,experience,recorded_at
            FROM stage7_participants WHERE campaign_id=? ORDER BY customer_id''',(campaign_id,))
        result['participant_count']=len(result['participants'])
        if not self._has_finance(actor):
            result['prerequisite_references']={}
            result['financial_references_visible']=False
        else:
            result['financial_references_visible']=True
        # No account names, addresses, email destinations or external IDs.
        return result

    def overview(self, actor):
        self._staff(actor)
        return dict(campaigns=[self.detail(actor,row['campaign_id']) for row in
            self.store.query('SELECT campaign_id FROM stage7_campaigns ORDER BY recorded_at,campaign_id')],
            phase_proposals=[dict(phase=key,**value) for key,value in PHASES.items()],
            live_execution_enabled=False,participants_automatically_selected=False)

    def report(self, actor, campaign_id, *, as_of=None):
        self._staff(actor)
        campaign=self.detail(actor,campaign_id)
        now=_time(as_of or self.clock())
        if now>_time(self.clock()):
            raise RegistryError('measurement_future_report')
        finance=self._has_finance(actor)
        rows=self.store.query('''SELECT o.*,p.customer_id FROM stage7_observations o
            JOIN stage7_participants p ON p.participant_id=o.participant_id
            WHERE p.campaign_id=? AND o.occurred_at<=?
            AND NOT EXISTS(SELECT 1 FROM stage7_observations n WHERE n.correction_of=o.observation_id)
            ORDER BY o.occurred_at,o.observation_id''',(campaign_id,now))
        if not finance:
            rows=[row for row in rows if row['event_type'] not in FINANCIAL_EVENTS]
        history=self.store.query('''SELECT o.*,p.customer_id FROM stage7_observations o
            JOIN stage7_participants p ON p.participant_id=o.participant_id
            WHERE p.campaign_id=? ORDER BY o.recorded_at,o.observation_id''',(campaign_id,))
        replaced={row['correction_of'] for row in history if row['correction_of']}
        history=[dict(row,is_current=row['observation_id'] not in replaced) for row in history
            if finance or row['event_type'] not in FINANCIAL_EVENTS]
        groups=[]
        for basis,mode in (('observed','record_only'),('assumption','record_only'),('test','test')):
            selected=[row for row in rows if row['basis']==basis and row['execution_mode']==mode]
            counts={kind:sum(row['event_type']==kind for row in selected) for kind in sorted(EVENT_TYPES)
                if finance or kind not in FINANCIAL_EVENTS}
            tasks=[row for row in selected if row['event_type']=='major_task_attempt']
            support=[row['duration_seconds'] for row in selected if row['event_type']=='support']
            groups.append(dict(basis=basis,execution_mode=mode,observation_count=len(selected),
                event_counts=counts,retention_7d=self._retention(selected,7,now),
                retention_30d=self._retention(selected,30,now),support_duration=_duration(support),
                safety_observations=dict(counts={kind:counts[kind] for kind in sorted(SAFETY_EVENTS) if kind in counts},
                    zero_verified=False,coverage='unknown',basis=basis,execution_mode=mode,
                    note='記録件数0は事故0件の検証完了を意味しません。確認範囲・期間と事例資料を別途照合してください。'),
                major_task_completion=dict(attempt_count=len(tasks),success_count=sum(row['outcome']==1 for row in tasks),
                    percent=_percentage(sum(row['outcome']==1 for row in tasks),len(tasks)),
                    target_percent='80.00',target_is_proposal=True),
                is_actual_verified=False,financial_events_visible=finance))
        return dict(campaign=campaign,as_of=now,groups=groups,observation_history=history,financial_events_visible=finance,
            source='staff_entered_evidence',provider_verified=False,live_execution_enabled=False,
            limitations=['未記録の行動を失敗・ゼロと見なしません。',
                '観察・仮定・模擬は別集計です。観察記録は外部決済の成功証明ではありません。',
                '継続率は登録完了から7日目/30日目の24時間の観察枠を使用します。',
                'キャンペーンは下書きであり、参加同意・実施開始・有料課金を意味しません。'])

    @staticmethod
    def _retention(rows, days, as_of):
        starts={}
        for row in rows:
            if row['event_type']=='registration_completed':
                starts.setdefault(row['customer_id'],row['occurred_at'])
        eligible=measured=retained=0
        incomplete=0
        for customer,start in starts.items():
            window_start=_dt(start)+timedelta(days=days)
            window_end=window_start+timedelta(days=1)
            if _dt(as_of)<window_end:
                incomplete+=1
                continue
            eligible+=1
            evidence=[row for row in rows if row['customer_id']==customer and row['event_type']=='retention_'+str(days)+'d'
                and window_start<=_dt(row['occurred_at'])<window_end]
            if evidence:
                measured+=1
                retained+=evidence[-1]['outcome']==1
        return dict(baseline_count=len(starts),eligible_count=eligible,observation_window_incomplete_count=incomplete,
            measured_count=measured,missing_count=eligible-measured,retained_count=retained,
            percent=_percentage(retained,eligible) if eligible and measured==eligible else None,
            measured_only_percent=_percentage(retained,measured),window_days=days,
            status='no_baseline' if not starts else 'insufficient_observation' if incomplete or eligible!=measured else 'observed')

    def receipt_cohort(self, actor, *, start_at, end_at, as_of=None, customer_id=None):
        """Keep the original received physical-item set, including unsold/returns.

        Stage-2 logs have record-only provenance. Test commerce activity is
        explicitly flagged per item, never promoted to measured operations.
        No customer sale proceeds or purchase balances become company profit.
        """
        self._staff(actor)
        if customer_id is not None:
            self.store.authorize(actor,customer_id,staff_required=True)
        start,end,now=_time(start_at),_time(end_at),_time(as_of or self.clock())
        if start>=end or now<start or now>_time(self.clock()):
            raise RegistryError('invalid_cohort_period')
        query='''SELECT o.*,a.assignment_id,a.started_at AS ownership_start,a.ended_at AS ownership_end
            FROM business_item_observations o
            JOIN business_item_assignments a ON a.physical_item_id=o.physical_item_id AND a.role='owner'
            JOIN business_parties p ON p.party_id=a.party_id AND p.kind='customer' AND p.user_id=o.customer_id
            WHERE o.kind='received' AND o.occurred_at>=? AND o.occurred_at<? AND o.occurred_at<=?
            AND a.started_at<=o.occurred_at AND (a.ended_at IS NULL OR a.ended_at>o.occurred_at)'''
        params=(start,end,now)
        if customer_id is not None:
            query+=' AND o.customer_id=?';params+=(customer_id,)
        receipts=self.store.query(query+' ORDER BY o.occurred_at,o.observation_id',params)
        seen=set();items=[]
        for receipt in receipts:
            key=receipt['physical_item_id']
            if key in seen:
                continue
            seen.add(key)
            items.append(self._cohort_item(receipt,now))
        groups=[]
        for mode in ('record_only','test'):
            selected=[row for row in items if row['execution_mode']==mode]
            shipped=[row for row in selected if row['status']=='sale_shipped']
            unsold=[row for row in selected if row['status']!='sale_shipped']
            groups.append(dict(execution_mode=mode,item_count=len(selected),sale_shipped_count=len(shipped),
                registration_complete_item_count=sum(row['registered_at'] is not None for row in selected),
                receipt_event_count=sum(row['event_counts'].get('received',0) for row in selected),
                registration_event_count=sum(row['event_counts'].get('registration_completed',0) for row in selected),
                shipment_event_count=sum(row['event_counts'].get('shipped',0) for row in selected),
                unsold_or_unresolved_count=len(unsold),return_count=sum(row['return_count'] for row in selected),
                transferred_count=sum(row['status']=='ownership_transferred' for row in selected),
                observed_sale_shipped_percent=_percentage(len(shipped),len(selected)),
                final_sell_through_percent=None,final_sell_through_status='cohort_observation_not_closed',
                work_duration=_duration([duration for row in selected for duration in row.pop('_work_durations')]),
                unsold_work_duration=_duration([duration for row in unsold for duration in row['_unsold_work_durations']]),
                rework_duration=_duration([duration for row in selected for duration in row.pop('_rework_durations')]),
                items_without_work_records_count=sum(row['work_duration']['record_count']==0 for row in selected),
                work_record_coverage='not_confirmed',
                duration_scope='記録済み作業の時間。未記録の作業や業務全体の総時間は含みません。',
                measured_company_profit_yen=None,company_profit_status='work_cost_and_service_revenue_not_reconciled',
                items=[{key:value for key,value in row.items() if not key.startswith('_')} for row in selected]))
        return dict(start_at=start,end_at=end,as_of=now,groups=groups,source='existing_registry_records',
            cohort_definition='期間内の最初の受領を個品単位で固定し、同じ所有者の記録を追跡',
            execution_mode='record_only',provider_verified=False,live_execution_enabled=False,
            limitations=['受領記録がない旧在庫は含めません。未記録を推定補完しません。',
                '記録上の発送率は最終売却率ではありません。未売却・返品・移管も分母に残します。',
                '一度所有者が変わった後の作業と売却は、前の所有者の集計へ含めません。',
                '作業分数の未入力と作業ログ自体の不足があり、記録された時間は総原価を保証しません。'])

    def _cohort_item(self, receipt, as_of):
        item,customer,start=receipt['physical_item_id'],receipt['customer_id'],receipt['occurred_at']
        ownership_end=receipt['ownership_end']
        until=min(as_of,ownership_end) if ownership_end else as_of
        observations=self.store.query('''SELECT * FROM business_item_observations WHERE physical_item_id=?
            AND customer_id=? AND occurred_at>=? AND occurred_at<=? ORDER BY occurred_at,recorded_at,observation_id''',
            (item,customer,start,until))
        if ownership_end:
            observations=[row for row in observations if row['occurred_at']<ownership_end]
        works=self.store.query('''SELECT w.* FROM business_work_records w JOIN business_work_context c ON c.work_id=w.work_id
            JOIN business_parties p ON p.party_id=w.actor_party_id AND p.kind='staff'
            WHERE w.physical_item_id=? AND c.customer_id=? AND w.performed_at>=? AND w.performed_at<=? ORDER BY w.performed_at,w.work_id''',
            (item,customer,start,until))
        if ownership_end:
            works=[row for row in works if row['performed_at']<ownership_end]
        registered=next((row['occurred_at'] for row in observations if row['kind']=='registration_completed'),None)
        state='unsold';last_departure=None;last_arrival=start;held_seconds=0;custody_intervals=[]
        return_count=0;custody_interval_unknown=False
        for row in observations:
            kind,at=row['kind'],row['occurred_at']
            if kind=='sold':state='sale_pending_shipment'
            elif kind=='sale_cancelled':state='unsold'
            elif kind=='returned':
                state='returned_after_sale';return_count+=1
                if last_arrival is None:last_arrival=at
            elif kind in ('shipped','return_shipped'):
                state='sale_shipped' if kind=='shipped' else 'returned_to_customer'
                last_departure=at
                if last_arrival:
                    custody_intervals.append((last_arrival,at))
                    held_seconds+=int((_dt(at)-_dt(last_arrival)).total_seconds());last_arrival=None
                else:custody_interval_unknown=True
            elif kind=='received' and last_arrival is None:
                last_arrival=at;state='unsold'
        if last_arrival:
            custody_intervals.append((last_arrival,until))
            held_seconds+=int((_dt(until)-_dt(last_arrival)).total_seconds())
        custody=self.store.query('''SELECT a.started_at,a.ended_at,p.kind FROM business_item_assignments a
            JOIN business_parties p ON p.party_id=a.party_id WHERE a.physical_item_id=? AND a.role='custodian'
            AND a.started_at<? AND (a.ended_at IS NULL OR a.ended_at>?)''',(item,until,start))
        # Explicit custody-role changes may not have a physical arrival event.
        # In that case do not report event-derived elapsed time as actual storage.
        if any(row['kind']!='kaika' and any(row['started_at']<closed and
                (row['ended_at'] is None or row['ended_at']>opened) for opened,closed in custody_intervals)
                for row in custody):
            custody_interval_unknown=True
        if ownership_end and ownership_end<=as_of:state='ownership_transferred'
        # Existing stage-4 orders are test records by design. Mark mixed source
        # items conservatively instead of promoting a test sale to actual data.
        test_rows=self.store.query('SELECT order_id FROM stage4_channel_orders WHERE physical_item_id=?',(item,))
        test_orders=self.store.query('SELECT order_id FROM stage4_purchase_orders WHERE physical_item_id=?',(item,))
        mode='test' if test_rows or test_orders else 'record_only'
        durations=[row['duration_seconds'] for row in works]
        return dict(physical_item_id=item,customer_id=customer,receipt_observation_id=receipt['observation_id'],
            ownership_assignment_id=receipt['assignment_id'],received_at=start,registered_at=registered,
            observation_until=until,elapsed_seconds=int((_dt(until)-_dt(start)).total_seconds()),
            pre_registration_seconds=int((_dt(registered)-_dt(start)).total_seconds()) if registered else None,
            registration_status='recorded' if registered else 'not_observed',
            custody_seconds=None if custody_interval_unknown else held_seconds,
            custody_period_closed=last_arrival is None,last_departure_at=last_departure,
            status=state,return_count=return_count,work_duration=_duration(durations),
            event_counts={kind:sum(row['kind']==kind for row in observations)
                for kind in ('received','registration_completed','sold','shipped','returned','return_shipped','sale_cancelled')},
            work_record_coverage='not_confirmed',execution_mode=mode,provider_verified=False,
            work_by_kind={kind:_duration([row['duration_seconds'] for row in works if row['kind']==kind])
                for kind in sorted({row['kind'] for row in works})},
            _work_durations=durations,_unsold_work_durations=durations,
            _rework_durations=[row['duration_seconds'] for row in works if row['rework_of']])
