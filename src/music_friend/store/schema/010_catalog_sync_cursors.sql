CREATE TABLE catalog_sync_cursors (
    source TEXT NOT NULL,
    capability TEXT NOT NULL,
    last_successful_at TEXT NOT NULL,
    PRIMARY KEY (source, capability)
);
