CREATE TEMP TABLE migration_002_integrity_guard (
    valid INTEGER NOT NULL CHECK (valid = 1)
);

INSERT INTO migration_002_integrity_guard (valid)
SELECT CASE WHEN NOT EXISTS (
    SELECT 1
    FROM record_sources AS mapping
    WHERE mapping.record_kind NOT IN ('artist', 'release', 'event')
       OR (mapping.record_kind = 'artist' AND NOT EXISTS (
            SELECT 1 FROM artists WHERE local_id = mapping.record_local_id
       ))
       OR (mapping.record_kind = 'release' AND NOT EXISTS (
            SELECT 1 FROM releases WHERE local_id = mapping.record_local_id
       ))
       OR (mapping.record_kind = 'event' AND NOT EXISTS (
            SELECT 1 FROM events WHERE local_id = mapping.record_local_id
       ))
       OR NOT EXISTS (
            SELECT 1 FROM source_references
            WHERE id = mapping.source_reference_id
       )
) AND NOT EXISTS (
    SELECT 1
    FROM interests AS interest
    WHERE interest.kind NOT IN ('artist', 'release', 'event')
       OR (interest.kind = 'artist' AND NOT EXISTS (
            SELECT 1 FROM artists WHERE local_id = interest.target_local_id
       ))
       OR (interest.kind = 'release' AND NOT EXISTS (
            SELECT 1 FROM releases WHERE local_id = interest.target_local_id
       ))
       OR (interest.kind = 'event' AND NOT EXISTS (
            SELECT 1 FROM events WHERE local_id = interest.target_local_id
       ))
) AND NOT EXISTS (
    SELECT 1
    FROM observations AS observation
    WHERE observation.record_kind NOT IN ('artist', 'release', 'event')
       OR NOT EXISTS (
            SELECT 1
            FROM source_references AS reference
            JOIN record_sources AS mapping
              ON mapping.source_reference_id = reference.id
             AND mapping.record_kind = observation.record_kind
             AND mapping.record_local_id = observation.record_local_id
            WHERE reference.source = observation.source
              AND reference.native_id = observation.native_id
       )
) THEN 1 ELSE 0 END;

DROP TABLE migration_002_integrity_guard;

ALTER TABLE observations RENAME TO observations_v1;

CREATE TABLE observations (
    local_id TEXT PRIMARY KEY,
    source_reference_id INTEGER NOT NULL,
    source TEXT NOT NULL,
    native_id TEXT NOT NULL,
    record_kind TEXT NOT NULL,
    record_local_id TEXT NOT NULL,
    fact_name TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    FOREIGN KEY (source, native_id)
        REFERENCES source_references(source, native_id) ON DELETE CASCADE,
    FOREIGN KEY (record_kind, record_local_id, source_reference_id)
        REFERENCES record_sources(record_kind, record_local_id, source_reference_id)
        ON DELETE CASCADE
);

INSERT INTO observations
    (local_id, source_reference_id, source, native_id, record_kind,
     record_local_id, fact_name, observed_at)
SELECT
    observation.local_id,
    (
        SELECT reference.id
        FROM source_references AS reference
        JOIN record_sources AS mapping
          ON mapping.source_reference_id = reference.id
         AND mapping.record_kind = observation.record_kind
         AND mapping.record_local_id = observation.record_local_id
        WHERE reference.source = observation.source
          AND reference.native_id = observation.native_id
    ),
    observation.source,
    observation.native_id,
    observation.record_kind,
    observation.record_local_id,
    observation.fact_name,
    observation.observed_at
FROM observations_v1 AS observation;

DROP TABLE observations_v1;

CREATE TRIGGER observations_source_identity_insert
BEFORE INSERT ON observations
BEGIN
    SELECT RAISE(ABORT, 'observation source identity does not match')
    WHERE NOT EXISTS (
        SELECT 1 FROM source_references
        WHERE id = NEW.source_reference_id
          AND source = NEW.source
          AND native_id = NEW.native_id
    );
END;

CREATE TRIGGER observations_source_identity_update
BEFORE UPDATE OF source_reference_id, source, native_id ON observations
BEGIN
    SELECT RAISE(ABORT, 'observation source identity does not match')
    WHERE NOT EXISTS (
        SELECT 1 FROM source_references
        WHERE id = NEW.source_reference_id
          AND source = NEW.source
          AND native_id = NEW.native_id
    );
END;

CREATE TRIGGER record_sources_target_insert
BEFORE INSERT ON record_sources
BEGIN
    SELECT RAISE(ABORT, 'record source target does not exist')
    WHERE NEW.record_kind NOT IN ('artist', 'release', 'event')
       OR (NEW.record_kind = 'artist'
           AND NOT EXISTS (SELECT 1 FROM artists WHERE local_id = NEW.record_local_id))
       OR (NEW.record_kind = 'release'
           AND NOT EXISTS (SELECT 1 FROM releases WHERE local_id = NEW.record_local_id))
       OR (NEW.record_kind = 'event'
           AND NOT EXISTS (SELECT 1 FROM events WHERE local_id = NEW.record_local_id));
END;

CREATE TRIGGER record_sources_target_update
BEFORE UPDATE OF record_kind, record_local_id ON record_sources
BEGIN
    SELECT RAISE(ABORT, 'record source target does not exist')
    WHERE NEW.record_kind NOT IN ('artist', 'release', 'event')
       OR (NEW.record_kind = 'artist'
           AND NOT EXISTS (SELECT 1 FROM artists WHERE local_id = NEW.record_local_id))
       OR (NEW.record_kind = 'release'
           AND NOT EXISTS (SELECT 1 FROM releases WHERE local_id = NEW.record_local_id))
       OR (NEW.record_kind = 'event'
           AND NOT EXISTS (SELECT 1 FROM events WHERE local_id = NEW.record_local_id));
END;

CREATE TRIGGER interests_target_insert
BEFORE INSERT ON interests
BEGIN
    SELECT RAISE(ABORT, 'interest target does not exist')
    WHERE NEW.kind NOT IN ('artist', 'release', 'event')
       OR (NEW.kind = 'artist'
           AND NOT EXISTS (SELECT 1 FROM artists WHERE local_id = NEW.target_local_id))
       OR (NEW.kind = 'release'
           AND NOT EXISTS (SELECT 1 FROM releases WHERE local_id = NEW.target_local_id))
       OR (NEW.kind = 'event'
           AND NOT EXISTS (SELECT 1 FROM events WHERE local_id = NEW.target_local_id));
END;

CREATE TRIGGER interests_target_update
BEFORE UPDATE OF kind, target_local_id ON interests
BEGIN
    SELECT RAISE(ABORT, 'interest target does not exist')
    WHERE NEW.kind NOT IN ('artist', 'release', 'event')
       OR (NEW.kind = 'artist'
           AND NOT EXISTS (SELECT 1 FROM artists WHERE local_id = NEW.target_local_id))
       OR (NEW.kind = 'release'
           AND NOT EXISTS (SELECT 1 FROM releases WHERE local_id = NEW.target_local_id))
       OR (NEW.kind = 'event'
           AND NOT EXISTS (SELECT 1 FROM events WHERE local_id = NEW.target_local_id));
END;

CREATE TRIGGER artists_referenced_delete
BEFORE DELETE ON artists
WHEN EXISTS (
    SELECT 1 FROM record_sources WHERE record_kind = 'artist' AND record_local_id = OLD.local_id
) OR EXISTS (
    SELECT 1 FROM interests WHERE kind = 'artist' AND target_local_id = OLD.local_id
)
BEGIN
    SELECT RAISE(ABORT, 'referenced artist cannot be deleted');
END;

CREATE TRIGGER artists_referenced_identity_update
BEFORE UPDATE OF local_id ON artists
WHEN NEW.local_id != OLD.local_id AND (
    EXISTS (
        SELECT 1 FROM record_sources
        WHERE record_kind = 'artist' AND record_local_id = OLD.local_id
    ) OR EXISTS (
        SELECT 1 FROM interests WHERE kind = 'artist' AND target_local_id = OLD.local_id
    )
)
BEGIN
    SELECT RAISE(ABORT, 'referenced artist identity cannot change');
END;

CREATE TRIGGER releases_referenced_delete
BEFORE DELETE ON releases
WHEN EXISTS (
    SELECT 1 FROM record_sources WHERE record_kind = 'release' AND record_local_id = OLD.local_id
) OR EXISTS (
    SELECT 1 FROM interests WHERE kind = 'release' AND target_local_id = OLD.local_id
)
BEGIN
    SELECT RAISE(ABORT, 'referenced release cannot be deleted');
END;

CREATE TRIGGER releases_referenced_identity_update
BEFORE UPDATE OF local_id ON releases
WHEN NEW.local_id != OLD.local_id AND (
    EXISTS (
        SELECT 1 FROM record_sources
        WHERE record_kind = 'release' AND record_local_id = OLD.local_id
    ) OR EXISTS (
        SELECT 1 FROM interests WHERE kind = 'release' AND target_local_id = OLD.local_id
    )
)
BEGIN
    SELECT RAISE(ABORT, 'referenced release identity cannot change');
END;

CREATE TRIGGER events_referenced_delete
BEFORE DELETE ON events
WHEN EXISTS (
    SELECT 1 FROM record_sources WHERE record_kind = 'event' AND record_local_id = OLD.local_id
) OR EXISTS (
    SELECT 1 FROM interests WHERE kind = 'event' AND target_local_id = OLD.local_id
)
BEGIN
    SELECT RAISE(ABORT, 'referenced event cannot be deleted');
END;

CREATE TRIGGER events_referenced_identity_update
BEFORE UPDATE OF local_id ON events
WHEN NEW.local_id != OLD.local_id AND (
    EXISTS (
        SELECT 1 FROM record_sources
        WHERE record_kind = 'event' AND record_local_id = OLD.local_id
    ) OR EXISTS (
        SELECT 1 FROM interests WHERE kind = 'event' AND target_local_id = OLD.local_id
    )
)
BEGIN
    SELECT RAISE(ABORT, 'referenced event identity cannot change');
END;
