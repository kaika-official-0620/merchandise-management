"""Offline transport and official-protocol request candidates. Never opens sockets.

Expo tickets / receipts mean acceptance by a provider, never device delivery.
LINE HTTP acceptance and SMTP acceptance likewise are not recipient reads.
"""
from copy import deepcopy
from dataclasses import dataclass
from email.message import EmailMessage
import base64
import hashlib
import hmac
import json
import re
import uuid

from item_registry import RegistryError

PUSH_TOKEN = re.compile(r'^(?:ExpoPushToken|ExponentPushToken)\[[A-Za-z0-9_-]{10,200}\]$')
LINE_USER = re.compile(r'^U[0-9a-f]{32}$')


@dataclass(frozen=True)
class DeliveryResult:
    state: str
    reference: str = ''
    code: str = ''


class MemoryNotificationTransport:
    """Explicit fictional destination harness; not a real email/push/LINE client."""
    network_enabled = False
    mode = 'test'

    def __init__(self):
        self.sent = []
        self.results = {}
        self.next_result = DeliveryResult('accepted', 'fixture-provider')
        self.timeout_after_send = False

    def send(self, request):
        self.sent.append(deepcopy(request))
        self.results[request['delivery_id']] = self.next_result
        if self.timeout_after_send:
            raise TimeoutError('fixture result unknown')
        return self.next_result

    def retrieve(self, delivery_id):
        return self.results.get(delivery_id, DeliveryResult('unknown', code='result_unavailable'))


def verified_line_events(raw_body, signature, channel_secret):
    if not isinstance(raw_body, bytes) or len(raw_body) > 256000 or not isinstance(channel_secret, str) or len(channel_secret) < 16:
        raise RegistryError('line_signature_invalid', status=403)
    expected = base64.b64encode(hmac.new(channel_secret.encode(), raw_body, hashlib.sha256).digest()).decode()
    if not isinstance(signature, str) or not hmac.compare_digest(expected, signature):
        raise RegistryError('line_signature_invalid', status=403)
    try:
        body = json.loads(raw_body)
        events = body['events']
    except (ValueError, KeyError, TypeError):
        raise RegistryError('line_payload_invalid') from None
    if not isinstance(events, list) or len(events) > 100:
        raise RegistryError('line_payload_invalid')
    return events


class OfficialRequestCandidates:
    """Pure plans only. Explicit future configuration/review is required to send."""
    @staticmethod
    def expo(request):
        if not PUSH_TOKEN.fullmatch(request['recipient']):
            raise RegistryError('push_token_invalid')
        return {'method': 'POST', 'url': 'https://exp.host/--/api/v2/push/send',
            'json': {'to': request['recipient'], 'title': request['title'], 'body': request['body'],
                     'data': {'notification_id': request['notification_id']}, 'sound': 'default'}}

    @staticmethod
    def expo_receipts(ticket_ids):
        if not isinstance(ticket_ids, list) or not 1 <= len(ticket_ids) <= 1000 or any(not isinstance(x,str) or len(x)>200 for x in ticket_ids):
            raise RegistryError('push_ticket_invalid')
        return {'method': 'POST', 'url': 'https://exp.host/--/api/v2/push/getReceipts', 'json': {'ids': ticket_ids}}

    @staticmethod
    def line(request):
        if not LINE_USER.fullmatch(request['recipient']):
            raise RegistryError('line_recipient_invalid')
        retry = str(uuid.UUID(request['delivery_id']))
        return {'method': 'POST', 'url': 'https://api.line.me/v2/bot/message/push',
                'headers': {'X-Line-Retry-Key': retry}, 'json': {'to': request['recipient'],
                    'messages': [{'type': 'text', 'text': request['title'] + '\n' + request['body'] +
                        ('\n' + OfficialRequestCandidates._notification_url(request) if request.get('notification_url') else '')}]}}

    @staticmethod
    def line_link_token(line_user):
        if not LINE_USER.fullmatch(line_user):
            raise RegistryError('line_recipient_invalid')
        return {'method': 'POST', 'url': 'https://api.line.me/v2/bot/user/' + line_user + '/linkToken'}

    @staticmethod
    def email(request, sender):
        # EmailMessage rejects newline injection. Envelopes must additionally be
        # verified by the destination service; secrets and SMTP hosts are absent.
        address=re.compile(r'^[A-Za-z0-9.!#$%&\x27*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,63}$')
        if not isinstance(sender,str) or len(sender)>254 or not address.fullmatch(sender):
            raise ValueError('Valid verified sender address required')
        message = EmailMessage()
        message['From'], message['To'], message['Subject'] = sender, request['recipient'], request['title']
        message['Message-ID'] = '<' + request['delivery_id'] + '@notifications.invalid>'
        message.set_content(request['body'] + ('\n' + request['verification_url'] if request.get('verification_url') else '') +
            ('\n'+OfficialRequestCandidates._notification_url(request) if request.get('notification_url') else ''))
        return message

    @staticmethod
    def _notification_url(request):
        value=request['notification_url']
        if not isinstance(value,str) or not re.fullmatch(r'https://[A-Za-z0-9.-]+(?::[0-9]+)?/notifications/[a-f0-9]{32}',value):
            raise RegistryError('notification_url_invalid')
        if value.rsplit('/',1)[1]!=request['notification_id']:
            raise RegistryError('notification_url_invalid')
        return value

    @staticmethod
    def execute(*args, **kwargs):
        raise RegistryError('external_notification_delivery_disabled', status=409)

    @staticmethod
    def expo_ticket(payload):
        """A single addressed request must yield exactly one matching ticket."""
        data=payload.get('data') if isinstance(payload,dict) else None
        if isinstance(data,list):
            data=data[0] if len(data)==1 else None
        if not isinstance(data,dict):
            return DeliveryResult('unknown',code='provider_result_invalid')
        if data.get('status')=='ok' and isinstance(data.get('id'),str) and data['id']:
            return DeliveryResult('accepted',data['id'])
        if data.get('status')=='error':
            details=data.get('details')
            code=details.get('error') if isinstance(details,dict) else None
            return DeliveryResult('failed',code='invalid_destination' if code=='DeviceNotRegistered' else
                'retryable_rejection' if code=='MessageRateExceeded' else 'provider_result_invalid')
        return DeliveryResult('unknown',code='provider_result_invalid')

    @staticmethod
    def expo_receipt(payload, ticket_id, *, elapsed_seconds=None):
        if not isinstance(ticket_id,str) or not ticket_id or len(ticket_id)>200:
            return DeliveryResult('unknown',code='provider_result_invalid')
        if elapsed_seconds is not None and (type(elapsed_seconds) is not int or elapsed_seconds<0):
            return DeliveryResult('unknown',code='provider_result_invalid')
        data=payload.get('data') if isinstance(payload,dict) else None
        if not isinstance(data,dict) or payload.get('errors'):
            return DeliveryResult('unknown',code='provider_result_invalid')
        item=data.get(ticket_id)
        if item is None:
            if elapsed_seconds is not None and elapsed_seconds>=900:
                # Expo recommends checking by 15m; receipts expire at 24h.
                # Missing evidence is not permission to resend this notice.
                return DeliveryResult('unknown',ticket_id,
                    'receipt_expired' if elapsed_seconds>=86400 else 'receipt_unavailable')
            return DeliveryResult('accepted',ticket_id)  # Receipt not ready; do not resend.
        if not isinstance(item,dict):
            return DeliveryResult('unknown',code='provider_result_invalid')
        if item.get('status')=='ok':
            return DeliveryResult('provider_accepted',ticket_id)
        if item.get('status')=='error':
            details=item.get('details')
            code=details.get('error') if isinstance(details,dict) else None
            return DeliveryResult('failed',ticket_id,'invalid_destination' if code=='DeviceNotRegistered' else
                'retryable_rejection' if code=='MessageRateExceeded' else 'provider_result_invalid')
        return DeliveryResult('unknown',code='provider_result_invalid')

    @staticmethod
    def line_response(status, request_id='', accepted_request_id=''):
        if status==200:
            return DeliveryResult('accepted',str(request_id))
        # A retry key already accepted is not a new delivery and not a failure.
        if status==409 and accepted_request_id:
            return DeliveryResult('accepted',str(accepted_request_id))
        if status==429:
            return DeliveryResult('failed',code='retryable_rejection')
        # Network/5xx can follow acceptance. Reconciliation/explicit retry-key
        # policy is needed; never call it definitely unsent from HTTP alone.
        return DeliveryResult('unknown',code='provider_result_invalid')

    @staticmethod
    def smtp_result(refused_recipients, recipient):
        if not isinstance(refused_recipients,dict):
            return DeliveryResult('unknown',code='provider_result_invalid')
        if recipient not in refused_recipients:
            return DeliveryResult('accepted')
        refusal=refused_recipients[recipient]
        if not isinstance(refusal,(tuple,list)) or len(refusal)!=2 or type(refusal[0]) is not int:
            return DeliveryResult('unknown',code='provider_result_invalid')
        code=refusal[0]
        return DeliveryResult('failed',code='retryable_rejection' if type(code) is int and 400<=code<500 else 'invalid_destination')
