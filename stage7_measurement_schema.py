"""Append-only, opt-in pilot records; this module performs no migration or I/O."""


def migration_statements(dialect='sqlite'):
    from stage7_common import immutable_statements
    if dialect not in ('sqlite', 'postgres'):
        raise ValueError('Unsupported dialect')
    tables = {
        'stage7_campaigns': '''campaign_id TEXT PRIMARY KEY NOT NULL,
            title TEXT NOT NULL, phase TEXT NOT NULL CHECK(phase IN ('pilot5','pilot15','novice')),
            planned_start_at TEXT NOT NULL, planned_end_at TEXT NOT NULL,
            paid INTEGER NOT NULL CHECK(paid IN (0,1)), prerequisite_references_json TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status='draft'), actor_user_id INTEGER NOT NULL REFERENCES users(id),
            recorded_at TEXT NOT NULL, source_key TEXT NOT NULL UNIQUE''',
        'stage7_participants': '''participant_id TEXT PRIMARY KEY NOT NULL,
            campaign_id TEXT NOT NULL REFERENCES stage7_campaigns(campaign_id),
            customer_id INTEGER NOT NULL REFERENCES users(id),
            experience TEXT NOT NULL CHECK(experience IN ('existing_reseller','novice')),
            actor_user_id INTEGER NOT NULL REFERENCES users(id), recorded_at TEXT NOT NULL,
            source_key TEXT NOT NULL UNIQUE, UNIQUE(campaign_id,customer_id)''',
        'stage7_observations': '''observation_id TEXT PRIMARY KEY NOT NULL,
            participant_id TEXT NOT NULL REFERENCES stage7_participants(participant_id),
            event_type TEXT NOT NULL, occurred_at TEXT NOT NULL,
            basis TEXT NOT NULL CHECK(basis IN ('observed','assumption','test')),
            execution_mode TEXT NOT NULL CHECK(execution_mode IN ('record_only','test')),
            outcome INTEGER CHECK(outcome IN (0,1)), duration_seconds INTEGER CHECK(duration_seconds>=0),
            evidence_reference TEXT NOT NULL,
            correction_of TEXT UNIQUE REFERENCES stage7_observations(observation_id),
            correction_reason TEXT, actor_user_id INTEGER NOT NULL REFERENCES users(id),
            recorded_at TEXT NOT NULL, source_key TEXT NOT NULL UNIQUE,
            CHECK((basis='test' AND execution_mode='test') OR (basis!='test' AND execution_mode='record_only')),
            CHECK(correction_of IS NULL OR correction_reason IS NOT NULL)''',
    }
    statements = []
    for name, columns in tables.items():
        if dialect == 'sqlite':
            columns = columns.replace('duration_seconds INTEGER CHECK(duration_seconds>=0)',
                "duration_seconds INTEGER CHECK(duration_seconds IS NULL OR (typeof(duration_seconds)='integer' AND duration_seconds>=0))")
        statements += ['CREATE TABLE IF NOT EXISTS ' + name + ' (' + columns + ')']
        statements += immutable_statements(name, dialect)
    statements += ['CREATE INDEX IF NOT EXISTS stage7_observation_participant ON stage7_observations(participant_id,occurred_at)',
                   'CREATE INDEX IF NOT EXISTS stage7_campaign_participant ON stage7_participants(campaign_id)']
    guard = '''NOT EXISTS(SELECT 1 FROM stage7_observations p WHERE p.observation_id=NEW.correction_of
        AND p.participant_id=NEW.participant_id AND p.event_type=NEW.event_type
        AND p.basis=NEW.basis AND p.execution_mode=NEW.execution_mode)'''
    if dialect == 'sqlite':
        statements += ["CREATE TRIGGER IF NOT EXISTS stage7_observation_scope BEFORE INSERT ON stage7_observations WHEN NEW.correction_of IS NOT NULL AND " + guard + " BEGIN SELECT RAISE(ABORT,'observation_correction_scope'); END"]
    else:
        statements += ["CREATE OR REPLACE FUNCTION stage7_observation_scope_guard() RETURNS trigger AS $$ BEGIN IF NEW.correction_of IS NOT NULL AND " + guard + " THEN RAISE EXCEPTION 'observation_correction_scope'; END IF; RETURN NEW; END; $$ LANGUAGE plpgsql",
                       'DROP TRIGGER IF EXISTS stage7_observation_scope ON stage7_observations',
                       'CREATE TRIGGER stage7_observation_scope BEFORE INSERT ON stage7_observations FOR EACH ROW EXECUTE FUNCTION stage7_observation_scope_guard()']
    return statements
