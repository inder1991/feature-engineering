"""Step 2's assembly — the stages `compile_generation_v2` stops before, run in order.

`compile_generation_v2` closes admitted formula → operator graph and returns. Everything after it
existed and nothing called it: `evaluate_generate`, `record_group_plan`, `record_bound_formula`,
`render_project` and `seal_v2` each had unit tests and zero production callers. This is the function
that runs them, and it is deliberately the only place that knows their ORDER.

The order, and why each step is where it is::

    evaluate_generate     the GATE. Before anything is recorded, because a refused generation
                          must leave no group plan, no bound formula and no artifact behind.
    record_group_plan     what the group publishes, persisted before the code that computes it
                          so a rendered project can be checked against a stored plan.
    record_bound_formula  each member's executable output policy, keyed to the compilation.
    render_project        the code, sealed under an identity DERIVED from the token and plan.
    seal_v2               the artifact, its per-member verdict and its realization links.

**EVERY MEMBER IS GATED, and the fold is all-or-nothing** — the same rule `seal_v2` uses, for the
same reason: a group is published as one row per key, so admitting some members and refusing others
would publish columns that were never cleared.

**Nothing is recorded before the gate passes.** The stages after it write durable rows, and a
refused generation that left a group plan behind would leave the next reader unable to tell a plan
that was cleared from one that was stopped.

**Cluster facts are arguments, not discoveries.** `EngineVersions` and the resolved spine
requirement describe an environment; deriving them here would make a formula-shaped failure report
a cluster-shaped reason, and make this function untestable without one.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from featuregen.contracts.db import DbConn
from featuregen.materialize.artifact_manifest import ArtifactManifestV1
from featuregen.materialize.canonical import materialize_hash
from featuregen.materialize.evaluate_generate import evaluate_generate
from featuregen.materialize.group_plan_v2 import record_group_plan
from featuregen.materialize.inputs import PhysicalInputRequirement
from featuregen.materialize.pilot_v2 import CompiledGenerationV2
from featuregen.materialize.render.project import EngineVersions, RenderedNode, render_project
from featuregen.materialize.seal_v2 import RealizationLinkV1, SealedArtifactV2, seal_v2
from featuregen.overlay.upload.evaluator_contracts import EvaluatorVerdictV1

__all__ = ["GeneratedArtifactV2", "GenerationRefused", "generate_v2"]


class GenerationRefused(Exception):
    """The Generate gate refused, and NOTHING was recorded.

    An exception rather than a returned verdict, unlike the compilation refusals: those collect —
    a group produces many and an operator reads them together — while this one is terminal for the
    whole generation and there is nothing further to collect. Carrying the verdict lets a caller
    report every blocker rather than the fact that there were some.
    """

    def __init__(self, verdict: EvaluatorVerdictV1, member: str) -> None:
        super().__init__(
            f"generation refused for {member}: {', '.join(verdict.blockers)}")
        self.verdict = verdict
        self.member = member


@dataclass(frozen=True, slots=True)
class GeneratedArtifactV2:
    """What a generation produced: the sealed artifact and the plan hash it was sealed against."""

    sealed: SealedArtifactV2
    group_plan_hash: str


def generate_v2(
    conn: DbConn,
    compiled: CompiledGenerationV2,
    *,
    environment_id: str,
    generation_authorization_revision_id: str,
    engine_id: str,
    engine_versions: EngineVersions,
    spine_input: PhysicalInputRequirement,
    nodes: Sequence[RenderedNode],
    manifest: ArtifactManifestV1,
    files: Mapping[str, str],
    occurrences_by_member: Mapping[str, object],
    realizations: Sequence[RealizationLinkV1],
    compiled_at: str,
    sealed_at: str,
    activation_blockers: Sequence[str] = (),
) -> GeneratedArtifactV2:
    """Gate, record, render and seal one compiled generation.

    Args:
        occurrences_by_member: each member's resolved policy occurrences, by feature name. Required
            per member and not defaulted: `evaluate_generate` refuses a generation whose policy
            references cannot be resolved, and a member with no entry would be gated against an
            empty set — which passes.

    Returns:
        :class:`GeneratedArtifactV2`.

    Raises:
        GenerationRefused: the gate refused for at least one member. Nothing was written.
        ValueError: a member has no occurrences entry, or the arguments disagree with the
            compilation.
    """
    members = sorted(compiled.graphs)
    missing = [name for name in members if name not in occurrences_by_member]
    if missing:
        raise ValueError(
            f"no policy occurrences supplied for {missing}: a member gated against an empty set is "
            f"a member whose policy references were never checked, and that check passes")

    # ── 1. THE GATE, EVERY MEMBER, BEFORE ANYTHING IS RECORDED ──────────────────────────────────
    for name in members:
        verdict = evaluate_generate(
            conn,
            generation_authorization_revision_id=generation_authorization_revision_id,
            activation_blockers=activation_blockers,
            occurrences=occurrences_by_member[name],
            graph=compiled.graphs[name],
            engine_id=engine_id)
        if not verdict.allowed:
            raise GenerationRefused(verdict, name)

    # ── 2. WHAT THE GROUP PUBLISHES ─────────────────────────────────────────────────────────────
    group_plan_hash = record_group_plan(conn, compiled.plan, environment_id=environment_id)

    # ── 3. THE CODE, under an identity derived from the token and the plan ──────────────────────
    project = render_project(
        compiled.authorized.token, compiled.plan,
        environment_id=environment_id, engine_versions=engine_versions,
        spine_input=spine_input, nodes=nodes)

    # ── 4. THE ARTIFACT — every member's graph, folded into one verdict ─────────────────────────
    sealed = seal_v2(
        conn, [compiled.graphs[name] for name in members], manifest, files,
        environment_id=environment_id,
        logical_group_name=compiled.plan.logical_group_name,
        # BOTH READ OFF THE SEALED PROJECT, never recomputed here. `render_project` DERIVES the
        # compilation identity rather than accepting one, precisely so the bytes cannot embed an
        # identity belonging to a different compilation — re-deriving it in this function would
        # reintroduce the second opinion that design removes.
        compilation_identity_hash=materialize_hash(
            project.identity.compilation.identity_payload()),
        group_plan_hash=group_plan_hash,
        # PREFIXED here, because `sealed_artifact_v2.project_digest` carries a CHECK requiring it
        # and `generated_project_hash` returns a bare digest. Adding the prefix at the boundary
        # rather than changing the hasher: the bare value is what the GENERATED.lock states, and
        # two spellings of one digest is how a lock and a row come to disagree.
        project_digest=f"sha256:{project.identity.generated_project_hash}",
        realizations=realizations,
        sealed_at=sealed_at,
        generation_authorization_revision_id=generation_authorization_revision_id)

    return GeneratedArtifactV2(sealed=sealed, group_plan_hash=group_plan_hash)
