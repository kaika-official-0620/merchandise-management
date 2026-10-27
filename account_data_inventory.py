"""Counts-only discovery for a staff-authorized account-erasure review.

No connection, migration, file access or provider call at import or discovery.
This module is NOT an eraser or an authorization boundary: callers must authorize
the staff actor before calling it. Unknown schemas and embedded payloads remain
explicit blockers, and an association never proves exclusive ownership.
"""
import re

from account_lifecycle import columns, tables


GROUPS = {
    'profile': ('プロフィール・連絡先', 'review_and_minimize'),
    'authentication': ('ログイン・端末認証', 'revoke_then_review'),
    'notifications': ('通知先・通知本文・連携履歴', 'suppress_then_review'),
    'inventory': ('個品・所有履歴・預かり作業', 'retain_pending_policy'),
    'financial': ('請求・決済・残高・精算', 'retain_pending_policy'),
    'contracts': ('契約・同意', 'retain_pending_policy'),
    'documents': ('帳票・明細・書類内の情報', 'retain_pending_policy'),
    'support': ('問い合わせ・個人メモ', 'review_and_minimize'),
    'imports': ('取込元・プレビュー・過去スナップショット', 'review_and_minimize'),
    'audit': ('操作履歴・削除証跡', 'retain_pending_policy'),
    'configuration': ('共通設定・料金定義', 'shared_not_customer_erasure'),
    'unreviewed': ('関連が未確認のテーブル', 'manual_scope_review'),
}

# Only explicit, code-reviewed columns refer to the application's users.id.
# In particular feature_* customer_id is a PSP string, NOT a local user ID.
DIRECT = {}
CATEGORY = {}


def _register(category, names, keys=('customer_id',)):
    for name in names.split():
        CATEGORY[name] = category
        DIRECT[name] = keys


_register('profile', 'users', ('id',))
_register('profile', 'customers', ('user_id',))
_register('authentication', 'mobile_sessions push_devices', ('user_id',))
_register('notifications', 'push_outbox line_message_logs', ('user_id',))
_register('notifications', '''stage6_notifications stage6_notification_preferences
    stage6_destinations stage6_destination_proofs stage6_notification_outbox''')
_register('notifications', 'stage6_delivery_audit stage6_line_invitations stage6_line_link_tokens stage6_line_webhooks', ())
_register('inventory', '''merchandise inventory_intakes inventory_custody_events
    item_disposal_requests sale_requests sales_agency_requests proxy_service_bids''', ('user_id',))
_register('inventory', 'business_parties', ('user_id',))
_register('inventory', 'business_item_profiles', ('initial_customer_id', 'deadline_customer_id'))
_register('inventory', '''business_item_events business_item_observations business_work_context
    business_item_reviews stage4_purchase_orders stage4_listing_versions stage4_channel_listings
    stage4_channel_orders stage6_custody_requests''')
_register('inventory', '''business_items business_item_assignments business_item_links business_work_records
    inventory_intake_items sales_agency_request_items sale_request_events stage4_purchase_claims
    stage4_listing_decisions stage4_order_claims stage4_order_corrections stage4_stop_tasks
    stage4_stop_events stage4_order_tasks stage4_order_task_events stage4_order_shipments
    stage4_order_returns stage6_custody_responses''', ())
_register('financial', '''business_charge_drafts business_fund_records stage3_charge_drafts
    stage3_invoice_revisions stage3_fund_records stage3_item_financial_facts stage3_sale_status_records
    stage5_physical_bills stage5_payment_attempts stage5_payment_refunds stage9_payment_customers
    stage9_payment_jobs''')
_register('financial', '''feature_billing_accounts feature_subscriptions feature_manual_grants
    feature_store_accounts feature_store_replacements feature_billing_reviews''', ('user_id',))
_register('financial', '''feature_plan_changes feature_store_receipts feature_stripe_receipts
    feature_store_refunds stage5_payment_events stage5_refund_events stage9_payment_events
    stage9_payment_webhooks''', ())
_register('contracts', 'business_customer_agreements stage3_agreements stage5_card_consents stage9_payment_consents')
_register('contracts', 'stage5_card_consent_revocations', ())
_register('documents', 'invoices', ('sender_id',))
_register('documents', 'shikiriosho', ('sender_id', 'recipient_id'))
_register('documents', 'service_documents user_mitsumori user_keisan user_kaitori_shoudaku', ('user_id',))
_register('documents', 'admin_kaitori_shoudaku', ('admin_id',))
_register('documents', '''invoice_items shikiriosho_items user_mitsumori_items user_keisan_items
    user_kaitori_shoudaku_items admin_kaitori_shoudaku_items''', ())
_register('support', 'inquiries inquiry_replies analytics_memos', ('user_id',))
_register('imports', 'business_legacy_snapshots business_import_batches business_import_sources stage4_legacy_projections')
_register('imports', 'business_import_rows', ())
_register('audit', 'business_registry_operations feature_plan_audit', ('actor_user_id', 'actor_id'))
_register('audit', '''stage3_operations account_deletion_requests account_deletion_events
    account_deletion_operations account_deletion_reauth''')
_register('audit', 'stage4_operations stage5_operations stage6_operations stage7_operations', ('customer_id', 'actor_user_id'))
_register('inventory', 'stage4_media', ())
_register('support', 'stage7_participants')
_register('support', 'stage7_observations', ())
_register('configuration', '''business_contract_versions business_fee_versions business_sales_accounts
    business_funding_reviews stage4_channel_accounts stage4_channel_reviews
    stage4_company_items stage4_catalog_revisions stage4_catalog_publications
    feature_plan_catalog feature_plan_prices feature_store_products feature_billing_events
    stage9_payment_scope widget_settings line_settings line_scheduled_messages
    proxy_service_settings master_brand_categories master_brands master_suppliers
    master_conditions master_payment_methods master_supplier_details master_document_settings
    master_fee_settings''', ())

# Child -> explicitly reviewed parent relation. Never traverse every FK, nor
# fan out through a physical item into a later owner's orders/financial records.
EDGES = {
    'invoice_items': [('invoices', (('invoice_id', 'id'),))],
    'shikiriosho_items': [('shikiriosho', (('shikiriosho_id', 'id'),))],
    'user_mitsumori_items': [('user_mitsumori', (('mitsumori_id', 'id'),))],
    'user_keisan_items': [('user_keisan', (('keisan_id', 'id'),))],
    'user_kaitori_shoudaku_items': [('user_kaitori_shoudaku', (('kaitori_shoudaku_id', 'id'),))],
    'admin_kaitori_shoudaku_items': [('admin_kaitori_shoudaku', (('kaitori_shoudaku_id', 'id'),))],
    'inquiry_replies': [('inquiries', (('inquiry_id', 'id'),))],
    'inventory_intake_items': [('inventory_intakes', (('intake_id', 'id'),))],
    'sales_agency_request_items': [('sales_agency_requests', (('request_id', 'id'),))],
    'sale_request_events': [('sale_requests', (('sale_request_id', 'id'),))],
    'business_item_assignments': [('business_parties', (('party_id', 'party_id'),))],
    'business_items': [('business_item_assignments', (('physical_item_id', 'physical_item_id'),))],
    'business_item_links': [('merchandise', (('merchandise_id', 'id'),))],
    'business_work_records': [('business_work_context', (('work_id', 'work_id'),))],
    'stage4_media': [('business_item_assignments', (('ownership_assignment_id', 'assignment_id'),))],
    'business_import_rows': [('business_import_batches', (('batch_id', 'batch_id'),))],
    'stage4_purchase_claims': [('stage4_purchase_orders', (('order_id', 'order_id'),))],
    'stage4_listing_decisions': [('stage4_listing_versions', (('version_id', 'version_id'),))],
    'stage4_order_claims': [('stage4_channel_orders', (('order_id', 'order_id'),))],
    'stage4_order_corrections': [('stage4_channel_orders', (('order_id', 'order_id'),))],
    'stage4_order_shipments': [('stage4_channel_orders', (('order_id', 'order_id'),))],
    'stage4_order_returns': [('stage4_channel_orders', (('order_id', 'order_id'),))],
    'stage4_order_tasks': [('stage4_channel_orders', (('order_id', 'order_id'),))],
    'stage4_order_task_events': [('stage4_order_tasks', (('task_id', 'task_id'),))],
    'stage4_stop_tasks': [('stage4_channel_listings', (('listing_id', 'listing_id'),))],
    'stage4_stop_events': [('stage4_stop_tasks', (('task_id', 'task_id'),))],
    'stage5_card_consent_revocations': [('stage5_card_consents', (('consent_id', 'consent_id'),))],
    'stage5_payment_events': [('stage5_payment_attempts', (('attempt_id', 'attempt_id'),))],
    'stage5_refund_events': [('stage5_payment_refunds', (('refund_id', 'refund_id'),))],
    'stage6_custody_responses': [('stage6_custody_requests', (('request_id', 'request_id'),))],
    'stage6_delivery_audit': [('stage6_notification_outbox', (('delivery_id', 'delivery_id'),))],
    'stage6_line_link_tokens': [('stage6_line_invitations', (('invitation_id', 'invitation_id'),))],
    'stage6_line_webhooks': [('stage6_line_invitations', (('webhook_event_id', 'webhook_event_id'),))],
    'stage7_observations': [('stage7_participants', (('participant_id', 'participant_id'),))],
    'stage9_payment_events': [('stage9_payment_jobs', (('job_id', 'job_id'),))],
    'stage9_payment_webhooks': [('stage9_payment_jobs', (('job_id', 'job_id'),))],
    'feature_plan_changes': [('feature_subscriptions', (('subscription_id', 'subscription_id'),))],
    'feature_store_receipts': [('feature_subscriptions', (('provider', 'provider'), ('subscription_id', 'subscription_id')))],
    'feature_stripe_receipts': [('feature_billing_accounts', (('customer_id', 'customer_id'),))],
    'feature_store_refunds': [('feature_subscriptions', (('provider', 'provider'), ('subscription_id', 'subscription_id')))],
}

PHOTO_COLUMNS = {'photo_path', 'additional_photos', 'image_path', 'qr_image_path', 'qr_image_path2',
                 'additional_image_paths', 'snapshot_photo_path', 'photo_refs_json', 'storage_path'}
SCHEMA_TABLES = {'business_registry_schema', 'account_lifecycle_schema', 'stage3_schema',
                 'stage4_schema', 'stage5_schema', 'stage6_schema', 'stage7_schema',
                 'stage4_locks', 'stage3_customer_locks'}


def inventory_data(store, customer_id):
    """Return aggregate associations only; never records, paths, IDs or tokens.

    A `None` count means unknown, not zero. The result is intentionally incomplete
    until JSON, shared assets, provider-side data and backups are reviewed.
    """
    if type(customer_id) is not int or customer_id <= 0:
        raise ValueError('positive integer customer_id required')
    names = tables(store)
    safe = {name for name in names if re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', name) and not name.startswith('sqlite_')}
    schema = {name: columns(store, name) for name in safe}
    blockers = {'retention_policy_required', 'provider_data_review_required', 'backup_review_required'}
    if names - safe - {name for name in names if name.startswith('sqlite_')}:
        blockers.add('unsupported_table_identifier')

    def direct(name, alias):
        keys = [key for key in DIRECT.get(name, ()) if key in schema[name]]
        return (' OR '.join(alias+'."'+key+'"=?' for key in keys), [customer_id] * len(keys))

    def related(name, alias, seen=()):
        if name not in schema or name in seen:
            return '', []
        expr, params = direct(name, alias)
        pieces = ['('+expr+')'] if expr else []
        if name == 'stage6_line_invitations' and 'line_user_id' in schema[name] and \
                {'address', 'customer_id', 'channel'} <= schema.get('stage6_destinations', set()):
            pieces.append("EXISTS (SELECT 1 FROM stage6_destinations d WHERE d.channel='line' AND d.address="+alias+'."line_user_id" AND d.customer_id=?)')
            params.append(customer_id)
        for index, (parent, pairs) in enumerate(EDGES.get(name, ())):
            if parent not in schema or any(child not in schema[name] or target not in schema[parent] for child, target in pairs):
                continue
            parent_alias = alias+'p'+str(index)
            target, values = related(parent, parent_alias, seen+(name,))
            if not target:
                continue
            join = ' AND '.join(alias+'."'+child+'"='+parent_alias+'."'+key+'"' for child, key in pairs)
            pieces.append('EXISTS (SELECT 1 FROM "'+parent+'" '+parent_alias+' WHERE '+join+' AND ('+target+'))')
            params.extend(values)
        return ' OR '.join(pieces), params

    output = []
    groups = {key: {'key': key, 'label': label, 'record_count': 0, 'action': action,
                   'needs_policy': key != 'configuration', 'notes': [], 'unknown_tables': 0}
              for key, (label, action) in GROUPS.items()}
    photo_rows = 0
    embedded_rows = 0
    for name in sorted(safe):
        category = CATEGORY.get(name, 'unreviewed')
        if name in SCHEMA_TABLES:
            category = 'configuration'
        if name == 'stage2_photo_records':
            category = 'inventory'
            # A historical owner in this index does not imply current exclusive access.
            direct_sql = 'r."owner_user_id"=?' if 'owner_user_id' in schema[name] else ''
            direct_params = [customer_id] if direct_sql else []
            related_sql, related_params = direct_sql, direct_params
        else:
            direct_sql, direct_params = direct(name, 'r')
            related_sql, related_params = related(name, 'r')
        known = bool(related_sql)
        direct_count = store.query('SELECT COUNT(*) AS n FROM "'+name+'" r WHERE '+direct_sql, tuple(direct_params))[0]['n'] if direct_sql else None
        total = store.query('SELECT COUNT(*) AS n FROM "'+name+'" r WHERE '+related_sql, tuple(related_params))[0]['n'] if known else None
        indirect_count = total - (direct_count or 0) if total is not None else None
        notes = []
        json_columns = sorted(col for col in schema[name] if col.endswith('_json') or col in ('service_data', 'shipping_addresses', 'additional_photos', 'additional_image_paths'))
        if json_columns:
            notes.append('埋込JSON・スナップショットの内容と第三者情報は未精査。値の抽出・削除は行っていません。')
            if total:
                embedded_rows += total
                blockers.add('embedded_payload_review_required')
        photo_columns = sorted(schema[name] & PHOTO_COLUMNS)
        if photo_columns:
            notes.append('写真参照がある行。実ファイル数・共有参照・保存先・削除可否は未確認。')
            if total:
                photo_rows += total
                blockers.add('shared_media_review_required')
        if name in ('business_items', 'business_item_assignments', 'business_item_profiles', 'stage2_photo_records'):
            notes.append('過去の所有・保管関係を含みます。現在の他顧客の記録を、この顧客の削除対象へ広げません。')
        if name.startswith('stage6_line_'):
            notes.append('連携前の招待は顧客と照合できない場合があります。照合0件でも全データ確認済みではありません。')
            blockers.add('unlinked_line_identity_review_required')
        if category == 'configuration':
            notes.append('共通定義は顧客単位で削除しません。顧客固有データが含まれない保証ではありません。')
        elif not known:
            blockers.add('unresolved_table_relationships')
            notes.append('顧客との照合経路が未確定です。件数不明を0件として扱いません。')
            groups[category]['unknown_tables'] += 1
        if category == 'unreviewed':
            blockers.add('unreviewed_tables')
        if total is not None:
            groups[category]['record_count'] += total
        output.append({'table': name, 'direct_records': direct_count, 'indirect_records': indirect_count,
                       'related_records': total, 'classification': category, 'action': GROUPS[category][1],
                       'embedded_columns': json_columns, 'photo_columns': photo_columns, 'notes': notes})
    for name in sorted(names-safe):
        if name.startswith('sqlite_'):
            continue
        groups['unreviewed']['unknown_tables'] += 1
        blockers.add('unreviewed_tables')
        output.append({'table': name, 'direct_records': None, 'indirect_records': None,
                       'related_records': None, 'classification': 'unreviewed', 'action': 'manual_scope_review',
                       'embedded_columns': [], 'photo_columns': [],
                       'notes': ['未対応の識別子です。SQL照会を行わず要確認として残します。']})
    for group in groups.values():
        group['known_record_count'] = group['record_count']
        group['notes'].append('件数は照合できた関連行の合計で、削除件数ではありません。未知の行・外部コピーは含みません。')
        if group['unknown_tables']:
            group['record_count'] = None
            group['notes'].append('関連件数が不明なテーブルを含みます。')
    extras = [
        {'key': 'photos', 'label': '写真・添付ファイルの保存先', 'record_count': photo_rows,
         'action': 'manual_shared_reference_review', 'needs_policy': True,
         'notes': ['上記テーブルのうち写真参照列がある関連行数（重複あり）。実ファイル数ではありません。']},
        {'key': 'embedded', 'label': 'JSON・帳票内の複製情報', 'record_count': embedded_rows,
         'action': 'manual_payload_review', 'needs_policy': True,
         'notes': ['上記関連行の再掲です。JSONにだけある顧客ID・住所・写真や共有情報は未照合です。']},
        {'key': 'providers', 'label': '外部サービスの保存データ', 'record_count': None,
         'action': 'provider_evidence_required', 'needs_policy': True,
         'notes': ['Stripe・Apple・Google・LINE・メール・プッシュ・画像保存先への照会や削除はしていません。']},
        {'key': 'backups', 'label': 'バックアップ・復旧後の再反映', 'record_count': None,
         'action': 'backup_evidence_required', 'needs_policy': True,
         'notes': ['バックアップの世代・期間・別保存先は未確認。復旧時の削除状態再反映が必要です。']},
    ]
    return {'groups': list(groups.values())+extras, 'tables': output, 'complete': False,
            'blockers': sorted(blockers), 'read_only': True, 'counts_only': True,
            'scope': 'explicit customer associations; no generic FK traversal or embedded-data scan'}
