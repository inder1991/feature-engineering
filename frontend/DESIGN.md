# Design

Design system for the FeatureGen catalog UI. Register: product. Theme decision comes from the
scene, not the category: a data engineer at a bank, mid-afternoon under office light on a 27-inch
monitor, checking whether yesterday's deposits upload staled any features before a retrain. That
scene forces a light, high-contrast, glare-friendly instrument. The Emirates NBD palette uses
official navy (`#072447`) for structure, vibrant blue (`#2765FF`) for action, and white (`#FFFFFF`)
for primary surfaces. Supporting blue-grays exist only to make dense analytical screens readable.
No dark mode in v1.

## Color

Strategy: **Emirates NBD blue** — the left rail uses official primary blue, content sits on a quiet
blue-gray ground with official-white panels, and interactive emphasis uses official accent blue.
Semantic success, warning, and danger retain green, amber, and red identities; proposal states use
steel blue instead of violet so status remains distinct without leaving the corporate palette.

```css
:root {
  --brand-primary:#072447;
  --brand-accent: #2765ff;
  --brand-white:  #ffffff;

  --ground:       #f3f6fb;
  --paper:        var(--ground);           /* legacy alias */
  --surface:      var(--brand-white);
  --surface-2:    #f7f9fc;
  --ink:          #102a43;
  --ink-soft:     #486581;
  --ink-faint:    #5d7187;
  --line:         #d9e2ec;
  --line-strong:  #9fb3c8;
  --shadow:       0 1px 2px rgb(7 36 71 / 0.045),
                  0 8px 24px rgb(7 36 71 / 0.08);

  --rail-bg:      var(--brand-primary);
  --rail-bg-2:    #04182f;
  --rail-ink:     var(--brand-white);
  --rail-ink-soft:#b8c7dc;
  --rail-line:    #244567;
  --rail-active:  var(--brand-accent);
  --rail-accent:  var(--brand-white);

  --accent:       var(--brand-accent);
  --accent-hover: #174edb;
  --accent-solid: #17457a;
  --accent-soft:  #edf3f9;
  --accent-line:  #a8bdd3;
  --accent-deep:  var(--brand-primary);

  /* semantic states: -solid fills carry chip text (small caps text needs fill L <= 0.55) */
  --ok:           oklch(0.50 0.115 163);
  --ok-solid:     oklch(0.52 0.115 163);
  --ok-soft:      oklch(0.945 0.035 163);
  --warn:         oklch(0.53 0.115 70);
  --warn-solid:   oklch(0.52 0.115 70);
  --warn-soft:    oklch(0.955 0.045 85);
  --danger:       oklch(0.48 0.15 25);
  --danger-solid: oklch(0.50 0.15 25);
  --danger-soft:  oklch(0.955 0.025 25);
  --proposal:     #175d87;
  --proposal-solid:#124d71;
  --proposal-soft:#edf5fa;
  --chip-ink:     var(--brand-white);      /* text on -solid chips */
}
```

Application rules that make it read committed, not decorated:

- Panels, rows, and callouts sit on `--surface` with `--shadow`; the colder ground shows between
  them. Depth comes from this one shadow, used consistently; hairlines remain.
- Strong states (held, rejected, pii, stale, proposal, resolved-mock) are SOLID chips
  (`*-solid` fill, `--chip-ink` text, 600 weight, 10-11px caps). Quiet facts (grain, as-of)
  stay soft chips. Labels always present; color never works alone.
- Numbers carry meaning: counts in ingest summaries and result lines take their semantic color
  at 600 weight (ok for asserted/live, warn for staled/quarantined).
- Every page-head opens with a mono 11px uppercase accent eyebrow: `CATALOG · <ROUTE>`.
- One hero moment for the whole app, on Overview: the start-here callout becomes an
  `--accent-deep` surface with light text and a light-on-dark primary button, and the loop's
  step numbers are 20px Plex Mono in `--accent`. Nothing else in the content column goes dark.

## Typography

IBM Plex Sans (UI) + IBM Plex Mono (object refs, code, counts), self-hosted via @fontsource
packages (no CDN). Engineered, legible, unmistakably a tool; deliberately not Inter.

- Body 14px/1.5; secondary 13px; micro-labels 11px uppercase tracked +0.06em, weight 600.
- Headings: 22px/600 page titles, 15px/600 section titles. Scale ratio ≥1.25, hierarchy through
  weight + size together.
- `font-variant-numeric: tabular-nums` on all counts and tables.
- Every `object_ref`, column name, feature id renders in Plex Mono 13px where it appears inline.
- A result-row title (a column or table name) renders in Plex Mono 14px/600 — the one place an
  identifier outranks body text, because it is the row's heading.
- A result-row address line (`source › table › column`) renders in Plex Mono 11px, lowercase and
  untracked — a tertiary address, not a micro-label.

## Layout

- App shell: fixed left rail 240px (nav + session identity at bottom), content column max 1120px
  with 32px gutters. No centered-tab navigation.
- Hash-routed views (#/overview, #/upload, #/search, #/review, #/workbench) so every screen is
  deep-linkable.
- Vertical rhythm: 8px base grid; section spacing 32-48px, control spacing 8-12px. Vary spacing
  for rhythm; page header zones get more air than data zones.
- Data renders as structured rows and tables, not card grids. Panels (single-level, 10px radius,
  hairline border, no shadow stacking) only when grouping is real. Nested cards are banned.
- Empty states are content: 1-line orientation + the next action, set in the normal voice.

## Components

- **Navigation accordion**: one group is open at a time and the group containing the current route
  opens automatically. Group toggles use compact uppercase labels with a rotating chevron. Items
  are 36px rows with 9px radius; active items use a restrained blue gradient, light text, and a
  subtle inset line. Icon 16px inline SVG, 1.5px stroke.
- **Button**: primary = `--accent` fill, paper text, 8px radius, 32px height; secondary = hairline
  border + ink text; destructive/confirm variants use semantic colors. Focus: 2px outline
  `--accent`, 2px offset — 6.43:1 on `--surface`, clearing the 3:1 non-text floor (WCAG 1.4.11).
  Not `--accent-line`, which is 2.56:1 and would fail; the ring is set once, on bare
  `:focus-visible`, so every control in the app inherits it.
- **Badge**: 20px pill, 11px caps label, soft background + strong text of its semantic pair, plus
  a glyph or text (never color alone): `grain`, `as-of`, `pii`, `stale`, `proposal`, `held`…
- **Field**: 32px input, hairline border, surface background; label 12px/600 above; focus ring as
  buttons. Inline validation text in `--danger`, 13px.
- **Table/list row**: 40px min height, hairline separators, mono for refs, right-aligned numerics.
- **Overflow disclosure** (a row's tertiary actions behind one `···` trigger): a panel anchored to
  the trigger — `--surface`, hairline border, 10px panel radius, `--shadow` — holding full-width
  item lines at 32px min height, 13px, left-aligned, 6px radius, `--surface-2` on hover. A
  DISCLOSURE, never an ARIA menu: `role="menu"` promises arrow-key roving this does not implement.
  Four exits, all of them kept: Escape, a pointer elsewhere, focus leaving the panel, and choosing
  an item; Escape and choosing an item return focus to the trigger.
- **Callout** (result states, honesty notes): full hairline border + semantic-soft background,
  10px radius, leading glyph; copy states the fact and the next action. No side-stripes.
- **Toast/status**: inline, role=status/alert as appropriate; no modal-first patterns.

## Motion

150-200ms, ease-out-quart, opacity/transform only. Nav and hover transitions 120ms. Respect
prefers-reduced-motion: reduce to opacity-only or none. No bounce, no elastic, no layout-property
animation.

## Voice in the UI

Plain declarative microcopy. "3 facts asserted, 1 staled." "Held: this upload removes 6 of 9
objects. Nothing was applied." "AI assist is not configured on this deployment." No exclamation
marks, no "oops", no emoji in product surfaces.
