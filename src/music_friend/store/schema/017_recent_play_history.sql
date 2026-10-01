CREATE TABLE recent_play_observations (
    provider TEXT NOT NULL,
    observation_key TEXT NOT NULL,
    played_at_seconds INTEGER NOT NULL,
    played_at_fraction TEXT NOT NULL,
    track_uri TEXT NOT NULL,
    track_name TEXT NOT NULL,
    primary_artist_name TEXT NOT NULL,
    album_name TEXT NOT NULL,
    context_uri TEXT,
    observed_at TEXT NOT NULL,
    PRIMARY KEY (provider, observation_key)
);

CREATE INDEX recent_play_observations_time
ON recent_play_observations(provider, played_at_seconds, played_at_fraction);

CREATE TABLE history_sync_state (
    provider TEXT PRIMARY KEY,
    last_attempt_at TEXT NOT NULL,
    last_successful_check_at TEXT,
    needs_repair INTEGER NOT NULL CHECK (needs_repair IN (0, 1)),
    newest_played_at_seconds INTEGER,
    newest_played_at_fraction TEXT,
    requested_after_ms INTEGER,
    attempt_outcome TEXT NOT NULL,
    interval_completeness TEXT NOT NULL,
    coverage_reason TEXT NOT NULL
);

CREATE TABLE history_incomplete_intervals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,
    lower_seconds INTEGER,
    lower_fraction TEXT,
    upper_seconds INTEGER,
    upper_fraction TEXT,
    reason TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);

CREATE INDEX history_intervals_provider ON history_incomplete_intervals(provider, id);
