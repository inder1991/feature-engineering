"""Richness Task 3C — the column dossier's payload: asset detail surfaces everything the platform
already holds about a column, honestly labelled.

Four contracts, each closing a "we store it but hide it" defect:

* ``source_glossary`` — the source file's own assertions (business term, term type, process path,
  related terms, BIAN/FIBO, physical FQN) read back from the Task-3-Step-6b source evidence, each
  with its source provenance label; an asset whose upload declared none gets an EMPTY section,
  never a fabricated one.
* type basis — a column whose operational type is unknown but whose source DECLARED a SQL type
  renders the declared type with basis ``declared``; bare "unknown" is reserved for genuinely
  holding nothing.
* AI-proposed instead of blank — a NULL display axis with a live ``llm/proposed`` evidence row
  carries that proposal (``proposed_value``) + its author label, so the screen can render
  "AI-proposed · unconfirmed" instead of a blank; the display value always wins when present.
* projected display axes — ``sensitivity_display`` / ``party_role`` (migrations 1042/1040) are in
  the payload, labelled "system projected" when the deterministic axis projection filled them.
"""
from __future__ import annotations

from tests.featuregen._helpers import mint_test_identity

from featuregen.overlay.field_evidence import record_field_evidence
from featuregen.overlay.upload.asset_detail import build_asset_detail
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.graph import build_graph

ADMIN = mint_test_identity(subject="user:admin", role_claims=("platform_admin",))


def _detail(conn, source, object_ref="public.trades.notional", include=None):
    return build_asset_detail(conn, source=source, object_ref=object_ref,
                              roles=list(ADMIN.role_claims), identity=ADMIN, include=include)


def _seed(conn, source, data_type="numeric"):
    build_graph(conn, source, [CanonicalRow(source, "trades", "notional", data_type)])


def _source_evidence(conn, source, field, value, *, strength="attested"):
    record_field_evidence(
        conn, logical_ref=f"{source}::public.trades.notional", field_name=field,
        proposed_value=value, producer="source", strength=strength,
        producer_ref="upload", source_snapshot_id="snap", input_hash=f"h-{field}",
    )


# ── source_glossary ──────────────────────────────────────────────────────────────────────────────

def test_source_glossary_round_trips_with_source_provenance(overlay_conn):
    source = "dossier_gloss"
    _seed(overlay_conn, source)
    declared = {
        "business_term": "Transaction Amount",
        "term_type": "measure",
        "process_path": "Payments > Screening > Reporting",
        "related_terms": "Transaction Currency, Value Date",
        "bian_path": "Payment Order",
        "fibo_path": "fibo-fnd:MonetaryAmount",
        "physical_fqn": "DPL.COMP_TRAN.NOTIONAL",
    }
    for field, value in declared.items():
        _source_evidence(overlay_conn, source, field, value)
    body = _detail(overlay_conn, source, include=["source_glossary"])
    fields = body["source_glossary"]["fields"]
    assert set(fields) == set(declared)
    for field, value in declared.items():
        assert fields[field]["value"] == value
        assert fields[field]["provenance"] == "source attested"


def test_source_glossary_is_empty_not_fabricated_when_nothing_declared(overlay_conn):
    source = "dossier_nogloss"
    _seed(overlay_conn, source)
    body = _detail(overlay_conn, source, include=["source_glossary"])
    assert body["source_glossary"] == {"fields": {}}
    assert "source_glossary" in body["included_sections"]


def test_source_glossary_ignores_non_source_and_stale_rows(overlay_conn):
    """Only ACTIVE source evidence speaks for the file: an LLM proposal for the same field name
    must never appear as a source-glossary value."""
    source = "dossier_llmnoise"
    _seed(overlay_conn, source)
    record_field_evidence(
        overlay_conn, logical_ref=f"{source}::public.trades.notional", field_name="term_type",
        proposed_value="dimension", producer="llm", strength="proposed",
        producer_ref="model", source_snapshot_id="snap", input_hash="h-llm",
    )
    body = _detail(overlay_conn, source, include=["source_glossary"])
    assert body["source_glossary"]["fields"] == {}


# ── type basis ───────────────────────────────────────────────────────────────────────────────────

def test_declared_type_backs_the_type_field_when_operational_is_unknown(overlay_conn):
    source = "dossier_decl"
    _seed(overlay_conn, source, data_type="unknown")
    overlay_conn.execute(
        "UPDATE graph_node SET declared_type = 'varchar(50)' "
        "WHERE catalog_source = %s AND object_ref = 'public.trades.notional'", (source,))
    field = _detail(overlay_conn, source,
                    include=["effective_metadata"])["effective_metadata"]["fields"]["type"]
    assert field["value"] == "varchar(50)"
    assert field["basis"] == "declared"
    assert field["evidence_provenance"] == "source declared"


def test_operational_type_keeps_operational_basis(overlay_conn):
    source = "dossier_op"
    _seed(overlay_conn, source, data_type="numeric")
    field = _detail(overlay_conn, source,
                    include=["effective_metadata"])["effective_metadata"]["fields"]["type"]
    assert field["value"] == "numeric"
    assert field["basis"] == "operational"


def test_no_type_at_all_is_an_honest_null_basis(overlay_conn):
    source = "dossier_notype"
    _seed(overlay_conn, source, data_type="unknown")
    field = _detail(overlay_conn, source,
                    include=["effective_metadata"])["effective_metadata"]["fields"]["type"]
    assert field["value"] == "unknown"   # the flat column's honest word — nothing is held
    assert field["basis"] is None


# ── AI-proposed instead of blank ─────────────────────────────────────────────────────────────────

def test_null_axis_with_llm_proposal_carries_the_proposed_value(overlay_conn):
    source = "dossier_aiprop"
    _seed(overlay_conn, source)
    record_field_evidence(
        overlay_conn, logical_ref=f"{source}::public.trades.notional", field_name="unit",
        proposed_value="AED", producer="llm", strength="proposed",
        producer_ref="model", source_snapshot_id="snap", input_hash="h-unit",
    )
    field = _detail(overlay_conn, source,
                    include=["effective_metadata"])["effective_metadata"]["fields"]["unit"]
    assert field["value"] is None                          # nothing governed/projected yet
    assert field["proposed_value"] == "AED"                # but the AI's proposal is usable
    assert field["evidence_provenance"] == "AI proposed"


def test_display_value_wins_over_a_proposal(overlay_conn):
    """A governed/file value present on the flat column stays the value; the proposal rides along
    in ``proposed_value`` without replacing it."""
    source = "dossier_govwins"
    _seed(overlay_conn, source)
    overlay_conn.execute(
        "UPDATE graph_node SET unit = 'USD' "
        "WHERE catalog_source = %s AND object_ref = 'public.trades.notional'", (source,))
    record_field_evidence(
        overlay_conn, logical_ref=f"{source}::public.trades.notional", field_name="unit",
        proposed_value="AED", producer="llm", strength="proposed",
        producer_ref="model", source_snapshot_id="snap", input_hash="h-unit2",
    )
    field = _detail(overlay_conn, source,
                    include=["effective_metadata"])["effective_metadata"]["fields"]["unit"]
    assert field["value"] == "USD"
    assert field["proposed_value"] == "AED"


def test_axis_with_nothing_is_explicitly_empty(overlay_conn):
    source = "dossier_nothing"
    _seed(overlay_conn, source)
    field = _detail(overlay_conn, source,
                    include=["effective_metadata"])["effective_metadata"]["fields"]["unit"]
    assert field["value"] is None
    assert field["proposed_value"] is None
    assert field["evidence_provenance"] is None


# ── projected display axes ───────────────────────────────────────────────────────────────────────

def test_projected_sensitivity_and_party_role_surface_as_system_projected(overlay_conn):
    source = "dossier_axes"
    _seed(overlay_conn, source)
    overlay_conn.execute(
        "UPDATE graph_node SET sensitivity_display = 'restricted', party_role = 'sender' "
        "WHERE catalog_source = %s AND object_ref = 'public.trades.notional'", (source,))
    fields = _detail(overlay_conn, source,
                     include=["effective_metadata"])["effective_metadata"]["fields"]
    assert fields["sensitivity_display"]["value"] == "restricted"
    assert fields["sensitivity_display"]["evidence_provenance"] == "system projected"
    assert fields["party_role"]["value"] == "sender"
    assert fields["party_role"]["evidence_provenance"] == "system projected"


def test_unfilled_display_axes_stay_null_never_invented(overlay_conn):
    source = "dossier_axesnull"
    _seed(overlay_conn, source)
    fields = _detail(overlay_conn, source,
                     include=["effective_metadata"])["effective_metadata"]["fields"]
    assert fields["sensitivity_display"]["value"] is None
    assert fields["sensitivity_display"]["evidence_provenance"] is None
    assert fields["party_role"]["value"] is None
