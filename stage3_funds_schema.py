"""Reviewable additive DDL for a test/entered-record ledger, never live money.

No connection, migration, backfill, commit, or provider invocation on import.
The caller must use the explicitly isolated Stage 3 fixture migration.
"""

MAX_YEN = 9_000_000_000_000
BUCKETS = ('purchase_available', 'purchase_reserved', 'sales_unpaid',
           'sales_unsettled', 'sales_return_hold', 'sales_settlement_ready',
           'fees_payable')
KINDS = ('deposit', 'reserve', 'release', 'purchase', 'refund', 'invoice',
         'payment', 'correction', 'sales_record', 'sales_receipt', 'sales_hold',
         'sales_release', 'sales_ready', 'sales_settlement')

# Restrict the accounting shape even if an application caller accidentally
# inserts a wrong delta. Origin-specific limits and concurrent balances remain
# transactional service checks; these patterns never authorize actual money.
_PATTERNS = {
    'deposit': ({'purchase_available': 1},),
    'reserve': ({'purchase_available': -1, 'purchase_reserved': 1},),
    'release': ({'purchase_available': 1, 'purchase_reserved': -1},),
    'purchase': ({'purchase_reserved': -1},),
    'invoice': ({'fees_payable': 1},), 'payment': ({'fees_payable': -1},),
    'sales_record': ({'sales_unpaid': 1},),
    'sales_receipt': ({'sales_unpaid': -1, 'sales_unsettled': 1},),
    'sales_hold': ({'sales_unsettled': -1, 'sales_return_hold': 1},
                   {'sales_settlement_ready': -1, 'sales_return_hold': 1}),
    'sales_release': ({'sales_return_hold': -1, 'sales_unsettled': 1},),
    'sales_ready': ({'sales_unsettled': -1, 'sales_settlement_ready': 1},),
    'sales_settlement': ({'sales_settlement_ready': -1},),
    'refund': ({'purchase_available': -1}, {'purchase_available': 1},
               {'fees_payable': 1}, {'sales_return_hold': -1}),
    'correction': ({'purchase_available': -1}, {'fees_payable': -1}, {'fees_payable': 1},
                   {'sales_unpaid': -1}, {'sales_unpaid': 1, 'sales_unsettled': -1}),
}


def migration_statements(dialect='sqlite'):
    if dialect not in ('sqlite', 'postgres'):
        raise ValueError('Unsupported database dialect')
    integer = 'BIGINT' if dialect == 'postgres' else 'INTEGER'

    def amount_check(column, positive=False):
        type_check = '' if dialect == 'postgres' else "typeof(" + column + ")='integer' AND "
        return type_check + column + ' BETWEEN ' + ('1' if positive else str(-MAX_YEN)) + ' AND ' + str(MAX_YEN)

    deltas = ',\n'.join('delta_' + bucket + ' ' + integer + ' NOT NULL CHECK(' +
                          amount_check('delta_' + bucket) + ')' for bucket in BUCKETS)
    shapes = []
    for kind, patterns in _PATTERNS.items():
        for pattern in patterns:
            conditions = ["kind='" + kind + "'"]
            for bucket in BUCKETS:
                sign = pattern.get(bucket, 0)
                expected = 'amount_yen' if sign == 1 else '-amount_yen' if sign == -1 else '0'
                conditions.append('delta_' + bucket + '=' + expected)
            shapes.append('(' + ' AND '.join(conditions) + ')')
    sql = ['''CREATE TABLE IF NOT EXISTS stage3_fund_records (
        record_id TEXT PRIMARY KEY NOT NULL,
        customer_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
        actor_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
        execution_mode TEXT NOT NULL CHECK(execution_mode IN ('test','record_only')),
        kind TEXT NOT NULL CHECK(kind IN (''' + ','.join("'" + k + "'" for k in KINDS) + ''')),
        amount_yen ''' + integer + ' NOT NULL CHECK(' + amount_check('amount_yen', True) + '''),
        currency TEXT NOT NULL DEFAULT 'JPY' CHECK(currency='JPY'),
        occurred_at TEXT NOT NULL,
        recorded_at TEXT NOT NULL,
        evidence_reference TEXT NOT NULL CHECK(length(evidence_reference)>0),
        source_key TEXT NOT NULL UNIQUE,
        original_record_id TEXT,
        physical_item_id TEXT REFERENCES business_items(physical_item_id) ON DELETE RESTRICT,
        note TEXT,
        provider_verified INTEGER NOT NULL DEFAULT 0 CHECK(provider_verified=0),
        live_execution_enabled INTEGER NOT NULL DEFAULT 0 CHECK(live_execution_enabled=0),
        ''' + deltas + ''',
        UNIQUE(record_id,customer_id,execution_mode),
        FOREIGN KEY(original_record_id,customer_id,execution_mode)
            REFERENCES stage3_fund_records(record_id,customer_id,execution_mode) ON DELETE RESTRICT,
        CHECK((kind IN ('deposit','reserve','invoice','sales_record') AND original_record_id IS NULL)
           OR (kind NOT IN ('deposit','reserve','invoice','sales_record') AND original_record_id IS NOT NULL)),
        CHECK(kind NOT IN ('reserve','purchase') OR physical_item_id IS NOT NULL),
        CHECK(original_record_id IS NULL OR original_record_id<>record_id),
        CHECK(''' + ' OR '.join(shapes) + ''')
    )''',
        'CREATE INDEX IF NOT EXISTS stage3_funds_customer ON stage3_fund_records(customer_id,execution_mode,recorded_at,record_id)',
        'CREATE INDEX IF NOT EXISTS stage3_funds_origin ON stage3_fund_records(original_record_id)',
        'CREATE INDEX IF NOT EXISTS stage3_funds_item ON stage3_fund_records(physical_item_id,execution_mode,kind)',
    ]
    if dialect == 'sqlite':
        for operation in ('UPDATE', 'DELETE'):
            sql.append('CREATE TRIGGER IF NOT EXISTS stage3_funds_no_' + operation.lower() +
                       ' BEFORE ' + operation + " ON stage3_fund_records BEGIN SELECT RAISE(ABORT,'append-only financial record'); END")
    else:
        sql += ["""CREATE OR REPLACE FUNCTION stage3_funds_append_only() RETURNS trigger
                    LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'append-only financial record'; END $$""",
                'DROP TRIGGER IF EXISTS stage3_funds_immutable ON stage3_fund_records',
                'CREATE TRIGGER stage3_funds_immutable BEFORE UPDATE OR DELETE ON stage3_fund_records FOR EACH ROW EXECUTE FUNCTION stage3_funds_append_only()']
    return tuple(sql)
