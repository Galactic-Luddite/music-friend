ALTER TABLE source_references ADD COLUMN confidence TEXT NOT NULL DEFAULT 'source_only' CHECK (confidence IN ('source_only', 'external_id', 'user_confirmed'));
