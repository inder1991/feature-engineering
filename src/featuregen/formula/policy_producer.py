"""C-C11 — the S4 policy producer's LLM seam: bounded input, registered output, closed taxonomy.

**What this asks a model.** Given ONE occurrence (C-C7) and a bounded view of the column it binds
to, propose how that policy is REALIZED here: which column carries it, and what the source's
physical spelling of each semantic token is — ``debit`` is ``D`` in this ledger, ``DR`` in the next.
That is a genuine reading task over a source's conventions and exactly the kind of thing a model is
useful for. What it produces is an :class:`~featuregen.formula.policy_realization.
PolicyRealizationRevisionV1` with ``LLM_PROPOSED`` provenance, and C-C9 decides whether it may be
used and under what name.

**The input contract is BOUNDED, and the bound is a whitelist.** Only the fields
:class:`PolicyProducerInputV1` declares leave the building: the column's governed ref, its logical
type, and up to :data:`MAX_SAMPLED_VALUES` DISTINCT values with their row counts. Distinct values of
a direction flag are the source's vocabulary; whole rows are customer data, and one is not a
convenient shorthand for the other. Building the payload through a typed contract rather than a
free dict is what makes the bound reviewable — a new field is a diff, not an accident.

**The closed taxonomy rides INSIDE the repair loop.** JSON Schema cannot express *"``semantic_value``
must be one of the tokens for the kind THIS call is about"* — the legal set depends on the
occurrence, not on the schema. So it goes through ``drive_audited_structured_call``'s
``validate_semantics``, which runs it after the registered schema and inside the bounded repair
loop. A caller that checked afterwards would discard a whole answer the model was never asked to
fix, which is the defect that seam was built to remove.

**Replay and idempotency** come from threading a ``run_id`` and a ``logical_call_ref`` derived from
the OCCURRENCE HASH: the same occurrence in the same run resolves to the same logical call, so a
re-run replays rather than re-spends.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from featuregen.formula.policy_occurrences import PolicyOccurrenceV1
from featuregen.formula.policy_realization import (
    PolicyRealizationRevisionV1,
    RealizationProvenanceV1,
    family_key_for,
)
from featuregen.formula.schema_v3 import SELECTION_TOKENS, SelectionKind

__all__ = [
    "MAX_SAMPLED_VALUES",
    "POLICY_REALIZATION_SCHEMA_ID",
    "POLICY_REALIZATION_SCHEMA_VERSION",
    "PolicyProducerInputV1",
    "PolicyProducerRefused",
    "SampledValueV1",
    "logical_call_ref_for",
    "realization_from_payload",
    "semantic_tokens_for",
    "validate_policy_realization",
]

POLICY_REALIZATION_SCHEMA_ID = "policy_realization"
POLICY_REALIZATION_SCHEMA_VERSION = 1

#: A direction or status flag has a handful of distinct values. Twenty is generous for a vocabulary
#: and far too few to be a data export, which is the line this constant draws.
MAX_SAMPLED_VALUES = 20

#: Which semantic role maps to which selection kind's token set. Roles without a closed token
#: vocabulary are absent on purpose: ``reversal`` and ``currency_conversion`` are not realized as a
#: token map, and inventing an empty entry for them would make ``semantic_tokens_for`` answer
#: "no legal tokens" where the truth is "this role is not a token map at all".
_ROLE_SELECTION_KIND: Mapping[str, SelectionKind] = {
    "direction": SelectionKind.TRANSACTION_DIRECTION,
    "status": SelectionKind.ELIGIBILITY,
}


class PolicyProducerRefused(Exception):
    """The proposal cannot become a realization — named, never silently degraded."""


def semantic_tokens_for(occurrence: PolicyOccurrenceV1) -> frozenset[str]:
    """The CLOSED token set legal for this occurrence's role.

    Raises:
        PolicyProducerRefused: the role has no token vocabulary, so a value map is the wrong shape
            of answer for it and accepting one would invent a taxonomy.
    """
    kind = _ROLE_SELECTION_KIND.get(occurrence.semantic_role)
    if kind is None:
        raise PolicyProducerRefused(
            f"semantic role {occurrence.semantic_role!r} is not realized as a token map — "
            f"{sorted(_ROLE_SELECTION_KIND)} are. Accepting a value map here would invent a "
            f"taxonomy for a policy that does not have one")
    return SELECTION_TOKENS[kind]


@dataclass(frozen=True, slots=True)
class SampledValueV1:
    """One distinct value of the bound column, with how many rows carry it.

    The count is what makes the sample readable: ``D`` on 4.2M rows and ``d`` on 3 rows is a
    data-quality tail, not a second direction, and a model given only the values cannot tell.
    """

    value: str
    row_count: int

    def __post_init__(self) -> None:
        if self.row_count < 0:
            raise ValueError(f"row_count {self.row_count} is negative")


@dataclass(frozen=True, slots=True)
class PolicyProducerInputV1:
    """Everything that may leave the building for one policy-realization call — and nothing else.

    A whitelist rather than a redaction: what egresses is enumerated here, so widening it is a diff
    someone reviews rather than an extra key somebody added to a dict.
    """

    occurrence: PolicyOccurrenceV1
    column_logical_type: str
    sampled_values: tuple[SampledValueV1, ...]

    def __post_init__(self) -> None:
        if len(self.sampled_values) > MAX_SAMPLED_VALUES:
            raise ValueError(
                f"{len(self.sampled_values)} sampled values exceeds {MAX_SAMPLED_VALUES}: a "
                f"direction or status flag has a handful of distinct values, and a larger sample "
                f"stops being a vocabulary and starts being a data export")
        if len({s.value for s in self.sampled_values}) != len(self.sampled_values):
            raise ValueError(
                "sampled_values repeats a value: these are DISTINCT values with row counts, and a "
                "repeat means the counts were computed over something else")

    def egress_payload(self) -> dict[str, Any]:
        """The exact dict that egresses. Nothing derives fields from the occurrence beyond these."""
        return {
            "policy_kind": self.occurrence.policy_kind,
            "semantic_role": self.occurrence.semantic_role,
            "bound_column": self.occurrence.bound_column,
            "column_logical_type": self.column_logical_type,
            "legal_semantic_values": sorted(semantic_tokens_for(self.occurrence)),
            "sampled_values": [
                {"value": s.value, "row_count": s.row_count} for s in self.sampled_values],
        }


def validate_policy_realization(
    occurrence: PolicyOccurrenceV1,
) -> Any:
    """A ``validate_semantics`` callable for THIS occurrence — the rules JSON Schema cannot state.

    Returned as a closure over the occurrence because the legal token set depends on it. Runs inside
    ``drive_audited_structured_call``'s repair loop, so a model that uses a physical literal where a
    semantic token belongs is asked to fix it rather than having its whole answer discarded.
    """
    legal = semantic_tokens_for(occurrence)

    def _validate(payload: Mapping[str, Any]) -> None:
        column = str(payload.get("policy_column_ref", ""))
        if not column.strip():
            raise ValueError("policy_column_ref is empty: the realization names no column")
        if not column.startswith(f"{occurrence.bound_dataset}."):
            raise ValueError(
                f"policy_column_ref {column!r} is not a column of {occurrence.bound_dataset}: a "
                f"realization for this occurrence must read the dataset the occurrence is bound "
                f"to, and one naming another dataset would authorize a read nobody planned")

        entries = payload.get("value_map") or []
        if not entries:
            raise ValueError(
                "value_map is empty: a realization that maps no semantic value to a physical one "
                "cannot execute the selection it exists to answer")

        seen_semantic: set[str] = set()
        seen_physical: set[str] = set()
        for entry in entries:
            semantic = str(entry.get("semantic_value", ""))
            physical = str(entry.get("physical_value", ""))
            if semantic not in legal:
                raise ValueError(
                    f"semantic_value {semantic!r} is not one of {sorted(legal)} — the LEFT side of "
                    f"the map is a semantic token and the right side is this source's spelling of "
                    f"it. A physical literal on the left inverts the map")
            if not physical.strip():
                raise ValueError(
                    f"semantic_value {semantic!r} maps to an empty physical value, which selects "
                    f"no rows rather than the rows it names")
            if semantic in seen_semantic:
                raise ValueError(
                    f"semantic_value {semantic!r} is mapped twice: which physical value applies "
                    f"would be decided by list order")
            if physical in seen_physical:
                raise ValueError(
                    f"physical value {physical!r} is claimed by two semantic values: one column "
                    f"value cannot mean both, and a row would match both selections")
            seen_semantic.add(semantic)
            seen_physical.add(physical)

    return _validate


def logical_call_ref_for(occurrence: PolicyOccurrenceV1) -> str:
    """The replay key: the same occurrence resolves to the same logical call, so a re-run replays
    rather than re-spends."""
    return f"policy_realization:{occurrence.occurrence_hash}"


def realization_from_payload(
    payload: Mapping[str, Any],
    occurrence: PolicyOccurrenceV1,
    *,
    revision_id: str,
    executable_content_hash: str,
    cas_pointer: str,
) -> PolicyRealizationRevisionV1:
    """Turn a VALIDATED payload into an ``LLM_PROPOSED`` realization revision.

    Provenance is hardcoded, not a parameter. A producer that could be asked to stamp
    ``SOURCE_DERIVED`` would be one call-site away from laundering a proposal into evidence, and
    C-C9's whole table rests on that distinction being true rather than declared.
    """
    validate_policy_realization(occurrence)(payload)
    return PolicyRealizationRevisionV1(
        revision_id=revision_id,
        family_key=family_key_for(occurrence),
        executable_content_hash=executable_content_hash,
        cas_pointer=cas_pointer,
        provenance=RealizationProvenanceV1.LLM_PROPOSED,
        realizes_occurrences=(occurrence.occurrence_hash,),
    )


def sampled_values_from(rows: Sequence[tuple[str, int]]) -> tuple[SampledValueV1, ...]:
    """``(value, row_count)`` pairs as sampled values, ordered by descending count then value.

    Ordered so the payload is stable across two identical queries that happened to return rows in
    different orders — otherwise the request-identity hash moves for a reason that is not about the
    data.
    """
    return tuple(sorted((SampledValueV1(value=value, row_count=count) for value, count in rows),
                        key=lambda s: (-s.row_count, s.value)))
