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
from featuregen.materialize.operator_graph_v2 import OperatorGraphV2
from featuregen.materialize.subgraph_requirements_v2 import (
    PILOT_REQUIREMENTS,
    RequirementFindingV2,
    RequirementVerdictV2,
    SubgraphRequirementV2,
    check_subgraph_requirements_v2,
)

__all__ = [
    "ArtifactNotServable",
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
    graph: OperatorGraphV2,
    manifest: ArtifactManifestV1,
    files: Mapping[str, str],
    *,
    environment_id: str,
    logical_group_name: str,
    compilation_identity_hash: str,
    group_plan_hash: str,
    project_digest: str,
    realizations: Sequence[RealizationLinkV1],
    sealed_at: str,
    requirements: tuple[SubgraphRequirementV2, ...] = PILOT_REQUIREMENTS,
) -> SealedArtifactV2:
    """Check the operator graph, persist the artifact and its evidence, and return what was sealed.

    The graph check runs FIRST but does not short-circuit the record: a refused artifact is stored
    with its findings and its realization links, and only its ``servable`` answer differs. Discarding
    a refusal would leave an operator with a compilation that failed and no record of what it
    depended on.

    Files are stored through :func:`~featuregen.materialize.artifact_store.store_manifest`, which
    verifies every entry against its bytes BEFORE writing — the one point where a renderer
    disagreeing with itself is still recoverable.

    Raises:
        ManifestIntegrityError: a manifest entry and its bytes disagree, or a file the manifest
            names was not supplied.
        ValueError: no environment, or a realization link with a blank half. A link that cannot name
            both ends records a dependency nobody can follow.
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

    verdict = check_subgraph_requirements_v2(graph, requirements)

    # Bytes first: a manifest that does not match its files must not leave a sealed row behind.
    store_manifest(conn, manifest, files)

    conn.execute(
        "INSERT INTO sealed_artifact_v2 (artifact_id, environment_id, logical_group_name, "
        "compilation_identity_hash, group_plan_hash, project_digest, subgraph_satisfied, "
        "triggered_requirements, subgraph_findings, sealed_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s) "
        "ON CONFLICT (artifact_id) DO NOTHING",
        (manifest.artifact_id, environment_id, logical_group_name, compilation_identity_hash,
         group_plan_hash, project_digest, verdict.satisfied,
         json.dumps(list(verdict.triggered)),
         json.dumps([{"requirement": f.requirement, "code": f.code, "detail": f.detail}
                     for f in verdict.findings]),
         sealed_at))

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
        compilation_identity_hash=compilation_identity_hash, group_plan_hash=group_plan_hash,
        project_digest=project_digest, verdict=verdict, realizations=links)


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
        "project_digest, subgraph_satisfied, triggered_requirements, subgraph_findings "
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
