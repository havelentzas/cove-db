# The cove ethos

How cove is built, and how it grows. Read this before you add a service or change one, whether you're a person
or an agent. It is short on purpose: each principle has a one-line statement, why it matters, and a check you
can hold your work against.

## The heart

> All cove sub-services serve deep-reaching API endpoints, so that integrations with other services are
> managed, non-critical, yet available. Non-critical matters: if one service falls out of use, the others
> carry on without that extra input; they simply won't show the full picture. No service dropping out may
> leave another unable to do its job. What services gain from one another is extra context, all displayed in
> the same place, but that is only ever an addition to each service's own primary responsibility.

Everything below follows from this.

## Principles

### 1. Stand alone, connect deeply

Every service does its own job with no other service present, and offers deep APIs so the others can draw on it.

**Why:** Services come and go with real use. If People needed Messages to work, losing Messages would break
People. Context from other services makes each one richer, but it is never what makes it work.

**In practice:** Your service's pages, tools and migrations work with every other service disabled. Anything it
uses from elsewhere is optional, and without it the service shows less but never fails.

### 2. One job, one owner

Each service has one primary job, owns its schema and is the only thing that writes its data.

**Why:** One owner means one place for validation, locking, permissions and history, so the web UI, the API and
agents can never disagree about the rules.

**In practice:** All writes go through `<service>/service.py`. The web UI, the HTTP API and MCP call that same
layer, and nothing else writes the service's tables: no SQL from other services, no shortcuts in routes.

### 3. Connect through contracts, not internals

Services meet through versioned capabilities, opaque references and events, never through each other's tables.

**Why:** Contracts let a service change its insides freely and let others drop out cleanly. A join or foreign
key across schemas welds two services together for good.

**In practice:** No foreign keys, joins, views or writes across service schemas. Links are opaque refs such as
`cove:people/contact/<id>`. Capabilities have JSON Schema contracts that never break within a version. Anything
derived from another service is a cache you can rebuild, and side effects travel as events consumers pull at
their own pace.

### 4. Be honest about the gaps

When an answer combines services, it says which parts are missing and why.

**Why:** "Nothing found" and "couldn't look" are different answers. People and agents both make worse decisions
when a partial picture passes for the whole one.

**In practice:** A missing capability comes back as `Unavailable(reason)`, never as an exception for the caller to
handle. Combined responses carry `coverage` with what was included and what was missing, and the UI shows it
plainly.

### 5. Permissions travel with the caller

Every call runs as the person or agent who made it, with exactly their scopes, all the way through.

**Why:** An integration must never become a back door. Seeing messages should not quietly grant contact names.

**In practice:** Scopes are checked once, at the service layer, by `policy.guard`, and capabilities run as the
current principal. Without a scope, the caller gets less and is told so (`no_permission`). New scopes are
narrow, and nothing joins the default agent scopes without a reason.

### 6. Agents are first-class users

Anything you can do in the web UI, an agent can do through the API or MCP, safely.

**Why:** Much of cove's value is agents having real context. They need the same depth as the UI, clear guidance
on when to use each tool, and guard rails against what they read.

**In practice:** Every service has a deep API with refs and cursors, plus MCP tools for its main jobs. Tool
descriptions say when to call them, not just what they do. Stored text is data and never instructions, and
untrusted content is marked as such. Agents can't delete anything, and their changes are recorded under their
own name.

### 7. Data that fills itself

Prefer collectors, imports and sync over typing things in.

**Why:** A database you have to feed by hand goes stale. Data that arrives on its own stays current and earns
its keep.

**In practice:** Look for a way in that needs no typing (a sync, a collector, an import from an export) before
you build a form. Ingest is idempotent and resumable, and collectors get write-only tokens. Manual entry is the
fallback and the place for corrections.

### 8. Your data stays yours

Everything in cove can leave in an open format, and nothing is lost by accident.

**Why:** It's a lifetime of personal data. It must outlive any one app, including cove itself.

**In practice:** Store open standards where they exist (JSContact and vCard for people, Obsidian Markdown for
notes) and offer an export. Archive instead of delete, keep versions and an append-only audit log, and make sure
backups cover the service.

### 9. Secure by default

Least privilege, isolation and no personal data in the repo, from the first commit.

**Why:** This is private data on a personal network, built in a public repository. Security that comes later
tends not to come at all.

**In practice:** Deny by default: sign-in on every page, CSRF on every form, scoped tokens for every API.
Workspaces stay isolated in their own databases and roles, containers stay hardened, and secrets stay in
Docker secrets. No hostnames, emails, tokens or real personal data in commits, fixtures or docs.

### 10. One look, one voice

Every service looks and sounds like part of the same cove.

**Why:** A shared brand makes many small databases feel like one welcoming place, and makes a new service
familiar the moment it opens.

**In practice:** Follow `brand/BRAND.md`: the shared shell, one signal colour per service, components added to
`cove.css` rather than per-service CSS, light and dark both working, and warm, plain copy.

## Checklist for a new service

1. Write its one job in a single sentence, and list what it can do without any other service (1, 2).
2. Give it its own schema and migration, with `meta.domains` and `meta.changelog` rows, and no cross-schema
   foreign keys (2, 3).
3. Build `<service>/service.py` as the only writer, with validation, versions, the audit log and a complete
   `policy.guard` table (2, 5).
4. Add narrow scopes, and keep them out of the default agent scopes unless they are read-only and low-risk (5, 9).
5. Declare what it provides, what it optionally consumes and which events it publishes, against versioned
   contracts (3).
6. Test it with every service it consumes disabled, and check that `coverage` names what is missing (1, 4).
7. Expose a deep HTTP API and MCP tools with trigger-style descriptions and scopes, and mark stored text as
   untrusted (6).
8. Find a way for its data to arrive without typing, and make ingest idempotent (7).
9. Offer export in an open format, and archive rather than delete (8).
10. Give it a key and colour in `brand/services.json`, use the shared shell and `csrf_field()` on every form, and
    check light, dark and 390px wide (9, 10).
11. Document it, update `CLAUDE.md`, and keep fixtures synthetic (9).

## Living document

**Version:** 1 · **Last reviewed:** 2026-11-16

This ethos is used from today and changes as cove does. The principles should follow real use, not the other
way round.

**Proposing a change:** edit `ETHOS.md` in a branch, add a changelog entry with a short rationale (what changed
and what in actual use prompted it), and bump the version. Renumbering is fine, but keep principle titles stable
where you can, because their anchors are linked.

**When to review:**

- a service has gone unused for a long time (did the others really carry on without it?)
- a principle keeps being worked around, which means it's wrong or its tooling is missing
- new kinds of users arrive, such as another person in a workspace, a new agent or a new device
- a new service doesn't fit the checklist
- at least once a year, even if nothing else prompts it

### Changelog

- **v1 · 2026-11-16:** first version. Distilled from how cove is already built (the service layer, the security
  phases, the brand) and from the fabric integration principles, with the heart in the user's own words.
