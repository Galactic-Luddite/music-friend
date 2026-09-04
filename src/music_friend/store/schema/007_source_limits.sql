CREATE TABLE source_limits (
    source TEXT PRIMARY KEY,
    state TEXT NOT NULL CHECK (state IN ('available', 'cooling_down', 'quota_exhausted')),
    observed_at TEXT NOT NULL,
    retry_at TEXT,
    retry_is_exact INTEGER NOT NULL CHECK (retry_is_exact IN (0, 1)),
    consecutive_limits INTEGER NOT NULL CHECK (consecutive_limits BETWEEN 0 AND 1000000),
    CHECK (
        (state = 'available' AND retry_at IS NULL AND retry_is_exact = 0
         AND consecutive_limits = 0)
        OR (state = 'cooling_down' AND retry_at IS NOT NULL AND consecutive_limits >= 1)
        OR (state = 'quota_exhausted' AND retry_at IS NULL AND retry_is_exact = 0
            AND consecutive_limits >= 1)
    )
);
