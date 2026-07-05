# Design

Design system for the FeatureGen catalog UI. Register: product. Theme decision comes from the
scene, not the category: a data engineer at a bank, mid-afternoon under office light on a 27-inch
monitor, checking whether yesterday's deposits upload staled any features before a retrain. That
scene forces a light, high-contrast, glare-friendly instrument. No dark mode in v1.

## Color

OKLCH throughout. Strategy: **Committed** — the left rail is a deep petrol-ink surface that
carries the identity (roughly a sixth of every screen), content sits on a distinctly colder gray
ground with near-white panels lifted by soft shadows, and strong data states speak in solid
chips. Amplified 2026-07-05 after the first cut read as a wireframe: value separation,
saturation, and depth were all under-committed. No pure #000/#fff anywhere.

```css
:root {
  /* content neutrals: the ground is VISIBLY colder than panels (value separation is the point) */
  --ground:       oklch(0.955 0.009 215);  /* app background behind panels */
  --paper:        var(--ground);           /* legacy alias */
  --surface:      oklch(0.995 0.002 210);  /* panels, rows: near-white, floats on the ground */
  --surface-2:    oklch(0.975 0.006 212);  /* inset zones inside panels (kv grids, editors) */
  --ink:          oklch(0.25 0.025 225);
  --ink-soft:     oklch(0.44 0.02 222);
  --ink-faint:    oklch(0.58 0.015 220);
  --line:         oklch(0.86 0.012 212);
  --line-strong:  oklch(0.76 0.018 212);
  --shadow:       0 1px 2px oklch(0.25 0.025 225 / 0.05), 0 6px 20px oklch(0.25 0.025 225 / 0.07);

  /* the rail: a committed dark petrol surface (identity lives here) */
  --rail-bg:      oklch(0.27 0.045 215);
  --rail-bg-2:    oklch(0.23 0.04 215);    /* rail footer / inset */
  --rail-ink:     oklch(0.93 0.01 210);    /* rail primary text */
  --rail-ink-soft:oklch(0.72 0.02 212);    /* rail secondary text */
  --rail-line:    oklch(0.36 0.04 215);
  --rail-active:  oklch(0.36 0.06 212);    /* active nav fill */
  --rail-accent:  oklch(0.78 0.09 200);    /* active nav text / logomark on dark */

  /* accent: petrol, now with real presence */
  --accent:       oklch(0.46 0.11 210);
  --accent-hover: oklch(0.39 0.115 210);
  --accent-soft:  oklch(0.93 0.03 208);
  --accent-line:  oklch(0.70 0.08 208);
  --accent-deep:  oklch(0.30 0.06 214);    /* committed hero surfaces (Overview start-here) */

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
  --proposal:     oklch(0.46 0.115 300);
  --proposal-solid: oklch(0.47 0.115 300);
  --proposal-soft:oklch(0.955 0.025 300);
  --chip-ink:     oklch(0.985 0.005 210);  /* text on -solid chips */
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
- Every `object_ref`, column name, feature id renders in Plex Mono 13px.

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

- **Nav item**: 32px row, 8px radius; active = `--accent-soft` fill + `--accent` text + 600 weight
  (no left-stripe accents, banned). Icon 16px inline SVG, 1.5px stroke.
- **Button**: primary = `--accent` fill, paper text, 8px radius, 32px height; secondary = hairline
  border + ink text; destructive/confirm variants use semantic colors. Focus: 2px outline
  `--accent-line`, 2px offset.
- **Badge**: 20px pill, 11px caps label, soft background + strong text of its semantic pair, plus
  a glyph or text (never color alone): `grain`, `as-of`, `pii`, `stale`, `proposal`, `held`…
- **Field**: 32px input, hairline border, surface background; label 12px/600 above; focus ring as
  buttons. Inline validation text in `--danger`, 13px.
- **Table/list row**: 40px min height, hairline separators, mono for refs, right-aligned numerics.
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
