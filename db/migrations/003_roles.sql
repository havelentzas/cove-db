-- migrate:up
-- Privileges. The login roles themselves are created by db/init/00-roles.sh
-- (they need passwords from the environment); this migration only grants.
--   cove_app       read/write crm.*, agent notes; append-only audit log
--   cove_readonly  SELECT everything (session is read-only, 5s timeout)

GRANT USAGE ON SCHEMA meta, crm TO cove_app, cove_readonly;

GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA crm TO cove_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA crm TO cove_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA crm GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO cove_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA crm GRANT USAGE, SELECT ON SEQUENCES TO cove_app;

GRANT SELECT ON ALL TABLES IN SCHEMA meta TO cove_app;
GRANT INSERT, UPDATE, DELETE ON meta.agent_notes TO cove_app;
GRANT INSERT ON meta.audit_log TO cove_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA meta TO cove_app;

GRANT SELECT ON ALL TABLES IN SCHEMA meta, crm TO cove_readonly;
ALTER DEFAULT PRIVILEGES IN SCHEMA meta, crm GRANT SELECT ON TABLES TO cove_readonly;

-- migrate:down
REVOKE ALL ON ALL TABLES IN SCHEMA meta, crm FROM cove_app, cove_readonly;
REVOKE USAGE ON SCHEMA meta, crm FROM cove_app, cove_readonly;
