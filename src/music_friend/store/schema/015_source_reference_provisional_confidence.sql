ALTER TABLE source_references RENAME TO temp_source_references;
CREATE TABLE source_references (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    native_id TEXT NOT NULL,
    canonical_url TEXT,
    observed_at TEXT NOT NULL,
    confidence TEXT NOT NULL DEFAULT 'source_only' CHECK (confidence IN ('source_only', 'external_id', 'user_confirmed', 'provisional')),
    UNIQUE (source, native_id)
);
INSERT INTO source_references SELECT * FROM temp_source_references;
DROP TABLE temp_source_references;
