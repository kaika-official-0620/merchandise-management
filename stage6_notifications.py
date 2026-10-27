"""Private, consented notification history and controlled test delivery outbox.

The caller supplies an explicitly migrated Store. Network I/O occurs only in
explicit dispatch/reconcile through enabled, allowlisted test transports.
No environment discovery, scheduler, schema creation or billing mutation occurs.
"""
from datetime import datetime, timezone, timedelta
import hashlib
import json
import re
import secrets
import time
import uuid
from urllib.parse import urlencode

from item_registry import RegistryError, _json, _text
from stage6_transports import DeliveryResult, MemoryNotificationTransport, PUSH_TOKEN, LINE_USER, verified_line_events
from stage6_external_transports import EXTERNAL_TRANSPORT_TYPES

CHANNELS = ('push', 'email', 'line')
EVENT_TEXT = {
    'listing_approval': ('出品案をご確認ください', '確認が必要な出品案があります。ログインして内容をご確認ください。'),
    'storage_pre_due': ('お預かり期限が近づいています', 'お預かり商品の今後の対応を、ログインしてご確認ください。'),
    'storage_due': ('お預かり期限をご確認ください', '期限に関する確認事項があります。ログインして今後の対応をお選びください。'),
    'invoice_draft': ('料金明細をご確認ください', '検証用の料金明細があります。実際のお支払い依頼ではありません。'),
    'payment_failed': ('支払い状況をご確認ください', '検証用の支払い状況に確認事項があります。実際の請求結果ではありません。'),
    'return_selection': ('商品の対応方法をご確認ください', '商品の返送など、選択が必要な内容があります。ログインしてご確認ください。'),
}
SOURCE_KEYS = ('customer_id', 'event_type', 'source_type', 'source_id', 'source_version', 'dedupe_key',
               'physical_item_id', 'ownership_assignment_id', 'path', 'execution_mode', 'external_delivery_allowed')
SAFE_PATH = re.compile(r'^/(?:home|notifications(?:/[a-f0-9]{32}|/items/[A-Za-z0-9_-]+/choice)?|inventory(?:/[A-Za-z0-9_/-]+)?|commerce(?:/[A-Za-z0-9_/-]+)?|finance(?:/[A-Za-z0-9_/-]+)?|payments(?:/[A-Za-z0-9_/-]+)?)$')
EMAIL = re.compile(r'^[A-Za-z0-9.!#$%&\x27*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?\.[A-Za-z]{2,63}$')


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def identifier():
    return uuid.uuid4().hex


def suppress_customer_notification_jobs(store,customer_id,reason,*,revoke=False,now=None):
    """Lifecycle transaction hook. Caller owns authorization/commit; no I/O.

    Pending requests pause queued work without deleting preference/history.
    Closure additionally retires destinations and all confirmation capabilities.
    An already accepted or in-flight message cannot be recalled by deletion.
    """
    if type(customer_id) is not int or customer_id<=0 or reason not in ('account_deletion_pending','account_closed'):
        raise ValueError('Explicit lifecycle customer and reason required')
    now=int(time.time()) if now is None else int(now)
    rows=store.query("SELECT delivery_id,state FROM stage6_notification_outbox WHERE customer_id=? AND state IN ('queued','claimed','failed','sending')",(customer_id,))
    for row in rows:
        state='unknown' if row['state']=='sending' else 'suppressed'
        store.execute('UPDATE stage6_notification_outbox SET state=?,claim_hash=NULL,outcome_code=? WHERE delivery_id=?',(state,reason,row['delivery_id']))
        store.execute('INSERT INTO stage6_delivery_audit VALUES(?,?,?,?,?)',(identifier(),row['delivery_id'],state,reason,now))
    if revoke:
        store.execute('UPDATE stage6_line_invitations SET consumed_at=COALESCE(consumed_at,?) WHERE line_user_id IN (SELECT address FROM stage6_destinations WHERE customer_id=? AND channel=\'line\' UNION SELECT address FROM stage6_destination_proofs WHERE customer_id=? AND channel=\'line\')',(now,customer_id,customer_id))
        store.execute('UPDATE stage6_destinations SET revoked_at=COALESCE(revoked_at,?) WHERE customer_id=?',(now,customer_id))
        store.execute('UPDATE stage6_destination_proofs SET consumed_at=COALESCE(consumed_at,?) WHERE customer_id=?',(now,customer_id))
    return {'paused_deliveries':len(rows),'destinations_revoked':revoke}


class NotificationService:
    def __init__(self, store, transports=None, clock=None, validator=None, destination_validator=None,
                 monthly_limits=None, origin='https://notification-preview.invalid', customer_active=None):
        self.store = store
        self.transports = transports or {}
        self.clock = clock or time.time
        self.validator = validator
        self.destination_validator = destination_validator
        self.monthly_limits = monthly_limits or {}
        self.customer_active = customer_active
        # Verification URL base is server configuration, never a user return URL.
        if not re.fullmatch(r'https://[A-Za-z0-9.-]+(?::[0-9]+)?', origin):
            raise ValueError('HTTPS origin required')
        self.origin = origin

    def now(self):
        return int(self.clock())

    def _op(self, actor, key, payload, callback, customer_id=None, staff=False):
        return self.store.operation(actor, key, payload, callback, customer_id=customer_id, staff_required=staff)

    def _self(self, actor):
        self.store.authorize(actor, actor.user_id)
        if not self._active(actor.user_id):
            raise RegistryError('notification_account_inactive',status=403)
        return actor.user_id

    def _active(self,customer_id):
        return self.customer_active is None or self.customer_active(customer_id) is True

    def _notification(self, actor, notification_id):
        rows = self.store.query('SELECT * FROM stage6_notifications WHERE notification_id=?', (notification_id,))
        if not rows:
            raise RegistryError('notification_not_found', status=404)
        self.store.authorize(actor, rows[0]['customer_id'])
        if rows[0]['event_kind'] in ('invoice_draft','payment_failed'):
            self.store.finance.authorize(actor,rows[0]['customer_id'])
        return rows[0]

    def _delivery(self, actor, delivery_id):
        rows = self.store.query('SELECT * FROM stage6_notification_outbox WHERE delivery_id=?', (delivery_id,))
        if not rows:
            raise RegistryError('notification_delivery_not_found', status=404)
        self.store.authorize(actor, rows[0]['customer_id'])
        if rows[0]['notification_id']:
            self._notification(actor,rows[0]['notification_id'])
        return rows[0]

    @staticmethod
    def _project_delivery(row):
        return {key: row.get(key) for key in ('delivery_id', 'notification_id', 'customer_id', 'channel', 'state',
            'attempts', 'due_at', 'outcome_code', 'test_only', 'created_at')}

    def inbox(self, actor, customer_id=None):
        customer_id = actor.user_id if customer_id is None else customer_id
        self.store.authorize(actor, customer_id)
        rows = self.store.query('SELECT * FROM stage6_notifications WHERE customer_id=? ORDER BY created_at DESC,notification_id', (customer_id,))
        result=[]
        for row in rows:
            try:
                result.append(self.get_notification(actor,row['notification_id']))
            except RegistryError as error:
                if error.code!='finance_permission_required':
                    raise
        return result

    def get_notification(self, actor, notification_id):
        row = self._notification(actor, notification_id)
        source = json.loads(row['source_json'])
        current = bool(self.validator and self.validator(actor, source))
        result = {key: row[key] for key in ('notification_id', 'customer_id', 'event_kind', 'title', 'body', 'created_at', 'read_at', 'test_only')}
        result.update(is_current=current, path=row['path'] if current else None,
                      deliveries=[self._project_delivery(x) for x in self.store.query('SELECT * FROM stage6_notification_outbox WHERE notification_id=?', (notification_id,))])
        return result

    def settings(self, actor):
        customer = self._self(actor)
        prefs = {kind: {channel: False for channel in CHANNELS} for kind in EVENT_TEXT}
        for row in self.store.query('SELECT * FROM stage6_notification_preferences WHERE customer_id=?', (customer,)):
            if row['event_kind'] in prefs:
                prefs[row['event_kind']][row['channel']] = bool(row['enabled'])
        destinations = [{key: row[key] for key in ('destination_id', 'channel', 'platform', 'verified_at', 'revoked_at')}
            for row in self.store.query('SELECT * FROM stage6_destinations WHERE customer_id=? ORDER BY verified_at DESC', (customer,))]
        return {'preferences': prefs, 'destinations': destinations, 'event_labels': {k:v[0] for k,v in EVENT_TEXT.items()},
                'external_delivery_enabled': any(type(adapter) in EXTERNAL_TRANSPORT_TYPES for adapter in self.transports.values()),
                'line_connection_ready': type(self.transports.get('line')) in EXTERNAL_TRANSPORT_TYPES, 'test_only': True}

    def set_preferences(self, actor, event_kind, channels, *, source_key):
        customer = self._self(actor)
        if event_kind not in EVENT_TEXT or not isinstance(channels, (list, tuple)) or len(set(channels)) != len(channels) or any(x not in CHANNELS for x in channels):
            raise RegistryError('notification_preferences_invalid')
        def action():
            for channel in CHANNELS:
                self.store.execute('''INSERT INTO stage6_notification_preferences(customer_id,event_kind,channel,enabled,updated_at)
                    VALUES (?,?,?,?,?) ON CONFLICT(customer_id,event_kind,channel) DO UPDATE SET enabled=excluded.enabled,updated_at=excluded.updated_at''',
                    (customer, event_kind, channel, int(channel in channels), self.now()))
            return {'updated': True}
        return self._op(actor, source_key, {'kind':event_kind,'channels':sorted(channels)}, action, customer)

    def mark_read(self, actor, notification_id, *, source_key):
        row = self._notification(actor, notification_id)
        # Staff may inspect customer work; only the customer marks their inbox read.
        if row['customer_id'] != actor.user_id:
            raise RegistryError('notification_owner_required', status=403)
        def action():
            self.store.execute('UPDATE stage6_notifications SET read_at=COALESCE(read_at,?) WHERE notification_id=?', (self.now(), notification_id))
            return {'read': True}
        return self._op(actor, source_key, {'read':notification_id}, action, actor.user_id)

    def publish_event(self, actor, event, *, source_key):
        if not isinstance(event, dict) or event.get('event_type') not in EVENT_TEXT or type(event.get('customer_id')) is not int:
            raise RegistryError('notification_event_invalid')
        source = {key: event.get(key) for key in SOURCE_KEYS}
        customer, kind = source['customer_id'], source['event_type']
        if not self.validator or not self.validator(actor, source):
            raise RegistryError('notification_source_not_current', status=409)
        key = _text(source['dedupe_key'], 'notification_event_key', 500)
        path = _text(source['path'], 'notification_path', 300)
        if not SAFE_PATH.fullmatch(path) or '..' in path:
            raise RegistryError('notification_path_invalid')
        def action():
            if not self.validator or not self.validator(actor,source):
                raise RegistryError('notification_source_not_current',status=409)
            old = self.store.query('SELECT notification_id,source_json FROM stage6_notifications WHERE customer_id=? AND dedupe_key=?', (customer, key))
            if old:
                if old[0]['source_json'] != _json(source):
                    raise RegistryError('notification_event_conflict', status=409)
                return {'notification_id': old[0]['notification_id'], 'created': False}
            notification_id = identifier()
            title, body = EVENT_TEXT[kind]
            self.store.execute('''INSERT INTO stage6_notifications(notification_id,customer_id,event_kind,dedupe_key,source_json,title,body,path,created_at,test_only)
                VALUES (?,?,?,?,?,?,?,?,?,1)''', (notification_id, customer, kind, key, _json(source), title, body, path, self.now()))
            destinations = self.store.query('''SELECT d.* FROM stage6_destinations d JOIN stage6_notification_preferences p
                ON p.customer_id=d.customer_id AND p.channel=d.channel WHERE d.customer_id=? AND d.revoked_at IS NULL AND p.event_kind=? AND p.enabled=1''', (customer,kind))
            for destination in destinations:
                self._enqueue(customer, destination['channel'], {}, notification_id=notification_id, destination_id=destination['destination_id'])
            return {'notification_id':notification_id, 'created':True}
        return self._op(actor, source_key, {'publish':source}, action, customer)

    def _enqueue(self, customer, channel, payload, notification_id=None, destination_id=None, proof_id=None):
        delivery_id = identifier()
        self.store.execute('''INSERT INTO stage6_notification_outbox(delivery_id,customer_id,notification_id,destination_id,proof_id,channel,
            private_payload_json,state,due_at,created_at,test_only) VALUES (?,?,?,?,?,?,?,'queued',?,?,1)''',
            (delivery_id,customer,notification_id,destination_id,proof_id,channel,_json(payload),self.now(),self.now()))
        self._audit(delivery_id,'queued')
        return delivery_id

    def _audit(self, delivery_id, state, code=None):
        self.store.execute('INSERT INTO stage6_delivery_audit(audit_id,delivery_id,state,outcome_code,recorded_at) VALUES (?,?,?,?,?)',
            (identifier(), delivery_id, state, code, self.now()))

    def _destination(self, customer, channel, address, **details):
        existing = self.store.query('SELECT * FROM stage6_destinations WHERE channel=? AND address_hash=? AND revoked_at IS NULL', (channel,digest(address)))
        if existing and existing[0]['customer_id'] != customer:
            raise RegistryError('notification_destination_in_use', status=409)
        if existing and channel != 'push':
            return existing[0]['destination_id']
        # A verified email/LINE replacement retires the previous recipient. Push
        # supports multiple installations but rotates the previous binding.
        if channel == 'push':
            self.store.execute('UPDATE stage6_destinations SET revoked_at=? WHERE channel=? AND (installation=? OR address_hash=?) AND revoked_at IS NULL',
                (self.now(),channel,details['installation'],digest(address)))
        else:
            self.store.execute('UPDATE stage6_destinations SET revoked_at=? WHERE customer_id=? AND channel=? AND revoked_at IS NULL', (self.now(),customer,channel))
        destination_id = identifier()
        self.store.execute('''INSERT INTO stage6_destinations(destination_id,customer_id,channel,address,address_hash,installation,binding_hash,platform,verified_at)
            VALUES (?,?,?,?,?,?,?,?,?)''', (destination_id,customer,channel,address,digest(address),details.get('installation'),details.get('binding_hash'),details.get('platform'),self.now()))
        return destination_id

    def register_push(self, actor, token, installation, binding, platform, *, source_key):
        customer = self._self(actor)
        if not isinstance(token,str) or not PUSH_TOKEN.fullmatch(token) or platform not in ('ios','android'):
            raise RegistryError('push_registration_invalid')
        installation, binding = _text(installation,'installation',200), _text(binding,'binding',200)
        if len(binding)<32 or len(installation)<16:
            raise RegistryError('push_binding_invalid')
        def action():
            # Session binding is asserted by the authenticated native bridge;
            # never called directly from an arbitrary destination form.
            current=self.store.query("SELECT * FROM stage6_destinations WHERE customer_id=? AND channel='push' AND address_hash=? AND installation=? AND binding_hash=? AND platform=? AND revoked_at IS NULL",
                (customer,digest(token),installation,digest(binding),platform))
            if current:
                return {'destination_id':current[0]['destination_id']}
            self.store.execute('UPDATE stage6_destinations SET revoked_at=? WHERE channel=\'push\' AND (installation=? OR address_hash=?) AND revoked_at IS NULL',
                (self.now(),installation,digest(token)))
            return {'destination_id':self._destination(customer,'push',token,installation=installation,binding_hash=digest(binding),platform=platform)}
        return self._op(actor,source_key,{'push':digest(token),'installation':installation,'binding':digest(binding),'platform':platform},action,customer)

    def revoke_push_binding(self, actor, binding, *, source_key):
        customer = self._self(actor)
        def action():
            self.store.execute("UPDATE stage6_destinations SET revoked_at=? WHERE customer_id=? AND channel='push' AND binding_hash=? AND revoked_at IS NULL", (self.now(),customer,digest(binding)))
            return {'revoked':True}
        return self._op(actor,source_key,{'revoke_binding':digest(binding)},action,customer)

    def revoke_push_installation(self, actor, installation, *, source_key):
        customer = self._self(actor)
        def action():
            self.store.execute("UPDATE stage6_destinations SET revoked_at=? WHERE customer_id=? AND channel='push' AND installation=? AND revoked_at IS NULL", (self.now(),customer,installation))
            return {'revoked':True}
        return self._op(actor,source_key,{'revoke_installation':installation},action,customer)

    def revoke_destination(self, actor, destination_id, *, source_key):
        customer = self._self(actor)
        def action():
            rows=self.store.query('SELECT * FROM stage6_destinations WHERE destination_id=? AND customer_id=?',(destination_id,customer))
            if not rows:
                raise RegistryError('notification_destination_not_found',status=404)
            self.store.execute('UPDATE stage6_destinations SET revoked_at=COALESCE(revoked_at,?) WHERE destination_id=?',(self.now(),destination_id))
            return {'revoked':True}
        return self._op(actor,source_key,{'revoke':destination_id},action,customer)

    def request_email_verification(self, actor, email, *, source_key):
        customer = self._self(actor)
        if not isinstance(email,str) or len(email)>254 or not EMAIL.fullmatch(email) or any(c in email for c in '\r\n'):
            raise RegistryError('email_address_invalid')
        email = email.rsplit('@',1)[0]+'@'+email.rsplit('@',1)[1].lower()
        def action():
            recent=self.store.query("SELECT COUNT(*) AS n FROM stage6_destination_proofs WHERE customer_id=? AND channel='email' AND created_at>?",(customer,self.now()-3600))[0]['n']
            if recent>=3:
                raise RegistryError('verification_rate_limit',status=429)
            self.store.execute("UPDATE stage6_destination_proofs SET consumed_at=? WHERE customer_id=? AND channel='email' AND consumed_at IS NULL",(self.now(),customer))
            proof_id,token=identifier(),secrets.token_urlsafe(32)
            self.store.execute('''INSERT INTO stage6_destination_proofs(proof_id,customer_id,channel,address,token_hash,expires_at,created_at)
                VALUES (?,?,'email',?,?,?,?)''',(proof_id,customer,email,digest(token),self.now()+1800,self.now()))
            # This private queue field is never included in a browser projection.
            self._enqueue(customer,'email',{'verification_url':self.origin+'/notifications/email/confirm#'+urlencode({'token':token})},proof_id=proof_id)
            return {'challenge_id':proof_id,'expires_at':self.now()+1800,'test_only':True}
        return self._op(actor,source_key,{'verify_email':digest(email)},action,customer)

    def confirm_email(self, actor, token, *, source_key):
        customer=self._self(actor)
        token=_text(token,'verification_token',200)
        def action():
            rows=self.store.query("SELECT * FROM stage6_destination_proofs WHERE token_hash=? AND customer_id=? AND channel='email'",(digest(token),customer))
            if not rows or rows[0]['consumed_at'] is not None or rows[0]['expires_at']<=self.now():
                raise RegistryError('verification_invalid_or_expired',status=409)
            row=rows[0]
            destination=self._destination(customer,'email',row['address'])
            self.store.execute('UPDATE stage6_destination_proofs SET consumed_at=? WHERE proof_id=?',(self.now(),row['proof_id']))
            return {'destination_id':destination,'verified':True}
        return self._op(actor,source_key,{'confirm_email':digest(token)},action,customer)

    def begin_line_link(self, actor, invitation_token, *, source_key, link_token=None, expected_customer_id=None):
        customer=self._self(actor)
        if expected_customer_id is not None and expected_customer_id!=customer:
            raise RegistryError('notification_test_recipient_not_approved',status=403)
        token=_text(invitation_token,'line_invitation',200)
        ephemeral={}
        def action():
            rows=self.store.query('SELECT * FROM stage6_line_invitations WHERE token_hash=?',(digest(token),))
            if not rows or rows[0]['consumed_at'] is not None or rows[0]['expires_at']<=self.now():
                raise RegistryError('line_invitation_invalid_or_expired',status=409)
            invitation=rows[0]
            if link_token is not None:
                bound=self.store.query('SELECT * FROM stage6_line_link_tokens WHERE invitation_id=? AND link_token_hash=?',
                    (invitation['invitation_id'],digest(_text(link_token,'line_link_token',1024))))
                if not bound or bound[0]['expires_at']<=self.now() or bound[0]['delivery_state'] not in ('accepted','unknown'):
                    raise RegistryError('line_link_token_invalid',status=409)
            nonce,proof=secrets.token_urlsafe(32),identifier()
            self.store.execute("INSERT INTO stage6_destination_proofs(proof_id,customer_id,channel,address,token_hash,expires_at,created_at) VALUES (?,?,'line',?,?,?,?)",
                (proof,customer,invitation['line_user_id'],digest(nonce),min(invitation['expires_at'],self.now()+600),self.now()))
            self.store.execute('UPDATE stage6_line_invitations SET consumed_at=? WHERE invitation_id=?',(self.now(),invitation['invitation_id']))
            # Internal redirect coordinator only. The LINE-provided linkToken
            # must be obtained/sent through official APIs before browser use.
            ephemeral['nonce']=nonce
            return {'proof_id':proof,'external_link_ready':False}
        result=self._op(actor,source_key,{'begin_line':digest(token),'link_token':digest(link_token) if link_token else None},action,customer)
        # Never persist the nonce in the immutable operation audit. Lost first
        # responses cannot replay a secret from that audit; request a new LINE
        # invitation and restart the official account-link flow instead.
        return dict(result,**ephemeral,resume_requires_new_invitation=not bool(ephemeral))

    def prepare_line_token(self,actor,invitation_token,line_user,link_token,*,source_key):
        def action():
            limit=self.monthly_limits.get('line')
            if type(limit) is not int or limit<0:
                raise RegistryError('notification_budget_unconfirmed',status=409)
            if self.usage(actor)['line']['reserved_or_attempted']>=limit:
                raise RegistryError('notification_monthly_limit',status=429)
            rows=self.store.query('SELECT * FROM stage6_line_invitations WHERE token_hash=? AND line_user_id=?',
                (digest(invitation_token),line_user))
            if not rows or rows[0]['consumed_at'] is not None or rows[0]['expires_at']<=self.now():
                raise RegistryError('line_invitation_invalid_or_expired',status=409)
            row=rows[0]
            self.store.execute("INSERT INTO stage6_line_link_tokens VALUES(?,?,?,?,'prepared')",
                (row['invitation_id'],digest(_text(link_token,'line_link_token',1024)),self.now(),row['expires_at']))
            return {'invitation_id':row['invitation_id']}
        return self._op(actor,source_key,{'line_token_for':digest(invitation_token),'token_hash':digest(link_token)},action,staff=True)

    def record_line_invitation_result(self,actor,invitation_id,result,*,source_key):
        state=result.state if isinstance(result,DeliveryResult) and result.state in ('accepted','failed','unknown') else 'unknown'
        def action():
            self.store.execute("UPDATE stage6_line_link_tokens SET delivery_state=? WHERE invitation_id=? AND delivery_state='prepared'",(state,invitation_id))
            return {'state':state}
        return self._op(actor,source_key,{'line_invitation_result':invitation_id,'state':state},action,staff=True)

    def line_invitation_customer(self,actor,invitation_token,policy):
        self._self(actor)
        rows=self.store.query('SELECT line_user_id FROM stage6_line_invitations WHERE token_hash=?',(digest(invitation_token),))
        if not rows:raise RegistryError('line_invitation_invalid_or_expired',status=409)
        return policy.customer_for('line',rows[0]['line_user_id'])

    def receive_line_webhook(self, actor, raw_body, signature, channel_secret, *, source_key, invitation_allowed=None, line_customer_for=None):
        self.store.authorize(actor,staff_required=True)
        events=verified_line_events(raw_body,signature,channel_secret)
        request_hash=hashlib.sha256(raw_body).hexdigest()
        internal_invitations=[]
        def action():
            for event in events:
                if not isinstance(event,dict):
                    raise RegistryError('line_payload_invalid')
                event_id=_text(event.get('webhookEventId'),'line_event_id',200)
                # Redelivery may change only deliveryContext; business content
                # under an already-seen provider ID must remain identical.
                event_hash=digest(_json({key:value for key,value in event.items() if key!='deliveryContext'}))
                previous=self.store.query('SELECT request_hash FROM stage6_line_webhooks WHERE webhook_event_id=?',(event_id,))
                if previous:
                    if previous[0]['request_hash']!=event_hash:
                        raise RegistryError('line_event_conflict',status=409)
                    continue
                user=event.get('source',{}).get('userId','')
                if not isinstance(user,str) or not LINE_USER.fullmatch(user):
                    raise RegistryError('line_user_invalid')
                kind=event.get('type')
                if kind=='accountLink':
                    link=event.get('link',{})
                    nonce=link.get('nonce')
                    if link.get('result')!='ok' or not isinstance(nonce,str):
                        raise RegistryError('line_link_failed',status=409)
                    proofs=self.store.query("SELECT * FROM stage6_destination_proofs WHERE channel='line' AND token_hash=?",(digest(nonce),))
                    if not proofs or proofs[0]['address']!=user or proofs[0]['consumed_at'] is not None or proofs[0]['expires_at']<=self.now():
                        raise RegistryError('line_link_proof_invalid',status=409)
                    proof=proofs[0]
                    if line_customer_for is not None and line_customer_for(user)!=proof['customer_id']:
                        raise RegistryError('notification_test_recipient_not_approved',status=403)
                    if not self._active(proof['customer_id']):
                        raise RegistryError('notification_account_inactive',status=403)
                    self._destination(proof['customer_id'],'line',user)
                    self.store.execute('UPDATE stage6_destination_proofs SET consumed_at=? WHERE proof_id=?',(self.now(),proof['proof_id']))
                elif kind in ('follow','message'):
                    # Bot coordinator must issue official linkToken and privately
                    # send this unpredictable invitation to this same LINE user.
                    recent=self.store.query('SELECT COUNT(*) AS n FROM stage6_line_invitations WHERE line_user_id=? AND expires_at>?',
                        (user,self.now()-3000))[0]['n']
                    if recent<3 and (invitation_allowed is None or invitation_allowed(event)):
                        invitation=secrets.token_urlsafe(32)
                        self.store.execute('INSERT INTO stage6_line_invitations(invitation_id,webhook_event_id,line_user_id,token_hash,expires_at) VALUES (?,?,?,?,?)',
                            (identifier(),event_id,user,digest(invitation),self.now()+600))
                        internal_invitations.append({'line_user_id':user,'invitation_token':invitation,'webhook_event_id':event_id})
                elif kind=='unfollow':
                    self.store.execute("UPDATE stage6_destinations SET revoked_at=? WHERE channel='line' AND address_hash=? AND revoked_at IS NULL",(self.now(),digest(user)))
                self.store.execute('INSERT INTO stage6_line_webhooks(webhook_event_id,request_hash,recorded_at) VALUES (?,?,?)',(event_id,event_hash,self.now()))
            return {'accepted':True,'invitation_count':len(internal_invitations)}
        result=self._op(actor,source_key,{'line_webhook':request_hash},action,staff=True)
        # The bot coordinator receives invitation tokens only on first handling.
        # They are excluded from operation/audit results, public HTTP responses,
        # and projections. Redelivery does not recover these secrets.
        return dict(result,internal_invitations=internal_invitations,
            invitation_replay_unavailable=bool(result['invitation_count'] and not internal_invitations))

    def deliveries(self, actor, customer_id=None):
        staff=self.store.authorize(actor,customer_id)
        if customer_id is None and not staff:
            customer_id=actor.user_id
        rows=self.store.query('SELECT * FROM stage6_notification_outbox'+(' WHERE customer_id=?' if customer_id is not None else '')+' ORDER BY created_at DESC',
            (customer_id,) if customer_id is not None else ())
        result=[]
        for row in rows:
            try:
                self._delivery(actor,row['delivery_id'])
                result.append(self._project_delivery(row))
            except RegistryError as error:
                if error.code!='finance_permission_required':
                    raise
        return result

    def _eligible(self, actor, row):
        if not self._active(row['customer_id']):
            return False
        if row['proof_id']:
            proof=self.store.query('SELECT * FROM stage6_destination_proofs WHERE proof_id=?',(row['proof_id'],))[0]
            return proof['consumed_at'] is None and proof['expires_at']>self.now()
        destination=self.store.query('SELECT * FROM stage6_destinations WHERE destination_id=?',(row['destination_id'],))[0]
        notification=self._notification(actor,row['notification_id'])
        preference=self.store.query('SELECT enabled FROM stage6_notification_preferences WHERE customer_id=? AND channel=? AND event_kind=?',
            (row['customer_id'],row['channel'],notification['event_kind']))
        if destination['revoked_at'] is not None or destination['customer_id']!=row['customer_id'] or not preference or not preference[0]['enabled']:
            return False
        if row['channel']=='push' and (not self.destination_validator or not self.destination_validator(destination)):
            return False
        return bool(self.validator and self.validator(actor,json.loads(notification['source_json'])))

    def _month_start(self):
        now=datetime.fromtimestamp(self.now(),timezone(timedelta(hours=9)))
        return int(now.replace(day=1,hour=0,minute=0,second=0,microsecond=0).timestamp())

    def usage(self, actor):
        self.store.authorize(actor,staff_required=True)
        start=self._month_start()
        result={}
        for channel in CHANNELS:
            rows=self.store.query('''SELECT COUNT(*) AS n FROM stage6_notification_outbox o
                JOIN stage6_delivery_audit a ON a.delivery_id=o.delivery_id
                WHERE o.channel=? AND a.recorded_at>=? AND a.state='claimed' ''',(channel,start))
            invitations=self.store.query('SELECT COUNT(*) AS n FROM stage6_line_link_tokens WHERE created_at>=?',(start,))[0]['n'] if channel=='line' else 0
            result[channel]={'reserved_or_attempted':rows[0]['n']+invitations,'test_monthly_limit':self.monthly_limits.get(channel),
                'estimated_cost_yen':None,'cost_status':'provider_plan_unconfirmed'}
        return result

    def claim(self, actor, delivery_id, *, source_key):
        row=self._delivery(actor,delivery_id)
        self.store.authorize(actor,staff_required=True)
        ephemeral={}
        def action():
            current=self._delivery(actor,delivery_id)
            if current['state']!='queued' or current['due_at']>self.now():
                raise RegistryError('notification_not_claimable',status=409)
            if not self._eligible(actor,current):
                self.store.execute("UPDATE stage6_notification_outbox SET state='suppressed',outcome_code='destination_or_source_changed' WHERE delivery_id=?",(delivery_id,))
                self._audit(delivery_id,'suppressed','destination_or_source_changed')
                return {'delivery_id':delivery_id,'state':'suppressed'}
            limit=self.monthly_limits.get(current['channel'])
            if type(limit) is not int or limit<0:
                raise RegistryError('notification_budget_unconfirmed',status=409)
            if self.usage(actor)[current['channel']]['reserved_or_attempted']>=limit:
                raise RegistryError('notification_monthly_limit',status=429)
            claim_token=secrets.token_urlsafe(32)
            self.store.execute("UPDATE stage6_notification_outbox SET state='claimed',claim_hash=?,claimed_at=? WHERE delivery_id=?",(digest(claim_token),self.now(),delivery_id))
            self._audit(delivery_id,'claimed')
            ephemeral['claim_token']=claim_token
            return {'delivery_id':delivery_id,'state':'claimed'}
        result=self._op(actor,source_key,{'claim':delivery_id},action,row['customer_id'],True)
        # Keep worker capabilities out of the immutable audit as well. A lost
        # first response must use lease recovery, never replay an old capability.
        return dict(result,**ephemeral,resume_requires_recovery=result['state']=='claimed' and not bool(ephemeral))

    def _request(self, row):
        request={'delivery_id':row['delivery_id'],'notification_id':row['notification_id'],'channel':row['channel'],
            'customer_id':row['customer_id'],'test_only':True}
        if row['proof_id']:
            proof=self.store.query('SELECT * FROM stage6_destination_proofs WHERE proof_id=?',(row['proof_id'],))[0]
            request.update(recipient=proof['address'],title='通知先メールアドレスの確認',body='この操作に心当たりがある場合だけ、ログインして通知先を確認してください。',
                **json.loads(row['private_payload_json']))
        else:
            dest=self.store.query('SELECT * FROM stage6_destinations WHERE destination_id=?',(row['destination_id'],))[0]
            note=self.store.query('SELECT * FROM stage6_notifications WHERE notification_id=?',(row['notification_id'],))[0]
            request.update(recipient=dest['address'],title=note['title'],body=note['body'],
                notification_url=self.origin+'/notifications/'+note['notification_id'])
        request['title']='【開花・検証】'+request['title']
        return request

    def _require_idle_connection(self):
        # Dedicated worker only. Never commit a caller's pending business write.
        if self.store.dialect=='sqlite':
            idle=not self.store.connection.in_transaction
        else:
            idle=self.store.connection.get_transaction_status()==0
        if not idle:
            raise RegistryError('notification_claim_commit_required',status=409)

    def _workflow_lock(self):
        self.store.execute("INSERT INTO stage4_locks(lock_key) VALUES ('workflow') ON CONFLICT(lock_key) DO NOTHING")
        self.store.execute("UPDATE stage4_locks SET lock_version=lock_version WHERE lock_key='workflow'")

    def _adapter(self,row,request):
        adapter=self.transports.get(row['channel'])
        if type(adapter) is MemoryNotificationTransport and not adapter.network_enabled and adapter.mode=='test':
            return adapter
        if type(adapter) not in EXTERNAL_TRANSPORT_TYPES or self.customer_active is None:
            raise RegistryError('external_notification_delivery_disabled',status=409)
        adapter.validate(request)
        return adapter

    def dispatch_claim(self, actor, delivery_id, claim_token):
        """Dedicated worker only: commits sending BEFORE touching transport.

        Re-entry from sending/unknown never sends again. Both DBs serialize the
        state transition and COMMIT before the explicit test transport is called.
        """
        self._require_idle_connection()
        self.store.authorize(actor,staff_required=True)
        suppressed=False
        with self.store.atomic():
            self._workflow_lock()
            row=self._delivery(actor,delivery_id)
            if row['state']!='claimed' or not isinstance(claim_token,str) or row['claim_hash']!=digest(claim_token):
                raise RegistryError('notification_claim_invalid',status=409)
            if not self._eligible(actor,row):
                self.store.execute("UPDATE stage6_notification_outbox SET state='suppressed',outcome_code='destination_or_source_changed' WHERE delivery_id=?",(delivery_id,))
                self._audit(delivery_id,'suppressed','destination_or_source_changed')
                suppressed=True
            else:
                request=self._request(row)
                adapter=self._adapter(row,request)
                self.store.execute("UPDATE stage6_notification_outbox SET state='sending',attempts=attempts+1 WHERE delivery_id=?",(delivery_id,))
                self._audit(delivery_id,'sending')
        self.store.connection.commit()
        if suppressed:
            return {'delivery_id':delivery_id,'state':'suppressed'}
        try:
            result=adapter.send(request)
        except Exception:
            # Even timeout can follow provider acceptance. No blind re-send.
            result=DeliveryResult('unknown',code='transport_result_unknown')
        return self._record_result(actor,delivery_id,result)

    def _record_result(self, actor, delivery_id, result):
        if not isinstance(result,DeliveryResult) or result.state not in ('accepted','provider_accepted','failed','unknown'):
            result=DeliveryResult('unknown',code='provider_result_invalid')
        code=result.code if result.code in ('','retryable_rejection','invalid_destination','result_unavailable','transport_result_unknown',
            'provider_result_invalid','receipt_unavailable','receipt_expired') else 'provider_result_invalid'
        with self.store.atomic():
            self._workflow_lock()
            row=self._delivery(actor,delivery_id)
            if row['state'] in ('suppressed','provider_accepted','failed'):
                return self._project_delivery(row)
            self.store.execute('UPDATE stage6_notification_outbox SET state=?,provider_reference=?,outcome_code=? WHERE delivery_id=?',
                (result.state,str(result.reference or row['provider_reference'] or '')[:200],code,delivery_id))
            self._audit(delivery_id,result.state,code)
            if code=='invalid_destination' and row['destination_id']:
                self.store.execute('UPDATE stage6_destinations SET revoked_at=COALESCE(revoked_at,?) WHERE destination_id=?',(self.now(),row['destination_id']))
        self.store.connection.commit()
        return self._project_delivery(self._delivery(actor,delivery_id))

    def reconcile(self, actor, delivery_id):
        self._require_idle_connection()
        self.store.authorize(actor,staff_required=True)
        row=self._delivery(actor,delivery_id)
        if row['state'] not in ('sending','unknown','accepted'):
            raise RegistryError('notification_not_reconcilable',status=409)
        request=self._request(row)
        adapter=self._adapter(row,request)
        sent=self.store.query("SELECT MAX(recorded_at) AS at FROM stage6_delivery_audit WHERE delivery_id=? AND state='sending'",(delivery_id,))[0]['at']
        elapsed=max(0,self.now()-(sent or row['created_at']))
        # All reads and any metadata transaction end before provider I/O.
        self.store.connection.commit()
        try:
            result=adapter.retrieve(delivery_id) if type(adapter) is MemoryNotificationTransport else adapter.retrieve(request,row['provider_reference'],elapsed)
        except Exception:
            result=DeliveryResult('unknown',row['provider_reference'] or '',code='result_unavailable')
        return self._record_result(actor,delivery_id,result)

    def retry_rejected(self, actor, delivery_id, *, source_key):
        row=self._delivery(actor,delivery_id)
        self.store.authorize(actor,staff_required=True)
        def action():
            current=self._delivery(actor,delivery_id)
            if current['state']!='failed' or current['outcome_code']!='retryable_rejection' or current['attempts']>=3:
                raise RegistryError('notification_retry_not_safe',status=409)
            delay=60*(2**current['attempts'])
            self.store.execute("UPDATE stage6_notification_outbox SET state='queued',due_at=?,claim_hash=NULL WHERE delivery_id=?",(self.now()+delay,delivery_id))
            self._audit(delivery_id,'queued','retryable_rejection')
            return {'delivery_id':delivery_id,'state':'queued','due_at':self.now()+delay}
        return self._op(actor,source_key,{'retry':delivery_id},action,row['customer_id'],True)

    def recover_stale_claims(self, actor, *, source_key):
        self.store.authorize(actor,staff_required=True)
        def action():
            rows=self.store.query("SELECT * FROM stage6_notification_outbox WHERE state IN ('claimed','sending') AND claimed_at<?",(self.now()-900,))
            recovered=0
            for row in rows:
                try:
                    self._delivery(actor,row['delivery_id'])
                except RegistryError as error:
                    if error.code=='finance_permission_required':
                        continue
                    raise
                # Claimed-only has not crossed send boundary. Sending may have
                # been accepted; hold for reconciliation rather than retry.
                state='queued' if row['state']=='claimed' else 'unknown'
                self.store.execute('UPDATE stage6_notification_outbox SET state=?,claim_hash=NULL,outcome_code=? WHERE delivery_id=?',(state,'worker_interrupted',row['delivery_id']))
                self._audit(row['delivery_id'],state,'worker_interrupted')
                recovered+=1
            return {'recovered':recovered}
        return self._op(actor,source_key,{'recover_before':self.now()-900},action,staff=True)
