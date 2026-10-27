"""Copied Flask + fictional DB, controlled adapters with local wire capture only.

Actual external sockets remain denied by run_app_preview. No provider settings,
real recipient, app key, password, payment or original DB is accessed.
"""
import base64
import hashlib
import hmac
import json
from urllib.parse import parse_qs,urlsplit

import verify_stage6 as harness
from verify_stage5 import Forms


def run(module,users):
    from stage6_external_transports import TestDeliveryPolicy,LineTestTransport,SmtpTestTransport
    app=module.app;app.config['PROPAGATE_EXCEPTIONS']=True
    app.config.update(STAGE6_ENABLED=True,STAGE6_LINE_LINK_ENABLED=True,
        STAGE6_LINE_CHANNEL_SECRET='fictional-channel-secret-123456',STAGE6_LINE_STAFF_USER_ID=users['admin']['id'],
        STAGE6_TEST_MONTHLY_LIMITS={'email':20,'line':20,'push':20})
    checks=[];requests=[];lines=[];mails=[]
    customer=users['normal']['id'];line_user='U'+'a'*32;email='tester@example.invalid'
    def policy(channel,address):
        return TestDeliveryPolicy(True,'test',({'channel':channel,'customer_id':customer,
            'recipient_sha256':hashlib.sha256(address.encode()).hexdigest(),'approval_reference':'fictional-operator-approval'},))
    def capture_line(plan,token):
        lines.append(plan)
        return (200,{}, {'linkToken':'fixture-line-token-123456789'}) if plan['url'].endswith('/linkToken') else (200,{'X-Line-Request-Id':'fixture-line-request'},{})
    class SMTP:
        def __init__(self,**kwargs):pass
        def ehlo(self):pass
        def starttls(self,**kwargs):pass
        def send_message(self,message,**kwargs):mails.append(message);return {}
        def quit(self):pass
    app.extensions['stage6_test_transports']={
        'line':LineTestTransport(policy('line',line_user),access_token='fixture-token',http=capture_line),
        'email':SmtpTestTransport(policy('email',email),host='smtp.example.invalid',port=587,
            sender='kaika@example.invalid',smtp_factory=SMTP)}
    clients={role:app.test_client() for role in ('customer','other','staff','anonymous')}
    def check(name,ok):
        checks.append({'name':name,'pass':bool(ok)})
        if not ok:raise AssertionError(name)
    def http(role,path,data=None,headers=None,raw=False):
        response=clients[role].open(path,method='GET' if data is None else 'POST',data=data,
            content_type='application/json' if raw else None,base_url='http://127.0.0.1:18786',
            headers={'User-Agent':'Mozilla/5.0 Stage6ExternalFixture',**(headers or {})})
        response.get_data();requests.append({'role':role,'path':urlsplit(path).path,'status':response.status_code})
        return response
    def fields(role,page,action):
        response=http(role,page);check('form loads '+action,response.status_code==200)
        result=Forms(response).fields(action);check('form has CSRF '+action,bool(result.get('registry_csrf')))
        return result
    def rows(sql,params=()):
        db=module.get_db()
        try:return [dict(row) for row in db.execute(sql,params)]
        finally:db.close()
    def webhook(events,signature_override=None):
        raw=json.dumps({'events':events},separators=(',',':')).encode()
        sig=base64.b64encode(hmac.new(app.config['STAGE6_LINE_CHANNEL_SECRET'].encode(),raw,hashlib.sha256).digest()).decode()
        return http('anonymous','/notifications/line/webhook',raw,{'x-line-signature':signature_override or sig},True)
    try:
        for role,account in (('customer','normal'),('other','business'),('staff','admin')):
            r=http(role,'/login',{'username':users[account]['username'],'password':users[account]['password']})
            check('login '+role,r.status_code==302 and '/login' not in (r.location or ''))
        check('anonymous LINE landing can preserve fragment before login',http('anonymous','/notifications/line/link').status_code==200)
        check('anonymous email landing can preserve fragment before login',http('anonymous','/notifications/email/confirm').status_code==200)
        event={'type':'follow','webhookEventId':'fixture-follow','source':{'userId':line_user}}
        check('unsigned webhook refused',webhook([event],'wrong').status_code==403 and not lines)
        check('signed follow invokes link-token then push',webhook([event]).status_code==200 and len(lines)==2)
        check('link request fixed to same LINE recipient',lines[0]['url'].endswith('/'+line_user+'/linkToken') and lines[1]['json']['to']==line_user)
        link_url=lines[1]['json']['messages'][0]['text'].splitlines()[-1]
        pieces=parse_qs(urlsplit(link_url).fragment)
        values={'invitation':pieces['invitation'][0],'link_token':pieces['linkToken'][0]}
        check('no token in server path or query',urlsplit(link_url).query=='')
        link_path='/notifications/line/link'
        check('cross-customer link refused',http('other',link_path,{**fields('other',link_path,link_path),**values}).status_code==403)
        response=http('customer',link_path,{**fields('customer',link_path,link_path),**values})
        check('owner redirects only to official account-link',response.status_code==302 and response.location.startswith('https://access.line.me/dialog/bot/accountLink?'))
        nonce=parse_qs(urlsplit(response.location).query)['nonce'][0]
        callback={'type':'accountLink','webhookEventId':'fixture-linked','source':{'userId':line_user},'link':{'result':'ok','nonce':nonce}}
        check('signed matching nonce activates recipient',webhook([callback]).status_code==200 and len(rows("SELECT * FROM stage6_destinations WHERE channel='line' AND customer_id=?",(customer,)))==1)
        check('redelivery does not send new invitation',webhook([event]).status_code==200 and len(lines)==2)
        check('replayed account-link has one destination',webhook([callback]).status_code==200 and len(rows("SELECT * FROM stage6_destinations WHERE channel='line'"))==1)
        other_event={'type':'follow','webhookEventId':'fixture-not-approved','source':{'userId':'U'+'b'*32}}
        check('unapproved LINE follow sends nothing',webhook([other_event]).status_code==200 and len(lines)==2)
        settings='/notifications/settings';email_action='/notifications/email'
        check('email request only enqueues',http('customer',email_action,{**fields('customer',settings,email_action),'email':email}).status_code==302 and not mails)
        delivery=rows("SELECT delivery_id FROM stage6_notification_outbox WHERE channel='email'")[0]['delivery_id']
        action='/notifications/deliveries/'+delivery+'/send_test'
        check('customer cannot send test queue',http('customer',action,{}).status_code==403 and not mails)
        check('staff send without CSRF refused',http('staff',action,{}).status_code==400 and not mails)
        check('explicit staff send captured SMTP once',http('staff',action,fields('staff','/notifications',action)).status_code==302 and len(mails)==1)
        check('SMTP acceptance committed',rows('SELECT state FROM stage6_notification_outbox WHERE delivery_id=?',(delivery,))[0]['state']=='accepted')
        content=mails[0].get_content();proof_url=next(line for line in content.splitlines() if '/notifications/email/confirm#' in line)
        token=parse_qs(urlsplit(proof_url).fragment)['token'][0];confirm='/notifications/email/confirm'
        check('other customer cannot consume email proof',http('other',confirm,{**fields('other',confirm,confirm),'token':token}).status_code==409)
        check('owner confirms email via CSRF POST',http('customer',confirm,{**fields('customer',confirm,confirm),'token':token}).status_code==302)
        audit=json.dumps(rows('SELECT result_json FROM stage4_operations'))
        check('bearer tokens absent from immutable operations',all(secret not in audit for secret in (nonce,token,values['invitation'],values['link_token'])))
        check('all notification records remain test-only',all(row['test_only']==1 for row in rows('SELECT test_only FROM stage6_notification_outbox')))
        app.config['STAGE6_LINE_LINK_ENABLED']=False
        check('LINE flag-off stops callback and landing',webhook([event]).status_code==404 and http('customer',link_path).status_code==404)
        return {'scope':'Copied Flask + fictional SQLite; actual adapter code with local HTTP/SMTP capture; all external sockets denied',
            'checks':checks,'requests':requests,'passed':len(checks),'failed':0,'server_errors':0,
            'real_provider_calls':0,'captured_line_calls':len(lines),'captured_smtp_messages':len(mails),
            'not_verified':['real email arrival','real LINE channel registration/accountLink callback','real Expo/APNs/FCM device receipt','browser fragment JavaScript execution']}
    except Exception as error:
        return {'scope':'controlled notification HTTP fixture','checks':checks,'requests':requests,
            'passed':sum(c['pass'] for c in checks),'failed':1,'server_errors':sum(r['status']>=500 for r in requests),
            'error_type':type(error).__name__,'error':str(error)}


if __name__=='__main__':
    harness.run=run
    raise SystemExit(harness.main())
