-- Applied transactionally by init_db; safe to re-run, no production data read.
CREATE TABLE IF NOT EXISTS user_consents (
    event_id UUID NOT NULL,
    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    document TEXT NOT NULL,
    version TEXT NOT NULL,
    document_sha256 TEXT NOT NULL,
    accepted BOOLEAN NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL,
    received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id,event_id)
);
CREATE INDEX IF NOT EXISTS idx_user_consents_user ON user_consents(user_id);
CREATE INDEX IF NOT EXISTS idx_users_retention ON users
    ((COALESCE(token_last_used_at, token_created_at, created_at)));
CREATE INDEX IF NOT EXISTS idx_schedule_retention ON schedule_lessons(lesson_date);

ALTER TABLE user_consents ADD COLUMN IF NOT EXISTS origin TEXT NOT NULL DEFAULT 'user';
