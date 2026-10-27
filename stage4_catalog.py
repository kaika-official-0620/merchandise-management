"""Company stock, immutable offers and isolated test purchase records.

No external payment, customer deposits, production inventory import, automatic
charge or statutory ownership rule is enabled. Caller owns commit/rollback.
"""
from datetime import datetime, timedelta, timezone
import json
import uuid

from item_registry import RegistryError, _json, _now, _text, _time
from stage3_common import yen
from stage3_funds import FundsService


def catalog_registration_proof(store,item_id,customer_id):
    """Internal immutable adoption proof; never creates registration/work counts."""
    owner=store.registry._roles(item_id).get('owner',{})
    if owner.get('user_id')!=customer_id:
        return None
    rows=store.query('''SELECT o.*,r.title,r.description,r.item_condition,r.photo_refs_json
        FROM stage4_purchase_orders o JOIN stage4_catalog_revisions r ON r.revision_id=o.revision_id
        WHERE o.physical_item_id=? AND o.customer_id=?''',(item_id,customer_id))
    for row in rows:
        if owner.get('source_event_id')!='stage4:'+row['order_id']+':owner':
            continue
        status=store.query('SELECT kind FROM stage4_catalog_events WHERE order_id=? ORDER BY event_no DESC',(row['order_id'],))
        if not status or status[0]['kind'] not in ('confirmed','handed_over'):
            continue
        photos=json.loads(row['photo_refs_json'])
        if not row['title'] or not row['description'] or not row['item_condition'] or not photos:
            continue
        return {'order_id':row['order_id'],'revision_id':row['revision_id'],
            'ownership_assignment_id':owner['assignment_id'],'photo_refs':photos,
            'registration_count_increment':0,'new_work_created':False,'execution_mode':'test'}
    return None


class _OrderFundsBridge:
    """Exact one-operation capability; never grants general finance rights.

    CatalogService constructs this only inside its authorized locked operation.
    The original authenticated actor is preserved in the fund audit record.
    """
    def __init__(self, store, actor, customer_id, item_id, source_key, expected):
        self.store, self.registry = store, store.registry
        self.actor, self.customer_id, self.item_id = actor, customer_id, item_id
        self.source_key, self.expected = source_key, expected
        self.used = False

    def execute(self, sql, params=()):
        return self.store.execute(sql, params)

    def query(self, sql, params=()):
        return self.store.query(sql, params)

    def require_ready(self):
        self.store.require_ready()

    def authorize(self, actor, customer_id=None, write=False):
        if actor != self.actor or customer_id not in (None, self.customer_id):
            raise RegistryError('catalog_funds_scope_denied', status=403)
        self.store.authorize(actor, customer_id=self.customer_id)

    def item(self, actor, item_id, write=False):
        self.authorize(actor, self.customer_id)
        if item_id != self.item_id:
            raise RegistryError('catalog_funds_item_denied', status=403)
        # No customer data is read. Company goods do not yet have a customer
        # Registry profile. The caller already locked and verified this item.
        return {'physical_item_id': item_id, 'owner_user_id': None}

    def operation(self, actor, customer_id, source_key, payload, callback):
        self.authorize(actor, customer_id)
        if self.used or source_key != 'funds:' + self.source_key or payload != self.expected:
            raise RegistryError('catalog_funds_operation_denied', status=403)
        if payload['kind'] not in ('reserve', 'release', 'purchase') or payload['mode'] != 'test':
            raise RegistryError('catalog_funds_operation_denied', status=403)
        self.used = True
        return callback()


class CatalogService:
    def __init__(self, store, *, ownership_rule='undecided', clock=None):
        if ownership_rule not in ('undecided', 'test_payment_confirmation'):
            raise ValueError('Only an explicit test ownership rule is supported')
        self.store = store
        self.ownership_rule = ownership_rule
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _when(self,value):
        when=_time(value)
        if when>_time(self.clock()):
            raise RegistryError('catalog_time_in_future')
        return when

    def _company(self, item_id):
        rows = self.store.query('SELECT * FROM stage4_company_items WHERE physical_item_id=?', (item_id,))
        if not rows:
            raise RegistryError('catalog_item_not_found', status=404)
        return rows[0]

    def _owner_is_company(self, item_id):
        return self.store.registry._roles(item_id).get('owner', {}).get('kind') == 'kaika'

    def _revision(self, item_id, revision_id):
        rows = self.store.query('SELECT * FROM stage4_catalog_revisions WHERE physical_item_id=? AND revision_id=?',
                                (item_id, revision_id))
        if not rows:
            raise RegistryError('catalog_revision_not_found', status=404)
        return rows[0]

    @staticmethod
    def _offer(row):
        return {key: row[key] for key in ('physical_item_id', 'revision_id', 'revision_no', 'title', 'description',
                'item_condition', 'price_yen', 'currency')} | {'photo_refs': json.loads(row['photo_refs_json'])}

    def _event(self, actor, item_id, kind, when, evidence, *, order_id=None, details=None):
        previous=self.store.query('SELECT occurred_at FROM stage4_catalog_events WHERE physical_item_id=? ORDER BY event_no DESC',(item_id,))
        if previous and when<previous[0]['occurred_at']:
            raise RegistryError('catalog_time_reversed',status=409)
        no = self.store.query('SELECT COALESCE(MAX(event_no),0)+1 AS n FROM stage4_catalog_events WHERE physical_item_id=?', (item_id,))[0]['n']
        event_id = 'ce_' + uuid.uuid4().hex
        self.store.execute('''INSERT INTO stage4_catalog_events(event_id,physical_item_id,order_id,event_no,kind,
            actor_user_id,occurred_at,recorded_at,evidence_reference,details_json) VALUES (?,?,?,?,?,?,?,?,?,?)''',
            (event_id,item_id,order_id,no,kind,actor.user_id,when,_now(),evidence,_json(details or {})))
        return event_id

    def _fund(self, actor, order, kind, when, source_key, evidence, origin=None):
        payload = dict(kind=kind,amount_yen=order['amount_yen'],mode='test',occurred_at=when,
            evidence_reference=evidence,original_record_id=origin,physical_item_id=order['physical_item_id'],note=None)
        bridge = _OrderFundsBridge(self.store,actor,order['customer_id'],order['physical_item_id'],source_key,payload)
        return FundsService(bridge).post(actor,order['customer_id'],source_key=source_key,**payload)['record']['record_id']

    def _check_photos(self, item_id, refs):
        if not isinstance(refs, (tuple,list)) or len(refs)>20 or len(set(refs))!=len(refs):
            raise RegistryError('catalog_photos_invalid')
        result=[]
        for ref in refs:
            ref=_text(ref,'photo_reference',200)
            rows=self.store.query('''SELECT m.media_id FROM stage4_media m JOIN business_item_assignments a
                ON a.assignment_id=m.ownership_assignment_id WHERE m.media_id=? AND m.physical_item_id=?
                AND a.physical_item_id=m.physical_item_id AND a.role='owner' AND a.party_id='kaika' AND a.ended_at IS NULL''', (ref,item_id))
            if not rows:
                raise RegistryError('catalog_photo_not_owned',status=409)
            result.append(ref)
        return result

    def create_company_item(self, actor, *, title, purchase_cost_yen=None, occurred_at, evidence_reference, source_key):
        title,when,evidence=_text(title,'title',300),self._when(occurred_at),_text(evidence_reference,'evidence')
        cost=yen(purchase_cost_yen,optional=True)
        payload=dict(action='company_create',title=title,cost=cost,when=when,evidence=evidence)
        def create():
            item_id='ki_'+uuid.uuid4().hex
            self.store.execute('INSERT INTO business_items(physical_item_id,label_code,identity_status) VALUES (?,NULL,?)',(item_id,'unresolved'))
            self.store.execute('''INSERT INTO stage4_company_items(physical_item_id,initial_title,purchase_cost_yen,
                created_at,actor_user_id,evidence_reference) VALUES (?,?,?,?,?,?)''',(item_id,title,cost,when,actor.user_id,evidence))
            party=self.store.registry._party('kaika')
            for role in ('owner','custodian','sales_operator','settlement_party'):
                self.store.registry._assign(item_id,role,party,when,evidence,source_key+':'+role)
            return {'physical_item_id':item_id,'title':title,'execution_mode':'test','live_execution_enabled':False}
        return self.store.operation(actor,source_key,payload,create,staff_required=True)

    def revise(self,actor,item_id,*,title,description,condition,price_yen,photo_refs=None,occurred_at,evidence_reference,source_key):
        price=yen(price_yen)
        if price<=0:
            raise RegistryError('catalog_positive_price_required')
        when,evidence=self._when(occurred_at),_text(evidence_reference,'evidence')
        title,description,condition=_text(title,'title',300),_text(description,'description',10000),_text(condition,'condition',2000)
        refs=list(photo_refs or [])
        payload=dict(action='catalog_revise',item=item_id,title=title,description=description,condition=condition,price=price,photos=refs,when=when,evidence=evidence)
        def create():
            company=self._company(item_id)
            self.store.lock_item(item_id)
            if not self._owner_is_company(item_id):
                raise RegistryError('catalog_company_ownership_required',status=409)
            if when<company['created_at']:
                raise RegistryError('catalog_event_precedes_item',status=409)
            photos=self._check_photos(item_id,refs)
            no=self.store.query('SELECT COALESCE(MAX(revision_no),0)+1 AS n FROM stage4_catalog_revisions WHERE physical_item_id=?',(item_id,))[0]['n']
            revision='cr_'+uuid.uuid4().hex
            self.store.execute('''INSERT INTO stage4_catalog_revisions(revision_id,physical_item_id,revision_no,title,
                description,item_condition,price_yen,photo_refs_json,created_at,actor_user_id,evidence_reference)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)''',(revision,item_id,no,title,description,condition,price,_json(photos),when,actor.user_id,evidence))
            return self._offer(self._revision(item_id,revision))
        return self.store.operation(actor,source_key,payload,create,staff_required=True)

    def publish(self,actor,item_id,revision_id,*,occurred_at,evidence_reference,source_key):
        when,evidence=self._when(occurred_at),_text(evidence_reference,'evidence')
        payload=dict(action='publish_test',item=item_id,revision=revision_id,when=when,evidence=evidence)
        def create():
            self.store.lock_item(item_id)
            row=self._revision(item_id,revision_id)
            if not self._owner_is_company(item_id):
                raise RegistryError('catalog_company_ownership_required',status=409)
            if when<row['created_at']:
                raise RegistryError('catalog_event_precedes_revision',status=409)
            if not self._check_photos(item_id,json.loads(row['photo_refs_json'])):
                raise RegistryError('catalog_photo_required',status=409)
            self.store.execute('''INSERT INTO stage4_catalog_publications(physical_item_id,revision_id,published_at) VALUES (?,?,?)
                ON CONFLICT(physical_item_id) DO UPDATE SET revision_id=excluded.revision_id,published_at=excluded.published_at''',(item_id,revision_id,when))
            self._event(actor,item_id,'published',when,evidence,details={'revision_id':revision_id})
            return self.get_catalog(actor,item_id)
        return self.store.operation(actor,source_key,payload,create,staff_required=True)

    def list_catalog(self,actor):
        self.store.require_ready()
        self.store.authorize(actor)
        ids=self.store.query('SELECT physical_item_id FROM stage4_catalog_publications ORDER BY published_at,physical_item_id')
        return [self.get_catalog(actor,row['physical_item_id']) for row in ids if self._owner_is_company(row['physical_item_id'])]

    def get_company_item(self,actor,item_id):
        self.store.authorize(actor,staff_required=True)
        company=self._company(item_id)
        revisions=self.store.query('SELECT * FROM stage4_catalog_revisions WHERE physical_item_id=? ORDER BY revision_no DESC',(item_id,))
        return dict(company)|{'revisions':[self._offer(row) for row in revisions],
            'company_owned':self._owner_is_company(item_id),'live_execution_enabled':False}

    def list_company_items(self,actor):
        self.store.authorize(actor,staff_required=True)
        return [self.get_company_item(actor,row['physical_item_id']) for row in
                self.store.query('SELECT physical_item_id FROM stage4_company_items ORDER BY created_at DESC')]

    def get_catalog(self,actor,item_id):
        self.store.require_ready()
        self.store.authorize(actor)
        rows=self.store.query('SELECT r.* FROM stage4_catalog_publications p JOIN stage4_catalog_revisions r ON r.revision_id=p.revision_id WHERE p.physical_item_id=?',(item_id,))
        if not rows or not self._owner_is_company(item_id):
            raise RegistryError('catalog_item_not_found',status=404)
        claim=self.store.query('SELECT order_id FROM stage4_purchase_claims WHERE physical_item_id=?',(item_id,))
        return self._offer(rows[0])|{'available_count':0 if claim else 1,'execution_mode':'test','live_execution_enabled':False}

    def _order(self,order_id):
        rows=self.store.query('SELECT * FROM stage4_purchase_orders WHERE order_id=?',(order_id,))
        if not rows:
            raise RegistryError('catalog_order_not_found',status=404)
        return rows[0]

    def _status(self,order_id):
        return self.store.query('SELECT kind,details_json,occurred_at FROM stage4_catalog_events WHERE order_id=? ORDER BY event_no DESC',(order_id,))[0]

    def get_order(self,actor,order_id):
        self.store.require_ready()
        row=self._order(order_id)
        self.store.authorize(actor,customer_id=row['customer_id'])
        state=self._status(order_id)
        # No assignment history, staff evidence, source IDs, company cost or
        # other customers' orders are joined into this customer projection.
        return {key:row[key] for key in ('order_id','physical_item_id','revision_id','customer_id','amount_yen','custody_choice','consent_reference','created_at','expires_at')}|{
            'status':state['kind'],'offer':self._offer(self._revision(row['physical_item_id'],row['revision_id'])),
            'expired_pending_release':state['kind']=='reserved' and row['expires_at']<=_time(self.clock()),
            'ownership_rule':self.ownership_rule,'execution_mode':'test','live_execution_enabled':False}

    def list_orders(self,actor,customer_id=None):
        self.store.require_ready()
        staff=self.store.authorize(actor,customer_id=customer_id)
        if not staff:
            customer_id=actor.user_id
        rows=self.store.query('SELECT order_id FROM stage4_purchase_orders'+(' WHERE customer_id=?' if customer_id is not None else '')+' ORDER BY created_at DESC',
            (customer_id,) if customer_id is not None else ())
        return [self.get_order(actor,row['order_id']) for row in rows]

    def reserve(self,actor,item_id,revision_id,*,custody_choice,consent_reference,occurred_at,source_key):
        if custody_choice not in ('kaika','self'):
            raise RegistryError('catalog_custody_choice_required')
        customer_id=actor.user_id
        self.store.registry._customer_exists(customer_id)
        when,consent=self._when(occurred_at),_text(consent_reference,'consent_reference')
        payload=dict(action='reserve_test',item=item_id,revision=revision_id,custody=custody_choice,consent=consent,when=when)
        def create():
            self.store.lock_item(item_id)
            offer=self.get_catalog(actor,item_id)
            if offer['revision_id']!=revision_id:
                raise RegistryError('catalog_offer_changed', '商品条件が変更されました。内容を確認して選び直してください。',409)
            if offer['available_count']!=1:
                raise RegistryError('catalog_item_unavailable',status=409)
            revision=self._revision(item_id,revision_id)
            if when<revision['created_at']:
                raise RegistryError('catalog_event_precedes_revision',status=409)
            order_id='co_'+uuid.uuid4().hex
            expires=_time(datetime.fromisoformat(_time(self.clock()))+timedelta(minutes=30))
            if when>=expires:
                raise RegistryError('catalog_reservation_time_invalid')
            order={'physical_item_id':item_id,'customer_id':customer_id,'amount_yen':offer['price_yen']}
            reserve_id=self._fund(actor,order,'reserve',when,'stage4:'+order_id+':reserve',consent)
            self.store.execute('''INSERT INTO stage4_purchase_orders(order_id,physical_item_id,revision_id,customer_id,amount_yen,
                custody_choice,consent_reference,created_at,expires_at,reservation_record_id) VALUES (?,?,?,?,?,?,?,?,?,?)''',
                (order_id,item_id,revision_id,customer_id,offer['price_yen'],custody_choice,consent,when,expires,reserve_id))
            self.store.execute('INSERT INTO stage4_purchase_claims(physical_item_id,order_id) VALUES (?,?)',(item_id,order_id))
            self._event(actor,item_id,'reserved',when,consent,order_id=order_id,details={'reservation_record_id':reserve_id})
            return self.get_order(actor,order_id)
        return self.store.operation(actor,source_key,payload,create,customer_id=customer_id)

    def _release(self,actor,row,kind,when,evidence):
        if self._status(row['order_id'])['kind']!='reserved':
            raise RegistryError('catalog_order_not_reserved',status=409)
        release_id=self._fund(actor,row,'release',when,'stage4:'+row['order_id']+':release',evidence,row['reservation_record_id'])
        self.store.execute('DELETE FROM stage4_purchase_claims WHERE physical_item_id=? AND order_id=?',(row['physical_item_id'],row['order_id']))
        self._event(actor,row['physical_item_id'],kind,when,evidence,order_id=row['order_id'],details={'release_record_id':release_id})

    def cancel(self,actor,order_id,*,occurred_at,evidence_reference,source_key):
        row=self._order(order_id)
        when,evidence=self._when(occurred_at),_text(evidence_reference,'evidence')
        def create():
            self.store.lock_item(row['physical_item_id'])
            self._release(actor,row,'cancelled',when,evidence)
            return self.get_order(actor,order_id)
        return self.store.operation(actor,source_key,dict(action='cancel',order=order_id,when=when,evidence=evidence),create,customer_id=row['customer_id'])

    def expire_orders(self,actor,*,occurred_at,evidence_reference,source_key):
        when,evidence=self._when(occurred_at),_text(evidence_reference,'evidence')
        def create():
            rows=self.store.query('SELECT o.* FROM stage4_purchase_orders o JOIN stage4_purchase_claims c ON c.order_id=o.order_id WHERE o.expires_at<=?',(_time(self.clock()),))
            released=[]
            for row in rows:
                if self._status(row['order_id'])['kind']=='reserved':
                    self.store.finance.customer_lock(row['customer_id'])
                    self.store.lock_item(row['physical_item_id'])
                    self._release(actor,row,'expired',when,evidence)
                    released.append(row['order_id'])
            return {'released_order_ids':released,'execution_mode':'test','live_execution_enabled':False}
        return self.store.operation(actor,source_key,dict(action='expire',when=when,evidence=evidence),create,staff_required=True,finance=True)

    def _transfer(self,actor,row,when,evidence):
        item_id=row['physical_item_id']
        if not self._owner_is_company(item_id) or self.store.query('SELECT physical_item_id FROM business_item_profiles WHERE physical_item_id=?',(item_id,)):
            raise RegistryError('catalog_ownership_conflict',status=409)
        revision=self._revision(item_id,row['revision_id'])
        owner=self.store.registry._roles(item_id)['owner']
        if when<owner['started_at']:
            raise RegistryError('catalog_transfer_time_invalid',status=409)
        self.store.execute('UPDATE business_item_assignments SET ended_at=? WHERE assignment_id=?',(when,owner['assignment_id']))
        party=self.store.registry._party('customer',row['customer_id'])
        self.store.registry._assign(item_id,'owner',party,when,evidence,'stage4:'+row['order_id']+':owner')
        settlement=self.store.registry._roles(item_id).get('settlement_party')
        if settlement:
            self.store.execute('UPDATE business_item_assignments SET ended_at=? WHERE assignment_id=?',(when,settlement['assignment_id']))
        self.store.registry._assign(item_id,'settlement_party',party,when,evidence,'stage4:'+row['order_id']+':settlement')
        # The actual object remains with Kaika even when the buyer requests
        # delivery home. The handover must be separately evidenced.
        self.store.execute('''INSERT INTO business_item_profiles(physical_item_id,title,origin,created_at,initial_customer_id,
            imported_fields_json,physical_state,workflow_state,received_at) VALUES (?,?,?,?,?,?,?,?,?)''',
            (item_id,revision['title'],'manual',when,row['customer_id'],_json({'purchase_price':row['amount_yen'],'store_name':'開花（検証用購入）'}),'received','unreviewed',when))
        # Company stock photos are carried by the immutable purchase revision.
        # No new work, registration count, sold/shipped event or charge is made.

    def confirm_test_purchase(self,actor,order_id,*,occurred_at,evidence_reference,source_key):
        row=self._order(order_id)
        when,evidence=self._when(occurred_at),_text(evidence_reference,'evidence')
        def create():
            self.store.lock_item(row['physical_item_id'])
            state=self._status(order_id)
            if when<state['occurred_at']:
                raise RegistryError('catalog_time_reversed',status=409)
            if state['kind']=='paid_ownership_pending' and self.ownership_rule=='test_payment_confirmation':
                self._transfer(actor,row,when,evidence)
                self._event(actor,row['physical_item_id'],'confirmed',when,evidence,order_id=order_id,
                    details={'ownership_rule':self.ownership_rule,'prior_event_kind':'paid_ownership_pending'})
                return self.get_order(actor,order_id)
            if state['kind']!='reserved':
                raise RegistryError('catalog_order_not_reserved',status=409)
            if row['expires_at']<=_time(self.clock()):
                raise RegistryError('catalog_reservation_expired',status=409)
            if not self._owner_is_company(row['physical_item_id']):
                raise RegistryError('catalog_company_ownership_required',status=409)
            purchase_id=self._fund(actor,row,'purchase',when,'stage4:'+order_id+':purchase',evidence,row['reservation_record_id'])
            status='paid_ownership_pending'
            if self.ownership_rule=='test_payment_confirmation':
                self._transfer(actor,row,when,evidence)
                status='confirmed'
            self._event(actor,row['physical_item_id'],status,when,evidence,order_id=order_id,
                details={'purchase_record_id':purchase_id,'ownership_rule':self.ownership_rule})
            return self.get_order(actor,order_id)
        return self.store.operation(actor,source_key,dict(action='confirm_test',order=order_id,when=when,evidence=evidence,ownership_rule=self.ownership_rule),
            create,customer_id=row['customer_id'],staff_required=True,finance=True)

    def handover_received(self,actor,order_id,*,occurred_at,evidence_reference,source_key):
        """Record evidenced customer receipt, never equate a request to custody."""
        row=self._order(order_id)
        when,evidence=self._when(occurred_at),_text(evidence_reference,'evidence')
        def create():
            self.store.lock_item(row['physical_item_id'])
            state=self._status(order_id)
            if row['custody_choice']!='self' or state['kind']!='confirmed':
                raise RegistryError('catalog_handover_not_ready',status=409)
            if when<state['occurred_at']:
                raise RegistryError('catalog_handover_time_invalid',status=409)
            profile=self.store.registry.detail(actor,row['physical_item_id'])
            latest=self.store.query('SELECT last_event_at FROM business_item_profiles WHERE physical_item_id=?',(row['physical_item_id'],))[0]['last_event_at']
            if latest and when<latest:
                raise RegistryError('catalog_handover_time_invalid',status=409)
            if profile['owner_user_id']!=row['customer_id'] or profile['custodian']!='kaika' or profile['held'] or profile['sales_state']!='unlisted':
                raise RegistryError('catalog_handover_item_changed',status=409)
            roles=self.store.registry._roles(row['physical_item_id'])
            party=self.store.registry._party('customer',row['customer_id'])
            for role in ('custodian','sales_operator'):
                previous=roles.get(role)
                if previous:
                    self.store.execute('UPDATE business_item_assignments SET ended_at=? WHERE assignment_id=?',(when,previous['assignment_id']))
                self.store.registry._assign(row['physical_item_id'],role,party,when,evidence,'stage4:'+order_id+':handover:'+role)
            self.store.execute("UPDATE business_item_profiles SET physical_state='with_customer',last_event_at=? WHERE physical_item_id=?",(when,row['physical_item_id']))
            self._event(actor,row['physical_item_id'],'handed_over',when,evidence,order_id=order_id,details={'custodian':'customer'})
            return self.get_order(actor,order_id)
        return self.store.operation(actor,source_key,dict(action='handover_received',order=order_id,when=when,evidence=evidence),create,
            customer_id=row['customer_id'],staff_required=True)
