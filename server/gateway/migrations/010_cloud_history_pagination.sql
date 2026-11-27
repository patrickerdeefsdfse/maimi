-- Additive pagination index; preserve the already-applied 009 checksum.
CREATE INDEX IF NOT EXISTS hybrid_runs_history_page
 ON platform.hybrid_runs(conversation_id,created_at DESC,id DESC);
