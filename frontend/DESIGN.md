# Design

Design system for the FeatureGen catalog UI. Register: product. Theme decision comes from the
scene, not the category: a data engineer at a bank, mid-afternoon under office light on a 27-inch
monitor, checking whether yesterday's deposits upload staled any features before a retrain. That
scene forces a light, high-contrast, glare-friendly instrument. No dark mode in v1.

## Color

OKLCH throughout. Strategy: **Restrained-plus** — warm-cool paper neutrals tinted toward the brand
hue, one committed petrol-ink accent carrying identity (nav, actions, focus), and a disciplined
semantic set for data states. No pure #000/#fff anywhere.

```css
:root {
  /* neutrals (tinted toward hue 200) */
  --paper:        oklch(0.975 0.005 200);  /* app background */
  --surface:      oklch(0.993 0.003 200);  /* panels, table rows */
  --ink:          oklch(0.26 0.02 220);    /* primary text */
  --ink-soft:     oklch(0.45 0.015 220);   /* secondary text */
  --ink-faint:    oklch(0.60 0.012 220);   /* tertiary, placeholders */
  --line:         oklch(0.885 0.008 200);  /* hairline borders */
  --line-strong:  oklch(0.80 0.012 200);

  /* brand accent: petrol ink */
  --accent:       oklch(0.42 0.085 205);
  --accent-hover: oklch(0.36 0.09 205);
  --accent-soft:  oklch(0.945 0.02 202);   /* selected nav, soft chips */
  --accent-line:  oklch(0.75 0.06 204);

  /* semantic states (never color alone; always pair with label/glyph) */
  --ok:           oklch(0.52 0.10 165);    /* fresh, ingested, registered */
  --ok-soft:      oklch(0.955 0.025 165);
  --warn:         oklch(0.55 0.11 75);     /* held, flagged, stale */
  --warn-soft:    oklch(0.965 0.035 85);
  --danger:       oklch(0.50 0.14 25);     /* rejected, quarantined, leakage */
  --danger-soft:  oklch(0.965 0.02 25);
  --proposal:     oklch(0.47 0.10 300);    /* AI suggestions, pre-confirm */
  --proposal-soft:oklch(0.96 0.02 300);
}
```

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
