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
    # Between the measured fully-trimmed floor (~203_600) and the untrimmed cost (~248_600): the
    # only way to serve every mandatory column here is to shed prose.
    columns, _ctx, dropped = select_relevant_context(
        wide_catalogs, _candidates(wide_catalogs), objective="customer balance", entity="customer",
        byte_budget=210_000)
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
