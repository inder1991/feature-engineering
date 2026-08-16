"""C-D5/C-D8 — the operator execution proof, and compare-and-swap publication.

**A proof is about BYTES, not versions** (C-D5). Every version field is pinned, and so is the exact
generated gold-project hash — so changed bytes invalidate a proof even when nobody bumped anything.
That is the whole point: a version bump is a claim someone remembered to make, and a byte hash is
one nobody can forget.

**The mutation-set version is NOT S9's check set.** They are deliberately separate concepts and S9's
does not exist at S8. A proof carrying a check-set version would be asserting something about a
stage that has not run, so this type refuses to have the field at all rather than leaving it
nullable — a nullable one would be filled in by somebody eventually.

**Publication is a COMPARE-AND-SWAP** (C-D8). Migration 1055's trigger stops two concurrent writers
both winning; it does not stop a slow writer publishing a stale verified output over a newer active
revision, because that writer never conflicts with anyone — it simply arrives late holding an old
answer. The expected active revision is therefore part of the call.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from featuregen.canonical import jcs_sha256

__all__ = [
    "ActiveRevisionConflict",
    "OperatorExecutionProofV1",
    "PublishRequestV1",
    "check_publish_precondition",
]


@dataclass(frozen=True, slots=True)
class OperatorExecutionProofV1:
    """Proof that the pilot operator graph executed correctly, pinned to everything that decided it.

    Every field is REQUIRED. A proof with a missing pin is a proof about an unknown configuration,
    which is not a proof — so construction refuses rather than defaulting.
    """

    signature: str
    signature_version: int
    compiler_version: str
    renderer_version: str
    physical_type_policy: str
    topology_version: int
    gold_corpus_hash: str
    generated_project_hash: str
    mutation_set_version: int
    engine_versions: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        for value, what in (
            (self.signature, "signature"),
            (self.compiler_version, "compiler_version"),
            (self.renderer_version, "renderer_version"),
            (self.physical_type_policy, "physical_type_policy"),
            (self.gold_corpus_hash, "gold_corpus_hash"),
            (self.generated_project_hash, "generated_project_hash"),
        ):
            if not value.strip():
                raise ValueError(
                    f"an operator execution proof with a blank {what} is a proof about an unknown "
                    f"configuration, which is not a proof")
        for value, what in ((self.signature_version, "signature_version"),
                            (self.topology_version, "topology_version"),
                            (self.mutation_set_version, "mutation_set_version")):
            if value < 1:
                raise ValueError(f"{what}={value} names no published version")
        if len(self.engine_versions) != 8:
            raise ValueError(
                f"a proof pinning {len(self.engine_versions)} runtimes rather than 8: the artifact "
                f"runs on all eight, and an unpinned one is a runtime the proof does not cover")
        names = [n for n, _ in self.engine_versions]
        if len(set(names)) != len(names):
            raise ValueError(f"a runtime is pinned twice: {sorted(names)}")

    def identity_payload(self) -> dict[str, Any]:
        return {
            "signature": self.signature,
            "signature_version": self.signature_version,
            "compiler_version": self.compiler_version,
            "renderer_version": self.renderer_version,
            "physical_type_policy": self.physical_type_policy,
            "topology_version": self.topology_version,
            "gold_corpus_hash": self.gold_corpus_hash,
            "generated_project_hash": self.generated_project_hash,
            "mutation_set_version": self.mutation_set_version,
            "engine_versions": [list(p) for p in sorted(self.engine_versions)],
        }

    @property
    def content_hash(self) -> str:
        return jcs_sha256(self.identity_payload())


class ActiveRevisionConflict(Exception):
    """The active revision moved since the caller read it — the publish is refused, not retried.

    Not retried HERE on purpose: the caller read a verified output against one active revision and
    the world has since changed. Whether the newer revision supersedes theirs is a question about
    the outputs, not about locking, and answering it automatically would republish an older result
    over a newer one for the sake of making the call succeed.
    """


@dataclass(frozen=True, slots=True)
class PublishRequestV1:
    """``publish_sandbox(verified_output_revision_id, expected_active_revision_id)``.

    ``expected_active_revision_id`` is ``None`` only for the FIRST publication, where there is no
    active revision to compare against. That is a distinct claim from "I did not check", which is
    why it is None rather than an empty string — an empty string would read as a revision id that
    happens to be blank.
    """

    verified_output_revision_id: str
    expected_active_revision_id: str | None

    def __post_init__(self) -> None:
        if not self.verified_output_revision_id.strip():
            raise ValueError("a publish request must name the verified output it publishes")
        if self.expected_active_revision_id is not None and (
                not self.expected_active_revision_id.strip()):
            raise ValueError(
                "expected_active_revision_id is blank rather than None: None means 'there is no "
                "active revision yet', and a blank string reads as a revision id that happens to "
                "be empty — the two are different claims")


def check_publish_precondition(
    request: PublishRequestV1, *, observed_active_revision_id: str | None,
) -> None:
    """Refuse a publish whose expected active revision is not the one in place.

    Raises:
        ActiveRevisionConflict: the active revision moved, or the caller expected none and one
            exists (a first publication racing a first publication).
    """
    if request.expected_active_revision_id != observed_active_revision_id:
        raise ActiveRevisionConflict(
            f"expected active revision {request.expected_active_revision_id!r} but found "
            f"{observed_active_revision_id!r}. Migration 1055's trigger stops two concurrent "
            f"writers both winning; it does not stop a slow writer publishing a stale verified "
            f"output over a newer active revision, because that writer never conflicts with "
            f"anyone — it simply arrives late holding an old answer")
