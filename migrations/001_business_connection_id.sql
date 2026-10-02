-- Hotfix for existing Supabase databases created before business_connection_id.
-- Safe to run multiple times in Supabase SQL Editor.
BEGIN;

ALTER TABLE events
    ADD COLUMN IF NOT EXISTS sender_id BIGINT;

ALTER TABLE events
    ADD COLUMN IF NOT EXISTS business_connection_id TEXT;

CREATE INDEX IF NOT EXISTS idx_events_connection_message
    ON events (business_connection_id, chat_id, message_id);

COMMIT;
