"""Opt-in deletion requests and review. No real erasure or startup migration."""
from functools import wraps
import os
import time
from flask import Blueprint, abort, jsonify, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required, logout_user
from werkzeug.security import check_password_hash
from account_lifecycle import LifecycleService, REVIEW_LABELS, access_allowed_in_store, outbound_allowed_in_store
from inventory_registry_web import RegistryWeb
from item_registry import Actor, RegistryError
from account_erasure import ErasureService, TASKS as ERASURE_TASKS, ACTIONS as ERASURE_ACTIONS
from account_erasure_notice import NoticeService
from account_lifecycle import tables

LABELS = {'requested':'削除申請を受け付けました','reviewing':'削除の対象と手続きを確認中',
    'withdrawn':'申請を取り下げました','access_closed':'利用停止済み・削除作業待ち',
    'erasure_pending':'利用停止済み・関連データの削除は未完了','completed':'削除完了'}


class LifecycleWeb(RegistryWeb):
    def _allowed(self, customer_id, predicate):
        try:
            with self.db() as store:
                if self.app.config.get('ACCOUNT_LIFECYCLE_ENABLED') is True:
                    LifecycleService(store).ready()
                return predicate(store, int(customer_id))
        except Exception:
            return False

    def access_allowed(self, customer_id):
        return self._allowed(customer_id, access_allowed_in_store)

    def outbound_allowed(self, customer_id):
        return self._allowed(customer_id, outbound_allowed_in_store)

    def actor(self):
        return Actor(int(current_user.get_id()))

    def reauthenticate(self):
        customer_id, now = int(current_user.get_id()), int(time.time())
        password, good = request.form.get('password',''), False
        with self.db(True) as store:
            api = LifecycleService(store)
            api.ready()
            api.lock(customer_id)
            rows = store.query('SELECT * FROM account_deletion_reauth WHERE customer_id=?',(customer_id,))
            row = rows[0] if rows else {'failures':0,'window_started':now}
            if now-row['window_started'] >= 900:
                row = {'failures':0,'window_started':now}
            if row['failures'] < 5 and 0 < len(password) <= 1024:
                users = store.query('SELECT password_hash FROM users WHERE id=?',(customer_id,))
                try:
                    good = bool(users and check_password_hash(users[0]['password_hash'],password))
                except (ValueError,TypeError):
                    good = False
            store.execute('''INSERT INTO account_deletion_reauth(customer_id,failures,window_started) VALUES (?,?,?)
                ON CONFLICT(customer_id) DO UPDATE SET failures=excluded.failures,window_started=excluded.window_started''',
                (customer_id,0 if good else row['failures']+1,row['window_started']))
        if not good:
            abort(403, description='本人確認ができませんでした。パスワードをご確認ください。続けて失敗した場合は15分後にお試しください。')


def register_account_lifecycle(runtime):
    app = runtime.app
    if 'account_lifecycle' in app.extensions:
        return app.extensions['account_lifecycle']
    app.config.setdefault('ACCOUNT_LIFECYCLE_ENABLED',os.environ.get('ACCOUNT_LIFECYCLE_ENABLED')=='1')
    service = LifecycleWeb(runtime)
    app.extensions['account_lifecycle'] = service
    app.extensions['stage6_customer_active'] = outbound_allowed_in_store
    bp = Blueprint('account_lifecycle',__name__)

    def allowed(staff=False):
        def decorate(view):
            @wraps(view)
            @login_required
            def inner(*args,**kwargs):
                if app.config.get('ACCOUNT_LIFECYCLE_ENABLED') is not True:
                    abort(404)
                try:
                    with service.db() as store:
                        LifecycleService(store).authorize(service.actor(),staff_only=staff)
                    return view(*args,**kwargs)
                except RegistryError as error:
                    return render_template('account_deletion/error.html',code=error.code),error.status
            return inner
        return decorate

    @bp.get('/account-deletion-info')
    def information():
        return render_template('account_deletion/info.html',enabled=app.config.get('ACCOUNT_LIFECYCLE_ENABLED') is True)

    @bp.get('/account/deletion')
    @allowed()
    def page():
        with service.db() as store:
            row = LifecycleService(store).get(service.actor(),int(current_user.get_id()))
            notice_ready='account_erasure_notice_schema' in tables(store)
            notice=None
            if notice_ready and row and row['state']!='withdrawn':
                notice=NoticeService(store).get(service.actor(),int(current_user.get_id()),row['request_id'])
        return render_template('account_deletion/request.html',row=row,labels=LABELS,csrf=service.csrf(),
            token=service.operation('account_deletion_request'),cancel_token=service.operation('account_deletion_withdraw',row['request_id']) if row else '',
            completion_notice=notice,completion_notice_ready=notice_ready,
            notice_token=service.operation('account_completion_notice',row['request_id']) if row else '')

    @bp.post('/account/deletion/completion-notice')
    @allowed()
    def completion_notice():
        request_id=request.form.get('request_id','')
        source_key=service.verify('account_completion_notice',request_id)
        choice=request.form.get('completion_email','')
        if choice not in ('yes','no'):abort(400)
        service.reauthenticate()
        with service.db(True) as store:
            NoticeService(store).set_consent(service.actor(),request_id,choice=='yes',source_key=source_key)
        return redirect(url_for('account_lifecycle.page'))

    @bp.post('/account/deletion')
    @allowed()
    def submit():
        source_key=service.verify('account_deletion_request')
        if request.form.get('confirm') != 'delete_account':
            abort(400)
        service.reauthenticate()
        with service.db(True) as store:
            LifecycleService(store).request(service.actor(),source_key=source_key)
        return redirect(url_for('account_lifecycle.page'))

    @bp.post('/account/deletion/withdraw')
    @allowed()
    def withdraw():
        request_id = request.form.get('request_id','')
        service.verify('account_deletion_withdraw',request_id)
        service.reauthenticate()
        with service.db(True) as store:
            LifecycleService(store).withdraw(service.actor(),request_id)
        return redirect(url_for('account_lifecycle.page'))

    @bp.get('/admin/account-deletions')
    @allowed(staff=True)
    def admin_list():
        with service.db() as store:
            rows=store.query('SELECT customer_id,request_id,state,requested_at FROM account_deletion_requests ORDER BY requested_at DESC')
        return render_template('account_deletion/list.html',rows=rows,labels=LABELS)

    @bp.get('/admin/account-deletions/<int:customer_id>')
    @allowed(staff=True)
    def admin_detail(customer_id):
        with service.db() as store:
            api=LifecycleService(store)
            row=api.get(service.actor(),customer_id)
            if not row:
                abort(404)
            inventory=api.inventory(service.actor(),customer_id)
            erasure=ErasureService(store)
            erasure_ready=erasure.ready()
            plan=erasure.get(service.actor(),customer_id,row['request_id']) if erasure_ready else None
        return render_template('account_deletion/review.html',row=row,inventory=inventory,labels=LABELS,categories=REVIEW_LABELS,
            csrf=service.csrf(),token=service.operation('account_deletion_review',row['request_id']),
            erasure_ready=erasure_ready,erasure_plan=plan,erasure_tasks=ERASURE_TASKS,erasure_actions=ERASURE_ACTIONS,
            erasure_prepare_token=service.operation('account_erasure_prepare',row['request_id']),
            erasure_decide_token=service.operation('account_erasure_decide',row['request_id']))

    @bp.post('/admin/account-deletions/<int:customer_id>/plan')
    @allowed(staff=True)
    def erasure_prepare(customer_id):
        request_id=request.form.get('request_id','')
        service.verify('account_erasure_prepare',request_id)
        service.reauthenticate()
        with service.db(True) as store:
            ErasureService(store).prepare(service.actor(),customer_id,request_id)
        return redirect(url_for('account_lifecycle.admin_detail',customer_id=customer_id))

    @bp.post('/admin/account-deletions/<int:customer_id>/plan/decision')
    @allowed(staff=True)
    def erasure_decide(customer_id):
        request_id=request.form.get('request_id','')
        service.verify('account_erasure_decide',request_id)
        service.reauthenticate()
        try:
            revision=int(request.form.get('revision',''))
        except ValueError:
            abort(400)
        with service.db(True) as store:
            ErasureService(store).decide(service.actor(),customer_id,request_id,revision=revision,
                task_key=request.form.get('task_key',''),action=request.form.get('action',''),
                reason=request.form.get('reason',''),reference=request.form.get('reference',''),
                retain_until=request.form.get('retain_until') or None)
        return redirect(url_for('account_lifecycle.admin_detail',customer_id=customer_id))

    @bp.post('/admin/account-deletions/<int:customer_id>/review')
    @allowed(staff=True)
    def admin_review(customer_id):
        request_id=request.form.get('request_id','')
        service.verify('account_deletion_review',request_id)
        service.reauthenticate()
        with service.db(True) as store:
            LifecycleService(store).review(service.actor(),customer_id,request_id,request.form.get('category',''),
                request.form.get('reference',''),request.form.get('notice',''))
        return redirect(url_for('account_lifecycle.admin_detail',customer_id=customer_id))

    @bp.after_request
    def private_response(response):
        response.headers.update({'Cache-Control':'private, no-store','Referrer-Policy':'no-referrer',
                                 'X-Content-Type-Options':'nosniff'})
        return response

    app.register_blueprint(bp)

    @app.context_processor
    def deletion_context():
        return {'account_deletion_enabled':app.config.get('ACCOUNT_LIFECYCLE_ENABLED') is True}

    original_loader=app.login_manager._user_callback
    if original_loader:
        @app.login_manager.user_loader
        def load_active_user(user_id):
            user=original_loader(user_id)
            if user and not service.access_allowed(user_id):
                # Do not call logout_user here: it recursively loads current_user.
                session.clear()
                session['_remember']='clear'
                return None
            return user

    @app.before_request
    def lifecycle_boundary():
        if not current_user.is_authenticated:
            return None
        if not service.access_allowed(current_user.get_id()):
            logout_user()
            session.clear()
            if request.path=='/api/push/context':
                return jsonify(available=False,authenticated=False)
            return redirect(url_for('login',next='/account-deletion-info'))
        if request.method=='POST' and request.path in ('/billing/checkout','/billing/change-plan') and not service.outbound_allowed(current_user.get_id()):
            return jsonify(error={'code':'account_deletion_pending','message':'削除申請中のため、新しいプランの購入を停止しています。'}),409
        return None
    return service
