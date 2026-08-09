"""Profile Task 5 — profiles become VISIBLE and CONSUMED, or the fields changed nothing.

The task's own exit is "every new field changes at least one real product behaviour", and its last
checklist item is the enforcement: *search, feature and analysis payload tests must FAIL if profile
context is disconnected*. That is what this file is. Each block below breaks if the wiring is
removed, not merely if it is wrong.

Four surfaces:

* **search** — facets on the derived `data_role` (migration 1052) plus `authority_role`,
  `temporal_storage_model` and the D13.1/D13.2 axes, under the EXISTING read-scope predicates.
  Facet counts respect derived table visibility, and a hidden table's profile prose is never an
  existence oracle.
* **feature generation** — data role, primary entity, description, authority label and temporal
  model arrive as CONTEXT and ADVISORIES. The numeric/currency/grain/availability/join gates stay
  authoritative; a data-role warning never refuses anything.
* **data-agent retrieval** — the table profile joins the controlled leg-3 expansion vocabulary.
* **the flag matrix** — with `FEATUREGEN_DATASET_PROFILES` off, every one of those payloads is
  byte-identical to its pre-profile shape, in BOTH feature-context states (D8).
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from tests.featuregen._helpers import mint_test_identity

from featuregen.analysis.retrieval import retrieve_candidates
from featuregen.overlay.config import OverlayConfig, register_overlay_config
from featuregen.overlay.evidence import AssertionStrength, EvidenceProducer
from featuregen.overlay.field_evidence import field_input_hash, record_field_evidence
from featuregen.overlay.upload import feature_assist as fa
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.enrich_llm import sanitize_feature_context
from featuregen.overlay.upload.feature_assist import _candidate_columns, _table_context
from featuregen.overlay.upload.field_resolution import resolve_and_project
from featuregen.overlay.upload.ingest import ingest_upload
from featuregen.overlay.upload.object_ref import normalize_ref
from featuregen.overlay.upload.search import column_facets, search

ACTOR = mint_test_identity(subject="user:owner", role_claims=("data_owner",))
ANALYST = mint_test_identity(subject="user:analyst", role_claims=("data_scientist",))

_NOW = datetime(2026, 8, 1, tzinfo=UTC)
_FRESH = timedelta(days=3650)
_SRC = "bankp"


def _seal() -> None:
    register_overlay_config(OverlayConfig(
        ttl_default=timedelta(days=180), ttl_min=timedelta(days=30), ttl_max=timedelta(days=365),
        ttl_jitter_fraction=0.1, renewal_grace=timedelta(days=14),
        drift_scan_interval=timedelta(minutes=15), drift_freshness_sla=timedelta(hours=24),
        profiler_require_restricted_role=False))


@pytest.fixture
def profiles_on(monkeypatch):
    monkeypatch.setenv("FEATUREGEN_DATASET_PROFILES", "1")
    return monkeypatch


def _table_evidence(db, table: str, field: str, value: str, *,
                    producer=EvidenceProducer.SOURCE,
                    strength=AssertionStrength.ATTESTED) -> None:
    ref = normalize_ref(_SRC, None, table)
    record_field_evidence(
        db, logical_ref=ref, field_name=field, proposed_value=value, producer=producer,
        strength=strength, producer_ref="test", source_snapshot_id="snap",
        input_hash=field_input_hash(logical_ref=ref, field_name=field,
                                    material=f"{value}:{producer.value}:{strength.value}"))


@pytest.fixture
def catalog(db):
    """A crosswalk table and a dimension table, both PROFILED, plus a restricted-only table whose
    prose must never become an existence oracle."""
    _seal()
    rows = [
        CanonicalRow(_SRC, "party_xref", "cif_id", "text", is_grain=True),
        CanonicalRow(_SRC, "party_xref", "legacy_id", "text"),
        CanonicalRow(_SRC, "customer_dim", "cust_key", "text", is_grain=True),
        CanonicalRow(_SRC, "customer_dim", "segment", "text"),
        # Every column restricted ⟹ the TABLE is invisible to a default caller by the DERIVED rule.
        CanonicalRow(_SRC, "secret_ledger", "amount", "numeric", sensitivity="restricted"),
    ]
    assert ingest_upload(db, _SRC, rows, actor=ACTOR, now=_NOW).status == "ingested"

    _table_evidence(db, "party_xref", "table_role", "bridge")
    _table_evidence(db, "party_xref", "business_context",
                    "Maps legacy party identifiers onto the customer information file.")
    _table_evidence(db, "party_xref", "authority_role", "system_of_record")
    _table_evidence(db, "party_xref", "temporal_storage_model", "current_only")
    _table_evidence(db, "customer_dim", "table_role", "dim")
    _table_evidence(db, "customer_dim", "definition", "One row per customer, current attributes.")
    _table_evidence(db, "customer_dim", "primary_entity", "customer")
    _table_evidence(db, "secret_ledger", "business_context",
                    "Restricted settlement obligations for sanctioned counterparties.")
    for table in ("party_xref", "customer_dim", "secret_ledger"):
        resolve_and_project(db, source=_SRC, logical_refs=[normalize_ref(_SRC, None, table)])
    return db


def _facets(db, roles=()):
    return search(db, "", now=_NOW, roles=roles, fresh_within=_FRESH).facets


# ── search: facets ──────────────────────────────────────────────────────────────────────────────


def test_the_profile_facets_do_not_exist_while_the_flag_is_off(catalog):
    # `sensitivity_display` is a BASE facet (the migration-1042 display axis), not a profile one —
    # it is present with the flag off, which is what keeps this assertion about the PROFILE set.
    assert set(column_facets()) == {"source", "domain", "sensitivity", "sensitivity_display",
                                    "additivity", "entity", "kind"}
    assert "data_role" not in _facets(catalog)


def test_the_derived_data_role_is_facetable_with_the_flag_on(catalog, profiles_on):
    facets = _facets(catalog)
    assert "data_role" in facets
    buckets = {b.value: b.count for b in facets["data_role"]}
    # The legacy canonical `bridge` role is faceted as `crosswalk` — one adapter, everywhere.
    assert buckets.get("crosswalk") == 1
    assert buckets.get("dimension") == 1
    assert "bridge" not in buckets


def test_filtering_by_a_profile_facet_returns_the_profiled_tables(catalog, profiles_on):
    result = search(catalog, "", now=_NOW, fresh_within=_FRESH,
                    filters={"data_role": ["crosswalk"]})
    assert [h.object_ref for h in result.hits] == ["public.party_xref"]
    assert result.total == 1


def test_the_authority_and_temporal_axes_are_facetable(catalog, profiles_on):
    facets = _facets(catalog)
    assert {b.value for b in facets["authority_role"]} >= {"system_of_record"}
    assert {b.value for b in facets["temporal_storage_model"]} >= {"current_only"}


def test_the_d13_classification_axes_are_facetable(catalog, profiles_on):
    facets = _facets(catalog)
    for name in ("bian_path", "process_path", "sub_domain"):
        assert name in facets, name


# ── search: read scope, on the facets themselves ────────────────────────────────────────────────


def test_a_hidden_tables_profile_is_never_counted_into_a_facet_bucket(catalog, profiles_on):
    """A facet count IS a disclosure: "1 table has authority_role=X" tells a caller a table exists.
    The base predicates (including the DERIVED table-visibility rule) apply to the facet queries,
    so the restricted table cannot appear in any bucket for a caller who cannot see its columns."""
    _table_evidence(catalog, "secret_ledger", "authority_role", "mastered_view")
    resolve_and_project(catalog, source=_SRC,
                        logical_refs=[normalize_ref(_SRC, None, "secret_ledger")])
    unprivileged = {b.value for b in _facets(catalog)["authority_role"]}
    assert "mastered_view" not in unprivileged
    privileged = {b.value
                  for b in _facets(catalog, roles=("restricted_reader",))["authority_role"]}
    assert "mastered_view" in privileged


def test_a_hidden_tables_profile_prose_is_not_an_existence_oracle(catalog, profiles_on):
    """The business context of a table whose every column is restricted is now IN the search
    document (migration 1052). The derived table predicate is what stops that from making the
    table matchable by anyone holding `catalog:read`."""
    hidden = search(catalog, "sanctioned counterparties", now=_NOW, fresh_within=_FRESH).hits
    assert hidden == []
    shown = search(catalog, "sanctioned counterparties", now=_NOW, fresh_within=_FRESH,
                   roles=("restricted_reader",)).hits
    assert [h.object_ref for h in shown] == ["public.secret_ledger"]


def test_table_prose_is_searchable_when_the_caller_can_see_the_table(catalog):
    """Not flag-gated: the search DOCUMENT is a projection, and hiding matchable text behind a flag
    would make the index disagree with itself between deployments."""
    hits = search(catalog, "legacy party identifiers", now=_NOW, fresh_within=_FRESH).hits
    assert [h.object_ref for h in hits] == ["public.party_xref"]


# ── feature generation: advisories, never gates ─────────────────────────────────────────────────


def _blocks(db):
    return {b["table"]: b for b in _table_context(_candidate_columns(db, _SRC, roles=()))}


def test_feature_context_carries_no_profile_keys_while_the_flag_is_off(catalog):
    block = _blocks(catalog)["party_xref"]
    for key in ("data_role", "authority_role", "temporal_storage_model", "business_context",
                "advisories"):
        assert key not in block, key


def test_feature_context_consumes_the_profile_as_advisories(catalog, profiles_on):
    blocks = _blocks(catalog)
    xref = blocks["party_xref"]
    assert xref["data_role"] == "crosswalk"
    assert xref["authority_role"] == "system_of_record"
    assert xref["temporal_storage_model"] == "current_only"
    assert "legacy party identifiers" in xref["business_context"]
    # The warning the role earns: a crosswalk's columns identify, they do not measure.
    assert any("CROSSWALK" in a for a in xref["advisories"])

    dim = blocks["customer_dim"]
    assert dim["data_role"] == "dimension"
    assert dim["primary_entity"] == "customer"
    assert any("DIMENSION" in a for a in dim["advisories"])


def test_a_data_role_warning_is_advisory_and_never_refuses_a_candidate(catalog, profiles_on):
    """The plan is explicit: data role "does not hard-refuse by itself". A crosswalk table's
    numeric column still produces a candidate — the model is TOLD why that is usually wrong, which
    is a better outcome than a rejection the human has to decode."""
    from featuregen.intake.llm import FakeLLM, FakeResponse
    from featuregen.overlay.upload.feature_assist import recommend_features_report

    catalog.execute("UPDATE graph_node SET data_type = 'numeric' "
                    "WHERE object_ref = 'public.party_xref.legacy_id'")
    client = FakeLLM(script={"overlay.feature.recommend": FakeResponse(output={"features": [
        {"name": "legacy_id_count", "description": "count of legacy ids",
         "derives_from": ["public.party_xref.legacy_id"], "aggregation": "count",
         "grain_table": "party_xref"}]})})
    report = recommend_features_report(catalog, "profile customers", client, catalog_source=_SRC)
    assert [i.name for i in report.ideas] == ["legacy_id_count"]
    assert not any(r["code"] in ("ADDITIVITY", "NON_NUMERIC") for r in report.rejections)


def test_a_snapshot_table_earns_the_time_mismatch_advisory(db, profiles_on):
    _seal()
    rows = [CanonicalRow(_SRC, "balances", "bal_id", "text", is_grain=True),
            CanonicalRow(_SRC, "balances", "snap_ts", "timestamp", as_of=True)]
    assert ingest_upload(db, _SRC, rows, actor=ACTOR, now=_NOW).status == "ingested"
    db.execute("UPDATE graph_node SET availability_fact_event_id = 'fe_a' "
               "WHERE object_ref = 'public.balances.snap_ts'")
    _table_evidence(db, "balances", "table_role", "snapshot_fact")
    resolve_and_project(db, source=_SRC, logical_refs=[normalize_ref(_SRC, None, "balances")])
    block = {b["table"]: b for b in _table_context(_candidate_columns(db, _SRC, roles=()))}
    advisories = " ".join(block["balances"]["advisories"])
    assert "SNAPSHOT" in advisories
    assert "counts snapshots, not activity" in advisories
    # Task 8b: `source_declared`, not `confirmed`. This upload's as-of really was auto-confirmed by
    # `_assert_fact` from the file's own flag, and no human ever saw it — the status now says which.
    assert block["balances"]["as_of_status"] == "source_declared"


def test_a_snapshot_table_earns_the_advisory_on_an_UNCONFIRMED_as_of_too(db, profiles_on):
    """The advisory must track what the block EMITS, not whether a governed fact is servable.

    Task 8 made `_table_context` emit a merely-DECLARED as-of column, so the identical table now
    hands the model a time anchor where it used to hand it nothing. Suppressing the warning there
    would silence it on exactly the tables whose anchor is least examined — and the sentence is true
    of the STORAGE MODEL, not of the fact lifecycle.

    THE UNSTAMPED STATE IS CONSTRUCTED, not assumed. `ingest_upload` AUTO-CONFIRMS a file-declared
    grain/as-of (`_assert_fact`, `authority_basis=source_declared`), so an ordinary upload lands on a
    STAMPED column; the first draft of this test asserted the unstamped state off a plain upload and
    was wrong. Nulling the stamps reproduces the real state that reaches here — the file's flag
    surviving a governed fact that `resolve_fact` will not currently serve (drift-STALEd / expired),
    which is what `project_table_facts_for_ref` leaves behind because its clear SPARES declared
    columns.

    TASK 8b: the token is `source_declared` in BOTH configurations, and that is the point of the
    change. Stamped or not, the uploader's file is who asserted this as-of and nobody reviewed it;
    whether the governed fact is currently servable is an EXECUTION question, and the execution path
    answers it from the fact stream rather than from this block. The state the two tests differ in is
    still constructed and still exercised — it just no longer produces two different labels for one
    assertion."""
    _seal()
    rows = [CanonicalRow(_SRC, "balances", "bal_id", "text", is_grain=True),
            CanonicalRow(_SRC, "balances", "snap_ts", "timestamp", as_of=True)]
    assert ingest_upload(db, _SRC, rows, actor=ACTOR, now=_NOW).status == "ingested"
    _table_evidence(db, "balances", "table_role", "snapshot_fact")
    resolve_and_project(db, source=_SRC, logical_refs=[normalize_ref(_SRC, None, "balances")])
    db.execute("UPDATE graph_node SET availability_fact_event_id = NULL, "
               "grain_fact_event_id = NULL WHERE catalog_source = %s AND table_name = 'balances'",
               (_SRC,))
    block = {b["table"]: b for b in _table_context(_candidate_columns(db, _SRC, roles=()))}["balances"]
    assert not db.execute(
        "SELECT 1 FROM graph_node WHERE catalog_source = %s AND table_name = 'balances' "
        "AND availability_fact_event_id IS NOT NULL", (_SRC,)).fetchall(), (
        "the unstamped state this test exists for was not constructed")
    assert (block["as_of_column"], block["as_of_status"]) == ("snap_ts", "source_declared")
    assert (block["grain_columns"], block["grain_status"]) == (["bal_id"], "source_declared")
    assert "counts snapshots, not activity" in " ".join(block["advisories"])


def test_the_profile_keys_pass_the_real_egress_adapter(catalog, profiles_on):
    """Every new table-context key is explicitly classified (D10) — the prose one through the
    definition sanitizer, the closed-vocabulary ones as identity tokens, the advisories as a
    bounded list. An unclassified key would block the whole menu."""
    blocks = _table_context(_candidate_columns(catalog, _SRC, roles=()))
    safe, _pii, _samples, _version = sanitize_feature_context({"table_context": blocks})
    assert safe is not None
    assert {b["table"] for b in safe["table_context"]} == {b["table"] for b in blocks}
    assert any("advisories" in b for b in safe["table_context"])


def test_an_unclassified_table_context_key_still_blocks(catalog, profiles_on):
    blocks = _table_context(_candidate_columns(catalog, _SRC, roles=()))
    blocks[0]["surprise"] = "unclassified"
    assert sanitize_feature_context({"table_context": blocks})[0] is None


# ── data-agent retrieval ────────────────────────────────────────────────────────────────────────


def test_retrieval_expansion_uses_the_table_profile_with_the_flag_on(catalog, profiles_on):
    got = retrieve_candidates(catalog, "cif identifier", now=_NOW, fresh_within=_FRESH)
    terms = set(got.expansion_terms)
    # From the table DESCRIPTION and BUSINESS CONTEXT…
    assert {"legacy", "identifiers"} & terms
    # …and from the derived data role itself.
    assert "crosswalk" in terms


def test_retrieval_expansion_is_unchanged_while_the_flag_is_off(catalog):
    got = retrieve_candidates(catalog, "cif identifier", now=_NOW, fresh_within=_FRESH)
    assert "crosswalk" not in got.expansion_terms
    assert "identifiers" not in got.expansion_terms


# ── the D8 flag matrix: byte-identity in every off-state ────────────────────────────────────────


def _feature_payload(db, monkeypatch, *, profiles: bool, feature_context: bool) -> str:
    if profiles:
        monkeypatch.setenv("FEATUREGEN_DATASET_PROFILES", "1")
    else:
        monkeypatch.delenv("FEATUREGEN_DATASET_PROFILES", raising=False)
    if feature_context:
        monkeypatch.setenv("FEATUREGEN_FEATURE_CONTEXT", "1")
    else:
        monkeypatch.delenv("FEATUREGEN_FEATURE_CONTEXT", raising=False)
    monkeypatch.delenv(fa.FEATURE_CONTEXT_VERSION_ENV, raising=False)
    cols = _candidate_columns(db, _SRC, roles=())
    menu, table_context = fa._build_menu(db, cols, objective="profile customers", roles=())
    return json.dumps({"columns": menu, "table_context": table_context}, sort_keys=True,
                      default=str)


@pytest.mark.parametrize("profiles", [False, True])
def test_the_feature_payload_is_byte_identical_with_feature_context_off(catalog, monkeypatch,
                                                                       profiles):
    """D8: profile-in-feature-context is governed by BOTH flags. With FEATURE_CONTEXT off there is
    no context to put a profile in, so the DATASET_PROFILES state must make no difference at all."""
    off = _feature_payload(catalog, monkeypatch, profiles=False, feature_context=False)
    got = _feature_payload(catalog, monkeypatch, profiles=profiles, feature_context=False)
    assert got == off


def test_the_feature_payload_is_byte_identical_with_profiles_off(catalog, monkeypatch):
    """The other axis: FEATURE_CONTEXT on, DATASET_PROFILES off ⟹ exactly the pre-profile
    context. The profile must be additive or the flag is not a rollback."""
    base = _feature_payload(catalog, monkeypatch, profiles=False, feature_context=True)
    again = _feature_payload(catalog, monkeypatch, profiles=False, feature_context=True)
    assert base == again
    withp = _feature_payload(catalog, monkeypatch, profiles=True, feature_context=True)
    assert withp != base, "with both flags on the profile must actually reach the payload"
    # …and turning profiles back off restores the exact bytes.
    assert _feature_payload(catalog, monkeypatch, profiles=False, feature_context=True) == base


def test_the_search_payload_is_byte_identical_with_profiles_off(catalog, monkeypatch):
    def _payload() -> str:
        result = search(catalog, "", now=_NOW, fresh_within=_FRESH)
        return json.dumps(
            {"facets": {k: [(b.value, b.count) for b in v] for k, v in result.facets.items()},
             "total": result.total}, sort_keys=True)

    monkeypatch.delenv("FEATUREGEN_DATASET_PROFILES", raising=False)
    before = _payload()
    monkeypatch.setenv("FEATUREGEN_DATASET_PROFILES", "1")
    assert _payload() != before
    monkeypatch.delenv("FEATUREGEN_DATASET_PROFILES", raising=False)
    assert _payload() == before
