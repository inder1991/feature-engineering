# E0b — Identifier-Link Admission, Critic Panel and Proposed-Link Planning

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make cross-catalog identifier links flow all the way to features, and make what flows
trustworthy enough to be worth flowing.

**Architecture:** Runs against the **shipped single-column `entity_bridge`**, not E3's tuple bridges,
so it does not wait for Foundation. Four stages: deterministic pre-checks reject the obvious with no
model call; `ground_bridge` corroborates from metadata; a two-lens LLM critic panel judges; `fuse`
combines them. Admitted links become `proposed` facts and reach the planner alongside `VERIFIED`
ones; every feature standing on one carries `JOIN_IDENTITY_UNCONFIRMED`, and the roadmap's link
policy decides which tiers may act on it.
Stages 2 and 4 reuse the shipped `attest/` contracts unchanged.

**Parent:** `2026-07-27-e3-e5-cross-catalog-ontology-program.md`, Phase E0b.
**Evidence base:** `docs/architecture/2026-07-28-verified-interfaces-cross-catalog.md`.

**Tech Stack:** Python 3.12, psycopg, pytest; FastAPI for the governance route; React/TypeScript for
the review surface.

## Global Constraints

- **Proposed links are TIER-GATED** (roadmap §4). Discovery and feature suggestion may use them
  freely; a sandbox analysis may, if visibly marked and never written to production feature or
  model-input tables; **production materialization requires a VERIFIED link** or policy-based
  automatic attestation backed by deterministic data evidence.
  *Revised 2026-07-28.* The earlier rule admitted proposed links all the way to published numbers on
  the strength of a `JOIN_IDENTITY_UNCONFIRMED` flag. That was wrong twice over: a warning does not
  make an incorrect customer join safe — users trust the headline number, not the provenance note —
  and `materialize/joins.py` already refuses unverified joins, unknown cardinality and per-hop
  fan-out, which is the correct production policy and must not be weakened.
  Automatic attestation is what keeps this from meaning "a human approves 100,000 columns"; it needs
  real data, so it arrives with Release 1.
- **The critic gates admission, never features.** A `no` prevents a link becoming `proposed` at all,
  so the AI only ever *narrows* what features may join.
- **Unanimity to admit — on the pairs that warrant a panel.** Two critics with different lenses run
  for **high-blast-radius or ambiguous** pairs (one that would merge a large component, a
  compliance-domain table, or a `fuse` confidence in the middle band); a single critic runs
  otherwise. A panel on every pair is cost without signal; no panel at all is worse, because a lone
  confident model on an identity claim is where the damage is largest. Any `different_namespace` or
  `insufficient_evidence` suppresses. Disagreement is its own `critics_disagree` state, not an
  ordinary rejection — it marks where human judgement is worth most.
- **`status` stays out of the STABLE PLAN hash, and belongs IN the execution/authorization hash**
  (roadmap §3b). Bridge `fact_key`s are hashed into plan identity (`plan.py:76`,
  `declarations.py:826`, `fingerprint.py:75`); if `status` entered THAT, confirming a link would
  invalidate every plan resting on it — the `operand_roles` trap. But it must enter the execution
  hash, or a sandbox result computed on a proposed link is reused unchanged after the link is
  verified or rejected.
- **Confidence never feeds eligibility.** It may order a review queue. The `influence_max` ceiling
  (`field_policies.py:58-65`, enforced at `field_authority.py:296-297`) is the hard guarantee.
- **Blind critic.** Following `attest/reclassify.ColumnContext`: the critic is never told the system
  proposed this pair, nor any confidence. A critic shown the answer agrees with it.
- **Fail closed.** An off-vocabulary or failed critic call suppresses; it never defaults to admit.
  One failed call is not unanimity.
- The catalog currently holds **one source**, so every acceptance test here runs on a two-source
  fixture. That is honest, and it is why Task 8's live check is a separate step.

---

### Task 1: The identifier-link governance route

**Files:**
- Modify: `src/featuregen/api/routes/governance.py`
- Test: `tests/featuregen/api/routes/test_governance_bridges.py`

**Interfaces:**
- Consumes: `propose_bridge` (`bridge_propose.py:28`), `confirm_fact`, `fold_overlay_state`,
  `load_fact`, `project_verified_bridge(conn, ref, *, now) -> "projected"|"pending"`,
  `demote_bridge_edges(conn, fact_key) -> int`.
- Produces: `GET /sources/{source}/governance/identifier-links`,
  `POST /governance/identifier-links/{fact_key}/confirm`, `.../reject`.

**Why first.** There is no route anywhere in `api/routes/` to propose, confirm or reject a bridge —
every VERIFIED bridge that exists anywhere came from a fixture or a script. Since production
materialization now REQUIRES a verified link (roadmap §4), this route is the only way a
cross-catalog feature can ever become production-eligible — and the only way to reject a wrong one.
Nothing else here is safe to ship without it.

- [ ] **Step 1: Write the failing tests**

```python
def test_a_link_can_be_listed_confirmed_and_becomes_operational(client, two_catalogs):
    """The gap this closes: today there is no verb anywhere that can produce a VERIFIED bridge."""
    listed = client.get(f"/sources/{SRC}/governance/identifier-links", headers=_admin()).json()
    key = listed["links"][0]["fact_key"]
    r = client.post(f"/governance/identifier-links/{key}/confirm", json={"note": "same CIF"},
                    headers=_admin())
    assert r.status_code == 200
    assert r.json() == {"governance_status": "VERIFIED", "operational_projection": "projected"}


def test_rejecting_a_link_demotes_the_projection_and_unpublished_artifacts(client, two_catalogs):
    """The correction mechanism, honestly scoped: the link and anything UNPUBLISHED resting on it.
    Correcting already-PUBLISHED numbers needs artifact lineage that does not exist — which is the
    second reason production materialization requires a VERIFIED link."""
    key = _proposed_key(client)
    assert client.post(f"/governance/identifier-links/{key}/reject", json={"note": "different scheme"},
                       headers=_admin()).status_code == 200
    assert _edge_rows(key) == 0


def test_the_route_requires_a_confirmer(client, two_catalogs):
    key = _proposed_key(client)
    assert client.post(f"/governance/identifier-links/{key}/confirm", json={},
                       headers=_h("feature_engineer")).status_code == 403


def test_the_proposer_cannot_be_the_confirmer(client, two_catalogs):
    """Four-eyes holds because the proposer is the service actor. Pinned so a future change that
    lets a human both propose and confirm fails here."""
    key = _proposed_key(client)
    assert client.post(f"/governance/identifier-links/{key}/confirm", json={},
                       headers=_service_actor()).status_code in (403, 409)


def test_an_unknown_fact_key_404s_without_confirming_existence_of_hidden_endpoints(client):
    assert client.post("/governance/identifier-links/nope/confirm", json={},
                       headers=_admin()).status_code == 404
```

- [ ] **Step 2: Run to verify they fail.** Expected: 404 on every route.

- [ ] **Step 3: Implement**, following `confirm_table_fact` (`governance.py:262-292`) exactly:
load context or 404; build a `confirm_fact` Command with an idempotency key; **return** a 409
JSONResponse on denial rather than raising, so the commit persists the `COMMAND_DENIED` audit row
and releases the advisory lock (audit finding I-3); then project on VERIFIED.

Record in the docstring what the review verified: the confirmation rule is **one platform admin**,
because every shipped `CatalogAdapter.owner_of` returns `None` (`upload_catalog.py:65-66,101-102`),
so `Authority.dual` never engages and "endpoint owner" authority does not exist. Do not describe
this route as four-eyes-by-owner; it is not.

- [ ] **Step 4: Run to verify they pass.**

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat(governance): identifier-link propose/confirm/reject route"
```

---

### Task 2: `ground_bridge` — deterministic corroboration

**Files:**
- Create: `src/featuregen/overlay/upload/attest/bridge_grounding.py`
- Test: `tests/featuregen/overlay/upload/attest/test_bridge_grounding.py`

**Interfaces:**
- Consumes: `attest.grounding.GroundingV1(checks, coverage, conflict)` and `_name_tokens` — reused,
  not redefined, so `fuse` accepts the result unchanged.
- Produces: `ground_bridge(conn, candidate, *, roles) -> GroundingV1`.

Checks, each `agree | disagree | absent`: `type_family` (carrying `type_basis`), `entity_link`,
`domain_compatible`, `name_tokens`, `definition_overlap`, `synonym_overlap`, `grain_role`,
`uniqueness`, `taxonomy_alignment`.

- [ ] **Step 1: Write the failing tests**

```python
def test_matching_definitions_and_domain_corroborate(db):
    g = ground_bridge(db, _candidate(db, "cif_id", "customer_no"))
    assert g.checks["definition_overlap"] == "agree"
    assert g.coverage > 0 and not g.conflict


def test_neither_side_being_a_key_is_not_a_conflict(db):
    """An FK-to-FK link is legitimate: `transactions.customer_id` bridging to another catalog's
    customer reference is a real namespace link and neither endpoint is its table's key."""
    g = ground_bridge(db, _candidate(db, "cif_id", "cp_cif_id"))
    assert g.checks["grain_role"] == "absent"
    assert not g.conflict


def test_a_missing_taxonomy_is_absent_never_disagree(db):
    """Taxonomy is OPTIONAL. Most catalogs carry none, and absence must not look like evidence
    against — it may only lower coverage, which is what `fuse` already does with it.

    NOTE (corrected 2026-07-28): BIAN/FIBO ARE persisted, in `field_evidence` (114 rows each on the
    deployment), and `attest/grounding.py` already implements `path_agreement` over
    `(bian_path, fibo_path, business_term)`. `taxonomy_alignment` EXTENDS that check to a column
    PAIR; it does not build a second one, and nothing needs persisting first."""
    g = ground_bridge(db, _candidate(db, "cif_id", "customer_no"))     # neither side has BIAN/FIBO
    assert g.checks["taxonomy_alignment"] == "absent"
    assert not g.conflict


def test_contradictory_taxonomy_on_both_sides_is_a_conflict(db):
    _set_taxonomy(db, "cif_id", bian="Customer Management")
    _set_taxonomy(db, "customer_no", bian="Payment Execution")
    assert ground_bridge(db, _candidate(db, "cif_id", "customer_no")).conflict


def test_uniqueness_is_absent_when_unprofiled(db):
    """FTR is a glossary upload with no dataset behind it, so the profiler has never run. This is
    the live case, not an edge case."""
    assert ground_bridge(db, _candidate(db, "cif_id", "customer_no")).checks["uniqueness"] == "absent"
```

- [ ] **Step 2: Run to verify they fail.** Expected: import error.

- [ ] **Step 3: Implement.** One read-scoped query loading both endpoints' row; build the checks
dict; `coverage` = fraction of checks that are not `absent`; `conflict` on a hard contradiction
(disjoint type families, contradictory BIAN level 1 where both sides have one).

- [ ] **Step 4: Run to verify they pass.**

- [ ] **Step 5: Commit**

---

### Task 3: The two-lens critic panel

**Files:**
- Create: `src/featuregen/overlay/upload/attest/bridge_critic.py`
- Test: `tests/featuregen/overlay/upload/attest/test_bridge_critic.py`

**Interfaces:**
- Consumes: `intake.llm.drive_structured_call(client, request, validate_output, *, repair_budget,
  retry_budget) -> StructuredCallOutcome`; `DEFAULT_LLM_MODEL`; the `FakeLLM` provider for tests.
- Produces: `BridgeCriticPanelV1(meaning, population, panel_outcome, reasons)` where
  `panel_outcome ∈ {admit, suppress, critics_disagree}`; `run_panel(conn, candidate, *, client, roles)`.

**The two lenses.** *Meaning*: do these denote the same **kind** of identifier? *Population*: do they
identify the same **set of real-world things**? A bank customer and a cardholder are both "customers"
and are not the same population. Two identical critics cost double and catch nothing extra.

**Envelope** (bounded, sanitized, metadata only — no sample values, no raw logical refs): per side,
column/table/schema name, business definition (bounded, sample-value clauses stripped), concept,
entity_link, domain, declared_type, type_basis, synonyms, and taxonomy when present.

**Response** (closed vocabulary, schema-validated): `verdict ∈ {same_namespace, different_namespace,
insufficient_evidence}`, `reasons[]`, `driving_evidence[]`.

- [ ] **Step 1: Write the failing tests**

```python
def test_both_yes_admits(db, fake_llm): ...
def test_one_no_suppresses(db, fake_llm): ...
def test_insufficient_evidence_suppresses(db, fake_llm): ...
def test_disagreement_is_its_own_outcome(db, fake_llm):
    """Not folded into rejection: disagreement marks where a human is worth most, and it is the
    natural top of the review queue."""
    panel = run_panel(db, _candidate(db), client=fake_llm(meaning="same_namespace",
                                                          population="different_namespace"))
    assert panel.panel_outcome == "critics_disagree"


def test_a_failed_call_is_not_unanimity(db, fake_llm):
    panel = run_panel(db, _candidate(db), client=fake_llm(meaning="same_namespace", population=None))
    assert panel.panel_outcome == "suppress"


def test_the_critic_is_never_told_the_pair_was_proposed(db, fake_llm):
    """Blindness, per ColumnContext's rule. Asserted on the captured prompt, because a critic shown
    the proposed answer agrees with it and the failure is invisible in the output."""
    captured = fake_llm.capture()
    run_panel(db, _candidate(db), client=captured)
    for prompt in captured.prompts:
        assert "proposed" not in prompt.lower() and "confidence" not in prompt.lower()


def test_no_sample_values_or_raw_refs_egress(db, fake_llm):
    captured = fake_llm.capture()
    _set_definition(db, "cif_id", "CIF id, representative values such as 84848368; 84469024")
    run_panel(db, _candidate(db), client=captured)
    assert "84848368" not in " ".join(captured.prompts)


def test_an_off_vocabulary_verdict_suppresses(db, fake_llm): ...
```

- [ ] **Step 2: Run to verify they fail.**

- [ ] **Step 3: Implement.** Dispatch the two lenses **independently** — neither sees the other's
verdict — so the opinions stay uncorrelated. Ask each to **refute** ("give the strongest reason these
are NOT the same namespace"), with `insufficient_evidence` stated as expected and unpenalised.

- [ ] **Step 4: Run to verify they pass.**

- [ ] **Step 5: Commit**

---

### Task 4: Deterministic replay for the panel

**Files:**
- Create: migration `10XX_llm_selection_result.sql` (resolve the number against main at
  implementation time; 1032 is taken)
- Modify: `src/featuregen/overlay/upload/attest/bridge_critic.py`
- Test: `tests/featuregen/overlay/upload/attest/test_bridge_critic_replay.py`

**Why this is a prerequisite, not a nicety.** `llm_dispatch` persists `redacted_input` and **no
response** (`1005_llm_dispatch_provenance.sql:19-34`); its key is per-attempt by design; and
`find_llm_call` (`intake/llm.py:409-453`) has **zero production callers** and is `run_id`-scoped.
Without a content-addressed store, re-deriving candidates admits a different set on identical
inputs and Task 8's shadow measurement means nothing.

- [ ] **Step 1: Write the failing tests**

```python
def test_the_same_pair_and_metadata_replays_without_a_second_call(db, counting_llm):
    run_panel(db, _candidate(db), client=counting_llm)
    run_panel(db, _candidate(db), client=counting_llm)
    assert counting_llm.calls == 2, "two lenses, one dispatch each — the replay makes no new call"


def test_changed_endpoint_metadata_is_a_new_selection(db, counting_llm):
    run_panel(db, _candidate(db), client=counting_llm)
    _set_definition(db, "cif_id", "a materially different definition")
    run_panel(db, _candidate(db), client=counting_llm)
    assert counting_llm.calls == 4


def test_a_transient_failure_retries_under_the_same_key(db, flaky_llm): ...
def test_an_explicit_re_evaluation_mints_a_new_revision_and_keeps_both(db, counting_llm): ...
```

- [ ] **Step 2-4:** implement a content-addressed key over `(candidate_id, both endpoints' metadata
fingerprint, lens, prompt/schema/model versions)` — not a minted id, not `run_id`-scoped — persist
the validated response body and hash, wire a reuse probe.

- [ ] **Step 5: Commit**

---

### Task 5: Admission — wire the stages together

**Files:**
- Create: `src/featuregen/overlay/upload/bridge_admission.py`
- Test: `tests/featuregen/overlay/upload/test_bridge_admission.py`

**Interfaces:**
- Consumes: `derive_bridge_candidates`, `ground_bridge`, `run_panel`, `attest.fusion.fuse`,
  `propose_bridge`.
- Produces: `admit_bridge_candidates(conn, *, client, roles, actor) -> AdmissionReport` with
  `admitted`, `suppressed`, `disagreed`, each carrying its reasons.

- [ ] **Step 1: Write the failing tests**

```python
def test_an_admitted_candidate_becomes_a_proposed_fact(db, fake_llm): ...
def test_a_suppressed_candidate_is_retained_as_audit_evidence_not_deleted(db, fake_llm): ...
def test_a_grounding_conflict_suppresses_regardless_of_the_panel(db, fake_llm):
    """`fuse`'s existing contract: a hard deterministic contradiction caps confidence whatever the
    critics said. Pinned here because it is the one place a confident model could otherwise win."""


def test_pre_checks_reject_before_any_model_call(db, counting_llm):
    _same_source_pair(db)
    admit_bridge_candidates(db, client=counting_llm, roles=(), actor=_svc())
    assert counting_llm.calls == 0


def test_confidence_never_reaches_operational_eligibility(db, fake_llm):
    """A mutation raising eligibility from confidence must fail this."""
```

- [ ] **Step 2-4:** implement; pre-checks first (no model call), then grounding, panel, fusion.

- [ ] **Step 5: Commit**

---

### Task 6: Proposed links reach the planner

**Files:**
- Modify: `src/featuregen/overlay/upload/bridge_projection.py` (`project_verified_bridge`
  → admit PROPOSED; `active_bridges` → return both, carrying `status`)
- Modify: the 12 `active_bridges` consumers as needed
- Test: `tests/featuregen/overlay/upload/planner/test_proposed_bridges_reach_the_planner.py`

**The fingerprint trap.** `fact_key`s are hashed into plan identity. The key goes in as today;
**`status` stays out of every hash**, so confirming a link does not invalidate existing plans.

- [ ] **Step 1: Write the failing tests**

```python
def test_a_proposed_link_is_visible_to_the_planner(db): ...
def test_confirming_a_link_does_not_change_any_plan_fingerprint(db):
    """THE trap. If `status` entered the hash, every plan resting on a link would be invalidated the
    moment a human confirmed it — the same shape operand_roles hit."""
    before = _plan_fingerprint(db)
    _confirm(db, key)
    assert _plan_fingerprint(db) == before


def test_a_rejected_link_disappears_from_the_active_set(db): ...
def test_the_active_set_carries_status(db): ...
```

- [ ] **Step 2-4:** implement.

- [ ] **Step 5: Commit**

---

### Task 7: `JOIN_IDENTITY_UNCONFIRMED` — the provenance loop

**Files:**
- Modify: `src/featuregen/overlay/upload/feature_assist.py` (`REQUIREMENT_CODES`)
- Modify: `src/featuregen/overlay/upload/validation_requirements.py` (versioned registry)
- Test: `tests/featuregen/overlay/upload/test_join_identity_requirement.py`

The requirement serializes onto the governed contract through
`contract/_serial.py::requirements_to_json` and travels onto the materialized artifact beside
`access_requirements` — the pattern `materialize/classify.py:40` already states as *"the requirement
travels with the artifact"*.

- [ ] **Step 1: Write the failing tests**

```python
def test_a_feature_on_a_proposed_link_is_created_and_flagged(db):
    """The directive: created, not blocked. NEEDS_EXTERNAL_VALIDATION plus a requirement naming the
    link — the E4a unit loop applied to identity."""
    idea = _generate_cross_catalog_feature(db)
    assert idea.validation_status == "NEEDS_EXTERNAL_VALIDATION"
    assert any(r.code == "JOIN_IDENTITY_UNCONFIRMED" for r in idea.requirements)


def test_confirming_the_link_clears_the_flag(db): ...
def test_rejecting_the_link_demotes_the_feature(db): ...
def test_the_requirement_survives_contract_serialization(db): ...
def test_it_reaches_the_materialized_artifact(db): ...
def test_a_feature_on_a_verified_link_carries_nothing(db):
    """The control — otherwise the flag is noise, and noise gets ignored."""
```

- [ ] **Step 2-4:** implement.

- [ ] **Step 5: Commit**

---

### Task 8: Shadow measurement, then the gate

> **ORDERING — read before Task 5.** The critic runs in SHADOW first and gates nothing. Task 5 must
> not suppress a candidate until M8/M9 are published and a threshold has been chosen. Implement
> grounding (Task 2) → run the critic in shadow (this task) → publish M8/M9 → decide thresholds and
> risk routing → only then enable admission gating in Task 5.

**Files:**
- Modify: `src/featuregen/overlay/upload/attest/runner.py`, `report.py`
- Test: `tests/featuregen/overlay/upload/attest/test_bridge_shadow.py`

**The panel does not gate until its numbers are published.** Two measures:

- **M8 — rejection rate.** A critic that never says no is theatre: it adds cost and false assurance.
  If it approves everything in shadow, it does not gate.
- **M9 — agreement with human confirm/reject decisions.**

- [ ] **Step 1: Write the failing tests** — the runner records verdicts and suppresses nothing;
the report emits M8 and M9; enabling the gate before the report exists is refused.

- [ ] **Step 2-4:** implement.

- [ ] **Step 5:** run in shadow, publish M8/M9 into the verified-interfaces reference §8.

- [ ] **Step 6: Commit**

---

### Task 9: The review surface

**Files:**
- Modify: `frontend/src/screens/GovernanceReviewScreen.tsx` (new "Identifier links" tab)
- Test: `frontend/src/screens/GovernanceReviewScreen.test.tsx`

- [ ] Render pending links with both endpoints, the grounding checks, both critic verdicts and
  reasons, and `type_basis` — a **declared** type is someone's spreadsheet entry, not a read of the
  physical schema, and the confirmer must see which they are trusting.
- [ ] Sort `critics_disagree` to the top: highest human value.
- [ ] Confirm and reject wired to Task 1's route.
- [ ] Run the changed file directly with vitest (the full suite hangs on worker-start here).

---

## Acceptance (Phase E0b)

- A `string`↔`string` identifier pair across two sources is admitted, reaches the planner as
  `proposed`, and a feature built on it is created and carries `JOIN_IDENTITY_UNCONFIRMED` naming
  the link. It is available for discovery and sandbox analysis; **production materialization refuses
  it** until the link is VERIFIED.
- Confirming clears the requirement on every feature resting on it; rejecting demotes the link and
  every **unpublished** artifact resting on it.
- **NOT claimed: correcting already-published outputs.** Deleting the projection row does not undo a
  published number. That needs artifact-to-fact lineage, a publication pointer, a transactional
  invalidation, a rule for whether the old artifact becomes unavailable or merely invalid, and
  cache/model-input invalidation — none of which exists. This is the second reason production
  materialization requires a VERIFIED link.
- A split panel is suppressed and surfaced as `critics_disagree`.
- A failed critic call suppresses.
- A grounding conflict suppresses regardless of the panel.
- A pair with no taxonomy on either side reaches the **same admission outcome** as one with matching
  taxonomy, differing only in confidence.
- Confirming a link changes no existing plan's fingerprint.
- Confidence never changes `operational_eligibility`.
- Re-running derivation on unchanged inputs makes no second provider call.
- **M8 and M9 published before the panel gates anything.**

## Deferred NFRs

Caching and incremental re-derivation; bulk review; confidence calibration dashboards; critic
latency and cost controls; multi-hop component analysis over proposed links.
