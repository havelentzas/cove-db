-- Workspaces: each one is its own database (cove_ws_<id>) with its own pair of roles (ws_<id>, ws_<id>_ro).
-- Creating databases and roles is privileged, so it happens in `python -m cove.control.workspaces`
-- (run as the owner), never from the web app.

-- migrate:up
CREATE TABLE control.workspaces (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  slug          text NOT NULL UNIQUE CHECK (slug ~ '^[a-z][a-z0-9-]{1,30}$'),
  name          text NOT NULL,
  db_name       text NOT NULL UNIQUE,
  role_name     text NOT NULL UNIQUE,
  status        text NOT NULL DEFAULT 'provisioning'
                CHECK (status IN ('provisioning', 'active', 'needs_attention', 'archived')),
  status_detail text,
  schema_version text,                         -- newest migration applied
  created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE control.memberships (
  workspace_id uuid NOT NULL REFERENCES control.workspaces ON DELETE CASCADE,
  user_id      uuid NOT NULL REFERENCES control.users ON DELETE CASCADE,
  role         text NOT NULL CHECK (role IN ('owner', 'editor', 'viewer')),
  created_at   timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (workspace_id, user_id)
);

-- Every API token is bound to exactly one workspace.
CREATE TABLE control.api_tokens (
  id           bigserial PRIMARY KEY,
  workspace_id uuid NOT NULL REFERENCES control.workspaces ON DELETE CASCADE,
  user_id      uuid REFERENCES control.users ON DELETE CASCADE,   -- null: created before sign-in existed
  kind         text NOT NULL CHECK (kind IN ('device', 'personal', 'oauth')),
  name         text NOT NULL,
  token_hash   text NOT NULL UNIQUE,
  scopes       text[] NOT NULL,
  created_at   timestamptz NOT NULL DEFAULT now(),
  last_used_at timestamptz,
  expires_at   timestamptz,
  revoked_at   timestamptz
);
CREATE INDEX api_tokens_workspace ON control.api_tokens (workspace_id);

ALTER TABLE control.sessions ADD COLUMN workspace_id uuid REFERENCES control.workspaces ON DELETE SET NULL;

GRANT SELECT ON control.workspaces TO cove_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON control.memberships TO cove_app;
GRANT SELECT, INSERT, UPDATE ON control.api_tokens TO cove_app;
GRANT USAGE ON SEQUENCE control.api_tokens_id_seq TO cove_app;

-- migrate:down
ALTER TABLE control.sessions DROP COLUMN workspace_id;
DROP TABLE control.api_tokens, control.memberships, control.workspaces;
