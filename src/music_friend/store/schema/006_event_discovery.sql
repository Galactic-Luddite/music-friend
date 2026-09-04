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

CREATE INDEX event_discoveries_variant_lookup
ON event_discoveries (source, artist_local_id, variant_identity, event_local_id);
