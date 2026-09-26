CREATE TABLE artist_identity_mappings (
    artist_local_id TEXT NOT NULL,
    source TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('mapped', 'unmapped')),
    method TEXT NOT NULL CHECK (method IN ('url_rel', 'name_search', 'user')),
    attempted_at TEXT NOT NULL,
    PRIMARY KEY (artist_local_id, source),
    FOREIGN KEY (artist_local_id) REFERENCES artists(local_id) ON DELETE CASCADE
);
