CREATE TABLE affinity_evidence (
    local_id TEXT PRIMARY KEY,
    artist_local_id TEXT NOT NULL REFERENCES artists(local_id),
    source TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (
        kind IN ('followed', 'saved_track', 'top_short_term', 'top_medium_term', 'top_long_term')
    ),
    evidence_key TEXT NOT NULL,
    rank INTEGER,
    observed_at TEXT NOT NULL,
    CHECK (
        (kind IN ('top_short_term', 'top_medium_term', 'top_long_term')
         AND rank BETWEEN 1 AND 50)
        OR (kind IN ('followed', 'saved_track') AND rank IS NULL)
    ),
    UNIQUE (source, kind, artist_local_id, evidence_key)
);

CREATE INDEX affinity_evidence_artist_order
ON affinity_evidence (artist_local_id, kind, evidence_key, local_id);

CREATE TABLE watchlist_overrides (
    artist_local_id TEXT PRIMARY KEY REFERENCES artists(local_id),
    action TEXT NOT NULL CHECK (action IN ('add', 'pin', 'mute')),
    updated_at TEXT NOT NULL
);

CREATE TABLE local_preferences (
    key TEXT PRIMARY KEY CHECK (
        key IN ('event_country_code', 'event_postal_code', 'event_radius', 'event_radius_unit')
    ),
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE refresh_runs (
    local_id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('catalog', 'releases', 'events', 'all')),
    status TEXT NOT NULL CHECK (status IN ('running', 'succeeded', 'partial', 'failed')),
    started_at TEXT NOT NULL,
    finished_at TEXT,
    summary_json TEXT NOT NULL CHECK (length(summary_json) <= 4096),
    CHECK (
        (status = 'running' AND finished_at IS NULL)
        OR (status != 'running' AND finished_at IS NOT NULL)
    )
);

CREATE INDEX refresh_runs_recent_order
ON refresh_runs (started_at DESC, local_id);

CREATE TABLE source_cursors (
    source TEXT NOT NULL,
    capability TEXT NOT NULL CHECK (
        capability IN (
            'followed_artists', 'saved_items', 'top_artists_short_term',
            'top_artists_medium_term', 'top_artists_long_term', 'recent_releases', 'events'
        )
    ),
    cursor TEXT NOT NULL CHECK (length(cursor) <= 2048),
    updated_at TEXT NOT NULL,
    PRIMARY KEY (source, capability)
);

CREATE TABLE signals (
    local_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('release', 'event')),
    record_local_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    provider_native_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    material_version TEXT NOT NULL,
    explanation_json TEXT NOT NULL CHECK (length(explanation_json) <= 8192),
    observed_at TEXT NOT NULL,
    UNIQUE (provider, kind, provider_native_id, material_version)
);

CREATE INDEX signals_fingerprint_order
ON signals (kind, fingerprint, material_version, local_id);

CREATE INDEX signals_observed_order
ON signals (observed_at DESC, local_id);

CREATE TRIGGER signals_target_insert
BEFORE INSERT ON signals
BEGIN
    SELECT RAISE(ABORT, 'signal target does not exist')
    WHERE (NEW.kind = 'release'
           AND NOT EXISTS (SELECT 1 FROM releases WHERE local_id = NEW.record_local_id))
       OR (NEW.kind = 'event'
           AND NOT EXISTS (SELECT 1 FROM events WHERE local_id = NEW.record_local_id));
END;

CREATE TRIGGER signals_target_update
BEFORE UPDATE OF kind, record_local_id ON signals
BEGIN
    SELECT RAISE(ABORT, 'signal target does not exist')
    WHERE (NEW.kind = 'release'
           AND NOT EXISTS (SELECT 1 FROM releases WHERE local_id = NEW.record_local_id))
       OR (NEW.kind = 'event'
           AND NOT EXISTS (SELECT 1 FROM events WHERE local_id = NEW.record_local_id));
END;

CREATE TRIGGER releases_signal_referenced_delete
BEFORE DELETE ON releases
WHEN EXISTS (
    SELECT 1 FROM signals WHERE kind = 'release' AND record_local_id = OLD.local_id
)
BEGIN
    SELECT RAISE(ABORT, 'referenced signal target cannot be deleted');
END;

CREATE TRIGGER releases_signal_referenced_identity_update
BEFORE UPDATE OF local_id ON releases
WHEN NEW.local_id != OLD.local_id AND EXISTS (
    SELECT 1 FROM signals WHERE kind = 'release' AND record_local_id = OLD.local_id
)
BEGIN
    SELECT RAISE(ABORT, 'referenced signal target identity cannot change');
END;

CREATE TRIGGER events_signal_referenced_delete
BEFORE DELETE ON events
WHEN EXISTS (
    SELECT 1 FROM signals WHERE kind = 'event' AND record_local_id = OLD.local_id
)
BEGIN
    SELECT RAISE(ABORT, 'referenced signal target cannot be deleted');
END;

CREATE TRIGGER events_signal_referenced_identity_update
BEFORE UPDATE OF local_id ON events
WHEN NEW.local_id != OLD.local_id AND EXISTS (
    SELECT 1 FROM signals WHERE kind = 'event' AND record_local_id = OLD.local_id
)
BEGIN
    SELECT RAISE(ABORT, 'referenced signal target identity cannot change');
END;

CREATE TABLE inbox_entries (
    local_id TEXT PRIMARY KEY,
    signal_local_id TEXT NOT NULL UNIQUE REFERENCES signals(local_id) ON DELETE CASCADE,
    state TEXT NOT NULL CHECK (state IN ('unread', 'saved', 'dismissed')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX inbox_entries_state_order
ON inbox_entries (state, updated_at DESC, local_id);
