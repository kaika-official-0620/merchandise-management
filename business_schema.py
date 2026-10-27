"""Stage 1 additive schema prototype, deliberately limited to in-memory tests.

No connection is opened on import. Do not import this into the runtime startup.
PostgreSQL migration, authorization-aware repositories and historical mapping
require a reviewed migration in later stages; these tables are not live billing.
"""
import sqlite3


SCHEMA_VERSION = 'business-stage1-2026-10-22-v2'

# Tables reference existing users and merchandise IDs. The caller creates those
# two fixture tables; this module never creates, changes or backfills them.
_TABLES = (
    ('business_parties', '''
        party_id TEXT PRIMARY KEY NOT NULL,
        kind TEXT NOT NULL CHECK(kind IN ('customer','kaika','staff','external')),
        user_id INTEGER REFERENCES users(id) ON DELETE RESTRICT,
        reference TEXT NOT NULL,
        UNIQUE(kind, user_id)
    '''),
    ('business_items', '''
        physical_item_id TEXT PRIMARY KEY NOT NULL,
        label_code TEXT UNIQUE,
        identity_status TEXT NOT NULL DEFAULT 'unresolved' CHECK(identity_status IN ('unresolved','verified')),
        identity_evidence TEXT,
        CHECK(identity_status != 'verified' OR identity_evidence IS NOT NULL)
    '''),
    ('business_item_links', '''
        merchandise_id INTEGER PRIMARY KEY REFERENCES merchandise(id) ON DELETE RESTRICT,
        physical_item_id TEXT NOT NULL REFERENCES business_items(physical_item_id) ON DELETE RESTRICT,
        link_status TEXT NOT NULL DEFAULT 'unresolved' CHECK(link_status IN ('unresolved','verified')),
        review_reference TEXT,
        CHECK(link_status != 'verified' OR review_reference IS NOT NULL)
    '''),
    ('business_sales_accounts', '''
        sales_account_id TEXT PRIMARY KEY NOT NULL,
        operator_party_id TEXT NOT NULL REFERENCES business_parties(party_id) ON DELETE RESTRICT,
        channel TEXT NOT NULL,
        external_reference TEXT NOT NULL,
        authorization_status TEXT NOT NULL DEFAULT 'unverified' CHECK(authorization_status IN ('unverified','verified','expired')),
        authorization_reference TEXT,
        UNIQUE(channel, external_reference),
        CHECK(authorization_status != 'verified' OR authorization_reference IS NOT NULL)
    '''),
    ('business_item_assignments', '''
        assignment_id TEXT PRIMARY KEY NOT NULL,
        physical_item_id TEXT NOT NULL REFERENCES business_items(physical_item_id) ON DELETE RESTRICT,
        role TEXT NOT NULL CHECK(role IN ('owner','custodian','sales_operator','settlement_party')),
        party_id TEXT NOT NULL REFERENCES business_parties(party_id) ON DELETE RESTRICT,
        started_at TEXT NOT NULL,
        ended_at TEXT,
        evidence_reference TEXT NOT NULL,
        source_event_id TEXT NOT NULL UNIQUE,
        CHECK(datetime(started_at) IS NOT NULL AND (substr(started_at,-1)='Z' OR substr(started_at,-6,1) IN ('+','-'))),
        CHECK(ended_at IS NULL OR (datetime(ended_at) IS NOT NULL AND (substr(ended_at,-1)='Z' OR substr(ended_at,-6,1) IN ('+','-')) AND julianday(ended_at)>=julianday(started_at)))
    '''),
    ('business_contract_versions', '''
        contract_version_id TEXT PRIMARY KEY NOT NULL,
        document_reference TEXT NOT NULL,
        content_sha256 TEXT NOT NULL CHECK(length(content_sha256)=64),
        status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','reviewed','retired')),
        terms_snapshot_json TEXT NOT NULL
    '''),
    ('business_fee_versions', '''
        fee_version_id TEXT PRIMARY KEY NOT NULL,
        source_reference TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','reviewed','retired')),
        currency TEXT NOT NULL DEFAULT 'JPY' CHECK(currency='JPY'),
        monthly_net_yen INTEGER CHECK(monthly_net_yen IS NULL OR (typeof(monthly_net_yen)='integer' AND monthly_net_yen>=0)),
        self_active_item_limit INTEGER CHECK(self_active_item_limit IS NULL OR (typeof(self_active_item_limit)='integer' AND self_active_item_limit>=0)),
        self_monthly_registration_limit INTEGER CHECK(self_monthly_registration_limit IS NULL OR (typeof(self_monthly_registration_limit)='integer' AND self_monthly_registration_limit>=0)),
        self_monthly_limit_kind TEXT NOT NULL DEFAULT 'undecided' CHECK(self_monthly_limit_kind IN ('undecided','no_limit_proposed','limited_proposed')),
        kaika_monthly_registration_limit INTEGER CHECK(kaika_monthly_registration_limit IS NULL OR (typeof(kaika_monthly_registration_limit)='integer' AND kaika_monthly_registration_limit>=0)),
        sales_tax_treatment TEXT CHECK(sales_tax_treatment IN ('inclusive','exclusive','exempt','outside')),
        rounding_method TEXT CHECK(rounding_method IN ('floor','ceil','half_up')),
        rounding_scope TEXT CHECK(rounding_scope IN ('per_line','invoice_per_rate')),
        billing_timezone TEXT,
        policy_snapshot_json TEXT NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled=0)
    '''),
    ('business_customer_agreements', '''
        agreement_id TEXT PRIMARY KEY NOT NULL,
        customer_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
        contract_version_id TEXT NOT NULL REFERENCES business_contract_versions(contract_version_id) ON DELETE RESTRICT,
        fee_version_id TEXT REFERENCES business_fee_versions(fee_version_id) ON DELETE RESTRICT,
        signed_document_reference TEXT,
        consent_reference TEXT,
        consent_at TEXT,
        charge_start_at TEXT,
        exemption_status TEXT NOT NULL DEFAULT 'undecided' CHECK(exemption_status IN ('undecided','none','exempt','adjusted')),
        exception_reference TEXT,
        custody_deadline_rule_json TEXT,
        approval_status TEXT NOT NULL DEFAULT 'draft' CHECK(approval_status IN ('draft','reviewed','retired')),
        CHECK(consent_at IS NULL OR (datetime(consent_at) IS NOT NULL AND (substr(consent_at,-1)='Z' OR substr(consent_at,-6,1) IN ('+','-')))),
        CHECK(charge_start_at IS NULL OR (datetime(charge_start_at) IS NOT NULL AND (substr(charge_start_at,-1)='Z' OR substr(charge_start_at,-6,1) IN ('+','-'))))
    '''),
    ('business_work_records', '''
        work_id TEXT PRIMARY KEY NOT NULL,
        physical_item_id TEXT NOT NULL REFERENCES business_items(physical_item_id) ON DELETE RESTRICT,
        actor_party_id TEXT NOT NULL REFERENCES business_parties(party_id) ON DELETE RESTRICT,
        kind TEXT NOT NULL CHECK(kind IN ('receipt','inspection','authenticity_check','photography','registration','repricing','packing','dispatch','support','return','other')),
        performed_at TEXT NOT NULL,
        duration_seconds INTEGER CHECK(duration_seconds IS NULL OR (typeof(duration_seconds)='integer' AND duration_seconds>=0)),
        measurement_source TEXT NOT NULL DEFAULT 'unknown' CHECK(measurement_source IN ('unknown','observed','timer')),
        rework_of TEXT REFERENCES business_work_records(work_id) ON DELETE RESTRICT,
        rework_reason TEXT,
        source_reference TEXT NOT NULL,
        source_event_key TEXT NOT NULL UNIQUE,
        CHECK(datetime(performed_at) IS NOT NULL AND (substr(performed_at,-1)='Z' OR substr(performed_at,-6,1) IN ('+','-'))),
        CHECK((duration_seconds IS NULL AND measurement_source='unknown') OR (duration_seconds IS NOT NULL AND measurement_source IN ('observed','timer'))),
        CHECK(rework_of IS NULL OR rework_reason IS NOT NULL)
    '''),
    ('business_item_events', '''
        event_id TEXT PRIMARY KEY NOT NULL,
        physical_item_id TEXT NOT NULL REFERENCES business_items(physical_item_id) ON DELETE RESTRICT,
        kind TEXT NOT NULL CHECK(kind IN ('expected','received','inspection_hold','registration_completed','sold','shipped','returned','return_shipped','hold','correction')),
        occurred_at TEXT NOT NULL,
        actor_party_id TEXT NOT NULL REFERENCES business_parties(party_id) ON DELETE RESTRICT,
        source_reference TEXT NOT NULL,
        source_event_key TEXT NOT NULL UNIQUE,
        corrects_event_id TEXT REFERENCES business_item_events(event_id) ON DELETE RESTRICT,
        customer_id INTEGER REFERENCES users(id) ON DELETE RESTRICT,
        CHECK(datetime(occurred_at) IS NOT NULL AND (substr(occurred_at,-1)='Z' OR substr(occurred_at,-6,1) IN ('+','-'))),
        CHECK(kind!='correction' OR corrects_event_id IS NOT NULL)
    '''),
    ('business_listing_versions', '''
        listing_version_id TEXT PRIMARY KEY NOT NULL,
        physical_item_id TEXT NOT NULL REFERENCES business_items(physical_item_id) ON DELETE RESTRICT,
        sales_account_id TEXT NOT NULL REFERENCES business_sales_accounts(sales_account_id) ON DELETE RESTRICT,
        version_number INTEGER NOT NULL CHECK(typeof(version_number)='integer' AND version_number>0),
        content_snapshot_json TEXT NOT NULL,
        minimum_sale_gross_yen INTEGER CHECK(minimum_sale_gross_yen IS NULL OR (typeof(minimum_sale_gross_yen)='integer' AND minimum_sale_gross_yen>=0)),
        change_scope_json TEXT,
        approval_valid_until TEXT,
        external_listing_id TEXT,
        UNIQUE(physical_item_id,sales_account_id,version_number),
        CHECK(approval_valid_until IS NULL OR (datetime(approval_valid_until) IS NOT NULL AND (substr(approval_valid_until,-1)='Z' OR substr(approval_valid_until,-6,1) IN ('+','-'))))
    '''),
    ('business_listing_approvals', '''
        listing_version_id TEXT NOT NULL REFERENCES business_listing_versions(listing_version_id) ON DELETE RESTRICT,
        role TEXT NOT NULL CHECK(role IN ('customer','kaika')),
        actor_party_id TEXT NOT NULL REFERENCES business_parties(party_id) ON DELETE RESTRICT,
        decision TEXT NOT NULL CHECK(decision IN ('approved','rejected')),
        decided_at TEXT NOT NULL,
        evidence_reference TEXT NOT NULL,
        PRIMARY KEY(listing_version_id,role),
        CHECK(datetime(decided_at) IS NOT NULL AND (substr(decided_at,-1)='Z' OR substr(decided_at,-6,1) IN ('+','-')))
    '''),
    ('business_orders', '''
        order_id TEXT PRIMARY KEY NOT NULL,
        physical_item_id TEXT NOT NULL REFERENCES business_items(physical_item_id) ON DELETE RESTRICT,
        seller_party_id TEXT NOT NULL REFERENCES business_parties(party_id) ON DELETE RESTRICT,
        buyer_party_id TEXT NOT NULL REFERENCES business_parties(party_id) ON DELETE RESTRICT,
        purchase_price_gross_yen INTEGER CHECK(purchase_price_gross_yen IS NULL OR (typeof(purchase_price_gross_yen)='integer' AND purchase_price_gross_yen>=0)),
        requested_custodian_party_id TEXT REFERENCES business_parties(party_id) ON DELETE RESTRICT,
        status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','reserved','confirmed','completed','cancelled','expired')),
        reservation_expires_at TEXT,
        terms_consent_reference TEXT,
        source_event_key TEXT NOT NULL UNIQUE,
        execution_mode TEXT NOT NULL DEFAULT 'test' CHECK(execution_mode='test'),
        CHECK(seller_party_id!=buyer_party_id),
        CHECK(reservation_expires_at IS NULL OR (datetime(reservation_expires_at) IS NOT NULL AND (substr(reservation_expires_at,-1)='Z' OR substr(reservation_expires_at,-6,1) IN ('+','-'))))
    '''),
    ('business_charge_drafts', '''
        draft_id TEXT PRIMARY KEY NOT NULL,
        customer_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
        physical_item_id TEXT REFERENCES business_items(physical_item_id) ON DELETE RESTRICT,
        agreement_id TEXT REFERENCES business_customer_agreements(agreement_id) ON DELETE RESTRICT,
        fee_version_id TEXT REFERENCES business_fee_versions(fee_version_id) ON DELETE RESTRICT,
        source_event_id TEXT REFERENCES business_item_events(event_id) ON DELETE RESTRICT,
        fee_kind TEXT NOT NULL CHECK(fee_kind IN ('work_bundle','sales_fee','expense','digital_monthly','physical_monthly','adjustment')),
        amount_yen INTEGER CHECK(amount_yen IS NULL OR typeof(amount_yen)='integer'),
        currency TEXT NOT NULL DEFAULT 'JPY' CHECK(currency='JPY'),
        review_reasons_json TEXT NOT NULL,
        calculation_snapshot_json TEXT,
        original_draft_id TEXT REFERENCES business_charge_drafts(draft_id) ON DELETE RESTRICT,
        status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','needs_review')),
        UNIQUE(source_event_id,fee_kind),
        CHECK(amount_yen IS NULL OR amount_yen>=0 OR (fee_kind='adjustment' AND original_draft_id IS NOT NULL))
    '''),
    ('business_fund_records', '''
        record_id TEXT PRIMARY KEY NOT NULL,
        customer_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
        bucket TEXT NOT NULL CHECK(bucket IN ('purchase_funds','purchase_reservations','sales_unsettled','fees_payable')),
        kind TEXT NOT NULL CHECK(kind IN ('deposit','purchase','reserve','release','refund','sales_settlement','invoice','payment','correction')),
        amount_yen INTEGER CHECK(amount_yen IS NULL OR (typeof(amount_yen)='integer' AND amount_yen>=0)),
        currency TEXT NOT NULL DEFAULT 'JPY' CHECK(currency='JPY'),
        occurred_at TEXT NOT NULL,
        original_record_id TEXT REFERENCES business_fund_records(record_id) ON DELETE RESTRICT,
        source_reference TEXT NOT NULL,
        source_event_key TEXT NOT NULL UNIQUE,
        execution_mode TEXT NOT NULL DEFAULT 'test' CHECK(execution_mode IN ('test','record_only')),
        CHECK(datetime(occurred_at) IS NOT NULL AND (substr(occurred_at,-1)='Z' OR substr(occurred_at,-6,1) IN ('+','-'))),
        CHECK(kind NOT IN ('correction','refund','release') OR original_record_id IS NOT NULL)
    '''),
    ('business_funding_reviews', '''
        review_id TEXT PRIMARY KEY NOT NULL,
        capability TEXT NOT NULL CHECK(capability IN ('bank_purchase_deposit','card_purchase_deposit','unused_purchase_refund','sales_settlement')),
        regulatory_reference TEXT,
        psp_reference TEXT,
        contract_reference TEXT,
        operator_approval_reference TEXT,
        approved_at TEXT,
        expires_at TEXT,
        enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled=0),
        CHECK(approved_at IS NULL OR (datetime(approved_at) IS NOT NULL AND (substr(approved_at,-1)='Z' OR substr(approved_at,-6,1) IN ('+','-')))),
        CHECK(expires_at IS NULL OR (datetime(expires_at) IS NOT NULL AND (substr(expires_at,-1)='Z' OR substr(expires_at,-6,1) IN ('+','-'))))
    '''),
)


def create_test_schema(connection):
    """Explicit in-memory SQLite fixtures only; refuse file-backed databases.

    Leaves legacy tables/rows unchanged. Savepoint makes partial DDL rollback
    possible; no commit or environment discovery is performed by this helper.
    """
    if not isinstance(connection, sqlite3.Connection):
        raise TypeError('SQLiteのインメモリ検証接続だけを受け付けます。')
    if any(row[2] for row in connection.execute('PRAGMA database_list')):
        raise ValueError('実ファイルのDBには適用できません。インメモリ検証専用です。')
    if connection.execute('PRAGMA foreign_keys').fetchone()[0] != 1:
        raise ValueError('検証接続のforeign_keysを有効にしてください。')
    names = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {'users', 'merchandise'}.issubset(names):
        raise ValueError('既存IDを表すusers/merchandiseの架空テーブルが必要です。')
    connection.execute('SAVEPOINT business_stage1_schema')
    try:
        for name, columns in _TABLES:
            connection.execute('CREATE TABLE IF NOT EXISTS ' + name + ' (' + columns + ')')
        connection.execute('''CREATE UNIQUE INDEX IF NOT EXISTS business_current_item_role
            ON business_item_assignments(physical_item_id,role) WHERE ended_at IS NULL''')
        connection.execute('''CREATE UNIQUE INDEX IF NOT EXISTS business_one_item_reservation
            ON business_orders(physical_item_id) WHERE status IN ('reserved','confirmed')''')
        for table in ('business_work_records', 'business_item_events', 'business_fund_records'):
            for operation in ('UPDATE', 'DELETE'):
                connection.execute('CREATE TRIGGER IF NOT EXISTS ' + table + '_no_' + operation.lower() +
                    ' BEFORE ' + operation + ' ON ' + table +
                    " BEGIN SELECT RAISE(ABORT, 'append-only: add a correction record'); END")
        connection.execute('''CREATE TRIGGER IF NOT EXISTS business_owner_history_immutable
            BEFORE UPDATE ON business_item_assignments WHEN
                NEW.assignment_id IS NOT OLD.assignment_id OR NEW.physical_item_id IS NOT OLD.physical_item_id
                OR NEW.role IS NOT OLD.role OR NEW.party_id IS NOT OLD.party_id
                OR NEW.started_at IS NOT OLD.started_at OR NEW.evidence_reference IS NOT OLD.evidence_reference
                OR NEW.source_event_id IS NOT OLD.source_event_id
                OR (OLD.ended_at IS NOT NULL AND NEW.ended_at IS NOT OLD.ended_at)
            BEGIN SELECT RAISE(ABORT, 'owner history: close current assignment and add a new record'); END''')
        connection.execute('''CREATE TRIGGER IF NOT EXISTS business_owner_history_no_delete
            BEFORE DELETE ON business_item_assignments
            BEGIN SELECT RAISE(ABORT, 'owner history cannot be deleted'); END''')
        connection.execute('''CREATE TRIGGER IF NOT EXISTS business_fund_same_customer_origin
            BEFORE INSERT ON business_fund_records WHEN NEW.original_record_id IS NOT NULL
                AND EXISTS(SELECT 1 FROM business_fund_records WHERE record_id=NEW.original_record_id
                           AND customer_id!=NEW.customer_id)
            BEGIN SELECT RAISE(ABORT, 'fund original belongs to another customer'); END''')
        # NULL evidence may remain a standalone draft. Once an agreement/event
        # is attached, its identity must match; unknown is never a wildcard.
        for operation in ('INSERT', 'UPDATE'):
            connection.execute('CREATE TRIGGER IF NOT EXISTS business_charge_consistency_' + operation.lower() +
                ' BEFORE ' + operation + ''' ON business_charge_drafts BEGIN
                SELECT CASE WHEN NEW.agreement_id IS NOT NULL AND EXISTS(
                    SELECT 1 FROM business_customer_agreements AS agreement
                    WHERE agreement.agreement_id=NEW.agreement_id AND
                        (agreement.customer_id IS NOT NEW.customer_id OR agreement.fee_version_id IS NOT NEW.fee_version_id)
                ) THEN RAISE(ABORT, 'charge agreement customer/fee version mismatch') END;
                SELECT CASE WHEN NEW.source_event_id IS NOT NULL AND EXISTS(
                    SELECT 1 FROM business_item_events AS event WHERE event.event_id=NEW.source_event_id AND
                        (event.physical_item_id IS NOT NEW.physical_item_id OR event.customer_id IS NOT NEW.customer_id)
                ) THEN RAISE(ABORT, 'charge event item/customer mismatch or unknown') END;
                SELECT CASE WHEN NEW.original_draft_id IS NOT NULL AND EXISTS(
                    SELECT 1 FROM business_charge_drafts AS original WHERE original.draft_id=NEW.original_draft_id
                        AND (original.customer_id IS NOT NEW.customer_id OR original.physical_item_id IS NOT NEW.physical_item_id
                             OR original.agreement_id IS NOT NEW.agreement_id OR original.fee_version_id IS NOT NEW.fee_version_id)
                ) THEN RAISE(ABORT, 'charge original attribution mismatch') END;
                SELECT CASE WHEN EXISTS(
                    SELECT 1 FROM business_charge_drafts AS correction WHERE correction.original_draft_id=NEW.draft_id
                        AND (correction.customer_id IS NOT NEW.customer_id OR correction.physical_item_id IS NOT NEW.physical_item_id
                             OR correction.agreement_id IS NOT NEW.agreement_id OR correction.fee_version_id IS NOT NEW.fee_version_id)
                ) THEN RAISE(ABORT, 'charge is referenced by another attribution') END;
                END''')
        connection.execute('''CREATE TRIGGER IF NOT EXISTS business_referenced_agreement_immutable
            BEFORE UPDATE ON business_customer_agreements WHEN
                (NEW.customer_id IS NOT OLD.customer_id OR NEW.fee_version_id IS NOT OLD.fee_version_id
                 OR NEW.contract_version_id IS NOT OLD.contract_version_id)
                AND EXISTS(SELECT 1 FROM business_charge_drafts WHERE agreement_id=OLD.agreement_id)
            BEGIN SELECT RAISE(ABORT, 'referenced agreement attribution cannot change'); END''')
        connection.execute('RELEASE SAVEPOINT business_stage1_schema')
    except Exception:
        connection.execute('ROLLBACK TO SAVEPOINT business_stage1_schema')
        connection.execute('RELEASE SAVEPOINT business_stage1_schema')
        raise
    return tuple(name for name, _ in _TABLES)
