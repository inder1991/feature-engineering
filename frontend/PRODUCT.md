# Product

## Register

product

## Users

Data engineers, ML engineers, and data scientists at a bank, at desks on large monitors in office
light, during real work sessions. Data engineers ingest schema+facts uploads and watch drift and
quarantine. ML engineers track feature freshness and drift impact before retrains. Data scientists
search the catalog for usable columns and assemble candidate features. All three are expert users
who scan dense structured data quickly; none of them are here to be marketed to.

## Product Purpose

FeatureGen is the bank's upload-driven data catalog and feature-engineering workbench: teams
upload schema+facts files per source, the platform serves a searchable, freshness-vouched map of
what data exists (grain, as-of, sensitivity, additivity, joins), quarantines what it cannot trust,
and helps assemble ML features safely (leakage and staleness checked, suggestion-then-confirm).
Success: an analyst finds the right column in seconds, trusts every badge they see, and never
ships a leaky or stale feature because the UI made the unsafe path look safe.

## Brand Personality

Precise, calm, trustworthy. The voice of a careful senior engineer: plain declarative sentences,
numbers where numbers belong, no exclamation marks, no hype. The interface should feel like a
well-kept ledger: quiet authority, everything in its place, nothing decorative pretending to be
information.

## Anti-references

- Generic admin templates (Bootstrap/AdminLTE/shadcn-default gray-on-white with blue buttons).
- Neon-on-black "hacker dashboard" cosplay; this is a daylight instrument, not a demo.
- Marketing-site gloss: hero gradients, oversized illustrations, feature-card grids.
- Walls of identical cards; the current unstyled prototype (bare inputs, centered tab buttons,
  no landing orientation) is the primary anti-reference.
- Finance clichés: navy-and-gold, dollar-green accents.

## Design Principles

1. **The catalog is the hero.** Reading structured metadata fast beats decoration; typography,
   alignment, and density do the aesthetic work.
2. **States are first-class citizens.** Fresh, stale, held, rejected, quarantined, proposal:
   each visibly distinct, each honest, never encoded by color alone.
3. **Never fake certainty.** Absent enrichment, unconfigured AI, stub identity: shown plainly and
   calmly, in the same voice as everything else (mirrors the platform's fail-closed backend).
4. **Every empty state teaches the next action.** First-run and zero-result surfaces orient and
   point forward; a new user should understand the whole loop from the landing view alone.
5. **Suggestion is not registration.** AI output always looks provisional until a human confirms;
   the visual system enforces the platform's suggestion-then-confirm contract.

## Accessibility & Inclusion

WCAG 2.1 AA. Fully keyboard navigable with visible focus rings. State encoding always pairs color
with a text label or glyph (color-blind safe). Respect prefers-reduced-motion. Body contrast
≥ 7:1, secondary text ≥ 4.5:1. Hit targets ≥ 32px.
