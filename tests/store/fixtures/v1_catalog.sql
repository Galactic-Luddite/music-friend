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

INSERT INTO schema_migrations (version, applied_at)
VALUES (1, '2026-08-31T20:00:00+05:45');

INSERT INTO artists (local_id, display_name, identity_confidence, observed_at) VALUES
    ('legacy-artist-1', 'Legacy Artist One', 'user_confirmed', '2026-08-31T23:17:41.123456+05:45'),
    ('legacy-artist-2', 'Legacy Artist Two', 'external_id', '2026-09-01T01:02:03.654321-07:00');

INSERT INTO releases
    (local_id, title, release_type, release_date, date_precision, observed_at)
VALUES
    ('legacy-release', 'Legacy Release', 'album', '2026-08-31', 'day',
     '2026-08-31T23:17:41.123456+05:45');

INSERT INTO release_artists (release_id, artist_id, position) VALUES
    ('legacy-release', 'legacy-artist-2', 0),
    ('legacy-release', 'legacy-artist-1', 1);

INSERT INTO events
    (local_id, title, venue_name, locality, starts_at, time_precision, observed_at)
VALUES
    ('legacy-event', 'Legacy Event', 'Legacy Venue', 'Legacy Locality',
     '2026-09-01T01:02:03.654321-07:00', 'second',
     '2026-08-31T23:17:41.123456+05:45');

INSERT INTO event_artists (event_id, artist_id, position) VALUES
    ('legacy-event', 'legacy-artist-1', 0),
    ('legacy-event', 'legacy-artist-2', 1);

INSERT INTO event_source_links (event_id, source_link, position) VALUES
    ('legacy-event', 'https://example.test/legacy/two', 0),
    ('legacy-event', 'https://example.test/legacy/one', 1);

INSERT INTO source_references (id, source, native_id, canonical_url, observed_at) VALUES
    (10, 'legacy-source-a', 'artist-one', 'https://example.test/legacy/artist-one',
     '2026-08-31T23:17:41.123456+05:45'),
    (11, 'legacy-source-b', 'artist-one', 'https://example.test/legacy/artist-one-b',
     '2026-09-01T01:02:03.654321-07:00'),
    (12, 'legacy-source-a', 'artist-two', 'https://example.test/legacy/artist-two',
     '2026-08-31T23:17:41.123456+05:45'),
    (13, 'legacy-source-a', 'release', 'https://example.test/legacy/release',
     '2026-08-31T23:17:41.123456+05:45'),
    (14, 'legacy-source-a', 'event', 'https://example.test/legacy/event',
     '2026-08-31T23:17:41.123456+05:45');

INSERT INTO record_sources
    (record_kind, record_local_id, source_reference_id, position)
VALUES
    ('artist', 'legacy-artist-1', 10, 0),
    ('artist', 'legacy-artist-1', 11, 1),
    ('artist', 'legacy-artist-2', 12, 0),
    ('release', 'legacy-release', 13, 0),
    ('event', 'legacy-event', 14, 0);

INSERT INTO interests
    (local_id, kind, target_local_id, status, created_by, created_at, updated_at)
VALUES
    ('legacy-interest', 'artist', 'legacy-artist-1', 'active', 'legacy-user',
     '2026-08-31T23:17:41.123456+05:45', '2026-09-01T01:02:03.654321-07:00');

INSERT INTO observations
    (local_id, source, native_id, record_kind, record_local_id, fact_name, observed_at)
VALUES
    ('legacy-observation', 'legacy-source-a', 'artist-one', 'artist',
     'legacy-artist-1', 'tour_status', '2026-09-01T01:02:03.654321-07:00');

INSERT INTO check_times (source, checked_at)
VALUES ('legacy-source-a', '2026-08-31T23:17:41.123456+05:45');
