"""S7 — render, seal, persist and serve (1078).

*"Deleting the FX duplicate-rate gate refuses with ``realizes_occurrences`` intact; a mismatched
digest is neither served nor executed; artifact bytes survive a worker restart."*

The first clause's load-bearing half is **intact**. That deleting the gate refuses is C-C10's, and
``test_subgraph_requirements_v2`` already proves it on the graph. What S7 adds is that the refusal
does not take the evidence with it: a graph that fails still depended on exactly the governed
policies it depended on, and an auditor asking "which policies were applied here" must not get an
answer for every artifact that succeeded and silence for every one that did not.
"""
from __future__ import annotations

import psycopg
import pytest
from tests.featuregen.materialize.test_subgraph_requirements_v2 import RATES, _fx_chain, _scan

from featuregen.materialize.artifact_manifest import ManifestIntegrityError, manifest_for
from featuregen.materialize.artifact_store import content_reference_for
from featuregen.materialize.seal_v2 import (
    ArtifactNotServable,
    RealizationLinkV1,
    load_manifest,
    load_sealed_artifact,
    realization_links_of,
    seal_v2,
    serve_artifact,
    verify_for_execution,
)
from featuregen.materialize.subgraph_requirements_v2 import MISSING_OPERATOR

ENV = "hdfc-local"
GROUP = "customer_txn_features"

FILES = {
    "conf/base/catalog.yml": "txn_features:\n  type: MemoryDataset\n",
    "src/pipeline.py": "def create_pipeline():\n    return None\n",
}

LINKS = (
    RealizationLinkV1(revision_id="rev-fx", occurrence_hash="occ-fx-001"),
    RealizationLinkV1(revision_id="rev-direction", occurrence_hash="occ-dir-001"),
)


def _authorization(db) -> str:
    """A real approval for this environment and group — the artifact's FK target.

    Seeded through `record_generation_authorization` rather than a raw INSERT: the composite keys
    below only mean something if the row they point at was made the way production makes it.
    """
    from featuregen.materialize.generation_authorization import (
        GenerationAuthorizationV1,
        record_generation_authorization,
    )
    from featuregen.overlay.upload.selection_revisions import TargetModeV1

    db.execute("INSERT INTO contract_intent (intent_id, hypothesis, intake_mode, "
               "redacted_hypothesis) VALUES ('int-seal','h','hypothesis','h') "
               "ON CONFLICT DO NOTHING")
    db.execute("INSERT INTO target_reading_revision (revision_id, intent_id, mode, content_hash) "
               "VALUES ('trr-seal','int-seal','exploration','h') ON CONFLICT DO NOTHING")
    db.execute("INSERT INTO build_set_revision (revision_id, target_reading_revision_id, "
               "declaration_hash, declaration_json, content_hash, declared_by, declared_at) "
               "VALUES ('bs-seal','trr-seal','dh','{}'::jsonb,'ch','user:ops','2026-08-20') "
               "ON CONFLICT DO NOTHING")
    return record_generation_authorization(
        db, GenerationAuthorizationV1(
            environment_id=ENV, logical_group_name=GROUP, build_set_revision_id="bs-seal",
            target_mode=TargetModeV1.EXPLORATION, target_ref=None),
        authorized_by="user:ops", authorized_at="2026-08-20T00:00:00Z")


def _manifest(artifact_id: str = "art-1", files=None):
    return manifest_for(artifact_id, files or FILES,
                        content_reference=lambda path: content_reference_for(
                            (files or FILES)[path]))


def _seal(db, *, graph=None, artifact_id: str = "art-1", files=None, links=LINKS):
    payload = files or FILES
    return seal_v2(
        db, graph if graph is not None else _fx_chain(), _manifest(artifact_id, payload), payload,
        environment_id=ENV, logical_group_name=GROUP,
        compilation_identity_hash="sha256:compilation", group_plan_hash="sha256:plan",
        project_digest="sha256:project", realizations=links, sealed_at="2026-08-17T00:00:00Z",
        generation_authorization_revision_id=_authorization(db))


# ══ ACCEPTANCE 1 — deleting the FX duplicate-rate gate refuses, links INTACT ════════════════════
def test_DELETING_THE_DUPLICATE_RATE_GATE_REFUSES(db):
    """An as-of join amplifies silently when the rate side offers two rows for a key at an instant.
    That is a wrong number rather than an error, which is why the gate is required wherever a rate
    was joined."""
    sealed = _seal(db, graph=_fx_chain(duplicate_gate=False))
    assert sealed.servable is False
    assert sealed.verdict.satisfied is False
    assert {finding.code for finding in sealed.verdict.findings} == {MISSING_OPERATOR}
    assert any("duplicate_rate_gate" in finding.detail for finding in sealed.verdict.findings)


def test_THE_REALIZATION_LINKS_SURVIVE_THE_REFUSAL(db):
    """The clause's load-bearing half. A refused compilation depended on exactly the policies it
    depended on, and dropping the links destroys that record at the moment it becomes most
    interesting."""
    _seal(db, graph=_fx_chain(duplicate_gate=False))

    stored = realization_links_of(db, "art-1")
    assert stored == tuple(sorted(LINKS, key=lambda link: link.revision_id))
    assert {link.occurrence_hash for link in stored} == {"occ-fx-001", "occ-dir-001"}


def test_a_refused_artifact_is_RECORDED_not_discarded(db):
    """Otherwise an operator is left with a compilation that failed and no record of what it was."""
    _seal(db, graph=_fx_chain(missing_gate=False))
    row = db.execute(
        "SELECT subgraph_satisfied, triggered_requirements, jsonb_array_length(subgraph_findings) "
        "FROM sealed_artifact_v2 WHERE artifact_id = %s", ("art-1",)).fetchone()
    assert row[0] is False
    assert row[1] == ["FX_CONVERSION"]
    assert row[2] > 0


def test_the_INTACT_graph_seals_and_is_servable(db):
    """The discriminator: the same everything, with the gate present."""
    sealed = _seal(db)
    assert sealed.verdict.satisfied is True
    assert sealed.servable is True
    assert sealed.verdict.triggered == ("FX_CONVERSION",)
    assert realization_links_of(db, "art-1") == tuple(
        sorted(LINKS, key=lambda link: link.revision_id))


def test_an_UNTRIGGERED_requirement_is_not_recorded_as_a_pass(db):
    """C-C10's rule, carried into what is stored: a fixed-base-currency feature contains no as-of FX
    join, so recording "FX subgraph sound" would claim an inspection nobody ran."""
    from tests.featuregen.materialize.test_subgraph_requirements_v2 import _fixed_aed_pilot

    sealed = _seal(db, graph=_fixed_aed_pilot())
    assert sealed.verdict.satisfied is True
    assert sealed.verdict.triggered == ()      # NOT ("FX_CONVERSION",)
    assert db.execute(
        "SELECT triggered_requirements FROM sealed_artifact_v2 WHERE artifact_id = %s",
        ("art-1",)).fetchone()[0] == []


def test_a_gate_ON_A_BRANCH_THAT_NEVER_SEES_THE_JOIN_refuses_too(db):
    """A gate that EXISTS, is genuinely consumed, and sits on a branch that never sees the joined
    rows satisfies "contains a duplicate-rate gate" and protects nothing. The graph type cannot
    catch this — it has one terminal and no dangling edges — which is why position is checked."""
    from featuregen.materialize.operator_graph_v2 import (
        DuplicateRateGateV2,
        GroupAssemblyV2,
        OperatorGraphV2,
        OperatorKindV2,
        OperatorNodeV2,
    )

    complete = _fx_chain(duplicate_gate=False)
    scan = _scan()
    stray = OperatorNodeV2(OperatorKindV2.DUPLICATE_RATE_GATE,
                           DuplicateRateGateV2((f"{RATES}.ccy",)), inputs=(scan.node_id,))
    old_terminal = complete.terminal
    assembled = OperatorNodeV2(
        OperatorKindV2.GROUP_ASSEMBLY, GroupAssemblyV2(("f", "g")),
        inputs=(old_terminal.inputs[0], stray.node_id))
    rest = tuple(node for node in complete.nodes if node.node_id != old_terminal.node_id)
    graph = OperatorGraphV2(nodes=(*rest, stray, assembled))

    assert OperatorKindV2.DUPLICATE_RATE_GATE in graph.kinds, "the gate really is present"
    sealed = _seal(db, graph=graph)
    assert sealed.servable is False
    # The links are kept here too — the refusal is about position, not about the policies.
    assert realization_links_of(db, "art-1")


def test_a_REFUSED_artifact_is_NOT_SERVED(db):
    """Refused before any byte is fetched: serving it and letting the caller decide would make the
    check advisory."""
    sealed = _seal(db, graph=_fx_chain(duplicate_gate=False))
    with pytest.raises(ArtifactNotServable, match="refused"):
        serve_artifact(db, sealed, _manifest())


# ══ ACCEPTANCE 2 — a MISMATCHED DIGEST is neither served nor executed ═══════════════════════════
def test_A_MISMATCHED_DIGEST_IS_NOT_SERVED(db):
    """The store returned something else. Caught at RETRIEVAL, where it becomes visible, rather
    than at execution, where it becomes damage."""
    sealed = _seal(db)
    tampered = _manifest()
    # Reach past the writer: the manifest is asked for bytes whose entry claims a different digest.
    db.execute(
        "INSERT INTO generated_artifact_blob (content_reference, content, byte_length) "
        "VALUES (%s, %s, %s)",
        ("sha256:not-the-real-digest", "totally different bytes", 23))
    import dataclasses

    entry = dataclasses.replace(tampered.entries[0],
                                content_reference="sha256:not-the-real-digest")
    forged = dataclasses.replace(tampered, entries=(entry, *tampered.entries[1:]))

    with pytest.raises(ManifestIntegrityError, match="retrieval"):
        serve_artifact(db, sealed, forged)


def test_A_MISSING_BLOB_IS_NOT_SERVED(db):
    """The manifest promises bytes nothing can produce."""
    sealed = _seal(db)
    import dataclasses

    manifest = _manifest()
    entry = dataclasses.replace(manifest.entries[0], content_reference="sha256:never-stored")
    forged = dataclasses.replace(manifest, entries=(entry, *manifest.entries[1:]))

    with pytest.raises(ManifestIntegrityError, match="does not hold"):
        serve_artifact(db, sealed, forged)


def test_A_MISMATCHED_DIGEST_IS_NOT_EXECUTED(db):
    """The second verification point, and it is not the same question: retrieval asks whether the
    store returned what it was asked for, execution asks whether the bytes are still what was
    retrieved. A pipeline verifying once would run whatever arrived in between."""
    manifest = _manifest()
    _seal(db)
    changed = dict(FILES)
    changed["src/pipeline.py"] = "def create_pipeline():\n    return SOMETHING_ELSE\n"

    with pytest.raises(ManifestIntegrityError, match="execution"):
        verify_for_execution(manifest, changed)


def test_a_file_MISSING_at_execution_is_refused(db):
    manifest = _manifest()
    _seal(db)
    with pytest.raises(ManifestIntegrityError, match="about to execute"):
        verify_for_execution(manifest, {"conf/base/catalog.yml": FILES["conf/base/catalog.yml"]})


def test_serving_ANOTHER_ARTIFACTS_manifest_is_refused(db):
    """Serving one artifact's bytes under another's identity is the failure the manifest exists to
    make impossible."""
    sealed = _seal(db)
    with pytest.raises(ManifestIntegrityError, match="does not describe"):
        serve_artifact(db, sealed, _manifest("art-other"))


def test_the_manifest_is_verified_BEFORE_anything_is_written(db):
    """At write the renderer disagrees with itself — the one point where that mistake is still
    recoverable, so nothing may be stored past it."""
    manifest = _manifest()
    with pytest.raises(ManifestIntegrityError):
        seal_v2(db, _fx_chain(), manifest,
                {**FILES, "src/pipeline.py": "different bytes entirely"},
                environment_id=ENV, logical_group_name=GROUP,
                compilation_identity_hash="sha256:c", group_plan_hash="sha256:p",
                project_digest="sha256:d", realizations=LINKS, sealed_at="t",
                generation_authorization_revision_id=_authorization(db))
    assert db.execute(
        "SELECT count(*) FROM sealed_artifact_v2").fetchone()[0] == 0


# ══ ACCEPTANCE 3 — artifact bytes SURVIVE A WORKER RESTART ═════════════════════════════════════
def test_ARTIFACT_BYTES_SURVIVE_A_WORKER_RESTART(db):
    """A restarted worker holds NOTHING. It has an artifact id and a database, so everything else —
    the sealed record, the verdict, the manifest and the bytes — has to come back out of the store.

    Not simulated with a second connection: the suite runs each test in one transaction, so a
    second connection would see an empty database and the test would prove the fixture rather than
    the claim. What is simulated is the state a restart destroys — every Python object from the
    sealing side is discarded and the artifact is reconstructed from its id alone.
    """
    _seal(db)

    restored = load_sealed_artifact(db, "art-1")
    manifest = load_manifest(db, "art-1")
    assert restored is not None and manifest is not None
    assert restored.servable is True
    assert restored.project_digest == "sha256:project"
    assert restored.verdict.triggered == ("FX_CONVERSION",)
    assert restored.realizations == tuple(sorted(LINKS, key=lambda link: link.revision_id))

    assert serve_artifact(db, restored, manifest) == FILES


def test_a_REFUSED_artifact_loads_and_STAYS_refused(db):
    """The verdict is reconstructed from the stored fields, never re-derived: the graph is not
    persisted, and re-deriving would answer with TODAY'S requirement set rather than the one the
    artifact was sealed under."""
    _seal(db, graph=_fx_chain(duplicate_gate=False))

    restored = load_sealed_artifact(db, "art-1")
    assert restored is not None
    assert restored.servable is False
    assert {finding.code for finding in restored.verdict.findings} == {MISSING_OPERATOR}
    assert restored.realizations == tuple(sorted(LINKS, key=lambda link: link.revision_id))
    with pytest.raises(ArtifactNotServable):
        serve_artifact(db, restored, load_manifest(db, "art-1"))


def test_loading_an_UNKNOWN_artifact_is_None_not_an_empty_one(db):
    """An empty artifact would let a retrieval that found nothing look like a complete one."""
    assert load_sealed_artifact(db, "art-never-sealed") is None
    assert load_manifest(db, "art-never-sealed") is None


def test_the_bytes_are_addressed_by_CONTENT_not_by_artifact(db):
    """Two artifacts that render byte-identical files share one row, so a re-render of unchanged
    bytes stores nothing new — and neither can invalidate the other's copy."""
    _seal(db)
    _seal(db, artifact_id="art-2")
    stored = db.execute("SELECT count(*) FROM generated_artifact_blob").fetchone()[0]
    assert stored == len(FILES)


def test_no_module_level_cache_stands_between_the_store_and_the_caller():
    """The claim above is only true if nothing memoizes. Checked structurally, because a cache added
    later would make the restart test pass for the wrong reason."""
    import inspect

    from featuregen.materialize import artifact_store
    from featuregen.materialize import seal_v2 as seal_module

    for module in (artifact_store, seal_module):
        source = inspect.getsource(module)
        assert "lru_cache" not in source, module.__name__
        assert "@cache" not in source, module.__name__


# ══ the record's own rules ══════════════════════════════════════════════════════════════════════
def test_a_sealed_artifact_must_name_its_ENVIRONMENT(db):
    with pytest.raises(ValueError, match="deployment placement"):
        seal_v2(db, _fx_chain(), _manifest(), FILES, environment_id="  ",
                logical_group_name=GROUP, compilation_identity_hash="sha256:c",
                group_plan_hash="sha256:p", project_digest="sha256:d", realizations=LINKS,
                sealed_at="t", generation_authorization_revision_id=_authorization(db))


def test_a_realization_link_with_a_BLANK_HALF_is_refused(db):
    with pytest.raises(ValueError, match="nobody can follow back"):
        _seal(db, links=(RealizationLinkV1(revision_id="rev-fx", occurrence_hash="  "),))


@pytest.mark.parametrize("table,column,key", [
    ("sealed_artifact_v2", "project_digest", "artifact_id"),
    ("sealed_artifact_realization", "occurrence_hash", "artifact_id"),
])
def test_the_seal_record_is_APPEND_ONLY(db, table, column, key):
    _seal(db)
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        db.execute(f"UPDATE {table} SET {column} = %s WHERE {key} = %s",
                   ("sha256:rewritten", "art-1"))


def test_sealing_the_same_artifact_twice_is_idempotent(db):
    _seal(db)
    _seal(db)
    assert db.execute(
        "SELECT count(*) FROM sealed_artifact_realization WHERE artifact_id = %s",
        ("art-1",)).fetchone()[0] == len(LINKS)


def test_the_migration_forbids_a_PASS_that_carries_findings(db):
    """A stored row whose verdict and findings disagree would be resolved by whichever field the
    reader happened to trust."""
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute(
            "INSERT INTO sealed_artifact_v2 (artifact_id, generation_authorization_revision_id, "
            "environment_id, logical_group_name, "
            "compilation_identity_hash, group_plan_hash, project_digest, subgraph_satisfied, "
            "triggered_requirements, subgraph_findings, sealed_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, true, '[]'::jsonb, "
            "'[{\"code\": \"X\"}]'::jsonb, %s)",
            ("art-bad", _authorization(db), ENV, GROUP, "sha256:c", "sha256:p", "sha256:d", "t"))


def test_the_migration_forbids_a_REFUSAL_with_no_findings(db):
    """A refusal with no findings tells an author nothing to fix, and is indistinguishable from a
    check that failed for its own reasons."""
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute(
            "INSERT INTO sealed_artifact_v2 (artifact_id, generation_authorization_revision_id, "
            "environment_id, logical_group_name, "
            "compilation_identity_hash, group_plan_hash, project_digest, subgraph_satisfied, "
            "triggered_requirements, subgraph_findings, sealed_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, false, '[]'::jsonb, '[]'::jsonb, %s)",
            ("art-bad", _authorization(db), ENV, GROUP, "sha256:c", "sha256:p", "sha256:d", "t"))


# ══ A GROUP IS ONE ARTIFACT AND MANY GRAPHS ════════════════════════════════════════════════════
def _named(column: str, *, duplicate_gate: bool = True):
    """An FX chain assembling ONE named column — one member of a multi-feature group."""
    from featuregen.materialize.operator_graph_v2 import (
        GroupAssemblyV2,
        OperatorGraphV2,
        OperatorKindV2,
        OperatorNodeV2,
    )

    base = _fx_chain(duplicate_gate=duplicate_gate)
    assembled = OperatorNodeV2(
        OperatorKindV2.GROUP_ASSEMBLY, GroupAssemblyV2((column,)),
        inputs=(base.terminal.node_id,))
    return OperatorGraphV2(nodes=(*base.nodes, assembled))


def test_EVERY_MEMBER_OF_A_GROUP_IS_CHECKED(db):
    """`compile_generation_v2` produces one graph per feature and the artifact is the GROUP's
    project — one manifest, one publication target. Sealing checked ONE graph and recorded that
    verdict as though it covered all of them."""
    sealed = _seal_many(db, [_named("alpha"), _named("beta")])
    assert sealed.servable is True


def test_ONE_BAD_MEMBER_REFUSES_THE_WHOLE_ARTIFACT(db):
    """All-or-nothing, the rule the rest of the chain uses. A group is published as one row per
    key, so a partially-satisfied group would publish some columns computed under requirements
    nobody checked."""
    sealed = _seal_many(db, [_named("alpha"), _named("beta", duplicate_gate=False)])

    assert sealed.servable is False
    assert {f.code for f in sealed.verdict.findings} == {MISSING_OPERATOR}


def test_A_FINDING_NAMES_THE_MEMBER_THAT_RAISED_IT(db):
    """A finding that names only the requirement sends an operator to read every graph in the group
    to discover which one failed. The member is read off the graph's own assembly, so there is no
    second place to say which member a graph is and nothing to disagree with."""
    _seal_many(db, [_named("alpha"), _named("beta", duplicate_gate=False)],
               artifact_id="art-named")
    stored = db.execute(
        "SELECT subgraph_findings FROM sealed_artifact_v2 WHERE artifact_id='art-named'"
    ).fetchone()[0]

    assert [f["feature_name"] for f in stored] == ["beta"]
    assert stored[0]["code"] == MISSING_OPERATOR


def test_TWO_GRAPHS_CLAIMING_THE_SAME_COLUMN_IS_A_CALLER_ERROR(db):
    """One column cannot be computed two ways in one group, and a verdict keyed on the columns
    could not say which graph it judged."""
    with pytest.raises(ValueError, match="two member graphs publish"):
        _seal_many(db, [_named("alpha"), _named("alpha")])


def test_SEALING_NO_GRAPHS_AT_ALL_IS_REFUSED(db):
    """The one result that must not be reachable: a satisfied verdict over nothing."""
    with pytest.raises(ValueError, match="was given no graphs"):
        _seal_many(db, [])


def test_a_SINGLE_GRAPH_IS_STILL_A_ONE_MEMBER_GROUP(db):
    """Not a compatibility shim — that is what a single-feature group IS. The callers that predate
    multi-member sealing keep saying exactly the same thing."""
    sealed = _seal(db)
    assert sealed.servable is True


def _seal_many(db, graphs, *, artifact_id: str = "art-many"):
    payload = FILES
    return seal_v2(
        db, graphs, _manifest(artifact_id, payload), payload,
        environment_id=ENV, logical_group_name=GROUP,
        compilation_identity_hash="sha256:compilation", group_plan_hash="sha256:plan",
        project_digest="sha256:project", realizations=LINKS, sealed_at="2026-08-20T00:00:00Z",
        generation_authorization_revision_id=_authorization(db))
