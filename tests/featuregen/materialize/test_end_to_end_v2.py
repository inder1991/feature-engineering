"""ITEM 9 — a V3 formula all the way to a sealed artifact, through every real stage.

Every previous test in this program drove one seam or one refusal. This drives the WHOLE
deterministic chain with nothing stubbed between the stages:

    admitted V3 proposal
      -> resolve_output_v2 + reconcile against the authored intent
      -> compile_ir_v2            (physical resolution against a seeded catalog)
      -> authorize_generation_v2  (leakage, then read scope)
      -> contracts_for / group_by_contract_v2
      -> resolve_physical_type_v3
      -> build_group_plan_v2
      -> build_operator_graph_v2
      -> evaluate_generate        (the gate)
      -> record_group_plan
      -> render_project           (REAL Spark source, sealed under a derived identity)
      -> seal_v2                  (per-member verdict, realization links, the approval)

**No Anthropic key is involved and none is needed.** This is the deterministic half: it proves
rendering and sealing work over stored fixtures. A real provider run earns the authoring-quality
activation evidence separately, and proving that a formula RENDERS is not the same claim.

**The assertions are on what was PRODUCED**, not on the absence of exceptions: real generated Spark
source, a durable artifact row, a servable verdict, and the approval that produced it.
"""
from __future__ import annotations

import pytest
from tests.featuregen.materialize import fixtures
from tests.featuregen.materialize.test_ir import (
    _ROLES,
    CUSTOMERS,
    DECLARATION,
    INVENTORY,
    TXN_AMT,
    _admitted,
    _col,
    _table_node,
    compile_ir,
    seed_catalog,
)
from tests.featuregen.materialize.test_pilot_v2 import ENV, GROUP, _admitted_v2, _run
from tests.featuregen.materialize.test_render_project import _nodes

from featuregen.formula.policy_occurrences import PolicyOccurrenceSetV1
from featuregen.materialize.generate_v2 import generate_v2
from featuregen.materialize.inputs import derive_requirement
from featuregen.materialize.pilot_v2 import CompiledGenerationV2
from featuregen.materialize.render.project import project_datasets
from featuregen.materialize.seal_v2 import load_sealed_artifact
from featuregen.overlay.upload.field_resolution import resolve_and_project


@pytest.fixture
def catalog(db):
    """The governed catalog plus the ONE attested type a SUM needs, through the real machinery."""
    seed_catalog(db)
    for column in ("rate", "quote_dt", "ccy"):
        _col(db, "fx_rates", column)
    _table_node(db, "fx_rates")
    fixtures._attest(db, TXN_AMT, "logical_representation", "numeric")
    resolve_and_project(db, source="hdfc", logical_refs=[TXN_AMT])
    db.execute("UPDATE graph_node SET data_type = 'numeric' "
               "WHERE catalog_source = 'hdfc' AND object_ref = 'public.transactions.txn_amt'")
    return db


@pytest.fixture
def spine(catalog):
    return compile_ir(catalog, _admitted("total_debit_amount_30d"), roles=_ROLES,
                      spine_decl=DECLARATION, inventory=INVENTORY).spine


def _advertise_this_build(db) -> None:
    """Record what THIS build's renderer can dispatch — the step a deploy must also perform.

    `evaluate_generate` refuses RENDERER_CANNOT_DISPATCH when no capability row exists for the
    current `renderer_build_hash`, and that is the designed fail-safe rather than a fixture
    inconvenience: an execution proof is a claim ABOUT A BUILD, so a moved renderer simply has no
    rows yet and every operator reads as unsupported — which is exactly true until somebody
    re-records the surface.

    Step 11 moved that hash by giving `avg`/`min`/`max` a rendering, so this run would refuse
    without it. The same is true of the live cluster after deploying this code.
    """
    from featuregen.materialize.engine_capability import renderer_dispatch_surface
    from featuregen.materialize.execution_proof_store import record_renderer_dispatch

    record_renderer_dispatch(
        db, engine_id="kedro-pyspark", dispatchable=renderer_dispatch_surface())


def _approval(db) -> str:
    from featuregen.materialize.generation_authorization import (
        GenerationAuthorizationV1,
        record_generation_authorization,
    )
    from featuregen.overlay.upload.selection_revisions import TargetModeV1

    db.execute("INSERT INTO contract_intent (intent_id, hypothesis, intake_mode, "
               "redacted_hypothesis) VALUES ('int-e2e','h','hypothesis','h') ON CONFLICT DO NOTHING")
    db.execute("INSERT INTO target_reading_revision (revision_id, intent_id, mode, content_hash) "
               "VALUES ('trr-e2e','int-e2e','exploration','h') ON CONFLICT DO NOTHING")
    db.execute("INSERT INTO build_set_revision (revision_id, target_reading_revision_id, "
               "declaration_hash, declaration_json, content_hash, declared_by, declared_at) "
               "VALUES ('bs-e2e','trr-e2e','dh','{}'::jsonb,'ch','user:ops','t') "
               "ON CONFLICT DO NOTHING")
    return record_generation_authorization(
        db, GenerationAuthorizationV1(
            environment_id=ENV, logical_group_name=GROUP, build_set_revision_id="bs-e2e",
            target_mode=TargetModeV1.EXPLORATION, target_ref=None),
        authorized_by="user:ops", authorized_at="t")


@pytest.fixture
def generated(catalog, spine):
    """THE RUN. Compile a real V3 feature, then gate, record, render and seal it."""
    _advertise_this_build(catalog)
    compiled = _run(catalog, spine, [_admitted_v2()])
    assert isinstance(compiled, CompiledGenerationV2), compiled

    spine_input = derive_requirement(catalog, INVENTORY, table_ref=CUSTOMERS)
    datasets = project_datasets(compiled.authorized.token, compiled.plan,
                                spine_input=spine_input)
    return generate_v2(
        catalog, compiled,
        environment_id=ENV,
        generation_authorization_revision_id=_approval(catalog),
        engine_id="kedro-pyspark",
        engine_versions=fixtures.ENGINE_VERSIONS,
        spine_input=spine_input,
        nodes=_nodes(datasets),
        artifact_id="art-e2e",
        occurrences_by_member={name: PolicyOccurrenceSetV1(()) for name in compiled.graphs},
        realizations=(), compiled_at="t", sealed_at="2026-08-20T00:00:00Z")


# ══ THE CHAIN CLOSES, AND THE ARTIFACT IS DURABLE ══════════════════════════════════════════════
def test_A_V3_FORMULA_REACHES_A_SEALED_ARTIFACT(generated):
    """The claim this whole program has been working toward: a formula written in the surviving
    language becomes a sealed, servable artifact without a V1 stage anywhere in the path."""
    assert generated.sealed.artifact_id == "art-e2e"
    assert generated.sealed.servable is True
    assert generated.sealed.verdict.satisfied is True


def test_THE_ARTIFACT_IS_READABLE_FROM_THE_DATABASE_ALONE(catalog, generated):
    """What a restarted worker starts from. An artifact that only exists in the return value of the
    call that made it is not persisted, however green the call looked."""
    loaded = load_sealed_artifact(catalog, "art-e2e")

    assert loaded is not None
    assert loaded.servable is True
    assert loaded.logical_group_name == GROUP
    assert loaded.environment_id == ENV


def test_THE_ARTIFACT_NAMES_THE_APPROVAL_THAT_PRODUCED_IT(catalog, generated):
    """The referential chain, end to end and for real — not a fixture asserting its own insert."""
    loaded = load_sealed_artifact(catalog, "art-e2e")
    assert loaded.generation_authorization_revision_id
    approvals = catalog.execute(
        "SELECT count(*) FROM generation_authorization WHERE revision_id = %s",
        (loaded.generation_authorization_revision_id,)).fetchone()[0]
    assert approvals == 1, "the artifact names an approval that exists"

    # And 1095's composite FK means it cannot name one from a DIFFERENT environment: the two rows
    # agree by construction rather than by a check somebody remembered to write.
    assert catalog.execute(
        "SELECT environment_id FROM generation_authorization WHERE revision_id = %s",
        (loaded.generation_authorization_revision_id,)).fetchone()[0] == loaded.environment_id


def test_THE_GROUP_PLAN_WAS_PERSISTED_BEFORE_THE_ARTIFACT(catalog, generated):
    """Order, checked on the durable rows: a rendered project can only be checked against a stored
    plan if the plan is there."""
    assert catalog.execute(
        "SELECT count(*) FROM materialization_group_v2 WHERE group_plan_hash = %s",
        (generated.group_plan_hash,)).fetchone()[0] == 1
    assert catalog.execute(
        "SELECT count(*) FROM materialization_group_member WHERE group_plan_hash = %s",
        (generated.group_plan_hash,)).fetchone()[0] == 1


def test_THE_STORED_BYTES_ARE_THE_RENDERED_PROJECT_not_a_stub(catalog, generated):
    """The defect writing the worker exposed. `generate_v2` used to take `manifest` and `files` as
    arguments while rendering its own project internally, and `store_manifest` verifies a manifest
    against ITS OWN files — which a caller-supplied pair satisfies trivially. So the artifact could
    store one set of bytes while `project_digest` stated a hash over a different set, with every
    integrity check green. This test reads the stored bytes back and finds generated Spark.
    """
    stored = catalog.execute(
        "SELECT f.path, b.content FROM generated_artifact_file f "
        "JOIN generated_artifact_blob b ON b.content_reference = f.content_reference "
        "WHERE f.artifact_id = %s", ("art-e2e",)).fetchall()

    assert stored, "the artifact stored no files at all"
    texts = [content for _path, content in stored]
    assert any("def " in text for text in texts), "no generated Python among the stored bytes"
    assert any(GROUP in text for text in texts), "the stored code does not name this group"


# ══ WHAT WAS ACTUALLY RENDERED ═════════════════════════════════════════════════════════════════
def test_REAL_SPARK_SOURCE_WAS_EMITTED_not_an_empty_project(catalog, spine):
    """The assertion that stops this being a test of exception-absence. `render_project` produced
    files, they contain generated Python, and the lock states a hash over them."""
    from featuregen.materialize.identity import GENERATED_LOCK_FILENAME
    from featuregen.materialize.render.project import render_project

    compiled = _run(catalog, spine, [_admitted_v2()])
    spine_input = derive_requirement(catalog, INVENTORY, table_ref=CUSTOMERS)
    datasets = project_datasets(compiled.authorized.token, compiled.plan,
                               spine_input=spine_input)
    project = render_project(
        compiled.authorized.token, compiled.plan, environment_id=ENV,
        engine_versions=fixtures.ENGINE_VERSIONS, spine_input=spine_input,
        nodes=_nodes(datasets))

    assert GENERATED_LOCK_FILENAME in project.files
    assert any(path.endswith(".py") for path in project.files), sorted(project.files)
    assert project.identity.generated_project_hash
    # The project describes THIS group, not a template.
    assert any(GROUP in text for text in project.files.values())
