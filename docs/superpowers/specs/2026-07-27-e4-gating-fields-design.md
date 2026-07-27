# E4 — LLM Proposes the Gating Fields (design)

Date: 2026-07-27 · Status: design for review · Parent: `2026-07-26-llm-metadata-enrichment-design.md` (E4) · Builds on: E1a (built)

> **Why this phase matters.** E1a made the AI's *advisory* metadata governed — but advisory fields never
> unblock anything. The fields that actually decide whether a proposed feature is `DESIGN_CHECKED` or
> `NEEDS_EXTERNAL_VALIDATION` are the ones the AI is forbidden to touch. E4 opens them: **AI proposes, a
> human confirms in one click, the requirement clears.** That is the first phase whose success metric is a
> real number — features moving out of "needs validation".

## The user's directive (the safety line)

Get as much as possible from the LLM; where it is unsure, flag "human verification needed"; **feature
generation still USES the unverified value — used, never blocked.** But an AI-proposed value must stay
**visibly unverified** until a human confirms it: it may never *silently* pass as verified.

## What the gauntlet actually requires (grounded)

`_validate_idea` has exactly one terminal line: `status = "NEEDS_EXTERNAL_VALIDATION" if requirements else
"DESIGN_CHECKED"` (`feature_assist.py:765`). So clearing requirements IS the goal. Seven codes are minted:

| Requirement | What clears it | Human-confirm reaches it? |
|---|---|---|
| `GRAIN_IS_UNIQUE` | VERIFIED `grain` fact → `graph_node.is_grain` + `grain_fact_event_id` | **YES — today, end-to-end** |
| `TEMPORAL_IS_POPULATED` | VERIFIED `availability_time` fact → `is_as_of` + `availability_fact_event_id` | **YES — today, end-to-end** |
| `JOIN_CONNECTIVITY` | VERIFIED `approved_join` (dual admin) | YES (but Pass C proposes, not the LLM) |
| `ADDITIVITY_SUPPORTS_OPERATION` | `_governed_read(...).status == "resolved"`, needing `_BEHAVIOURAL.operational_rule` = taxonomy/confirmed ∨ source/attested ∨ **human/confirmed** | **YES mechanically — but unreachable today** (`human_editable=False`) |
| `UNIT_CONSISTENT` | **any non-empty `graph_node.unit` — mere presence** (`_column_meta`, `feature_assist.py:532-544,703-712`) | **NO** — no display projection, so a confirmed decision never reaches the column the validator reads |
| `CURRENCY_CONSISTENT` | same, on `graph_node.currency` | **NO** (and the governed `currency_binding` fact that *does* exist is never consulted) |
| `TYPE_IS_NUMERIC` | bare `ov.value` numeric — **not** `status` (`:663-665`) | **NO** — `graph_node.data_type` is source-only; policy excludes HUMAN from the operational rule by design |

## E4a — the free + cheap slice (build this first)

### A1. Turn on and surface what already exists — `is_grain` / `is_as_of`
**Zero new mechanism.** Pass B `table_synth._propose_table_facts` (`table_synth.py:592-700`) **already has the
LLM proposing grain and availability_time as governed DRAFT overlay facts** with the service actor stamped
for four-eyes; `table_fact_governance` (`:149-205`) already lists them with an honest
`"llm_proposed_not_profiled"` origin; `POST /governance/table-facts/{fact_key}/confirm`
(`api/routes/governance.py:262-291`) already projects a VERIFIED fact into `is_grain`/`is_as_of` +
`*_fact_event_id`; C1 already turns that into a cleared requirement.
**Work:** enable `OVERLAY_TABLE_SYNTH`, make the queue visible and workable in the Governance screen, and
prove the chain end-to-end on the sample. Caveat: the projector can honestly return `"pending"` under a
stale drift watermark (`table_fact_governance.py:303-315`) — surface that state rather than hiding it.

### A2. Open `additivity` to the LLM — three small edits
- add `_LLM_PROPOSED` to `_BEHAVIOURAL.display_rule` (`field_policies.py:124-131`) so an AI proposal is
  **visible**;
- set `human_editable=True` on `_BEHAVIOURAL` so the generic correction route stops 403-ing
  (`field_correction.py:250-256`);
- add an `additivity` writer in `enrich.py` reusing E1a's `_write_llm_field_evidence` verbatim.
**Do NOT touch `operational_rule`.** The human confirm is what makes it load-bearing — that IS the design.
The display column, `_DECISION_LINK_COLUMN` entry and `additivity_decision_id` already exist
(`field_resolution.py:91,109`, migration `0984:10`), and GATE 2 hash verification already protects the clear.
*Note:* confirming `semi_additive`/`non_additive` correctly turns the requirement into a **rejection**
(`feature_assist.py:683-686`) — confirming is not always "unblocking", and the UI must say so.

### A3. Make one-click confirm actually issuable
`confirm_existing` (`field_correction.py:441-479`) **already** lets a human accept an AI proposal by
`evidence_id` without retyping — it appends `human/confirmed` carrying the LLM's own value, and the
four-eyes denial explicitly permits `llm` as the third party (`:464-465`). The gap is the CAS anchor:
`read_field_cas` (`:190-199`) **is wired to no route**, and `asset_detail` exposes evidence ids but not
`evidence_set_hash` — so a client cannot assemble the required CAS triple from a GET. **Expose it.** Small,
and it is what turns "AI proposed" into a one-click human decision.

**A1+A2+A3 clear 3 of the 7 requirement codes, add zero new authority mechanism, and create zero
silent-clear exposure.**

## E4b — `unit` / `currency` (self-contained second slice)

Expensive and hazardous; ship it as one atomic change:
1. migration: `unit_decision_id` / `currency_decision_id` columns;
2. `_DISPLAY_COLUMN` + `_DECISION_LINK_COLUMN` entries;
3. `_LLM_PROPOSED` into `_MEASURE_ANNOTATION.display_rule` + `human_editable=True`;
4. an LLM writer;
5. **MANDATORY, same change:** switch `_validate_idea`'s unit/currency reads (`:695-713`) from
   `_column_meta` flat presence to `_governed_read(...).status == "resolved"`.

**Step 5 is not optional.** Steps 2+3 without it create a real silent clear: an AI unit would land in
`graph_node.unit` and clear `UNIT_CONSISTENT` with no human in the loop — the exact failure this program
exists to prevent. It can also cause a spurious `MIXED_UNITS` hard reject (`:697-699`).

**Projection-wipe hazard (must be handled):** giving `unit` a display column makes the resolver
authoritative over a column `build_graph` populates directly. A glossary upload's source fields do **not**
include unit/currency, so a `logical_ref` carrying only LLM evidence would resolve `display_value=None` and
**NULL a real source-declared unit** (`field_resolution.py:196-213`). Technical CSVs write SOURCE/ATTESTED
unit evidence and re-project fine; glossary uploads do not. Guard this explicitly and test it.

**Acceptance test for E4b:** *an `llm/proposed` unit is VISIBLE, and a feature resting on it stays
`NEEDS_EXTERNAL_VALIDATION` until a human confirms — used, flagged, never silently cleared, never blocked.*

## NOT in E4 — `logical_representation`
Leave closed. Its clear reads `graph_node.data_type`, the **physical** type, writable only by `build_graph`
from the upload; and `_LOGICAL_REPRESENTATION.operational_rule` (`field_policies.py:98-105`) excludes HUMAN
**by design**. Letting an AI proposal (or a human) rewrite a physical type contradicts the policy's intent.

## Correction to a prior claim: there are THREE flat-presence surfaces, not two
`TYPE_IS_NUMERIC` also clears on a bare `ov.value` rather than `status` (`feature_assist.py:663-665`,
deliberately — an ungoverned type is treated as a numeric hint). Plus the `is_as_of`/`is_grain` **column
discovery** at `:562-573` reads the flat flags: an AI-set flag there would not *clear* a requirement but
would **suppress a hard `NO_POINT_IN_TIME` reject** (`:723-726`). All three are unreachable by the LLM today
only because no display projection exists — **any E4 change that adds one opens that surface in the same
stroke.** This is the single most important invariant for the implementer.

## Surfacing "used, but resting on an AI value"
The reviewer currently sees `{code, operand: (catalog_source, object_ref), detail}` — the **column is
already there**; whose value it rests on is not. This needs **no** schema break: `Requirement.params` is a
sorted hashable tuple with a default, emitted only when non-empty (`contract/_serial.py:47-55`), and
`_validate_idea` already holds `ov.producer`/`ov.strength`/`ov.status` from every `_governed_read`. Work:
(a) mint `resting_producer`/`resting_status` params under a new registry schema version, (b) **fix
`api/feature_serialize.py:16` dropping `params`/`schema_version`** (a real omission vs `_serial.py`), (c)
render the badge.

## Build order
1. **A1** — switch on + surface the existing grain/as-of queue; prove the chain (biggest win, no build).
2. **A3** — expose the CAS anchor so one-click confirm works.
3. **A2** — open `additivity` (3 edits + writer).
4. **Surfacing** — the "rests on an AI value" badge (params + the serializer fix).
5. **E4b** — unit/currency as one atomic slice, step 5 included, wipe-hazard guarded.

## Success criteria (a real number, at last)
- On the sample: **N features move from `NEEDS_EXTERNAL_VALIDATION` to `DESIGN_CHECKED`** after a human
  confirms AI-proposed grain/as-of/additivity. Measure before and after.
- A human confirms an AI proposal in ONE action, without retyping the value.
- An AI-proposed gating value is always VISIBLE and always flagged until confirmed; no requirement is ever
  silently cleared by an unconfirmed AI value (test per field).
- Features are never BLOCKED by an AI proposal — the tri-state stays advisory.

## Deferred NFRs
Bulk/by-convention confirm UI, coverage dashboards, per-field confidence (P2), cost controls, ops. E4 is
functional: propose → confirm → clear.

## Risks
- **Silent clear** — the three flat-presence surfaces above; step 5 of E4b is mandatory and same-change.
- **Projection wipe** of a source-declared unit on glossary uploads (E4b) — guard + test.
- **Confirming can reject, not unblock** (`semi_additive`) — the UI must not promise "confirm to unblock".
- **Drift-stale projector** returns `"pending"` honestly (A1) — surface, don't hide.
