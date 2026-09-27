-- 1. Subjects: one row per real-world release or event. Releases map many-to-one.
CREATE TABLE inbox_subjects (
    local_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('release', 'event')),
    created_at TEXT NOT NULL
);
ALTER TABLE releases ADD COLUMN subject_local_id TEXT REFERENCES inbox_subjects(local_id);
-- Backfill: every existing release is its own subject (subject id = release id).
INSERT INTO inbox_subjects (local_id, kind, created_at)
    SELECT local_id, 'release', observed_at FROM releases;
UPDATE releases SET subject_local_id = local_id;
-- Events are 1:1 with their subject; the subject id is the event id (no column needed).
INSERT INTO inbox_subjects (local_id, kind, created_at)
    SELECT local_id, 'event', observed_at FROM events;

-- 2. Snapshots first, so every collapse below is reversible.
CREATE TABLE inbox_entry_snapshots (      -- every row touched by a merge, as it was
    snapshot_id INTEGER PRIMARY KEY,
    merge_id TEXT NOT NULL,               -- '014' for the migration, else the CLI op id
    role TEXT NOT NULL CHECK (role IN ('winner_before', 'loser')),
    local_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    subject_local_id TEXT NOT NULL,
    signal_local_id TEXT NOT NULL,
    state TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    winner_local_id TEXT NOT NULL,
    winner_updated_at_after TEXT NOT NULL,   -- CAS token for unmerge (section 3.6)
    merged_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE subject_merges (             -- release re-pointing, for the CLI merge only
    merge_id TEXT NOT NULL,
    release_local_id TEXT NOT NULL,
    from_subject_local_id TEXT NOT NULL,
    to_subject_local_id TEXT NOT NULL,
    merged_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (merge_id, release_local_id)
);

-- 3. Rebuild inbox_entries with subject identity (SQLite cannot ADD a UNIQUE constraint).
CREATE TABLE inbox_entries_v2 (
    local_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('release', 'event')),
    subject_local_id TEXT NOT NULL REFERENCES inbox_subjects(local_id),
    latest_signal_local_id TEXT NOT NULL REFERENCES signals(local_id) ON DELETE CASCADE,
    state TEXT NOT NULL CHECK (state IN ('unread', 'saved', 'dismissed')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (kind, subject_local_id)
);

-- 4. Collapse: one winner per (kind, subject). Rank: decided beats unread; among decided,
--    the most recently updated wins; ties break on local_id. The winner keeps its own
--    local_id and state; created_at = earliest in the group; updated_at = latest in the
--    group; latest_signal = the group's signal with the greatest observed_at (tie: local_id).
-- Materialized into a temp table (rather than a CTE per statement) because both statements
-- below need to share it, and a WITH clause's scope is one statement.
CREATE TEMP TABLE _inbox_014_ranked AS
    SELECT e.local_id, s.kind,
           CASE s.kind WHEN 'release' THEN r.subject_local_id ELSE s.record_local_id END
               AS subject_local_id,
           e.signal_local_id, e.state, e.created_at, e.updated_at, s.observed_at,
           ROW_NUMBER() OVER (PARTITION BY s.kind, s.record_local_id
               ORDER BY CASE e.state WHEN 'unread' THEN 1 ELSE 0 END,
                        e.updated_at DESC, e.local_id) AS rank,
           MIN(e.created_at) OVER (PARTITION BY s.kind, s.record_local_id) AS first_created,
           MAX(e.updated_at) OVER (PARTITION BY s.kind, s.record_local_id) AS last_updated,
           FIRST_VALUE(e.signal_local_id) OVER (PARTITION BY s.kind, s.record_local_id
               ORDER BY s.observed_at DESC, s.local_id) AS head_signal
    FROM inbox_entries AS e
    JOIN signals AS s ON s.local_id = e.signal_local_id
    LEFT JOIN releases AS r ON r.local_id = s.record_local_id;

INSERT INTO inbox_entries_v2
SELECT local_id, kind, subject_local_id, head_signal, state, first_created, last_updated
FROM _inbox_014_ranked WHERE rank = 1;
-- Snapshots: every loser, and every winner whose row changed (rank = 1 in a group of > 1).
INSERT INTO inbox_entry_snapshots (merge_id, role, local_id, kind, subject_local_id,
    signal_local_id, state, created_at, updated_at, winner_local_id, winner_updated_at_after)
SELECT '014', CASE WHEN l.rank = 1 THEN 'winner_before' ELSE 'loser' END, l.local_id, l.kind,
       l.subject_local_id, l.signal_local_id, l.state, l.created_at, l.updated_at,
       w.local_id, w.last_updated
FROM _inbox_014_ranked AS l JOIN _inbox_014_ranked AS w
  ON w.kind = l.kind AND w.subject_local_id = l.subject_local_id AND w.rank = 1
WHERE EXISTS (SELECT 1 FROM _inbox_014_ranked AS o
              WHERE o.kind = l.kind AND o.subject_local_id = l.subject_local_id AND o.rank > 1);

DROP TABLE _inbox_014_ranked;

DROP TABLE inbox_entries;
ALTER TABLE inbox_entries_v2 RENAME TO inbox_entries;
CREATE INDEX inbox_entries_state_order ON inbox_entries (state, updated_at DESC, local_id);
CREATE INDEX releases_subject ON releases (subject_local_id);
