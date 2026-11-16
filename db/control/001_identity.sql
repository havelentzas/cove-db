-- cove_control: who can sign in, their sessions, and security events.
-- Lives in its own database so workspace data (Phase 3) and identity never share tables.

-- migrate:up
CREATE SCHEMA control;

CREATE TABLE control.users (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  username      text NOT NULL,
  email         text,
  display_name  text,
  is_admin      boolean NOT NULL DEFAULT false,
  created_at    timestamptz NOT NULL DEFAULT now(),
  last_login_at timestamptz,
  disabled_at   timestamptz
);

-- One row per (identity provider, subject). Users are matched on this, never on email.
CREATE TABLE control.identities (
  issuer     text NOT NULL,
  subject    text NOT NULL,
  user_id    uuid NOT NULL REFERENCES control.users ON DELETE CASCADE,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (issuer, subject)
);

-- Server-side sessions. The cookie holds a random token; only its HMAC is stored.
CREATE TABLE control.sessions (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  token_hash   text NOT NULL UNIQUE,
  user_id      uuid NOT NULL REFERENCES control.users ON DELETE CASCADE,
  created_at   timestamptz NOT NULL DEFAULT now(),
  last_seen_at timestamptz NOT NULL DEFAULT now(),
  expires_at   timestamptz NOT NULL,          -- absolute limit; idle limit is checked against last_seen_at
  ip           inet,
  user_agent   text,
  revoked_at   timestamptz
);
CREATE INDEX sessions_user_active ON control.sessions (user_id) WHERE revoked_at IS NULL;

-- Append-only: the app role can insert and read, never change or delete.
CREATE TABLE control.security_events (
  id         bigserial PRIMARY KEY,
  at         timestamptz NOT NULL DEFAULT now(),
  kind       text NOT NULL,
  user_id    uuid REFERENCES control.users ON DELETE SET NULL,
  session_id uuid,
  ip         inet,
  user_agent text,
  detail     jsonb NOT NULL DEFAULT '{}'
);
CREATE INDEX security_events_at ON control.security_events (at DESC);
CREATE INDEX security_events_user ON control.security_events (user_id, at DESC);

GRANT USAGE ON SCHEMA control TO cove_app;
GRANT SELECT, INSERT, UPDATE ON control.users, control.identities, control.sessions TO cove_app;
GRANT SELECT, INSERT ON control.security_events TO cove_app;
GRANT USAGE ON SEQUENCE control.security_events_id_seq TO cove_app;

-- migrate:down
DROP SCHEMA control CASCADE;
