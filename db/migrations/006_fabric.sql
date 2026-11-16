-- migrate:up
-- The fabric (docs/fabric.md): which services are installed and turned on in this workspace, and the
-- change-event outbox services publish to and consumers pull from with their own cursor.
-- Nothing here references another schema: services stay independent.

CREATE TABLE meta.services (
    key                text PRIMARY KEY CHECK (key ~ '^[a-z][a-z0-9_]*$'),
    enabled            boolean NOT NULL DEFAULT true,
    installed_version  integer NOT NULL DEFAULT 1,
    settings           jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(settings) = 'object'),
    updated_at         timestamptz NOT NULL DEFAULT now()
);

-- Outbox: small, body-free change notices. A row exists exactly when the change it describes committed.
CREATE TABLE meta.events (
    seq          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    service      text NOT NULL,
    type         text NOT NULL CHECK (type ~ '^[a-z]+(\.[a-z_]+)+$'),
    ref          text,
    occurred_at  timestamptz NOT NULL DEFAULT now(),
    payload      jsonb NOT NULL DEFAULT '{}' CHECK (pg_column_size(payload) < 2048)
);
CREATE INDEX events_type_seq_idx ON meta.events (type, seq);
CREATE INDEX events_occurred_idx ON meta.events (occurred_at);

CREATE TABLE meta.event_cursors (
    consumer    text PRIMARY KEY,
    last_seq    bigint NOT NULL DEFAULT 0,
    updated_at  timestamptz NOT NULL DEFAULT now(),
    last_error  text
);

INSERT INTO meta.services (key) VALUES ('people'), ('notes');

GRANT SELECT, UPDATE ON meta.services TO cove_app;
GRANT SELECT, INSERT, DELETE ON meta.events TO cove_app;
GRANT SELECT, INSERT, UPDATE ON meta.event_cursors TO cove_app;
GRANT USAGE, SELECT ON SEQUENCE meta.events_seq_seq TO cove_app;
GRANT SELECT ON meta.services TO cove_readonly;
-- Events can describe message activity (refs and times), so they stay out of ad-hoc read-only SQL:
-- system:sql must not get around messages:read.
REVOKE ALL ON meta.events, meta.event_cursors FROM cove_readonly;

INSERT INTO meta.changelog (migration, summary) VALUES
  ('006_fabric', 'Added the fabric: meta.services (per-workspace enablement), meta.events outbox and meta.event_cursors.');

-- migrate:down
DROP TABLE meta.event_cursors, meta.events, meta.services;
DELETE FROM meta.changelog WHERE migration = '006_fabric';
