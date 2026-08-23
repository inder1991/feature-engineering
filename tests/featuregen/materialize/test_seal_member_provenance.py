"""1099 — HOW EACH MEMBER OF A SEALED ARTIFACT WAS AUTHORED, written by the act that seals it.

The sealed row records formula and IR hashes, the plan, the environment and the authorization — and
nothing about the authoring RUN or METHOD. Both disappear at sealing, so a production gate choosing
between a platform gold evaluation and a deterministic compiler certification had nothing to choose
from. These tests hold the four properties that make the new row worth trusting:

1. **One row per PUBLISHED COLUMN**, because one artifact can hold several features authored
   differently and a single method on the artifact could only ever be a lie about one of them.
2. **The method is DERIVED from the run's own evidence**, never supplied — `MemberAuthoringInputV1`
   has no field for it, and the derivation is re-runnable from the stored row.
3. **Refuse rather than guess.** A member whose evidence establishes no method stops the WHOLE seal,
   before any byte is stored, and a member omitted from the call is refused rather than skipped.
4. **Append-only**, because a row that could be edited afterwards would let an artifact acquire a
   more convenient authoring method than the one it had.

Every run named here is driven through the real orchestrator (`provenance_fixtures`). Nothing
inserts a trace event or a dispatch row by hand: a fixture that wrote `REVIEW_BYPASSED` directly
would prove the derivation reads a row somebody typed.
"""
from __future__ import annotations

import psycopg
import pytest
from tests.featuregen.materialize.provenance_fixtures import (
    evidenced_members,
    proposal_hash_of,
    reviewed_blueprint_run,
)
from tests.featuregen.materialize.test_seal_v2_s7 import (
    ENV,
    FILES,
    GROUP,
    LINKS,
    _authorization,
    _manifest,
    _named,
)

from featuregen.materialize.authoring_provenance import (
    REVIEWED_RECIPE_BLUEPRINT,
    MemberAuthoringInputV1,
    MemberProvenanceRefused,
    derive_authoring_method,
    member_provenance_of,
)
from featuregen.materialize.seal_v2 import seal_v2


def _seal(db, graphs, provenance, *, artifact_id="art-prov"):
    return seal_v2(
        db, graphs, _manifest(artifact_id, FILES), FILES,
        environment_id=ENV, logical_group_name=GROUP,
        compilation_identity_hash="sha256:compilation", group_plan_hash="sha256:plan",
        project_digest="sha256:project", realizations=LINKS,
        member_provenance=provenance, sealed_at="2026-08-22T00:00:00Z",
        generation_authorization_revision_id=_authorization(db))


def _two_members(db):
    """Two graphs publishing `alpha` and `beta`, each authored by its OWN real run."""
    graphs = [_named("alpha"), _named("beta")]
    return graphs, evidenced_members(db, "alpha", "beta", run_prefix="far-prov")


# ══ 1. ONE ROW PER PUBLISHED COLUMN ════════════════════════════════════════════════════════════
def test_SEALING_WRITES_ONE_PROVENANCE_ROW_PER_MEMBER(db):
    """A group is one artifact and many features. Two members authored by two runs produce two
    rows, each naming its own run — which is the whole reason 1099 is a child table rather than a
    column on the artifact."""
    graphs, provenance = _two_members(db)
    _seal(db, graphs, provenance)

    rows = member_provenance_of(db, "art-prov")
    assert [row.member_name for row in rows] == ["alpha", "beta"]
    assert len({row.authoring_run_id for row in rows}) == 2, "two members, two authoring acts"
    for row, supplied in zip(rows, provenance, strict=True):
        assert row.selection_revision_id == supplied.selection_revision_id
        assert row.authoring_run_id == supplied.authoring_run_id
        assert row.formula_content_hash == supplied.formula_content_hash
        assert row.authoring_method == REVIEWED_RECIPE_BLUEPRINT


def test_the_ROWS_COME_BACK_FROM_THE_STORE_ALONE(db):
    """What a production gate holds is an artifact id and a database. An answer that existed only
    in the return value of the sealing call would not be a record of anything."""
    graphs, provenance = _two_members(db)
    _seal(db, graphs, provenance)

    stored = db.execute(
        "SELECT member_name, authoring_method FROM sealed_artifact_member_provenance "
        "WHERE artifact_id = 'art-prov' ORDER BY member_name").fetchall()
    assert stored == [("alpha", REVIEWED_RECIPE_BLUEPRINT), ("beta", REVIEWED_RECIPE_BLUEPRINT)]


def test_PROVENANCE_IS_KEPT_FOR_A_REFUSED_ARTIFACT_TOO(db):
    """The realization links' rule, and it holds for the same reason: a refused compilation was
    still authored somehow, and "where did this formula come from" is the same question about a
    build that failed as about one that passed."""
    graphs = [_named("alpha", duplicate_gate=False)]
    sealed = _seal(db, graphs, evidenced_members(db, "alpha", run_prefix="far-refused"))

    assert sealed.servable is False
    assert [row.member_name for row in member_provenance_of(db, "art-prov")] == ["alpha"]


# ══ 2. THE METHOD IS DERIVED, NEVER SUPPLIED ═══════════════════════════════════════════════════
def test_the_CALLER_HAS_NO_WAY_TO_ASSERT_A_METHOD(db):
    """▲ Structural, because it is the design rather than a behaviour: the input a caller supplies
    carries facts it holds — which selection, which draft, which run, which bytes — and no
    `authoring_method` field at all. A field for one would be a place to put an asserted method,
    and an asserted method is exactly what this table exists to make impossible."""
    fields = set(MemberAuthoringInputV1.__dataclass_fields__)

    assert "authoring_method" not in fields
    assert "authoring_evidence_hash" not in fields
    assert fields == {"member_name", "selection_revision_id", "authoring_run_id",
                      "formula_content_hash", "formula_draft_id"}


def test_the_STORED_METHOD_RE_DERIVES_FROM_THE_RUN(db):
    """The evidence hash is what makes the method CHECKABLE rather than asserted: a reader
    re-derives it from the same run and compares. A stored hash nothing could reproduce would
    certify nothing."""
    graphs, provenance = _two_members(db)
    _seal(db, graphs, provenance)

    for row in member_provenance_of(db, "art-prov"):
        again = derive_authoring_method(db, row.authoring_run_id)
        assert again.authoring_method == row.authoring_method
        assert again.evidence_hash == row.authoring_evidence_hash


# ══ 3. REFUSE RATHER THAN GUESS ════════════════════════════════════════════════════════════════
def test_a_MEMBER_WITH_NO_PROVENANCE_IS_REFUSED_not_skipped(db):
    """▲ The rule that makes the row worth reading. A published column with no provenance row is
    indistinguishable, later, from an artifact whose provenance was never written — both read as
    "nothing to check", which a gate would have to treat as a pass or as a refusal with no way to
    tell which it is looking at."""
    graphs = [_named("alpha"), _named("beta")]
    only_one = evidenced_members(db, "alpha", run_prefix="far-partial")

    with pytest.raises(ValueError, match=r"missing \['beta'\]"):
        _seal(db, graphs, only_one)

    assert db.execute("SELECT count(*) FROM sealed_artifact_v2").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM generated_artifact_file").fetchone()[0] == 0


def test_PROVENANCE_FOR_A_COLUMN_THE_ARTIFACT_DOES_NOT_PUBLISH_IS_REFUSED(db):
    """A row filed under a name this artifact does not publish describes nothing, and would answer
    a gate asking about a feature that is not here."""
    graphs = [_named("alpha")]
    strangers = evidenced_members(db, "alpha", "gamma", run_prefix="far-stranger")

    with pytest.raises(ValueError, match=r"unexpected \['gamma'\]"):
        _seal(db, graphs, strangers)


def test_a_RUN_THAT_ESTABLISHES_NO_METHOD_REFUSES_THE_SEAL(db):
    """▲ The refusal this whole design turns on. `far-nothing-happened` has no reconciled provider
    calls and no reviewed bypass, so how its formula was written is unanswerable — and an
    unanswerable provenance is not a permissive one."""
    graphs = [_named("alpha")]
    invented = (MemberAuthoringInputV1(
        member_name="alpha", selection_revision_id="sel-1",
        authoring_run_id="far-nothing-happened", formula_content_hash="sha256:whatever"),)

    with pytest.raises(MemberProvenanceRefused, match="cannot establish how its formula"):
        _seal(db, graphs, invented)


def test_the_REFUSAL_WRITES_NOTHING_AT_ALL(db):
    """Derivation runs BEFORE the manifest is stored, so a member whose method cannot be
    established leaves no bytes, no artifact row and no provenance row — the same rule the manifest
    check follows, and for the same reason: this is the last point where the mistake is still
    recoverable."""
    graphs = [_named("alpha")]
    invented = (MemberAuthoringInputV1(
        member_name="alpha", selection_revision_id="sel-1",
        authoring_run_id="far-nothing-happened", formula_content_hash="sha256:whatever"),)

    with pytest.raises(MemberProvenanceRefused):
        _seal(db, graphs, invented)

    assert db.execute("SELECT count(*) FROM sealed_artifact_v2").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM generated_artifact_blob").fetchone()[0] == 0
    assert db.execute(
        "SELECT count(*) FROM sealed_artifact_member_provenance").fetchone()[0] == 0


def test_ONE_UNDECIDABLE_MEMBER_REFUSES_THE_OTHERS_TOO(db):
    """All-or-nothing, the rule the rest of the chain uses. Sealing the members that COULD be
    established and skipping the one that could not would publish a group whose provenance is
    partial — and partial reads as complete to anyone who only checks the rows that exist."""
    graphs = [_named("alpha"), _named("beta")]
    good = evidenced_members(db, "alpha", run_prefix="far-mixed")
    mixed = (*good, MemberAuthoringInputV1(
        member_name="beta", selection_revision_id="sel-2",
        authoring_run_id="far-nothing-happened", formula_content_hash="sha256:whatever"))

    with pytest.raises(MemberProvenanceRefused, match="'beta'"):
        _seal(db, graphs, mixed)

    assert db.execute(
        "SELECT count(*) FROM sealed_artifact_member_provenance").fetchone()[0] == 0


@pytest.mark.parametrize("field,blank", [
    ("selection_revision_id", "  "),
    ("authoring_run_id", ""),
    ("formula_content_hash", " "),
])
def test_a_BLANK_FACT_IS_REFUSED_rather_than_recorded(db, field, blank):
    """Each of these answers a different question — whose choice, which authoring act, which bytes
    — and a blank makes its question unanswerable while leaving a row that looks recorded."""
    import dataclasses

    graphs = [_named("alpha")]
    good = evidenced_members(db, "alpha", run_prefix="far-blank")[0]
    blanked = (dataclasses.replace(good, **{field: blank}),)

    with pytest.raises(MemberProvenanceRefused, match=f"blank {field}"):
        _seal(db, graphs, blanked)


# ══ 4. APPEND-ONLY, AND ONE ANSWER PER MEMBER ══════════════════════════════════════════════════
def test_a_PROVENANCE_ROW_CANNOT_BE_EDITED(db):
    """This is the record of how a published number was produced. A row that could be edited after
    the fact would let an artifact acquire a more convenient authoring method than the one it had."""
    graphs, provenance = _two_members(db)
    _seal(db, graphs, provenance)

    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        db.execute(
            "UPDATE sealed_artifact_member_provenance SET authoring_method = %s "
            "WHERE artifact_id = 'art-prov' AND member_name = 'alpha'", (REVIEWED_RECIPE_BLUEPRINT,))


def test_a_PROVENANCE_ROW_CANNOT_BE_DELETED(db):
    """Deleting one is how a member would come to have no answer at all — the state the coverage
    check refuses at write time."""
    graphs, provenance = _two_members(db)
    _seal(db, graphs, provenance)

    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        db.execute("DELETE FROM sealed_artifact_member_provenance WHERE artifact_id = 'art-prov'")


def test_SEALING_THE_SAME_ARTIFACT_TWICE_IS_IDEMPOTENT(db):
    """A redelivered job must seal the same artifact rather than failing on the second insert."""
    graphs, provenance = _two_members(db)
    _seal(db, graphs, provenance)
    _seal(db, graphs, provenance)

    assert len(member_provenance_of(db, "art-prov")) == 2


def test_RESEALING_A_MEMBER_WITH_A_DIFFERENT_RUN_IS_REFUSED(db):
    """▲ `ON CONFLICT DO NOTHING` makes re-sealing safe and would make MIS-sealing silent: the
    second call's run would be ignored and this function would return as though it had recorded it.
    The read-back is what turns that into a disagreement somebody finds out about — and the row
    cannot be edited to resolve it, which is the point."""
    import dataclasses

    graphs, provenance = _two_members(db)
    _seal(db, graphs, provenance)

    other_run = reviewed_blueprint_run(db, "far-prov-other", window=45)
    moved = (dataclasses.replace(provenance[0], authoring_run_id=other_run,
                                 formula_content_hash=proposal_hash_of(db, other_run)),
             provenance[1])

    with pytest.raises(MemberProvenanceRefused, match="already records member"):
        _seal(db, graphs, moved)


def test_the_MIGRATION_REFUSES_AN_LLM_ROW_WITH_NO_RUN(db):
    """The constraint the writer leans on. An LLM-authored member whose run is unnamed cannot have
    its method re-derived, and an unverifiable claim is what this table exists to stop."""
    graphs, provenance = _two_members(db)
    _seal(db, graphs, provenance)

    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute(
            "INSERT INTO sealed_artifact_member_provenance (artifact_id, member_name, "
            "selection_revision_id, authoring_run_id, formula_content_hash, authoring_method, "
            "authoring_evidence_hash) VALUES ('art-prov','gamma','sel-x',NULL,'sha256:f',"
            "'LLM_AUTHORED', %s)", ("0" * 64,))
