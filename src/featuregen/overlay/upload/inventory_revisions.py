"""C-B6/C-B7/C-B8 — bound input set, generation inventory, verification inventory.

**Store the whole observation; hash a SUBSET.** This is C-B7's real content. An inventory is a
snapshot of an environment — every table somebody looked at, plus the runtimes. If a feature's
identity covered the whole thing, adding an unrelated table to the cluster would invalidate every
compiled feature, and so would re-capturing the same environment tomorrow. So the record keeps the
complete observation for audit and hashes only: ``environment_id``, the engine versions, the
logical-schema mappings ACTUALLY USED, and the physical layouts for the EXACT read set.

**Captured BEFORE binding, because binding needs it.** ``compile_feature_group`` and ``compile_ir``
both require an inventory, so an observation taken afterwards would describe an environment the
compilation had already assumed something about.

**The bound input set is policy-free** (C-B6). It says which physical datasets and columns a
formula's refs resolved to — a question with an answer before anyone asks which policies apply, and
C-C7's occurrence derivation consumes it rather than the other way round.

**Compatibility is per-DIMENSION and total** (C-B8). Eight runtime versions and every physical
layout, each with its own rule and its own reason. A single "are these the same" boolean cannot say
that a Kedro patch bump is tolerable while a Spark major is not, and the difference is precisely
what an operator needs to know before re-running a verification.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from featuregen.canonical import jcs_sha256
from featuregen.materialize.inventory import ClusterInventoryV1

__all__ = [
    "RUNTIME_DIMENSIONS",
    "BoundInputSetRevisionV2",
    "BoundInputV2",
    "CompatibilityFindingV1",
    "CompatibilityRule",
    "GenerationInventoryObservationV1",
    "VerificationInventoryObservationV1",
    "compare_inventories",
]


# ── C-B6: the bound input set ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class BoundInputV2:
    """One logical ref resolved to a physical dataset and column."""

    logical_ref: str
    physical_dataset: str
    physical_column: str

    def __post_init__(self) -> None:
        for value, what in ((self.logical_ref, "logical_ref"),
                            (self.physical_dataset, "physical_dataset")):
            if not value.strip():
                raise ValueError(
                    f"a bound input with a blank {what} binds nothing: the binding is the whole "
                    f"content of this record")

    def identity_payload(self) -> dict[str, Any]:
        return {"logical_ref": self.logical_ref, "physical_dataset": self.physical_dataset,
                "physical_column": self.physical_column}


@dataclass(frozen=True, slots=True)
class BoundInputSetRevisionV2:
    """Which physical datasets and columns a formula's refs resolved to.

    Deliberately POLICY-FREE (C-B6's gate). "Where does this ref live" has an answer before anyone
    asks which policies apply to it, and C-C7's occurrence derivation consumes this rather than the
    reverse — a bound set that needed policies first would make the two mutually dependent.
    """

    revision_id: str
    environment_id: str
    inputs: tuple[BoundInputV2, ...]

    def __post_init__(self) -> None:
        if not self.revision_id.strip() or not self.environment_id.strip():
            raise ValueError(
                "a bound input set must name itself and the environment it bound against: the same "
                "logical ref resolves to different physical datasets in different environments")
        if not self.inputs:
            raise ValueError("a bound input set with no inputs binds nothing")
        refs = [b.logical_ref for b in self.inputs]
        if len(set(refs)) != len(refs):
            raise ValueError(
                f"a logical ref is bound twice ({sorted(refs)}): one ref resolves to one place, and "
                f"two bindings make which one applies a tuple-order accident")

    @property
    def datasets(self) -> tuple[str, ...]:
        return tuple(sorted({b.physical_dataset for b in self.inputs}))

    def identity_payload(self) -> dict[str, Any]:
        return {
            "environment_id": self.environment_id,
            "inputs": [b.identity_payload()
                       for b in sorted(self.inputs, key=lambda b: b.logical_ref)],
        }

    @property
    def content_hash(self) -> str:
        return jcs_sha256(self.identity_payload())


# ── C-B7: the generation inventory observation ───────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class GenerationInventoryObservationV1:
    """A captured inventory, whose IDENTITY covers only the part a compilation depended on.

    ``observation_id`` and the inventory's ``captured_at`` are provenance and are outside identity —
    that is what makes an identical re-capture identical. ``used_logical_schema_refs`` and
    ``read_set`` are supplied by the caller because only the compilation knows which mappings it
    consulted and which columns it read; deriving them here would mean guessing, and guessing wide
    reintroduces exactly the over-broad identity this type exists to avoid.
    """

    observation_id: str
    inventory: ClusterInventoryV1
    used_logical_schema_refs: tuple[str, ...]
    read_set: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.observation_id.strip():
            raise ValueError("an inventory observation must name itself")
        unknown = sorted(set(self.used_logical_schema_refs)
                         - set(self.inventory.logical_schema_map))
        if unknown:
            raise ValueError(
                f"the compilation used logical-schema mappings {unknown} that this inventory does "
                f"not declare: the identity would cover a mapping nobody captured, and the "
                f"compilation read a table this observation cannot account for")

    @property
    def environment_id(self) -> str:
        return self.inventory.environment_id

    def _used_layouts(self) -> dict[str, Any]:
        """The physical layouts for the EXACT read set — never the whole inventory.

        Read-set refs are ``source::schema.table.column``; the layout is keyed by
        ``schema.table``, so the physical table is the middle of the ref.
        """
        wanted: set[str] = set()
        for ref in self.read_set:
            _, _, object_ref = ref.partition("::")
            parts = object_ref.split(".")
            if len(parts) >= 2:
                wanted.add(f"{parts[0]}.{parts[1]}".lower())
        # `semantic_payload` rather than the whole layout: it already excludes `location` and
        # `rewritten_in_place` for exactly C-B7's reason — moving a warehouse directory does not
        # change what a feature means, and an identity that moved with it would force a recompile
        # for an estate migration that changed nothing semantic.
        return {
            key: layout.semantic_payload()
            for key, layout in sorted(self.inventory.tables.items())
            if key.lower() in wanted
        }

    def identity_payload(self) -> dict[str, Any]:
        """Four things, and nothing else — see the module docstring on why not the whole thing."""
        return {
            "environment_id": self.environment_id,
            "engine_versions": self.inventory.engine_versions.identity_payload(),
            "logical_schema_map": {
                ref: self.inventory.logical_schema_map[ref]
                for ref in sorted(set(self.used_logical_schema_refs))
            },
            "used_layouts": self._used_layouts(),
        }

    @property
    def content_hash(self) -> str:
        return jcs_sha256(self.identity_payload())


# ── C-B8: verification inventory and compatibility ───────────────────────────────────────────────


class CompatibilityRule(StrEnum):
    """How one dimension is compared. Named, because "are these the same" cannot express the
    difference between a Kedro patch bump and a Spark major."""

    #: Any difference is incompatible — the dimension decides execution semantics.
    EXACT = "exact"
    #: Major must match; a higher minor/patch is tolerated.
    MAJOR_COMPATIBLE = "major_compatible"


#: The EIGHT runtime dimensions, each with its rule and the reason for it. Exhaustive over
#: ``EngineVersions`` by test, so a ninth runtime cannot be added without deciding how it compares.
RUNTIME_DIMENSIONS: Mapping[str, tuple[CompatibilityRule, str]] = {
    "spark": (CompatibilityRule.EXACT,
              "Spark decides decimal arithmetic and null ordering; a different Spark is a "
              "different number, not a different build"),
    "hive": (CompatibilityRule.EXACT,
             "the metastore client's view of partitions and types comes from here"),
    "metastore": (CompatibilityRule.EXACT,
                  "schema evolution semantics differ across metastore versions"),
    "java": (CompatibilityRule.MAJOR_COMPATIBLE,
             "a JVM patch does not change arithmetic; a major can change GC and codegen paths"),
    "python": (CompatibilityRule.MAJOR_COMPATIBLE,
               "a patch is a bug fix; a minor can change dict ordering guarantees relied on by "
               "canonicalisation"),
    "pyspark": (CompatibilityRule.EXACT,
                "pyspark is bound to its Spark; drifting it drifts the engine"),
    "kedro": (CompatibilityRule.MAJOR_COMPATIBLE,
              "the pipeline API is stable within a major; a major moves the hooks the project uses"),
    "kedro_datasets": (CompatibilityRule.MAJOR_COMPATIBLE,
                       "dataset classes decide how bytes are written; a major can change the "
                       "writer"),
}


@dataclass(frozen=True, slots=True)
class CompatibilityFindingV1:
    """One dimension that does not match, and why it matters."""

    dimension: str
    rule: CompatibilityRule
    generated_with: str
    verifying_with: str
    reason: str


@dataclass(frozen=True, slots=True)
class VerificationInventoryObservationV1:
    """The inventory a VERIFICATION ran against — compared to the one generation used."""

    observation_id: str
    inventory: ClusterInventoryV1

    def __post_init__(self) -> None:
        if not self.observation_id.strip():
            raise ValueError("a verification inventory observation must name itself")

    @property
    def environment_id(self) -> str:
        return self.inventory.environment_id


def _major(version: str) -> str:
    return version.strip().split(".")[0]


def compare_inventories(
    generation: GenerationInventoryObservationV1,
    verification: VerificationInventoryObservationV1,
    *,
    read_set: Sequence[str] | None = None,
) -> tuple[CompatibilityFindingV1, ...]:
    """Every dimension on which verification's environment differs from generation's.

    Covers all eight runtime versions AND every physical input layout for the read set. Returns
    findings rather than a boolean, because an operator deciding whether to re-run needs to know
    WHICH dimension moved — "incompatible" alone sends them to diff two whole inventories by hand.
    """
    findings: list[CompatibilityFindingV1] = []

    if generation.environment_id != verification.environment_id:
        findings.append(CompatibilityFindingV1(
            dimension="environment_id", rule=CompatibilityRule.EXACT,
            generated_with=generation.environment_id, verifying_with=verification.environment_id,
            reason="a verification in another environment reads other data; it is not a "
                   "verification of this artifact"))

    generated = generation.inventory.engine_versions
    verifying = verification.inventory.engine_versions
    for dimension, (rule, reason) in RUNTIME_DIMENSIONS.items():
        left, right = getattr(generated, dimension), getattr(verifying, dimension)
        same = left == right if rule is CompatibilityRule.EXACT else _major(left) == _major(right)
        if not same:
            findings.append(CompatibilityFindingV1(
                dimension=dimension, rule=rule, generated_with=left, verifying_with=right,
                reason=reason))

    wanted = generation._used_layouts() if read_set is None else GenerationInventoryObservationV1(
        observation_id=generation.observation_id, inventory=generation.inventory,
        used_logical_schema_refs=generation.used_logical_schema_refs,
        read_set=tuple(read_set))._used_layouts()
    for key, layout in sorted(wanted.items()):
        other = verification.inventory.tables.get(key)
        observed = None if other is None else other.semantic_payload()
        if observed != layout:
            findings.append(CompatibilityFindingV1(
                dimension=f"layout:{key}", rule=CompatibilityRule.EXACT,
                generated_with=jcs_sha256(layout),
                verifying_with="ABSENT" if observed is None else jcs_sha256(observed),
                reason="the physical layout this feature reads changed between generation and "
                       "verification; partitioning and types decide what a scan returns"))

    return tuple(findings)
