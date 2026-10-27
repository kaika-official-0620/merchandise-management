"""Append-only evidence for operating costs and comparison scenarios."""


def migration_statements(dialect='sqlite'):
    from stage7_common import immutable_statements
    if dialect not in ('sqlite', 'postgres'):
        raise ValueError('Unsupported dialect')
    tables = {
        'stage7_economic_entries': '''entry_id TEXT PRIMARY KEY NOT NULL,
            kind TEXT NOT NULL CHECK(kind IN ('cost','resource')),
            period TEXT NOT NULL, scope TEXT NOT NULL, metric TEXT NOT NULL,
            source_reference TEXT NOT NULL, actor_user_id INTEGER NOT NULL REFERENCES users(id),
            recorded_at TEXT NOT NULL, UNIQUE(kind,period,source_reference)''',
        'stage7_economic_revisions': '''revision_id TEXT PRIMARY KEY NOT NULL,
            entry_id TEXT NOT NULL REFERENCES stage7_economic_entries(entry_id),
            revision INTEGER NOT NULL CHECK(revision>0),
            previous_revision_id TEXT UNIQUE REFERENCES stage7_economic_revisions(revision_id),
            amount_yen BIGINT CHECK(amount_yen IS NULL OR amount_yen>=0),
            quantity BIGINT CHECK(quantity IS NULL OR quantity>=0),
            basis TEXT NOT NULL CHECK(basis IN ('measured','assumption','reference')),
            tax_basis TEXT NOT NULL CHECK(tax_basis IN ('exclusive','inclusive','unknown','not_applicable')),
            evidence_reference TEXT NOT NULL, reason TEXT NOT NULL,
            voided INTEGER NOT NULL CHECK(voided IN (0,1)),
            actor_user_id INTEGER NOT NULL REFERENCES users(id), recorded_at TEXT NOT NULL,
            UNIQUE(entry_id,revision)''',
        'stage7_economic_scenarios': '''scenario_id TEXT PRIMARY KEY NOT NULL,
            period TEXT NOT NULL, name TEXT NOT NULL, inputs_json TEXT NOT NULL,
            result_json TEXT NOT NULL, actor_user_id INTEGER NOT NULL REFERENCES users(id),
            recorded_at TEXT NOT NULL''',
    }
    result = []
    for table, columns in tables.items():
        result.append('CREATE TABLE IF NOT EXISTS ' + table + ' (' + columns + ')')
        result += immutable_statements(table, dialect)
    result.append("CREATE UNIQUE INDEX IF NOT EXISTS stage7_resource_dimension ON stage7_economic_entries(period,scope,metric) WHERE kind='resource'")
    return result
