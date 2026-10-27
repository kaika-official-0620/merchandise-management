"""Additive financial evidence DDL. Import does not connect or migrate."""
import re


_MONEY = ('purchase_cost_yen', 'sale_gross_yen', 'channel_fee_yen', 'materials_yen',
          'kaika_fee_yen', 'return_adjustment_yen', 'monthly_allocation_yen')


def migration_statements(dialect='sqlite'):
    if dialect not in ('sqlite', 'postgres'):
        raise ValueError('Unsupported database dialect')
    amounts = ','.join(name + " BIGINT CHECK(" + name + " IS NULL OR (typeof(" + name +
                      ")='integer' AND " + name + (' BETWEEN -9000000000000 AND 9000000000000' if name == 'return_adjustment_yen'
                                                  else ' BETWEEN 0 AND 9000000000000') + '))' for name in _MONEY)
    facts = '''CREATE TABLE IF NOT EXISTS stage3_item_financial_facts (
        version_id TEXT PRIMARY KEY NOT NULL,
        physical_item_id TEXT NOT NULL REFERENCES business_items(physical_item_id),
        customer_id INTEGER NOT NULL REFERENCES users(id),
        ownership_assignment_id TEXT NOT NULL REFERENCES business_item_assignments(assignment_id),
        revision_number INTEGER NOT NULL CHECK(revision_number > 0),
        previous_version_id TEXT UNIQUE REFERENCES stage3_item_financial_facts(version_id),
        actor_user_id INTEGER NOT NULL REFERENCES users(id),
        occurred_at TEXT NOT NULL, recorded_at TEXT NOT NULL,
        source_reference TEXT NOT NULL, source_event_key TEXT NOT NULL UNIQUE,
        currency TEXT NOT NULL DEFAULT 'JPY' CHECK(currency='JPY'),
        basis TEXT NOT NULL CHECK(basis IN ('actual','estimate')),
        channel TEXT CHECK(channel IN ('wholesale','multichannel','dealer_auction','self_sale_shipping','live','other')),
        sale_event_id TEXT REFERENCES business_item_observations(observation_id),
        channel_fee_includes_shipping INTEGER CHECK(channel_fee_includes_shipping IN (0,1)),
        shipping_entries_json TEXT,
        ''' + amounts + ''',
        UNIQUE(ownership_assignment_id,revision_number),
        CHECK((revision_number=1 AND previous_version_id IS NULL) OR (revision_number>1 AND previous_version_id IS NOT NULL))
    )'''
    states = '''CREATE TABLE IF NOT EXISTS stage3_sale_status_records (
        status_record_id TEXT PRIMARY KEY NOT NULL,
        physical_item_id TEXT NOT NULL REFERENCES business_items(physical_item_id),
        customer_id INTEGER NOT NULL REFERENCES users(id),
        ownership_assignment_id TEXT NOT NULL REFERENCES business_item_assignments(assignment_id),
        sale_event_id TEXT NOT NULL REFERENCES business_item_observations(observation_id),
        fact_version_id TEXT NOT NULL REFERENCES stage3_item_financial_facts(version_id),
        previous_status_id TEXT UNIQUE REFERENCES stage3_sale_status_records(status_record_id),
        actor_user_id INTEGER NOT NULL REFERENCES users(id),
        status TEXT NOT NULL CHECK(status IN ('unpaid','paid','return_hold','settlement_ready')),
        occurred_at TEXT NOT NULL, recorded_at TEXT NOT NULL,
        source_reference TEXT NOT NULL, resolution_reference TEXT,
        source_event_key TEXT NOT NULL UNIQUE,
        purchase_funds_credit_yen BIGINT NOT NULL DEFAULT 0 CHECK(purchase_funds_credit_yen=0)
    )'''
    if dialect == 'postgres':
        facts = re.sub(r"typeof\((\w+)\)='integer' AND ", '', facts)
    statements = [facts, states,
        'CREATE INDEX IF NOT EXISTS stage3_financial_facts_customer ON stage3_item_financial_facts(customer_id,physical_item_id,revision_number)',
        'CREATE INDEX IF NOT EXISTS stage3_sale_status_customer ON stage3_sale_status_records(customer_id,physical_item_id,sale_event_id)',
        'CREATE UNIQUE INDEX IF NOT EXISTS stage3_sale_status_initial ON stage3_sale_status_records(ownership_assignment_id,sale_event_id) WHERE previous_status_id IS NULL']
    if dialect == 'sqlite':
        for table in ('stage3_item_financial_facts', 'stage3_sale_status_records'):
            for op in ('UPDATE', 'DELETE'):
                statements.append('CREATE TRIGGER IF NOT EXISTS ' + table + '_no_' + op.lower() + ' BEFORE ' + op +
                                  ' ON ' + table + " BEGIN SELECT RAISE(ABORT,'append-only financial evidence'); END")
        statements.extend([
            '''CREATE TRIGGER IF NOT EXISTS stage3_financial_fact_chain BEFORE INSERT ON stage3_item_financial_facts
            WHEN NOT EXISTS (SELECT 1 FROM business_item_assignments a JOIN business_parties p ON p.party_id=a.party_id
              WHERE a.assignment_id=NEW.ownership_assignment_id AND a.physical_item_id=NEW.physical_item_id
              AND a.role='owner' AND p.kind='customer' AND p.user_id=NEW.customer_id)
              OR (NEW.previous_version_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM stage3_item_financial_facts f
              WHERE f.version_id=NEW.previous_version_id AND f.ownership_assignment_id=NEW.ownership_assignment_id
              AND f.physical_item_id=NEW.physical_item_id AND f.customer_id=NEW.customer_id AND f.revision_number=NEW.revision_number-1))
              OR (NEW.sale_event_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM business_item_observations e
              WHERE e.observation_id=NEW.sale_event_id AND e.kind='sold' AND e.physical_item_id=NEW.physical_item_id AND e.customer_id=NEW.customer_id))
            BEGIN SELECT RAISE(ABORT,'financial evidence owner/event/chain mismatch'); END''',
            '''CREATE TRIGGER IF NOT EXISTS stage3_sale_status_chain BEFORE INSERT ON stage3_sale_status_records
            WHEN NOT EXISTS (SELECT 1 FROM stage3_item_financial_facts f WHERE f.version_id=NEW.fact_version_id
              AND f.physical_item_id=NEW.physical_item_id AND f.customer_id=NEW.customer_id
              AND f.ownership_assignment_id=NEW.ownership_assignment_id AND f.sale_event_id=NEW.sale_event_id)
              OR (NEW.previous_status_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM stage3_sale_status_records s
              WHERE s.status_record_id=NEW.previous_status_id AND s.ownership_assignment_id=NEW.ownership_assignment_id
              AND s.sale_event_id=NEW.sale_event_id AND s.customer_id=NEW.customer_id))
            BEGIN SELECT RAISE(ABORT,'sale status evidence chain mismatch'); END'''])
    else:
        statements.append("CREATE OR REPLACE FUNCTION stage3_profit_append_only() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'append-only financial evidence'; END $$")
        for table in ('stage3_item_financial_facts', 'stage3_sale_status_records'):
            statements.append('DROP TRIGGER IF EXISTS ' + table + '_immutable ON ' + table)
            statements.append('CREATE TRIGGER ' + table + '_immutable BEFORE UPDATE OR DELETE ON ' + table + ' FOR EACH ROW EXECUTE FUNCTION stage3_profit_append_only()')
        # PostgreSQL owner/chain constraints must have the same meaning as SQLite.
        statements.append('''CREATE OR REPLACE FUNCTION stage3_profit_fact_guard() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
            IF NOT EXISTS (SELECT 1 FROM business_item_assignments a JOIN business_parties p ON p.party_id=a.party_id
              WHERE a.assignment_id=NEW.ownership_assignment_id AND a.physical_item_id=NEW.physical_item_id AND a.role='owner' AND p.kind='customer' AND p.user_id=NEW.customer_id)
              OR (NEW.previous_version_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM stage3_item_financial_facts f
              WHERE f.version_id=NEW.previous_version_id AND f.ownership_assignment_id=NEW.ownership_assignment_id AND f.physical_item_id=NEW.physical_item_id AND f.customer_id=NEW.customer_id AND f.revision_number=NEW.revision_number-1))
              OR (NEW.sale_event_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM business_item_observations e WHERE e.observation_id=NEW.sale_event_id AND e.kind='sold' AND e.physical_item_id=NEW.physical_item_id AND e.customer_id=NEW.customer_id))
            THEN RAISE EXCEPTION 'financial evidence owner/event/chain mismatch'; END IF; RETURN NEW; END $$''')
        statements.append('DROP TRIGGER IF EXISTS stage3_financial_fact_chain ON stage3_item_financial_facts')
        statements.append('CREATE TRIGGER stage3_financial_fact_chain BEFORE INSERT ON stage3_item_financial_facts FOR EACH ROW EXECUTE FUNCTION stage3_profit_fact_guard()')
        statements.append('''CREATE OR REPLACE FUNCTION stage3_profit_status_guard() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
            IF NOT EXISTS (SELECT 1 FROM stage3_item_financial_facts f WHERE f.version_id=NEW.fact_version_id AND f.physical_item_id=NEW.physical_item_id AND f.customer_id=NEW.customer_id AND f.ownership_assignment_id=NEW.ownership_assignment_id AND f.sale_event_id=NEW.sale_event_id)
              OR (NEW.previous_status_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM stage3_sale_status_records s WHERE s.status_record_id=NEW.previous_status_id AND s.ownership_assignment_id=NEW.ownership_assignment_id AND s.sale_event_id=NEW.sale_event_id AND s.customer_id=NEW.customer_id))
            THEN RAISE EXCEPTION 'sale status evidence chain mismatch'; END IF; RETURN NEW; END $$''')
        statements.append('DROP TRIGGER IF EXISTS stage3_sale_status_chain ON stage3_sale_status_records')
        statements.append('CREATE TRIGGER stage3_sale_status_chain BEFORE INSERT ON stage3_sale_status_records FOR EACH ROW EXECUTE FUNCTION stage3_profit_status_guard()')
    return tuple(statements)
