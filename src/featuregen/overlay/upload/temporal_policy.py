"""Temporal policy + row-selection contracts (profile plan §6.5, interface doc D1/D2/D12.6).

WHICH ROWS of a dataset answer a question — the second, separately explainable half of the
Release-B decision (§5.7: source choice and row choice are different decisions).

**Home:** beside :mod:`featuregen.overlay.upload.source_selection` — same reasoning, recorded in
that module's docstring. ``PolicyProvenanceV1`` is imported from there (one provenance contract,
not two).

**Four kinds, and what actually exists (D12.6).** All four :class:`TemporalSelectionKind` members
are represented here because a POLICY must be able to declare any of them. Only
``VALID_AT_REPORT_CUTOFF`` has an engine today (``data_agent.dimensions``'s
``DimensionAttributionPolicyV1``, whose ``_SUPPORTED_BASES`` is ``(REPORT_CUTOFF,)``).
``CURRENT_RECORD`` maps to a basis that is declared and REFUSED, and ``LATEST_SNAPSHOT_AS_OF`` has
no analysis-path implementation at all (the only latest-as-of logic is ``materialize/spine.py``,
which analysis does not import). Both are NEW ENGINE WORK owned by Task 8 — this module does not
pretend otherwise, and nothing here emits an attribution policy.

**The agreement rule (Task 7 item; D12.7 escape hatch).** A policy's ``temporal_storage_model``
must AGREE with a current LOAD-BEARING profile value when one exists — a policy may not overrule a
governed classification. When the profile value is absent or merely PROPOSED, the policy ITSELF is
the operational declaration: that is what keeps an upload-only catalog (which has no source-attested
temporal model at all) functional without promoting an LLM proposal to load-bearing. Provenance
records who declared it; the four-eyes admin path is what gives it operational effect.
:func:`temporal_policy_agreement` is that rule, returning a typed refusal code, never a bare bool.

**Identity (D1).** ``materialize_hash`` over content only; ``dtp_`` + hash is the deterministic,
CHECK-pinned revision id. NO free SQL, NO live cutoff value, NO upload time and NO catalog
watermark may enter identity — the policy names normalized logical column REFS and the runtime
value is a parameter (§6.5). :func:`_reject_literal_cutoff` and the closed predicate shape below
make that mechanical rather than a convention.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum, StrEnum
from typing import Any

from featuregen.materialize.canonical import materialize_hash
from featuregen.overlay.upload.object_ref import normalize_ref, parse_ref
from featuregen.overlay.upload.profile_vocab import TemporalStorageModel
from featuregen.overlay.upload.source_selection import (
    TEMPORAL_HISTORICAL_CURRENT_ONLY,
    TEMPORAL_MODEL_UNKNOWN,
    PolicyProvenanceV1,
    SelectionError,
    reject_freshness_keys,
)

_TEMPORAL_POLICY_CONTRACT = "dataset_temporal_policy_revision_v1"
_ROW_SELECTION_CONTRACT = "dataset_row_selection_v1"

#: Deterministic id prefix for a temporal-policy revision (the ``cpr_``/``pbr_``/``dsp_`` family).
TEMPORAL_POLICY_ID_PREFIX = "dtp_"


class TemporalSelectionKind(StrEnum):
    """HOW rows are chosen (§6.5). ``EXPLICIT_ONLY`` is the honest terminal state for a dataset
    that cannot answer the shape of question being asked — it asks for another source or an
    explicit limitation instead of quietly answering from today's row."""

    CURRENT_RECORD = "current_record"
    VALID_AT_REPORT_CUTOFF = "valid_at_report_cutoff"
    LATEST_SNAPSHOT_AS_OF = "latest_snapshot_as_of"
    EXPLICIT_ONLY = "explicit_only"


#: Which storage models can honestly answer a HISTORICAL question. `current_only` cannot by
#: definition; `scd1` overwrites in place, so it cannot either (§5.5).
_HISTORY_CAPABLE = frozenset({
    TemporalStorageModel.SCD2.value,
    TemporalStorageModel.SNAPSHOT.value,
    TemporalStorageModel.EVENT_LOG.value,
})

#: The column refs each selection kind REQUIRES a policy to name. Half-open `[from, to)` SCD2
#: intervals reuse `data_agent.dimensions`' semantics; this table only says which refs must exist.
_REQUIRED_REFS: dict[str, tuple[str, ...]] = {
    TemporalSelectionKind.VALID_AT_REPORT_CUTOFF.value: ("effective_from_ref", "effective_to_ref"),
    TemporalSelectionKind.LATEST_SNAPSHOT_AS_OF.value: ("snapshot_ref",),
    TemporalSelectionKind.CURRENT_RECORD.value: (),   # current_flag_ref OR the model implies it
    TemporalSelectionKind.EXPLICIT_ONLY.value: (),
}

#: The CLOSED predicate shape a row selection may carry. "No free SQL" is enforced by the key set +
#: the operator vocabulary, not by scanning strings for SQL keywords.
PREDICATE_KEYS = frozenset({"kind", "column_ref", "operator", "parameter_ref"})
PREDICATE_KINDS = frozenset({"effective_time", "availability_time", "current_flag",
                             "snapshot_time", "tie_break"})
PREDICATE_OPERATORS = frozenset({"<", "<=", "=", ">=", ">", "is_true"})


def _column_ref(raw: object, *, what: str) -> str:
    """Normalize one schema-preserving logical COLUMN ref; refuse a table ref or a blank."""
    if not isinstance(raw, str) or not raw.strip():
        raise SelectionError(f"{what} must be a non-empty logical column ref")
    try:
        source, schema, table, column = parse_ref(raw.strip().lower())
    except ValueError as exc:
        raise SelectionError(f"{what} {raw!r} is not a normalized logical ref: {exc}") from exc
    if column is None:
        raise SelectionError(
            f"{what} {raw!r} must address a COLUMN: a temporal policy references normalized "
            "logical column refs only, never a table and never a SQL fragment")
    return normalize_ref(source, schema, table, column)


def _reject_literal_cutoff(raw: str, *, what: str) -> str:
    """Refuse a runtime VALUE where a ref belongs (§6.5: a report date is a parameter).

    A literal cutoff inside a decision identity is the exact defect this guards: two replays of the
    same governed decision would hash differently purely because the report ran on another day."""
    text = raw.strip()
    if not text:
        raise SelectionError(f"{what} must be non-empty")
    if any(ch in text for ch in " \t\n'\"();"):
        raise SelectionError(
            f"{what} {raw!r} looks like a value or a fragment, not a ref: a runtime cutoff is a "
            "parameter, never concatenated into a decision")
    for parser in (date.fromisoformat, datetime.fromisoformat):
        try:
            parser(text)
        except ValueError:
            continue
        raise SelectionError(
            f"{what} {raw!r} is a literal date. The cutoff VALUE is a runtime parameter; identity "
            "carries the ref it binds to, or the decision re-keys every day it is replayed")
    return text


def _predicate(payload: object, *, index: int) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise SelectionError(f"predicate_payloads[{index}] must be a JSON object")
    unknown = set(payload) - PREDICATE_KEYS
    if unknown:
        raise SelectionError(
            f"predicate_payloads[{index}] carries unknown keys {sorted(unknown)} "
            f"(allowed: {sorted(PREDICATE_KEYS)}). A predicate is a typed shape, never free SQL")
    kind = str(payload.get("kind", "")).strip().lower()
    if kind not in PREDICATE_KINDS:
        raise SelectionError(
            f"predicate_payloads[{index}].kind {kind!r} is not one of {sorted(PREDICATE_KINDS)}")
    operator = str(payload.get("operator", "")).strip().lower()
    if operator not in PREDICATE_OPERATORS:
        raise SelectionError(
            f"predicate_payloads[{index}].operator {operator!r} is not one of "
            f"{sorted(PREDICATE_OPERATORS)}")
    out: dict[str, Any] = {
        "kind": kind,
        "column_ref": _column_ref(payload.get("column_ref"),
                                  what=f"predicate_payloads[{index}].column_ref"),
        "operator": operator,
    }
    parameter_ref = payload.get("parameter_ref")
    if parameter_ref is not None:
        out["parameter_ref"] = _reject_literal_cutoff(
            str(parameter_ref), what=f"predicate_payloads[{index}].parameter_ref")
    return out


@dataclass(frozen=True, slots=True)
class DatasetTemporalPolicyRevisionV1:
    """One immutable temporal-policy revision for ONE dataset (§6.5).

    ``current_selection`` and ``historical_selection`` are separate on purpose (§5.5): a current
    request and a historical request are different questions, and a historical one must never fall
    back to today's row. ``availability_ref`` is kept DISTINCT from the effective-time refs — Task
    8 preserves separate effective-time and availability-time predicates rather than collapsing
    them into one "as of"."""

    dataset_logical_ref: str
    temporal_storage_model: TemporalStorageModel
    current_selection: TemporalSelectionKind
    historical_selection: TemporalSelectionKind
    effective_from_ref: str | None = None
    effective_to_ref: str | None = None
    snapshot_ref: str | None = None
    current_flag_ref: str | None = None
    availability_ref: str | None = None
    tie_break_refs: tuple[str, ...] = ()
    provenance: PolicyProvenanceV1 = field(default_factory=PolicyProvenanceV1)
    content_hash: str = ""
    revision_id: str = ""

    def __post_init__(self) -> None:
        from featuregen.overlay.upload.source_selection import normalize_dataset_ref

        object.__setattr__(self, "dataset_logical_ref", normalize_dataset_ref(
            self.dataset_logical_ref, what="dataset_logical_ref"))
        object.__setattr__(self, "temporal_storage_model", TemporalStorageModel(
            _enum_value(self.temporal_storage_model, TemporalStorageModel,
                        what="temporal_storage_model")))
        for name in ("current_selection", "historical_selection"):
            object.__setattr__(self, name, TemporalSelectionKind(
                _enum_value(getattr(self, name), TemporalSelectionKind, what=name)))
        for name in ("effective_from_ref", "effective_to_ref", "snapshot_ref", "current_flag_ref",
                     "availability_ref"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _column_ref(value, what=name))
        object.__setattr__(self, "tie_break_refs", tuple(
            _column_ref(r, what="tie_break_ref") for r in self.tie_break_refs))
        if len(set(self.tie_break_refs)) != len(self.tie_break_refs):
            raise SelectionError("tie_break_refs must be distinct")
        if not isinstance(self.provenance, PolicyProvenanceV1):
            raise SelectionError("provenance must be a PolicyProvenanceV1")

        # Capability FIRST: "this dataset keeps no history" is the root cause, and reporting a
        # missing effective_from_ref on a current-only table would send the reader to fix the wrong
        # thing (there is no such column to name).
        if (self.historical_selection is not TemporalSelectionKind.EXPLICIT_ONLY
                and self.temporal_storage_model.value not in _HISTORY_CAPABLE):
            raise SelectionError(
                f"{self.temporal_storage_model.value!r} keeps no history, so a historical selection "
                f"of {self.historical_selection.value!r} cannot be honoured; declare "
                f"{TemporalSelectionKind.EXPLICIT_ONLY.value!r} and let the request ask for another "
                f"source or an explicit limitation ({TEMPORAL_HISTORICAL_CURRENT_ONLY})")
        for kind in (self.current_selection, self.historical_selection):
            for required in _REQUIRED_REFS[kind.value]:
                if getattr(self, required) is None:
                    raise SelectionError(
                        f"selection kind {kind.value!r} requires {required}: a policy that names no "
                        "column cannot be compiled into a predicate, only guessed at")
        if (self.current_selection is TemporalSelectionKind.CURRENT_RECORD
                and self.current_flag_ref is None
                and self.temporal_storage_model is not TemporalStorageModel.CURRENT_ONLY):
            raise SelectionError(
                "current_record on a dataset that stores history needs current_flag_ref: without "
                "it 'the current row' is whichever row is read first")
        content_hash = materialize_hash(self.content_payload())
        object.__setattr__(self, "content_hash", content_hash)
        object.__setattr__(self, "revision_id", f"{TEMPORAL_POLICY_ID_PREFIX}{content_hash}")

    def content_payload(self) -> dict[str, Any]:
        payload = {
            "contract": _TEMPORAL_POLICY_CONTRACT,
            "dataset_logical_ref": self.dataset_logical_ref,
            "temporal_storage_model": self.temporal_storage_model.value,
            "current_selection": self.current_selection.value,
            "historical_selection": self.historical_selection.value,
            "effective_from_ref": self.effective_from_ref,
            "effective_to_ref": self.effective_to_ref,
            "snapshot_ref": self.snapshot_ref,
            "current_flag_ref": self.current_flag_ref,
            "availability_ref": self.availability_ref,
            "tie_break_refs": list(self.tie_break_refs),
            "authority": self.provenance.authority_axes(),
        }
        reject_freshness_keys(payload, where="a temporal policy")
        return payload

    def column_refs(self) -> tuple[str, ...]:
        """Every logical column ref the policy names — what the store validates at authoring."""
        named = [self.effective_from_ref, self.effective_to_ref, self.snapshot_ref,
                 self.current_flag_ref, self.availability_ref, *self.tie_break_refs]
        return tuple(sorted({ref for ref in named if ref}))


@dataclass(frozen=True, slots=True)
class DatasetRowSelectionV1:
    """The immutable record of WHICH rows answered one question, and under which policy (§6.5)."""

    dataset_logical_ref: str
    dataset_profile_hash: str
    temporal_policy_revision_id: str
    selection_kind: TemporalSelectionKind
    cutoff_value_ref: str | None = None
    predicate_payloads: tuple[dict[str, Any], ...] = ()
    content_hash: str = ""

    def __post_init__(self) -> None:
        from featuregen.overlay.upload.source_selection import normalize_dataset_ref

        object.__setattr__(self, "dataset_logical_ref", normalize_dataset_ref(
            self.dataset_logical_ref, what="dataset_logical_ref"))
        if not str(self.dataset_profile_hash or "").strip():
            raise SelectionError("dataset_profile_hash is required on a row selection")
        object.__setattr__(self, "dataset_profile_hash", self.dataset_profile_hash.strip())
        policy_id = str(self.temporal_policy_revision_id or "").strip()
        if not policy_id.startswith(TEMPORAL_POLICY_ID_PREFIX):
            raise SelectionError(
                f"temporal_policy_revision_id {policy_id!r} is not a "
                f"{TEMPORAL_POLICY_ID_PREFIX}<hash> revision id")
        object.__setattr__(self, "temporal_policy_revision_id", policy_id)
        object.__setattr__(self, "selection_kind", TemporalSelectionKind(
            _enum_value(self.selection_kind, TemporalSelectionKind, what="selection_kind")))
        if self.cutoff_value_ref is not None:
            object.__setattr__(self, "cutoff_value_ref", _reject_literal_cutoff(
                str(self.cutoff_value_ref), what="cutoff_value_ref"))
        object.__setattr__(self, "predicate_payloads", tuple(
            _predicate(p, index=i) for i, p in enumerate(self.predicate_payloads)))
        object.__setattr__(self, "content_hash", materialize_hash(self.content_payload()))

    def content_payload(self) -> dict[str, Any]:
        payload = {
            "contract": _ROW_SELECTION_CONTRACT,
            "dataset_logical_ref": self.dataset_logical_ref,
            "dataset_profile_hash": self.dataset_profile_hash,
            "temporal_policy_revision_id": self.temporal_policy_revision_id,
            "selection_kind": self.selection_kind.value,
            "cutoff_value_ref": self.cutoff_value_ref,
            # Predicate ORDER is preserved: predicates are conjunctive but a caller may have a
            # meaningful reading order, and unlike a candidate SET this list is authored, not
            # enumerated. Two policies differing only in predicate order are two policies.
            "predicate_payloads": list(self.predicate_payloads),
        }
        reject_freshness_keys(payload, where="a row selection")
        return payload


def _enum_value(raw: object, vocab: type[Enum], *, what: str) -> str:
    if isinstance(raw, vocab):
        return raw.value
    if isinstance(raw, str):
        try:
            return vocab(raw.strip().lower()).value
        except ValueError:
            pass
    raise SelectionError(f"{what} {raw!r} is not one of {sorted(m.value for m in vocab)}")


def temporal_policy_agreement(
    *,
    policy_model: TemporalStorageModel | str,
    load_bearing_model: str | None,
    displayed_model: str | None = None,
) -> str | None:
    """The Task-7 agreement rule. Returns a refusal code, or ``None`` when the policy may operate.

    Three cases, deliberately distinguished:

    * a current LOAD-BEARING profile model exists and CONTRADICTS the policy — refuse
      (:data:`TEMPORAL_MODEL_UNKNOWN` is not it; a contradiction is not an absence, so the caller
      gets ``TEMPORAL_MODEL_UNKNOWN`` only when nothing is known). A policy may not overrule a
      governed classification, because the classification is what the execution gates read.
    * a load-bearing model exists and AGREES — the policy operates on the governed value.
    * NO load-bearing model (absent, or only a PROPOSED/display value) — the POLICY is the
      operational declaration (D12.7). This is the case that keeps upload-only catalogs working:
      their temporal model was never attested by anything, and an LLM proposal must not be promoted
      to load-bearing just because a policy agrees with it. ``displayed_model`` is accepted only to
      make that distinction explicit at the call site; it never gates the answer.

    A policy declaring ``unknown`` declares nothing, and is refused with
    :data:`TEMPORAL_MODEL_UNKNOWN` regardless of the profile."""
    declared = _enum_value(policy_model, TemporalStorageModel, what="policy_model")
    if declared == TemporalStorageModel.UNKNOWN.value:
        return TEMPORAL_MODEL_UNKNOWN
    if load_bearing_model is None or not str(load_bearing_model).strip():
        return None                                  # the policy IS the declaration (D12.7)
    governed = str(load_bearing_model).strip().lower()
    if governed == TemporalStorageModel.UNKNOWN.value:
        return None                                  # "unknown" governs nothing
    if governed != declared:
        return TEMPORAL_MODEL_UNKNOWN
    return None


def contradicts_load_bearing_model(
    *, policy_model: TemporalStorageModel | str, load_bearing_model: str | None,
) -> bool:
    """True when a load-bearing profile value exists and names a DIFFERENT model.

    Split out from :func:`temporal_policy_agreement` so the store can say WHICH failure it hit:
    "you declared scd2 and the governed classification says snapshot" is a very different message
    from "nothing knows the temporal model of this dataset"."""
    if load_bearing_model is None or not str(load_bearing_model).strip():
        return False
    governed = str(load_bearing_model).strip().lower()
    if governed == TemporalStorageModel.UNKNOWN.value:
        return False
    return governed != _enum_value(policy_model, TemporalStorageModel, what="policy_model")
