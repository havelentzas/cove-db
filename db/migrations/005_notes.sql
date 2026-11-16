-- migrate:up
-- Notes: a mirror of an Obsidian vault that can sync both ways.
-- Every file in the vault (markdown note, canvas, attachment) is a row in notes.files.
-- Raw content is stored byte-for-byte; title/frontmatter/tags/links are derived from it.
-- Every write bumps a per-file version (kept in notes.versions for 3-way merges) and takes
-- a value from a global sequence, which is the change feed clients poll.

CREATE SCHEMA notes;

CREATE SEQUENCE notes.change_seq;

CREATE TABLE notes.blobs (
    sha256      text PRIMARY KEY CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    size        bigint NOT NULL,
    mime        text,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE notes.files (
    id            uuid PRIMARY KEY,
    kind          text NOT NULL CHECK (kind IN ('note', 'canvas', 'attachment')),
    path          text NOT NULL CHECK (path !~ '(^/|\.\./|^\.\.$|\\)' AND length(path) BETWEEN 1 AND 1024),
    body          text,                                  -- note / canvas text (UTF-8)
    blob_sha256   text REFERENCES notes.blobs (sha256),  -- attachment bytes
    content_hash  text NOT NULL,                          -- sha256 of the content bytes
    size          bigint NOT NULL DEFAULT 0,
    version       integer NOT NULL DEFAULT 1,
    seq           bigint NOT NULL DEFAULT nextval('notes.change_seq'),
    -- derived (set by the service on write)
    title         text NOT NULL DEFAULT '',
    frontmatter   jsonb NOT NULL DEFAULT '{}',
    tags          text[] NOT NULL DEFAULT '{}',
    aliases       text[] NOT NULL DEFAULT '{}',
    search        tsvector GENERATED ALWAYS AS (
                    setweight(to_tsvector('simple', coalesce(title, '')), 'A') ||
                    setweight(to_tsvector('simple', coalesce(body, '')), 'C')) STORED,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    updated_by    text NOT NULL DEFAULT '',
    deleted_at    timestamptz,
    CHECK ((kind = 'attachment') = (blob_sha256 IS NOT NULL))
);
CREATE UNIQUE INDEX files_live_path_uq ON notes.files (lower(path)) WHERE deleted_at IS NULL;
CREATE INDEX files_seq_idx ON notes.files (seq);
CREATE INDEX files_search_idx ON notes.files USING gin (search);
CREATE INDEX files_path_trgm_idx ON notes.files USING gin (path gin_trgm_ops);
CREATE INDEX files_tags_idx ON notes.files USING gin (tags);
CREATE INDEX files_updated_idx ON notes.files (updated_at DESC) WHERE deleted_at IS NULL;

CREATE FUNCTION notes.files_touch() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    NEW.seq := nextval('notes.change_seq');
    NEW.updated_at := now();
    RETURN NEW;
END $$;
CREATE TRIGGER files_touch BEFORE UPDATE ON notes.files FOR EACH ROW EXECUTE FUNCTION notes.files_touch();

-- Full history: the base for 3-way merges, and an undo trail.
CREATE TABLE notes.versions (
    file_id       uuid NOT NULL REFERENCES notes.files (id) ON DELETE CASCADE,
    version       integer NOT NULL,
    path          text NOT NULL,
    body          text,
    blob_sha256   text,
    content_hash  text NOT NULL,
    deleted       boolean NOT NULL DEFAULT false,
    actor         text NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (file_id, version)
);

-- Outgoing links parsed from note bodies. Targets are resolved at query time, the way
-- Obsidian does it, so links to notes created later still resolve.
CREATE TABLE notes.links (
    from_id     uuid NOT NULL REFERENCES notes.files (id) ON DELETE CASCADE,
    target      text NOT NULL,            -- as written: "Folder/Note", "Note", "image.png"
    target_key  text NOT NULL,            -- lower-case, no .md: used for resolution
    kind        text NOT NULL CHECK (kind IN ('link', 'embed', 'markdown')),
    anchor      text,                     -- #heading or #^block
    alias       text
);
CREATE INDEX links_from_idx ON notes.links (from_id);
CREATE INDEX links_target_idx ON notes.links (target_key);

-- Per-device API tokens (shared by every service; first user is the Obsidian plugin).
CREATE TABLE meta.api_tokens (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name          text NOT NULL,
    token_hash    text NOT NULL UNIQUE,   -- sha256 of the token; the token itself is never stored
    scopes        text[] NOT NULL DEFAULT '{notes}',
    created_at    timestamptz NOT NULL DEFAULT now(),
    last_used_at  timestamptz,
    revoked_at    timestamptz
);

GRANT USAGE ON SCHEMA notes TO cove_app, cove_readonly;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA notes TO cove_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA notes TO cove_app;
GRANT SELECT ON ALL TABLES IN SCHEMA notes TO cove_readonly;
ALTER DEFAULT PRIVILEGES IN SCHEMA notes GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO cove_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA notes GRANT USAGE, SELECT ON SEQUENCES TO cove_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA notes GRANT SELECT ON TABLES TO cove_readonly;
GRANT SELECT, INSERT, UPDATE ON meta.api_tokens TO cove_app;
GRANT USAGE, SELECT ON SEQUENCE meta.api_tokens_id_seq TO cove_app;
REVOKE ALL ON meta.api_tokens FROM cove_readonly;   -- keep token hashes out of ad-hoc SQL

INSERT INTO meta.domains (name, description, db_schema, standard, conventions) VALUES (
  'notes',
  'Notes: everything I write (notes, ideas, journal entries), mirrored two-way with my Obsidian vault.',
  'notes',
  'Obsidian Flavored Markdown files (YAML frontmatter, [[wikilinks]], ![[embeds]], #tags); JSON Canvas for .canvas files.',
  'notes.files holds every vault file by path; body is the raw file and is never reformatted. Writes need the '
  'current version (optimistic locking); concurrent edits are 3-way merged, and true conflicts are saved as a '
  '"(conflict …)" copy. Deletes are tombstones (deleted_at). Change feed = notes.files.seq. Attachments are '
  'content-addressed blobs. Links resolve by file name (case-insensitive, without .md), like Obsidian.'
);
INSERT INTO meta.changelog (migration, summary) VALUES
  ('005_notes', 'Created notes schema (files, versions, links, blobs, change feed) and meta.api_tokens.');

-- migrate:down
DROP SCHEMA notes CASCADE;
DROP TABLE meta.api_tokens;
DELETE FROM meta.domains WHERE name = 'notes';
DELETE FROM meta.changelog WHERE migration = '005_notes';
