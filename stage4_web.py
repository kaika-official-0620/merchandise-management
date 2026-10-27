"""Stage 4 test-commerce screens. No external publication/payment connector."""
from contextlib import contextmanager
from datetime import datetime,timedelta
from functools import wraps
import hashlib
import json
import os
from pathlib import Path
import uuid

from flask import Blueprint,abort,flash,redirect,render_template,request,send_file,url_for
from flask_login import current_user,login_required
from werkzeug.exceptions import HTTPException

from inventory_registry_web import RegistryWeb,JST,_text,_time
from item_registry import Actor,RegistryError,_now
from stage3_web import _amount
from stage4_common import Stage4Store

CHANNEL_NAMES={'mercari_shops':'メルカリShops','yahoo_store':'Yahoo!オークションストア',
    'rakuma_official':'ラクマ公式ショップ','wholesale':'業者卸','dealer_auction':'業者競売','other':'その他（要確認）'}
ERROR_MESSAGES={
    'catalog_item_unavailable':'この商品は予約中、または購入済みです。商品一覧を開き直してください。',
    'catalog_reservation_expired':'予約期限を過ぎています。予約を解除し、商品を選び直してください。',
    'catalog_order_not_reserved':'この予約はすでに処理済み、または所有移転の条件を確認中です。購入記録を確認してください。',
    'catalog_photo_required':'掲載する写真を1枚以上選んでください。',
    'catalog_photo_not_owned':'選択した写真と、この商品の所有情報を確認してください。',
    'listing_customer_approval_required':'この版に対するお客様の承認が必要です。',
    'listing_kaika_approval_required':'この版に対する開花の承認が必要です。',
    'listing_expired':'承認期限が過ぎています。新しい出品案で両者の承認を受けてください。',
    'listing_item_changed':'商品状態が変わっています。新しい出品案で再確認してください。',
    'listing_superseded':'新しい出品案があります。最新の版を開いてください。',
    'listing_rejected':'この版は差戻し済みです。修正した新しい版を作成してください。',
    'order_price_requires_reapproval':'訂正した販売額が承認済みの価格と異なるため、発送を保留しました。原資料と承認内容を確認してください。',
}


def staff():
    return current_user.is_authenticated and current_user.is_admin() and current_user.has_permission('merchandise')


class CommerceWeb(RegistryWeb):
    def actor(self):
        return Actor(int(current_user.get_id()),staff=staff())

    @contextmanager
    def db(self,write=False):
        with super().db(write=write) as registry:
            store=Stage4Store(registry)
            store.require_ready()
            yield store

    def catalog(self,store):
        from stage4_catalog import CatalogService
        return CatalogService(store,ownership_rule=self.app.config.get('STAGE4_OWNERSHIP_RULE','undecided'))

    def action(self,purpose,target=''):
        key=self.verify(purpose,target)
        # Server-signed form time is stable for retry; expiry still uses the
        # service's current clock, not this value or a user-supplied timestamp.
        payload=self.signer.loads(request.form['operation_token'],max_age=86400)
        return key,payload['issued_at']

    def photos(self,store,item_id):
        owner=store.registry._roles(item_id).get('owner',{})
        result=[]
        for row in store.query('SELECT media_id FROM stage4_media WHERE physical_item_id=? ORDER BY recorded_at,media_id',(item_id,)):
            if owner.get('kind')=='kaika' and staff() or store.listing_photo_allowed(item_id,owner.get('user_id'),row['media_id']):
                result.append({'reference':row['media_id'],'url':url_for('commerce.media',media_id=row['media_id'])})
        if owner.get('kind')=='customer':
            for row in store.query('''SELECT p.photo_path FROM stage2_photo_records p JOIN business_item_links l
                ON l.merchandise_id=p.merchandise_id WHERE l.physical_item_id=? AND p.owner_user_id=?
                AND p.removed_from_item_at IS NULL''',(item_id,owner['user_id'])):
                result.append({'reference':row['photo_path'],'url':url_for('static',filename=row['photo_path'])})
        return result


def register_stage4_commerce(runtime):
    app=runtime.app
    if 'commerce' in app.blueprints:
        return app.extensions['stage4_commerce']
    app.config.setdefault('STAGE4_COMMERCE_ENABLED',os.environ.get('STAGE4_COMMERCE_ENABLED')=='1')
    app.config.setdefault('STAGE4_OWNERSHIP_RULE','undecided')
    service=CommerceWeb(runtime)
    bp=Blueprint('commerce',__name__)

    def allowed(staff_only=False):
        def wrap(view):
            @wraps(view)
            @login_required
            def inner(*args,**kwargs):
                if not all(app.config.get(flag) is True for flag in ('STAGE2_INVENTORY_ENABLED','STAGE3_FINANCE_ENABLED','STAGE4_COMMERCE_ENABLED')):
                    abort(404)
                if staff_only and not staff() or not staff() and current_user.role!='user':
                    abort(403)
                try:
                    return view(*args,**kwargs)
                except HTTPException:
                    raise
                except RegistryError as exc:
                    message=ERROR_MESSAGES.get(exc.code,str(exc))
                    if message==exc.code:
                        message='対象の記録・権限・確認の順番が現在の状態と一致しません。最新の画面を開き直して確認してください。'
                    return render_template('commerce/error.html',message=message),exc.status
                except ValueError:
                    return render_template('commerce/error.html',message='入力内容と対象の記録を確認してください。'),400
            return inner
        return wrap

    @bp.get('/commerce')
    @allowed()
    def index():
        with service.db() as store:
            products=service.catalog(store).list_catalog(service.actor())
            companies=service.catalog(store).list_company_items(service.actor()) if staff() else []
        return render_template('commerce/index.html',products=products,companies=companies,staff=staff())

    @bp.post('/commerce/company')
    @allowed(True)
    def create_company():
        key,when=service.action('commerce_create')
        with service.db(True) as store:
            result=service.catalog(store).create_company_item(service.actor(),title=_text('title',300,True),
                purchase_cost_yen=_amount('purchase_cost_yen'),occurred_at=when,
                evidence_reference=_text('evidence_reference',1000,True),source_key=key)
        return redirect(url_for('commerce.company',item_id=result['physical_item_id']))

    @bp.get('/commerce/company/<item_id>')
    @allowed(True)
    def company(item_id):
        with service.db() as store:
            detail=service.catalog(store).get_company_item(service.actor(),item_id)
            photos=service.photos(store,item_id)
        return render_template('commerce/company.html',item=detail,photos=photos,staff=True)

    @bp.post('/commerce/company/<item_id>/revise')
    @allowed(True)
    def revise(item_id):
        key,when=service.action('commerce_revise',item_id)
        with service.db(True) as store:
            service.catalog(store).revise(service.actor(),item_id,title=_text('title',300,True),
                description=_text('description',10000,True),condition=_text('condition',2000,True),
                price_yen=_amount('price_yen'),photo_refs=request.form.getlist('photo_refs'),
                occurred_at=when,evidence_reference=_text('evidence_reference',1000,True),source_key=key)
        return redirect(url_for('commerce.company',item_id=item_id))

    @bp.post('/commerce/company/<item_id>/publish')
    @allowed(True)
    def publish_catalog(item_id):
        key,when=service.action('commerce_catalog_publish',item_id)
        with service.db(True) as store:
            service.catalog(store).publish(service.actor(),item_id,_text('revision_id',100,True),
                occurred_at=when,evidence_reference=_text('evidence_reference',1000,True),source_key=key)
        return redirect(url_for('commerce.product',item_id=item_id))

    @bp.get('/commerce/products/<item_id>')
    @allowed()
    def product(item_id):
        with service.db() as store:
            item=service.catalog(store).get_catalog(service.actor(),item_id)
        return render_template('commerce/product.html',item=item,staff=staff())

    @bp.post('/commerce/products/<item_id>/reserve')
    @allowed()
    def reserve(item_id):
        if staff() or request.form.get('agree')!='yes':
            abort(400)
        key,when=service.action('commerce_reserve',item_id)
        with service.db(True) as store:
            service.catalog(store).reserve(service.actor(),item_id,_text('revision_id',100,True),
                custody_choice=_text('custody_choice',20,True),consent_reference='画面の価格・商品版・保管希望への同意：'+key,
                occurred_at=when,source_key=key)
        return redirect(url_for('commerce.orders'))

    @bp.get('/commerce/orders')
    @allowed()
    def orders():
        with service.db() as store:
            orders=service.catalog(store).list_orders(service.actor())
        return render_template('commerce/orders.html',orders=orders,staff=staff())

    @bp.post('/commerce/orders/<order_id>/<action>')
    @allowed()
    def order_action(order_id,action):
        if action not in ('cancel','confirm','handover'):
            abort(404)
        if action!='cancel' and not staff():
            abort(403)
        key,when=service.action('commerce_order_'+action,order_id)
        with service.db(True) as store:
            method={'cancel':service.catalog(store).cancel,'confirm':service.catalog(store).confirm_test_purchase,
                    'handover':service.catalog(store).handover_received}[action]
            method(service.actor(),order_id,occurred_at=when,evidence_reference=_text('evidence_reference',1000,True),source_key=key)
        return redirect(url_for('commerce.orders'))

    @bp.post('/commerce/orders/expire')
    @allowed(True)
    def expire():
        key,when=service.action('commerce_expire')
        with service.db(True) as store:
            service.catalog(store).expire_orders(service.actor(),occurred_at=when,
                evidence_reference='期限経過した検証予約の解除確認',source_key=key)
        return redirect(url_for('commerce.orders'))

    @bp.post('/commerce/items/<item_id>/photos')
    @allowed(True)
    def photos(item_id):
        key,when=service.action('commerce_photo',item_id)
        files=[f for f in request.files.getlist('photos') if f.filename]
        if not files or len(files)>20 or request.content_length is None or request.content_length>40*1024*1024:
            abort(413)
        digests=[]
        for file in files:
            data=file.stream.read(15*1024*1024+1)
            if len(data)>15*1024*1024:
                abort(413)
            digests.append(hashlib.sha256(data).hexdigest())
            file.stream.seek(0)
        created=[]
        try:
            with service.db(True) as store:
                def write():
                    store.lock_item(item_id)
                    owner=store.registry._roles(item_id).get('owner',{})
                    if owner.get('kind') not in ('kaika','customer'):
                        abort(409)
                    if owner['kind']=='customer':
                        item=store.registry.detail(service.actor(),item_id)
                        if item['held'] or item['custodian']!='kaika' or item['sales_state'] in ('sold','shipped'):
                            abort(409)
                    count=store.query('SELECT COUNT(*) AS n FROM stage4_media WHERE physical_item_id=?',(item_id,))[0]['n']
                    if count+len(files)>20:
                        abort(409,description='検証用の写真上限は1個品20枚です。')
                    for file in files:
                        path=runtime.save_validated_product_image(file,prefix='stage4_')
                        created.append(path)
                        resolved=(Path(app.static_folder)/path).resolve()
                        uploads=(Path(app.static_folder)/'uploads').resolve()
                        if uploads not in resolved.parents or resolved.is_symlink():
                            abort(409)
                        content=resolved.read_bytes()
                        store.execute('INSERT INTO stage4_media(media_id,physical_item_id,ownership_assignment_id,storage_path,content_sha256,byte_size,actor_user_id,recorded_at) VALUES (?,?,?,?,?,?,?,?)',
                            ('m4_'+uuid.uuid4().hex,item_id,owner['assignment_id'],path,hashlib.sha256(content).hexdigest(),len(content),service.actor().user_id,_now()))
                    return {'added':len(files)}
                store.operation(service.actor(),key,{'action':'photos','item':item_id,'files':digests},write,staff_required=True)
        except Exception:
            if created:
                runtime.remove_uploaded_relative_paths(created)
            raise
        return redirect(url_for('commerce.company',item_id=item_id) if owner_is_company(runtime,item_id) else url_for('commerce.listing_item',item_id=item_id))

    def owner_is_company(runtime,item_id):
        with service.db() as store:
            return store.registry._roles(item_id).get('owner',{}).get('kind')=='kaika'

    @bp.get('/commerce/media/<media_id>')
    @allowed()
    def media(media_id):
        with service.db() as store:
            store.authorize(service.actor())
            rows=store.query('SELECT * FROM stage4_media WHERE media_id=?',(media_id,))
            if not rows:
                abort(404)
            row=rows[0]
            if not staff():
                owner=store.registry._roles(row['physical_item_id']).get('owner',{})
                if owner.get('kind')=='kaika':
                    offer=service.catalog(store).get_catalog(service.actor(),row['physical_item_id'])
                    if media_id not in offer['photo_refs']:
                        abort(404)
                elif owner.get('user_id')!=service.actor().user_id or not store.listing_photo_allowed(row['physical_item_id'],service.actor().user_id,media_id):
                    abort(404)
        root=Path(app.static_folder).resolve()
        candidate=root/row['storage_path']
        path=candidate.resolve()
        if (root/'uploads') not in path.parents or not path.is_file() or any(p.is_symlink() or getattr(p,'is_junction',lambda:False)() for p in (candidate,*candidate.parents) if p!=root and root in p.parents):
            abort(404)
        return send_file(path,conditional=False,etag=False)

    @bp.get('/commerce/listings')
    @allowed()
    def listing_index():
        with service.db() as store:
            store.authorize(service.actor())
            items=store.registry.list_items(service.actor())
        return render_template('commerce/listings.html',items=items,staff=staff())

    @bp.route('/commerce/items/<item_id>/listing',methods=['GET','POST'])
    @allowed()
    def listing_item(item_id):
        from stage4_listings import ListingService
        from stage4_channels import ChannelService
        if request.method=='POST':
            key,when=service.action('commerce_listing',item_id)
            with service.db(True) as store:
                accounts=ChannelService(store).accounts(service.actor())
                selected=request.form.getlist('accounts')
                channels=[{'channel':a['channel'],'account_id':a['account_id']} for a in accounts if a['account_id'] in selected]
                if len(channels)!=len(set(selected)):
                    abort(400)
                ListingService(store).propose(service.actor(),item_id,title=_text('title',300,True),
                    description=_text('description',15000,True),condition=_text('condition',2000,True),
                    photo_references=request.form.getlist('photo_refs'),price_gross_yen=_amount('price_yen'),
                    minimum_price_gross_yen=_amount('minimum_price_yen'),channels=channels,
                    valid_until=_time(request.form.get('valid_until')),occurred_at=when,source_key=key,
                    evidence_reference=_text('evidence_reference',1000,True),supersedes_version_id=_text('supersedes_version_id',100) or None)
            return redirect(url_for('commerce.listing_item',item_id=item_id))
        with service.db() as store:
            item=store.registry.detail(service.actor(),item_id)
            versions=ListingService(store).list_versions(service.actor(),item_id)
            accounts=ChannelService(store).accounts(service.actor())
            photos=service.photos(store,item_id)
        return render_template('commerce/listing_item.html',item=item,versions=versions,accounts=accounts,photos=photos,staff=staff())

    @bp.post('/commerce/listings/<version_id>/<action>')
    @allowed()
    def listing_action(version_id,action):
        from stage4_listings import ListingService
        from stage4_channels import ChannelService
        key,when=service.action('commerce_listing_'+action,version_id)
        with service.db(True) as store:
            listing=ListingService(store).detail(service.actor(),version_id)
            values=dict(occurred_at=when,evidence_reference=_text('evidence_reference',1000,True),source_key=key)
            if action=='approve':
                ListingService(store).approve(service.actor(),version_id,**values)
            elif action=='reject':
                ListingService(store).reject(service.actor(),version_id,reason=_text('reason',1000,True),**values)
            elif action=='publish' and staff():
                ChannelService(store).record_listing(service.actor(),version_id,_text('account_id',100,True),
                    external_listing_id=_text('external_listing_id',200,True),**values)
            else:
                abort(403)
        return redirect(url_for('commerce.listing_item',item_id=listing['physical_item_id']))

    @bp.route('/commerce/channels',methods=['GET','POST'])
    @allowed(True)
    def channels():
        from stage4_channels import ChannelService
        if request.method=='POST':
            key,when=service.action('commerce_account')
            with service.db(True) as store:
                ChannelService(store).configure_account(service.actor(),channel=_text('channel',40,True),
                    display_name=_text('display_name',200,True),capabilities={},
                    evidence_reference=_text('evidence_reference',1000,True),source_key=key)
            return redirect(url_for('commerce.channels'))
        with service.db() as store:
            overview=ChannelService(store).overview(service.actor())
            accounts=ChannelService(store).accounts(service.actor())
        return render_template('commerce/channels.html',overview=overview,accounts=accounts,staff=True)

    @bp.post('/commerce/channels/audit')
    @allowed(True)
    def audit_approvals():
        from stage4_channels import ChannelService
        key,when=service.action('commerce_audit')
        with service.db(True) as store:
            ChannelService(store).audit_listing_approvals(service.actor(),occurred_at=when,source_key=key,
                evidence_reference='出品承認の期限・最新の版・却下状態の確認')
        return redirect(url_for('commerce.channels'))

    @bp.post('/commerce/channels/csv-preview')
    @allowed(True)
    def csv_preview():
        from stage4_channels import ChannelService
        service.action('commerce_csv_preview')
        file=request.files.get('csv_file')
        if not file:
            abort(400)
        with service.db() as store:
            api=ChannelService(store)
            preview=api.preview_order_csv(service.actor(),file.stream.read(2*1024*1024+1))
            overview=api.overview(service.actor())
            accounts=api.accounts(service.actor())
        return render_template('commerce/channels.html',overview=overview,accounts=accounts,csv_preview=preview,staff=True)

    @bp.post('/commerce/items/<item_id>/stop-listings')
    @allowed(True)
    def request_item_stop(item_id):
        from stage4_channels import ChannelService
        key,when=service.action('commerce_item_stop',item_id)
        with service.db(True) as store:
            ChannelService(store).request_item_stop(service.actor(),item_id,
                reason=_text('reason',1000,True),occurred_at=when,source_key=key,
                evidence_reference=_text('evidence_reference',1000,True))
        flash('出品停止の確認作業へ追加しました。外部の販売先で停止し、確認結果を記録してください。','info')
        return redirect(url_for('commerce.channels'))

    @bp.post('/commerce/channel-orders')
    @allowed(True)
    def record_order():
        from stage4_channels import ChannelService
        key,when=service.action('commerce_external_order')
        with service.db(True) as store:
            ChannelService(store).record_order(service.actor(),_text('listing_id',100,True),
                external_order_id=_text('external_order_id',200,True),sale_gross_yen=_amount('sale_gross_yen'),
                occurred_at=when,source_key=key,evidence_reference=_text('evidence_reference',1000,True))
        return redirect(url_for('commerce.channels'))

    @bp.post('/commerce/channel-orders/<order_id>/<action>')
    @allowed(True)
    def fulfillment(order_id,action):
        from stage4_channels import ChannelService
        key,when=service.action('commerce_fulfill_'+action,order_id)
        with service.db(True) as store:
            api=ChannelService(store)
            args=dict(occurred_at=when,source_key=key,evidence_reference=_text('evidence_reference',1000,True))
            if action=='ship':
                api.ship_order(service.actor(),order_id,tracking_reference=_text('tracking_reference',200,True),**args)
            elif action=='return':
                api.return_order(service.actor(),order_id,reason=_text('reason',1000,True),**args)
            elif action=='cancel':
                api.cancel_order(service.actor(),order_id,reason=_text('reason',1000,True),**args)
            elif action=='correct':
                api.correct_order(service.actor(),order_id,sale_gross_yen=_amount('sale_gross_yen'),reason=_text('reason',1000,True),**args)
            else:
                abort(404)
        return redirect(url_for('commerce.channels'))

    @bp.post('/commerce/tasks/<task_id>')
    @allowed(True)
    def update_task(task_id):
        from stage4_channels import ChannelService
        key,when=service.action('commerce_task',task_id)
        raw=_text('assignee_user_id',20)
        if raw and not raw.isdigit():
            abort(400)
        with service.db(True) as store:
            ChannelService(store).update_task(service.actor(),task_id,status=_text('status',20,True),note=_text('note',1000,True),
                assignee_user_id=int(raw) if raw else None,deadline_at=_time(request.form['deadline_at']) if request.form.get('deadline_at') else None,
                occurred_at=when,source_key=key,evidence_reference=_text('evidence_reference',1000,True))
        return redirect(url_for('commerce.channels'))

    @bp.post('/commerce/stop-tasks/<task_id>')
    @allowed(True)
    def stop_task(task_id):
        from stage4_channels import ChannelService
        key,when=service.action('commerce_stop',task_id)
        with service.db(True) as store:
            ChannelService(store).stop_task(service.actor(),task_id,status=_text('status',30,True),
                occurred_at=when,source_key=key,evidence_reference=_text('evidence_reference',1000,True))
        return redirect(url_for('commerce.channels'))

    @bp.after_request
    def private(response):
        response.headers.update({'Cache-Control':'private, no-store','X-Content-Type-Options':'nosniff'})
        return response

    @app.context_processor
    def context():
        return dict(stage4_commerce_available=all(app.config.get(f) is True for f in ('STAGE2_INVENTORY_ENABLED','STAGE3_FINANCE_ENABLED','STAGE4_COMMERCE_ENABLED')),
            commerce_csrf=service.csrf,commerce_operation=service.operation,commerce_channels=CHANNEL_NAMES,
            commerce_expiry=(datetime.now(JST)+timedelta(days=7)).strftime('%Y-%m-%dT%H:%M'),
            commerce_ownership_rule=app.config.get('STAGE4_OWNERSHIP_RULE','undecided'))
    app.register_blueprint(bp)
    app.extensions['stage4_commerce']=service
    return service
