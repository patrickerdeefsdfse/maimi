-- Stable browser conversation ids and the first durable chat-turn writer.
-- Existing rows remain valid because the client id is nullable for backfill.
ALTER TABLE platform.conversations
    ADD COLUMN IF NOT EXISTS client_conversation_id VARCHAR(100);

CREATE UNIQUE INDEX IF NOT EXISTS conversations_user_client_id_uq
    ON platform.conversations (user_id, client_conversation_id)
    WHERE client_conversation_id IS NOT NULL;
