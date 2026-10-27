"""Official wire shapes exercised offline; no credentials, HTTP or SMTP client."""
import unittest
from item_registry import RegistryError
from stage6_transports import OfficialRequestCandidates as Plans


class TransportCandidateTests(unittest.TestCase):
    def request(self,recipient):
        return dict(delivery_id='12345678123456781234567812345678',notification_id='a'*32,
            title='確認事項があります',body='ログインしてご確認ください。',recipient=recipient)

    def test_all_candidate_execution_disabled(self):
        with self.assertRaises(RegistryError) as caught:
            Plans.execute({'any':'request'})
        self.assertEqual(caught.exception.code,'external_notification_delivery_disabled')

    def test_expo_request_contains_notice_only_no_path_or_customer(self):
        request=self.request('ExpoPushToken[fictionalToken1234567]')
        plan=Plans.expo(request)
        self.assertEqual(plan['url'],'https://exp.host/--/api/v2/push/send')
        self.assertEqual(plan['json']['data'],{'notification_id':'a'*32})
        self.assertNotIn('Authorization',str(plan))

    def test_expo_invalid_token_rejected(self):
        with self.assertRaises(RegistryError):
            Plans.expo(self.request('not a token'))

    def test_expo_ticket_is_only_accepted(self):
        result=Plans.expo_ticket({'data':{'status':'ok','id':'fixture-ticket'}})
        self.assertEqual((result.state,result.reference),('accepted','fixture-ticket'))

    def test_expo_multiple_or_malformed_tickets_not_matched_to_one_recipient(self):
        for payload in ({'data':[]},{'data':[{'status':'ok','id':'a'},{'status':'ok','id':'b'}]},None,{'data':{'status':'ok'}}):
            self.assertEqual(Plans.expo_ticket(payload).state,'unknown')

    def test_expo_receipt_waiting_and_provider_accepted(self):
        self.assertEqual(Plans.expo_receipt({'data':{}},'fixture-ticket').state,'accepted')
        self.assertEqual(Plans.expo_receipt({'data':{'fixture-ticket':{'status':'ok'}}},'fixture-ticket').state,'provider_accepted')
        self.assertEqual(Plans.expo_receipt({'data':{'other-ticket':{'status':'ok'}}},'fixture-ticket').state,'accepted')

    def test_expo_uninstalled_and_rate_limited(self):
        for code,expected in [('DeviceNotRegistered','invalid_destination'),('MessageRateExceeded','retryable_rejection')]:
            item={'status':'error','details':{'error':code}}
            self.assertEqual(Plans.expo_ticket({'data':item}).code,expected)
            self.assertEqual(Plans.expo_receipt({'data':{'ticket':item}},'ticket').code,expected)

    def test_malformed_receipt_response_is_unknown_not_accepted(self):
        for payload in (None, [], {}, {'data':None}, {'data':[]}, {'errors':[{'code':'SERVER_ERROR'}]},
                        {'data':{},'errors':[{'code':'SERVER_ERROR'}]}):
            with self.subTest(payload=payload):
                self.assertEqual(Plans.expo_receipt(payload,'ticket').state,'unknown')

    def test_missing_receipt_after_recommended_window_is_unknown_without_resend(self):
        self.assertEqual(Plans.expo_receipt({'data':{}},'ticket',elapsed_seconds=899).state,'accepted')
        for age, code in ((900,'receipt_unavailable'),(86399,'receipt_unavailable'),(86400,'receipt_expired')):
            result=Plans.expo_receipt({'data':{}},'ticket',elapsed_seconds=age)
            self.assertEqual((result.state,result.reference,result.code),('unknown','ticket',code))
        for age in (-1,True,0.1,'900'):
            self.assertEqual(Plans.expo_receipt({'data':{}},'ticket',elapsed_seconds=age).state,'unknown')

    def test_malformed_error_details_cannot_raise_or_be_retried_automatically(self):
        for details in (None,[],42,'DeviceNotRegistered'):
            item={'status':'error','details':details}
            self.assertEqual(Plans.expo_ticket({'data':item}).code,'provider_result_invalid')
            self.assertEqual(Plans.expo_receipt({'data':{'ticket':item}},'ticket').code,'provider_result_invalid')

    def test_malformed_smtp_refusal_is_unknown(self):
        for refusal in (None,[],[550],['550',b'error'],550):
            self.assertEqual(Plans.smtp_result({'user@example.invalid':refusal},'user@example.invalid').state,'unknown')

    def test_receipt_request_bounded(self):
        self.assertEqual(Plans.expo_receipts(['ticket'])['json'],{'ids':['ticket']})
        for value in ([],['a']*1001,'ticket',[None]):
            with self.assertRaises(RegistryError):
                Plans.expo_receipts(value)

    def test_line_retry_identity_and_generic_text(self):
        plan=Plans.line(self.request('U'+'a'*32))
        self.assertEqual(plan['headers']['X-Line-Retry-Key'],'12345678-1234-5678-1234-567812345678')
        self.assertEqual(plan['json']['messages'][0]['type'],'text')
        self.assertNotIn('customer_id',str(plan))

    def test_line_link_token_uses_official_endpoint(self):
        plan=Plans.line_link_token('U'+'a'*32)
        self.assertEqual(plan['url'],'https://api.line.me/v2/bot/user/U'+'a'*32+'/linkToken')
        self.assertNotIn('Authorization',plan)
        with self.assertRaises(RegistryError):
            Plans.line_link_token('../../malicious')

    def test_line_acceptance_redelivery_and_unknown(self):
        self.assertEqual(Plans.line_response(200,'req').state,'accepted')
        self.assertEqual(Plans.line_response(409,accepted_request_id='original').reference,'original')
        self.assertEqual(Plans.line_response(429).code,'retryable_rejection')
        self.assertEqual(Plans.line_response(500).state,'unknown')

    def test_email_message_and_smtp_acceptance_are_distinct_from_delivery(self):
        request=self.request('customer@example.invalid')
        message=Plans.email(request,'sender@example.invalid')
        self.assertEqual(str(message['To']),request['recipient'])
        self.assertNotIn('customer_id',message.get_content())
        self.assertEqual(Plans.smtp_result({},request['recipient']).state,'accepted')
        self.assertEqual(Plans.smtp_result({request['recipient']:(450,b'try later')},request['recipient']).code,'retryable_rejection')
        self.assertEqual(Plans.smtp_result({request['recipient']:(550,b'no mailbox')},request['recipient']).code,'invalid_destination')

    def test_email_headers_cannot_inject_new_headers(self):
        request=self.request('customer@example.invalid\r\nBcc:other@example.invalid')
        with self.assertRaises(ValueError):
            Plans.email(request,'sender@example.invalid')

    def test_email_and_line_include_authenticated_notice_link_only(self):
        request=self.request('U'+'a'*32)
        request['notification_url']='https://preview.example.invalid/notifications/'+'a'*32
        self.assertIn(request['notification_url'],Plans.line(request)['json']['messages'][0]['text'])
        request['recipient']='customer@example.invalid'
        self.assertIn(request['notification_url'],Plans.email(request,'sender@example.invalid').get_content())
        request['notification_url']='https://preview.example.invalid/notifications/'+'b'*32
        with self.assertRaises(RegistryError):
            Plans.email(request,'sender@example.invalid')

    def test_sender_and_notice_link_header_injection_rejected(self):
        request=self.request('customer@example.invalid')
        for sender in ('','Name <a@example.invalid>','a@example.invalid\nBcc: b@example.invalid'):
            with self.assertRaises(ValueError):
                Plans.email(request,sender)
        request['notification_url']='//evil.invalid/notifications/'+'a'*32
        with self.assertRaises(RegistryError):
            Plans.email(request,'sender@example.invalid')


if __name__=='__main__':
    unittest.main()
