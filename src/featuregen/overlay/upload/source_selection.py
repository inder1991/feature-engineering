"""Dataset need + source-selection contracts (profile plan §6.4, interface doc D1/D2/D5/D8).

WHICH COPY of a dataset serves a need, and WHY — as an immutable, content-addressed decision.

**Home (Task-7 amendment).** The plan's Create-list named a neutral ``featuregen.selection``
package; the amendment asks for ``source_selection.py``/``temporal_policy.py`` in the home that
matches the consumers. That home is ``overlay/upload``:

* the values these contracts key on already live here — ``dataset_profile_hash``
  (``dataset_profiles.py``), ``TemporalStorageModel`` (``profile_vocab.py``), ``ExecutionTier``
  (``bridge_realization.py``), ``EvidenceAuthorityV1`` (``semantic_context.py``);
* BOTH execution stacks already import ``overlay.upload`` directly (``analysis/execution.py``,
  ``analysis/grounding.py``, ``materialize/ir.py``, ``materialize/joins.py``), and
  ``overlay/upload/__init__.py`` is EMPTY — so importing one module here costs only that module;
* the neutral-package rule's actual requirement is that *neither execution stack imports the other
  to get a source decision*. A third top-level package would have satisfied that too, at the cost
  of a new edge from both stacks into a package holding four dataclasses. This home satisfies it
  with zero new edges.

**Identity (D1).** ``materialize_hash`` (RFC 8785 JCS) over content only. Policy revision ids are
DETERMINISTIC — ``dsp_`` + the content hash, the CHECK-pinned ``cpr_``/``pbr_`` precedent — so a
byte-identical re-declaration resolves to the SAME revision and never churns a decision that
references it. Excluded from every hash, deliberately: wall-clock, upload time, catalog watermark,
job state, the authoring subject, and any freshness/SLA notion (§6.4 — no freshness field exists
until a real delivery-SLA contract does; :func:`reject_freshness_keys` makes that mechanical).

**Authority (D2).** ``PolicyProvenanceV1`` carries the REAL triple per evidence record. The policy
CONTENT hash includes the authority AXES (producer/strength/lifecycle) and excludes ``producer_ref``
/ ``evidence_id`` / ``decision_refs`` — the §6.2 split between "what was declared, under what class
of authority" and "who filed it". A second admin re-declaring identical content therefore reuses
the revision; WHO made it current is recorded on the CAS pointer row, not by forking identity.

**Import weight.** ``EvidenceAuthorityV1`` lives in ``semantic_context.py``, which pulls the
concept registry (~1s). ``analysis/intent.py`` imports :data:`SELECTION_REFUSAL_CODES` from here at
module scope, so that import is made LAZILY inside ``__post_init__`` instead of at module level:
the vocabulary stays single-sourced without dragging the concept registry into the analysis
package's import graph (the same concern ``intent.py``'s own docstring records about
``enrich_llm``).
"""
from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from featuregen.materialize.canonical import materialize_hash
from featuregen.overlay.evidence import (
    AssertionStrength,
    EvidenceLifecycle,
    EvidenceProducer,
)
from featuregen.overlay.upload.bridge_realization import ExecutionTier
from featuregen.overlay.upload.object_ref import normalize_ref, parse_ref

if TYPE_CHECKING:                      # import weight — see the module docstring
    from featuregen.contracts import DbConn
    from featuregen.overlay.upload.semantic_context import EvidenceAuthorityV1

#: Canonical-content contract tokens — bump only on a meaning-bearing schema change.
_SERVING_POLICY_CONTRACT = "dataset_serving_policy_revision_v1"
_SOURCE_SELECTION_CONTRACT = "dataset_source_selection_v1"

#: Deterministic id prefix for a serving-policy revision (the ``cpr_``/``pbr_`` precedent).
SERVING_POLICY_ID_PREFIX = "dsp_"


class SelectionError(ValueError):
    """A benign, PRE-WRITE validation refusal (unknown member, bad ref, broken invariant).

    Routes map it to a 400; nothing was written. Distinct from a governed REFUSAL
    (:data:`SELECTION_REFUSAL_CODES`), which is a decision outcome, not a malformed input."""


class PolicyStoreConflict(Exception):
    """A CAS miss (the current pointer advanced past the expected version) or store corruption.

    ONE exception for both policy stores: the two are the same mechanism over different keys, and a
    route that handles a serving-policy conflict differently from a temporal one would be handling
    the same race twice. Routes map it to a 409."""


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# Flag (D8): FEATUREGEN_SOURCE_TEMPORAL_SELECTION, default off, DEPENDS ON FEATUREGEN_DATASET_PROFILES
# ─────────────────────────────────────────────────────────────────────────────────────────────────

SOURCE_TEMPORAL_SELECTION_FLAG = "FEATUREGEN_SOURCE_TEMPORAL_SELECTION"

#: The widened truthy set every flag in this program reads (D8; the ``feature_assist`` pattern).
_TRUTHY = {"1", "true", "yes", "on"}


@dataclass(frozen=True, slots=True)
class SourceTemporalFlagStatus:
    """The flag's configured intent, its dependency, and whether the pair is a VALID configuration.

    Fail-closed (D8 / plan §5.16): ``FEATUREGEN_SOURCE_TEMPORAL_SELECTION=1`` with
    ``FEATUREGEN_DATASET_PROFILES`` off is an INVALID configuration, not a half-enabled path — the
    feature reports itself DISABLED (routes 404) and the startup check says so loudly. Enforcing it
    in the reader rather than by convention is the point: no surface can accidentally opt out."""

    configured_value: str | None
    requested: bool
    dependency_met: bool

    @property
    def enabled(self) -> bool:
        return self.requested and self.dependency_met

    @property
    def configuration_valid(self) -> bool:
        return not (self.requested and not self.dependency_met)


def source_temporal_selection_status() -> SourceTemporalFlagStatus:
    """Read both flags and report the combined posture (never raises — the app must still boot)."""
    from featuregen.overlay.upload.profile_vocab import dataset_profiles_enabled

    configured = os.environ.get(SOURCE_TEMPORAL_SELECTION_FLAG)
    requested = (configured or "").strip().lower() in _TRUTHY
    return SourceTemporalFlagStatus(
        configured_value=configured,
        requested=requested,
        dependency_met=dataset_profiles_enabled(),
    )


def source_temporal_selection_enabled() -> bool:
    """The single env gate for every Release-B selection surface. Default OFF; fail-closed when the
    ``FEATUREGEN_DATASET_PROFILES`` dependency is unmet."""
    return source_temporal_selection_status().enabled


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# §6.4 vocabularies
# ─────────────────────────────────────────────────────────────────────────────────────────────────


class DatasetNeedRole(StrEnum):
    """WHAT a plan needs a dataset FOR (§6.4). ``POPULATION`` is the one that must always be
    declared — it decides who is in the answer, and inferring it silently shrinks results."""

    POPULATION = "population"
    EVENT_SOURCE = "event_source"
    DIMENSION_SOURCE = "dimension_source"
    REFERENCE = "reference"
    MAPPING = "mapping"


class ServingPurpose(StrEnum):
    """The two purposes a policy is declared for (§6.4). The superseded plan's ``OPERATIONAL`` /
    ``AUDIT`` purposes were dropped in the rebase (D12.9) and are NOT reinstated here."""

    FEATURE_SERVING = "feature_serving"
    ANALYTICAL = "analytical"


class CandidateDisposition(StrEnum):
    """What happened to one considered candidate (§6.4 / functional rule 9). ``TIED`` is a
    first-class outcome, never broken by upload time, lexical order or newest catalog."""

    SELECTED = "selected"
    ELIGIBLE = "eligible"
    REJECTED = "rejected"
    TIED = "tied"


class SelectionBasis(StrEnum):
    """WHICH rule chose the source, in the fixed precedence Task 8 applies."""

    EXPLICIT_REQUEST = "explicit_request"
    SERVING_POLICY = "serving_policy"
    SINGLE_ELIGIBLE_CANDIDATE = "single_eligible_candidate"


class AuthorityBasis(StrEnum):
    """WHY the chosen copy was allowed to be authoritative for this tier.

    ``PROPOSED_AUTHORITY_SANDBOX`` is the sandbox-only tier: a unique LLM-proposed authority value
    may rank in sandbox with a visible warning, and may NOT in production (Task 8). ``UNKNOWN``
    exists so a selection made on an explicit request with no authority story is still honest."""

    LOAD_BEARING_PROFILE = "load_bearing_profile"
    POLICY_DECLARATION = "policy_declaration"
    PROPOSED_AUTHORITY_SANDBOX = "proposed_authority_sandbox"
    UNKNOWN = "unknown"


#: Why a candidate was admitted, rejected or tied. CLOSED (functional rule 9): a free-text reason
#: cannot be counted, compared across decisions, or rendered without a human reading prose.
CANDIDATE_REASON_CODES: frozenset[str] = frozenset({
    "explicit_request",             # the caller named this dataset
    "policy_preferred",             # named in the current policy's preferred set
    "policy_eligible",              # named in the current policy's eligible set
    "load_bearing_authority",       # a load-bearing authority_role admitted it
    "proposed_authority_sandbox",   # ranked on a PROPOSED authority value, sandbox tier only
    "not_in_policy",                # a policy exists and this dataset is not in it
    "no_binding",                   # no current authorized physical binding
    "authority_insufficient",       # authority value absent/too weak for this tier
    "profile_absent",               # no assembled dataset profile
    "equally_preferred",            # tied with another equally preferred candidate
})


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# Closed refusal codes (Task 7 item: population undeclared, source ambiguous, binding missing,
# authority insufficient, temporal model unknown, historical current-only, SCD overlap, snapshot
# tie). ONE spelling per condition, SCREAMING_SNAKE like every other refusal code in the platform
# (`REFUSAL_TO_GAP` keys, `physical.py` codes) — the lowercase `intent.UNRESOLVED_CODES` members
# are the model's own abstention vocabulary and stay separate (see `analysis/intent.py`).
# ─────────────────────────────────────────────────────────────────────────────────────────────────

#: The population dataset was never declared. Never inferred from `primary_entity`, authority role
#: or table name (§5.4) — a wrong population silently shrinks every answer built on it.
SELECTION_POPULATION_UNDECLARED = "SELECTION_POPULATION_UNDECLARED"
#: Two or more equally eligible candidates remain. Refuses rather than ordering by accident (§5.9).
SELECTION_SOURCE_AMBIGUOUS = "SELECTION_SOURCE_AMBIGUOUS"
#: The chosen dataset has no current AUTHORIZED physical binding — explainable metadata, not an
#: executable source (Task 8).
SELECTION_BINDING_MISSING = "SELECTION_BINDING_MISSING"
#: No authority value strong enough for this execution tier (production may not rank a proposal).
SELECTION_AUTHORITY_INSUFFICIENT = "SELECTION_AUTHORITY_INSUFFICIENT"

#: The dataset's temporal storage model is unknown and no policy declares one (§6.5).
TEMPORAL_MODEL_UNKNOWN = "TEMPORAL_MODEL_UNKNOWN"
#: A HISTORICAL request against a current-only (or history-less SCD1) dataset. Never falls back to
#: today's row (§5.5) — it asks for another source or an explicit limitation.
TEMPORAL_HISTORICAL_CURRENT_ONLY = "TEMPORAL_HISTORICAL_CURRENT_ONLY"
#: Overlapping SCD intervals for one key. A DATA-QUALITY defect, deliberately NOT a learning gap —
#: see the `REFUSAL_TO_GAP` comment in `data_agent/learning.py`.
TEMPORAL_SCD_OVERLAP = "TEMPORAL_SCD_OVERLAP"
#: Two snapshots tie at or before the cutoff and the policy declares no governed tie-breaker.
TEMPORAL_SNAPSHOT_TIE = "TEMPORAL_SNAPSHOT_TIE"

#: The CLOSED refusal vocabulary for source + row selection. Owned here, imported by
#: `analysis.intent`, `analysis.clarify` and `data_agent.learning` — one set, one spelling.
SELECTION_REFUSAL_CODES: frozenset[str] = frozenset({
    SELECTION_POPULATION_UNDECLARED,
    SELECTION_SOURCE_AMBIGUOUS,
    SELECTION_BINDING_MISSING,
    SELECTION_AUTHORITY_INSUFFICIENT,
    TEMPORAL_MODEL_UNKNOWN,
    TEMPORAL_HISTORICAL_CURRENT_ONLY,
    TEMPORAL_SCD_OVERLAP,
    TEMPORAL_SNAPSHOT_TIE,
})


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# Shared validation
# ─────────────────────────────────────────────────────────────────────────────────────────────────

#: Substrings that would smuggle a freshness/SLA notion into a policy or decision identity. §6.4:
#: "No freshness field appears until a real delivery SLA/observation contract exists." Mechanical,
#: so the rule cannot be broken by a well-meant field added later without reading the plan.
#: Matched on WORD SEGMENTS, not substrings — `current_flag_ref` contains "lag" and is a perfectly
#: legitimate policy field. A substring test here refused half the shipped contract.
_FRESHNESS_SEGMENTS = frozenset({"fresh", "freshness", "sla", "lag", "lagged", "latency",
                                 "watermark", "staleness", "stale"})
#: Matched on the whole key — multi-word notions that no single segment carries.
_FRESHNESS_PHRASES = ("loaded_at", "last_refresh", "updated_at", "upload_time", "ingested_at",
                      "as_of_wall_clock")


def reject_freshness_keys(payload: dict[str, Any], *, where: str) -> None:
    """Refuse any key whose name carries a freshness/SLA/watermark notion, at any depth."""
    for key, value in payload.items():
        lowered = str(key).lower()
        segments = {part for part in lowered.replace("-", "_").split("_") if part}
        if segments & _FRESHNESS_SEGMENTS or any(p in lowered for p in _FRESHNESS_PHRASES):
            raise SelectionError(
                f"{where} may not carry a freshness/SLA key ({key!r}): no delivery-SLA or "
                "observation contract exists yet, and a freshness field in a decision identity "
                "would silently re-key every replay when the data merely arrived later")
        if isinstance(value, dict):
            reject_freshness_keys(value, where=where)
        elif isinstance(value, (list, tuple)):
            for item in value:
                if isinstance(item, dict):
                    reject_freshness_keys(item, where=where)


def normalize_dataset_ref(raw: object, *, what: str = "dataset_ref") -> str:
    """Normalize one SCHEMA-PRESERVING TABLE logical ref; refuse a column ref or a blank.

    Table-scoped on purpose: a serving policy chooses a DATASET, and a column ref would name a
    thing that cannot be a source."""
    if not isinstance(raw, str) or not raw.strip():
        raise SelectionError(f"{what} must be a non-empty logical ref")
    try:
        source, schema, table, column = parse_ref(raw.strip().lower())
    except ValueError as exc:
        raise SelectionError(f"{what} {raw!r} is not a normalized logical ref: {exc}") from exc
    if column is not None:
        raise SelectionError(f"{what} {raw!r} must address a TABLE, not a column")
    return normalize_ref(source, schema, table)


def _member(raw: object, vocab: type[StrEnum], *, what: str) -> str:
    if isinstance(raw, vocab):
        return raw.value
    if isinstance(raw, str):
        try:
            return vocab(raw.strip().lower()).value
        except ValueError:
            pass
    raise SelectionError(
        f"{what} {raw!r} is not one of {sorted(m.value for m in vocab)}")


def _reason_codes(raw: object, *, what: str) -> tuple[str, ...]:
    """Normalize + sort a closed reason-code tuple. Sorted because a reason SET has no order, and
    an incidental order would re-key an identical decision."""
    if isinstance(raw, str) or not isinstance(raw, (list, tuple)):
        raise SelectionError(f"{what} must be a tuple of reason codes")
    out: set[str] = set()
    for item in raw:
        if not isinstance(item, str) or item.strip().lower() not in CANDIDATE_REASON_CODES:
            raise SelectionError(
                f"{what} carries {item!r}, which is not one of "
                f"{sorted(CANDIDATE_REASON_CODES)}")
        out.add(item.strip().lower())
    return tuple(sorted(out))


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# §6.4 contracts
# ─────────────────────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class PolicyProvenanceV1:
    """WHY a policy is trusted: the real D2 authority triples plus the governed decision refs.

    ``evidence`` items are ``semantic_context.EvidenceAuthorityV1`` (imported lazily — module
    docstring). Only the AXES enter policy identity; ``producer_ref`` / ``evidence_id`` /
    ``decision_refs`` are provenance."""

    evidence: tuple[EvidenceAuthorityV1, ...] = ()
    decision_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.evidence:
            # Lazy: an EMPTY provenance must not drag the concept registry into the import graph
            # of whatever constructed it (module docstring).
            from featuregen.overlay.upload.semantic_context import EvidenceAuthorityV1 as _EA

            for item in self.evidence:
                if not isinstance(item, _EA):
                    raise SelectionError(
                        "PolicyProvenanceV1.evidence carries the real EvidenceAuthorityV1 triple "
                        f"(D2); got {type(item).__name__}")
        for ref in self.decision_refs:
            if not isinstance(ref, str) or not ref.strip():
                raise SelectionError("decision_refs entries must be non-empty strings")
        object.__setattr__(self, "evidence", tuple(self.evidence))
        object.__setattr__(self, "decision_refs", tuple(sorted(
            {r.strip() for r in self.decision_refs})))

    def authority_axes(self) -> list[dict[str, str]]:
        """The identity-bearing half: sorted (producer, strength, lifecycle) triples, deduped."""
        axes = {(e.producer, e.strength, e.lifecycle) for e in self.evidence}
        return [{"producer": p, "strength": s, "lifecycle": lc} for p, s, lc in sorted(axes)]

    def payload(self) -> dict[str, Any]:
        """The FULL provenance, for persistence (not identity)."""
        return {
            "evidence": [
                {"producer": e.producer, "strength": e.strength, "lifecycle": e.lifecycle,
                 "producer_ref": e.producer_ref, "evidence_id": e.evidence_id}
                for e in self.evidence],
            "decision_refs": list(self.decision_refs),
        }


def provenance_from_payload(payload: object) -> PolicyProvenanceV1:
    """Rebuild provenance from its stored JSON (the store's read path)."""
    from featuregen.overlay.upload.semantic_context import EvidenceAuthorityV1

    if not isinstance(payload, dict):
        raise SelectionError("provenance payload must be a JSON object")
    evidence = []
    for item in payload.get("evidence") or ():
        if not isinstance(item, dict):
            raise SelectionError("provenance evidence entries must be JSON objects")
        evidence.append(EvidenceAuthorityV1(
            producer=str(item.get("producer", "")),
            strength=str(item.get("strength", "")),
            lifecycle=str(item.get("lifecycle", "")),
            producer_ref=item.get("producer_ref"),
            evidence_id=item.get("evidence_id")))
    return PolicyProvenanceV1(
        evidence=tuple(evidence),
        decision_refs=tuple(str(r) for r in (payload.get("decision_refs") or ())))


def human_declaration_provenance(*, producer_ref: str) -> PolicyProvenanceV1:
    """The provenance of a policy declared by a person through the four-eyes admin surface.

    This is the D12.7 escape hatch made concrete: for an upload-only catalog the POLICY is the
    operational declaration, so its authority is ``human/confirmed/active`` — not an invented
    source attestation, and not an LLM proposal promoted by being written down."""
    from featuregen.overlay.upload.semantic_context import EvidenceAuthorityV1

    return PolicyProvenanceV1(evidence=(EvidenceAuthorityV1(
        producer=EvidenceProducer.HUMAN.value,
        strength=AssertionStrength.CONFIRMED.value,
        lifecycle=EvidenceLifecycle.ACTIVE.value,
        producer_ref=producer_ref),))


@dataclass(frozen=True, slots=True)
class DatasetNeedV1:
    """ONE dataset a plan needs, and the tier it will be read at (§6.4).

    ``execution_tier`` reuses ``bridge_realization.ExecutionTier`` verbatim (D5) — there is no
    second tier vocabulary. It is part of the need because the SAME need answered for sandbox and
    for production is two different decisions: production may not rank a proposed authority value.
    """

    entity_id: str
    need_role: DatasetNeedRole
    serving_purpose: ServingPurpose
    execution_tier: ExecutionTier
    required_concepts: tuple[str, ...] = ()
    explicit_dataset_ref: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.entity_id, str) or not self.entity_id.strip():
            raise SelectionError("entity_id must be a non-empty string")
        object.__setattr__(self, "entity_id", self.entity_id.strip().lower())
        object.__setattr__(self, "need_role",
                           DatasetNeedRole(_member(self.need_role, DatasetNeedRole,
                                                   what="need_role")))
        object.__setattr__(self, "serving_purpose",
                           ServingPurpose(_member(self.serving_purpose, ServingPurpose,
                                                  what="serving_purpose")))
        object.__setattr__(self, "execution_tier",
                           ExecutionTier(_member(self.execution_tier, ExecutionTier,
                                                 what="execution_tier")))
        # Sorted + deduped: required concepts are a SET, and an incidental order must not re-key an
        # otherwise identical need.
        concepts = {c.strip().lower() for c in self.required_concepts if str(c).strip()}
        object.__setattr__(self, "required_concepts", tuple(sorted(concepts)))
        if self.explicit_dataset_ref is not None:
            object.__setattr__(self, "explicit_dataset_ref", normalize_dataset_ref(
                self.explicit_dataset_ref, what="explicit_dataset_ref"))

    def policy_key(self) -> tuple[str, str, str]:
        """The serving-policy key (§6.4): ``entity_id + need_role + serving_purpose``."""
        return (self.entity_id, self.need_role.value, self.serving_purpose.value)

    def payload(self) -> dict[str, Any]:
        return {
            "entity_id": self.entity_id,
            "need_role": self.need_role.value,
            "serving_purpose": self.serving_purpose.value,
            "execution_tier": self.execution_tier.value,
            "required_concepts": list(self.required_concepts),
            "explicit_dataset_ref": self.explicit_dataset_ref,
        }


@dataclass(frozen=True, slots=True)
class CandidateDecisionV1:
    """One candidate the selector considered, and why it won, lost or tied (§6.4, rule 9)."""

    dataset_ref: str
    dataset_profile_hash: str
    binding_revision_id: str | None
    disposition: CandidateDisposition
    reason_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "dataset_ref", normalize_dataset_ref(self.dataset_ref))
        if not isinstance(self.dataset_profile_hash, str) or not self.dataset_profile_hash.strip():
            raise SelectionError("dataset_profile_hash must be a non-empty hash")
        object.__setattr__(self, "dataset_profile_hash", self.dataset_profile_hash.strip())
        if self.binding_revision_id is not None:
            object.__setattr__(self, "binding_revision_id",
                               normalize_binding_revision_id(self.binding_revision_id))
        object.__setattr__(self, "disposition", CandidateDisposition(
            _member(self.disposition, CandidateDisposition, what="disposition")))
        object.__setattr__(self, "reason_codes",
                           _reason_codes(self.reason_codes, what="reason_codes"))

    def payload(self) -> dict[str, Any]:
        return {
            "dataset_ref": self.dataset_ref,
            "dataset_profile_hash": self.dataset_profile_hash,
            "binding_revision_id": self.binding_revision_id,
            "disposition": self.disposition.value,
            "reason_codes": list(self.reason_codes),
        }


def normalize_binding_revision_id(raw: object) -> str:
    """A binding revision id is the CHECK-pinned ``pbr_<64 hex>`` shape (migration 1036).

    Validated structurally HERE so a typo cannot reach a decision; whether the revision is actually
    PERSISTED is a store question, answered by
    ``data_agent.binding_store.binding_revision_exists`` at authoring time."""
    text = str(raw or "").strip()
    if not text.startswith("pbr_") or len(text) != 68 or not all(
            c in "0123456789abcdef" for c in text[4:]):
        raise SelectionError(
            f"binding_revision_id {raw!r} is not a physical binding revision id (pbr_<64 hex>); a "
            "selection must reference a persisted, immutable revision")
    return text


@dataclass(frozen=True, slots=True)
class DatasetServingPolicyRevisionV1:
    """One immutable serving-policy revision: which datasets MAY serve a need, and which are
    preferred (§6.4).

    ``preferred ⊆ eligible`` is enforced, and BOTH sets are sorted and deduped: "empty or multiple
    equally preferred candidates remain an explicit ambiguity, never an ordering accident" — so the
    contract refuses to carry an order that could be mistaken for a ranking."""

    entity_id: str
    need_role: DatasetNeedRole
    serving_purpose: ServingPurpose
    eligible_dataset_refs: tuple[str, ...]
    preferred_dataset_refs: tuple[str, ...]
    provenance: PolicyProvenanceV1
    content_hash: str = ""
    revision_id: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.entity_id, str) or not self.entity_id.strip():
            raise SelectionError("entity_id must be a non-empty string")
        object.__setattr__(self, "entity_id", self.entity_id.strip().lower())
        object.__setattr__(self, "need_role", DatasetNeedRole(
            _member(self.need_role, DatasetNeedRole, what="need_role")))
        object.__setattr__(self, "serving_purpose", ServingPurpose(
            _member(self.serving_purpose, ServingPurpose, what="serving_purpose")))
        eligible = tuple(sorted({normalize_dataset_ref(r, what="eligible_dataset_ref")
                                 for r in self.eligible_dataset_refs}))
        preferred = tuple(sorted({normalize_dataset_ref(r, what="preferred_dataset_ref")
                                  for r in self.preferred_dataset_refs}))
        if not eligible:
            raise SelectionError(
                "a serving policy with no eligible dataset declares nothing; delete the policy "
                "instead of publishing one that can never select")
        missing = [r for r in preferred if r not in eligible]
        if missing:
            raise SelectionError(
                f"preferred dataset refs must be a subset of eligible: {missing} are preferred but "
                "not eligible — a preference for something that may not serve is a contradiction, "
                "not a ranking")
        object.__setattr__(self, "eligible_dataset_refs", eligible)
        object.__setattr__(self, "preferred_dataset_refs", preferred)
        if not isinstance(self.provenance, PolicyProvenanceV1):
            raise SelectionError("provenance must be a PolicyProvenanceV1")
        content_hash = materialize_hash(self.content_payload())
        object.__setattr__(self, "content_hash", content_hash)
        object.__setattr__(self, "revision_id", f"{SERVING_POLICY_ID_PREFIX}{content_hash}")

    @property
    def policy_key(self) -> tuple[str, str, str]:
        return (self.entity_id, self.need_role.value, self.serving_purpose.value)

    @property
    def ambiguous(self) -> bool:
        """True when the policy cannot, by itself, pick one dataset — zero preferred with more than
        one eligible, or two-plus equally preferred. An EXPLICIT ambiguity the selector refuses on
        (``SELECTION_SOURCE_AMBIGUOUS``), never something an ordering quietly resolves."""
        if len(self.preferred_dataset_refs) > 1:
            return True
        return not self.preferred_dataset_refs and len(self.eligible_dataset_refs) > 1

    def content_payload(self) -> dict[str, Any]:
        """The canonical JCS content: the key, both sets, and the AUTHORITY AXES. No subject, no
        timestamp, no upload time, no watermark (module docstring)."""
        payload = {
            "contract": _SERVING_POLICY_CONTRACT,
            "entity_id": self.entity_id,
            "need_role": self.need_role.value,
            "serving_purpose": self.serving_purpose.value,
            "eligible_dataset_refs": list(self.eligible_dataset_refs),
            "preferred_dataset_refs": list(self.preferred_dataset_refs),
            "authority": self.provenance.authority_axes(),
        }
        reject_freshness_keys(payload, where="a serving policy")
        return payload


@dataclass(frozen=True, slots=True)
class DatasetSourceSelectionV1:
    """The immutable record of WHICH copy served one need, and why (§6.4).

    Physical binding identity lives HERE and nowhere in the semantic profile (§6.3), so moving
    environments never re-keys meaning."""

    need: DatasetNeedV1
    selected_dataset_ref: str
    selected_dataset_profile_hash: str
    selected_binding_revision_id: str
    serving_policy_revision_id: str | None
    authority_role: str
    authority_basis: AuthorityBasis
    selection_basis: SelectionBasis
    considered_candidates: tuple[CandidateDecisionV1, ...] = ()
    content_hash: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.need, DatasetNeedV1):
            raise SelectionError("need must be a DatasetNeedV1")
        object.__setattr__(self, "selected_dataset_ref",
                           normalize_dataset_ref(self.selected_dataset_ref,
                                                 what="selected_dataset_ref"))
        if not str(self.selected_dataset_profile_hash or "").strip():
            raise SelectionError(
                "selected_dataset_profile_hash is required: a selection that does not pin WHICH "
                "profile it read cannot be replayed or revalidated")
        object.__setattr__(self, "selected_dataset_profile_hash",
                           self.selected_dataset_profile_hash.strip())
        object.__setattr__(self, "selected_binding_revision_id",
                           normalize_binding_revision_id(self.selected_binding_revision_id))
        if self.serving_policy_revision_id is not None:
            policy_id = str(self.serving_policy_revision_id).strip()
            if not policy_id.startswith(SERVING_POLICY_ID_PREFIX):
                raise SelectionError(
                    f"serving_policy_revision_id {policy_id!r} is not a "
                    f"{SERVING_POLICY_ID_PREFIX}<hash> revision id")
            object.__setattr__(self, "serving_policy_revision_id", policy_id)
        if not isinstance(self.authority_role, str) or not self.authority_role.strip():
            raise SelectionError("authority_role must be a non-empty string")
        object.__setattr__(self, "authority_role", self.authority_role.strip().lower())
        object.__setattr__(self, "authority_basis", AuthorityBasis(
            _member(self.authority_basis, AuthorityBasis, what="authority_basis")))
        object.__setattr__(self, "selection_basis", SelectionBasis(
            _member(self.selection_basis, SelectionBasis, what="selection_basis")))
        for candidate in self.considered_candidates:
            if not isinstance(candidate, CandidateDecisionV1):
                raise SelectionError("considered_candidates entries must be CandidateDecisionV1")
        # Sorted by ref: the candidate SET is what was considered; the order they were enumerated
        # in is an implementation detail and must not re-key the decision.
        candidates = tuple(sorted(self.considered_candidates, key=lambda c: c.dataset_ref))
        selected = [c for c in candidates if c.disposition is CandidateDisposition.SELECTED]
        if len(selected) > 1:
            raise SelectionError("a source selection has exactly one SELECTED candidate")
        if selected and selected[0].dataset_ref != self.selected_dataset_ref:
            raise SelectionError(
                f"the SELECTED candidate {selected[0].dataset_ref!r} is not the selected dataset "
                f"{self.selected_dataset_ref!r}")
        # A TIED candidate is RECORDED, not refused here (rule 9: admitted, rejected OR tied). What
        # a tie may never do is decide: when the tie is among the candidates a rule would have
        # chosen from, the selector refuses with SELECTION_SOURCE_AMBIGUOUS (Task 8) and no
        # selection is built at all — so the only tie that reaches this contract is one a
        # higher-precedence rule (an explicit request) already settled.
        if selected and any(c.disposition is CandidateDisposition.TIED for c in candidates) \
                and self.selection_basis is not SelectionBasis.EXPLICIT_REQUEST:
            raise SelectionError(
                "a recorded tie can only coexist with a selection an EXPLICIT request settled; "
                f"otherwise refuse with {SELECTION_SOURCE_AMBIGUOUS} rather than selecting over it")
        object.__setattr__(self, "considered_candidates", candidates)
        object.__setattr__(self, "content_hash", materialize_hash(self.content_payload()))

    def content_payload(self) -> dict[str, Any]:
        payload = {
            "contract": _SOURCE_SELECTION_CONTRACT,
            "need": self.need.payload(),
            "selected_dataset_ref": self.selected_dataset_ref,
            "selected_dataset_profile_hash": self.selected_dataset_profile_hash,
            "selected_binding_revision_id": self.selected_binding_revision_id,
            "serving_policy_revision_id": self.serving_policy_revision_id,
            "authority_role": self.authority_role,
            "authority_basis": self.authority_basis.value,
            "selection_basis": self.selection_basis.value,
            "considered_candidates": [c.payload() for c in self.considered_candidates],
        }
        reject_freshness_keys(payload, where="a source selection")
        return payload


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# Authoring-time reference validation (Task 7: "validate every dataset and column ref exists and is
# readable when authored"). Lives HERE, beside `normalize_dataset_ref`, so both policy stores and
# the routes share ONE notion of "this ref names something the author may see" — a second copy in
# each store is how two surfaces end up with two different answers for one ref.
#
# HIDDEN AND MISSING ARE THE SAME MESSAGE, deliberately. Telling an author "that dataset exists but
# you may not see it" is an existence oracle over a catalog they were not granted; the D11 derived
# table-anchor scope is applied and both outcomes read as "not a readable dataset".
# ─────────────────────────────────────────────────────────────────────────────────────────────────


def _graph_key(logical_ref: str) -> tuple[str, str]:
    """(catalog_source, graph object_ref) for a normalized logical ref.

    The graph stores nodes PUBLIC-FLATTENED (`field_resolution._graph_key` convention), so a
    schema-preserving `bank::dpl_eib.orders` is looked up as `public.orders`."""
    source, _schema, table, column = parse_ref(logical_ref)
    ref = f"public.{table}" + (f".{column}" if column else "")
    return source, ref.lower()


def assert_dataset_refs_readable(conn: DbConn, refs: Iterable[str], *,
                                 roles: Iterable[str] = ()) -> None:
    """Every ref must name a TABLE node the caller can see under the D11 DERIVED anchor scope."""
    from featuregen.overlay.upload.read_scope import allowed_classes, anchor_visibility_predicate

    allowed = allowed_classes(roles)
    for ref in refs:
        source, object_ref = _graph_key(ref)
        row = conn.execute(
            "SELECT 1 FROM graph_node gn WHERE gn.catalog_source = %s AND gn.object_ref = %s "
            f"AND gn.kind = 'table' AND {anchor_visibility_predicate('gn')} LIMIT 1",
            (source, object_ref, allowed, allowed)).fetchone()
        if row is None:
            raise SelectionError(
                f"{ref!r} is not a readable dataset in this catalog: a policy may only name "
                "datasets that exist and that its author can see")


def assert_column_refs_readable(conn: DbConn, refs: Iterable[str], *,
                                roles: Iterable[str] = ()) -> None:
    """Every ref must name a COLUMN node the caller can see (its own ``visible_requires``)."""
    from featuregen.overlay.upload.read_scope import allowed_classes

    allowed = allowed_classes(roles)
    for ref in refs:
        source, object_ref = _graph_key(ref)
        row = conn.execute(
            "SELECT 1 FROM graph_node WHERE catalog_source = %s AND object_ref = %s "
            "AND kind = 'column' AND COALESCE(visible_requires, '{}') <@ %s LIMIT 1",
            (source, object_ref, allowed)).fetchone()
        if row is None:
            raise SelectionError(
                f"{ref!r} is not a readable column in this catalog: a policy may only reference "
                "columns that exist and that its author can see")
