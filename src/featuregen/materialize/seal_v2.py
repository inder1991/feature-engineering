"""S7 — sealing a V2 artifact: the graph check, the realization links, and serving verified bytes.

**A refusal keeps its evidence.** S7's acceptance is that deleting the FX duplicate-rate gate refuses
*with ``realizes_occurrences`` intact* — and "intact" is the load-bearing half. A graph that fails the
FX subgraph check still depended on exactly the governed policies it depended on, and dropping the
links on refusal destroys that record at the moment it becomes most interesting: an auditor asking
"which policies were applied here" gets an answer for every artifact that succeeded and silence for
every one that did not. :func:`seal_v2` therefore records the artifact, the verdict AND the links on
both paths, and refuses only to mark it servable.

**An untriggered requirement is not a pass.** C-C10's rule, carried into what is stored: a
fixed-base-currency feature contains no as-of FX join, so the FX requirement never applies, and
recording that as "FX subgraph sound" would claim an inspection nobody ran.
``triggered_requirements`` is stored beside the boolean because a boolean cannot tell those apart.

**Bytes are verified at three points, and this module owns two of them.**
``artifact_manifest.verify_bytes`` names the point — ``write``, ``retrieval``, ``execution`` — because
the failures mean different things: at write the renderer disagrees with itself, at retrieval the
store returned something else, at execution the bytes changed after they were fetched. Serving goes
through :func:`serve_artifact`, which re-derives every digest, so a mismatched digest is neither
served nor executed rather than being caught by whoever remembered to check.

**HOW EACH MEMBER WAS AUTHORED IS DECIDED HERE, because it disappears everywhere else.** The sealed
row records formula and IR hashes, the plan, the environment and the authorization — and nothing
about the authoring RUN or METHOD, so a production gate choosing between a platform gold evaluation
and a deterministic compiler certification would have no basis to choose from. :func:`seal_v2`
therefore takes one :class:`~featuregen.materialize.authoring_provenance.MemberAuthoringInputV1` per
published column and DERIVES each method from that run's own evidence (1099). It is not
reconstructible afterwards: "the latest draft for this selection" is a different question, and by
then another draft may describe a formula this artifact does not contain.

**Surviving a worker restart is a property of the store, not of a process.** A restarted worker holds
an artifact id and a database and nothing else, so :func:`load_sealed_artifact` and
:func:`load_manifest` exist: the sealed record, its verdict, its realization links and the manifest
all come back out of the store, and the bytes follow from the content-addressed blobs (C-D4). Nothing
is memoized in a module global — a cache is what would make "survives a restart" accidentally true in
one process and false in the next. The verdict is REBUILT from stored fields rather than re-derived
from the graph, which is not persisted and would answer with today's requirement set rather than the
one the artifact was sealed under.
"""
from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from featuregen.contracts.db import DbConn
from featuregen.materialize.artifact_manifest import (
    ArtifactFileEntryV1,
    ArtifactManifestV1,
    ManifestIntegrityError,
    verify_bytes,
)
from featuregen.materialize.artifact_store import fetch_file, store_manifest
from featuregen.materialize.method_identity import (
    MethodIdentityUndecidable,
    derive_method_identity,
    record_method_identity,
)
from featuregen.materialize.authoring_provenance import (
    MemberAuthoringInputV1,
    MemberProvenanceRefused,
    derive_member_provenance,
    record_member_provenance,
)
from featuregen.materialize.operator_graph_v2 import OperatorGraphV2, OperatorKindV2
from featuregen.materialize.subgraph_requirements_v2 import (
    PILOT_REQUIREMENTS,
    RequirementFindingV2,
    RequirementVerdictV2,
    SubgraphRequirementV2,
    check_subgraph_requirements_v2,
)

__all__ = [
    "SealedArtifactConflict",
    "ArtifactNotServable",
    "MemberAuthoringInputV1",
    "MemberProvenanceRefused",
    "MethodIdentityUndecidable",
    "RealizationLinkV1",
    "SealedArtifactV2",
    "load_manifest",
    "load_sealed_artifact",
    "realization_links_of",
    "seal_v2",
    "serve_artifact",
]


class ArtifactNotServable(Exception):
    """The artifact exists and is recorded, but the graph check refused it.

    A distinct exception from :class:`ManifestIntegrityError` because the remedies are distinct: a
    refused graph is a compilation to fix, a mismatched digest is a store to investigate.
    """


@dataclass(frozen=True, slots=True)
class RealizationLinkV1:
    """One governed policy realization this artifact depended on, and the occurrence it answered."""

    revision_id: str
    occurrence_hash: str


@dataclass(frozen=True, slots=True)
class SealedArtifactV2:
    """What was sealed, under which verdict, depending on which policy realizations."""

    artifact_id: str
    environment_id: str
    logical_group_name: str
    #: WHICH approval produced this. On the object rather than a second query away, so a caller
    #: asking "was this authorized" cannot get an answer by forgetting to ask.
    generation_authorization_revision_id: str
    compilation_identity_hash: str
    group_plan_hash: str
    project_digest: str
    verdict: RequirementVerdictV2
    realizations: tuple[RealizationLinkV1, ...]

    @property
    def servable(self) -> bool:
        """Whether this artifact may be served — the verdict, never a separate flag.

        A separate flag could disagree with the verdict, and the disagreement would be resolved by
        whichever field the caller happened to read.
        """
        return self.verdict.satisfied


def seal_v2(
    conn: DbConn,
    graphs: OperatorGraphV2 | Sequence[OperatorGraphV2],
    manifest: ArtifactManifestV1,
    files: Mapping[str, str],
    *,
    environment_id: str,
    logical_group_name: str,
    compilation_identity_hash: str,
    group_plan_hash: str,
    project_digest: str,
    realizations: Sequence[RealizationLinkV1],
    member_provenance: Sequence[MemberAuthoringInputV1],
    sealed_at: str,
    generation_authorization_revision_id: str,
    requirements: tuple[SubgraphRequirementV2, ...] = PILOT_REQUIREMENTS,
) -> SealedArtifactV2:
    """Check EVERY member's operator graph, persist the artifact and its evidence, and return it.

    **A group is one artifact and many graphs.** ``compile_generation_v2`` produces one graph per
    feature — each ending in its own one-column ``GROUP_ASSEMBLY`` — while the sealed artifact is the
    group's rendered project: one manifest, one set of files, one publication target. This function
    took a single graph, so a multi-feature group could only be sealed by checking one member and
    recording that verdict as though it covered all of them.

    **The member name is READ OFF THE GRAPH, never supplied.** Each graph's terminal assembly names
    the column it publishes, so a caller cannot mislabel which member a finding belongs to, and a
    graph that publishes zero or several columns is refused rather than attributed to a guess.

    **ALL-OR-NOTHING, the same rule the rest of the chain uses.** The artifact is servable only if
    every member satisfies its requirements; the findings are the union, each naming its own member.
    A group is published as one row per key, so a partially-satisfied group would publish some
    columns computed under requirements nobody checked.

    The graph check runs FIRST but does not short-circuit the record: a refused artifact is stored
    with its findings and its realization links, and only its ``servable`` answer differs. Discarding
    a refusal would leave an operator with a compilation that failed and no record of what it
    depended on.

    Files are stored through :func:`~featuregen.materialize.artifact_store.store_manifest`, which
    verifies every entry against its bytes BEFORE writing — the one point where a renderer
    disagreeing with itself is still recoverable.

    **▲ ONE AUTHORING-PROVENANCE ROW PER PUBLISHED COLUMN, AND THE METHOD IS DERIVED HERE.**
    ``member_provenance`` is REQUIRED and must name exactly the columns the graphs publish. It is
    not defaulted and there is no empty case: an artifact sealed without it publishes numbers whose
    authoring method nobody can name, and the production gate reads a missing row as "nothing to
    check" — a pass by omission. What the caller supplies are FACTS it holds (which selection, which
    draft, which run, which formula bytes); the METHOD is derived from the run's own evidence by
    :func:`~featuregen.materialize.authoring_provenance.derive_member_provenance`, so a caller has
    no way to assert one. The derivation runs BEFORE the manifest is stored, so a member whose
    method cannot be established leaves no bytes, no artifact row and no provenance row behind.

    Args:
        member_provenance: one record per published column — the selection a person made, the draft
            and run that answered it, and the formula hash this artifact carries. See above.

    Raises:
        ManifestIntegrityError: a manifest entry and its bytes disagree, or a file the manifest
            names was not supplied.
        MemberProvenanceRefused: a member's authoring method cannot be established from its run's
            evidence, or a supplied record is blank where it must not be. Nothing is written.
        ValueError: no environment, a realization link with a blank half, no graphs at all, a graph
            whose terminal does not publish exactly one column, two graphs publishing the same one,
            or a ``member_provenance`` that does not describe exactly the published columns. All are
            calls assembled wrongly rather than governed verdicts.
    """
    if not environment_id.strip():
        raise ValueError(
            "a sealed artifact must name the environment it was sealed for: environment is "
            "deployment placement, and an artifact that did not say which one could be served "
            "into a cluster it was never rendered against")
    links = tuple(realizations)
    for link in links:
        if not link.revision_id.strip() or not link.occurrence_hash.strip():
            raise ValueError(
                f"realization link {link} has a blank half: a dependency that cannot name both the "
                f"realization and the occurrence it answered is one nobody can follow back")

    members = _members_of(graphs)

    # ▲ PROVENANCE IS DECIDED BEFORE ANYTHING IS WRITTEN, for the reason the manifest check is:
    # this is the last point at which a member whose authoring method cannot be established can be
    # refused without leaving an artifact behind. `derive_member_provenance` only reads.
    _require_provenance_covers(members, member_provenance)
    derived = derive_member_provenance(conn, member_provenance)

    # ▲ THE EXACT METHOD IDENTITY, DERIVED HERE FOR THE SAME REASON, and refused here for the same
    # reason. `derive_member_provenance` establishes WHICH of two methods; a production certificate
    # covers a model, a pair of contracts, a grammar and a schema — so a member whose exact identity
    # cannot be named from its run's evidence can never be certificate-matched, and sealing it would
    # manufacture an artifact production must permanently refuse. Both derivations read only, and
    # both happen before a single byte is written.
    identities = tuple(
        (item.member.member_name,
         derive_method_identity(conn, authoring_run_id=item.member.authoring_run_id,
                                authoring_method=item.provenance.authoring_method))
        for item in derived)

    verdict, by_member = _fold_requirements(members, requirements)

    # Bytes first: a manifest that does not match its files must not leave a sealed row behind.
    store_manifest(conn, manifest, files)

    conn.execute(
        "INSERT INTO sealed_artifact_v2 (artifact_id, environment_id, logical_group_name, "
        "compilation_identity_hash, group_plan_hash, project_digest, subgraph_satisfied, "
        "triggered_requirements, subgraph_findings, sealed_at, "
        "generation_authorization_revision_id) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s) "
        "ON CONFLICT (artifact_id) DO NOTHING",
        (manifest.artifact_id, environment_id, logical_group_name, compilation_identity_hash,
         group_plan_hash, project_digest, verdict.satisfied,
         json.dumps(list(verdict.triggered)),
         json.dumps([{"requirement": f.requirement, "code": f.code, "detail": f.detail,
                      # WHICH member. A finding that names only the requirement sends an operator
                      # to read every graph in the group to discover which one failed.
                      "feature_name": name}
                     for name, f in by_member]),
         sealed_at,
         # WHICH APPROVAL PRODUCED THIS. Inside a composite FK with the environment and the group,
         # so an artifact sealed under an authorization issued for another group is unwritable
         # rather than merely wrong.
         generation_authorization_revision_id))
    _require_identity_unchanged(
        conn, manifest.artifact_id,
        environment_id=environment_id, logical_group_name=logical_group_name,
        compilation_identity_hash=compilation_identity_hash, group_plan_hash=group_plan_hash,
        project_digest=project_digest,
        generation_authorization_revision_id=generation_authorization_revision_id)

    # HOW EACH MEMBER WAS AUTHORED — written on BOTH paths, for the realization links' reason. A
    # refused artifact was still authored somehow, and an operator asking "where did this formula
    # come from" about a build that failed is asking the same question as about one that passed.
    record_member_provenance(conn, manifest.artifact_id, derived)

    # ▲ SAME TRANSACTION AS THE PROVENANCE ABOVE, all members or none. An artifact whose members
    # have a method but no method IDENTITY is one production can never evaluate — and it would look
    # complete, because 1099's rows are all there.
    for member_name, identity in identities:
        record_method_identity(conn, manifest.artifact_id, member_name, identity)

    # RECORDED ON BOTH PATHS. See the module docstring: the acceptance clause is that a refusal
    # keeps these, and a refused compilation depended on exactly the policies it depended on.
    for link in links:
        conn.execute(
            "INSERT INTO sealed_artifact_realization (artifact_id, revision_id, occurrence_hash) "
            "VALUES (%s, %s, %s) ON CONFLICT (artifact_id, revision_id, occurrence_hash) "
            "DO NOTHING",
            (manifest.artifact_id, link.revision_id, link.occurrence_hash))

    return SealedArtifactV2(
        artifact_id=manifest.artifact_id, environment_id=environment_id,
        logical_group_name=logical_group_name,
        generation_authorization_revision_id=generation_authorization_revision_id,
        compilation_identity_hash=compilation_identity_hash, group_plan_hash=group_plan_hash,
        project_digest=project_digest, verdict=verdict, realizations=links)


class SealedArtifactConflict(RuntimeError):
    """An artifact id was re-sealed with DIFFERENT identity-bearing content.

    Distinct from a manifest integrity error: the bytes are fine, and it is the artifact's own
    identity that two callers disagree about.
    """


#: The fields that make a sealed artifact THAT artifact. A retry may differ in none of them.
#: `sealed_at` is deliberately absent — the same artifact re-sealed a minute later is the same
#: artifact, and refusing on the clock would turn every legitimate retry into an error.
_IDENTITY_FIELDS = (
    "environment_id", "logical_group_name", "compilation_identity_hash",
    "group_plan_hash", "project_digest", "generation_authorization_revision_id",
)


def _require_identity_unchanged(conn: DbConn, artifact_id: str, **supplied: str) -> None:
    """After an idempotent insert, prove the STORED row is the one the caller described.

    ``ON CONFLICT (artifact_id) DO NOTHING`` makes re-sealing safe, and silently makes MIS-sealing
    safe too: an id retried with a different authorization, environment, group or project digest is
    ignored, and the function then returns an object built from the CALLER's values — describing a
    row that does not exist. Every later reader trusts the database; the caller trusted its own
    arguments; nothing reconciles them.

    So the insert is followed by a read-back. Identity-bearing fields must match exactly, and the
    first that does not is named on both sides.
    """
    row = conn.execute(
        f"SELECT {', '.join(_IDENTITY_FIELDS)} FROM sealed_artifact_v2 WHERE artifact_id = %s",
        (artifact_id,)).fetchone()
    if row is None:                       # pragma: no cover — the insert above guarantees it
        raise SealedArtifactConflict(
            f"artifact {artifact_id!r} vanished between its insert and its read-back")
    for field, stored, given in zip(_IDENTITY_FIELDS, row, 
                                    (supplied[f] for f in _IDENTITY_FIELDS), strict=True):
        if stored != given:
            raise SealedArtifactConflict(
                f"artifact {artifact_id!r} is already sealed with a different {field}: stored "
                f"{stored!r}, this call supplied {given!r}. The id names ONE artifact, so the two "
                f"callers disagree about what it is — and returning either answer would describe a "
                f"row the other one is reading")


def _members_of(
    graphs: OperatorGraphV2 | Sequence[OperatorGraphV2],
) -> tuple[tuple[str, OperatorGraphV2], ...]:
    """``(published columns, graph)`` for every member graph, ORDERED, derived from the graphs.

    A bare graph is accepted as a one-member group — that is what a single-feature group IS, not a
    compatibility shim — so the single-feature callers that predate multi-member sealing keep saying
    the same thing.

    The label comes from each graph's terminal ``GROUP_ASSEMBLY`` — the columns it publishes, in
    published order. Taking it from the graph rather than from a caller-supplied key is what makes a
    finding's attribution unforgeable: there is no second place to say which member a graph belongs
    to, so there is nothing to disagree with.

    Both shapes are legitimate and both are sealed the same way: ONE graph assembling the whole
    group's columns, or one graph per feature each assembling its own. What is refused is two graphs
    claiming the same columns, because then a finding could not say which of them it judged.
    """
    ordered = (graphs,) if isinstance(graphs, OperatorGraphV2) else tuple(graphs)
    if not ordered:
        raise ValueError(
            "seal_v2 was given no graphs: an artifact whose members were never checked would record "
            "a satisfied verdict over nothing, which is the one result that must not be reachable")

    members: list[tuple[str, OperatorGraphV2]] = []
    for graph in ordered:
        terminal = graph.terminal
        if terminal.kind is not OperatorKindV2.GROUP_ASSEMBLY:
            raise ValueError(
                f"a member graph terminates in {terminal.kind.value}, not a group assembly: the "
                f"assembly is what names the columns this graph publishes, and without it a finding "
                f"cannot say which part of the group it is about")
        # The columns this graph assembles, joined, as its label. NOT "exactly one column": the
        # vocabulary's GROUP_ASSEMBLY was designed to assemble THE GROUP's published columns in
        # published order, and a graph doing that for several is legitimate and shipped.
        # `build_operator_graph_v2` happens to emit one graph per feature with a one-column
        # assembly, and an earlier version of this function mistook its own habit for the rule —
        # it refused a two-column assembly a passing test had been building all along.
        members.append((",".join(terminal.payload.column_names), graph))

    names = [name for name, _ in members]
    duplicated = sorted({n for n in names if names.count(n) > 1})
    if duplicated:
        raise ValueError(
            f"two member graphs publish {duplicated}: one column cannot be computed two ways in one "
            f"group, and a verdict keyed on the columns could not say which graph it judged")
    return tuple(sorted(members, key=lambda member: member[0]))


def _require_provenance_covers(
    members: tuple[tuple[str, OperatorGraphV2], ...],
    member_provenance: Sequence[MemberAuthoringInputV1],
) -> None:
    """EXACTLY the published columns — no gap, no stranger.

    The set is the columns the terminal assemblies PUBLISH, not the member labels. A group may be
    sealed as one graph assembling several columns or as one graph per column (``_members_of``
    accepts both), and provenance is per PUBLISHED FEATURE either way — the migration's key is the
    feature name, because that is what a production gate is asked about.

    A GAP is refused because a published column with no provenance row is indistinguishable, later,
    from an artifact whose provenance was never written: both read as "nothing to check". A
    STRANGER is refused because a row filed under a name this artifact does not publish describes
    nothing, and would answer a gate asking about a column that is not here.
    """
    published: set[str] = set()
    for _label, graph in members:
        published.update(graph.terminal.payload.column_names)

    supplied = [member.member_name for member in member_provenance]
    missing = sorted(published - set(supplied))
    unexpected = sorted(set(supplied) - published)
    if missing or unexpected:
        raise ValueError(
            f"member_provenance must describe exactly the columns this artifact publishes: missing "
            f"{missing}, unexpected {unexpected}. A published column with no provenance row reads "
            f"later as 'nothing to check', and a row for a column nobody publishes answers a "
            f"question about a feature this artifact does not contain")


def _fold_requirements(
    members: tuple[tuple[str, OperatorGraphV2], ...],
    requirements: tuple[SubgraphRequirementV2, ...],
) -> tuple[RequirementVerdictV2, tuple[tuple[str, RequirementFindingV2], ...]]:
    """One verdict over every member, and every finding still attached to the member that raised it.

    ALL-OR-NOTHING: satisfied only when every member is. The triggered requirements are the union,
    because "which requirements applied to this artifact" is a question about the group.
    """
    findings: list[tuple[str, RequirementFindingV2]] = []
    triggered: list[str] = []
    for name, graph in members:
        verdict = check_subgraph_requirements_v2(graph, requirements)
        triggered.extend(verdict.triggered)
        findings.extend((name, finding) for finding in verdict.findings)

    folded = RequirementVerdictV2(
        satisfied=not findings,
        triggered=tuple(sorted(set(triggered))),
        findings=tuple(finding for _name, finding in findings),
    )
    return folded, tuple(findings)


def realization_links_of(conn: DbConn, artifact_id: str) -> tuple[RealizationLinkV1, ...]:
    """Which governed policy realizations an artifact depended on — refused or not.

    The question an auditor asks about a published number, answerable for a REFUSED artifact too,
    which is the whole reason the links are written before the verdict is consulted.
    """
    return tuple(
        RealizationLinkV1(revision_id=row[0], occurrence_hash=row[1])
        for row in conn.execute(
            "SELECT revision_id, occurrence_hash FROM sealed_artifact_realization "
            "WHERE artifact_id = %s ORDER BY revision_id, occurrence_hash",
            (artifact_id,)).fetchall())


def load_sealed_artifact(conn: DbConn, artifact_id: str) -> SealedArtifactV2 | None:
    """Rebuild a sealed artifact from the database alone — what a restarted worker starts from.

    The verdict is reconstructed from the stored fields rather than re-derived from the graph: the
    graph is not persisted, and re-deriving would answer with today's requirement set rather than
    the one the artifact was sealed under. A refused artifact loads and stays refused.
    """
    row = conn.execute(
        "SELECT environment_id, logical_group_name, compilation_identity_hash, group_plan_hash, "
        "project_digest, subgraph_satisfied, triggered_requirements, subgraph_findings, "
        "generation_authorization_revision_id "
        "FROM sealed_artifact_v2 WHERE artifact_id = %s", (artifact_id,)).fetchone()
    if row is None:
        return None
    verdict = RequirementVerdictV2(
        satisfied=row[5], triggered=tuple(row[6]),
        findings=tuple(RequirementFindingV2(requirement=item["requirement"], code=item["code"],
                                            detail=item["detail"])
                       for item in row[7]))
    return SealedArtifactV2(
        artifact_id=artifact_id, environment_id=row[0], logical_group_name=row[1],
        # The authorization comes back with the artifact. A loader that dropped it would make
        # "which approval produced this" a second query — and therefore optional.
        generation_authorization_revision_id=row[8],
        compilation_identity_hash=row[2], group_plan_hash=row[3], project_digest=row[4],
        verdict=verdict, realizations=realization_links_of(conn, artifact_id))


def load_manifest(conn: DbConn, artifact_id: str) -> ArtifactManifestV1 | None:
    """The stored manifest for an artifact — the digests a retrieval is checked AGAINST.

    Read from ``generated_artifact_file`` rather than recomputed from the blobs, which is the whole
    reason 1086 carries ``sha256`` on the manifest row as well: a digest read from the same row as
    the bytes could not disprove them.
    """
    rows = conn.execute(
        "SELECT path, sha256, byte_length, media_type, content_reference "
        "FROM generated_artifact_file WHERE artifact_id = %s ORDER BY path",
        (artifact_id,)).fetchall()
    if not rows:
        return None
    return ArtifactManifestV1(
        artifact_id=artifact_id,
        entries=tuple(ArtifactFileEntryV1(path=row[0], sha256=row[1], byte_length=row[2],
                                          media_type=row[3], content_reference=row[4])
                      for row in rows))


def serve_artifact(
    conn: DbConn, sealed: SealedArtifactV2, manifest: ArtifactManifestV1, *, at: str = "retrieval",
) -> dict[str, str]:
    """Fetch and VERIFY every file of a servable artifact.

    Args:
        at: the verification point — ``"retrieval"`` or ``"execution"``. Named rather than assumed,
            because the two failures mean different things and an operator reading "digest mismatch"
            needs to know whether the store returned the wrong bytes or the bytes changed after they
            were fetched.

    Raises:
        ArtifactNotServable: the graph check refused this artifact. Refused first, before any byte
            is fetched — serving it and letting the caller decide would make the check advisory.
        ManifestIntegrityError: a stored file is absent or does not match its manifest entry.
    """
    if not sealed.servable:
        raise ArtifactNotServable(
            f"{sealed.artifact_id} was sealed under a REFUSED subgraph verdict "
            f"({', '.join(finding.code for finding in sealed.verdict.findings)}): the artifact and "
            f"its evidence are recorded, but serving it would execute a graph the check refused")
    if manifest.artifact_id != sealed.artifact_id:
        raise ManifestIntegrityError(
            f"manifest {manifest.artifact_id} does not describe artifact {sealed.artifact_id}: "
            f"serving one artifact's bytes under another's identity is the failure the manifest "
            f"exists to make impossible")
    return {entry.path: fetch_file(conn, entry, at=at) for entry in manifest.entries}


def verify_for_execution(
    manifest: ArtifactManifestV1, files: Mapping[str, str],
) -> Mapping[str, str]:
    """Re-verify already-fetched bytes at the point of EXECUTION.

    Separate from :func:`serve_artifact` because the two answer different questions: retrieval asks
    whether the store returned what it was asked for, execution asks whether the bytes are still
    what was retrieved. A pipeline that verified only once would run whatever arrived in between.
    """
    for entry in manifest.entries:
        if entry.path not in files:
            raise ManifestIntegrityError(
                f"the manifest names {entry.path!r} but the bytes about to execute do not contain "
                f"it: the artifact and what is running describe different things")
        verify_bytes(entry, files[entry.path], at="execution")
    return files
