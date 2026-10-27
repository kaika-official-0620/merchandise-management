"""Explicit, allowlisted TEST delivery. No network or environment I/O on import.

These providers send real messages even in a test deployment. A deployment flag
alone is insufficient: every channel/customer/address needs explicit approval.
"""
from dataclasses import dataclass
from email.utils import parseaddr
import hashlib
import json
import re
import smtplib
import ssl
import urllib.error
import urllib.request

from item_registry import RegistryError
from stage6_transports import DeliveryResult, OfficialRequestCandidates as Plans


@dataclass(frozen=True)
class TestDeliveryPolicy:
    enabled: bool = False
    environment: str = ''
    approvals: tuple = ()

    def customer_for(self, channel, recipient):
        if self.enabled is not True or self.environment not in ('test','staging'):
            raise RegistryError('external_notification_delivery_disabled',status=409)
        hashed=hashlib.sha256(recipient.encode()).hexdigest()
        matches=[row for row in self.approvals if row.get('channel')==channel and row.get('recipient_sha256')==hashed
            and type(row.get('customer_id')) is int and row['customer_id']>0
            and isinstance(row.get('approval_reference'),str) and row['approval_reference'].strip()]
        if len(matches)!=1:
            raise RegistryError('notification_test_recipient_not_approved',status=403)
        return matches[0]['customer_id']

    def require(self, channel, request):
        if request.get('test_only') is not True or type(request.get('customer_id')) is not int:
            raise RegistryError('notification_test_context_required',status=409)
        if self.customer_for(channel,request.get('recipient',''))!=request['customer_id']:
            raise RegistryError('notification_test_recipient_not_approved',status=403)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):
        return None


def official_https(plan, token):
    """Fixed official endpoints, verified TLS, bounded response, no redirects."""
    url=plan['url']
    if not (url in ('https://exp.host/--/api/v2/push/send','https://exp.host/--/api/v2/push/getReceipts',
                    'https://api.line.me/v2/bot/message/push')
            or re.fullmatch(r'https://api\.line\.me/v2/bot/user/U[0-9a-f]{32}/linkToken',url)):
        raise RegistryError('notification_provider_endpoint_invalid')
    headers={'Content-Type':'application/json','Accept':'application/json',**plan.get('headers',{})}
    if token:
        if not isinstance(token,str) or any(c in token for c in '\r\n'):
            raise RegistryError('notification_provider_configuration_invalid')
        headers['Authorization']='Bearer '+token
    body=json.dumps(plan.get('json',{}),ensure_ascii=False).encode()
    request=urllib.request.Request(url,data=body,headers=headers,method=plan['method'])
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),_NoRedirect(),
        urllib.request.HTTPSHandler(context=ssl.create_default_context()))
    try:
        response=opener.open(request,timeout=15)
    except urllib.error.HTTPError as error:
        response=error
    with response:
        raw=response.read(1024*1024+1)
        if len(raw)>1024*1024:
            raise RegistryError('notification_provider_response_invalid')
        try: payload=json.loads(raw) if raw else {}
        except (ValueError,UnicodeError): payload=None
        return response.status,dict(response.headers.items()),payload


class ExpoTestTransport:
    mode='test';network_enabled=True;channel='push'

    def __init__(self, policy, *, access_token='', http=official_https):
        self.policy,self.access_token,self.http=policy,access_token,http

    def validate(self,request):
        self.policy.require(self.channel,request)
        Plans.expo(request)

    def send(self,request):
        self.validate(request)
        status,headers,payload=self.http(Plans.expo(request),self.access_token)
        if status==200:return Plans.expo_ticket(payload)
        if status==429:return DeliveryResult('failed',code='retryable_rejection')
        # 5xx/timeouts may occur after acceptance. No blind retry.
        return DeliveryResult('unknown',code='provider_result_invalid')

    def retrieve(self,request,reference,elapsed_seconds):
        self.validate(request)
        if not reference:return DeliveryResult('unknown',code='result_unavailable')
        status,headers,payload=self.http(Plans.expo_receipts([reference]),self.access_token)
        if status!=200:return DeliveryResult('unknown',reference,'result_unavailable')
        return Plans.expo_receipt(payload,reference,elapsed_seconds=elapsed_seconds)


class LineTestTransport:
    mode='test';network_enabled=True;channel='line'

    def __init__(self,policy,*,access_token,http=official_https):
        self.policy,self.access_token,self.http=policy,access_token,http

    def validate(self,request):
        self.policy.require(self.channel,request)
        if not isinstance(self.access_token,str) or not self.access_token or any(c in self.access_token for c in '\r\n'):
            raise RegistryError('notification_provider_configuration_invalid')
        Plans.line(request)

    def send(self,request):
        self.validate(request)
        plan=Plans.line(request)
        # Link URLs are generated internally, never accepted from a public form.
        if request.get('link_invitation_url'):
            url=request['link_invitation_url']
            if not re.fullmatch(r'https://[A-Za-z0-9.-]+(?::[0-9]+)?/notifications/line/link#[A-Za-z0-9_=&%+.-]+',url):
                raise RegistryError('line_link_url_invalid')
            plan['json']['messages'][0]['text']+='\n'+url
        status,headers,payload=self.http(plan,self.access_token)
        lowered={key.lower():value for key,value in headers.items()}
        return Plans.line_response(status,lowered.get('x-line-request-id',''),lowered.get('x-line-accepted-request-id',''))

    def retrieve(self,request,reference,elapsed_seconds):
        self.validate(request)
        # Messaging API does not expose an individual delivered/read receipt.
        return DeliveryResult('accepted',reference) if reference else DeliveryResult('unknown',code='result_unavailable')

    def link_token(self,recipient):
        self.policy.customer_for('line',recipient)
        if not isinstance(self.access_token,str) or not self.access_token or any(c in self.access_token for c in '\r\n'):
            raise RegistryError('notification_provider_configuration_invalid')
        status,headers,payload=self.http(Plans.line_link_token(recipient),self.access_token)
        token=payload.get('linkToken') if isinstance(payload,dict) else None
        if status!=200 or not isinstance(token,str) or not re.fullmatch(r'[A-Za-z0-9_+=/.-]{10,1024}',token):
            raise RegistryError('line_link_token_unavailable',status=409)
        return token


class SmtpTestTransport:
    mode='test';network_enabled=True;channel='email'

    def __init__(self,policy,*,host,port,sender,username='',password='',tls_mode='starttls',smtp_factory=None):
        self.policy=policy
        self.host,self.port,self.sender,self.username,self.password=host,port,sender,username,password
        self.tls_mode,self.smtp_factory=tls_mode,smtp_factory

    def validate(self,request):
        self.policy.require(self.channel,request)
        if (not isinstance(self.host,str) or not re.fullmatch(r'[A-Za-z0-9.-]+',self.host)
                or type(self.port) is not int or self.port not in (465,587)
                or self.tls_mode not in ('ssl','starttls') or (self.tls_mode=='ssl')!=(self.port==465)
                or any(c in request['recipient'] for c in '\r\n')
                or not re.fullmatch(r'[^\s@<>]+@[^\s@<>]+\.[A-Za-z]{2,63}',request['recipient'])
                or parseaddr(request['recipient'])[1]!=request['recipient']):
            raise RegistryError('notification_provider_configuration_invalid')
        Plans.email(request,self.sender)

    def send(self,request):
        self.validate(request)
        message=Plans.email(request,self.sender)
        domain=self.sender.rsplit('@',1)[1]
        message.replace_header('Message-ID','<'+request['delivery_id']+'@'+domain+'>')
        factory=self.smtp_factory or (smtplib.SMTP_SSL if self.tls_mode=='ssl' else smtplib.SMTP)
        kwargs={'host':self.host,'port':self.port,'timeout':15}
        if self.tls_mode=='ssl':kwargs['context']=ssl.create_default_context()
        smtp=factory(**kwargs)
        try:
            smtp.ehlo()
            if self.tls_mode=='starttls':
                smtp.starttls(context=ssl.create_default_context());smtp.ehlo()
            if self.username:smtp.login(self.username,self.password)
            try:
                refused=smtp.send_message(message,from_addr=self.sender,to_addrs=[request['recipient']])
            except smtplib.SMTPRecipientsRefused as error:
                refused=error.recipients
            except smtplib.SMTPDataError as error:
                return DeliveryResult('failed',code='retryable_rejection' if 400<=error.smtp_code<500 else 'provider_result_invalid')
            result=Plans.smtp_result(refused,request['recipient'])
            return DeliveryResult(result.state,str(message['Message-ID']) if result.state=='accepted' else '',result.code)
        finally:
            # QUIT failure after DATA acceptance must not erase known acceptance.
            try:smtp.quit()
            except Exception:
                try:smtp.close()
                except Exception:pass

    def retrieve(self,request,reference,elapsed_seconds):
        self.validate(request)
        # SMTP has no generic per-message retrieval API. Never resend unknown DATA.
        return DeliveryResult('accepted',reference) if reference else DeliveryResult('unknown',code='result_unavailable')


EXTERNAL_TRANSPORT_TYPES=(ExpoTestTransport,LineTestTransport,SmtpTestTransport)


def transports_from_config(config):
    """Called explicitly by web registration. Invalid opt-in fails closed."""
    if config.get('STAGE6_TEST_DELIVERY_ENABLED') is not True:return {}
    raw=config.get('STAGE6_TEST_RECIPIENT_APPROVALS',[])
    if isinstance(raw,str):
        try:raw=json.loads(raw)
        except (ValueError,TypeError):raise RegistryError('notification_test_approvals_invalid') from None
    if not isinstance(raw,list) or any(not isinstance(row,dict) for row in raw):
        raise RegistryError('notification_test_approvals_invalid')
    policy=TestDeliveryPolicy(True,config.get('KAIKA_RUNTIME_ENV',''),tuple(raw))
    if policy.environment not in ('test','staging'):
        raise RegistryError('external_notification_delivery_disabled',status=409)
    result={}
    if config.get('STAGE6_EXPO_TEST_ENABLED') is True:
        result['push']=ExpoTestTransport(policy,access_token=config.get('STAGE6_EXPO_ACCESS_TOKEN',''))
    if config.get('STAGE6_LINE_TEST_ENABLED') is True:
        result['line']=LineTestTransport(policy,access_token=config.get('STAGE6_LINE_CHANNEL_ACCESS_TOKEN',''))
    if config.get('STAGE6_SMTP_TEST_ENABLED') is True:
        try:port=int(config.get('STAGE6_SMTP_PORT',587))
        except (TypeError,ValueError):raise RegistryError('notification_provider_configuration_invalid') from None
        result['email']=SmtpTestTransport(policy,host=config.get('STAGE6_SMTP_HOST',''),port=port,
            sender=config.get('STAGE6_SMTP_SENDER',''),username=config.get('STAGE6_SMTP_USERNAME',''),
            password=config.get('STAGE6_SMTP_PASSWORD',''),tls_mode=config.get('STAGE6_SMTP_TLS_MODE','starttls'))
    return result
