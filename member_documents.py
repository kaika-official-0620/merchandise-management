"""Staff-only view of a member's receipt and Kaika-origin documents."""

from flask import abort, render_template, request, url_for
from flask_login import current_user, login_required

from document_workflow import INBOX_STATES, inbox_staff


PAGE_SIZE = 50
# These are the existing staff workflows with a direct member relationship.
KAIKA_DOCUMENTS = (
    ('user_mitsumori', 'user_id', 'document_no', '業者向け見積依頼書', 'admin_mitsumori_view',
     "d.document_scope='vendor_outgoing'"),
    ('invoices', 'sender_id', 'invoice_no', '買取明細書', 'admin_kaitori_view',
     "(COALESCE(d.source_admin_kaitori_id,0)>0 OR d.document_scope='admin_kaitori' "
     "OR SUBSTR(UPPER(COALESCE(d.invoice_no,'')),1,3)='KT-')"),
    ('shikiriosho', 'recipient_id', 'document_no', '精算書', 'admin_shikiriosho_view', '1=1'),
    ('user_keisan', 'user_id', 'document_no', '計算書', 'admin_auction_keisan_view',
     'COALESCE(d.is_admin_created,FALSE)=TRUE OR d.proxy_service_auction_id IS NOT NULL'),
)
DOCUMENT_STATES = {
    'draft': '下書き', 'completed': '作成済み', 'sent': '送付済み', 'submitted': '提出済み',
    'approved': '承認済み', 'accepted': '受領済み', 'pending': '確認待ち', 'cancelled': '取消済み',
    'canceled': '取消済み', 'rejected': '差し戻し', 'paid': '支払済み',
}


def staff_documents(service, cur, user_id, page):
    queries, params = [], []
    for table, member_column, number_column, title, endpoint, predicate in KAIKA_DOCUMENTS:
        marker = service.mark
        queries.append(f'SELECT d.id, d.{number_column} AS number, CAST(d.issue_date AS TEXT) AS issue_date, '
                       f'd.status, CAST({marker} AS TEXT) AS kind, CAST({marker} AS TEXT) AS endpoint '
                       f'FROM {table} d WHERE d.{member_column}={marker} AND ({predicate})')
        params.extend((title, endpoint, user_id))
    cur.execute('SELECT * FROM (' + ' UNION ALL '.join(queries) + ') AS docs '
                'ORDER BY issue_date DESC, id DESC, endpoint '
                f'LIMIT {service.mark} OFFSET {service.mark}',
                (*params, PAGE_SIZE + 1, (page - 1) * PAGE_SIZE))
    documents = service.rows(cur)
    for row in documents:
        row['url'] = url_for(row['endpoint'], id=row['id'])
        row['state_label'] = DOCUMENT_STATES.get(row['status'], '状態を確認')
    return documents[:PAGE_SIZE], len(documents) > PAGE_SIZE


def eligible_staff_actions(service, cur, user_id):
    cur.execute(f'''
        SELECT DISTINCT i.workflow_status
        FROM sales_agency_request_items i JOIN sales_agency_requests r ON r.id=i.request_id
        WHERE r.user_id={service.mark} AND COALESCE(i.item_status,'active') NOT IN ('cancelled','canceled')
          AND (i.workflow_status='step2_ready' OR (i.workflow_status='step4_ready'
               AND (i.vendor_document_id IS NOT NULL OR EXISTS (
                    SELECT 1 FROM vendor_document_item_links l
                    WHERE l.request_item_id=i.id AND l.vendor_document_id IS NOT NULL))))''', (user_id,))
    ready = {row['workflow_status'] for row in service.rows(cur)}
    return 'step2_ready' in ready, 'step4_ready' in ready


def register_member_documents(runtime):
    app = runtime.app
    service = app.extensions['kaika_document_workflow']

    @app.get('/admin/users/<int:user_id>/documents', endpoint='admin_member_documents')
    @login_required
    def member_documents(user_id):
        if not inbox_staff(current_user) or not current_user.has_permission('users'):
            abort(403)
        if not 0 < user_id <= 2_147_483_647:
            abort(404)
        search = request.args.get('search', '').strip()[:100]
        incoming_page = min(100_000, max(1, request.args.get('incoming', 1, type=int)))
        outgoing_page = min(100_000, max(1, request.args.get('outgoing', 1, type=int)))
        with service.db() as (_, cur):
            cur.execute(f"SELECT id,username,display_name FROM users WHERE id={service.mark} AND role='user'", (user_id,))
            member = service.one(cur)
            if not member:
                abort(404)
            outgoing, more_outgoing = staff_documents(service, cur, user_id, outgoing_page)
            can_prepare_quote, can_prepare_statement = eligible_staff_actions(service, cur, user_id)
            staff = service.eligible_inbox_staff(cur)

        incoming, more_incoming = service.inbox(query='', category='', received_from=None, received_through=None,
                                               page=incoming_page, customer_id=user_id)
        for row in incoming:
            row['url'] = url_for('admin_customer_submission_detail', kind=row['kind'], document_id=row['id'],
                                 member_id=user_id)

        return render_template('admin/member_documents.html', member=member, search=search, incoming=incoming,
                               outgoing=outgoing, incoming_page=incoming_page, outgoing_page=outgoing_page,
                               more_incoming=more_incoming, more_outgoing=more_outgoing, inbox_states=INBOX_STATES,
                               staff=staff, can_prepare_quote=can_prepare_quote, can_prepare_statement=can_prepare_statement)

    app.jinja_env.globals['member_documents_available'] = True
