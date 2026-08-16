"""C-D5/C-D8 — the operator execution proof and compare-and-swap publication.

Two gates: *"the type refuses construction with any field absent and carries no S9 check-set
version"* with *"changed bytes invalidate a proof even with no version bump"*, and *"the signature
carries both"* with *"an older verified output over a newer active revision refuses"*.
"""
from __future__ import annotations

import dataclasses

import pytest

from featuregen.overlay.upload.publication_revisions import (
    ActiveRevisionConflict,
    OperatorExecutionProofV1,
    PublishRequestV1,
    check_publish_precondition,
)

RUNTIMES = (("hive", "3.1.2"), ("spark", "3.3.0"), ("metastore", "3.1.2"), ("python", "3.11.14"),
            ("java", "11.0.20"), ("pyspark", "3.3.0"), ("kedro", "0.19.3"),
            ("kedro_datasets", "2.1.0"))


def _proof(**overrides) -> OperatorExecutionProofV1:
    kwargs = dict(signature="sha256:sig", signature_version=1, compiler_version="1.4.2",
                  renderer_version="2.0.1", physical_type_policy="formula-v2/physical-types@1",
                  topology_version=1, gold_corpus_hash="sha256:gold",
                  generated_project_hash="sha256:project", mutation_set_version=3,
                  engine_versions=RUNTIMES)
    kwargs.update(overrides)
    return OperatorExecutionProofV1(**kwargs)


# ══ C-D5 — every field required, and no S9 check set ═════════════════════════════════════════════
def test_THE_PROOF_CARRIES_NO_S9_CHECK_SET_VERSION():
    """They are deliberately separate concepts and S9's does not exist at S8. A proof carrying one
    would assert something about a stage that has not run — refused by having no field at all,
    because a nullable one gets filled in by somebody eventually."""
    names = {f.name for f in dataclasses.fields(OperatorExecutionProofV1)}
    assert "check_set_version" not in names
    assert "check_set_hash" not in names
    assert "mutation_set_version" in names


@pytest.mark.parametrize("blank", ["signature", "compiler_version", "renderer_version",
                                   "physical_type_policy", "gold_corpus_hash",
                                   "generated_project_hash"])
def test_it_refuses_construction_with_any_field_absent(blank):
    with pytest.raises(ValueError, match="proof about an unknown configuration"):
        _proof(**{blank: "  "})


@pytest.mark.parametrize("version", ["signature_version", "topology_version",
                                     "mutation_set_version"])
def test_every_version_must_name_a_published_one(version):
    with pytest.raises(ValueError, match="names no published version"):
        _proof(**{version: 0})


def test_CHANGED_BYTES_INVALIDATE_A_PROOF_EVEN_WITH_NO_VERSION_BUMP():
    """S8's gate, and the reason the project hash is pinned: a version bump is a claim someone
    remembered to make, and a byte hash is one nobody can forget."""
    unchanged_versions = _proof(generated_project_hash="sha256:project-rebuilt")
    assert unchanged_versions.compiler_version == _proof().compiler_version
    assert unchanged_versions.content_hash != _proof().content_hash


def test_all_EIGHT_runtimes_must_be_pinned():
    """An unpinned runtime is one the proof does not cover."""
    with pytest.raises(ValueError, match="rather than 8"):
        _proof(engine_versions=RUNTIMES[:7])
    with pytest.raises(ValueError, match="pinned twice"):
        _proof(engine_versions=(*RUNTIMES[:7], ("kedro", "0.19.3")))


def test_a_runtime_change_invalidates_the_proof():
    moved = (*RUNTIMES[:1], ("spark", "9.9.9"), *RUNTIMES[2:])
    assert _proof(engine_versions=moved).content_hash != _proof().content_hash


def test_runtime_ORDER_does_not_change_the_proof():
    assert _proof(engine_versions=tuple(reversed(RUNTIMES))).content_hash == _proof().content_hash


def test_the_gold_corpus_and_the_generated_project_are_SEPARATE_pins():
    """The corpus is what was tested against; the project is what was built. A proof needs both, and
    conflating them would let a rebuild against a new corpus look like the same proof."""
    assert _proof(gold_corpus_hash="sha256:other").content_hash != _proof().content_hash
    assert _proof(generated_project_hash="sha256:other").content_hash != _proof().content_hash


# ══ C-D8 — the signature carries BOTH ════════════════════════════════════════════════════════════
def test_THE_PUBLISH_SIGNATURE_CARRIES_BOTH():
    """C-D8's gate."""
    names = {f.name for f in dataclasses.fields(PublishRequestV1)}
    assert names == {"verified_output_revision_id", "expected_active_revision_id"}


def test_AN_OLDER_OUTPUT_OVER_A_NEWER_ACTIVE_REVISION_REFUSES():
    """S10's gate. 1055's trigger stops two concurrent writers both winning; it does not stop a slow
    writer arriving late holding an old answer, because that writer conflicts with nobody."""
    request = PublishRequestV1(verified_output_revision_id="vor-1",
                               expected_active_revision_id="active-1")
    check_publish_precondition(request, observed_active_revision_id="active-1")

    with pytest.raises(ActiveRevisionConflict, match="arrives late holding an old answer"):
        check_publish_precondition(request, observed_active_revision_id="active-2")


def test_a_FIRST_publication_expects_NONE():
    first = PublishRequestV1(verified_output_revision_id="vor-1",
                             expected_active_revision_id=None)
    check_publish_precondition(first, observed_active_revision_id=None)

    with pytest.raises(ActiveRevisionConflict):
        check_publish_precondition(first, observed_active_revision_id="active-1")


def test_NONE_and_BLANK_are_different_claims():
    """None means "there is no active revision yet"; a blank string reads as a revision id that
    happens to be empty."""
    with pytest.raises(ValueError, match="different claims"):
        PublishRequestV1(verified_output_revision_id="vor-1", expected_active_revision_id="  ")


def test_a_publish_must_name_what_it_publishes():
    with pytest.raises(ValueError, match="must name the verified output"):
        PublishRequestV1(verified_output_revision_id=" ", expected_active_revision_id=None)


def test_the_conflict_is_RAISED_not_retried():
    """Whether the newer revision supersedes theirs is a question about the OUTPUTS, not about
    locking; answering it automatically would republish an older result over a newer one for the
    sake of making the call succeed."""
    import inspect

    from featuregen.overlay.upload import publication_revisions

    source = inspect.getsource(publication_revisions.check_publish_precondition)
    for retry in ("while ", "for _ in range", "retry", "sleep"):
        assert retry not in source, retry
