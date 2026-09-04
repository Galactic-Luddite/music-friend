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

CREATE INDEX release_discoveries_variant_lookup
ON release_discoveries (source, normalized_title, release_date, release_local_id);
