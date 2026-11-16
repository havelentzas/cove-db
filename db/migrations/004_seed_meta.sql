-- migrate:up
INSERT INTO meta.domains (name, description, db_schema, standard, conventions) VALUES (
  'crm',
  'Personal CRM: people I know, how I know them, when we last talked, and who I should reach out to.',
  'crm',
  'JSContact Card (RFC 9553) in crm.contacts.card; vCard 4.0 import/export via RFC 9555 mapping.',
  'Contact data lives ONLY in the JSContact card (crm.contacts.card). Tags, relationships, '
  'interactions and follow-up cadences live in their own tables and are merged into keywords/relatedTo '
  'on export. App-specific card extensions use the vendor prefix "cove.local:". Edits use optimistic '
  'locking: pass the contact''s current version. Interactions can involve several contacts. '
  'Follow-up due = (last interaction, or creation date) + every_days. Timestamps are timestamptz (UTC).'
);

INSERT INTO meta.changelog (migration, summary) VALUES
  ('001_meta', 'Created meta schema: domains, agent_notes, changelog, audit_log.'),
  ('002_crm', 'Created crm schema: contacts (JSContact jsonb), tags, relationships, interactions, follow_up_rules, overview views.'),
  ('003_roles', 'Granted cove_app read/write and cove_readonly read-only access.'),
  ('004_seed_meta', 'Registered the crm domain.');

-- migrate:down
DELETE FROM meta.changelog WHERE migration IN ('001_meta', '002_crm', '003_roles', '004_seed_meta');
DELETE FROM meta.domains WHERE name = 'crm';
