"""Hold legacy mutations that cannot synchronize a linked physical item.

Opt-in only; no schema writes, automatic linking, billing or file access.
Read pages remain available. Proxy settlement helpers also call the same guard
inside their transaction, including the legacy GET/scheduler reconciliation.
"""
from decimal import Decimal, InvalidOperation
import json

from flask import jsonify, make_response, render_template, request
from flask_login import current_user


MESSAGE = ('この商品は個品管理と連動しています。旧画面からの変更は記録が食い違うため保留しました。'
           '個品の受領・作業記録を使用し、販売・所有者変更・発送の連携確認を待ってください。データは変更していません。')


class LinkedInventoryMutationPending(ValueError):
    code = 'linked_inventory_mutation_pending'
    status = 409

    def __init__(self):
        super().__init__(MESSAGE)


def _query(runtime, connection, sql, params=()):
    cur = connection.cursor()
    try:
        cur.execute(sql.replace('?', '%s') if runtime.DATABASE_URL else sql, params)
        names = [c[0] for c in cur.description]
        return [dict(row) if hasattr(row, 'keys') else dict(zip(names, row)) for row in cur.fetchall()]
    finally:
        cur.close()


def _ids(values):
    result = set()
    for value in values:
        try:
            # Legacy handlers variously use int(), SQL numeric affinity, and
            # int(str(...).strip()). Include all equivalent numeric inputs.
            number = Decimal(int(value) if isinstance(value, bool) else str(value).strip())
            if number.is_finite() and 1 <= number < 10**12:
                result.add(int(number))
        except (InvalidOperation, ValueError, TypeError, OverflowError):
            continue
    return sorted(result)


def linked_ids(runtime, connection, merchandise_ids):
    if runtime.app.config.get('STAGE2_INVENTORY_ENABLED') is not True:
        return []
    values = _ids(merchandise_ids)
    result = []
    for offset in range(0, len(values), 500):
        part = values[offset:offset + 500]
        result.extend(row['merchandise_id'] for row in _query(runtime, connection,
            'SELECT merchandise_id FROM business_item_links WHERE merchandise_id IN (' + ','.join('?' for _ in part) + ')', tuple(part)))
    return result


def reject_linked_items(runtime, connection, merchandise_ids):
    """Call before legacy writes, on the caller's existing transaction."""
    if linked_ids(runtime, connection, merchandise_ids):
        raise LinkedInventoryMutationPending()


DIRECT = {
    'edit_item': 'id', 'admin_transfer_item': 'id',
    'admin_proxy_service_toggle_item': 'item_id', 'admin_proxy_service_toggle_item_quick': 'item_id',
    'admin_proxy_service_reflect_item': 'item_id', 'admin_proxy_service_update_item_pricing': 'item_id',
    'submit_sale_request': 'item_id',
}
REQUEST_TABLES = {
    'approve_sale_request': 'sale_requests', 'admin_sale_request_shipment': 'sale_requests',
    'edit_sale_request': 'sale_requests', 'cancel_sale_request': 'sale_requests',
    'process_disposal_request': 'item_disposal_requests',
}
AUCTIONS = frozenset(('admin_proxy_service_start', 'admin_proxy_service_end_now',
    'admin_proxy_service_visibility', 'admin_proxy_service_settings', 'admin_proxy_service_delete',
    'admin_proxy_service_finalize', 'admin_proxy_service_reflect_all', 'admin_proxy_service_prepare_client_document'))
BULK = frozenset(('admin_transfer_items_bulk', 'admin_proxy_service_bulk_toggle', 'sales_agency_apply',
                  'submit_disposal_request', 'submit_long_term_disposal_request'))
DOCUMENTS = frozenset(('admin_auction_keisan_submit', 'admin_auction_keisan_send_bulk', 'admin_auction_keisan_edit'))
SPECIAL = frozenset(('proxy_service_bid', 'proxy_service_purchase', 'admin_sales_agency_process', 'sales_agency_cancel_request'))
ENDPOINTS = frozenset(DIRECT) | frozenset(REQUEST_TABLES) | AUCTIONS | BULK | DOCUMENTS | SPECIAL


class LegacyGuard:
    def __init__(self, runtime):
        self.runtime = runtime

    def candidates(self, connection):
        endpoint = request.endpoint
        args = request.view_args or {}
        values = []
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            # The legacy proxy bulk endpoint also accepts raw JSON without a
            # JSON content type. Its guard must resolve the identical targets.
            raw = request.get_data(cache=True)
            if raw.lstrip().startswith(b'{'):
                try:
                    data = json.loads(raw)
                except (ValueError, TypeError, UnicodeDecodeError):
                    pass
        data = data if isinstance(data, dict) else request.form

        def q(sql, params=()):
            return _query(self.runtime, connection, sql, params)

        if endpoint in DIRECT:
            values.append(args.get(DIRECT[endpoint]))
        if endpoint in REQUEST_TABLES:
            values.extend(row['merchandise_id'] for row in q('SELECT merchandise_id FROM ' + REQUEST_TABLES[endpoint] + ' WHERE id=?',
                                                           (args.get('request_id'),)))
        if endpoint in ('admin_sales_agency_process', 'sales_agency_cancel_request'):
            request_id = args.get('id', args.get('request_id'))
            values.extend(row['merchandise_id'] for row in q('SELECT merchandise_id FROM sales_agency_request_items WHERE request_id=?', (request_id,)))
        if endpoint in BULK:
            for field in ('item_ids', 'merchandise_ids', 'merchandise_ids[]'):
                raw = data.getlist(field) if hasattr(data, 'getlist') else data.get(field, [])
                values.extend(raw if isinstance(raw, (list, tuple)) else [raw])
            if isinstance(data.get('items'), list):
                values.extend(entry.get('id') for entry in data['items'] if isinstance(entry, dict))
        if endpoint in ('proxy_service_bid', 'proxy_service_purchase'):
            values.append(data.get('merchandise_id'))
        if endpoint in AUCTIONS or endpoint == 'admin_proxy_service_bulk_toggle':
            auction = args.get('auction_id')
            values.extend(row['id'] for row in q('SELECT id FROM merchandise WHERE auction_id=?', (auction,)))
            # Include historical source IDs whose current auction_id was cleared.
            values.extend(row['merchandise_id'] for row in q('SELECT merchandise_id FROM proxy_service_auction_items WHERE auction_id=?', (auction,)))
        if endpoint in DOCUMENTS:
            documents = [args.get('id')] if endpoint != 'admin_auction_keisan_send_bulk' else request.form.getlist('document_ids')
            if endpoint == 'admin_auction_keisan_send_bulk' and request.form.get('send_scope') == 'all':
                auction_id = request.form.get('auction_id')
                sql = "SELECT id FROM user_keisan WHERE is_admin_created=? AND status='completed' AND proxy_service_auction_id IS NOT NULL"
                params = [True if self.runtime.DATABASE_URL else 1]
                if auction_id:
                    sql += ' AND proxy_service_auction_id=?'
                    params.append(auction_id)
                documents = [row['id'] for row in q(sql, tuple(params))]
            for document in _ids(documents):
                rows = q('SELECT merchandise_id,proxy_source_item_id FROM user_keisan_items WHERE keisan_id=?', (document,))
                for row in rows:
                    values.extend((row['merchandise_id'], row['proxy_source_item_id']))
        return _ids(values)

    def boundary(self):
        if (self.runtime.app.config.get('STAGE2_INVENTORY_ENABLED') is not True
                or request.method in ('GET', 'HEAD', 'OPTIONS') or request.endpoint not in ENDPOINTS):
            return None
        if not current_user.is_authenticated:
            return None  # Preserve the original login response.
        if request.endpoint == 'submit_long_term_disposal_request':
            # The former age-based disposal form does not carry the customer's
            # contract, individual deadline or current custody consent. Hold
            # even unlinked legacy stock before any request/notification write.
            return self.hold('legacy_custody_request_pending',
                '以前の保管日数だけで返送・販売・処分を申請する操作は停止しています。'
                '「保管場所・作業履歴」で現在の契約と商品を確認し、開花へご相談ください。')
        staff_only = request.endpoint.startswith('admin_') or request.endpoint in (
            'edit_item', 'approve_sale_request', 'process_disposal_request')
        if staff_only and not current_user.is_admin():
            return None  # Preserve original staff authorization before lookup.
        connection = None
        try:
            connection = self.runtime.get_db()
            reject_linked_items(self.runtime, connection, self.candidates(connection))
        except LinkedInventoryMutationPending:
            return self.hold()
        except Exception:
            # A missing migration/lookup error cannot open the old write route.
            return self.hold('inventory_link_check_unavailable', '個品との連動を確認できないため、この旧操作を保留しました。データは変更していません。')
        finally:
            if connection is not None:
                connection.close()
        return None

    @staticmethod
    def hold(code='linked_inventory_mutation_pending', message=MESSAGE):
        if (not request.is_json and request.headers.get('X-Requested-With') != 'XMLHttpRequest'
                and request.accept_mimetypes['text/html'] > request.accept_mimetypes['application/json']):
            response = make_response(render_template('inventory_registry/error.html', message=message))
        else:
            response = jsonify(error={'code': code, 'message': message})
        response.status_code = 409
        response.headers['Cache-Control'] = 'private, no-store'
        return response


def register_stage2_legacy_guard(runtime):
    app = runtime.app
    if 'stage2_legacy_guard' in app.extensions:
        return app.extensions['stage2_legacy_guard']
    guard = LegacyGuard(runtime)
    app.extensions['stage2_legacy_guard'] = guard
    # Must precede the proxy GET reconciliation and later legacy handlers.
    app.before_request_funcs.setdefault(None, []).insert(0, guard.boundary)
    return guard
