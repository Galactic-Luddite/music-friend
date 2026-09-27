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
CREATE TABLE artist_identity_mappings (
    artist_local_id TEXT NOT NULL,
    source TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('mapped', 'unmapped')),
    method TEXT NOT NULL CHECK (method IN ('url_rel', 'name_search', 'user')),
    attempted_at TEXT NOT NULL,
    PRIMARY KEY (artist_local_id, source),
    FOREIGN KEY (artist_local_id) REFERENCES artists(local_id) ON DELETE CASCADE
);
CREATE TABLE artists (
    local_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    identity_confidence TEXT NOT NULL,
    observed_at TEXT NOT NULL
);
CREATE TABLE catalog_sync_cursors (
    source TEXT NOT NULL,
    capability TEXT NOT NULL,
    last_successful_at TEXT NOT NULL,
    PRIMARY KEY (source, capability)
);
CREATE TABLE check_times (
    source TEXT PRIMARY KEY,
    checked_at TEXT NOT NULL
);
CREATE TABLE event_artists (
    event_id TEXT NOT NULL REFERENCES events(local_id) ON DELETE CASCADE,
    artist_id TEXT NOT NULL REFERENCES artists(local_id),
    position INTEGER NOT NULL,
    PRIMARY KEY (event_id, position),
    UNIQUE (event_id, artist_id)
);
CREATE TABLE event_check_caches (
    source TEXT NOT NULL,
    artist_local_id TEXT NOT NULL REFERENCES artists(local_id),
    expires_at TEXT NOT NULL,
    PRIMARY KEY (source, artist_local_id)
);
CREATE TABLE event_discoveries (
    event_local_id TEXT PRIMARY KEY REFERENCES events(local_id),
    source TEXT NOT NULL,
    provider_native_id TEXT NOT NULL,
    artist_local_id TEXT NOT NULL REFERENCES artists(local_id),
    variant_identity TEXT NOT NULL,
    material_identity TEXT NOT NULL,
    attribution TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    UNIQUE (source, provider_native_id)
);
CREATE TABLE event_source_links (
    event_id TEXT NOT NULL REFERENCES events(local_id) ON DELETE CASCADE,
    source_link TEXT NOT NULL,
    position INTEGER NOT NULL,
    PRIMARY KEY (event_id, position),
    UNIQUE (event_id, source_link)
);
CREATE TABLE events (
    local_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    venue_name TEXT,
    locality TEXT,
    starts_at TEXT,
    time_precision TEXT,
    observed_at TEXT NOT NULL
);
INSERT INTO "events" VALUES('event-c','Case C Event','Venue','City','2026-06-01T20:00:00+00:00','minute','2026-01-01T00:00:00+00:00');
CREATE TABLE inbox_entries (
    local_id TEXT PRIMARY KEY,
    signal_local_id TEXT NOT NULL UNIQUE REFERENCES signals(local_id) ON DELETE CASCADE,
    state TEXT NOT NULL CHECK (state IN ('unread', 'saved', 'dismissed')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
INSERT INTO "inbox_entries" VALUES('entry-a-saved','signal-a1','saved','2026-01-01T01:00:00+00:00','2026-01-01T02:00:00+00:00');
INSERT INTO "inbox_entries" VALUES('entry-a-unread','signal-a2','unread','2026-01-02T01:00:00+00:00','2026-01-02T02:00:00+00:00');
INSERT INTO "inbox_entries" VALUES('entry-b-dismissed','signal-b1','dismissed','2026-01-01T01:00:00+00:00','2026-01-01T03:00:00+00:00');
INSERT INTO "inbox_entries" VALUES('entry-b-saved','signal-b2','saved','2026-01-03T01:00:00+00:00','2026-01-03T04:00:00+00:00');
INSERT INTO "inbox_entries" VALUES('entry-c1','signal-c1','unread','2026-01-01T01:00:00+00:00','2026-01-01T01:30:00+00:00');
INSERT INTO "inbox_entries" VALUES('entry-c2','signal-c2','unread','2026-01-04T01:00:00+00:00','2026-01-04T05:00:00+00:00');
INSERT INTO "inbox_entries" VALUES('entry-d','signal-d1','saved','2026-01-01T01:00:00+00:00','2026-01-01T02:00:00+00:00');
CREATE TABLE interests (
    local_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    target_local_id TEXT NOT NULL,
    status TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE listening_history (
    event_id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    played_at TEXT NOT NULL,
    milliseconds_played INTEGER NOT NULL CHECK (milliseconds_played >= 0),
    track_uri TEXT NOT NULL,
    track_name TEXT NOT NULL,
    artist_name TEXT NOT NULL,
    album_name TEXT,
    reason_start TEXT,
    reason_end TEXT,
    shuffle INTEGER CHECK (shuffle IN (0, 1) OR shuffle IS NULL),
    skipped INTEGER CHECK (skipped IN (0, 1) OR skipped IS NULL),
    offline INTEGER CHECK (offline IN (0, 1) OR offline IS NULL),
    incognito INTEGER CHECK (incognito IN (0, 1) OR incognito IS NULL),
    archive_digest TEXT NOT NULL,
    imported_at TEXT NOT NULL
);
CREATE TABLE local_preferences (
    key TEXT PRIMARY KEY CHECK (
        key IN ('event_country_code', 'event_postal_code', 'event_radius', 'event_radius_unit')
    ),
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE observations (
    local_id TEXT PRIMARY KEY,
    source_reference_id INTEGER NOT NULL,
    source TEXT NOT NULL,
    native_id TEXT NOT NULL,
    record_kind TEXT NOT NULL,
    record_local_id TEXT NOT NULL,
    fact_name TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    FOREIGN KEY (source, native_id)
        REFERENCES source_references(source, native_id) ON DELETE CASCADE,
    FOREIGN KEY (record_kind, record_local_id, source_reference_id)
        REFERENCES record_sources(record_kind, record_local_id, source_reference_id)
        ON DELETE CASCADE
);
CREATE TABLE record_sources (
    record_kind TEXT NOT NULL,
    record_local_id TEXT NOT NULL,
    source_reference_id INTEGER NOT NULL REFERENCES source_references(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    PRIMARY KEY (record_kind, record_local_id, position),
    UNIQUE (record_kind, record_local_id, source_reference_id)
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
CREATE TABLE release_artists (
    release_id TEXT NOT NULL REFERENCES releases(local_id) ON DELETE CASCADE,
    artist_id TEXT NOT NULL REFERENCES artists(local_id),
    position INTEGER NOT NULL,
    PRIMARY KEY (release_id, position),
    UNIQUE (release_id, artist_id)
);
CREATE TABLE release_check_continuations (
    source TEXT NOT NULL,
    artist_local_id TEXT NOT NULL REFERENCES artists(local_id),
    cursor TEXT NOT NULL CHECK (length(cursor) <= 512),
    since TEXT NOT NULL,
    PRIMARY KEY (source, artist_local_id)
);
CREATE TABLE release_check_cursors (
    source TEXT NOT NULL,
    artist_local_id TEXT NOT NULL REFERENCES artists(local_id),
    last_successful_at TEXT NOT NULL,
    PRIMARY KEY (source, artist_local_id)
);
CREATE TABLE release_discoveries (
    release_local_id TEXT PRIMARY KEY REFERENCES releases(local_id),
    source TEXT NOT NULL,
    provider_native_id TEXT NOT NULL,
    normalized_title TEXT NOT NULL,
    release_date TEXT NOT NULL,
    material_identity TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    UNIQUE (source, provider_native_id)
);
CREATE TABLE releases (
    local_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    release_type TEXT NOT NULL,
    release_date TEXT NOT NULL,
    date_precision TEXT NOT NULL,
    observed_at TEXT NOT NULL
);
INSERT INTO "releases" VALUES('release-a','Case A Release','album','2026-01-01','day','2026-01-01T00:00:00+00:00');
INSERT INTO "releases" VALUES('release-b','Case B Release','album','2026-01-01','day','2026-01-01T00:00:00+00:00');
INSERT INTO "releases" VALUES('release-d','Case D Release','album','2026-01-01','day','2026-01-01T00:00:00+00:00');
CREATE TABLE schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
INSERT INTO "schema_migrations" VALUES(1,'2026-09-27 01:31:55');
INSERT INTO "schema_migrations" VALUES(2,'2026-09-27 01:31:55');
INSERT INTO "schema_migrations" VALUES(3,'2026-09-27 01:31:55');
INSERT INTO "schema_migrations" VALUES(4,'2026-09-27 01:31:55');
INSERT INTO "schema_migrations" VALUES(5,'2026-09-27 01:31:55');
INSERT INTO "schema_migrations" VALUES(6,'2026-09-27 01:31:55');
INSERT INTO "schema_migrations" VALUES(7,'2026-09-27 01:31:55');
INSERT INTO "schema_migrations" VALUES(8,'2026-09-27 01:31:55');
INSERT INTO "schema_migrations" VALUES(9,'2026-09-27 01:31:55');
INSERT INTO "schema_migrations" VALUES(10,'2026-09-27 01:31:55');
INSERT INTO "schema_migrations" VALUES(11,'2026-09-27 01:31:55');
INSERT INTO "schema_migrations" VALUES(12,'2026-09-27 01:31:55');
INSERT INTO "schema_migrations" VALUES(13,'2026-09-27 01:31:55');
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
INSERT INTO "signals" VALUES('signal-a1','release','release-a','spotify','a-native-1','fp-a1','material-v1','{"version": 1, "reasons": [{"kind": "new_release", "detail": null}]}','2026-01-01T00:00:00+00:00');
INSERT INTO "signals" VALUES('signal-a2','release','release-a','spotify','a-native-1','fp-a1','material-v2','{"version": 1, "reasons": [{"kind": "new_release", "detail": null}]}','2026-01-02T00:00:00+00:00');
INSERT INTO "signals" VALUES('signal-b1','release','release-b','spotify','b-native-1','fp-b1','material-v1','{"version": 1, "reasons": [{"kind": "new_release", "detail": null}]}','2026-01-01T00:00:00+00:00');
INSERT INTO "signals" VALUES('signal-b2','release','release-b','spotify','b-native-1','fp-b1','material-v2','{"version": 1, "reasons": [{"kind": "new_release", "detail": null}]}','2026-01-03T00:00:00+00:00');
INSERT INTO "signals" VALUES('signal-c1','event','event-c','ticketmaster','c-native-1','fp-c1','material-v1','{"version": 1, "reasons": [{"kind": "new_release", "detail": null}]}','2026-01-01T00:00:00+00:00');
INSERT INTO "signals" VALUES('signal-c2','event','event-c','ticketmaster','c-native-1','fp-c1','material-v2','{"version": 1, "reasons": [{"kind": "new_release", "detail": null}]}','2026-01-04T00:00:00+00:00');
INSERT INTO "signals" VALUES('signal-d1','release','release-d','spotify','d-native-1','fp-d1','material-v1','{"version": 1, "reasons": [{"kind": "new_release", "detail": null}]}','2026-01-01T00:00:00+00:00');
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
CREATE TABLE source_limits (
    source TEXT PRIMARY KEY,
    state TEXT NOT NULL CHECK (state IN ('available', 'cooling_down', 'quota_exhausted')),
    observed_at TEXT NOT NULL,
    retry_at TEXT,
    retry_is_exact INTEGER NOT NULL CHECK (retry_is_exact IN (0, 1)),
    consecutive_limits INTEGER NOT NULL CHECK (consecutive_limits BETWEEN 0 AND 1000000), window_calls INTEGER NOT NULL DEFAULT 8
    CHECK (window_calls BETWEEN 1 AND 8),
    CHECK (
        (state = 'available' AND retry_at IS NULL AND retry_is_exact = 0
         AND consecutive_limits = 0)
        OR (state = 'cooling_down' AND retry_at IS NOT NULL AND consecutive_limits >= 1)
        OR (state = 'quota_exhausted' AND retry_at IS NULL AND retry_is_exact = 0
            AND consecutive_limits >= 1)
    )
);
CREATE TABLE source_references (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    native_id TEXT NOT NULL,
    canonical_url TEXT,
    observed_at TEXT NOT NULL, confidence TEXT NOT NULL DEFAULT 'source_only' CHECK (confidence IN ('source_only', 'external_id', 'user_confirmed')),
    UNIQUE (source, native_id)
);
CREATE TABLE watchlist_overrides (
    artist_local_id TEXT PRIMARY KEY REFERENCES artists(local_id),
    action TEXT NOT NULL CHECK (action IN ('add', 'pin', 'mute')),
    updated_at TEXT NOT NULL
);
CREATE TRIGGER observations_source_identity_insert
BEFORE INSERT ON observations
BEGIN
    SELECT RAISE(ABORT, 'observation source identity does not match')
    WHERE NOT EXISTS (
        SELECT 1 FROM source_references
        WHERE id = NEW.source_reference_id
          AND source = NEW.source
          AND native_id = NEW.native_id
    );
END;
CREATE TRIGGER observations_source_identity_update
BEFORE UPDATE OF source_reference_id, source, native_id ON observations
BEGIN
    SELECT RAISE(ABORT, 'observation source identity does not match')
    WHERE NOT EXISTS (
        SELECT 1 FROM source_references
        WHERE id = NEW.source_reference_id
          AND source = NEW.source
          AND native_id = NEW.native_id
    );
END;
CREATE TRIGGER record_sources_target_insert
BEFORE INSERT ON record_sources
BEGIN
    SELECT RAISE(ABORT, 'record source target does not exist')
    WHERE NEW.record_kind NOT IN ('artist', 'release', 'event')
       OR (NEW.record_kind = 'artist'
           AND NOT EXISTS (SELECT 1 FROM artists WHERE local_id = NEW.record_local_id))
       OR (NEW.record_kind = 'release'
           AND NOT EXISTS (SELECT 1 FROM releases WHERE local_id = NEW.record_local_id))
       OR (NEW.record_kind = 'event'
           AND NOT EXISTS (SELECT 1 FROM events WHERE local_id = NEW.record_local_id));
END;
CREATE TRIGGER record_sources_target_update
BEFORE UPDATE OF record_kind, record_local_id ON record_sources
BEGIN
    SELECT RAISE(ABORT, 'record source target does not exist')
    WHERE NEW.record_kind NOT IN ('artist', 'release', 'event')
       OR (NEW.record_kind = 'artist'
           AND NOT EXISTS (SELECT 1 FROM artists WHERE local_id = NEW.record_local_id))
       OR (NEW.record_kind = 'release'
           AND NOT EXISTS (SELECT 1 FROM releases WHERE local_id = NEW.record_local_id))
       OR (NEW.record_kind = 'event'
           AND NOT EXISTS (SELECT 1 FROM events WHERE local_id = NEW.record_local_id));
END;
CREATE TRIGGER interests_target_insert
BEFORE INSERT ON interests
BEGIN
    SELECT RAISE(ABORT, 'interest target does not exist')
    WHERE NEW.kind NOT IN ('artist', 'release', 'event')
       OR (NEW.kind = 'artist'
           AND NOT EXISTS (SELECT 1 FROM artists WHERE local_id = NEW.target_local_id))
       OR (NEW.kind = 'release'
           AND NOT EXISTS (SELECT 1 FROM releases WHERE local_id = NEW.target_local_id))
       OR (NEW.kind = 'event'
           AND NOT EXISTS (SELECT 1 FROM events WHERE local_id = NEW.target_local_id));
END;
CREATE TRIGGER interests_target_update
BEFORE UPDATE OF kind, target_local_id ON interests
BEGIN
    SELECT RAISE(ABORT, 'interest target does not exist')
    WHERE NEW.kind NOT IN ('artist', 'release', 'event')
       OR (NEW.kind = 'artist'
           AND NOT EXISTS (SELECT 1 FROM artists WHERE local_id = NEW.target_local_id))
       OR (NEW.kind = 'release'
           AND NOT EXISTS (SELECT 1 FROM releases WHERE local_id = NEW.target_local_id))
       OR (NEW.kind = 'event'
           AND NOT EXISTS (SELECT 1 FROM events WHERE local_id = NEW.target_local_id));
END;
CREATE TRIGGER artists_referenced_delete
BEFORE DELETE ON artists
WHEN EXISTS (
    SELECT 1 FROM record_sources WHERE record_kind = 'artist' AND record_local_id = OLD.local_id
) OR EXISTS (
    SELECT 1 FROM interests WHERE kind = 'artist' AND target_local_id = OLD.local_id
)
BEGIN
    SELECT RAISE(ABORT, 'referenced artist cannot be deleted');
END;
CREATE TRIGGER artists_referenced_identity_update
BEFORE UPDATE OF local_id ON artists
WHEN NEW.local_id != OLD.local_id AND (
    EXISTS (
        SELECT 1 FROM record_sources
        WHERE record_kind = 'artist' AND record_local_id = OLD.local_id
    ) OR EXISTS (
        SELECT 1 FROM interests WHERE kind = 'artist' AND target_local_id = OLD.local_id
    )
)
BEGIN
    SELECT RAISE(ABORT, 'referenced artist identity cannot change');
END;
CREATE TRIGGER releases_referenced_delete
BEFORE DELETE ON releases
WHEN EXISTS (
    SELECT 1 FROM record_sources WHERE record_kind = 'release' AND record_local_id = OLD.local_id
) OR EXISTS (
    SELECT 1 FROM interests WHERE kind = 'release' AND target_local_id = OLD.local_id
)
BEGIN
    SELECT RAISE(ABORT, 'referenced release cannot be deleted');
END;
CREATE TRIGGER releases_referenced_identity_update
BEFORE UPDATE OF local_id ON releases
WHEN NEW.local_id != OLD.local_id AND (
    EXISTS (
        SELECT 1 FROM record_sources
        WHERE record_kind = 'release' AND record_local_id = OLD.local_id
    ) OR EXISTS (
        SELECT 1 FROM interests WHERE kind = 'release' AND target_local_id = OLD.local_id
    )
)
BEGIN
    SELECT RAISE(ABORT, 'referenced release identity cannot change');
END;
CREATE TRIGGER events_referenced_delete
BEFORE DELETE ON events
WHEN EXISTS (
    SELECT 1 FROM record_sources WHERE record_kind = 'event' AND record_local_id = OLD.local_id
) OR EXISTS (
    SELECT 1 FROM interests WHERE kind = 'event' AND target_local_id = OLD.local_id
)
BEGIN
    SELECT RAISE(ABORT, 'referenced event cannot be deleted');
END;
CREATE TRIGGER events_referenced_identity_update
BEFORE UPDATE OF local_id ON events
WHEN NEW.local_id != OLD.local_id AND (
    EXISTS (
        SELECT 1 FROM record_sources
        WHERE record_kind = 'event' AND record_local_id = OLD.local_id
    ) OR EXISTS (
        SELECT 1 FROM interests WHERE kind = 'event' AND target_local_id = OLD.local_id
    )
)
BEGIN
    SELECT RAISE(ABORT, 'referenced event identity cannot change');
END;
CREATE INDEX affinity_evidence_artist_order
ON affinity_evidence (artist_local_id, kind, evidence_key, local_id);
CREATE INDEX refresh_runs_recent_order
ON refresh_runs (started_at DESC, local_id);
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
CREATE INDEX inbox_entries_state_order
ON inbox_entries (state, updated_at DESC, local_id);
CREATE INDEX release_discoveries_variant_lookup
ON release_discoveries (source, normalized_title, release_date, release_local_id);
CREATE INDEX event_discoveries_variant_lookup
ON event_discoveries (source, artist_local_id, variant_identity, event_local_id);
CREATE INDEX listening_history_played_at_idx ON listening_history (played_at);
CREATE INDEX listening_history_artist_idx ON listening_history (artist_name, played_at);
CREATE INDEX listening_history_track_idx ON listening_history (track_uri, played_at);
CREATE INDEX release_discoveries_cross_source_variant_lookup
ON release_discoveries (normalized_title, release_date);
