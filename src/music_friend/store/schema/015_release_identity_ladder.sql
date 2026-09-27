-- Issue #63: release identity ladder.
-- A harvested cross-source link is provisional on the release it was harvested onto, not on
-- the shared source reference: the same (source, native_id) can be the linked provider's own,
-- confirmed reference on another release. So provisionality belongs to the mapping row.
ALTER TABLE record_sources ADD COLUMN provisional INTEGER NOT NULL DEFAULT 0
    CHECK (provisional IN (0, 1));

-- A release whose cross-source links have not been harvested yet (resumable across runs).
ALTER TABLE release_discoveries ADD COLUMN link_harvest_pending INTEGER NOT NULL DEFAULT 0
    CHECK (link_harvest_pending IN (0, 1));

-- A late link between two releases that already have different subjects: recorded, never
-- merged automatically. The CLI duplicates merge closes it.
CREATE TABLE release_identity_conflicts (
    observation_local_id TEXT PRIMARY KEY REFERENCES observations(local_id) ON DELETE CASCADE,
    release_local_id TEXT NOT NULL REFERENCES releases(local_id) ON DELETE CASCADE,
    other_release_local_id TEXT NOT NULL REFERENCES releases(local_id) ON DELETE CASCADE,
    opened_at TEXT NOT NULL,
    closed_at TEXT
);
CREATE INDEX release_identity_conflicts_open ON release_identity_conflicts (closed_at);
