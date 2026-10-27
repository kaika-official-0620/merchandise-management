"""Additive immutable listing/approval evidence. SQL only; no execution on import."""
import re


def migration_statements(dialect='sqlite'):
    if dialect not in ('sqlite', 'postgres'):
        raise ValueError('Unsupported database dialect')
    statements = [
        '''CREATE TABLE IF NOT EXISTS stage4_listing_versions(
            version_id TEXT PRIMARY KEY NOT NULL,
            physical_item_id TEXT NOT NULL REFERENCES business_items(physical_item_id),
            customer_id INTEGER NOT NULL REFERENCES users(id),
            ownership_assignment_id TEXT NOT NULL REFERENCES business_item_assignments(assignment_id),
            revision_number INTEGER NOT NULL CHECK(revision_number>0),
            supersedes_version_id TEXT UNIQUE REFERENCES stage4_listing_versions(version_id),
            actor_user_id INTEGER NOT NULL REFERENCES users(id),
            title TEXT NOT NULL, description TEXT NOT NULL, item_condition TEXT NOT NULL,
            photo_references_json TEXT NOT NULL, channels_json TEXT NOT NULL,
            price_gross_yen BIGINT NOT NULL CHECK(typeof(price_gross_yen)='integer' AND price_gross_yen BETWEEN 1 AND 9000000000000),
            minimum_price_gross_yen BIGINT NOT NULL CHECK(typeof(minimum_price_gross_yen)='integer' AND minimum_price_gross_yen BETWEEN 1 AND price_gross_yen),
            valid_until TEXT NOT NULL,
            price_change_policy TEXT NOT NULL CHECK(price_change_policy='both_reapprove'),
            item_anchor_json TEXT NOT NULL, content_hash TEXT NOT NULL,
            occurred_at TEXT NOT NULL, recorded_at TEXT NOT NULL,
            evidence_reference TEXT NOT NULL, source_key TEXT NOT NULL UNIQUE,
            UNIQUE(physical_item_id,revision_number),
            CHECK((revision_number=1 AND supersedes_version_id IS NULL) OR (revision_number>1 AND supersedes_version_id IS NOT NULL))
        )''',
        '''CREATE TABLE IF NOT EXISTS stage4_listing_decisions(
            decision_id TEXT PRIMARY KEY NOT NULL,
            version_id TEXT NOT NULL REFERENCES stage4_listing_versions(version_id),
            party_role TEXT NOT NULL CHECK(party_role IN ('customer','kaika')),
            decision TEXT NOT NULL CHECK(decision IN ('approved','rejected')),
            actor_user_id INTEGER NOT NULL REFERENCES users(id),
            content_hash TEXT NOT NULL,
            reason TEXT, occurred_at TEXT NOT NULL, recorded_at TEXT NOT NULL,
            evidence_reference TEXT NOT NULL, source_key TEXT NOT NULL UNIQUE,
            UNIQUE(version_id,party_role,decision),
            CHECK(decision!='rejected' OR reason IS NOT NULL)
        )''',
        'CREATE INDEX IF NOT EXISTS stage4_listing_customer ON stage4_listing_versions(customer_id,physical_item_id,revision_number)',
    ]
    guard = '''NOT EXISTS(SELECT 1 FROM business_item_assignments a JOIN business_parties p ON p.party_id=a.party_id
        WHERE a.assignment_id=NEW.ownership_assignment_id AND a.physical_item_id=NEW.physical_item_id
        AND a.role='owner' AND p.kind='customer' AND p.user_id=NEW.customer_id)
        OR (NEW.supersedes_version_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM stage4_listing_versions v
        WHERE v.version_id=NEW.supersedes_version_id AND v.physical_item_id=NEW.physical_item_id
        AND v.revision_number=NEW.revision_number-1))'''
    decision_guard = '''NOT EXISTS(SELECT 1 FROM stage4_listing_versions v WHERE v.version_id=NEW.version_id
        AND v.content_hash=NEW.content_hash AND (NEW.party_role!='customer' OR v.customer_id=NEW.actor_user_id))'''
    tables = ('stage4_listing_versions', 'stage4_listing_decisions')
    if dialect == 'sqlite':
        for table in tables:
            for op in ('UPDATE', 'DELETE'):
                statements.append(f"CREATE TRIGGER IF NOT EXISTS {table}_no_{op.lower()} BEFORE {op} ON {table} BEGIN SELECT RAISE(ABORT,'append-only listing evidence'); END")
        statements.append("CREATE TRIGGER IF NOT EXISTS stage4_listing_chain BEFORE INSERT ON stage4_listing_versions WHEN " + guard + " BEGIN SELECT RAISE(ABORT,'listing owner/chain mismatch'); END")
        statements.append("CREATE TRIGGER IF NOT EXISTS stage4_listing_decision_guard BEFORE INSERT ON stage4_listing_decisions WHEN " + decision_guard + " BEGIN SELECT RAISE(ABORT,'listing decision/hash mismatch'); END")
    else:
        statements = [re.sub(r"typeof\((\w+)\)='integer' AND ", '', sql) for sql in statements]
        statements.append("CREATE OR REPLACE FUNCTION stage4_listing_append_only() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'append-only listing evidence'; END $$")
        for table in tables:
            statements.append(f'DROP TRIGGER IF EXISTS {table}_immutable ON {table}')
            statements.append(f'CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION stage4_listing_append_only()')
        for name, table, predicate in (('stage4_listing_chain', 'stage4_listing_versions', guard), ('stage4_listing_decision_guard', 'stage4_listing_decisions', decision_guard)):
            statements.append(f"CREATE OR REPLACE FUNCTION {name}_fn() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF {predicate} THEN RAISE EXCEPTION 'listing evidence mismatch'; END IF; RETURN NEW; END $$")
            statements.append(f'DROP TRIGGER IF EXISTS {name} ON {table}')
            statements.append(f'CREATE TRIGGER {name} BEFORE INSERT ON {table} FOR EACH ROW EXECUTE FUNCTION {name}_fn()')
    return tuple(statements)
