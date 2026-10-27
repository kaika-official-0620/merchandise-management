"""Real HTTP forms on a copied application with fictional data and no network.

Only a test deposit is seeded. Catalog, photos, purchase, approval, manual
marketplace records, fulfillment and return all pass through authenticated HTTP.
The test ownership rule is an explicit fixture setting, not a business decision.
"""
from contextlib import redirect_stdout,redirect_stderr
from datetime import datetime,timedelta
from html.parser import HTMLParser
import csv
import hashlib
import importlib
import io
import json
import logging
import os
from pathlib import Path
import sys
import tempfile
import traceback
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import run_app_preview as sandbox


class Forms(HTMLParser):
    def __init__(self,response):
        super().__init__()
        self.forms,self.current=[],None
        self.feed(response.get_data(as_text=True))

    def handle_starttag(self,tag,attrs):
        attrs=dict(attrs)
        if tag=='form':
            self.current={'action':attrs.get('action',''),'fields':{}}
            self.forms.append(self.current)
        elif tag=='input' and self.current is not None and attrs.get('type')=='hidden':
            self.current['fields'][attrs.get('name')]=attrs.get('value','')

    def handle_endtag(self,tag):
        if tag=='form':
            self.current=None

    def fields(self,action):
        return next((dict(f['fields']) for f in self.forms if urlsplit(f['action']).path==action),{})


def run(module,users):
    from PIL import Image
    from item_registry import Actor,Registry
    from stage3_common import FinanceStore
    from stage3_funds import FundsService
    from stage3_profit import ProfitService
    app=module.app
    app.config['PROPAGATE_EXCEPTIONS']=True
    app.logger.setLevel(logging.ERROR)
    clients={role:app.test_client() for role in ('customer','other','staff','anonymous')}
    checks,requests=[],[]
    def check(name,condition):
        checks.append({'name':name,'pass':bool(condition)})
    def http(role,path,data=None):
        response=clients[role].open(path,method='GET' if data is None else 'POST',data=data,
            base_url='http://127.0.0.1:18784',headers={'User-Agent':'Mozilla/5.0 Stage4Fixture'})
        response.get_data()
        requests.append({'role':role,'method':'GET' if data is None else 'POST','path':path,'status':response.status_code})
        return response
    def form(role,page,action):
        response=http(role,page)
        check('form opens '+role+' '+action,response.status_code==200)
        fields=Forms(response).fields(action)
        check('protected form '+role+' '+action,bool(fields.get('operation_token')) and bool(fields.get('registry_csrf')))
        return fields
    def post(name,role,page,action,values,*,expected=302):
        data={**form(role,page,action),**values}
        response=http(role,action,data)
        check(name,response.status_code==expected)
        if response.status_code!=expected:
            raise AssertionError(name+': status '+str(response.status_code))
        return response,data
    def rows(sql,params=()):
        conn=module.get_db()
        try:
            return [dict(row) for row in conn.execute(sql,params)]
        finally:
            conn.close()
    def baseline():
        result={}
        for table in ('user_invoices','user_keisan','admin_kaitori','admin_shikiriosho','monthly_settlements',
                      'invoice_payments','payment_transactions','business_charge_drafts','business_fund_records'):
            if not rows("SELECT name FROM sqlite_master WHERE type='table' AND name=?",(table,)):
                continue
            serialized=sorted(json.dumps(r,sort_keys=True,ensure_ascii=False,default=str) for r in rows('SELECT * FROM '+table))
            result[table]=hashlib.sha256(json.dumps(serialized).encode()).hexdigest()
        return result
    def image_bytes(color):
        image=Image.new('RGB',(160,120),color)
        stream=io.BytesIO();image.save(stream,'PNG')
        return stream.getvalue()
    def now():
        return datetime.now(ZoneInfo('Asia/Tokyo')).isoformat()
    before=baseline()
    owner_id,staff_id=users['normal']['id'],users['admin']['id']
    staff=Actor(staff_id,True)
    try:
        for role,account in (('customer','normal'),('other','business'),('staff','admin')):
            user=users[account]
            response=http(role,'/login',{'username':user['username'],'password':user['password']})
            check('real login '+role,response.status_code==302 and '/login' not in (response.location or ''))
        check('commerce default disabled',app.config.get('STAGE4_COMMERCE_ENABLED') is not True)
        check('default flag blocks staff',http('staff','/commerce').status_code==404)
        app.config['STAGE4_COMMERCE_ENABLED']=True
        check('ownership undecided default',app.config.get('STAGE4_OWNERSHIP_RULE')=='undecided')
        check('anonymous login required',http('anonymous','/commerce').status_code in (302,401))
        check('customer cannot create company stock',http('customer','/commerce/company',{}).status_code==403)
        check('staff missing CSRF denied',http('staff','/commerce/company',{}).status_code==400)

        created,creation=post('company stock created','staff','/commerce','/commerce/company',
            {'title':'【架空】開花の販売商品','purchase_cost_yen':'12345','evidence_reference':'fixture-company-stock'})
        item_id=rows('SELECT physical_item_id FROM stage4_company_items')[0]['physical_item_id']
        company='/commerce/company/'+item_id
        product='/commerce/products/'+item_id
        check('new company item has single ID',len(rows('SELECT physical_item_id FROM business_items'))==1)
        check('company has no customer profile yet',not rows('SELECT physical_item_id FROM business_item_profiles'))
        check('repeat creation accepts same token',http('staff','/commerce/company',creation).status_code==302)
        check('repeat creation adds no item',len(rows('SELECT physical_item_id FROM business_items'))==1)
        check('changed creation same token rejected',http('staff','/commerce/company',{**creation,'title':'changed'}).status_code==409)
        check('customer private company detail denied',http('customer',company).status_code==403)
        check('unpublished product not visible',http('customer',product).status_code==404)

        photo_path='/commerce/items/'+item_id+'/photos'
        photo_fields=form('staff',company,photo_path)
        picture=image_bytes('#728da5')
        upload={**photo_fields,'photos':[(io.BytesIO(picture),'fixture-public.png'),(io.BytesIO(image_bytes('#885533')),'fixture-internal.png')]}
        check('validated photos uploaded',http('staff',photo_path,upload).status_code==302)
        media=rows('SELECT * FROM stage4_media ORDER BY recorded_at,media_id')
        check('two media records stored',len(media)==2)
        check('unpublished media denied to customer',http('customer','/commerce/media/'+media[0]['media_id']).status_code==404)
        check('raw static media denied',http('customer','/static/'+media[0]['storage_path']).status_code in (403,404))
        repeat_upload={**photo_fields,'photos':[(io.BytesIO(picture),'fixture-public.png'),(io.BytesIO(image_bytes('#885533')),'fixture-internal.png')]}
        check('photo upload retry accepted',http('staff',photo_path,repeat_upload).status_code==302)
        check('photo retry no extra files',len(rows('SELECT media_id FROM stage4_media'))==2)
        selected=media[0]['media_id']
        unused=media[1]['media_id']
        post('catalog revision saved','staff',company,company+'/revise',{'title':'【架空】承認対象バッグ',
            'description':'検証用の中古商品です。実在の販売商品ではありません。','condition':'中古・検証用',
            'price_yen':'30000','photo_refs':[selected],'evidence_reference':'fixture-product-version'})
        revision=rows('SELECT revision_id FROM stage4_catalog_revisions')[0]['revision_id']
        post('catalog published only inside fixture','staff',company,company+'/publish',
            {'revision_id':revision,'evidence_reference':'fixture-publish'})
        body=http('customer',product).get_data(as_text=True)
        check('published customer product visible','30,000' in body and '承認対象バッグ' in body)
        check('company cost absent customer product','12,345' not in body and 'purchase_cost_yen' not in body)
        check('published image visible',http('customer','/commerce/media/'+selected).status_code==200)
        media_response=http('customer','/commerce/media/'+selected)
        check('media is never publicly cached','private' in media_response.headers.get('Cache-Control','') and 'no-store' in media_response.headers.get('Cache-Control',''))
        check('unused company image private',http('customer','/commerce/media/'+unused).status_code==404)

        # Explicit fictional funding only; the application has no live deposit.
        conn=module.get_db()
        try:
            conn.execute('PRAGMA foreign_keys=ON');conn.execute('BEGIN IMMEDIATE')
            FundsService(FinanceStore(Registry(conn))).post(staff,owner_id,kind='deposit',amount_yen=50000,
                occurred_at=now(),source_key='stage4-fixture-test-deposit',evidence_reference='fictional-test-only')
            conn.commit()
        finally:
            conn.close()
        reserve_path=product+'/reserve'
        reserve_form=form('customer',product,reserve_path)
        # Capture a genuine second customer's form before the first buyer
        # claims it; the UI intentionally removes reservation after claiming.
        other_fields=form('other',product,reserve_path)
        check('reservation requires explicit agreement',http('customer',reserve_path,{**reserve_form,'revision_id':revision,'custody_choice':'kaika'}).status_code==400)
        reserve={**reserve_form,'revision_id':revision,'custody_choice':'kaika','agree':'yes'}
        check('customer reserves test product',http('customer',reserve_path,reserve).status_code==302)
        order=rows('SELECT * FROM stage4_purchase_orders')[0]
        check('reservation retry accepted',http('customer',reserve_path,reserve).status_code==302)
        check('reservation creates one order',len(rows('SELECT order_id FROM stage4_purchase_orders'))==1)
        check('reservation creates one test allocation',len(rows("SELECT record_id FROM stage3_fund_records WHERE kind='reserve'"))==1)
        check('other buyer cannot reserve same physical item',http('other',reserve_path,{**other_fields,'revision_id':revision,'custody_choice':'kaika','agree':'yes'}).status_code==409)
        check('another user token cannot be reused',http('other',reserve_path,reserve).status_code in (400,403))
        check('other purchase list hides order',order['order_id'] not in http('other','/commerce/orders').get_data(as_text=True))
        confirm_path='/commerce/orders/'+order['order_id']+'/confirm'
        check('customer cannot confirm payment',http('customer',confirm_path,{}).status_code==403)
        post('default undecided ownership holds transfer','staff','/commerce/orders',confirm_path,{'evidence_reference':'fixture-test-confirm'})
        check('pending ownership creates no customer profile',not rows('SELECT physical_item_id FROM business_item_profiles'))
        check('pending ownership keeps company as owner',rows("SELECT party_id FROM business_item_assignments WHERE role='owner' AND ended_at IS NULL")[0]['party_id']=='kaika')
        # Deliberately approved only inside this fictional test, never by form.
        app.config['STAGE4_OWNERSHIP_RULE']='test_payment_confirmation'
        _,confirmed=post('fixture ownership adopted same item','staff','/commerce/orders',confirm_path,{'evidence_reference':'fixture-explicit-test-rule'})
        check('confirmation retry does not buy twice',http('staff',confirm_path,confirmed).status_code==302)
        check('test purchase ledger once',len(rows("SELECT record_id FROM stage3_fund_records WHERE kind='purchase'"))==1)
        check('adoption preserves one physical item',len(rows('SELECT physical_item_id FROM business_items'))==1)
        check('adoption creates no fabricated work',not rows('SELECT work_id FROM business_work_records'))
        profile=rows('SELECT * FROM business_item_profiles')[0]
        check('adoption does not create monthly registration',profile['registration_completed_at'] is None)
        check('own adopted inventory readable',http('customer','/inventory/items/'+item_id).status_code==200)
        check('other adopted inventory denied',http('other','/inventory/items/'+item_id).status_code in (403,404))
        check('sold company product removed',http('other',product).status_code==404)
        check('buyer can view adopted public photo',http('customer','/commerce/media/'+selected).status_code==200)
        check('other cannot view bought photo',http('other','/commerce/media/'+selected).status_code==404)
        check('unpublished company photo not inherited',http('customer','/commerce/media/'+unused).status_code==404)
        fund_before=rows('SELECT * FROM stage3_fund_records ORDER BY record_id')

        # Record customer's purchase cost, not Kaika's internal cost. All other
        # amounts are unknown until evidence arrives through the workflow.
        finance_path='/finance/items/'+item_id
        post('customer purchase evidence recorded','staff',finance_path,finance_path+'/facts',
            {'purchase_cost_yen':'30000','sale_gross_yen':'','channel_fee_yen':'','shipping_cost_yen':'',
             'materials_yen':'','kaika_fee_yen':'','return_adjustment_yen':'','monthly_allocation_yen':'',
             'basis':'actual','channel':'multichannel','shipping_scope':'','occurred_at':now(),
             'source_reference':'fixture-customer-purchase'})
        for channel,name in (('mercari_shops','【架空】Shops'),('yahoo_store','【架空】Yahoo')):
            post('manual account '+channel,'staff','/commerce/channels','/commerce/channels',
                {'channel':channel,'display_name':name,'evidence_reference':'fixture-unverified-account'})
        accounts=rows('SELECT * FROM stage4_channel_accounts ORDER BY account_id')
        check('two channels remain disabled',len(accounts)==2 and all(row['live_enabled']==0 for row in accounts))
        check('customer channel administration denied',http('customer','/commerce/channels').status_code==403)
        listing_page='/commerce/items/'+item_id+'/listing'
        check('customer own listing page readable',http('customer',listing_page).status_code==200)
        check('other listing page denied',http('other',listing_page).status_code in (403,404))
        post('listing immutable version proposed','staff',listing_page,listing_page,
            {'title':'【架空】出品提案バッグ','description':'購入時の商品写真と説明を再利用した検証提案。',
             'condition':'中古・検証用','price_yen':'40000','minimum_price_yen':'35000','photo_refs':[selected],
             'accounts':[row['account_id'] for row in accounts],
             'valid_until':(datetime.now(ZoneInfo('Asia/Tokyo'))+timedelta(days=7)).isoformat(),
             'evidence_reference':'fixture-listing-proposal'})
        version=rows('SELECT * FROM stage4_listing_versions')[0]
        approval='/commerce/listings/'+version['version_id']+'/approve'
        post('customer approves exact version','customer',listing_page,approval,{'evidence_reference':'fixture-customer-approval'})
        check('customer single approval not enough',len(rows('SELECT decision_id FROM stage4_listing_decisions'))==1)
        post('staff approves same version','staff',listing_page,approval,{'evidence_reference':'fixture-staff-approval'})
        check('dual approvals share exact content hash',all(row['content_hash']==version['content_hash'] for row in rows('SELECT content_hash FROM stage4_listing_decisions')))
        publication='/commerce/listings/'+version['version_id']+'/publish'
        for index,account in enumerate(accounts):
            post('manual publication '+str(index),'staff',listing_page,publication,
                {'account_id':account['account_id'],'external_listing_id':'fixture-listing-'+str(index),'evidence_reference':'fixture-manual-publication'})
        listings=rows('SELECT * FROM stage4_channel_listings ORDER BY listing_id')
        check('two manual listings one physical item',len(listings)==2 and all(row['physical_item_id']==item_id for row in listings))
        check('catalog registration reused no new work',not rows('SELECT work_id FROM business_work_records'))
        check('no registration date fabricated for reuse',rows('SELECT registration_completed_at FROM business_item_profiles')[0]['registration_completed_at'] is None)
        csv_path='/commerce/channels/csv-preview'
        csv_stream=io.StringIO(newline='')
        writer=csv.writer(csv_stream)
        writer.writerow(['listing_id','external_order_id','sale_gross_yen','occurred_at','evidence_reference'])
        writer.writerow([listings[0]['listing_id'],'FIXTURE-CSV-PREVIEW-ONLY',31234,now(),'fixture-csv-source'])
        order_count_before_csv=len(rows('SELECT order_id FROM stage4_channel_orders'))
        csv_response,_=post('CSV preview is read only','staff','/commerce/channels',csv_path,
            {'csv_file':(io.BytesIO(csv_stream.getvalue().encode('utf-8')),'fixture-orders.csv')},expected=200)
        csv_body=csv_response.get_data(as_text=True)
        check('CSV preview shows actual order number','FIXTURE-CSV-PREVIEW-ONLY' in csv_body)
        check('CSV preview shows actual integer price','<td>31234</td>' in csv_body)
        check('CSV preview creates no external orders',len(rows('SELECT order_id FROM stage4_channel_orders'))==order_count_before_csv)
        check('customer cannot use staff CSV preview',http('customer',csv_path,{}).status_code==403)
        audit_path='/commerce/channels/audit'
        post('valid approval audit creates no stop','staff','/commerce/channels',audit_path,{})
        check('valid dual-approved listings need no stop',not rows('SELECT task_id FROM stage4_stop_tasks'))
        order_path='/commerce/channel-orders'
        _,external_form=post('manual external order accepted','staff','/commerce/channels',order_path,
            {'listing_id':listings[0]['listing_id'],'external_order_id':'fixture-external-order','sale_gross_yen':'40000','evidence_reference':'fixture-order-source'})
        channel_order=rows('SELECT * FROM stage4_channel_orders')[0]
        check('external order status accepted',channel_order['initial_status']=='accepted')
        check('external order replay accepted',http('staff',order_path,external_form).status_code==302)
        check('external replay no duplicate order',len(rows('SELECT order_id FROM stage4_channel_orders'))==1)
        check('external replay no duplicate sale',len(rows("SELECT observation_id FROM business_item_observations WHERE kind='sold'"))==1)
        check('external amount disagreement not silently overwritten',http('staff',order_path,{**external_form,'sale_gross_yen':'39000'}).status_code==409)
        post('competing external order recorded as conflict','staff','/commerce/channels',order_path,
            {'listing_id':listings[1]['listing_id'],'external_order_id':'fixture-conflicting-order','sale_gross_yen':'40000','evidence_reference':'fixture-conflict-source'})
        conflicts=rows("SELECT * FROM stage4_channel_orders WHERE initial_status='conflict'")
        check('conflicting order does not create second sale',len(conflicts)==1 and len(rows("SELECT observation_id FROM business_item_observations WHERE kind='sold'"))==1)
        check('one accepted individual-item order claim',len(rows('SELECT physical_item_id FROM stage4_order_claims'))==1)
        packing=rows("SELECT task_id FROM stage4_order_tasks WHERE order_id=? AND kind='packing'",(channel_order['order_id'],))[0]['task_id']
        task_path='/commerce/tasks/'+packing
        task_deadline=(datetime.now(ZoneInfo('Asia/Tokyo'))+timedelta(days=1)).replace(microsecond=0).isoformat()
        _,task_form=post('packing task gets staff and deadline','staff','/commerce/channels',task_path,
            {'status':'working','assignee_user_id':str(staff_id),'deadline_at':task_deadline,
             'note':'fixture-packing-in-progress','evidence_reference':'fixture-task-evidence'})
        task_record=rows('SELECT * FROM stage4_order_task_events WHERE task_id=?',(packing,))[0]
        check('task saved correct staff',task_record['assignee_user_id']==staff_id and task_record['status']=='working')
        check('task deadline retains correct instant',datetime.fromisoformat(task_record['deadline_at'])==datetime.fromisoformat(task_deadline))
        check('task update replay accepted',http('staff',task_path,task_form).status_code==302)
        check('task replay no duplicate history',len(rows('SELECT event_id FROM stage4_order_task_events WHERE task_id=?',(packing,)))==1)
        ship_path='/commerce/channel-orders/'+channel_order['order_id']+'/ship'
        post('shipment held until every listing stop confirmed','staff','/commerce/channels',ship_path,
            {'tracking_reference':'FIXTURE-TRACK-1','evidence_reference':'fixture-shipment'},expected=409)
        tasks=rows('SELECT * FROM stage4_stop_tasks ORDER BY task_id')
        check('all recorded outlets get stop tasks',set(row['listing_id'] for row in tasks)==set(row['listing_id'] for row in listings))
        first_task='/commerce/stop-tasks/'+tasks[0]['task_id']
        post('failed stop remains work','staff','/commerce/channels',first_task,{'status':'failed','evidence_reference':'fixture-stop-failed'})
        post('failed stop still prevents shipment','staff','/commerce/channels',ship_path,
            {'tracking_reference':'FIXTURE-TRACK-1','evidence_reference':'fixture-shipment'},expected=409)
        for task in tasks:
            post('stop confirmed '+task['task_id'],'staff','/commerce/channels','/commerce/stop-tasks/'+task['task_id'],
                {'status':'confirmed_stopped','evidence_reference':'fixture-manual-stop-verification'})
        _,shipment=post('shipment records Stage3 charge drafts','staff','/commerce/channels',ship_path,
            {'tracking_reference':'FIXTURE-TRACK-1','evidence_reference':'fixture-shipment'})
        check('shipment replay accepted',http('staff',ship_path,shipment).status_code==302)
        check('shipment event recorded once',len(rows("SELECT observation_id FROM business_item_observations WHERE kind='shipped'"))==1)
        check('single shipment record',len(rows('SELECT shipment_id FROM stage4_order_shipments'))==1)
        drafts=rows('SELECT * FROM stage3_charge_drafts')
        check('bundle and sales fee candidates only once',len(drafts)==2 and set(row['fee_kind'] for row in drafts)=={'work_bundle','sales_fee'})
        check('unapproved pricing stays review',all(row['status']=='needs_review' for row in drafts))
        check('shipment does not add purchased funds',rows('SELECT * FROM stage3_fund_records ORDER BY record_id')==fund_before)
        check('customer sees finance after linked sale',http('customer',finance_path).status_code==200)
        check('other customer cannot see finance',http('other',finance_path).status_code in (403,404))
        conn=module.get_db()
        try:
            profit=ProfitService(FinanceStore(Registry(conn))).profit(Actor(owner_id),item_id)
            check('sale facts use original 40000 source',profit['facts']['sale_gross_yen']==40000)
            check('buyer purchase cost remains 30000',profit['facts']['purchase_cost_yen']==30000)
            check('unverified costs do not become certain profit',profit['realized_profit_yen'] is None)
        finally:
            conn.close()
        return_path='/commerce/channel-orders/'+channel_order['order_id']+'/return'
        _,returned=post('return linked to original external order','staff','/commerce/channels',return_path,
            {'reason':'fixture-condition-difference','evidence_reference':'fixture-return-received'})
        check('return replay accepted',http('staff',return_path,returned).status_code==302)
        check('one return record',len(rows('SELECT return_id FROM stage4_order_returns'))==1)
        returned_profile=rows('SELECT * FROM business_item_profiles')[0]
        check('return keeps same physical item on hold',returned_profile['held']==1 and returned_profile['physical_state']=='received' and len(rows('SELECT physical_item_id FROM business_items'))==1)
        check('return does not erase original sale or charge',len(rows("SELECT observation_id FROM business_item_observations WHERE kind='sold'"))==1 and len(rows('SELECT draft_id FROM stage3_charge_drafts'))==2)
        check('return does not issue cash refund',rows('SELECT * FROM stage3_fund_records ORDER BY record_id')==fund_before)
        conn=module.get_db()
        try:
            profit=ProfitService(FinanceStore(Registry(conn))).profit(Actor(owner_id),item_id)
            check('returned profit held',profit['realized_profit_yen'] is None and profit['recognition']=='return_hold')
        finally:
            conn.close()
        stops_before_audit=rows('SELECT * FROM stage4_stop_tasks ORDER BY task_id')
        _,audit_form=post('post-return approval audit retains existing stop records','staff','/commerce/channels',audit_path,{})
        check('audit replay accepted',http('staff',audit_path,audit_form).status_code==302)
        post('fresh audit does not duplicate existing stop work','staff','/commerce/channels',audit_path,{})
        check('audit does not duplicate stop queue',rows('SELECT * FROM stage4_stop_tasks ORDER BY task_id')==stops_before_audit)
        held_fields=form('staff',listing_page,photo_path)
        check('returned held item blocks extra photos',http('staff',photo_path,{**held_fields,
            'photos':(io.BytesIO(image_bytes('#aaccee')),'fixture-return-held.png')}).status_code==409)
        inventory_page='/inventory/items/'+item_id
        post('return hold released through inventory form','staff',inventory_page,inventory_page+'/events',
            {'kind':'release_hold','occurred_at':now(),'note':'fixture-return-condition-reviewed'})
        released_profile=rows('SELECT * FROM business_item_profiles')[0]
        check('release retains historical sale and shipment timestamps',released_profile['sold_at']==returned_profile['sold_at'] and released_profile['shipped_at']==returned_profile['shipped_at'])
        check('released returned item is currently available at Kaika',released_profile['held']==0 and released_profile['physical_state']=='received' and released_profile['sales_state']=='unlisted')
        photo_before=len(rows('SELECT media_id FROM stage4_media'))
        post('returned released item accepts new photographs','staff',listing_page,photo_path,
            {'photos':(io.BytesIO(image_bytes('#aaccee')),'fixture-return-rephotograph.png')})
        after_photos=rows('SELECT * FROM stage4_media ORDER BY recorded_at,media_id')
        new_photo=next(row for row in after_photos if row['media_id'] not in {old['media_id'] for old in media})
        check('return photograph adds just one media record',len(after_photos)==photo_before+1)
        check('owner can view new return photograph',http('customer','/commerce/media/'+new_photo['media_id']).status_code==200)
        check('other customer cannot view return photograph',http('other','/commerce/media/'+new_photo['media_id']).status_code==404)
        check('raw static return photograph private',http('customer','/static/'+new_photo['storage_path']).status_code in (403,404))
        check('return photograph creates no automatic work or charges',not rows('SELECT work_id FROM business_work_records') and len(rows('SELECT draft_id FROM stage3_charge_drafts'))==2)

        # A separate fictional unsold item proves the return-dispatch boundary
        # through actual HTTP without cancelling or rewriting the sale above.
        from stage4_common import Stage4Store
        from stage4_listings import ListingService
        from stage4_channels import ChannelService
        conn=module.get_db()
        try:
            registry=Registry(conn)
            unsold=registry.create_item(Actor(owner_id),'【架空】返送前の出品停止確認',occurred_at=now(),
                evidence_reference='fixture-unsold',source_key='fixture-unsold')['physical_item_id']
            for kind in ('received','photographed','registration_completed'):
                registry.record_event(staff,unsold,kind,now(),'fixture-unsold:'+kind,'fixture-unsold')
            store=Stage4Store(registry)
            assignment=registry._roles(unsold)['owner']['assignment_id']
            store.execute('INSERT INTO stage4_media VALUES(?,?,?,?,?,?,?,?)',
                ('fixture-unsold-photo',unsold,assignment,'fixture-unsold-metadata-only.jpg','a'*64,123,staff_id,now()))
            listing_api=ListingService(store)
            proposal=listing_api.propose(Actor(owner_id),unsold,title='【架空】返送前の出品停止確認',description='検証専用',
                condition='検証専用',photo_references=['fixture-unsold-photo'],price_gross_yen=40000,
                minimum_price_gross_yen=40000,channels=[dict(channel=a['channel'],account_id=a['account_id']) for a in accounts],
                valid_until=(datetime.now(ZoneInfo('Asia/Tokyo'))+timedelta(days=7)).isoformat(),
                source_key='fixture-unsold-proposal',evidence_reference='fixture',occurred_at=now())
            for actor in (Actor(owner_id),staff):
                listing_api.approve(actor,proposal['version_id'],source_key='fixture-unsold-approve:'+str(actor.user_id),
                    evidence_reference='fixture',occurred_at=now())
            channel_api=ChannelService(store)
            for index,account in enumerate(accounts):
                channel_api.record_listing(staff,proposal['version_id'],account['account_id'],external_listing_id='fixture-unsold:'+str(index),
                    source_key='fixture-unsold-listing:'+str(index),evidence_reference='fixture',occurred_at=now())
            conn.commit()
        finally:
            conn.close()
        unsold_page='/inventory/items/'+unsold
        unsold_stop='/commerce/items/'+unsold+'/stop-listings'
        unsold_event=unsold_page+'/events'
        post('unsold return rejects still-active external listings','staff',unsold_page,unsold_event,
            {'kind':'return_shipped','occurred_at':now(),'note':'fixture-unsold-return'},expected=409)
        check('blocked unsold return leaves custody unchanged',rows('SELECT physical_state FROM business_item_profiles WHERE physical_item_id=?',(unsold,))[0]['physical_state']=='received')
        check('customer cannot request staff listing stops',http('customer',unsold_stop,{}).status_code==403)
        check('listing stop request requires CSRF',http('staff',unsold_stop,{}).status_code==400)
        _,stop_form=post('staff queues all unsold item listings','staff','/commerce/channels',unsold_stop,
            {'reason':'お客様への返送','evidence_reference':'fixture-return-agreement'})
        check('listing stop request replay accepted',http('staff',unsold_stop,stop_form).status_code==302)
        check('stop form bound to exact physical item',http('staff','/commerce/items/'+item_id+'/stop-listings',stop_form).status_code==403)
        unsold_tasks=rows('''SELECT t.task_id FROM stage4_stop_tasks t JOIN stage4_channel_listings l
            ON l.listing_id=t.listing_id WHERE l.physical_item_id=?''',(unsold,))
        check('repeat unsold request creates exactly two stop tasks',len(unsold_tasks)==2)
        for task in unsold_tasks:
            post('unsold market stop confirmation','staff','/commerce/channels','/commerce/stop-tasks/'+task['task_id'],
                {'status':'confirmed_stopped','evidence_reference':'fixture-stopped-market'})
        post('unsold return allowed after all market stops','staff',unsold_page,unsold_event,
            {'kind':'return_shipped','occurred_at':now(),'note':'fixture-unsold-return'})
        future=(datetime.now(ZoneInfo('Asia/Tokyo'))+timedelta(days=1)).isoformat()
        post('future item event rejected through HTTP','staff',unsold_page,unsold_event,
            {'kind':'hold','occurred_at':future,'note':'fixture-future'},expected=400)
        post('future work actual rejected through HTTP','staff',unsold_page,unsold_page+'/work',
            {'kind':'support','performed_at':future,'duration_minutes':'1','note':'fixture-future'},expected=400)
        check('unsold return and rejected future work create no fees',len(rows('SELECT draft_id FROM stage3_charge_drafts'))==2 and not rows('SELECT work_id FROM business_work_records'))
        check('legacy financial tables unchanged',baseline()==before)
        check('PSP and scheduler disabled',not module.STRIPE_ENABLED and not module.SCHEDULER_ENABLED)
        check('no live channel flags enabled',not rows('SELECT account_id FROM stage4_channel_accounts WHERE live_enabled!=0'))
        app.config['STAGE4_COMMERCE_ENABLED']=False
        check('disabled commerce returns404',http('staff','/commerce').status_code==404)
        check('disabled private media returns404',http('customer','/commerce/media/'+selected).status_code==404)
        return dict(scope='Copied render_app, actual HTTP forms, fictional isolated SQLite only',checks=checks,requests=requests,
            passed=sum(x['pass'] for x in checks),failed=sum(not x['pass'] for x in checks),
            server_errors=sum(x['status']>=500 for x in requests),
            fixture_only=['50000 JPY fictional deposit','test_payment_confirmation ownership rule','two manual fake storefronts'],
            not_verified=['production data','PostgreSQL','native devices','marketplace/API permission','real external publication',
                          'real payment','real refund','real notification','production ownership rule'])
    except Exception as exc:
        return dict(scope='isolated Stage4 HTTP integration',checks=checks,requests=requests,
            passed=sum(x['pass'] for x in checks),failed=max(1,sum(not x['pass'] for x in checks)),
            server_errors=sum(x['status']>=500 for x in requests),error_type=type(exc).__name__,error=str(exc),traceback=traceback.format_exc())


def main():
    sys.stdout.reconfigure(encoding='utf-8',errors='replace')
    original=Path.cwd()
    preview=Path(tempfile.mkdtemp(prefix='kaika-stage4-verification-')).resolve()
    runtime=sandbox.mirror_source(preview,None,None)
    sandbox.isolate_environment(preview,18784)
    connections=sandbox.install_runtime_boundary(preview,18784)
    sys.path[:]=[str(runtime)]+[p for p in sys.path if p and not sandbox.inside(Path(p).resolve(),sandbox.SOURCE)]
    os.chdir(runtime)
    diagnostics=io.StringIO()
    try:
        with redirect_stdout(diagnostics),redirect_stderr(diagnostics):
            loaded=importlib.import_module('render_app')
            users=sandbox.seed_preview(loaded.module,preview)
            from stage2_migration import prepare_stage2_fixture
            from stage3_migration import prepare_stage3_fixture
            from stage4_migration import prepare_stage4_fixture
            for migrate in (prepare_stage2_fixture,prepare_stage3_fixture,prepare_stage4_fixture):
                migrate(loaded.module,allowed_test_path=preview/'preview.sqlite3')
            loaded.module.app.config.update(STAGE2_INVENTORY_ENABLED=True,STAGE2_FREE_LIMIT_PROPOSAL_ENABLED=True,STAGE3_FINANCE_ENABLED=True)
            report=run(loaded.module,users)
    except Exception as exc:
        report=dict(scope='isolated Stage4 integration',passed=0,failed=1,server_errors=1,
            error_type=type(exc).__name__,error=str(exc),traceback=traceback.format_exc())
    finally:
        for conn in connections:
            conn.close()
        os.chdir(original)
    if report['failed'] or report['server_errors']:
        report['isolated_runtime_diagnostics']=diagnostics.getvalue()[-18000:]
    path=preview/'verification.json'
    path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    summary={key:report.get(key) for key in ('passed','failed','server_errors','error_type','error')}
    summary.update(failed_checks=[row['name'] for row in report.get('checks',[]) if not row['pass']],
                   requests=len(report.get('requests',[])),report_path=str(path))
    print('STAGE4_VERIFICATION='+json.dumps(summary,ensure_ascii=False))
    return 1 if report['failed'] or report['server_errors'] else 0


if __name__=='__main__':
    raise SystemExit(main())
