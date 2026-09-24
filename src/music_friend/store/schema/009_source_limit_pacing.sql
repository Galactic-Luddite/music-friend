ALTER TABLE source_limits
    ADD COLUMN window_calls INTEGER NOT NULL DEFAULT 8
    CHECK (window_calls BETWEEN 1 AND 8);
