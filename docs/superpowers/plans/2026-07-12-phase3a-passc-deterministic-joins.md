# Phase 3A — Pass C Deterministic Governed Joins — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Deterministically discover single-column join candidates from the uploaded glossary, file each strong candidate as a governed `approved_join` **proposal**, let a human confirm it (governance-fallback), and project a confirmed join into an operational `graph_edge` that `find_join_path` traverses — so relationships become known, trustworthy, and usable. No LLM (that is Phase 3B).

**Architecture:** ~85% reuse. The entire `approved_join` propose → dual-owner-confirm → expiry → feature-gen-reads-only-operational spine already exists (`overlay/proposal_commands.py`, `join_confirmation.py`, `authority.py`, `expiry.py`, `resolve.py`, `join_path.py`). Phase 3A adds a **pure deterministic candidate producer** (blocker + namespace classifier + scorer), a **dedupe lifecycle**, **propose wiring**, a **reverse projector** (confirmed fact → operational edge), and a **relationship readiness** dimension. Design spec: `docs/superpowers/specs/2026-07-12-phase3-passc-governed-joins-design.md` (v2).

**Tech Stack:** Python 3.12, `uv`, psycopg 3, PostgreSQL (ephemeral PG via `postgresql_proc`), pytest.

## Global Constraints

- **Deterministic-first, no LLM in 3A.** Candidate generation is pure rules + scoring. No LLM client, no egress.
- **No operational join without human confirmation.** Pass C only ever appends `OVERLAY_FACT_PROPOSED`; only `confirm_fact` (human) makes it VERIFIED; only a VERIFIED fact projects to an operational edge.
- **Default-OFF.** Gated behind env `OVERLAY_PASS_C` (default `0`). Flag-off ⇒ `ingest_upload` byte-for-byte today's behaviour. (Phase-3B flags `OVERLAY_PASS_C_LLM_CHALLENGER`/`OVERLAY_PASS_C_EXPLORATION` are **not** built here.)
- **Fail-soft.** Pass C never aborts an upload. The LLM-free discovery + propose block is savepointed + `except`, mirroring the Phase-2 enrichment stages (`ingest.py:596-614`); the end-of-ingest projector is savepointed like Phase-2's `project_table_facts`.
- **Migration slot:** last used is `0986` (Phase 2). Phase 3A uses **`0987`**. Verify free before writing.
- **Cardinality enum is `1:1 | 1:N | N:1`** (`facts.py:103`) — there is **no `N:M`**. A many-to-many candidate is flagged/lowered, never proposed as a direct join.
- **Confirmation authority:** with `owner_of → None` both sides route to the platform-admin governance queue; `Authority.dual` collapses to False ⇒ single-confirmer path; four-eyes is satisfied because the proposer is the Pass C **service actor** (reuse `_ENRICH_ACTOR`) ≠ the human confirmer. Label `confirmation_mode = governance_fallback_single_confirmer`.
- Reuse types verbatim: `ApprovedJoinRef(from_ref, to_ref, column_pairs: tuple[ColumnPair,...], cardinality: str)`, `ColumnPair(from_col, to_col)`, `CatalogObjectRef(catalog_source, object_kind, schema, table, column=None)` (`overlay/identity.py:9-29`). The `approved_join` value = `{from_ref: asdict, to_ref: asdict, column_pairs: [{from_col,to_col}], cardinality}` (`facts.py:79-106`).
- Tests live under `tests/featuregen/overlay/upload/passc/`. Runner: `uv run pytest <path> -q`.

---

## Reuse Map (verified — do NOT rebuild)

| Need | Status | Home |
|---|---|---|
| `approved_join` fact + value schema + `ApprovedJoinRef`/`ColumnPair` | REUSE | `overlay/facts.py:79-106`; `overlay/identity.py:9-29` |
| Propose a governed join (F4/consistency checks, mints evidence, one task per owner side) | REUSE | `overlay/proposal_commands.py:34` `propose_fact` (via `overlay/commands.py`) |
| Dual/single-owner confirmation (`confirm_fact`), governance-queue routing, four-eyes | REUSE | `overlay/confirmation_commands.py:47`; `overlay/authority.py:92-137` |
| Fact-state read (folded) for dedupe | REUSE | `overlay/store.py` `load_fact` + `overlay/state.py` `fold_overlay_state`; `overlay/identity.py:65` `fact_key(ApprovedJoinRef,'approved_join')` |
| VERIFIED-only fact read for projection | REUSE | `overlay/resolve.py:183` `resolve_fact` (serves only VERIFIED; carries `.provenance['confirmed_event_id']`) |
| Feature-gen traverses only operational join edges | REUSE | `overlay/upload/join_path.py:38` `find_join_path` (`WHERE kind='joins' AND authority='operational'`); `entity.py:224` same filter |
| Governed joins seam (declared edge → display_only when flag on) | REUSE | `overlay/upload/graph.py:16` `governed_joins_enabled` / `:24` `_join_edge_authority` |
| Id-like column detector (name-suffix based) | REUSE/EXTEND | `overlay/upload/entity.py` `_is_id_like(column_name, data_type)` — must be *combined* with `term_type`/concept (misses `FORACID`/`REF_NUM`) |
| Upload-context adapter (un-gates the fact lifecycle) + `_ENRICH_ACTOR` service actor | REUSE | `overlay/upload/upload_catalog.py` `ensure_upload_catalog_adapter`; `overlay/upload/enrich_llm.py:164` `_ENRICH_ACTOR` |
| Fail-soft savepointed stage pattern + end-of-ingest projector pattern | REUSE | `overlay/upload/ingest.py:596-614` (Pass A) and the Phase-2 `project_table_facts` call site |
| Readiness machinery (dimensions, causes, per-table iteration) | EXTEND | `overlay/upload/readiness.py` |
| `graph_edge` (`catalog_source,kind,from_ref,to_ref` PK; `authority` default `'operational'`; `cardinality`) | EXTEND | migrations `0945/0982/0956`; Phase 3A adds fact-link columns (`0987`) |

---

## File Structure

**New (`src/featuregen/overlay/upload/passc/`):**
- `types.py` — enums (`NamespaceCompatibility`, `CardinalityInferenceStatus`), `SignalEvidence`, `JoinCandidateEvidenceV1`, `PassCConfig` + defaults + versions.
- `identifiers.py` — `is_join_key_eligible(col)`, `normalized_identifier_concept(col)`.
- `namespace.py` — `classify_namespace(a, b) -> (NamespaceCompatibility, reason_codes)` + the mixed-leaf registry.
- `candidates.py` — `block_candidates(columns) -> [CandidatePair]`; `score(pair, ctx) -> JoinCandidateEvidenceV1`.
- `lifecycle.py` — `candidate_fingerprint(...)`, `decide_action(conn, ref) -> Action`.
- `propose.py` — `propose_join_candidates(conn, source, evidences, *, actor) -> None`.
- `projection.py` — `project_confirmed_joins(conn, *, source, tables, now=None) -> None`.

**Modified:**
- `overlay/upload/readiness.py` — relationships dimension.
- `overlay/upload/ingest.py` — wire Pass C (behind `OVERLAY_PASS_C`) + end-of-ingest projector + `pass_c_enabled()`.
- `db/migrations/0987_graph_edge_join_authority_links.sql` — fact-link columns.

---

## Task 1: Pass C types + scoring config

**Files:** Create `src/featuregen/overlay/upload/passc/__init__.py`, `.../passc/types.py`; Test `tests/featuregen/overlay/upload/passc/test_types.py`.

**Interfaces — Produces:** `NamespaceCompatibility`, `CardinalityInferenceStatus` (StrEnum); `SignalEvidence`, `JoinCandidateEvidenceV1` (frozen dataclasses); `PassCConfig` with `DEFAULT_CONFIG` (weights, thresholds, negative-filter set, bucket boundaries) + `CONFIG_VERSION`, `ALGORITHM_VERSION`.

- [ ] **Step 1: Write the failing test**
```python
# tests/featuregen/overlay/upload/passc/test_types.py
from featuregen.overlay.upload.passc.types import (
    NamespaceCompatibility, CardinalityInferenceStatus, SignalEvidence,
    JoinCandidateEvidenceV1, DEFAULT_CONFIG, CONFIG_VERSION, ALGORITHM_VERSION)


def test_enums_and_config_defaults():
    assert NamespaceCompatibility.COMPATIBLE == "compatible"
    assert set(NamespaceCompatibility) >= {NamespaceCompatibility.COMPATIBLE,
        NamespaceCompatibility.POSSIBLE, NamespaceCompatibility.AMBIGUOUS,
        NamespaceCompatibility.INCOMPATIBLE}
    assert DEFAULT_CONFIG.strong_threshold == 80 and DEFAULT_CONFIG.weak_threshold == 50
    assert "amount" in DEFAULT_CONFIG.negative_concepts and "date" in DEFAULT_CONFIG.negative_concepts
    assert DEFAULT_CONFIG.weights["same_identifier_concept"] == 40
    assert CONFIG_VERSION and ALGORITHM_VERSION


def test_evidence_is_frozen_and_serializable():
    ev = JoinCandidateEvidenceV1(
        candidate_id="c1", from_ref="src::public.txn.cif_id", to_ref="src::public.cust.cif_id",
        column_pairs=(("cif_id", "cif_id"),), proposed_direction="N:1", proposed_cardinality="N:1",
        cardinality_status=CardinalityInferenceStatus.INFERRED_FROM_CONFIRMED_GRAIN,
        bucket="strong", score=95, positive_signals=(), negative_signals=(),
        namespace_compatibility=NamespaceCompatibility.COMPATIBLE, namespace_reason_codes=("same_identifier_concept",),
        grain_evidence=(), missing_requirements=(), llm_annotations=(), explanation="…",
        producer="deterministic_pass_c", config_version=CONFIG_VERSION,
        candidate_algorithm_version=ALGORITHM_VERSION, source_snapshot_id="snap")
    import dataclasses
    assert dataclasses.asdict(ev)["score"] == 95
```

- [ ] **Step 2: Run → fail** (`ModuleNotFoundError`).

- [ ] **Step 3: Implement `types.py`**
```python
from __future__ import annotations
from dataclasses import dataclass, field
from enum import StrEnum

CONFIG_VERSION = "passc-config-v1"
ALGORITHM_VERSION = "passc-algo-v1"


class NamespaceCompatibility(StrEnum):
    COMPATIBLE = "compatible"; POSSIBLE = "possible"
    AMBIGUOUS = "ambiguous"; INCOMPATIBLE = "incompatible"


class CardinalityInferenceStatus(StrEnum):
    INFERRED_FROM_CONFIRMED_GRAIN = "inferred_from_confirmed_grain"
    MISSING_GRAIN = "missing_grain"
    AMBIGUOUS_BOTH_GRAINS = "ambiguous_both_grains"
    MANY_TO_MANY_RISK = "many_to_many_risk"


@dataclass(frozen=True, slots=True)
class SignalEvidence:
    signal_name: str
    score_delta: int
    evidence_refs: tuple[str, ...]
    explanation: str


@dataclass(frozen=True, slots=True)
class JoinCandidateEvidenceV1:
    candidate_id: str
    from_ref: str
    to_ref: str
    column_pairs: tuple[tuple[str, str], ...]
    proposed_direction: str | None
    proposed_cardinality: str | None
    cardinality_status: CardinalityInferenceStatus
    bucket: str                 # "strong" | "weak" | "suppressed"
    score: int
    positive_signals: tuple[SignalEvidence, ...]
    negative_signals: tuple[SignalEvidence, ...]
    namespace_compatibility: NamespaceCompatibility
    namespace_reason_codes: tuple[str, ...]
    grain_evidence: tuple[str, ...]
    missing_requirements: tuple[str, ...]
    llm_annotations: tuple[str, ...]
    explanation: str
    producer: str
    config_version: str
    candidate_algorithm_version: str
    source_snapshot_id: str


@dataclass(frozen=True, slots=True)
class PassCConfig:
    weights: dict[str, int]
    negative_concepts: frozenset[str]
    strong_threshold: int = 80
    weak_threshold: int = 50
    # BIAN leaves that mix distinct key namespaces (customer+counterparty etc.) -> AMBIGUOUS.
    mixed_bian_leaves: frozenset[str] = frozenset({"customer and counterparty identification"})


DEFAULT_CONFIG = PassCConfig(
    weights={
        "same_identifier_concept": 40, "related_terms_key_link": 50, "same_column_name": 30,
        "same_term_name": 25, "same_entity_tag": 25, "same_bian_leaf": 10, "same_fibo_leaf": 10,
        "compatible_phase2_entity": 15, "one_side_confirmed_grain": 10, "compatible_domain": 10,
        # suppressors (negative):
        "non_identifier": -100, "negative_filter_field": -100,
        "namespace_incompatible": -50, "namespace_ambiguous": -40, "different_entity_grains": -30,
    },
    negative_concepts=frozenset({
        "amount", "balance", "rate", "date", "timestamp", "description", "name", "status",
        "free_text", "address", "phone", "email", "currency", "flag", "score"}),
)
```

- [ ] **Step 4: Run → pass.**
- [ ] **Step 5: Commit** (`feat(passc): types + scoring config`).

---

## Task 2: Identifier eligibility + concept normalization

**Why:** `_is_id_like` is name-suffix based and misses FTR ids (`FORACID`, `REF_NUM`). Eligibility must combine it with `term_type`/concept, and apply the negative filters. Normalization groups the *specific* identifier concept (the real discriminator).

**Files:** Create `.../passc/identifiers.py`; Test `test_identifiers.py`.

**Interfaces:**
- Consumes: `_is_id_like` (`overlay/upload/entity.py`), `DEFAULT_CONFIG` (Task 1).
- Produces: `ColMeta` (a small view: `object_ref, table, column, data_type, term_name, term_type, concept, synonyms, bian_leaf, fibo_leaf, entity_tag, data_domain, is_grain`); `is_join_key_eligible(col: ColMeta, cfg=DEFAULT_CONFIG) -> bool`; `normalized_identifier_concept(col: ColMeta) -> str | None`.

- [ ] **Step 1: Write the failing test**
```python
# tests/featuregen/overlay/upload/passc/test_identifiers.py
from featuregen.overlay.upload.passc.identifiers import (
    ColMeta, is_join_key_eligible, normalized_identifier_concept)


def _c(**kw):
    base = dict(object_ref="src::public.t.c", table="t", column="c", data_type="text",
                term_name="", term_type="", concept="", synonyms="", bian_leaf="",
                fibo_leaf="", entity_tag="", data_domain="", is_grain=False)
    base.update(kw); return ColMeta(**base)


def test_ftr_ids_eligible_even_when_name_suffix_misses():
    # FORACID doesn't end in _id; term_name carries the identifier concept
    assert is_join_key_eligible(_c(column="foracid", term_name="Customer Account Number",
                                   term_type="Dimension"))
    assert is_join_key_eligible(_c(column="cif_id", term_name="Customer Information File Identifier"))


def test_negative_filter_fields_never_eligible():
    assert not is_join_key_eligible(_c(column="cust_name", term_name="Customer Name", concept="name"))
    assert not is_join_key_eligible(_c(column="tran_amt", term_name="Transaction Amount",
                                       term_type="Measure"))
    assert not is_join_key_eligible(_c(column="tran_date", term_name="Transaction Date", concept="date"))


def test_concept_normalization_groups_synonyms():
    a = normalized_identifier_concept(_c(column="cif_id", term_name="Customer Information File Identifier"))
    b = normalized_identifier_concept(_c(column="cif", term_name="Customer Information File Identifier",
                                         synonyms="CIF"))
    assert a and a == b
    # account number is a DIFFERENT concept from CIF id
    assert normalized_identifier_concept(_c(column="foracid", term_name="Customer Account Number")) != a
```

- [ ] **Step 2: Run → fail.**

- [ ] **Step 3: Implement `identifiers.py`**
```python
from __future__ import annotations
import re
from dataclasses import dataclass
from featuregen.overlay.upload.entity import _is_id_like
from featuregen.overlay.upload.passc.types import DEFAULT_CONFIG, PassCConfig


@dataclass(frozen=True, slots=True)
class ColMeta:
    object_ref: str; table: str; column: str; data_type: str | None
    term_name: str; term_type: str; concept: str; synonyms: str
    bian_leaf: str; fibo_leaf: str; entity_tag: str; data_domain: str; is_grain: bool


# term_name/concept tokens that mark an identifier even when the column name doesn't end in _id.
_ID_WORDS = ("identifier", "number", "reference", "code", " id", "id ", "cif", "account no")
_MEASURE_TYPES = frozenset({"measure"})


def _neg(text: str, cfg: PassCConfig) -> bool:
    t = (text or "").lower()
    return any(w in t for w in cfg.negative_concepts)


def is_join_key_eligible(col: ColMeta, cfg: PassCConfig = DEFAULT_CONFIG) -> bool:
    if (col.term_type or "").strip().lower() in _MEASURE_TYPES:
        return False
    # hard negative filter on concept/term_name (name/amount/date/... never a key)
    if _neg(col.concept, cfg) or _neg(col.term_name, cfg):
        # allow if the concept is explicitly an identifier despite a generic word (e.g. "name" in a term)
        if "identifier" not in (col.term_name or "").lower():
            return False
    if _is_id_like(col.column, col.data_type):
        return True
    hay = f" {(col.term_name or '').lower()} "
    return any(w in hay for w in _ID_WORDS)


def normalized_identifier_concept(col: ColMeta) -> str | None:
    if not is_join_key_eligible(col):
        return None
    # canonicalize the identifier concept from term_name (primary) + synonyms, stripping generic
    # words so "Customer Information File Identifier"/"CIF" collapse but "Account Number" stays distinct.
    base = (col.term_name or col.column or "").lower()
    base = re.sub(r"[^a-z0-9]+", "_", base).strip("_")
    for junk in ("_identifier", "_number", "_reference", "_code", "_id"):
        base = base.removesuffix(junk)
    return base or None
```
> **Implementer note:** confirm `_is_id_like`'s exact import + `_ID_SUFFIXES`/`_NON_ID_TYPES` semantics. Tune `_ID_WORDS`/normalization against the 127-row FTR sample (read-only, in `~/Downloads`; do NOT copy it into the repo) so `CIF_ID`/`CIF` collapse and `FORACID` stays distinct from `CIF_ID`. Keep it deterministic and explainable.

- [ ] **Step 4: Run → pass.**  - [ ] **Step 5: Commit.**

---

## Task 3: Namespace classifier

**Files:** Create `.../passc/namespace.py`; Test `test_namespace.py`.

**Interfaces:**
- Consumes: `ColMeta`, `normalized_identifier_concept` (Task 2), `NamespaceCompatibility`, `DEFAULT_CONFIG`.
- Produces: `classify_namespace(a: ColMeta, b: ColMeta, cfg=DEFAULT_CONFIG) -> tuple[NamespaceCompatibility, tuple[str,...]]`.

- [ ] **Step 1: Write the failing test**
```python
# tests/featuregen/overlay/upload/passc/test_namespace.py
from featuregen.overlay.upload.passc.identifiers import ColMeta
from featuregen.overlay.upload.passc.namespace import classify_namespace
from featuregen.overlay.upload.passc.types import NamespaceCompatibility as N


def _c(col, term, **kw):
    base = dict(object_ref="", table="t", column=col, data_type="text", term_name=term,
                term_type="", concept="", synonyms="", bian_leaf="", fibo_leaf="",
                entity_tag="", data_domain="", is_grain=False); base.update(kw); return ColMeta(**base)


def test_same_confirmed_entity_is_compatible():
    a = _c("cif_id", "Customer Information File Identifier", entity_tag="customer")
    b = _c("cif_id", "Customer Information File Identifier", entity_tag="customer")
    assert classify_namespace(a, b)[0] == N.COMPATIBLE


def test_mixed_bian_leaf_is_ambiguous():
    a = _c("cust_id", "Customer Reference", bian_leaf="Customer and Counterparty Identification")
    b = _c("cpty_id", "Counterparty Reference", bian_leaf="Customer and Counterparty Identification")
    assert classify_namespace(a, b)[0] == N.AMBIGUOUS


def test_same_concept_only_is_possible():
    a = _c("cif_id", "Customer Information File Identifier")
    b = _c("cif_id", "Customer Information File Identifier")
    ns, reasons = classify_namespace(a, b)
    assert ns == N.POSSIBLE and "same_identifier_concept" in reasons


def test_different_entities_incompatible():
    a = _c("cif_id", "Customer Information File Identifier", entity_tag="customer")
    b = _c("foracid", "Customer Account Number", entity_tag="account")
    assert classify_namespace(a, b)[0] == N.INCOMPATIBLE
```

- [ ] **Step 3: Implement `namespace.py`**
```python
from __future__ import annotations
from featuregen.overlay.upload.passc.identifiers import ColMeta, normalized_identifier_concept
from featuregen.overlay.upload.passc.types import DEFAULT_CONFIG, NamespaceCompatibility as N, PassCConfig


def classify_namespace(a: ColMeta, b: ColMeta, cfg: PassCConfig = DEFAULT_CONFIG):
    reasons: list[str] = []
    ta, tb = (a.entity_tag or "").lower(), (b.entity_tag or "").lower()
    if ta and tb:
        if ta == tb:
            reasons.append("same_confirmed_entity"); return N.COMPATIBLE, tuple(reasons)
        reasons.append("different_confirmed_entity"); return N.INCOMPATIBLE, tuple(reasons)
    ca, cb = normalized_identifier_concept(a), normalized_identifier_concept(b)
    same_concept = bool(ca and cb and ca == cb)
    la, lb = (a.bian_leaf or "").lower(), (b.bian_leaf or "").lower()
    mixed = la and la == lb and la in cfg.mixed_bian_leaves
    if same_concept:
        reasons.append("same_identifier_concept")
        # a shared *specific* concept overrides the coarse mixed leaf; else it's a corroborating signal
        if a.synonyms or b.synonyms or (a.term_name and a.term_name == b.term_name):
            return N.COMPATIBLE, tuple(reasons)
        return N.POSSIBLE, tuple(reasons)
    if mixed:
        reasons.append("mixed_bian_leaf"); return N.AMBIGUOUS, tuple(reasons)
    if la and la == lb:
        reasons.append("same_bian_leaf_only"); return N.AMBIGUOUS, tuple(reasons)
    reasons.append("generic_reference_without_context"); return N.AMBIGUOUS, tuple(reasons)
```

- [ ] **Step 4: Run → pass.**  - [ ] **Step 5: Commit.**

---

## Task 4: Candidate blocker

**Files:** Create `.../passc/candidates.py` (`block_candidates`); Test `test_block.py`.

**Interfaces:**
- Consumes: `ColMeta`, `is_join_key_eligible`, `classify_namespace`, `NamespaceCompatibility`.
- Produces: `CandidatePair` (frozen: `a: ColMeta, b: ColMeta, namespace, namespace_reasons`); `block_candidates(columns: list[ColMeta], *, allow_self_join=False) -> list[CandidatePair]` — enumerates distinct-table pairs where both are key-eligible and namespace ∈ {COMPATIBLE, POSSIBLE}. Ordered/deterministic (sort by object_ref) so the same input yields the same output.

- [ ] **Step 1: Write the failing test** (pairs formed for two `cif_id` across tables; a `cust_name`/`amount` column never paired; a mixed-leaf/INCOMPATIBLE pair excluded; self-table excluded unless `allow_self_join`; output order stable).
- [ ] **Step 3: Implement** — double loop over `is_join_key_eligible` columns, `i<j`, `a.table != b.table` (unless self-join), `classify_namespace` gate admits COMPATIBLE|POSSIBLE. Sort inputs by `object_ref` first for determinism.
- [ ] **Steps 2/4/5: fail → pass → commit.**

---

## Task 5: Scorer + direction/cardinality

**Files:** Add `score(...)` to `.../passc/candidates.py`; Test `test_score.py`.

**Interfaces:**
- Consumes: `CandidatePair`, `DEFAULT_CONFIG`, `JoinCandidateEvidenceV1`, `CardinalityInferenceStatus`, the config weights; `normalized_identifier_concept`.
- Produces: `score(pair: CandidatePair, *, source_snapshot_id, cfg=DEFAULT_CONFIG) -> JoinCandidateEvidenceV1` — fires weighted signals, sums to `score`, buckets (strong/weak/suppressed), caps a POSSIBLE-namespace pair at `weak` unless a `same_entity_tag`/`related_terms_key_link` signal fired; infers direction/cardinality from grain; builds the `SignalEvidence` list + reason codes + human explanation.

**Direction/cardinality (from Task-1 table):**
- right(=b) grain, left(=a) not → `from=a → to=b`, `N:1`, `INFERRED_FROM_CONFIRMED_GRAIN`.
- left grain, right not → orient `from=b → to=a`, `N:1`.
- both grain → `1:1`, `AMBIGUOUS_BOTH_GRAINS`, confidence lowered.
- neither grain → `MANY_TO_MANY_RISK`; **no direct cardinality proposed**; `missing_requirements` lists both grains; do NOT emit a `1:1` default (the enum has no `N:M`).

- [ ] **Step 1: Write the failing test** — same-concept + reinforcement → `strong`, `COMPATIBLE`; same-BIAN-leaf-only → `weak`/`AMBIGUOUS`; b-grain → `N:1` `INFERRED_FROM_CONFIRMED_GRAIN` with `from=a`; neither grain → `MANY_TO_MANY_RISK` + `missing_requirements` has both; every result has a non-empty `explanation` and reason codes; a POSSIBLE namespace never reaches `strong` without an entity/related-terms signal.
- [ ] **Step 3: Implement** the weighted sum + bucket + direction inference + evidence assembly. `candidate_id = sha256(from_ref|to_ref|pairs|algo_version)[:16]`.
- [ ] **Steps 2/4/5.**

---

## Task 6: Candidate fingerprint + dedupe lifecycle

**Files:** Create `.../passc/lifecycle.py`; Test `test_lifecycle.py` (uses the overlay conftest DB).

**Interfaces:**
- Consumes: `ApprovedJoinRef` builder (Task 7 shares it — define `build_join_ref(evidence, source)` here), `fact_key` (`overlay/identity.py:65`), `load_fact`, `fold_overlay_state`.
- Produces: `candidate_fingerprint(evidence) -> str`; `Action` (StrEnum: `PROPOSE, SKIP_DUPLICATE, SKIP_ACTIVE, CONFLICT, REPROPOSE`); `decide_action(conn, ref: ApprovedJoinRef, evidence) -> Action` — folds the fact and applies §11's table:
  - no stream → `PROPOSE`; `VERIFIED`/`DRAFT`/`PARTIALLY_CONFIRMED` (same fingerprint) → `SKIP_ACTIVE`; `REJECTED` → `SKIP_ACTIVE` unless `bucket`/`namespace_compatibility` materially changed → `REPROPOSE`; `STALE`/`REVERIFY`/`EXPIRED` → `REPROPOSE`. (`propose_fact`'s own sticky-rejected/duplicate guards are the backstop.)

- [ ] **Step 1: Write the failing test** — absent → PROPOSE; a DRAFT same-fingerprint → SKIP_ACTIVE; a VERIFIED → SKIP_ACTIVE. Use `_propose_join` conftest helper (authored in Task 7's conftest step).
- [ ] **Steps 3/2/4/5.**

---

## Task 7: Propose wiring

**Files:** Create `.../passc/propose.py`; author the passc conftest; Test `test_propose.py`.

**Interfaces:**
- Consumes: `propose_fact` + `Command` (`contracts.envelopes`), `proposal_fingerprint` (`overlay/identity.py`), `ApprovedJoinRef`/`ColumnPair`/`CatalogObjectRef`, `_ENRICH_ACTOR`, `decide_action` (Task 6), `current_catalog_adapter`.
- Produces: `build_join_ref(evidence, source) -> ApprovedJoinRef`; `propose_join_candidates(conn, source, evidences: list[JoinCandidateEvidenceV1], *, actor) -> None` — for each **strong** evidence: `decide_action`; if PROPOSE/REPROPOSE, build the ref + `propose_fact` with the value `{from_ref, to_ref, column_pairs, cardinality}` and the candidate evidence carried for the reviewer; fail-soft (a propose error never aborts), adapter-gated (mirror `_propose_governed_joins`), counters.

- [ ] **Step 0: Author `tests/featuregen/overlay/upload/passc/conftest.py`** — `passc_conn` (aliases the overlay `db` + `ensure_upload_catalog_adapter()`), `service_actor`/`human_actor(platform-admin)` (reuse the Phase-2 conftest patterns), and `_confirm_join(conn, ref, *, actor, cardinality)` / `_reject_join(...)` helpers that drive `confirm_fact`/`reject_fact` against the open governance-queue gate task **and then `run_projection(conn, OverlayProjection())`** (the projection-drain carry-forward from Phase 2 — `resolve_fact` reads the read model).
- [ ] **Step 1: Write the failing test** — a strong candidate → `approved_join` folds to `DRAFT` (PROPOSED, not VERIFIED); the gate task exists; a weak candidate is NOT proposed; a propose error is swallowed (fail-soft).
- [ ] **Step 3: Implement.** Command shape (all 6 fields, mirror `_propose_governed_joins`): `Command("propose_fact","overlay_fact",None,{"ref":ref,"fact_type":"approved_join","proposed_value":value}, actor, proposal_fingerprint(value))`.
  > **Implementer note — reviewer evidence (verify):** `propose_fact`'s `evidence=` slot is profiler-metric-shaped; the `JoinCandidateEvidenceV1` is NOT that shape. Read `proposal_commands.py` + `task_read.py::get_task_proposal` and choose the minimal way to surface the candidate evidence to the reviewer: either (a) carry a compact `candidate_evidence` dict inside the proposal payload/args that `get_task_proposal` returns, or (b) a small `pass_c_candidate_evidence` side table keyed by `(fact_key, proposed_event_id)`. Prefer (a) if the payload round-trips through `get_task_proposal`; do NOT force it into the profiler `metric_values` shape. Pin the choice in this task and add a test that the reviewer read surfaces the score + reason codes + explanation.
- [ ] **Steps 2/4/5.**

---

## Task 8: Migration 0987 + reverse projector

**Files:** Create `db/migrations/0987_graph_edge_join_authority_links.sql`, `.../passc/projection.py`; Test `test_projection.py`.

- [ ] **Step 1: Migration**
```sql
-- Phase 3A: link a governed 'joins' edge's operational authority back to its confirming approved_join.
ALTER TABLE graph_edge ADD COLUMN IF NOT EXISTS approved_join_fact_key text;
ALTER TABLE graph_edge ADD COLUMN IF NOT EXISTS approved_join_event_id text;
ALTER TABLE graph_edge ADD COLUMN IF NOT EXISTS approved_join_status text;
ALTER TABLE graph_edge ADD COLUMN IF NOT EXISTS authority_updated_at timestamptz;
```
- [ ] **Step 2: Write the failing test** — a VERIFIED `approved_join` → its `joins` edge is `authority='operational'` with `approved_join_status='VERIFIED'` and the fact links set, and `find_join_path` traverses it; a subsequently rejected/expired fact → the projector **demotes** the edge to `display_only` and `find_join_path` no longer traverses.
- [ ] **Step 3: Implement `project_confirmed_joins(conn, *, source, tables, now=None)`** — clear-then-apply per candidate table pair:
  1. resolve the pair's `approved_join` via `resolve_fact` (VERIFIED-only; `now=now`);
  2. if VERIFIED: **UPSERT the single `joins` edge in place** to `authority='operational'`, `cardinality=<value>`, `approved_join_fact_key/_event_id (from .provenance['confirmed_event_id'])/_status='VERIFIED'`, `authority_updated_at=now`; (respect the `(catalog_source,kind,from_ref,to_ref)` PK — upgrade, never insert a duplicate; orient `from_ref/to_ref` to the confirmed direction);
  3. else (absent/PROPOSED/STALE/REJECTED/EXPIRED): **demote** any existing `joins` edge for the pair to `authority='display_only'`, clear the fact links.
  Idempotent + fail-closed (non-VERIFIED never leaves an operational edge). Model on the Phase-2 `project_table_facts_for_ref` clear-then-apply + `now=now` discipline.
  > **Implementer note:** determine the candidate table pairs to project either from the open/closed `approved_join` gate tasks for this source, or from the `graph_edge` `joins` rows for the source. Read how confirmed table facts are enumerated in Phase 2 and mirror it. Confirm `resolve_fact` accepts an `ApprovedJoinRef` and the join `fact_key`.
- [ ] **Steps 4/5.**

---

## Task 9: Relationship readiness

**Files:** Modify `overlay/upload/readiness.py`; Test `test_readiness_relationships.py`.

- [ ] Add a per-table **relationships** dimension: fold the table's `approved_join` facts (via `fact_key`/`load_fact`/`fold_overlay_state`) + the persisted weak-candidate diagnostics → status ∈ `no_candidates / candidate_proposed / weak_candidates_only / confirmed / conflicting`, cause-labelled (reuse the Phase-2 cause vocabulary). Only `VERIFIED` reads `confirmed`.
- [ ] Tests: proposed join → `candidate_proposed`; confirmed → `confirmed`; a table with only weak candidates → `weak_candidates_only`; none → `no_candidates`.
- [ ] Commit.
> **Implementer note:** mirror the Phase-2 `_table_fact_status` fold-state mapping (`DRAFT`→proposed, `VERIFIED`→confirmed, `REJECTED`→missing, `STALE/REVERIFY`→proposed). A table has many possible joins, so aggregate per-table.

---

## Task 10: Ingest wiring + governed mode

**Files:** Modify `overlay/upload/ingest.py`; Test `test_passc_ingest.py`.

- [ ] Add `pass_c_enabled()` → `os.environ.get("OVERLAY_PASS_C","0")=="1"`.
- [ ] At the governed-joins seam (near `_propose_governed_joins`, `ingest.py:~650`), behind `pass_c_enabled()`, add a **savepointed + `except` fail-soft** block: build `ColMeta` for the upload's columns from `graph_node` + the glossary sidecar (concept/entity/bian from Phase-1/2 evidence), `block_candidates` → `score` → **strong → `propose_join_candidates`**, **weak → persist a readiness diagnostic**, **suppressed → counters**. Use `_ENRICH_ACTOR` as the proposer (four-eyes). Reuse the `mint_id("psc")` snapshot pattern.
- [ ] **Governed mode:** when `pass_c_enabled()`, ensure `governed_joins_enabled()` behaviour applies so declared `joins_to` edges are `display_only` (only confirmed `approved_join`s are operational). Confirm the two flags compose (or make `OVERLAY_PASS_C` imply governed joins); do NOT weaken the flag-off path.
- [ ] Call `project_confirmed_joins(...)` at end-of-ingest inside a savepoint+except (mirror the Phase-2 projector call), **unconditional** (a join confirmed in a prior cycle must survive a graph rebuild), gated internally on `projection_lag==0` like Phase 2.
- [ ] Tests: `OVERLAY_PASS_C` unset → ingest byte-for-byte (spy: no candidates, no facts); set → strong candidate proposed + declared edges display_only.
> **Implementer note:** confirm what `ColMeta` fields are available from `graph_node` (concept/entity/sensitivity/is_grain from Phase-1/2 migrations) vs must come from the glossary sidecar; assemble deterministically. This block does NOT call the LLM.

---

## Task 11: Integration — the authority proof (both directions)

**Files:** Test `test_passc_integration.py`.

- [ ] Glossary upload (2 tables sharing a `cif_id` concept, `customer.cif_id` a confirmed grain), `OVERLAY_PASS_C=1`:
  1. a **strong** candidate is proposed (`approved_join` `DRAFT`); `find_join_path(txn, customer)` returns **None** (fail-closed — unconfirmed);
  2. `_confirm_join(...)` (governance fallback, platform-admin) → VERIFIED; re-ingest / call `project_confirmed_joins` → `find_join_path` now **traverses** (a step exists) and relationship readiness = `confirmed`;
  3. `_reject_join` / force expiry → `project_confirmed_joins` **demotes** → `find_join_path` returns None again.
- [ ] Run the full `tests/featuregen/overlay/upload/ -q` + `tests/featuregen/overlay/ -q` — all green; flag-off byte-for-byte holds.
- [ ] Commit.

---

## Self-Review (spec coverage)

| Spec §  | Covered by |
|---|---|
| §6 hard gates + negative filters + weights + buckets | Tasks 2/4/5 |
| §7 namespace enum + reason codes + mixed-leaf | Task 3 |
| §8 related_terms gating | Task 5 (signal `related_terms_key_link` gated on id-like + concept match) |
| §9 direction/cardinality + CardinalityInferenceStatus + missing-grain | Task 5; confirm-edit is a Task-7/11 confirm-time value override |
| §10 evidence payload | Task 1 + Task 7 surfacing |
| §11 dedupe/lifecycle | Task 6 |
| §12 LLM challenger | **deferred to Phase 3B — not in this plan** |
| §13 governed source of truth + confirmation mode | Task 10 (governed mode) + Task 7 (service-actor proposer, governance queue) |
| §14 reverse projection (promote/demote, idempotent, fail-closed, links) | Task 8 |
| §15 weak/suppressed behaviour | Task 5 buckets + Task 10 (weak→diagnostic, suppressed→counters) |
| §16 relationship readiness | Task 9 |
| §17/§18 composite/self-join deferred | Task 4 (`allow_self_join` off; single-pair only) |
| §19 versioning | Task 1 (`config_version`/`candidate_algorithm_version` on every candidate) |
| §22 fail-closed proof | Task 11 |

**Deferred to Phase 3B (call out to reviewer):** the LLM challenger/explainer, exploration mode, and the `INCOMPATIBLE`-via-challenger demotion (Task 3 supports the enum value; nothing writes it in 3A).

**Placeholder scan:** the three "Implementer note (verify)" callouts (id-word tuning against FTR, reviewer-evidence surfacing shape, ColMeta assembly) are grounding directives with a recommended default — not open placeholders. **Type consistency:** `ColMeta`, `JoinCandidateEvidenceV1`, `ApprovedJoinRef`, `NamespaceCompatibility`, `CardinalityInferenceStatus` are used identically across tasks.

---

## Execution Handoff

**Start Phase 3A from a fresh worktree off the updated `main`** (`origin/main` = the Phase-2 merge + Phase 3B.2A) so Pass C sits on top of everything it references; carry this plan + the v2 spec across. Then:

1. **Subagent-Driven (recommended)** — Fable-5 implementers per task, Opus task-reviewers, adversarial whole-branch review before merge (the pattern that caught Phase 2's cross-cutting bugs).
2. **Inline execution** — executing-plans with checkpoints.

Which approach?
