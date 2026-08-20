"""Spec §7 — identity in TWO phases, because one phase was circular.

Rev 3 of the spec put ``generated_project_hash`` *inside* the identity that the rendered files
embed. That cannot be built: the hash is taken over the rendered bytes, and those bytes contain the
identity that is supposed to contain the hash. The fix is a split, and it is the reason this module
exists:

1. :class:`CompilationIdentity` — **complete before anything is rendered**. Four values, all decided
   by the compilation chain (§2): the features' formula content hashes, their IR hashes, the one
   materialization contract hash and the group plan hash. This is what rendered files embed.
2. :class:`RenderedArtifactIdentity` — **built afterwards**, from bytes that already exist. It is
   the compilation identity plus ``generated_project_hash``.

``generated_project_hash`` is computed over every generated file **except** ``GENERATED.lock``, and
is written only *into* that lock — the detached manifest. That precise exclusion is what makes the
identity non-circular, and it is why :func:`generated_project_hash` answers the same value before
and after the lock exists: L0 (§11.2) recomputes it over a COMPLETE project and compares it with
what the lock states. Every other generated file must be free of it, or the self-reference returns
by another door.

**No run-time value may reach the compilation phase.** A partition value or a business date inside
:class:`CompilationIdentity` would make the generated project change every business date — which is
exactly the defect §3.3 separates the static requirement from the run-time snapshot to prevent. All
of it lives in :func:`sandbox_execution_hash` instead, which covers the compilation identity, the
project hash, the environment, the resolved parameter values, ``business_dt``, the input snapshot
ids, the compiler and renderer versions and the publication capability attestation id (§10.3).

**Sandbox only, structurally.** :func:`derive_namespace` takes no parameters, so there is no
argument on which another namespace could arrive, and it returns ``binding.SANDBOX_NAMESPACE``
rather than re-spelling it, so the two cannot drift. Rev 4 removed a gate that would have unlocked
a production namespace on two non-empty strings; there is no such gate here and no production
publication path exists. Child-2 must later supply a factory that validates actual frozen bindings.

**There is no production execution hash, and the reduced identity is never recorded as
``execution_hash``** — a name without the ``sandbox_`` prefix would read as one half of a pair whose
other half does not exist.

**What raises and what refuses.** Nothing here is a governed verdict about a feature: a malformed
file set, a hand-built identity with mismatched tuples or an execution hash missing its attestation
id are all defects at the call site, the same class as ``canonical.materialize_hash``'s
``TypeError`` on a non-mapping. §14's closed vocabularies have no member for them, so they raise.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from featuregen.materialize import binding, compile, render
from featuregen.materialize.boundary_v2 import CompilationIdentityV2
from featuregen.materialize.canonical import materialize_hash
from featuregen.materialize.group_plan import FeatureGroupPlanV1, group_plan_hash
from featuregen.materialize.identifiers import hive_identifier
from featuregen.materialize.ir import (
    FormulaExecutionIRV1,
    bridge_realization_dependencies,
    crosswalk_execution_pins,
    ir_hash,
)

__all__ = [
    "GENERATED_LOCK_FILENAME",
    "REQUIREMENTS_LOCK_FILENAME",
    "CompilationIdentity",
    "RenderedArtifactIdentity",
    "SealedProject",
    "build_compilation_identity",
    "derive_namespace",
    "generated_project_hash",
    "read_lock",
    "sandbox_execution_hash",
    "seal_project",
]

#: The detached manifest (§7). The ONE file excluded from ``generated_project_hash`` and the ONE
#: file that carries it. Named once, so the renderer, L0 and the rendered assembly node (§10.2)
#: cannot disagree about which file to read.
GENERATED_LOCK_FILENAME = "GENERATED.lock"

#: The artifact's DECLARED runtime environment (§0/§7): the three engine pins
#: :func:`~featuregen.materialize.render.project._render_requirements` takes from
#: ``ClusterInventoryV1.engine_versions``. Unlike the manifest above it IS inside
#: :func:`generated_project_hash`, so what it says is sealed rather than advisory.
#:
#: Named here, beside the manifest, for the identical reason: the renderer WRITES it and L0's build
#: probe READS it back to check the proving interpreter against it (DEFERRED-WORK A.42). A literal
#: on each side would be two spellings of one filename, and a renamed file would silently turn that
#: comparison into "the project declares nothing".
REQUIREMENTS_LOCK_FILENAME = "requirements.lock"


def derive_namespace() -> str:
    """The ONE namespace this slice publishes into (§7) — no parameters, so no other is reachable.

    Returns ``binding.SANDBOX_NAMESPACE`` through the module rather than importing the value, so a
    change there changes this answer. A literal spelled here would be a second source of truth, and
    the second one is always the one nobody updates.
    """
    return binding.SANDBOX_NAMESPACE


# ── phase 1: compilation ─────────────────────────────────────────────────────────────────────────


def _hashes(value: Any, field: str) -> tuple[str, ...]:
    """Coerce a sequence of member hashes to a tuple, refusing the shapes that silently lie."""
    if isinstance(value, str | bytes):
        # A single hash passed unwrapped iterates per CHARACTER, so a one-feature group would be
        # identified as a group of however many characters its hash happens to have.
        raise TypeError(
            f"{field} is a bare {type(value).__name__} ({value!r}): a group's hashes are PLURAL, "
            f"and one hash passed unwrapped would be read one character at a time")
    if not isinstance(value, Sequence):
        raise TypeError(
            f"{field} must be a sequence of hashes, got {type(value).__name__}: an identity built "
            f"from a one-shot iterable could not be rebuilt from the same object")
    items = tuple(value)
    for item in items:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(
                f"{field} carries a blank member ({item!r}): a blank hash compares equal to nothing "
                f"and would let a feature enter the group's identity without being identified")
    return items


def _required(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(
            f"{field} is blank ({value!r}): an identity is what distinguishes one compiled group "
            f"from another, and a blank component distinguishes nothing")
    return value


@dataclass(frozen=True, slots=True)
class CompilationIdentity:
    """What the compilation chain decided — and NOTHING about a render or a run (§7).

    The two tuples are **positionally paired**: ``formula_content_hashes[i]`` and ``ir_hashes[i]``
    describe one feature, in :func:`build_compilation_identity`'s order (the plan's, which is sorted
    by column name). They are therefore neither sorted independently nor de-duplicated here — either
    would break that pairing, and de-duplication would additionally report a two-feature group as a
    one-feature group when two features are authored from one formula.

    There is deliberately no ``generated_project_hash`` field: the rendered files embed THIS object,
    so a project hash on it would be a value the bytes contain and are hashed to produce.
    """

    formula_content_hashes: tuple[str, ...]
    ir_hashes: tuple[str, ...]
    materialization_contract_hash: str
    group_plan_hash: str
    bridge_realization_dependencies: tuple[tuple[str, str], ...] = ()
    #: Every pinned CROSSWALK revision this group would execute, one tuple per two-leg traversal:
    #: ``(execution_revision_id, definition_revision_id, mapping_binding_revision_id,
    #: mapping_temporal_policy_revision_id, composed_observation_revision_id)`` — empty strings
    #: where a crosswalk legitimately pins none. The execution revision is content-addressed over
    #: both leg pins, so the pair of legs is named by the first member and the others are here
    #: because an auditor reading the generated project must be able to look each one up without
    #: re-deriving a hash.
    crosswalk_execution_pins: tuple[tuple[str, str, str, str, str], ...] = ()

    def __post_init__(self) -> None:
        formulas = _hashes(self.formula_content_hashes, "formula_content_hashes")
        irs = _hashes(self.ir_hashes, "ir_hashes")
        if not irs:
            raise ValueError(
                "a compilation identity names no feature: an empty group compiles nothing, and an "
                "identity for it would be the same identity for every empty group")
        if len(formulas) != len(irs):
            raise ValueError(
                f"{len(formulas)} formula hashes and {len(irs)} IR hashes: they are paired one per "
                f"feature, so unequal lengths mean one of the two lists is not the group's members")
        object.__setattr__(self, "formula_content_hashes", formulas)
        object.__setattr__(self, "ir_hashes", irs)
        _required(self.materialization_contract_hash, "materialization_contract_hash")
        _required(self.group_plan_hash, "group_plan_hash")
        dependencies = tuple(sorted(set(self.bridge_realization_dependencies)))
        for revision_id, snapshot_id in dependencies:
            _required(revision_id, "bridge realization revision")
            _required(snapshot_id, "bridge dependency snapshot")
        object.__setattr__(self, "bridge_realization_dependencies", dependencies)
        pins = tuple(sorted({tuple(pin) for pin in self.crosswalk_execution_pins}))
        for pin in pins:
            if len(pin) != 5:
                raise ValueError(
                    f"a crosswalk execution pin names {len(pin)} values and must name 5 "
                    f"(execution, definition, mapping binding, temporal policy, composed "
                    f"observation): {pin!r}")
            _required(pin[0], "crosswalk execution revision")
            _required(pin[1], "crosswalk definition revision")
        object.__setattr__(self, "crosswalk_execution_pins", pins)

    def identity_payload(self) -> dict[str, Any]:
        """Every field, because every field is identity — no provenance, no run-time value.

        Deliberately TOTAL: no parsing, no catalog read, nothing that can raise. It is called while
        building a hash, where an exception would be indistinguishable from a governed refusal.
        """
        payload: dict[str, Any] = {
            "formula_content_hashes": list(self.formula_content_hashes),
            "ir_hashes": list(self.ir_hashes),
            "materialization_contract_hash": self.materialization_contract_hash,
            "group_plan_hash": self.group_plan_hash,
        }
        # Preserve the existing identity bytes for the overwhelmingly common single-catalog case.
        # A cross-catalog artifact adds the exact execution dependencies it must revalidate.
        if self.bridge_realization_dependencies:
            payload["bridge_realization_dependencies"] = [
                list(dependency) for dependency in self.bridge_realization_dependencies
            ]
        # Same rule, same reason: a group with no crosswalk adds no key, so its identity bytes are
        # exactly what they were before crosswalks could be compiled at all.
        if self.crosswalk_execution_pins:
            payload["crosswalk_execution_pins"] = [
                list(pin) for pin in self.crosswalk_execution_pins
            ]
        return payload


def build_compilation_identity(
    irs: Sequence[FormulaExecutionIRV1],
    plan: FeatureGroupPlanV1,
) -> CompilationIdentity:
    """The identity of a compiled group, DERIVED from the IRs and the plan they were planned into.

    Every value is computed here rather than accepted, and the two sides are checked against each
    other: ``ir_hash(ir)`` must equal the ``ir_hash`` the plan carries for that column. **Nothing
    else in the chain makes that comparison** — ``PlannedFeature.ir_hash`` is a value Task 10's
    caller supplies — and §9 later gates every staging manifest against it, so a plan carrying a
    hash no IR produced would send an operator round the regenerate loop chasing a mismatch between
    two things that were never compiled together.

    The order is the PLAN's (sorted by column name), so the identity does not depend on the order
    the features happened to compile in.

    Raises:
        TypeError: ``plan`` is not a :class:`~featuregen.materialize.group_plan.FeatureGroupPlanV1`
            (a bare list of features never showed that its members share one materialization
            contract), or an element of ``irs`` is not a compiled IR.
        ValueError: ``irs`` does not describe exactly the plan's features, two IRs occupy one
            column, or a planned ``ir_hash`` is not the hash of the IR supplied for that column.
    """
    if not isinstance(plan, FeatureGroupPlanV1):
        raise TypeError(
            f"build_compilation_identity requires a FeatureGroupPlanV1, got {type(plan).__name__}: "
            f"the plan is what states the group's ONE materialization contract, and an identity "
            f"built without it would name a group nobody showed to agree")

    by_column: dict[str, FormulaExecutionIRV1] = {}
    for compiled in irs:
        if not isinstance(compiled, FormulaExecutionIRV1):
            raise TypeError(
                f"build_compilation_identity requires compiled IRs, got "
                f"{type(compiled).__name__}: the identity is derived from what was compiled, never "
                f"from a summary of it")
        column = hive_identifier(compiled.feature_name)
        if column in by_column:
            raise ValueError(
                f"two IRs occupy the column {column!r}: one of them would silently overwrite the "
                f"other, and the identity would name a group with a feature missing")
        by_column[column] = compiled

    planned = {feature.column_name: feature for feature in plan.features}
    if set(by_column) != set(planned):
        raise ValueError(
            f"the compiled IRs must describe exactly the plan's features: missing "
            f"{sorted(set(planned) - set(by_column))}, unexpected "
            f"{sorted(set(by_column) - set(planned))}. An IR with no planned column publishes "
            f"nothing, and a planned column with no IR is a column no computation produced")

    mismatched = [
        f"{column} (plan {planned[column].ir_hash!r}, IR {ir_hash(compiled)!r})"
        for column, compiled in sorted(by_column.items())
        if ir_hash(compiled) != planned[column].ir_hash]
    if mismatched:
        raise ValueError(
            f"the plan names an ir_hash no supplied IR produced: {', '.join(mismatched)}. §9 gates "
            f"every staging manifest against the PLAN's value, so a plan and a compilation that "
            f"disagree here would fail at run time against a computation nobody performed")

    ordered = [by_column[feature.column_name] for feature in plan.features]
    return CompilationIdentity(
        formula_content_hashes=tuple(compiled.formula_content_hash for compiled in ordered),
        ir_hashes=tuple(ir_hash(compiled) for compiled in ordered),
        materialization_contract_hash=plan.materialization_contract_hash,
        group_plan_hash=group_plan_hash(plan),
        bridge_realization_dependencies=bridge_realization_dependencies(ordered),
        crosswalk_execution_pins=crosswalk_execution_pins(ordered),
    )


# ── phase 2: the rendered artifact ───────────────────────────────────────────────────────────────


#: The completed first-phase identities this module can seal bytes against. Both carry the same
#: `identity_payload`, which is all §7's phase split needs — the lock states the compilation and the
#: project hash, and neither reads a field the two do not share.
#:
#: ▲ This was `CompilationIdentity` alone, and it is the SAME class of bug §0.8 records one level
#: deeper: `render/` was widened to accept a V2 token and plan, `render_project` duly derived a
#: `CompilationIdentityV2`, and then sealing refused it. Nothing caught it because no test rendered
#: a V2 token END TO END until the item-9 run did.
SealableCompilation = CompilationIdentity | CompilationIdentityV2


@dataclass(frozen=True, slots=True)
class RenderedArtifactIdentity:
    """The compilation identity PLUS the hash of the bytes that were rendered from it (§7).

    Built only after rendering, which is what keeps the two phases apart: ``compilation`` was
    complete and embeddable before a single file existed, and ``generated_project_hash`` could not
    have been known then.
    """

    compilation: SealableCompilation
    generated_project_hash: str

    def __post_init__(self) -> None:
        if not isinstance(self.compilation, CompilationIdentity | CompilationIdentityV2):
            raise TypeError(
                f"a rendered identity is built ON a completed compilation identity, got "
                f"{type(self.compilation).__name__}: the second phase does not replace the first, "
                f"it wraps it")
        _required(self.generated_project_hash, "generated_project_hash")

    def identity_payload(self) -> dict[str, Any]:
        """The lock's content, and the payload the execution hash covers. TOTAL."""
        return {
            "compilation": self.compilation.identity_payload(),
            "generated_project_hash": self.generated_project_hash,
        }


@dataclass(frozen=True, slots=True)
class SealedProject:
    """A rendered project and its identity, sealed together — the files INCLUDE the lock.

    ``files`` is a read-only mapping. The lock states a hash over the other files as they were when
    it was written, so a file set that could still be edited would let the lock become a statement
    about a project that no longer exists.
    """

    identity: RenderedArtifactIdentity
    files: Mapping[str, str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "files", MappingProxyType(dict(self.files)))


def _project_path(path: Any) -> str:
    """One project-relative POSIX path per file — paths are identity, so they must be unambiguous."""
    if not isinstance(path, str) or not path.strip():
        raise ValueError(
            f"a generated file needs a path ({path!r}): the path is part of the project's identity, "
            f"so a blank one would hash a file nobody could find")
    if path.startswith("/") or "\\" in path:
        raise ValueError(
            f"the generated file path {path!r} is not project-relative: an absolute or "
            f"backslash-separated path names a location outside the project being hashed")
    segments = path.split("/")
    if any(segment in ("", ".", "..") for segment in segments):
        raise ValueError(
            f"the generated file path {path!r} has an empty or relative segment: '..' escapes the "
            f"project, and two spellings of one path would hash as two files")
    return path


def generated_project_hash(files: Mapping[str, str]) -> str:
    """The hash of a rendered project — over every file **except** ``GENERATED.lock`` (§7).

    The lock is excluded whether or not it is present, so this answers the SAME value for the files
    about to be sealed and for the complete project L0 later verifies. Including it would make the
    hash unreproducible: the lock is written from the hash.

    Each file contributes ``sha256`` of its UTF-8 bytes under its path, so a changed byte, a renamed
    path, an added file and a deleted file all move the hash.

    Raises:
        TypeError: ``files`` is not a mapping, or a file's content is not text.
        ValueError: a path is not project-relative, or nothing but the lock was supplied — an empty
            project wearing a manifest is not a project.
    """
    if not isinstance(files, Mapping):
        raise TypeError(
            f"generated_project_hash requires a mapping of path to text, got "
            f"{type(files).__name__}")
    digests: dict[str, str] = {}
    for path, text in files.items():
        checked = _project_path(path)
        if not isinstance(text, str):
            raise TypeError(
                f"the generated file {checked!r} holds {type(text).__name__}, not text: this slice "
                f"renders source, configuration and documentation, all of which are text")
        if checked == GENERATED_LOCK_FILENAME:
            continue
        digests[checked] = hashlib.sha256(text.encode("utf-8")).hexdigest()
    if not digests:
        raise ValueError(
            "a rendered project has no files besides the lock: the lock states a hash over the "
            "OTHER files, so there would be nothing for it to be a manifest of")
    return materialize_hash({"files": digests})


def seal_project(compilation: SealableCompilation, files: Mapping[str, str]) -> SealedProject:
    """Hash the rendered files, mint ``GENERATED.lock`` from that hash, and seal the two together.

    This is the ONLY path that writes a lock. A renderer that supplied its own would either state a
    hash it could not yet know, or feed a previous hash back into the bytes being hashed — the
    circularity §7's phase split exists to remove.

    Raises:
        TypeError: ``compilation`` is not a :data:`SealableCompilation`, or a file is not text.
        ValueError: ``files`` already contains a lock, or a path is not project-relative.
    """
    if not isinstance(compilation, CompilationIdentity | CompilationIdentityV2):
        raise TypeError(
            f"seal_project requires a completed compilation identity, got "
            f"{type(compilation).__name__}: the first phase must be COMPLETE before the bytes that "
            f"embed it are hashed")
    if GENERATED_LOCK_FILENAME in files:
        raise ValueError(
            f"the rendered files already contain {GENERATED_LOCK_FILENAME}: the lock is minted from "
            f"the project hash, so a renderer-supplied one either states a hash it could not know "
            f"or feeds an older one back into the bytes being hashed")
    rendered = RenderedArtifactIdentity(compilation=compilation,
                                        generated_project_hash=generated_project_hash(files))
    sealed = dict(files)
    sealed[GENERATED_LOCK_FILENAME] = _lock_document(rendered)
    return SealedProject(identity=rendered, files=sealed)


def _lock_document(rendered: RenderedArtifactIdentity) -> str:
    """``GENERATED.lock``'s bytes: plain sorted JSON, so the rendered pipeline can read it too.

    §10.2's assembly node reads ``generated_project_hash`` from this file AT RUN TIME rather than
    carrying it as a literal, which is what keeps every other generated file free of it.
    """
    return json.dumps(rendered.identity_payload(), indent=2, sort_keys=True) + "\n"


def read_lock(document: str) -> RenderedArtifactIdentity:
    """Parse ``GENERATED.lock`` back into the identity it records (§11.2's L0 comparison).

    Read here rather than wherever a reader happens to need it: a second parser would be a second
    chance to disagree about which key holds the project hash.

    Raises:
        ValueError: the document is not JSON, or does not carry exactly a rendered identity.
    """
    if not isinstance(document, str):
        raise TypeError(f"a lock document is text, got {type(document).__name__}")
    try:
        parsed = json.loads(document)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{GENERATED_LOCK_FILENAME} is not JSON ({exc})") from exc
    if not isinstance(parsed, dict) or set(parsed) != {"compilation", "generated_project_hash"}:
        raise ValueError(
            f"{GENERATED_LOCK_FILENAME} must hold exactly a rendered identity, got keys "
            f"{sorted(parsed) if isinstance(parsed, dict) else type(parsed).__name__}")
    compilation = parsed["compilation"]
    required = {"formula_content_hashes", "ir_hashes", "materialization_contract_hash",
                "group_plan_hash"}
    optional = {"bridge_realization_dependencies", "crosswalk_execution_pins"}
    if (
        not isinstance(compilation, dict)
        or not required <= set(compilation)
        or set(compilation) - required - optional
    ):
        raise ValueError(
            f"{GENERATED_LOCK_FILENAME} must hold exactly a compilation identity, got keys "
            f"{sorted(compilation) if isinstance(compilation, dict) else type(compilation).__name__}")
    if "bridge_realization_dependencies" in compilation:
        compilation["bridge_realization_dependencies"] = tuple(
            tuple(dependency)
            for dependency in compilation["bridge_realization_dependencies"]
        )
    if "crosswalk_execution_pins" in compilation:
        compilation["crosswalk_execution_pins"] = tuple(
            tuple(pin) for pin in compilation["crosswalk_execution_pins"]
        )
    return RenderedArtifactIdentity(
        compilation=CompilationIdentity(**compilation),
        generated_project_hash=parsed["generated_project_hash"])


# ── the run: the ONLY place a run-time value appears ─────────────────────────────────────────────


def sandbox_execution_hash(
    rendered: RenderedArtifactIdentity,
    *,
    environment_id: str,
    parameters: Mapping[str, Any],
    business_dt: str,
    input_snapshot_ids: Sequence[str],
    capability_attestation_id: str,
) -> str:
    """The identity of ONE execution (§7) — everything the compilation identity may not contain.

    It takes the RENDERED identity, not a bare compilation one: §7 says the hash covers both the
    compilation identity and the project hash, and passing them separately would let a caller pair
    one compilation with another project's bytes.

    Nothing is defaulted. A defaulted ``capability_attestation_id`` would let an execution be
    identified without naming the attestation §10.3 requires before anything may be published.

    ``input_snapshot_ids`` keeps the caller's ORDER: §3.4 calls it *the exact ordered partition set
    read*, and sorting would declare two different read orders equivalent — a judgement run
    preparation makes, not this module. ``parameters``' key order is irrelevant, because RFC 8785
    sorts object keys.

    **Both toolchain versions are READ, not accepted** (Tasks 12 and 15). §7 says this hash covers
    them, and the one thing they exist to detect is a compiler or renderer that changed — so a call
    site free to pass a literal could silently freeze one at whatever it typed, and the hash would
    keep answering the same value across a toolchain that had changed underneath it. There is
    therefore neither a ``renderer_version`` nor a ``compiler_version`` parameter: the values are
    ``render.RENDERER_VERSION`` and ``compile.COMPILER_VERSION``, read *through* their modules so a
    test can move a constant and see this answer move with it. Both live in package ``__init__``
    files that import nothing, because the modules that will use them (``render.project``, §2's
    chain) import this one.

    There is no production counterpart, and this value is never recorded as ``execution_hash``.

    Raises:
        TypeError: ``rendered`` is not a :class:`RenderedArtifactIdentity`, ``parameters`` is not a
            mapping, or ``input_snapshot_ids`` is a bare string (which would be read one character
            at a time) or not a sequence.
        ValueError: a required value is blank, or no input snapshot was named.
        featuregen.formula._jcs.CanonicalizationError: a parameter value is not JSON-canonicalizable.
    """
    if not isinstance(rendered, RenderedArtifactIdentity):
        raise TypeError(
            f"sandbox_execution_hash requires a RenderedArtifactIdentity, got "
            f"{type(rendered).__name__}: an execution hash without the project hash could not "
            f"distinguish two runs of two different builds of the same compilation")
    if not isinstance(parameters, Mapping):
        raise TypeError(
            f"parameters must be the mapping run preparation resolved, got "
            f"{type(parameters).__name__}: §11.1 requires execution to read exactly those values")
    if isinstance(input_snapshot_ids, str | bytes):
        raise TypeError(
            f"input_snapshot_ids is a bare {type(input_snapshot_ids).__name__} "
            f"({input_snapshot_ids!r}): a single id passed unwrapped would be read one character at "
            f"a time")
    if not isinstance(input_snapshot_ids, Sequence):
        raise TypeError(
            f"input_snapshot_ids must be a sequence, got {type(input_snapshot_ids).__name__}")
    snapshots = tuple(input_snapshot_ids)
    if not snapshots:
        raise ValueError(
            "the run names no input snapshot: every run reads at least the entity population, so "
            "an empty set is a resolution that produced nothing — and it would hash identically to "
            "a run whose inputs nobody recorded")
    for snapshot in snapshots:
        if not isinstance(snapshot, str) or not snapshot.strip():
            raise ValueError(
                f"input_snapshot_ids carries a blank id ({snapshot!r}): an unnamed input is an "
                f"input the execution hash cannot distinguish from one that was never read")

    return materialize_hash({
        "rendered_artifact": rendered.identity_payload(),
        "environment_id": _required(environment_id, "environment_id"),
        "parameters": dict(parameters),
        "business_dt": _required(business_dt, "business_dt"),
        "input_snapshot_ids": list(snapshots),
        "compiler_version": _required(compile.COMPILER_VERSION, "compiler_version"),
        "renderer_version": _required(render.RENDERER_VERSION, "renderer_version"),
        "capability_attestation_id": _required(capability_attestation_id,
                                               "capability_attestation_id"),
    })
