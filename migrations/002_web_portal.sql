CREATE TABLE IF NOT EXISTS web_portal_sessions (
    id UUID PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    cookie_cipher BYTEA NOT NULL,
    access_hash TEXT NOT NULL,
    access_expires_at TIMESTAMPTZ NOT NULL,
    refresh_hash TEXT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS web_portal_sessions_user ON web_portal_sessions(user_id);
CREATE INDEX IF NOT EXISTS web_portal_sessions_expiry ON web_portal_sessions(expires_at);
CREATE TABLE IF NOT EXISTS web_portal_refresh_used (
    session_id UUID NOT NULL REFERENCES web_portal_sessions(id) ON DELETE CASCADE,
    token_hash TEXT NOT NULL,
    used_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY(session_id, token_hash)
);
