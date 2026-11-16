-- migrate:up
-- Shared "meta" schema: the system's description of itself, the managing
-- agent's persistent notes, and an append-only audit trail of every change.

CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE SCHEMA meta;

CREATE TABLE meta.domains (
    name         text PRIMARY KEY,
    description  text NOT NULL,
    db_schema    text NOT NULL,
    conventions  text,
    standard     text,
    added_at     timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE meta.agent_notes (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    topic       text NOT NULL,
    note        text NOT NULL,
    author      text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX agent_notes_topic_idx ON meta.agent_notes (topic);

CREATE TABLE meta.changelog (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    migration   text NOT NULL,
    summary     text NOT NULL,
    applied_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE meta.audit_log (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    at         timestamptz NOT NULL DEFAULT now(),
    actor      text NOT NULL,          -- 'web' | 'mcp:<client>' | 'import' ...
    entity     text NOT NULL,          -- e.g. 'crm.contact'
    entity_id  text NOT NULL,
    action     text NOT NULL,          -- create | update | archive | ...
    diff       jsonb
);
CREATE INDEX audit_log_entity_idx ON meta.audit_log (entity, entity_id, at DESC);

-- migrate:down
DROP SCHEMA meta CASCADE;
