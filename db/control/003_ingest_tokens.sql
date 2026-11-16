-- migrate:up
-- Ingest tokens (cove_ing_…): write-only tokens for message collectors. They carry only messages:ingest
-- and are accepted only on /api/v1/messages/ingest/*, so a stolen collector token can't read anything back.
ALTER TABLE control.api_tokens DROP CONSTRAINT IF EXISTS api_tokens_kind_check;
ALTER TABLE control.api_tokens ADD CONSTRAINT api_tokens_kind_check
    CHECK (kind IN ('device', 'personal', 'oauth', 'ingest'));

-- migrate:down
ALTER TABLE control.api_tokens DROP CONSTRAINT api_tokens_kind_check;
ALTER TABLE control.api_tokens ADD CONSTRAINT api_tokens_kind_check CHECK (kind IN ('device', 'personal', 'oauth'));
