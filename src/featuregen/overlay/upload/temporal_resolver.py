"""The temporal RESOLVER — a request plus a policy in, a :class:`DatasetRowSelectionV1` out.

The second half of the Release-B decision (§5.7: source choice and row choice are separate and
separately explainable). Task 7 froze the §6.5 contracts and the eight closed refusal codes; this
module is their producer for the row half, and it is where the two NEW ENGINES are ADAPTED — they
are BUILT in ``data_agent`` (``dimensions.CURRENT_VALUE`` and ``snapshots``), because that is where
the dialect-SQL path lives.

**A current request and a historical request are different questions (§5.5).** The policy declares
a selection kind for each, and the resolver reads the one the request asked for. A HISTORICAL
request against a dataset that keeps no history resolves to ``EXPLICIT_ONLY`` and refuses
:data:`TEMPORAL_HISTORICAL_CURRENT_ONLY` — it never quietly answers from today's row, which is the
failure mode that looks completely reasonable in the output.

**Effective time and availability time stay SEPARATE.** A policy may name both an
``effective_from``/``effective_to`` interval and an ``availability_ref``; they answer different
questions ("when was this true" vs "when could we have known it") and collapsing them into one
"as of" is how a look-ahead leak gets built on purpose. The emitted ``predicate_payloads`` keep
their distinct ``kind``, and a test pins that both survive.

**The runtime cutoff is a PARAMETER, never a literal in the decision.** The row selection carries
the cutoff's REF; the engine adapters take the VALUE. Two replays of one governed decision must
hash identically however many days apart they run
(``temporal_policy._reject_literal_cutoff`` enforces the ref half mechanically).

**What this module does NOT own.** SCD overlap detection stays exactly where it is — the existing
``data_agent.analysis.assert_no_dimension_overlap`` refusal, which is a statement about the DATA and
runs at execution. :data:`ENGINE_REFUSAL_TO_SELECTION` is the one-line translation from that
engine's code to the closed selection spelling, so the clarification Task 7 wrote renders; it does
not re-implement the check, and it deliberately does not make overlap a learning gap (the
``REFUSAL_TO_GAP`` argument in ``data_agent.learning``).
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from featuregen.overlay.upload.object_ref import parse_ref
from featuregen.overlay.upload.source_selection import (
    TEMPORAL_HISTORICAL_CURRENT_ONLY,
    TEMPORAL_MODEL_UNKNOWN,
    TEMPORAL_SCD_OVERLAP,
    TEMPORAL_SNAPSHOT_TIE,
    SelectionError,
)
from featuregen.overlay.upload.source_selector import SelectionRefusalV1
from featuregen.overlay.upload.temporal_policy import (
    DatasetRowSelectionV1,
    DatasetTemporalPolicyRevisionV1,
    TemporalSelectionKind,
    temporal_policy_agreement,
)

if TYPE_CHECKING:                       # import weight — the `source_selection` precedent
    from featuregen.contracts import DbConn
    from featuregen.data_agent.dimensions import DimensionAttributionPolicyV1
    from featuregen.data_agent.snapshots import LatestSnapshotPolicyV1

#: The ENGINE's own refusal codes, in the closed SELECTION vocabulary. Two entries, both
#: deliberate: the overlap check already exists and keeps its data-quality identity, and the
#: snapshot tie is new and uses the closed code from birth.
ENGINE_REFUSAL_TO_SELECTION: dict[str, str] = {
    "ATTRIBUTION_OVERLAPPING_RECORDS": TEMPORAL_SCD_OVERLAP,
    TEMPORAL_SNAPSHOT_TIE: TEMPORAL_SNAPSHOT_TIE,
}

#: The selection kinds that read rows AS OF an instant, and therefore cannot be resolved without
#: the REF the runtime cutoff binds to. `current_record` is not one: "the row the source flags as
#: current" needs no instant at all.
_CUTOFF_REQUIRING_KINDS: frozenset[TemporalSelectionKind] = frozenset({
    TemporalSelectionKind.VALID_AT_REPORT_CUTOFF,
    TemporalSelectionKind.LATEST_SNAPSHOT_AS_OF,
})


class RequestTemporality(StrEnum):
    """WHICH question is being asked of the dataset. Not a property of the dataset."""

    CURRENT = "current"
    HISTORICAL = "historical"


@dataclass(frozen=True, slots=True)
class RowSelectionOutcomeV1:
    """Exactly one of ``row_selection`` / ``refusal``, plus the policy the answer came from."""

    dataset_logical_ref: str
    row_selection: DatasetRowSelectionV1 | None = None
    refusal: SelectionRefusalV1 | None = None
    policy: DatasetTemporalPolicyRevisionV1 | None = None

    def __post_init__(self) -> None:
        if (self.row_selection is None) == (self.refusal is None):
            raise SelectionError(
                "a row-selection outcome is exactly one of a selection or a refusal")

    @property
    def resolved(self) -> bool:
        return self.row_selection is not None


def _column(ref: str) -> str:
    """The BARE column name a predicate renders. The ref's shape is the contract."""
    _source, _schema, _table, column = parse_ref(ref)
    if column is None:
        raise SelectionError(f"{ref!r} must address a column")
    return column


def _predicate(kind: str, column_ref: str, operator: str,
               parameter_ref: str | None = None) -> dict[str, object]:
    payload: dict[str, object] = {"kind": kind, "column_ref": column_ref, "operator": operator}
    if parameter_ref is not None:
        payload["parameter_ref"] = parameter_ref
    return payload


def resolve_row_selection(
    conn: DbConn,
    *,
    dataset_logical_ref: str,
    dataset_profile_hash: str,
    temporality: RequestTemporality,
    cutoff_value_ref: str | None = None,
    roles: Sequence[str] = (),
) -> RowSelectionOutcomeV1:
    """Resolve WHICH ROWS of one dataset answer the question being asked.

    ``cutoff_value_ref`` is the REF the runtime cutoff binds to (a request parameter's name), never
    a date: the two kinds that need one RETURN A REFUSAL without it — like every other outcome in
    this module — and a literal is refused by the contract.
    """
    from featuregen.overlay.upload.temporal_policy_store import (
        current_temporal_policy,
        load_bearing_temporal_model,
    )

    found = current_temporal_policy(conn, dataset_logical_ref)
    if found is None:
        return _refuse(dataset_logical_ref, TEMPORAL_MODEL_UNKNOWN, (
            f"nothing declares how {dataset_logical_ref!r} stores history, so nothing can say which "
            "of its rows answers this question. Declare a temporal policy for the dataset"),
            missing="temporal_policy")
    policy, _pointer = found

    source = dataset_logical_ref.split("::", 1)[0]
    load_bearing, _displayed = load_bearing_temporal_model(
        conn, source=source, dataset_logical_ref=dataset_logical_ref)
    disagreement = temporal_policy_agreement(
        policy_model=policy.temporal_storage_model, load_bearing_model=load_bearing)
    if disagreement is not None:
        return _refuse(dataset_logical_ref, disagreement, (
            f"the temporal policy for {dataset_logical_ref!r} declares "
            f"{policy.temporal_storage_model.value!r} and the governed classification says "
            f"{load_bearing!r}; a policy may not overrule the classification the execution gates "
            "read"), policy=policy)

    kind = (policy.current_selection if temporality is RequestTemporality.CURRENT
            else policy.historical_selection)
    if kind in _CUTOFF_REQUIRING_KINDS and not cutoff_value_ref:
        # RETURNED, NOT RAISED. This was the ONE place the new code raised a `SelectionError`, and
        # the caller's blanket `except SelectionError: continue` then erased it: the plan came back
        # with no row rule, no refusal, and `resolved` True — the agent silently had no row rule
        # while claiming resolution, which is worse than either outcome on its own.
        #
        # VOCABULARY ADJUDICATION (the closed eight). TEMPORAL_MODEL_UNKNOWN, and deliberately not
        # TEMPORAL_HISTORICAL_CURRENT_ONLY: that code says the DATASET keeps only current values,
        # which on an SCD2 table asked without a cutoff would be a plain falsehood about the data,
        # and a refusal may not assert something untrue to get a better-worded question. This code
        # already covers "the temporal model is not SETTLED" in both directions — absence AND
        # contradiction (`temporal_policy_agreement` records that reading) — and a row rule that
        # cannot be applied because the request names no as-of instant is exactly that: unsettled,
        # not false. The DETAIL carries the actual fix, because the shared clarification cannot.
        return _refuse(dataset_logical_ref, TEMPORAL_MODEL_UNKNOWN, (
            f"the temporal policy for {dataset_logical_ref!r} answers this question with "
            f"{kind.value!r}, which reads rows AS OF an instant, and the request names no "
            "parameter for the report cutoff to bind to. Without one the row rule is 'whatever is "
            "there now', which is a different question — ask with a report cutoff, or declare a "
            "row rule that does not need one"), policy=policy, missing="report_cutoff")
    if kind is TemporalSelectionKind.EXPLICIT_ONLY:
        if temporality is RequestTemporality.HISTORICAL:
            return _refuse(dataset_logical_ref, TEMPORAL_HISTORICAL_CURRENT_ONLY, (
                f"{dataset_logical_ref!r} stores {policy.temporal_storage_model.value!r}, so it "
                "holds no row for an earlier date. Answering from today's row would look entirely "
                "reasonable and be a different answer — name a dataset that holds the history, or "
                "accept the limitation explicitly"), policy=policy)
        return _refuse(dataset_logical_ref, TEMPORAL_MODEL_UNKNOWN, (
            f"the temporal policy for {dataset_logical_ref!r} declares no rule for a CURRENT "
            "question; nothing says which of its rows is the current one"), policy=policy,
            missing="current_row_rule")

    predicates = _predicates_for(kind, policy, cutoff_value_ref)
    return RowSelectionOutcomeV1(
        dataset_logical_ref=dataset_logical_ref, policy=policy,
        row_selection=DatasetRowSelectionV1(
            dataset_logical_ref=dataset_logical_ref,
            dataset_profile_hash=dataset_profile_hash,
            temporal_policy_revision_id=policy.revision_id,
            selection_kind=kind,
            cutoff_value_ref=cutoff_value_ref,
            predicate_payloads=tuple(predicates)))


def _predicates_for(kind: TemporalSelectionKind, policy: DatasetTemporalPolicyRevisionV1,
                    cutoff_value_ref: str | None) -> list[dict[str, object]]:
    """The typed, closed-shape row filter for one selection kind. Never free SQL.

    The cutoff's PRESENCE is decided by :func:`resolve_row_selection` — as a returned refusal, not
    an exception — so this function is only ever reached for a kind whose inputs are all there."""
    if kind is TemporalSelectionKind.VALID_AT_REPORT_CUTOFF:
        # HALF-OPEN [from, to) — the same convention `data_agent.dimensions` renders, stated here
        # as the two complementary operators rather than re-derived.
        predicates = [
            _predicate("effective_time", policy.effective_from_ref, "<=", cutoff_value_ref),
            _predicate("effective_time", policy.effective_to_ref, ">", cutoff_value_ref),
        ]
    elif kind is TemporalSelectionKind.LATEST_SNAPSHOT_AS_OF:
        predicates = [_predicate("snapshot_time", policy.snapshot_ref, "<=", cutoff_value_ref)]
    elif kind is TemporalSelectionKind.CURRENT_RECORD:
        # No flag is legal ONLY on a current-only dataset, where every row IS the current row and
        # the honest predicate set is EMPTY (`DatasetTemporalPolicyRevisionV1` enforces that).
        predicates = ([_predicate("current_flag", policy.current_flag_ref, "is_true")]
                      if policy.current_flag_ref else [])
    else:                                                   # pragma: no cover - guarded upstream
        raise SelectionError(f"no predicate shape for selection kind {kind.value!r}")

    # AVAILABILITY TIME IS A SEPARATE PREDICATE, always appended last and never merged into the
    # effective-time pair. "When was this true" and "when could we have known it" are different
    # questions, and one combined "as of" answers neither honestly.
    if policy.availability_ref and cutoff_value_ref is not None:
        predicates.append(
            _predicate("availability_time", policy.availability_ref, "<=", cutoff_value_ref))
    return predicates


def _refuse(dataset_logical_ref: str, code: str, detail: str,
            policy: DatasetTemporalPolicyRevisionV1 | None = None,
            missing: str | None = None) -> RowSelectionOutcomeV1:
    return RowSelectionOutcomeV1(
        dataset_logical_ref=dataset_logical_ref, policy=policy,
        refusal=SelectionRefusalV1(code=code, subject_refs=(dataset_logical_ref,), detail=detail,
                                   missing=missing))


# ── engine adapters: a governed decision -> the thing that renders SQL ───────────────────────────
#
# The decision is immutable and carries REFS; the engines take the runtime VALUE. These two
# functions are the only place the two meet, which is what keeps a report date out of a decision
# identity and out of every replay.


def attribution_policy_for(policy: DatasetTemporalPolicyRevisionV1, *, kind: TemporalSelectionKind,
                           cutoff_value: str = "") -> DimensionAttributionPolicyV1:
    """ENGINE A / the reused SCD2 engine, per the resolved kind.

    ``valid_at_report_cutoff`` -> the existing half-open interval attribution (REUSE, unchanged);
    ``current_record`` -> the ``current_value`` basis this release added to the same engine, so
    there is exactly ONE interval renderer and one place the half-open rule lives.
    """
    from featuregen.data_agent.dimensions import AttributionBasis, DimensionAttributionPolicyV1

    if kind is TemporalSelectionKind.CURRENT_RECORD:
        return DimensionAttributionPolicyV1(
            attribution_basis=AttributionBasis.CURRENT_VALUE,
            effective_from_column=_column(policy.effective_from_ref)
            if policy.effective_from_ref else "",
            effective_to_column=_column(policy.effective_to_ref) if policy.effective_to_ref else "",
            current_flag_column=_column(policy.current_flag_ref) if policy.current_flag_ref else "",
            report_cutoff="")
    if kind is TemporalSelectionKind.VALID_AT_REPORT_CUTOFF:
        return DimensionAttributionPolicyV1(
            attribution_basis=AttributionBasis.REPORT_CUTOFF,
            effective_from_column=_column(policy.effective_from_ref),
            effective_to_column=_column(policy.effective_to_ref),
            report_cutoff=cutoff_value)
    raise SelectionError(
        f"{kind.value!r} is not an interval selection; {TemporalSelectionKind.LATEST_SNAPSHOT_AS_OF.value!r} "
        "is compiled by the snapshot engine, not the attribution one")


def snapshot_policy_for(policy: DatasetTemporalPolicyRevisionV1, *, cutoff_value: str,
                        key_column: str) -> LatestSnapshotPolicyV1:
    """ENGINE B. The greatest snapshot at or before the cutoff, per entity, with the policy's own
    governed tie-breakers — and a refusal, not a pick, when they do not separate two rows."""
    from featuregen.data_agent.snapshots import LatestSnapshotPolicyV1, SnapshotScope

    if not policy.snapshot_ref:
        raise SelectionError(
            f"{policy.dataset_logical_ref!r} declares latest_snapshot_as_of with no snapshot_ref")
    return LatestSnapshotPolicyV1(
        snapshot_column=_column(policy.snapshot_ref),
        cutoff=cutoff_value,
        scope=SnapshotScope.PER_ENTITY,
        key_column=key_column,
        tie_break_columns=tuple(_column(ref) for ref in policy.tie_break_refs))


def selection_code_for_engine_refusal(code: str) -> str | None:
    """The closed selection spelling of an ENGINE refusal, or ``None`` when it is not one.

    ``None`` is the honest answer for a capability limit or a malformed request — the same default
    ``learning.record_refusal`` takes, and for the same reason: an unrecognised code is far more
    likely to be an outage than a governed decision."""
    return ENGINE_REFUSAL_TO_SELECTION.get(code)
