ALTER TABLE refresh_runs RENAME TO refresh_runs_before_history_kind;

CREATE TABLE refresh_runs (
    local_id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('catalog', 'history', 'releases', 'events', 'all')),
    status TEXT NOT NULL CHECK (status IN ('running', 'succeeded', 'partial', 'failed')),
    started_at TEXT NOT NULL,
    finished_at TEXT,
    summary_json TEXT NOT NULL CHECK (length(summary_json) <= 4096),
    CHECK (
        (status = 'running' AND finished_at IS NULL)
        OR (status != 'running' AND finished_at IS NOT NULL)
    )
);

INSERT INTO refresh_runs (
    local_id, source, kind, status, started_at, finished_at, summary_json
)
SELECT local_id, source, kind, status, started_at, finished_at, summary_json
FROM refresh_runs_before_history_kind;

DROP TABLE refresh_runs_before_history_kind;

CREATE INDEX refresh_runs_recent_order
ON refresh_runs (started_at DESC, local_id);
