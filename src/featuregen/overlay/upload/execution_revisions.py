"""C-B3/C-B4/C-B5 — the execution chain: executable feature, derived group, authorization envelope.

**Composed over opaque HASHES.** ``ExecutableFeatureRevisionV2`` names its bound formula and its
executable output policy by content hash rather than holding the objects, so this layer is frozen
now while S5 produces the instances. That is not a convenience: a revision that held the objects
could only be constructed once they existed, and the whole point of freezing the identity graph
early is that the shape stops moving before anything depends on it.

**The first durable group→member map.** Nothing maps a group to its members today —
``compile_feature_group`` builds one in memory and forgets it, so "which features are in this group"
is answerable only by replaying the request that produced it. :class:`DerivedGroupRevisionV2` is
where that stops being true.

**The authorization envelope binds EVERY member, not one.** A group has many features and each was
selected separately; an envelope naming a single selection would authorize a group by pointing at
one of its members. It also carries the planned IR hashes the leakage verdict actually screened, so
"authorized" and "screened" cannot drift apart — an artifact authorized for one target cannot be
reused for another, and that is checked here rather than assumed.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from featuregen.canonical import jcs_sha256
from featuregen.materialize.boundary_v2 import CompilationIdentityV2

__all__ = [
    "DerivedGroupRevisionV2",
    "ExecutableFeatureRevisionV2",
    "GenerationAuthorizationRevisionV2",
]


def _required(value: str, what: str, why: str) -> str:
    if not value.strip():
        raise ValueError(f"a blank {what}: {why}")
    return value


@dataclass(frozen=True, slots=True)
class ExecutableFeatureRevisionV2:
    """ONE feature, executable — composed entirely of content hashes.

    Constructible from strings alone (C-B3's gate), so this type is frozen here while S5 delivers
    the objects the hashes refer to.
    """

    revision_id: str
    feature_name: str
    selection_revision_id: str
    bound_formula_hash: str
    executable_output_hash: str
    ir_hash: str

    def __post_init__(self) -> None:
        for value, what in (
            (self.revision_id, "revision_id"),
            (self.feature_name, "feature_name"),
            (self.selection_revision_id, "selection_revision_id"),
            (self.bound_formula_hash, "bound_formula_hash"),
            (self.executable_output_hash, "executable_output_hash"),
            (self.ir_hash, "ir_hash"),
        ):
            _required(value, what,
                      "an executable feature revision names what it executes and what was chosen; "
                      "any part missing makes it unpinnable to either")

    def identity_payload(self) -> dict[str, Any]:
        return {
            "feature_name": self.feature_name,
            "selection_revision_id": self.selection_revision_id,
            "bound_formula_hash": self.bound_formula_hash,
            "executable_output_hash": self.executable_output_hash,
            "ir_hash": self.ir_hash,
        }

    @property
    def content_hash(self) -> str:
        return jcs_sha256(self.identity_payload())


@dataclass(frozen=True, slots=True)
class DerivedGroupRevisionV2:
    """A group, its members, and the identity of what compiling it produced.

    ``member_revision_ids`` is ORDERED by the executable revisions' own order, so the group's
    identity does not depend on which member happened to compile first. Membership is queryable
    from this record alone — that is the thing that does not exist today.
    """

    revision_id: str
    build_set_revision_id: str
    member_revision_ids: tuple[str, ...]
    materialization_contract_hash: str
    group_plan_hash: str
    compilation_identity: CompilationIdentityV2

    def __post_init__(self) -> None:
        _required(self.revision_id, "revision_id", "a group revision must name itself")
        _required(self.build_set_revision_id, "build_set_revision_id",
                  "a derived group with no build set has no declaration and no target reading")
        _required(self.materialization_contract_hash, "materialization_contract_hash",
                  "the contract is the group KEY; without it the group is not grouped by anything")
        _required(self.group_plan_hash, "group_plan_hash",
                  "the plan decides which columns are published")
        if not self.member_revision_ids:
            raise ValueError(
                "a derived group with no members publishes nothing, and its compilation identity "
                "would describe a compilation of nothing")
        if len(set(self.member_revision_ids)) != len(self.member_revision_ids):
            raise ValueError(
                f"the same member appears twice: {self.member_revision_ids!r}. A group's membership "
                f"is a set of distinct features, and a duplicate would double-count in every "
                f"coverage answer derived from it")
        if len(self.compilation_identity.ir_hashes) != len(self.member_revision_ids):
            raise ValueError(
                f"{len(self.member_revision_ids)} members but "
                f"{len(self.compilation_identity.ir_hashes)} IR hashes in the compilation identity: "
                f"the group and the thing it compiled to disagree about how many features there "
                f"are, and the smaller number is the one that gets published")

    def identity_payload(self) -> dict[str, Any]:
        return {
            "build_set_revision_id": self.build_set_revision_id,
            "member_revision_ids": list(self.member_revision_ids),
            "materialization_contract_hash": self.materialization_contract_hash,
            "group_plan_hash": self.group_plan_hash,
            "compilation_identity": self.compilation_identity.identity_payload(),
        }

    @property
    def content_hash(self) -> str:
        return jcs_sha256(self.identity_payload())


@dataclass(frozen=True, slots=True)
class GenerationAuthorizationRevisionV2:
    """The envelope: what was authorized, for which target, against which screened IRs.

    ``member_selection_revision_ids`` covers EVERY member. Revision 18 named one, which would
    authorize a group by pointing at a single feature's selection; a group has many features and
    each was chosen separately.

    ``screened_ir_hashes`` are the planned IR hashes the leakage verdict actually ran over. Carried
    so "authorized" and "screened" cannot drift: an envelope whose verdict screened a different set
    than the group contains is refused here rather than discovered at execution.
    """

    revision_id: str
    derived_group_revision_id: str
    member_selection_revision_ids: tuple[str, ...]
    target_reading_revision_id: str
    leakage_policy_version: int
    leakage_verdict: str
    screened_ir_hashes: tuple[str, ...]
    gate2_token_ir_hashes: tuple[str, ...]

    def __post_init__(self) -> None:
        _required(self.revision_id, "revision_id", "an authorization must name itself")
        _required(self.derived_group_revision_id, "derived_group_revision_id",
                  "an authorization with no group authorizes nothing in particular")
        _required(self.target_reading_revision_id, "target_reading_revision_id",
                  "a generation is authorized FOR a target; without the exact reading, an artifact "
                  "authorized for one target could be reused for another")
        _required(self.leakage_verdict, "leakage_verdict",
                  "an envelope with no verdict records that a gate ran without recording what it "
                  "decided")
        if not self.member_selection_revision_ids:
            raise ValueError(
                "an authorization naming no member selections authorizes a group without recording "
                "what anyone chose")
        if not self.screened_ir_hashes:
            raise ValueError(
                "an authorization with no screened IR hashes claims a leakage verdict over nothing")
        if set(self.screened_ir_hashes) != set(self.gate2_token_ir_hashes):
            missing = sorted(set(self.gate2_token_ir_hashes) - set(self.screened_ir_hashes))
            extra = sorted(set(self.screened_ir_hashes) - set(self.gate2_token_ir_hashes))
            raise ValueError(
                "the leakage verdict screened a different set of IRs than Gate 2 authorized"
                + (f"; UNSCREENED {missing}" if missing else "")
                + (f"; SCREENED-BUT-UNAUTHORIZED {extra}" if extra else "")
                + ". These two must cover the same features, or the group ships a feature nobody "
                  "screened and the envelope still reads as authorized")

    def authorizes_target(self, target_reading_revision_id: str) -> bool:
        """Whether this envelope authorizes generation for THAT exact target reading.

        A method rather than a field comparison a caller writes each time: "a generation is
        authorized FOR a target" is the invariant, and it should be asked in one place.
        """
        return self.target_reading_revision_id == target_reading_revision_id

    def identity_payload(self) -> dict[str, Any]:
        return {
            "derived_group_revision_id": self.derived_group_revision_id,
            "member_selection_revision_ids": list(self.member_selection_revision_ids),
            "target_reading_revision_id": self.target_reading_revision_id,
            "leakage_policy_version": self.leakage_policy_version,
            "leakage_verdict": self.leakage_verdict,
            "screened_ir_hashes": sorted(self.screened_ir_hashes),
            "gate2_token_ir_hashes": sorted(self.gate2_token_ir_hashes),
        }

    @property
    def content_hash(self) -> str:
        return jcs_sha256(self.identity_payload())
