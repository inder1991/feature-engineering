"""The seven-stage identity chain (plan §Identity chain) — every digest sha256 hex, every
serialization canonical (RFC 8785 via :func:`featuregen.materialize.canonical.materialize_hash`),
every payload carrying an explicit versioned contract tag.

::

    card                logical_digest
    formula binding     formula_binding_digest        = H(logical_digest | formula_content_hash
                                                          | formula_method_identity)
    physical adoption   member_execution_input_digest = H(formula_binding_digest | physical_digest
                                                          | member_output_contract)
    compilation         member_compile_digest         = H(member_execution_input_digest | ir_hash
                                                          | policy_occurrence_bindings)
    build               build_compilation_digest      = H(target_and_spine_revision
                                                          | ORDERED member_compile_digests
                                                          | generation_configuration_digest)
    rendered artifact   project_digest                = H(actual rendered files)   [generate_v2's]
    sealed artifact     sealed_artifact_identity      = H(build_compilation_digest
                                                          | render_profile_digest | project_digest)

``project_digest`` is minted by ``materialize/generate_v2.py`` over the actually-rendered files
and is consumed here, never recomputed. Digests THIS chain mints are validated as strict 64-hex;
externally minted identities (formula content hash, method identity, IR hash, target/spine
revision, project digest) are validated non-empty — their format is their owner's contract.

Member ORDER inside a build is identity (the two-member order-reversal pin): a build is an
ordered binding of members, not a set.
"""
from __future__ import annotations

from featuregen.materialize.canonical import materialize_hash
from featuregen.overlay.upload.planner.logical_plan_v2 import (
    ContractDefect,
    _non_empty,
    logical_digest,
)
from featuregen.overlay.upload.planner.physical_plan_v1 import _hex_digest, physical_digest
from featuregen.overlay.upload.planner.render_profile import (
    MemberOutputContractV1,
    generation_configuration_digest,
    render_digest,
)

__all__ = [
    "build_compilation_digest",
    "formula_binding_digest",
    "generation_configuration_digest",
    "logical_digest",
    "member_compile_digest",
    "member_execution_input_digest",
    "physical_digest",
    "render_digest",
    "sealed_artifact_identity",
]


def formula_binding_digest(
    logical_digest: str,
    formula_content_hash: str,
    formula_method_identity: str,
) -> str:
    """Stage 2: the card's meaning bound to ONE authored formula and the method that authored it."""
    return materialize_hash({
        "contract": "formula_binding_digest_v1",
        "logical_digest": _hex_digest(logical_digest, what="logical_digest"),
        "formula_content_hash": _non_empty(formula_content_hash, what="formula_content_hash"),
        "formula_method_identity": _non_empty(
            formula_method_identity, what="formula_method_identity"),
    })


def member_execution_input_digest(
    formula_binding_digest: str,
    physical_digest: str,
    member_output_contract: MemberOutputContractV1,
) -> str:
    """Stage 3: the formula binding joined to its adopted physical plan and output contract."""
    if not isinstance(member_output_contract, MemberOutputContractV1):
        raise ContractDefect(
            "member_execution_input_digest takes a MemberOutputContractV1 — the output surface "
            "is part of what will execute, never a rendering afterthought")
    return materialize_hash({
        "contract": "member_execution_input_digest_v1",
        "formula_binding_digest": _hex_digest(
            formula_binding_digest, what="formula_binding_digest"),
        "physical_digest": _hex_digest(physical_digest, what="physical_digest"),
        "member_output_contract": member_output_contract.content_payload(),
    })


def member_compile_digest(
    member_execution_input_digest: str,
    ir_hash: str,
    policy_occurrence_bindings: tuple[tuple[str, str], ...],
) -> str:
    """Stage 4: one member compiled — the execution input, the IR it compiled to, and the exact
    policy realization bound at each occurrence. Occurrence ORDER is the IR's occurrence order
    and is preserved (the same policies bound at different occurrences are different compiles)."""
    bindings = [
        [_non_empty(occurrence, what="policy occurrence ref"),
         _non_empty(realization, what=f"policy realization for occurrence {occurrence!r}")]
        for occurrence, realization in policy_occurrence_bindings]
    return materialize_hash({
        "contract": "member_compile_digest_v1",
        "member_execution_input_digest": _hex_digest(
            member_execution_input_digest, what="member_execution_input_digest"),
        "ir_hash": _non_empty(ir_hash, what="ir_hash"),
        "policy_occurrence_bindings": bindings,
    })


def build_compilation_digest(
    target_and_spine_revision: str,
    ordered_member_compile_digests: tuple[str, ...],
    generation_configuration_digest: str,
) -> str:
    """Stage 5: the build — target/spine revision, the ORDERED member compiles, the generation
    configuration. Member order is identity; an empty build is a construction error."""
    members = tuple(ordered_member_compile_digests)
    if not members:
        raise ContractDefect("a build with zero members has no compilation identity")
    return materialize_hash({
        "contract": "build_compilation_digest_v1",
        "target_and_spine_revision": _non_empty(
            target_and_spine_revision, what="target_and_spine_revision"),
        "ordered_member_compile_digests": [
            _hex_digest(digest, what="member_compile_digest") for digest in members],  # ORDERED
        "generation_configuration_digest": _hex_digest(
            generation_configuration_digest, what="generation_configuration_digest"),
    })


def sealed_artifact_identity(
    build_compilation_digest: str,
    render_profile_digest: str,
    project_digest: str,
) -> str:
    """Stage 7: the sealed artifact — WHAT was compiled, HOW it was rendered, and the bytes that
    actually came out (``project_digest``, generate_v2's hash of the rendered files)."""
    return materialize_hash({
        "contract": "sealed_artifact_identity_v1",
        "build_compilation_digest": _hex_digest(
            build_compilation_digest, what="build_compilation_digest"),
        "render_profile_digest": _hex_digest(
            render_profile_digest, what="render_profile_digest"),
        "project_digest": _non_empty(project_digest, what="project_digest"),
    })
