-- Additive, deliberately uses standard PostgreSQL; no extension or user data rewrite.
CREATE SCHEMA IF NOT EXISTS unio_assistant;
CREATE TABLE IF NOT EXISTS unio_assistant.documents (
 id text PRIMARY KEY, file_name text NOT NULL UNIQUE, title text NOT NULL,
 content_hash text NOT NULL, reviewed boolean NOT NULL DEFAULT false,
 active boolean NOT NULL DEFAULT false, reviewed_at timestamptz,
 effective_from date, effective_to date,
 CHECK (NOT active OR reviewed)
);
CREATE TABLE IF NOT EXISTS unio_assistant.chunks (
 id text PRIMARY KEY, document_id text NOT NULL REFERENCES unio_assistant.documents(id) ON DELETE CASCADE,
 ordinal integer NOT NULL, page integer, locator text NOT NULL, text text NOT NULL,
 ocr boolean NOT NULL DEFAULT false,
 fts tsvector GENERATED ALWAYS AS (to_tsvector('russian', text)) STORED,
 UNIQUE(document_id, ordinal)
);
CREATE INDEX IF NOT EXISTS assistant_chunks_fts ON unio_assistant.chunks USING gin(fts);
CREATE TABLE IF NOT EXISTS unio_assistant.daily_usage (
 user_id bigint NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 day date NOT NULL, calls integer NOT NULL DEFAULT 0,
 PRIMARY KEY(user_id,day)
);
CREATE TABLE IF NOT EXISTS unio_assistant.budget (
 period text PRIMARY KEY, reserved_tokens bigint NOT NULL DEFAULT 0
);
