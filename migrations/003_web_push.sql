CREATE TABLE IF NOT EXISTS web_push_subscriptions (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    endpoint_hash TEXT UNIQUE NOT NULL,
    subscription_cipher BYTEA NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('group','teacher')),
    owner TEXT NOT NULL,
    status_cipher BYTEA,
    last_status_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS web_push_deliveries (
    subscription_id BIGINT NOT NULL REFERENCES web_push_subscriptions(id) ON DELETE CASCADE,
    event_key TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY(subscription_id,event_key)
);
