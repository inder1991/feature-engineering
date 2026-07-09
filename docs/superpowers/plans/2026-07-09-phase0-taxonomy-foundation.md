# Phase 0 — Governed Taxonomy Foundation — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: subagent-driven-development. Steps use `- [ ]` checkboxes.

**Goal:** Land the governed use-case taxonomy, the supporting dimension registries, the 107-legacy-tag crosswalk, and a derived per-recipe applicability mapping for all 153 recipes — **behaviour-neutral** (grounding still grounds everything; nothing user-visible changes).

**Architecture:** New read-only registry modules under `src/featuregen/overlay/upload/taxonomy/`, mirroring the `concepts.py` pattern (frozen dataclass + `_ALL` tuple + registry dict + import-time `_validate_registry()`). Recipe applicability is *derived* from the legacy `Template.use_cases` tags via the crosswalk + a small per-recipe override table — so **`templates.py` is not modified** and grounding is untouched.

**Tech stack:** Python 3.12, `uv run pytest -q`, `uv run ruff check src tests`, `uv run mypy src`. Source of truth for content: `docs/superpowers/specs/2026-07-09-usecase-taxonomy-crosswalk-draft.md` (v2).

## Global Constraints

- **Behaviour-neutral.** Do NOT modify `templates.py`, `gate1.py`, `feature_assist.py`, or grounding. The existing `Template.use_cases` tuples and `ground_all(use_case=…)` keep working unchanged.
- **Closed vocabularies.** Every taxonomy ID comes from spec v2 §3 (use-cases) and §1 (other dimensions). No invented IDs.
- **Ownership is metadata, never tree identity** (spec §0).
- **Intentionally-empty leaves are valid** (spec §3 `*` leaves) — validation flags them with an explanation, it does not fail on them.
- **Additive + typed.** New modules only; `mypy` clean on new files; `ruff` clean.
- Recipe id lists for the hard cases are fixed (from querying the 153 recipes) and appear verbatim in Task 4.

---

## Task 1: Use-case taxonomy registry

**Files:**
- Create: `src/featuregen/overlay/upload/taxonomy/__init__.py`
- Create: `src/featuregen/overlay/upload/taxonomy/use_cases.py`
- Test: `tests/featuregen/overlay/upload/taxonomy/test_use_cases.py`

**Interfaces — Produces:**
- `@dataclass(frozen=True, slots=True) class UseCase: id: str; parent: str | None; display_name: str; description: str; selectable: bool = True; intentionally_empty: bool = False; include_examples: tuple[str, ...] = (); exclude_examples: tuple[str, ...] = ()`
- `USE_CASE_REGISTRY: dict[str, UseCase]`, `use_case(id) -> UseCase | None`, `is_known_use_case(id) -> bool`, `ancestors(id) -> tuple[str, ...]`, `descendants(id) -> tuple[str, ...]`, `selectable_leaves() -> tuple[str, ...]`.
- Import-time `_validate_registry()` — every `parent` resolves; no duplicate id; a non-`selectable` node must have children; ids are dot-paths whose prefix equals `parent`.

- [ ] **Step 1: Failing test** — assert `use_case("financial_crime").selectable is False`; `use_case("fraud").parent == "financial_crime"`; `ancestors("customer.relationship_attrition.primacy_loss") == ("customer", "customer.relationship_attrition")`; `"customer.relationship_attrition.deposit_attrition"` exists; `use_case("pricing.fee_pricing").intentionally_empty is True`; every parent in the registry resolves; `USE_CASE_REGISTRY` has no dup ids.

```python
from featuregen.overlay.upload.taxonomy.use_cases import USE_CASE_REGISTRY, use_case, ancestors

def test_domain_parent_not_selectable():
    assert use_case("financial_crime").selectable is False
    assert use_case("fraud").parent == "financial_crime"

def test_hierarchy_resolves():
    assert ancestors("customer.relationship_attrition.primacy_loss") == (
        "customer", "customer.relationship_attrition")
    for uc in USE_CASE_REGISTRY.values():
        assert uc.parent is None or uc.parent in USE_CASE_REGISTRY

def test_declared_future_leaf_flagged():
    assert use_case("pricing.fee_pricing").intentionally_empty is True
```

- [ ] **Step 2: Run — expect fail** (`ModuleNotFoundError`).
- [ ] **Step 3: Implement** `use_cases.py` — author the full §3 tree (every node in the v2 hierarchy, parents first), with `selectable=False` on `financial_crime`, `intentionally_empty=True` on the nine `*` leaves, and `include_examples`/`exclude_examples` on at least the branch heads and the funnel-boundary leaves (`credit.early_warning`, `customer.relationship_attrition.deposit_attrition`, `treasury_alm.deposit_runoff_forecasting`, `fraud.transaction_fraud_detection`, `aml_cft.suspicious_transaction_monitoring`). Implement helpers + `_validate_registry()`; call it at import.
- [ ] **Step 4: Run — expect pass.**
- [ ] **Step 5: Gates + commit** — `uv run pytest tests/featuregen/overlay/upload/taxonomy/test_use_cases.py -q`, `uv run ruff check src tests`, `uv run mypy src/featuregen/overlay/upload/taxonomy/use_cases.py`. Commit `feat(taxonomy): governed use-case registry (phase 0 task 1)`.

---

## Task 2: Supporting dimension registries

**Files:**
- Create: `src/featuregen/overlay/upload/taxonomy/dimensions.py`
- Test: `tests/featuregen/overlay/upload/taxonomy/test_dimensions.py`

**Interfaces — Produces:** five frozensets + accessors — `MODELLING_CONTEXTS`, `PRODUCT_CONTEXTS`, `TYPOLOGIES`, `JOURNEY_STAGES`, `BUSINESS_OUTCOMES` (members per spec §1), and `DIMENSIONS: dict[str, frozenset[str]]`; `is_known(dimension, value) -> bool`.

- [ ] **Step 1: Failing test** — `"ifrs9" in MODELLING_CONTEXTS`; `"crypto_assets" in PRODUCT_CONTEXTS`; `"crypto_asset_laundering" in TYPOLOGIES`; `"unbundling" in JOURNEY_STAGES`; `"cost_efficiency" in BUSINESS_OUTCOMES`; the five sets are pairwise disjoint.
- [ ] **Step 2: Run — expect fail.**
- [ ] **Step 3: Implement** the five closed vocabularies from spec §1 + §5 (modelling_context includes `xva`,`lgd`; measure is separate — see note) + validation asserting disjointness.
- [ ] **Step 4: Run — expect pass.**
- [ ] **Step 5: Gates + commit** `feat(taxonomy): supporting dimension registries (phase 0 task 2)`.

---

## Task 3: Legacy-tag crosswalk (all 107 tags)

**Files:**
- Create: `src/featuregen/overlay/upload/taxonomy/legacy_crosswalk.py`
- Test: `tests/featuregen/overlay/upload/taxonomy/test_legacy_crosswalk.py`

**Interfaces — Produces:** `LEGACY_TAG_CROSSWALK: dict[str, CrosswalkEntry]` where `CrosswalkEntry` = `{dimension: str, target: str, status: Literal["mapped","merged","deprecated"]}`, covering **every one of the 107 tags** (spec v2 §5 + the v1 compact crosswalk for the clean ~84). `crosswalk(tag) -> CrosswalkEntry | None`.

- [ ] **Step 1: Failing test** — the headline coverage guarantee: gather every tag on every recipe and assert all are covered, and every `use_case`-dimension target resolves in `USE_CASE_REGISTRY`, and no framework/measure/typology tag maps to the `use_case` dimension.

```python
from featuregen.overlay.upload.templates import ALL_TEMPLATES
from featuregen.overlay.upload.taxonomy.legacy_crosswalk import LEGACY_TAG_CROSSWALK, crosswalk
from featuregen.overlay.upload.taxonomy.use_cases import USE_CASE_REGISTRY

def test_every_legacy_tag_is_covered():
    tags = {uc for t in ALL_TEMPLATES for uc in t.use_cases}
    missing = tags - set(LEGACY_TAG_CROSSWALK)
    assert not missing, missing

def test_use_case_targets_resolve():
    for tag, e in LEGACY_TAG_CROSSWALK.items():
        if e["dimension"] == "use_case":
            assert e["target"] in USE_CASE_REGISTRY, (tag, e["target"])

def test_frameworks_left_use_case_dimension():
    for tag in ("ifrs9_staging", "frtb", "xva", "lgd", "lcr", "nsfr"):
        assert crosswalk(tag)["dimension"] != "use_case"
```

- [ ] **Step 2: Run — expect fail.**
- [ ] **Step 3: Implement** the crosswalk for all 107 tags per spec v2 §5 (dimension + target + status). Reclassify frameworks/measures/typologies/contexts out of `use_case`; mark merges/deprecations.
- [ ] **Step 4: Run — expect pass.**
- [ ] **Step 5: Gates + commit** `feat(taxonomy): 107-tag legacy crosswalk (phase 0 task 3)`.

---

## Task 4: Per-recipe applicability (derive + overrides)

**Files:**
- Create: `src/featuregen/overlay/upload/taxonomy/recipe_applicability.py`
- Test: `tests/featuregen/overlay/upload/taxonomy/test_recipe_applicability.py`

**Interfaces — Produces:** `@dataclass(frozen=True, slots=True) class ApplicabilitySpec: primary: str; secondary: tuple[str, ...] = (); supporting: tuple[str, ...] = (); product_context: tuple[str, ...] = (); typology: tuple[str, ...] = (); journey_stage: tuple[str, ...] = (); business_outcome: tuple[str, ...] = ()` and `recipe_applicability(template) -> ApplicabilitySpec`.

**Derivation rule:** map the recipe's legacy `use_cases` through the crosswalk → the use-case leaves; the **primary** is the leaf of the recipe's first tag (convention), unless an override applies. Non-primary use-case leaves become `secondary`. Non-use-case tags route to their dimension field.

**Override table (verbatim — these recipes cannot be derived from the tag alone):**
- `transaction_monitoring` split by family → **primary**:
  - `fraud.transaction_fraud_detection` for: `card_testing_velocity, device_sharing_velocity, new_device_flag, geo_velocity_impossible, first_time_payee_high_value, merchant_risk_anomaly, txn_velocity_spike, amount_zscore_spike, cross_channel_rail_anomaly, cross_border_burst, amount_just_under_limit`
  - `aml_cft.suspicious_transaction_monitoring` for: `structuring_smurfing, cash_intensity_ratio, rapid_movement_passthrough, round_amount_ratio, fan_in_fan_out, high_risk_corridor_exposure, nested_correspondent_flow, crypto_offramp_exposure, dormant_reactivation, screening_exposure, prior_alert_recidivism`
- `crypto_offramp_exposure` → also `product_context=("crypto_assets",)`, `typology=("crypto_asset_laundering",)`
- `salary_signal`, `external_own_transfer_trend` → add secondary `customer.relationship_attrition.primacy_loss`; `external_own_transfer_trend` → also secondary `wealth.asset_outflow`
- concentration recipes (`rate_sensitive_concentration, book_desk_concentration, sukuk_concentration, syndication_concentration, group_exposure_aggregation, guarantor_reliance`) → primary `portfolio_risk.concentration`
- `notional_netting_exposure` → primary `counterparty_risk.exposure_monitoring`; `margin_call_intensity` → primary `counterparty_risk.margin_call_risk`; `benchmark_basis_dislocation` → primary `markets.market_risk.basis_risk`
- `dd_cancellation_rate` → `journey_stage=("unbundling",)`; `right_party_contact_intensity` → `business_outcome` unaffected, keep primary collections (contactability→metadata, not a use-case); `cost_to_collect_ratio` → `business_outcome=("cost_efficiency",)`

- [ ] **Step 1: Failing test** — every recipe resolves to a spec with a **selectable** primary; the 22 transaction_monitoring recipes split correctly by family; `crypto_offramp_exposure` carries the crypto context+typology; `external_own_transfer_trend` carries both primacy-loss and wealth secondaries.

```python
def test_all_recipes_have_selectable_primary():
    from featuregen.overlay.upload.templates import ALL_TEMPLATES
    from featuregen.overlay.upload.taxonomy.use_cases import use_case
    for t in ALL_TEMPLATES:
        spec = recipe_applicability(t)
        assert use_case(spec.primary) and use_case(spec.primary).selectable, t.id

def test_transaction_monitoring_split_by_family():
    assert _spec("txn_velocity_spike").primary == "fraud.transaction_fraud_detection"
    assert _spec("structuring_smurfing").primary == "aml_cft.suspicious_transaction_monitoring"
```

- [ ] **Step 2: Run — expect fail.**
- [ ] **Step 3: Implement** the derivation + override table.
- [ ] **Step 4: Run — expect pass.**
- [ ] **Step 5: Gates + commit** `feat(taxonomy): per-recipe applicability mapping (phase 0 task 4)`.

---

## Task 5: Phase-0 validation suite + coverage report

**Files:**
- Create: `tests/featuregen/overlay/upload/taxonomy/test_phase0_exit_criteria.py`
- Create: `src/featuregen/overlay/upload/taxonomy/coverage.py` (a `coverage_report() -> dict` used by the test and callable for humans)

**Interfaces — Produces:** `coverage_report()` returning `{leaf_id: [recipe_id, ...]}` for every selectable leaf, plus `empty_intentional` and `empty_unexpected` lists.

- [ ] **Step 1: Failing test** — the exit criteria: 153/153 recipes map; every recipe has a selectable primary; every non-intentionally-empty selectable leaf has ≥1 recipe (`empty_unexpected == []`); every intentionally-empty leaf indeed has 0 recipes; no `use_case`-dimension crosswalk target is a framework/measure/typology.
- [ ] **Step 2: Run — expect fail.**
- [ ] **Step 3: Implement** `coverage.py` (invert `recipe_applicability` over `ALL_TEMPLATES` against `selectable_leaves()`), then satisfy the criteria.
- [ ] **Step 4: Run — expect pass**, and run the **full** overlay/contract/governance suite to confirm behaviour-neutrality (`uv run pytest tests/featuregen/overlay/ tests/featuregen/api/test_contract.py tests/featuregen/governance/ -q`).
- [ ] **Step 5: Gates + commit** `test(taxonomy): phase-0 exit-criteria + coverage report (phase 0 task 5)`.

---

## Task 6: Governance contract

**Files:**
- Create: `docs/superpowers/specs/2026-07-09-taxonomy-governance-contract.md`

- [ ] **Step 1:** Write the contract: taxonomy owner, mapping approver, version-bump rules (semver on the registry), deprecation/alias process + backward-compatibility period, unknown/retired-ID resolution, review cadence. Reference the intentionally-empty-leaf policy.
- [ ] **Step 2: Commit** `docs(taxonomy): governance contract (phase 0 task 6)`.

---

## Self-review

- Spec coverage: Tasks 1–4 build every dimension in spec §1 and the tree in §3; Task 3 covers all 107 tags; Task 4 encodes every D3/D6/D7 hard case; Task 5 enforces the Phase-0 exit criteria and behaviour-neutrality. Task 6 is the governance contract the spec requires.
- Behaviour-neutrality: no task touches `templates.py`/grounding; applicability is derived, not stamped onto recipes.
- Type consistency: `ApplicabilitySpec.primary` is a use-case id validated against `USE_CASE_REGISTRY`; `crosswalk` targets in the `use_case` dimension resolve to the same registry.
