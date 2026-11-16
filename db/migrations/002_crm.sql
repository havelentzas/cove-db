-- migrate:up
-- Personal CRM. Contact data is a JSContact Card (RFC 9553) stored as jsonb;
-- CRM-specific data (tags, relationships, interactions, follow-ups) lives in
-- relational tables and is merged into the card on export.

CREATE SCHEMA crm;

CREATE TABLE crm.contacts (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    card          jsonb NOT NULL CHECK (card->>'@type' = 'Card'),
    version       integer NOT NULL DEFAULT 1,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    archived_at   timestamptz,
    -- derived by trigger from card, for indexing/search only
    display_name  text NOT NULL DEFAULT '',
    emails        text[] NOT NULL DEFAULT '{}',
    phones        text[] NOT NULL DEFAULT '{}',
    search        tsvector
);
CREATE INDEX contacts_search_idx ON crm.contacts USING gin (search);
CREATE INDEX contacts_name_trgm_idx ON crm.contacts USING gin (display_name gin_trgm_ops);
CREATE INDEX contacts_emails_idx ON crm.contacts USING gin (emails);
CREATE INDEX contacts_phones_idx ON crm.contacts USING gin (phones);

CREATE FUNCTION crm.contacts_derive() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    c jsonb := NEW.card;
    name_full text;
    orgs text;
    notes text;
BEGIN
    name_full := nullif(trim(c->'name'->>'full'), '');
    IF name_full IS NULL THEN
        SELECT nullif(trim(string_agg(comp->>'value', ' ' ORDER BY ord)), '')
          INTO name_full
          FROM jsonb_array_elements(coalesce(c->'name'->'components', '[]'::jsonb))
               WITH ORDINALITY AS t(comp, ord)
         WHERE comp->>'kind' NOT IN ('separator', 'title', 'credential');
    END IF;

    NEW.emails := coalesce(ARRAY(
        SELECT lower(v->>'address') FROM jsonb_each(coalesce(c->'emails', '{}'::jsonb)) AS e(k, v)
         WHERE v->>'address' IS NOT NULL), '{}');
    NEW.phones := coalesce(ARRAY(
        SELECT regexp_replace(v->>'number', '[^0-9+]', '', 'g')
          FROM jsonb_each(coalesce(c->'phones', '{}'::jsonb)) AS p(k, v)
         WHERE v->>'number' IS NOT NULL), '{}');

    SELECT string_agg(v->>'name', ' ') INTO orgs
      FROM jsonb_each(coalesce(c->'organizations', '{}'::jsonb)) AS o(k, v);
    SELECT string_agg(v->>'note', ' ') INTO notes
      FROM jsonb_each(coalesce(c->'notes', '{}'::jsonb)) AS n(k, v);

    NEW.display_name := coalesce(name_full, NEW.emails[1], orgs, 'Unnamed contact');
    NEW.search :=
        setweight(to_tsvector('simple', coalesce(NEW.display_name, '')), 'A') ||
        setweight(to_tsvector('simple', coalesce(array_to_string(NEW.emails, ' '), '') || ' ' || coalesce(orgs, '')), 'B') ||
        setweight(to_tsvector('simple', coalesce(notes, '')), 'C');
    IF TG_OP = 'UPDATE' THEN
        NEW.updated_at := now();
    END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER contacts_derive BEFORE INSERT OR UPDATE ON crm.contacts
    FOR EACH ROW EXECUTE FUNCTION crm.contacts_derive();

-- Tags & groups ------------------------------------------------------------
CREATE TABLE crm.tags (
    id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name   text NOT NULL,
    kind   text NOT NULL DEFAULT 'tag' CHECK (kind IN ('tag', 'group')),
    color  text
);
CREATE UNIQUE INDEX tags_name_uq ON crm.tags (lower(name));

CREATE TABLE crm.contact_tags (
    contact_id  uuid   NOT NULL REFERENCES crm.contacts (id) ON DELETE CASCADE,
    tag_id      bigint NOT NULL REFERENCES crm.tags (id) ON DELETE CASCADE,
    PRIMARY KEY (contact_id, tag_id)
);
CREATE INDEX contact_tags_tag_idx ON crm.contact_tags (tag_id);

-- Relationships (directional; type uses JSContact relatedTo vocabulary) --
CREATE TABLE crm.relationships (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    from_id     uuid NOT NULL REFERENCES crm.contacts (id) ON DELETE CASCADE,
    to_id       uuid NOT NULL REFERENCES crm.contacts (id) ON DELETE CASCADE,
    type        text NOT NULL,
    note        text,
    created_at  timestamptz NOT NULL DEFAULT now(),
    CHECK (from_id <> to_id),
    UNIQUE (from_id, to_id, type)
);
CREATE INDEX relationships_to_idx ON crm.relationships (to_id);

-- Interactions --------------------------------------------------------------
CREATE TABLE crm.interactions (
    id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    occurred_at        timestamptz NOT NULL DEFAULT now(),
    kind               text NOT NULL DEFAULT 'other'
                       CHECK (kind IN ('call', 'meeting', 'message', 'email', 'other')),
    summary            text NOT NULL,
    notes              text,
    calendar_event_id  text,
    created_at         timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX interactions_occurred_idx ON crm.interactions (occurred_at DESC);

CREATE TABLE crm.interaction_contacts (
    interaction_id  bigint NOT NULL REFERENCES crm.interactions (id) ON DELETE CASCADE,
    contact_id      uuid   NOT NULL REFERENCES crm.contacts (id) ON DELETE CASCADE,
    PRIMARY KEY (interaction_id, contact_id)
);
CREATE INDEX interaction_contacts_contact_idx ON crm.interaction_contacts (contact_id);

-- Follow-up cadence ---------------------------------------------------------
CREATE TABLE crm.follow_up_rules (
    contact_id     uuid PRIMARY KEY REFERENCES crm.contacts (id) ON DELETE CASCADE,
    every_days     integer NOT NULL CHECK (every_days > 0),
    snoozed_until  timestamptz,
    active         boolean NOT NULL DEFAULT true
);

-- Views ---------------------------------------------------------------------
CREATE VIEW crm.contact_overview AS
SELECT c.id,
       c.display_name,
       c.emails,
       c.phones,
       c.version,
       c.archived_at,
       c.updated_at,
       coalesce((SELECT array_agg(t.name ORDER BY t.name)
                   FROM crm.contact_tags ct JOIN crm.tags t ON t.id = ct.tag_id
                  WHERE ct.contact_id = c.id), '{}') AS tags,
       li.last_contacted,
       f.every_days,
       f.snoozed_until,
       CASE WHEN f.active THEN coalesce(li.last_contacted, c.created_at)
                               + make_interval(days => f.every_days) END AS next_due
  FROM crm.contacts c
  LEFT JOIN LATERAL (
        SELECT max(i.occurred_at) AS last_contacted
          FROM crm.interaction_contacts ic JOIN crm.interactions i ON i.id = ic.interaction_id
         WHERE ic.contact_id = c.id) li ON true
  LEFT JOIN crm.follow_up_rules f ON f.contact_id = c.id;

CREATE VIEW crm.follow_ups_due AS
SELECT *
  FROM crm.contact_overview
 WHERE archived_at IS NULL
   AND next_due IS NOT NULL
   AND next_due <= now()
   AND (snoozed_until IS NULL OR snoozed_until <= now())
 ORDER BY next_due;

-- migrate:down
DROP SCHEMA crm CASCADE;
