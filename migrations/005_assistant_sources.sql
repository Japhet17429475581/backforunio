-- Public source links and traceable, one-time owner approval of a corpus edition.
ALTER TABLE unio_assistant.documents ADD COLUMN IF NOT EXISTS source_url text;
ALTER TABLE unio_assistant.documents ADD COLUMN IF NOT EXISTS approval_hash text;
ALTER TABLE unio_assistant.documents ADD COLUMN IF NOT EXISTS review_basis text;
