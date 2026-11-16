# cove brand

cove is a set of small personal databases, such as People, Move and Money, under one roof. The brand
needs to feel **sure of itself and welcoming at the same time**, the way good public signage does: bold,
legible and calm. It is not a dashboard and not a soft lifestyle app.

> **Principles**
> 1. **Ink first, colour as a signal.** Pages are near-black on paper. Each service gets exactly one bright
>    colour, used as solid blocks, not washes or gradients.
> 2. **Confident type.** Headings are wide and heavy. Data such as dates, counts and labels is set in mono, so it
>    reads as information.
> 3. **Crisp, not soft.** Tight corners, solid 1.5px ink borders, hard offset shadows. No blur, glow or
>    pastel gradients.
> 4. **Warm words.** The structure is strict and the language is kind. Write like a thoughtful friend, not a
>    database: "Worth reaching out to", "Your cove is quiet", "Add someone".
> 5. **One obvious action.** Each screen has one signal-coloured primary action. Everything else is
>    secondary (outlined) or ghost.

---

## Logo
- **Mark** (`logo/mark.svg`): a rounded square in the service's signal colour, with an ink "c" holding a
  wave. The bay that gives cove its name. Inline it so it follows `--signal`.
- **Favicon** (`logo/favicon.svg`): the mark on the core sea-glass colour.
- **Lockup:** mark + **cove** (Archivo 800, width 125%, tracking −4%), then a thin divider, then the service name in
  mono caps (`cove | PEOPLE`).
- Minimum size 16px. Keep clear space of half the mark's width around it. Never recolour the glyph,
  outline the mark, or put it on a busy image.

## Colour
Neutrals (light / dark):

| token | light | dark | use |
|---|---|---|---|
| `--bg` | `#F4F3EF` paper | `#0E0F10` | page |
| `--panel` | `#FFFFFF` | `#17191B` | cards, inputs |
| `--bg-sunk` | `#E9E8E2` | `#24272A` | avatars, code, hover |
| `--ink` | `#111210` | `#F2F1EC` | text, borders, primary outlines |
| `--muted` | `#5B5D56` | `#9B9C95` | secondary text (AA on both) |
| `--line` | `#DDDCD5` | `#2A2D2F` | dividers, quiet borders |

**Service colours.** Each service owns one colour pair. `signal` is the bright fill, always with ink text on
it. `text` is the deeper shade for links on light backgrounds. In dark mode, `signal` is used for text too.
Every pair passes WCAG AA (≥ 4.5:1) in both themes. The source of truth is `services.json`, mirrored in `cove.css`.

| service | key | signal | text | tone |
|---|---|---|---|---|
| cove (core) | — | `#17C3A5` | `#0A6B5C` | sea glass |
| People (CRM) | `people` | `#FF5B37` | `#B9300F` | vermilion |
| Move (fitness) | `move` | `#A4E03A` | `#3C6A0C` | lime |
| Money (finance) | `money` | `#FFC22E` | `#8A5800` | marigold |
| Journal | `journal` | `#A68CFF` | `#5638C2` | violet |
| Home | `home` | `#3EC5F2` | `#09698B` | pool blue |
| Reading (media) | `reading` | `#FF78B6` | `#AE1F66` | flamingo |

Where the signal colour appears, and only there:
- the 5px **service stripe** at the top of every page
- the logo mark and the eyebrow square
- the one **primary button** per screen
- one **highlight block** per screen (`.card.accent`, e.g. "Reach out soon"): solid signal with ink text
- small markers: active nav underline, timeline nodes, `due` badges, selection

Never use two service colours on one screen, except in a cross-service overview or the footer's
service dots.

## Type
- **Archivo** (variable: width 62–125, weight 100–900) for UI and headings.
  - `h1`: 44px / 800 / width 125% / tracking −3.5%. Mobile 32px.
  - `h2`: 19px / 750 / width 112%.
  - Body 15px / 400 / line-height 1.5. Buttons 13.5px / 700.
- **Martian Mono** for **data and labels**: form labels, field names, dates, counts, chips, table headers.
  Uppercase, 10.5–12px, tracking +4–6%.
- Both are self-hosted in `fonts/` (SIL Open Font License). Never load fonts from a CDN at runtime.

## Shape, space, depth
- 4px spacing grid (`--s1` … `--s7`). Generous page padding, dense lists.
- Corner radius: 3px for chips, 5px for nav, 8px for inputs, buttons and avatars, 12px for cards. No pills
  except the theme toggle.
- Borders: 1.5px `--ink` for interactive outlines (buttons, search, focused inputs), 1px `--line` for
  passive structure.
- Depth is a **hard offset shadow** (`4px 4px 0 var(--ink)`) and only for things you can act on or that
  identify a record (button hover, a contact's avatar). Never blurred shadows.
- Motion: 110–170ms. Buttons lift 2px on hover. `prefers-reduced-motion` turns it off.

## Components (in `cove.css`)
`.shell-header` (with service stripe), `.brand`, `.nav`, `.page-head` (eyebrow + h1 + lede), `.card`
(`.accent` = signal block, `.inset` = dashed form well), `.btn`/`button` (`.secondary`, `.ghost`,
`.link`), `.search`, `.toggle-chip`, `.chip` (`.accent`, `.group`), `.avatar` (`.lg`, `.signal`),
`.list`/`.list-item`, `dl.fields`, `.timeline`, `.notice` (`.error`), `.empty`, `.due`, `.eyebrow`.
Icons: 24px grid, 2px stroke, square caps (see `app/src/cove/web/templates/_icons.html`).

## Voice
- Second person, plain words, short. Sentence case everywhere except mono labels.
- Name things the way people talk: **People**, **Circles** (not "tags"), **Rhythm** (not "follow-up
  cadence"), **Conversations** (not "interactions"), **Reach out** (not "due").
- Empty states invite rather than report: "Your cove is quiet. Add someone you'd like to keep close."
- Errors say what happened and what to do, without blame: "This preview expired. Please upload the
  file again."
- Agents' changes are shown as "agent (name)", the user's as "you".

## Building a new service
1. Add it to `services.json` with a new colour pair (check AA contrast for `signal` with ink text and
   `text` on `--bg`), and mirror it in `cove.css` (`[data-service]` + `.svc-dot`).
2. Serve `brand/` at `/brand`, link `/brand/cove.css`, set `<html data-service="<key>">`.
3. Reuse the shell (header with stripe, nav, page-head, footer service dots) and the components above.
   Add new components to `cove.css`, not per-service CSS, so every service stays consistent.
4. Light and dark both have to work. Check at 390px width.
