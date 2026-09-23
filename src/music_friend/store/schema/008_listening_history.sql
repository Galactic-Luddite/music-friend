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

CREATE INDEX listening_history_played_at_idx ON listening_history (played_at);
CREATE INDEX listening_history_artist_idx ON listening_history (artist_name, played_at);
CREATE INDEX listening_history_track_idx ON listening_history (track_uri, played_at);
