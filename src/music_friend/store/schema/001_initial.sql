CREATE TABLE schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE artists (
    local_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    identity_confidence TEXT NOT NULL,
    observed_at TEXT NOT NULL
);

CREATE TABLE releases (
    local_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    release_type TEXT NOT NULL,
    release_date TEXT NOT NULL,
    date_precision TEXT NOT NULL,
    observed_at TEXT NOT NULL
);

CREATE TABLE release_artists (
    release_id TEXT NOT NULL REFERENCES releases(local_id) ON DELETE CASCADE,
    artist_id TEXT NOT NULL REFERENCES artists(local_id),
    position INTEGER NOT NULL,
    PRIMARY KEY (release_id, position),
    UNIQUE (release_id, artist_id)
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

CREATE TABLE event_artists (
    event_id TEXT NOT NULL REFERENCES events(local_id) ON DELETE CASCADE,
    artist_id TEXT NOT NULL REFERENCES artists(local_id),
    position INTEGER NOT NULL,
    PRIMARY KEY (event_id, position),
    UNIQUE (event_id, artist_id)
);

CREATE TABLE event_source_links (
    event_id TEXT NOT NULL REFERENCES events(local_id) ON DELETE CASCADE,
    source_link TEXT NOT NULL,
    position INTEGER NOT NULL,
    PRIMARY KEY (event_id, position),
    UNIQUE (event_id, source_link)
);

CREATE TABLE source_references (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    native_id TEXT NOT NULL,
    canonical_url TEXT,
    observed_at TEXT NOT NULL,
    UNIQUE (source, native_id)
);

CREATE TABLE record_sources (
    record_kind TEXT NOT NULL,
    record_local_id TEXT NOT NULL,
    source_reference_id INTEGER NOT NULL REFERENCES source_references(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    PRIMARY KEY (record_kind, record_local_id, position),
    UNIQUE (record_kind, record_local_id, source_reference_id)
);

CREATE TABLE interests (
    local_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    target_local_id TEXT NOT NULL,
    status TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE observations (
    local_id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    native_id TEXT NOT NULL,
    record_kind TEXT NOT NULL,
    record_local_id TEXT NOT NULL,
    fact_name TEXT NOT NULL,
    observed_at TEXT NOT NULL
);

CREATE TABLE check_times (
    source TEXT PRIMARY KEY,
    checked_at TEXT NOT NULL
);
