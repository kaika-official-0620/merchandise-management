"""Additive customer custody-choice requests; no shipping or fee execution."""


def migration_statements(dialect='sqlite'):
    from stage6_common import immutable_statements
    if dialect not in ('sqlite', 'postgres'):
        raise ValueError('Unsupported dialect')
    tables = {
        'stage6_custody_requests': '''request_id TEXT PRIMARY KEY NOT NULL,
            physical_item_id TEXT NOT NULL REFERENCES business_items(physical_item_id),
            customer_id INTEGER NOT NULL REFERENCES users(id),
            ownership_assignment_id TEXT NOT NULL REFERENCES business_item_assignments(assignment_id),
            revision INTEGER NOT NULL CHECK(revision>0),
            previous_request_id TEXT UNIQUE REFERENCES stage6_custody_requests(request_id),
            choice TEXT NOT NULL CHECK(choice IN ('return','alternate_channel')),
            note TEXT NOT NULL, deadline_at TEXT NOT NULL, deadline_version TEXT NOT NULL,
            recorded_at TEXT NOT NULL, source_key TEXT NOT NULL UNIQUE,
            UNIQUE(ownership_assignment_id,revision)''',
        'stage6_custody_responses': '''response_id TEXT PRIMARY KEY NOT NULL,
            request_id TEXT NOT NULL REFERENCES stage6_custody_requests(request_id),
            revision INTEGER NOT NULL CHECK(revision>0),
            status TEXT NOT NULL CHECK(status IN ('acknowledged','resolved')),
            note TEXT NOT NULL, actor_user_id INTEGER NOT NULL REFERENCES users(id),
            recorded_at TEXT NOT NULL, source_key TEXT NOT NULL UNIQUE,
            UNIQUE(request_id,revision)''',
    }
    statements = []
    for table, columns in tables.items():
        statements.append('CREATE TABLE IF NOT EXISTS ' + table + ' (' + columns + ')')
        statements += immutable_statements(table, dialect)
    guard = '''NOT EXISTS (SELECT 1 FROM business_item_assignments a JOIN business_parties p ON p.party_id=a.party_id
        WHERE a.assignment_id=NEW.ownership_assignment_id AND a.physical_item_id=NEW.physical_item_id
        AND a.role='owner' AND p.kind='customer' AND p.user_id=NEW.customer_id)'''
    if dialect == 'sqlite':
        statements.append("CREATE TRIGGER IF NOT EXISTS stage6_custody_scope BEFORE INSERT ON stage6_custody_requests WHEN " + guard + " BEGIN SELECT RAISE(ABORT,'custody_request_scope'); END")
    else:
        statements += ["CREATE OR REPLACE FUNCTION stage6_custody_scope_guard() RETURNS trigger AS $$ BEGIN IF " + guard + " THEN RAISE EXCEPTION 'custody_request_scope'; END IF; RETURN NEW; END; $$ LANGUAGE plpgsql",
            'DROP TRIGGER IF EXISTS stage6_custody_scope ON stage6_custody_requests',
            'CREATE TRIGGER stage6_custody_scope BEFORE INSERT ON stage6_custody_requests FOR EACH ROW EXECUTE FUNCTION stage6_custody_scope_guard()']
    return statements
