"""The Release-A gate bars, one named test each.

Every bar runs against the REAL code path it is a bar on, over the checked-in gold sets, with no
provider and no network. A bar that could pass by doing nothing carries its own positive control in
the same test — a "zero violations" number is worthless if the machine produced zero of everything.

Run it:

    uv run --extra dev pytest -q -m eval tests/eval/test_release_a_bars.py
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from tests.eval import gold_catalog_profiles as pg
from tests.eval import gold_semantic_context as sg
from tests.eval import release_a_harness as h

from featuregen.intake.llm import FakeLLM, FakeResponse
from featuregen.overlay.upload import semantic_context as sc

pytestmark = pytest.mark.eval

NOW = datetime(2026, 8, 3, tzinfo=UTC)
ROLES = ("platform_admin", "pii_reader", "restricted_reader")
_CONTEXT_REVISION = "release_a_bars_v1"


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    for var in ("ANTHROPIC_API_KEY", "FEATUREGEN_LLM_PROVIDER"):
        monkeypatch.delenv(var, raising=False)
    h.LIVE_PROVIDER_BUILDS.clear()
    with h.no_network():
        yield
    assert h.LIVE_PROVIDER_BUILDS == [], "a bar built a live provider"


# ── bar 1 ────────────────────────────────────────────────────────────────────────────────────────


def test_bar_zero_bic_cif_candidates_on_the_gold_set(db) -> None:
    """No identifier-link candidate may ever pair two disjoint namespaces.

    Driven through the REAL `derive_bridge_candidates`, whose blocking key IS the namespace, over a
    catalog seeded with the reviewer-expected concepts. The positive control is asserted in the same
    test: a derivation that produced nothing at all would satisfy the zero trivially, and did
    exactly that the first time this harness ran.
    """
    h.seed_gold_catalog(db, now=NOW)
    result = h.cross_namespace_candidates(db)

    assert result["false_cross_namespace_candidates"] == 0, result["violations"]
    assert result["positive_controls_offered"], (
        "the same-namespace control produced no candidate either — the zero above is vacuous, "
        "not a pass")


# ── bar 2 ────────────────────────────────────────────────────────────────────────────────────────


def test_bar_zero_physical_facts_attributed_to_an_llm_producer(db) -> None:
    """Release A observes no physical data, so nothing it writes may claim to have.

    Two halves. STRUCTURAL: the fields Pass B is allowed to propose are disjoint from every physical
    assertion the gold names, so an LLM cannot reach one even in principle. EMPIRICAL: after
    recording every gold synthesis, no stored output carries a physical key.
    """
    from featuregen.overlay.upload.table_synth import PROFILE_DISPOSITION_FIELDS

    physical = set(pg.unsafe_physical_assertions())
    assert physical, "an empty forbidden set would make this bar vacuous"
    assert set(PROFILE_DISPOSITION_FIELDS) & physical == set(), (
        f"Pass B may propose a physical fact: {set(PROFILE_DISPOSITION_FIELDS) & physical}")

    h.record_passb_gold(db, context_revision=_CONTEXT_REVISION)
    replay = h.replay_passb(db, context_revision=_CONTEXT_REVISION)
    assert replay["replayed"], "nothing was recorded, so nothing was checked"

    for dataset in pg.datasets():
        if dataset.expected_synthesis is None:
            continue
        leaked = physical & set(dataset.expected_synthesis)
        assert leaked == set(), f"{dataset.table} synthesis asserts a physical fact: {leaked}"

    rows = db.execute(
        "SELECT field_name, count(*) FROM field_evidence "
        "WHERE producer = 'llm' AND field_name = ANY(%s) GROUP BY field_name",
        (sorted(physical),)).fetchall()
    assert rows == [], f"LLM-produced physical facts found: {rows}"


# ── bar 3 ────────────────────────────────────────────────────────────────────────────────────────


def test_bar_zero_source_or_human_evidence_overwritten_by_a_full_re_enrichment(db) -> None:
    """A full re-enrichment replay must leave every source and human assertion byte-identical.

    This is the supersession contract stated as an outcome rather than a mechanism: LLM retirement
    is producer-scoped (`stale_source_evidence` never touches another producer's rows), so a
    re-enrichment may retire its OWN prior proposals and nothing else. Asserted by fingerprinting
    every non-LLM evidence row before and after two complete runs.
    """
    from featuregen.overlay.field_evidence import record_field_evidence

    rows = h.gold_rows()
    glossary = h.gold_glossary()
    anchor = sg.case_by_column("counter_party_bic")
    ref = f"{anchor.catalog_source}::public.{anchor.table}.{anchor.column}"

    record_field_evidence(
        db, logical_ref=ref, field_name="concept", proposed_value="bank_bic",
        producer="source", strength="attested", input_hash="gold-source-1",
        source_snapshot_id="gold-snap", producer_ref="release_a_bars")
    record_field_evidence(
        db, logical_ref=ref, field_name="definition",
        proposed_value="A human-confirmed definition that no re-enrichment may touch.",
        producer="human", strength="confirmed", input_hash="gold-human-1",
        source_snapshot_id="gold-snap", producer_ref="release_a_bars")

    def _fingerprint() -> list[tuple]:
        return db.execute(
            "SELECT evidence_id, logical_ref, field_name, producer, strength, lifecycle, "
            "proposed_value_hash FROM field_evidence WHERE producer IN ('source','human') "
            "ORDER BY evidence_id").fetchall()

    before = _fingerprint()
    assert before, "no source/human evidence was written, so nothing was protected"

    for _pass in range(2):
        h.reset_concept_cache(db)
        provider = h.build_concept_provider(h.FIXTURE, cases_by_ref=h.cases_by_content_hash())
        from featuregen.overlay.upload.enrich import build_enrichment_context, enrich_concepts
        enrich_concepts(db, rows, provider, glossary=glossary,
                        bundles=build_enrichment_context(db, rows, glossary),
                        source_snapshot_id="gold-snap")

    assert _fingerprint() == before, "a re-enrichment mutated source or human evidence"


# ── bar 4 ────────────────────────────────────────────────────────────────────────────────────────


def _propose_gold_features(db):
    """Put every gold feature in front of the REAL generator and return what it accepted."""
    from featuregen.overlay.upload.feature_assist import recommend_features_report

    h.seed_gold_catalog(db, now=NOW)
    proposals = [{"name": c["name"],
                  "derives_from": [_flat(ref) for ref in c["derives_from"]],
                  "aggregation": "latest"}
                 for c in sg.unsafe_feature_cases()]
    client = FakeLLM(script={
        "overlay.feature.recommend": FakeResponse(output={"features": proposals})})
    return recommend_features_report(
        db, "predict which transactions will be reported", client,
        catalog_source=sg.load()["catalog_source"],
        target_ref=_flat("ftr.comp_fin_tran.sar_filed_flag"),
        roles=ROLES, now=NOW, budget=3, critic=False)


def test_bar_zero_unsafe_gold_features_accepted_leakage_class(db) -> None:
    """The half of the bar the as-built platform DOES meet: a target-leakage feature is refused.

    Driven through the REAL `recommend_features_report` with the leakage anchor named as the target,
    so the platform's own gate decides. The safe control must be accepted, or "refused everything"
    would read as a pass.
    """
    report = _propose_gold_features(db)
    accepted = {idea.name for idea in report.ideas}
    leaky = {c["name"] for c in sg.unsafe_feature_cases()
             if c["unsafe"] and c["id"] == "leak_sar_outcome"}

    assert accepted, f"nothing was accepted at all; rejections={report.rejections}"
    assert accepted & leaky == set(), f"a target-leakage feature was accepted: {accepted & leaky}"
    assert any(r["code"] == "LEAKAGE" for r in report.rejections), report.rejections

    # No accepted idea may be DESIGN_CHECKED while carrying an unresolved requirement — the same
    # hard-safety definition `feature_eval.unsafe_accepted` has always used.
    from tests.eval.feature_eval import GenFeature, unsafe_accepted
    gens = [GenFeature(name=i.name, derives_from=tuple(i.derives_from),
                       operation_kind=i.operation_kind, validation_status=i.validation_status,
                       requirement_count=len(i.requirements)) for i in report.ideas]
    assert unsafe_accepted(gens) == [], [g.name for g in unsafe_accepted(gens)]


@pytest.mark.xfail(strict=True, reason=(
    "RELEASE-A FINDING, not a flaky test. Measured 2026-08-03: of the five unsafe gold classes the "
    "platform refuses exactly ONE — target leakage. It accepts as DESIGN_CHECKED with zero "
    "requirements: a PII column (cust_dob), a protected characteristic (cust_ctzn_ctry_cd), a "
    "currency-blind sum of a monetary flow whose currency column sits on the same table, and a "
    "free-text description used as a join key. Sensitivity gates VISIBILITY (read scope worked "
    "correctly — the columns were visible because the caller held pii_reader/restricted_reader) "
    "and does not gate USE. No Release-A task owns that gate, so this is recorded for the Gate-A "
    "approver rather than silently weakened. strict=True: the day the gate lands, THIS test fails "
    "and someone must promote it to a real bar."))
def test_bar_zero_unsafe_gold_features_accepted(db) -> None:
    """The full bar as the plans state it: no unsafe gold feature is accepted."""
    report = _propose_gold_features(db)
    accepted = {idea.name for idea in report.ideas}
    unsafe_names = {c["name"] for c in sg.unsafe_feature_cases() if c["unsafe"]}
    safe_names = {c["name"] for c in sg.unsafe_feature_cases() if not c["unsafe"]}

    assert accepted & safe_names, (
        f"no safe control was accepted (accepted={sorted(accepted)}, "
        f"rejections={report.rejections}) — a zero below would be vacuous")
    assert accepted & unsafe_names == set(), (
        f"unsafe gold features accepted: {sorted(accepted & unsafe_names)}")


def _flat(ref: str) -> str:
    """`ftr.comp_fin_tran.sar_filed_flag` -> `public.comp_fin_tran.sar_filed_flag` (the schema-
    flattened form the feature menu and the graph both speak)."""
    return "public." + ref.split(".", 1)[1]


# ── bar 5 ────────────────────────────────────────────────────────────────────────────────────────


def test_bar_no_regression_in_grounded_acceptance_against_the_thin_baseline(db) -> None:
    """Rich context may not accept FEWER correct concepts than thin, nor more wrong ones."""
    thin = h.run_concepts(db, context=h.THIN)
    rich = h.run_concepts(db, context=h.RICH)
    families = h.family_accuracy(thin, rich)

    thin_hits = sum(f.thin_hits for f in families)
    rich_hits = sum(f.rich_hits for f in families)
    thin_bad = sum(f.thin_forbidden for f in families)
    rich_bad = sum(f.rich_forbidden for f in families)

    assert rich_hits >= thin_hits, f"grounded acceptance regressed: {rich_hits} < {thin_hits}"
    assert rich_bad <= thin_bad, f"forbidden selections regressed: {rich_bad} > {thin_bad}"
    for f in families:
        assert f.rich_hits >= f.thin_hits, f"{f.family} regressed: {f.rich_hits} < {f.thin_hits}"
    assert thin_hits < rich_hits, (
        "thin and rich scored identically — the comparison is measuring a constant, which is what "
        "the concept cache made it do before `reset_concept_cache` existed")


def test_bar_unclassified_precision_does_not_regress(db) -> None:
    """Declining honestly is part of grounded acceptance: rich must not decline MORE loosely."""
    thin = h.unclassified_precision(h.run_concepts(db, context=h.THIN))
    rich = h.unclassified_precision(h.run_concepts(db, context=h.RICH))
    assert rich["precision"] >= thin["precision"]
    assert rich["recall"] >= thin["recall"], "rich stopped declining columns it should decline"


# ── bar 6 ────────────────────────────────────────────────────────────────────────────────────────


def test_bar_profile_context_lifts_retrieval(db, monkeypatch) -> None:
    """Profile context must measurably ADD to retrieval, and take nothing away.

    The direction is asserted on leg 3's own contribution, not on the grounded hit rate. On a gold
    set of two tables and ~26 columns the lexical leg already reaches every expected column in both
    modes (8/8, unchanged at max_columns 60/6/4/3), so a hit-rate lift has nowhere to come from and
    asserting one would be asserting an artifact of the fixture size. What the flag genuinely
    changes is the controlled expansion, which harvests the TABLE profile projections only when it
    is on — so that is where the direction is checked, and the hit rate is checked for
    NON-REGRESSION beside it. Live retrieval lift against a real index is Gate B.
    """
    h.seed_gold_catalog(db, now=NOW)

    monkeypatch.delenv("FEATUREGEN_DATASET_PROFILES", raising=False)
    thin = h.retrieval_hits(db, now=NOW, profiles_on=False)
    monkeypatch.setenv("FEATUREGEN_DATASET_PROFILES", "1")
    rich = h.retrieval_hits(db, now=NOW, profiles_on=True)

    assert rich["grounded_hits"] >= thin["grounded_hits"], "grounded retrieval regressed"
    assert rich["expansion_terms"] > thin["expansion_terms"], (
        f"the profile harvest added no expansion terms "
        f"({rich['expansion_terms']} vs {thin['expansion_terms']}) — profile context is not "
        f"reaching leg 3")
    assert rich["leg3_offered"] > thin["leg3_offered"], (
        f"the semantic-expansion leg offered no more with profiles on "
        f"({rich['leg3_offered']} vs {thin['leg3_offered']})")


def test_bar_profile_context_lifts_table_selection(db, monkeypatch) -> None:
    """The profile classifications must make the wrong table distinguishable from the right one."""
    h.seed_gold_catalog(db, now=NOW)
    source = sg.load()["catalog_source"]

    monkeypatch.delenv("FEATUREGEN_DATASET_PROFILES", raising=False)
    thin = h.table_selection_quality(profiles_on=False,
                                     visible=h.visible_table_fields(db, source=source))
    monkeypatch.setenv("FEATUREGEN_DATASET_PROFILES", "1")
    rich = h.table_selection_quality(profiles_on=True,
                                     visible=h.visible_table_fields(db, source=source))

    assert rich["resolved"] > thin["resolved"], (
        f"profiles resolved no additional selection question "
        f"({rich['resolved']} vs {thin['resolved']})")
    assert rich["resolved"] == rich["questions"], rich["per_question"]


# ── bar 7 ────────────────────────────────────────────────────────────────────────────────────────


def unexplained_zero(*, considered: int, produced: int, denials: dict) -> bool:
    """The guard `ingest.py` applies to the semantic-binding proposal stage, as a predicate.

    Its shape is the point: a stage that CONSIDERED work, produced nothing, and cannot say why is
    the one failure mode a stage report cannot launder. An explained zero (non-empty denials) is a
    truthful success.
    """
    return considered > 0 and produced == 0 and not denials


def test_bar_no_unexplained_zero_output_stage(db, monkeypatch) -> None:
    """Every stage this evaluation drives either produces output or explains its zero."""
    h.seed_gold_catalog(db, now=NOW)

    rich = h.run_concepts(db, context=h.RICH)
    classified = sum(1 for c in sg.cases() if rich.answer_for(c) != sg.UNCLASSIFIED)
    stages = {
        "enrich_concept": (len(sg.cases()), classified, {}),
        "bridge_candidates": (len(h.gold_rows()),
                              h.cross_namespace_candidates(db)["candidates_derived"], {}),
        "passb_replay": (len(pg.datasets()),
                         len(h.replay_passb(db, context_revision=_CONTEXT_REVISION)["replayed"]),
                         {"no_synthesis_for_unknown_tables": 1}),
    }
    monkeypatch.setenv("FEATUREGEN_DATASET_PROFILES", "1")
    retrieval = h.retrieval_hits(db, now=NOW, profiles_on=True)
    stages["retrieval_leg3"] = (retrieval["leg3_considered"], retrieval["leg3_offered"], {})

    unexplained = [name for name, (considered, produced, denials) in stages.items()
                   if unexplained_zero(considered=considered, produced=produced, denials=denials)]
    assert unexplained == [], f"stages produced nothing and did not say why: {unexplained}"

    # The predicate must be capable of firing, or the assertion above says nothing.
    assert unexplained_zero(considered=5, produced=0, denials={})
    assert not unexplained_zero(considered=5, produced=0, denials={"refused": 5})
    assert not unexplained_zero(considered=0, produced=0, denials={})


# ── bar 8 ────────────────────────────────────────────────────────────────────────────────────────


def test_bar_zero_reviewed_but_unsafe_relationships_displayed_executable(db) -> None:
    """A review badge is never a permission.

    The link below is APPROVED and its stored realization claims `production_eligible` — the exact
    shape a caller might render as "safe to run". `executable_now` is asked of the REVALIDATING
    reader and of nothing else, and that reader knows nothing about this realization, so both the
    link-level and per-realization answers must be False while the stored `production_eligible`
    stays visibly True beside them. Two fields, two different questions, and the payload contradicts
    anyone who conflates them.
    """
    from featuregen.overlay.upload.context_graph import _relationship_dict

    realization = sc.DirectionalRealizationContextV1(
        realization_revision_id="brr_gold_reviewed_but_unvalidated",
        from_ref="ftr::comp_fin_tran.counter_party_cif_id",
        to_ref="cib::cust_master.cust_cif_id",
        lifecycle="active",
        safety_status="deterministically_validated",
        cardinality="N:1",
        scope_id="scope_gold",
        sandbox_eligible=True,
        production_eligible=True,
    )
    link = sc.RelationshipContextV1(
        relationship_ref="bridge_gold_reviewed",
        kind=sc.RelationshipKind.DIRECT_EQUALITY,
        left_ref="ftr::comp_fin_tran.counter_party_cif_id",
        right_ref="cib::cust_master.cust_cif_id",
        availability="available",
        review_status="approved",
        assessment_revision_id="bca_gold",
        realizations=(realization,),
        producer="human", strength="confirmed", lifecycle="active",
        current=True, evidence_ids=(),
    )

    payload = _relationship_dict(db, link)

    assert payload["review_status"] == "approved"
    assert payload["realizations"][0]["production_eligible"] is True
    assert payload["executable_now"] is False, "an approved review was displayed as executable"
    assert payload["realizations"][0]["executable_now"] is False


def test_bar_a_revalidation_fault_never_reads_as_executable(db, monkeypatch) -> None:
    """The fail-closed half: a reader that RAISES must degrade to 'nothing is executable'."""
    import featuregen.overlay.upload.bridge_store as bridge_store
    from featuregen.overlay.upload.context_graph import _executable_realization_ids

    def _boom(*_args, **_kwargs):
        raise RuntimeError("revalidation exploded")

    monkeypatch.setattr(bridge_store, "executable_bridge_realizations", _boom)
    assert _executable_realization_ids(db, "bridge_gold_reviewed") == frozenset()
    # and the transaction is still usable — the savepoint scoped the rollback
    assert db.execute("SELECT 1").fetchone()[0] == 1
