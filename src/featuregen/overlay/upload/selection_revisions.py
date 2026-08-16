"""C-B1/C-B2/C-B5b — the selection chain as APPEND-ONLY revisions.

**What is wrong today.** ``record_target_reading`` does an ``UPDATE contract_intent SET target_ref
= …`` — destructive, with no provenance guard, and it stores ``target_ref`` while dropping
``catalog_source``. Three consequences: the reading a leakage gate ran against is gone the moment
anyone re-reads; a person's confirmed target can be replaced by an exploration declaration with
nothing recording that it happened; and two catalogs that both contain ``public.txns.churned`` are
indistinguishable.

**The union is DISCRIMINATED, so the forbidden fields cannot be present.**
:class:`PredictionTargetV1` requires ref, type and horizon; :class:`ExplorationTargetV1` has no such
fields to set. That is stronger than nulling them, which is what the current code does: a nulled
column still admits a value, so "exploring, but somebody wrote a horizon" stays representable and
somebody eventually reads it.

**ONE fact about the source, not two.** A canonical logical ref already contains its catalog
(``hdfc::public.txns.churned``), so this stores only the ref and DERIVES ``catalog_source`` from it.
The plan offered "or enforce that the parsed source equals ``catalog_source``"; storing one fact is
better than checking two, because a check can be skipped and a derivation cannot.

**A human confirmation is never silently erased.** Superseding a human-origin reading with an
exploration declaration requires naming who acknowledged the loss. A person said "predict churn";
an exploration declaration that quietly removes that target is how a governed run loses its subject
between one screen and the next.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from featuregen.canonical import jcs_sha256

__all__ = [
    "NOT_APPLICABLE_EXPLORATION",
    "BuildDeclarationV1",
    "BuildSetRevisionV1",
    "ExplorationTargetV1",
    "FeatureSelectionRevisionV1",
    "PredictionTargetV1",
    "refuse_multi_grain",
    "TargetProvenanceV1",
    "TargetReadingRevisionV1",
    "supersede_target_reading",
]

#: A leakage gate has nothing to guard when no target was declared. Named rather than reported as a
#: pass, because "no leakage found" and "there was nothing to look for" are different answers.
NOT_APPLICABLE_EXPLORATION = "NOT_APPLICABLE_EXPLORATION"


class TargetProvenanceV1(StrEnum):
    """The existing closed vocabulary, carried verbatim from ``record_target_reading``."""

    #: The fuzzy path — a person clicked.
    HUMAN_CONFIRMED = "human_confirmed"
    #: The person literally named the column: human-origin by construction, recorded without a click.
    USER_TYPED = "user_typed"
    #: An explicit no-target declaration.
    EXPLORING = "exploring"

    @property
    def is_human_origin(self) -> bool:
        return self is not TargetProvenanceV1.EXPLORING


def _split_ref(logical_ref: str) -> tuple[str, str]:
    source, _, object_ref = logical_ref.partition("::")
    if not _ or not source.strip() or not object_ref.strip():
        raise ValueError(
            f"target_logical_ref {logical_ref!r} is not a canonical '<source>::<object>' ref. The "
            f"source is what distinguishes two catalogs that both contain the same table and "
            f"column, and a bare 'public.txns.churned' names a target in neither")
    return source, object_ref


@dataclass(frozen=True, slots=True)
class PredictionTargetV1:
    """A declared prediction target. All three fields required — a target with no horizon is not one."""

    target_logical_ref: str
    target_type: str
    horizon_days: int

    def __post_init__(self) -> None:
        _split_ref(self.target_logical_ref)
        if not self.target_type.strip():
            raise ValueError("a prediction target with no type cannot be checked against anything")
        if self.horizon_days <= 0:
            raise ValueError(
                f"horizon_days={self.horizon_days} is not a horizon: a prediction over zero or "
                f"negative days has no future to predict into")

    @property
    def catalog_source(self) -> str:
        """DERIVED from the ref, never stored beside it — a derivation cannot be skipped."""
        return _split_ref(self.target_logical_ref)[0]

    @property
    def leakage_applicable(self) -> bool:
        return True

    def identity_payload(self) -> dict[str, Any]:
        return {"kind": "prediction", "target_logical_ref": self.target_logical_ref,
                "target_type": self.target_type, "horizon_days": self.horizon_days}


@dataclass(frozen=True, slots=True)
class ExplorationTargetV1:
    """An explicit no-target declaration.

    Carries no ref, type or horizon — not nulled ones. A nulled column still admits a value, so
    "exploring, but somebody wrote a horizon" would stay representable and somebody would read it.
    """

    @property
    def leakage_applicable(self) -> bool:
        return False

    @property
    def leakage_result(self) -> str:
        return NOT_APPLICABLE_EXPLORATION

    def identity_payload(self) -> dict[str, Any]:
        return {"kind": "exploration"}


TargetReadingV1 = PredictionTargetV1 | ExplorationTargetV1


@dataclass(frozen=True, slots=True)
class TargetReadingRevisionV1:
    """One APPEND-ONLY reading of what a build is predicting.

    ``supersedes_revision_id`` makes the chain readable: the reading a leakage gate ran against is
    still there after somebody changes their mind, which is the whole difference from an UPDATE.
    """

    revision_id: str
    intent_id: str
    reading: TargetReadingV1
    provenance: TargetProvenanceV1
    confirmed_by: str | None = None
    supersedes_revision_id: str | None = None
    acknowledged_human_loss_by: str | None = None

    def __post_init__(self) -> None:
        if not self.revision_id.strip() or not self.intent_id.strip():
            raise ValueError(
                "a target reading revision must name itself and the intent it governs, or it "
                "cannot be superseded by anything or found again")
        exploring = self.provenance is TargetProvenanceV1.EXPLORING
        if exploring and isinstance(self.reading, PredictionTargetV1):
            raise ValueError(
                "provenance 'exploring' with a prediction target: the declaration says no target "
                "was chosen and the reading names one, and a reader cannot tell which is true")
        if not exploring and isinstance(self.reading, ExplorationTargetV1):
            raise ValueError(
                f"provenance {self.provenance.value!r} with an exploration reading: a human-origin "
                f"provenance claims a person named a target, and this reading has none")

    @property
    def catalog_source(self) -> str | None:
        return (self.reading.catalog_source
                if isinstance(self.reading, PredictionTargetV1) else None)

    def identity_payload(self) -> dict[str, Any]:
        return {
            "intent_id": self.intent_id,
            "reading": self.reading.identity_payload(),
            "provenance": self.provenance.value,
            "confirmed_by": self.confirmed_by,
            "supersedes_revision_id": self.supersedes_revision_id,
            "acknowledged_human_loss_by": self.acknowledged_human_loss_by,
        }

    @property
    def content_hash(self) -> str:
        """Content identity. Deliberately excludes ``revision_id``: the id names the row, the hash
        names what it says, and two ids for one reading is what append-only produces."""
        return jcs_sha256(self.identity_payload())


def supersede_target_reading(
    previous: TargetReadingRevisionV1,
    *,
    revision_id: str,
    reading: TargetReadingV1,
    provenance: TargetProvenanceV1,
    confirmed_by: str | None = None,
    acknowledged_human_loss_by: str | None = None,
) -> TargetReadingRevisionV1:
    """A NEW revision superseding ``previous`` — never an edit of it.

    Raises:
        ValueError: ``previous`` is human-origin and this supersedes it with an exploration
            declaration without naming who acknowledged the loss.
    """
    if previous.provenance.is_human_origin and provenance is TargetProvenanceV1.EXPLORING:
        if not (acknowledged_human_loss_by or "").strip():
            raise ValueError(
                f"revision {previous.revision_id} was confirmed by a person "
                f"({previous.provenance.value}) and this would replace it with an exploration "
                f"declaration, removing the target entirely. Name who acknowledged that: a "
                f"governed run losing its subject between one screen and the next is exactly what "
                f"an append-only chain exists to make visible")
    return TargetReadingRevisionV1(
        revision_id=revision_id, intent_id=previous.intent_id, reading=reading,
        provenance=provenance, confirmed_by=confirmed_by,
        supersedes_revision_id=previous.revision_id,
        acknowledged_human_loss_by=acknowledged_human_loss_by)


# ── C-B2: the selection ──────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class FeatureSelectionRevisionV1:
    """WHICH served option a person selected, pinned to the exact thing that served it.

    Migration 1063 records every option SERVED, not which was chosen, so this record is genuinely
    new — it PINS 1063's identity rather than inventing one. All five pins are required: an option
    id without the considered revision it came from could name a different option in a later run.

    Constructible with NO definition, deliberately: ``FeatureDefinitionV1`` is created or resolved
    at authoring, and requiring one here would mean a person cannot choose a feature before the
    system has decided what to call it.
    """

    revision_id: str
    target_reading_revision_id: str
    considered_revision_id: str
    option_id: str
    decision_id: str
    planning_request_hash: str
    binding_plan_hash: str

    def __post_init__(self) -> None:
        for value, what in (
            (self.revision_id, "revision_id"),
            (self.target_reading_revision_id, "target_reading_revision_id"),
            (self.considered_revision_id, "considered_revision_id"),
            (self.option_id, "option_id"),
            (self.decision_id, "decision_id"),
            (self.planning_request_hash, "planning_request_hash"),
            (self.binding_plan_hash, "binding_plan_hash"),
        ):
            if not value.strip():
                raise ValueError(
                    f"a feature selection with a blank {what} does not pin what was selected: an "
                    f"option id alone can name a different option in a later run")

    def identity_payload(self) -> dict[str, Any]:
        return {
            "target_reading_revision_id": self.target_reading_revision_id,
            "considered_revision_id": self.considered_revision_id,
            "option_id": self.option_id,
            "decision_id": self.decision_id,
            "planning_request_hash": self.planning_request_hash,
            "binding_plan_hash": self.binding_plan_hash,
        }

    @property
    def content_hash(self) -> str:
        return jcs_sha256(self.identity_payload())


# ── C-B5b: the missing root ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class BuildDeclarationV1:
    """What a build set is FOR — one declaration per build set.

    Per build set and not per derived group, because a derived group does not exist until S6 and a
    declaration that could only be attached to one would have nowhere to live during selection.
    """

    entity: str
    grain_keys: tuple[str, ...]
    purpose: str

    def __post_init__(self) -> None:
        if not self.entity.strip():
            raise ValueError("a build declaration must name the entity it is about")
        if not self.grain_keys:
            raise ValueError(
                "a build declaration with no grain keys describes no population: every feature in "
                "the set would be computed at a grain nobody stated")
        if len(set(self.grain_keys)) != len(self.grain_keys):
            raise ValueError(f"grain_keys repeats a key: {self.grain_keys!r}")

    def identity_payload(self) -> dict[str, Any]:
        return {"entity": self.entity, "grain_keys": list(self.grain_keys),
                "purpose": self.purpose}


@dataclass(frozen=True, slots=True)
class BuildSetRevisionV1:
    """The root the UI's build-set/child-group hierarchy needs, and which does not exist today.

    ``selection_revision_ids`` is ORDERED because the order a person chose features in is a fact
    about the build, and a set would discard it. Derived access or sensitivity differences may still
    split this into several groups later — that is S6's, and it is why the declaration lives here
    rather than on a group.
    """

    revision_id: str
    target_reading_revision_id: str
    selection_revision_ids: tuple[str, ...]
    declaration: BuildDeclarationV1

    def __post_init__(self) -> None:
        if not self.revision_id.strip():
            raise ValueError("a build set revision must name itself")
        if not self.target_reading_revision_id.strip():
            raise ValueError(
                "a build set with no target reading is a set of features predicting nothing in "
                "particular; the reading is the EXACT revision, so a later re-read cannot silently "
                "change what this set was built for")
        if not self.selection_revision_ids:
            raise ValueError("a build set with no selections builds nothing")
        if len(set(self.selection_revision_ids)) != len(self.selection_revision_ids):
            raise ValueError(
                f"the same selection appears twice: {self.selection_revision_ids!r}. Order is "
                f"meaningful here, so a duplicate makes 'which position is this feature in' "
                f"unanswerable")

    def identity_payload(self) -> dict[str, Any]:
        return {
            "target_reading_revision_id": self.target_reading_revision_id,
            "selection_revision_ids": list(self.selection_revision_ids),
            "declaration": self.declaration.identity_payload(),
        }

    @property
    def content_hash(self) -> str:
        return jcs_sha256(self.identity_payload())


def refuse_multi_grain(declarations: Sequence[BuildDeclarationV1]) -> None:
    """Refuse a build set whose declarations disagree about grain (C-B5b's S11 gate).

    ONE declaration per build set is the rule; this is what enforces it when a caller assembles a
    set from parts. Two grains in one set means two populations, and every downstream identity —
    spine, contract, group — is keyed on exactly one.
    """
    grains = {(d.entity, d.grain_keys) for d in declarations}
    if len(grains) > 1:
        raise ValueError(
            f"this build set carries {len(grains)} distinct grains "
            f"({sorted((e, list(k)) for e, k in grains)}): two grains are two populations, and the "
            f"spine, contract and group identities downstream are each keyed on exactly one")

