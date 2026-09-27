-- Issue #70: a discovered release whose content is committed but whose signal and inbox entry
-- have not been recorded yet. Release discovery sets it in the same transaction that commits
-- the release content; the single write path clears it in the same transaction that records
-- the signal and repoints the inbox entry. A run that dies in between leaves it set, and the
-- next refresh's repair pass completes the observation.
ALTER TABLE release_discoveries ADD COLUMN observation_pending INTEGER NOT NULL DEFAULT 0
    CHECK (observation_pending IN (0, 1));
CREATE INDEX release_discoveries_observation_pending
    ON release_discoveries (observation_pending, first_seen_at, release_local_id);
