CREATE TABLE release_check_continuations (
    source TEXT NOT NULL,
    artist_local_id TEXT NOT NULL REFERENCES artists(local_id),
    cursor TEXT NOT NULL CHECK (length(cursor) <= 512),
    since TEXT NOT NULL,
    PRIMARY KEY (source, artist_local_id)
);
