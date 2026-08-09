"""The feature-context byte budget, MEASURED against the real catalog shapes (semantic Task 8).

The review's finding: `FEATURE_CONTEXT_BYTE_BUDGET = 60_000` with `ContextTooLarge` raised when the
MANDATORY set alone exceeds it turns a 111/126-column catalog into a whole-request reject. Adding
v4's richer per-column payload on top of that would have made a live cliff steeper.

So this file measures rather than asserts a guess:

* the 126-column table is the committed synthetic FTR export routed through the REAL reader
  (`synthetic_ftr_upload`) — real prose, real declared types, real sample-stripped definitions;
* the 111-column table is a CIB-shaped technical upload with descriptions of comparable length;
* every column is made entity-matched so the whole 237-column catalog is MANDATORY — the worst
  realistic case, and the one that used to refuse.

The properties pinned: neither version raises at the re-budgeted value; v4 costs more than v3 and
the file records BOTH numbers so a future change is measured against them; and when the budget is
genuinely too small the mandatory set is TRIMMED by the explicit policy, per-kind, before anything
is refused.
"""
from __future__ import annotations

import pytest
from tests.featuregen._helpers import mint_test_identity

from featuregen.intake.llm import FakeLLM, FakeResponse
from featuregen.overlay.field_evidence import field_input_hash, record_field_evidence
from featuregen.overlay.upload import feature_assist as fa
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.feature_assist import (
    FEATURE_CONTEXT_BYTE_BUDGET,
    ContextTooLarge,
    _assembled_bytes,
    _candidate_columns,
    _table_context,
    select_relevant_context,
)
from featuregen.overlay.upload.ingest import ingest_upload
from featuregen.overlay.upload.semantic_context import normalize_ref
from featuregen.overlay.upload.source_profile import FTR_GLOSSARY_PROFILE, strength_for

ACTOR = mint_test_identity(subject="user:owner", role_claims=("data_owner",))

#: A CIB-shaped description: the real export fills these from a bucket, so they are long, similar
#: and mostly useless for search — which is exactly why the summary/definition bytes are large.
_CIB_DESCRIPTION = ("Status or indicator used to classify the customer condition as recorded by "
                    "the core banking system at the time of the daily extract.")


@pytest.fixture
def wide_catalogs(db, synthetic_ftr_upload):
    """126 real FTR glossary columns + 111 CIB-shaped technical columns, all entity-matched."""
    ftr = synthetic_ftr_upload(db, source="budget_ftr")
    assert ftr.columns == 126

    rows = [
        CanonicalRow("budget_cib", "bo_cib_customer", f"cust_attr_{i:03d}", "text",
                     definition=_CIB_DESCRIPTION)
        for i in range(111)
    ]
    assert ingest_upload(db, "budget_cib", rows, actor=ACTOR).status == "ingested"

    # Make EVERY column entity-matched, so `_is_mandatory` admits all 237 — the worst realistic
    # case for the budget, and the shape the review said would refuse.
    db.execute("UPDATE graph_node SET entity = 'customer' WHERE kind = 'column' "
               "AND catalog_source IN ('budget_ftr', 'budget_cib')")
    return db


#: A realistic value for each field the readiness wave / branch added but the `wide_catalogs`
#: fixture leaves EMPTY. Sized like the real thing — an FTR taxonomy path, a screening sub-domain,
#: a shortlist of concept alternatives — because the point is to measure bytes, not to prove a key
#: exists.
_SATURATION = {
    "sub_domain": "Sanctions Screening and Adverse Media",
    "bian_path": "BIAN > Party Reference Data Directory > Party Routing Profile",
    "process_path": "Onboarding > KYC Refresh > Periodic Review",
    "fibo_path": "FIBO > FND > Relations > Relations > hasLegalName",
    "related_terms": "obligor exposure, credit exposure, counterparty exposure, net exposure",
    "table_role": "fact",
    "event_or_snapshot": "event",
}


@pytest.fixture
def saturated_catalogs(wide_catalogs):
    """`wide_catalogs` with EVERY field the branch added actually populated, through the same
    writers the real pipeline uses.

    WHY THIS FIXTURE EXISTS. `wide_catalogs` leaves 8 of the 11 added fields at exactly 0, so the
    pinned pair below measured a catalog thinner than any real one — and the gap was not theoretical:
    F7's `proposed_authority` subkey grew the payload and
    `test_the_floor_rose_by_exactly_what_the_payload_rose_by` passed COMPLETELY UNCHANGED, because
    the field it sits beside is empty here. A payload change went straight through the one test whose
    job is to notice payload changes.

    Writers, not fabrication: the projection columns take the same `UPDATE graph_node` that
    `resolve_and_project` performs, the source fields take `record_field_evidence` under the FTR
    glossary profile exactly as `ingest._write_glossary_source_evidence` does, and the LLM proposal
    takes an `llm/proposed` evidence row on `_MEASURE_ANNOTATION` fields — the one shape that
    produces `proposed_value` + `proposed_authority` by design.
    """
    conn = wide_catalogs
    conn.execute(
        "UPDATE graph_node SET sub_domain = %s, bian_path = %s, process_path = %s, fibo_path = %s "
        "WHERE kind = 'column' AND catalog_source IN ('budget_ftr', 'budget_cib')",
        (_SATURATION["sub_domain"], _SATURATION["bian_path"],
         _SATURATION["process_path"], _SATURATION["fibo_path"]))
    conn.execute(
        "UPDATE graph_node SET table_role = %s, event_or_snapshot = %s "
        "WHERE kind = 'table' AND catalog_source IN ('budget_ftr', 'budget_cib')",
        (_SATURATION["table_role"], _SATURATION["event_or_snapshot"]))

    # The SCHEMA-PRESERVING logical ref, NOT `graph_node.object_ref` (which is public-flattened).
    # `feature_assist` documents this exact trap: the adjudication lookup keyed on the flattened
    # form matched no row, so every column came back unadjudicated and "the feature would look
    # implemented". An evidence write keyed on the wrong form fails the same silent way — the
    # `related_terms` count in the saturation assertion below is what catches it.
    refs = [normalize_ref(src, schema or None, table, column) for src, schema, table, column
            in conn.execute(
                "SELECT catalog_source, schema_name, table_name, column_name FROM graph_node "
                "WHERE kind = 'column' AND catalog_source IN ('budget_ftr', 'budget_cib') "
                "ORDER BY object_ref").fetchall()]
    for ref in refs:
        # SOURCE curated vocabulary — the glossary reader's own field and profile.
        record_field_evidence(
            conn, logical_ref=ref, field_name="related_terms",
            proposed_value=_SATURATION["related_terms"],
            producer="source", strength=strength_for(FTR_GLOSSARY_PROFILE, "related_terms"),
            producer_ref="budget-saturation", source_snapshot_id="snap",
            input_hash=field_input_hash(logical_ref=ref, field_name="related_terms",
                                        material=f"{_SATURATION['related_terms']}:source"))
        # The LLM's answer on a field its policy EXCLUDES it from winning (`_MEASURE_ANNOTATION`),
        # which is exactly the state that emits `proposed_value` + `proposed_authority`.
        for field_name, value in (("unit", "currency"), ("currency", "AED")):
            record_field_evidence(
                conn, logical_ref=ref, field_name=field_name, proposed_value=value,
                producer="llm", strength="proposed", producer_ref="budget-saturation",
                source_snapshot_id="snap",
                input_hash=field_input_hash(logical_ref=ref, field_name=field_name,
                                            material=f"{value}:llm"))

    # Adjudication — `confidence_band` + `concept_alternatives` come from the 1046 current pointer,
    # so they need the REAL adjudication writer, not a hand-written row. One scripted answer drives
    # all 237: the shortlist length is what costs bytes, and a realistic shortlist is short.
    from featuregen.overlay.upload import semantic_adjudication as adj
    # The concept/shape pair must SURVIVE `shape_conflicts`, which re-runs on read: a `branch_id`
    # verdict on a "Branch description" column is the deterministic conflict the critic exists to
    # refute, and adjudications that fail re-validation land on ~5% of columns instead of all of
    # them — which would understate the bytes exactly the way this fixture exists to stop.
    answer = {"selected_concept": "customer_id",
              "alternatives": ["counterparty_id", "bank_bic", "account_id"],
              "confidence_band": "medium",
              "reason_codes": ["name_only_signal"],
              "missing_context": ["definition_missing"]}
    client = FakeLLM(script={adj.SEMANTIC_ADJUDICATION_TASK: FakeResponse(output=answer)})
    targets = [
        adj.AdjudicationTargetV1(
            candidate=adj.AdjudicationCandidateV1(
                logical_ref=ref, column_name=ref.rsplit(".", 1)[-1],
                declared_type="varchar(20)",
                definition="Customer master identifier for the party.", concept=None),
            selection_reasons=("unclassified_column",), context=None)
        for ref in refs
    ]
    adj.adjudicate_targets(conn, client, targets, catalog_revision="budget-rev", actor=ACTOR)
    return conn


def _candidates(conn):
    return (_candidate_columns(conn, "budget_ftr", roles=())
            + _candidate_columns(conn, "budget_cib", roles=()))


def _mandatory_bytes(conn, version: int, monkeypatch) -> int:
    monkeypatch.setenv("FEATUREGEN_FEATURE_CONTEXT", "1")
    monkeypatch.setenv(fa.FEATURE_CONTEXT_VERSION_ENV, str(version))
    cols = _candidates(conn)
    assert len(cols) == 237
    columns = [fa._context_column(conn, c, roles=()) for c in cols]
    return _assembled_bytes(columns, _table_context(cols))


def test_measured_mandatory_bytes_for_v3_and_v4(wide_catalogs, monkeypatch, record_property):
    """The numbers the budget is set from. Recorded, not merely asserted, so the next person who
    changes the payload can see what it used to cost."""
    v3 = _mandatory_bytes(wide_catalogs, 3, monkeypatch)
    v4 = _mandatory_bytes(wide_catalogs, 4, monkeypatch)
    record_property("mandatory_bytes_v3", v3)
    record_property("mandatory_bytes_v4", v4)

    # The pre-existing cliff: the SHIPPED v3 payload already blew the old 60_000 budget on these
    # very catalogs — by nearly 3x. v4 did not create the problem; it would have deepened it.
    assert v3 > 60_000
    assert v4 > v3, "v4 carries strictly more context than v3"
    # The measured values the budget was set from, pinned with tolerance so a payload change that
    # moves them materially has to come back here and re-argue the budget.
    #
    # THE BAND IS NO LONGER "+/-15% OF THE CURRENT MEASUREMENT", and saying so would be the same
    # rot this comment was written to prevent. It was centred on v4=241_491 in 2026-08-06; the
    # 2026-08-07 measurement is 250_982, so the live band is now -18%/+11% around it. Deliberately
    # NOT re-centred: re-centring on every measurement turns the guard into a ratchet that always
    # passes. It is re-argued when a change actually threatens the budget, and the numbers below
    # record what each measurement was.
    #
    # RE-MEASURED 2026-08-06 at the zero-truncation caps: v3 175_520, v4 241_491. The old v4 band
    # (215_000..285_000) was centred on 248_601, a figure the payload had already drifted away from
    # while staying inside the tolerance — which is how a "measured" number stops being one. Both
    # bands are now +/-15% of a freshly measured value.
    #
    # RE-MEASURED 2026-08-07 (Task 6, the D13.1/D13.2 axes in the payload): v3 175_520 (unmoved —
    # v3 has no bundle keys), v4 250_982 (+3.9%). The move is real and is the 126 FTR columns:
    # `bian_path`/`process_path` come from the glossary export, so those columns now carry two real
    # taxonomy paths each (~+75 bytes/column there, ~+40 averaged over all 237). Recorded here
    # DELIBERATELY rather than left to drift inside the band — that is the failure the paragraph
    # above describes. The band itself is unchanged: +3.9% does not warrant re-arguing the budget.
    #
    # RE-MEASURED 2026-08-07 (Task 7b, the glossary's curated vocabulary in the payload): v3 175_520
    # (unmoved again), v4 259_405 (+3.4% on 250_982). The move is `business_term` + `related_terms`
    # on the 126 FTR glossary columns, plus their two `semantic_authority` entries — ~36
    # bytes/column averaged over all 237. The catalog
    # NARRATIVE is not in this figure by design: it rides the per-TABLE block, and its own cost is
    # measured separately in `test_measured_cost_of_the_catalog_narrative` below. The band is
    # unchanged; 259_405 sits mid-band and ~5.8x under the budget.
    assert 149_000 < v3 < 202_000, f"v3 mandatory bytes moved: {v3}"
    assert 205_000 < v4 < 278_000, f"v4 mandatory bytes moved: {v4}"
    # …and the re-budgeted value clears the worst realistic case with headroom.
    assert v4 < FEATURE_CONTEXT_BYTE_BUDGET


def test_the_raised_caps_leave_headroom_on_the_worst_realistic_catalog(wide_catalogs, monkeypatch):
    """237 mandatory columns at the zero-truncation caps must still clear the budget with room.

    Headroom is the point: the budget is not a target to fill. If this fails, the caps grew faster
    than the budget and one of the two is wrong — decide which, do not just raise the budget."""
    v4 = _mandatory_bytes(wide_catalogs, 4, monkeypatch)
    assert v4 < FEATURE_CONTEXT_BYTE_BUDGET * 0.6, (
        f"{v4} bytes leaves under 40% headroom against {FEATURE_CONTEXT_BYTE_BUDGET}")


def test_the_budget_is_what_decides_how_wide_a_catalog_can_be_served_untrimmed(wide_catalogs,
                                                                               monkeypatch):
    """WHY the budget was raised, given that the cap raise did not move the bytes above.

    The assembled `definition` comes straight from `graph_node`, so `MAX_DEFINITION_LEN` never
    touches this path — the truncation risk here is not per-VALUE length, it is CATALOG WIDTH: past
    roughly `budget / bytes-per-column` mandatory columns, `_V4_TRIM_ORDER` starts shedding prose
    (definition first) and the model stops seeing what the platform knows. That shed is silent, and
    it is the same defect the cap raise exists to remove, arriving through a different door.

    Pinned as a RATIO so it survives a payload that gets fatter or leaner per column."""
    v4 = _mandatory_bytes(wide_catalogs, 4, monkeypatch)
    bytes_per_column = v4 / 237
    columns_servable_untrimmed = FEATURE_CONTEXT_BYTE_BUDGET / bytes_per_column
    # The 200-column-per-table ingest cap (canonical.MAX_COLUMNS_PER_TABLE) means a catalog reaches
    # this width across several tables; the budget is per feature-generation call, not per table.
    assert columns_servable_untrimmed > 1_000, (
        f"only {columns_servable_untrimmed:.0f} mandatory columns fit untrimmed at "
        f"{bytes_per_column:.0f} bytes/column — prose starts being shed below a realistic catalog")


@pytest.mark.parametrize("version", [3, 4])
def test_no_context_too_large_on_the_real_catalog_shapes(wide_catalogs, monkeypatch, version):
    """The plan's own acceptance: a 111/126-column table must not become a whole-request reject."""
    monkeypatch.setenv("FEATUREGEN_FEATURE_CONTEXT", "1")
    monkeypatch.setenv(fa.FEATURE_CONTEXT_VERSION_ENV, str(version))
    columns, table_context, dropped = select_relevant_context(
        wide_catalogs, _candidates(wide_catalogs), objective="customer balance trend",
        entity="customer")
    assert len(columns) == 237       # every mandatory column survives
    assert dropped == 0
    assert len(table_context) == 2   # one block per table, never per column


def test_an_over_budget_mandatory_set_is_trimmed_before_it_is_refused(wide_catalogs, monkeypatch):
    """The explicit trim policy. Prose is shed first; the mandatory columns THEMSELVES are never
    dropped, because a missing grain or time column produces a confidently wrong feature rather
    than a smaller one."""
    monkeypatch.setenv("FEATUREGEN_FEATURE_CONTEXT", "1")
    monkeypatch.setenv(fa.FEATURE_CONTEXT_VERSION_ENV, "4")
    # Between the measured fully-trimmed floor and the untrimmed cost: the only way to serve every
    # mandatory column here is to shed prose.
    #
    # RE-MEASURED 2026-08-07 (Task 7b): floor 206_010 -> 214_433, untrimmed 250_982 -> 259_405, so
    # the old 210_000 pin had fallen BELOW the floor and this test refused instead of trimming.
    #
    # WHY THE FLOOR ROSE, corrected — the first version of this note claimed the new fields "shed
    # with the prose ladder's tail" and that was simply false. `_V4_TRIM_ORDER` is unchanged, so
    # NOTHING Task 7b added is trimmable: `business_term`, `related_terms` and their two
    # `semantic_authority` entries all survive a full trim. The floor therefore rose by exactly what
    # the payload rose by, +8_423 — pinned as an equality by
    # `test_the_floor_rose_by_exactly_what_the_payload_rose_by`, which also reconstructs the BEFORE
    # numbers in-process so neither side can go stale again.
    #
    # It is a 4.1% rise in the FLOOR, not in the budget: `FEATURE_CONTEXT_BYTE_BUDGET` is 1_500_000,
    # ~7x this figure, so no real catalog trims at all. Re-pinned to a MEASURED midpoint.
    columns, _ctx, dropped = select_relevant_context(
        wide_catalogs, _candidates(wide_catalogs), objective="customer balance", entity="customer",
        byte_budget=235_000)
    assert len(columns) == 237 and dropped == 0
    # Prose is what went — `definition` is the biggest single field on these catalogs.
    assert all("definition" not in c for c in columns)
    assert all("semantic_terms" not in c and "ai_summary" not in c for c in columns)
    # …and never the fields that keep an AI proposal legible as a proposal, nor the identity the
    # model must name back.
    assert all("semantic_authority" in c for c in columns)
    # The identity the model must name back, and the honest absence codes, are NOT trimmable.
    assert all("object_ref" in c for c in columns)
    assert all("missing_context" in c for c in columns)
    assert not (set(fa._V4_TRIM_ORDER) & {"missing_context", "object_ref", "semantic_authority"})


def test_refusal_survives_only_when_even_the_fully_trimmed_set_does_not_fit(wide_catalogs,
                                                                           monkeypatch):
    """`ContextTooLarge` is not deleted — it is demoted to the last resort, and its message names
    what was already shed so the refusal is actionable."""
    monkeypatch.setenv("FEATUREGEN_FEATURE_CONTEXT", "1")
    monkeypatch.setenv(fa.FEATURE_CONTEXT_VERSION_ENV, "4")
    with pytest.raises(ContextTooLarge) as exc:
        select_relevant_context(
            wide_catalogs, _candidates(wide_catalogs), objective="x", entity="customer",
            byte_budget=1_000)
    assert "every trimmable field removed" in str(exc.value)
    for field in fa._V4_TRIM_ORDER:
        assert field in str(exc.value)


def test_enrichment_is_lazy_so_a_dropped_column_is_never_assembled(wide_catalogs, monkeypatch):
    """The 157-scan defect class. Enrichment used to run for EVERY candidate before scoring, and
    the budget then threw most of it away; at v4 that would have been a semantic bundle per column
    of the whole catalog. Assembly must be bounded by what FITS, not by catalog size."""
    monkeypatch.setenv("FEATUREGEN_FEATURE_CONTEXT", "1")
    monkeypatch.setenv(fa.FEATURE_CONTEXT_VERSION_ENV, "4")
    built: list[str] = []
    real = fa._context_v4_column

    def _counting(conn, c, *, roles):
        built.append(c["object_ref"])
        return real(conn, c, roles=roles)

    monkeypatch.setattr(fa, "_context_v4_column", _counting)
    cols = _candidates(wide_catalogs)
    # No column is entity-matched for THIS objective, so the mandatory set is tiny and a small
    # budget admits only a handful — everything else must never be assembled at all.
    wide_catalogs.execute("UPDATE graph_node SET entity = NULL WHERE kind = 'column'")
    cols = _candidates(wide_catalogs)
    selected, _ctx, dropped = select_relevant_context(
        wide_catalogs, cols, objective="customer balance", entity=None, byte_budget=6_000)
    assert dropped > 0, "the budget must actually bite for this test to mean anything"
    # One extra assembly is the column the budget refused (it must be measured to be refused).
    assert len(built) <= len(selected) + 1
    assert len(built) < len(cols)


# ── Task 7b: what the curated human context costs ───────────────────────────────────────────────

#: The per-column keys Task 7b added. Named here so the baseline below is RECONSTRUCTED rather than
#: remembered — every "before" number in this file is then a same-commit measurement.
_TASK_7B_COLUMN_KEYS = ("business_term", "related_terms")


def _without_task_7b(column: dict) -> dict:
    """One v4 column payload as it stood BEFORE Task 7b. Nothing else about the column moved, so
    this is an exact reconstruction, not an approximation."""
    out = {k: v for k, v in column.items() if k not in _TASK_7B_COLUMN_KEYS}
    if isinstance(out.get("semantic_authority"), dict):
        out["semantic_authority"] = {k: v for k, v in out["semantic_authority"].items()
                                     if k not in _TASK_7B_COLUMN_KEYS}
    return out


def test_the_floor_rose_by_exactly_what_the_payload_rose_by(wide_catalogs, monkeypatch,
                                                            record_property):
    """The reconciliation, MEASURED at one commit — and the reason the first version of this
    explanation was wrong.

    That version said `business_term`/`related_terms` "shed with the prose ladder's tail". They do
    not: `_V4_TRIM_ORDER` is `(semantic_terms, ai_summary, definition, relationships, concept_path)`
    and Task 7b did not touch it, so NOTHING it added is trimmable. The trimmed set is a strict
    subset of the untrimmed one, so if nothing added is sheddable the two deltas must be EQUAL —
    and the committed figures (+10_804 floor against +8_423 payload) could not both be true. The
    stale one was the 203_629 floor baseline: it dates from the 2026-08-06 measurement, when the
    untrimmed payload was 241_491, and it was never re-measured when Task 6 moved the payload to
    250_982. Reconstructing the baseline in-process removes the class of error entirely.

    MEASURED HERE (237 columns, 2 tables): floor 206_010 -> 214_433, payload 250_982 -> 259_405 —
    the same +8_423, un-sheddable, as the equality below now enforces.
    """
    monkeypatch.setenv("FEATUREGEN_FEATURE_CONTEXT", "1")
    monkeypatch.setenv(fa.FEATURE_CONTEXT_VERSION_ENV, "4")
    cols = _candidates(wide_catalogs)
    base = _table_context(cols)
    full = [fa._context_column(wide_catalogs, c, roles=()) for c in cols]
    trimmed = [fa._trimmed(c, len(fa._V4_TRIM_ORDER)) for c in full]

    untrimmed_now = _assembled_bytes(full, base)
    untrimmed_before = _assembled_bytes([_without_task_7b(c) for c in full], base)
    floor_now = _assembled_bytes(trimmed, base)
    floor_before = _assembled_bytes([_without_task_7b(c) for c in trimmed], base)

    record_property("floor_before", floor_before)
    record_property("floor_now", floor_now)
    # The reconstructed baselines, pinned so the prose above is checkable and not remembered.
    assert (floor_before, untrimmed_before) == (215_507, 260_479), (
        f"reconstructed baseline moved: floor {floor_before}, payload {untrimmed_before}")
    assert not set(_TASK_7B_COLUMN_KEYS) & set(fa._V4_TRIM_ORDER), (
        "a Task-7b key became trimmable — the equality below no longer has to hold, and the "
        "explanation above needs rewriting rather than the assertion relaxing")
    assert floor_now - floor_before == untrimmed_now - untrimmed_before, (
        f"floor +{floor_now - floor_before} vs payload "
        f"+{untrimmed_now - untrimmed_before}: impossible while nothing added is sheddable")
    # RE-MEASURED 2026-08-09 (readiness wave): `fibo_path` reached the payload for the first
    # time (migration 1058 gave it a `graph_node` column; it had SOURCE evidence and nowhere
    # to land), so BOTH sides rose by exactly 9_497 on the 126 FTR glossary columns that
    # carry one. Baseline 206_010/250_982 -> 215_507/260_479, current 214_433/259_405 ->
    # 223_930/268_902. The equal-delta invariant above is what proves the rise is a new
    # un-sheddable field rather than a trim-policy change.
    assert (floor_now, untrimmed_now) == (223_930, 268_902), (
        f"re-measure and re-pin: floor {floor_now}, payload {untrimmed_now}")


def test_measured_cost_of_the_catalog_narrative(wide_catalogs, record_property):
    """What the catalog narrative costs, MEASURED — the reason it rides the TABLE block.

    It is one fact about the whole catalog. Per column it would have been the most expensive thing
    in the payload; per table it is small beside the ~1_094 bytes each COLUMN costs:

      * 2 tables, 237 columns, base table_context     171 bytes
      * a realistic narrative                        +902 bytes total  (~451 / table)
      * the LARGEST a human can author            +29_788 bytes total (~14_894 / table)

    THE WORST CASE IS THE REAL ONE. Every bound is read from `catalog_profiles`
    (200 + 4_000 + 4_000 + 32x200 = 14_600 characters of authored text, which is what the measured
    ~14_894 bytes/table reconciles against), because the first version of this test wrote `32 x 200`
    in its comment and then built `f"domain-{i}"` — 32 strings of ~9 characters. It pinned 17_544
    and called it the maximum; the maximum is 29_788, ~70% higher. The corrected note then got the
    SUM wrong (12_600) on the way past, which is why the figure is now tied to the measurement.

    So the honest numbers: the worst case is ~11% of the measured v4 payload and ~2% of the budget.
    It does NOT scale with catalog width — a 1_000-column catalog pays the same per table — and
    that is the whole argument for the placement, pinned as arithmetic rather than prose: on the
    COLUMN payload the same worst case would be ~14.9 KB x 237 = ~3.5 MB, more than TEN TIMES the
    entire v4 payload and over twice the whole byte budget.
    """
    import json

    from featuregen.overlay.upload.catalog_profiles import (
        MAX_BUSINESS_CONTEXT,
        MAX_DESCRIPTION,
        MAX_DISPLAY_NAME,
        MAX_DOMAIN_LEN,
        MAX_DOMAINS,
        build_catalog_profile_revision,
        catalog_narrative_block,
    )

    cols = _candidates(wide_catalogs)
    base = _table_context(cols)
    # EVERY bound READ from `catalog_profiles`, never re-typed. The first version of this test
    # documented `32 x 200` and then built 32 strings of ~9 characters (`f"domain-{i}"`, ~278 bytes
    # against 6_400), so the "authorable maximum" it pinned was ~59% of the real one — the exact
    # class of drift the v3/v4 band comment above exists to prevent, committed in the same change.
    biggest = catalog_narrative_block(build_catalog_profile_revision(
        catalog_source="budget_ftr", display_name="D" * MAX_DISPLAY_NAME,
        description="x" * MAX_DESCRIPTION, business_context="y" * MAX_BUSINESS_CONTEXT,
        business_domains=tuple(f"{i:03d}" + "d" * (MAX_DOMAIN_LEN - 3) for i in range(MAX_DOMAINS)),
        producer_ref="u"))
    realistic = catalog_narrative_block(build_catalog_profile_revision(
        catalog_source="budget_ftr", display_name="CIB Payments Catalog",
        description=("Funds-transfer records for the corporate and investment bank: one row per "
                     "outbound payment instruction."),
        business_context=("Compliance owns this catalog; it is the book of record for outbound "
                          "SWIFT and RTGS payments, reconciled nightly against settlement."),
        business_domains=("payments", "financial crime"), producer_ref="u"))

    def _size(blocks):
        return len(json.dumps(blocks, sort_keys=True, default=str).encode("utf-8"))

    def _delta(block):
        both = {"budget_ftr": block, "budget_cib": block}
        return _size(_table_context(cols, narratives=both)) - _size(base)

    worst, real = _delta(biggest), _delta(realistic)
    record_property("narrative_worst_case_bytes", worst)
    record_property("narrative_realistic_bytes", real)
    # The authored-character sum in the docstring, CHECKED rather than typed — two successive
    # revisions of this test got a hand-arithmetic figure wrong, so the number now has to hold.
    assert MAX_DISPLAY_NAME + MAX_DESCRIPTION + MAX_BUSINESS_CONTEXT + MAX_DOMAINS * MAX_DOMAIN_LEN \
        == 14_600
    assert len(base) == 2, "the per-TABLE claim above is measured against 2 tables"
    assert 700 < real < 1_200, f"realistic narrative cost moved: {real}"
    assert 28_000 < worst < 32_000, f"worst-case narrative cost moved: {worst}"
    # The placement argument, as arithmetic rather than assertion: per column this same worst case
    # would be ~237x, more than TEN TIMES the whole v4 payload.
    assert worst // len(base) * 237 > 3_000_000
    # Headroom against the budget, stated as the real ratio rather than a comfortable-sounding one.
    assert _size(base) + worst < FEATURE_CONTEXT_BYTE_BUDGET // 40


def test_measured_cost_of_the_grain_and_as_of_status_block(wide_catalogs, record_property):
    """WHAT THE TASK-8b BLOCK COSTS, MEASURED — and the blindness that made measuring it necessary.

    Task 8's review found this fixture carries ZERO `is_grain` / `is_as_of` columns, so the pinned
    v3/v4 bands above never saw the grain/as-of block at all: they would have stayed green while it
    doubled. That is ASSERTED here rather than described, so the day someone declares a grain in the
    fixture this test says so instead of silently measuring a different thing.

    The cost is bounded and does NOT scale with catalog width — it rides the per-TABLE block, like
    the catalog narrative and for the same reason. Per table at most:

      * `grain_columns` + `grain_status`   — the grain's column name(s) + one token
      * `as_of_column`  + `as_of_status`   — one column name + one token

    `ai_proposed` is the state this task genuinely ADDS (a table that previously carried nothing now
    carries both axes), so it is the one measured. `human_confirmed` / `source_declared` replace
    Task 8's `confirmed` / `declared` on tables that already emitted a block, moving the payload only
    by the difference in token length.
    """
    import json

    cols = _candidates(wide_catalogs)
    tables = sorted({(c["catalog_source"], c["table"]) for c in cols})
    assert len(tables) == 2

    # THE BLINDNESS, PINNED. Not "the fixture happens to have no grain" as prose — asserted, so the
    # bands above can never quietly start measuring something else.
    assert not any(c["is_grain"] or c["is_as_of"] for c in cols), (
        "the budget fixture now declares a grain/as-of — the v3/v4 bands above are no longer blind "
        "to this block, and this test's baseline must be re-derived rather than re-pinned")

    def _size(blocks):
        return len(json.dumps(blocks, sort_keys=True, default=str).encode("utf-8"))

    base = _size(_table_context(cols))
    # Real columns of each table: the read-scope guard drops a proposal naming anything the caller
    # was not offered, so an invented name would measure the EMPTY block and call it the full one.
    first = {t: sorted(c["column"] for c in cols
                       if (c["catalog_source"], c["table"]) == t)[:1] for t in tables}
    assert all(first.values())
    ai_both = _size(_table_context(cols, authority={
        t: {"grain": {"human": False, "proposed": first[t]},
            "availability_time": {"human": False, "proposed": first[t][0]}}
        for t in tables})) - base

    record_property("table_fact_base_bytes", base)
    record_property("ai_proposed_block_bytes", ai_both)
    # Nothing proposed and nothing declared: byte-identical to the pre-8b shape. This is the ordinary
    # case for a catalog whose Pass B abstained, and it must cost exactly nothing.
    empty = {t: {"grain": {"human": False, "proposed": None},
                 "availability_time": {"human": False, "proposed": None}} for t in tables}
    assert _size(_table_context(cols, authority=empty)) == base, (
        "an empty authority map changed the payload")
    # Both axes on both tables — the full new cost, bounded to a couple of names and tokens/table.
    assert 0 < ai_both < 400, f"the ai_proposed block cost moved: {ai_both}"
    # It does NOT scale with catalog width: 237 columns pay what 2 tables pay.
    assert ai_both // len(tables) < 200
    # …and it is negligible against the budget — the real ratio, not a comfortable-sounding one.
    assert ai_both < FEATURE_CONTEXT_BYTE_BUDGET // 3_000


# ── the SATURATED measurement (readiness wave) ───────────────────────────────────────────────────


def test_the_saturation_fixture_ACTUALLY_populates_what_it_claims(saturated_catalogs, monkeypatch):
    """A saturation fixture that quietly stops saturating is worse than none — it would pin a
    confident number for a catalog as thin as the one it replaced. So the COVERAGE is asserted
    before the bytes are.

    Two of these counts are deliberately not 237, and both are product facts rather than fixture
    defects:

    * `confidence_band` / `concept_alternatives` are capped by `adjudication_bounds()`
      (`max_provider_calls`, shipped at 12) — adjudication is the EXCEPTION path, so these can
      never scale with catalog width no matter how unclear the catalog is;
    * `relationships` (and its `outbound_cardinality`) needs cross-catalog LINK rows, a different
      subsystem this fixture does not stand up — the one field still measured at zero, recorded
      rather than faked.
    """
    from featuregen.overlay.upload.semantic_adjudication import adjudication_bounds

    monkeypatch.setenv("FEATUREGEN_FEATURE_CONTEXT", "1")
    monkeypatch.setenv(fa.FEATURE_CONTEXT_VERSION_ENV, "4")
    cols = _candidates(saturated_catalogs)
    columns = [fa._context_column(saturated_catalogs, c, roles=()) for c in cols]

    def present(key: str) -> int:
        return sum(1 for c in columns if c.get(key) not in (None, "", [], {}))

    for key in ("sub_domain", "bian_path", "process_path", "fibo_path", "related_terms"):
        assert present(key) == 237, f"{key} saturates {present(key)}/237"
    # F7's subkeys — the pair whose emptiness let a real payload change through this file unnoticed.
    for key in ("unit", "currency"):
        assert sum(1 for c in columns
                   if c[key].get("proposed_value") is not None) == 237, f"{key}.proposed_value"
        assert sum(1 for c in columns
                   if c[key].get("proposed_authority") is not None) == 237, f"{key}.proposed_authority"
    assert present("confidence_band") == adjudication_bounds().max_provider_calls
    assert present("concept_alternatives") == adjudication_bounds().max_provider_calls
    assert present("relationships") == 0          # the one honest remaining zero


def test_the_SATURATED_catalog_is_measured_and_still_clears_the_budget(saturated_catalogs,
                                                                       monkeypatch):
    """The number `wide_catalogs` could not produce, and the reason this fixture exists.

    The un-saturated pair (214_433 / 259_405) measures a catalog with 8 of the branch's 11 added
    fields at exactly 0, so it was a narrower guarantee than its name suggested — proven when F7's
    `proposed_authority` grew the payload and
    `test_the_floor_rose_by_exactly_what_the_payload_rose_by` passed COMPLETELY UNCHANGED, because
    the field it sits beside was empty here.

    The handover's "measured if all populate" figures (payload 313_197, floor 268_225) were never
    pinned by anything and are BOTH LOW — the real saturated payload is ~29% above the first and the
    floor ~34% above the second. They came from a gitignored report, the same provenance that made
    `171_347` and `203_629` wrong. These two numbers replace them and are asserted, so they cannot
    rot the same way.
    """
    monkeypatch.setenv("FEATUREGEN_FEATURE_CONTEXT", "1")
    monkeypatch.setenv(fa.FEATURE_CONTEXT_VERSION_ENV, "4")
    cols = _candidates(saturated_catalogs)
    columns = [fa._context_column(saturated_catalogs, c, roles=()) for c in cols]
    payload = _assembled_bytes(columns, _table_context(cols))
    floor = _assembled_bytes([fa._trimmed(c, len(fa._V4_TRIM_ORDER)) for c in columns],
                             _table_context(cols))

    assert (floor, payload) == (360_891, 405_863), (
        f"the saturated measurement moved: floor {floor}, payload {payload}. Re-derive the rungs in "
        "`feature_assist.FEATURE_CONTEXT_BYTE_BUDGET`'s note and update A.58 in the SAME change — "
        "that comment is the one an operator reads.")
    # Still comfortably inside the budget: saturation costs ~56% more than the sparse fixture and
    # is STILL ~3.7x under. The budget remains a runaway backstop, not a working constraint.
    assert payload < FEATURE_CONTEXT_BYTE_BUDGET * 0.3
