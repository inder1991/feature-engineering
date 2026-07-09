# Taxonomy Governance Contract

**Status:** Active (Phase-0 task 6)
**Governs:** `src/featuregen/overlay/upload/taxonomy/` — the use-case registry, dimension registries, legacy crosswalk, and per-recipe applicability.
**Related:** `2026-07-09-usecase-taxonomy-crosswalk-draft.md` (v2, the authoritative content), `2026-07-09-intent-aware-recipe-selection-plan.md`.

The taxonomy is a **governed vocabulary**, not application code that any change may freely edit. Recognition, applicability, ranking, and (later) policy all bind to these IDs, and persisted generation records reference them by version. Changes therefore follow this contract.

## 1. Roles

- **Taxonomy owner** — accountable for the semantic structure (the use-case tree, the dimension vocabularies) and for adjudicating whether a proposed node passes the promotion test. A single named owner; the design decisions in v2 are theirs.
- **Mapping approver** — approves changes to the legacy crosswalk and per-recipe applicability (which recipe maps to which leaf). May be the same person as the owner for now; separable later.
- **Recognizer-prompt owner** — owns the recognizer prompt + model config (a *consumer* of the taxonomy, versioned separately). Named here so the two version streams don't drift silently.

## 2. The promotion test (the rule for adding a use-case leaf)

A tag/label earns a **use-case leaf** only if it has a distinct **prediction target, business decision, success measure, or policy regime**. Otherwise it is context or metadata (product/channel, framework, journey stage, potential consumer, feature theme, or org owner) and goes in the appropriate dimension. Org ownership is **never** tree identity — it lives in `governance_owner`/`operating_owner` metadata, so the tree survives re-orgs.

## 3. Versioning (semver on the registry)

The taxonomy carries a version (`taxonomy_version`) and the recipe mapping a separate `applicability_mapping_version`. Both are persisted on every generation record.

- **PATCH** — editorial only: descriptions, examples, display names. No ID or structure change. Consumers unaffected.
- **MINOR** — additive: a new leaf/dimension member, or a new alias for an existing ID. Backward-compatible; existing IDs keep resolving.
- **MAJOR** — breaking: renaming/removing an ID, changing a node's `selectable`, or re-parenting. Requires the deprecation process (§4) and owner + mapping-approver sign-off.

A change to which recipe maps to which leaf bumps `applicability_mapping_version` (MINOR/MAJOR by the same rules) even when `taxonomy_version` is unchanged — because *same recognition + changed mapping = different candidate set*.

## 4. Deprecation, aliases, and retired IDs

- **No hard deletes.** A retired ID is marked `status: deprecated` with a `replacement_id`, kept for a **backward-compatibility period of two MAJOR versions**, then removed only in a subsequent MAJOR.
- **Aliases** map an old string to a canonical ID (the legacy crosswalk is exactly this mechanism for the 107 pre-taxonomy tags). Aliases are additive (MINOR).
- **Unknown ID at runtime** — a recognizer output or persisted record referencing an ID not in the current registry resolves as follows: if it is a known alias/deprecated ID → follow `replacement_id`; otherwise it is rejected (recognition falls back to `unscoped`; a persisted record is read with the taxonomy version it was written under). The registry never dynamically creates an ID.

## 5. Intentionally-empty leaves

A leaf may be declared (`intentionally_empty: True`) ahead of any recipe — governed structure before content (e.g. `pricing.*`, `operations.*`). Policy:

- Coverage validation treats these as expected-empty (they must have **zero** primary and secondary recipes; a recipe mapping onto one is an error).
- When the recognizer proposes an intentionally-empty leaf, the disposition lens shows "no recipes authored for this objective yet" — honest, not a silent blank.
- Promoting one to populated is just authoring recipes that map to it (MINOR on the mapping version); no taxonomy change needed.

## 6. Review cadence & change process

- **Change proposal** — anyone may propose; the proposal states the dimension, the promotion-test justification (for a new leaf), and the crosswalk/mapping impact.
- **Approval** — owner (structure) + mapping approver (recipe mapping). A change touching a modelling-context/policy-relevant node also needs the recognizer-prompt owner's ack (the gold set may need new examples).
- **Validation gate** — every change must keep the import-time registry validators green (`_validate_registry`, `_validate_dimensions`, `_validate_crosswalk`) and the Phase-0 exit-criteria suite (G1–G4) green.
- **Cadence** — a scheduled quarterly review of the crosswalk + unpopulated-leaf list; ad-hoc changes anytime via the process above.

## 7. Known open items (carried from Phase-0 execution)

Three recipes currently sit on closest-fit leaves because the tree lacks a precise home — candidates for a MINOR taxonomy addition, pending owner sign-off:
- `claims_frequency_severity` → an `insurance.claims.frequency_severity` (actuarial claims-cost) leaf.
- `mortality_morbidity_loading` → an `insurance.underwriting` (or `insurance.mortality_morbidity`) leaf.
- `custody_holding_dynamics` → a `securities_services.custody.holdings` leaf.

Until promoted, their applicability primary is documented in `recipe_applicability.py` as interim.
