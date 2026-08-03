"""P4 v1 Task 1 — per-table feature suggestions with NO hypothesis, NO intent and NO LLM.

The catalog is built through the REAL FTR path (``read_ftr_glossary`` -> ``to_glossary_upload`` ->
``ingest_upload``) under its OWN source name: a generic name like ``bank`` is created as a
schema-less TECHNICAL source elsewhere in the suite, and the source-kind guard would then HOLD this
FTR upload in a full-suite run. The table's grain / availability facts are then GOVERNED through the
real propose -> confirm -> project path, because ``_validate_idea`` refuses a windowed candidate on a
table with no point-in-time basis.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import NamedTuple

import pytest
from tests.featuregen._helpers import mint_test_identity
from tests.featuregen.overlay.upload.test_ftr_adapter import _HDR, _row

from featuregen.contracts.envelopes import Command, IdentityEnvelope
from featuregen.intake.llm import FakeLLM, FakeResponse
from featuregen.overlay.commands import confirm_fact, propose_fact
from featuregen.overlay.config import OverlayConfig, register_overlay_config
from featuregen.overlay.identity import fact_key, proposal_fingerprint
from featuregen.overlay.upload import join_path
from featuregen.overlay.upload import suggestions as suggestions_module
from featuregen.overlay.upload import templates as templates_module
from featuregen.overlay.upload.canonical import validate_rows
from featuregen.overlay.upload.contract.gate1 import _template_candidates
from featuregen.overlay.upload.enrich import content_hash
from featuregen.overlay.upload.feature_assist import FeatureIdea
from featuregen.overlay.upload.ftr_adapter import read_ftr_glossary, to_glossary_upload
from featuregen.overlay.upload.ingest import ingest_upload
from featuregen.overlay.upload.join_path import clearing_reachable_tables
from featuregen.overlay.upload.source_profile import FTR_GLOSSARY_PROFILE
from featuregen.overlay.upload.suggestions import render_recipe, suggest_features_for_table
from featuregen.overlay.upload.table_fact_governance import (
    load_table_fact_confirmation_context,
    project_verified_table_fact,
)
from featuregen.overlay.upload.upload_catalog import table_ref

SOURCE = "p4_suggestions_ftr"
TABLE = "comp_fin_tran"
OTHER_TABLE = "mkt_risk_pos"
# The SIBLING: transaction-shaped exactly like TABLE, so it competes for the same recipes. Named to
# sort AFTER TABLE because `_ranked_matches` breaks ties on (score, table, column, object_ref) — so
# catalog-wide grounding gives every shared recipe to `comp_fin_tran` and this table gets NOTHING.
SIBLING_TABLE = "loan_repay"
NOW = datetime(2026, 7, 27, tzinfo=UTC)
_FQN_PREFIX = "DPL_EIB_COMPLIANCE."

# table -> column -> (concept, declared type, business term). The concepts are what the enrichment
# stage proposes; grounding is the router, so this is what decides which template families surface.
# THREE tables and TWO source entities on purpose: a per-table filter and a per-entity grouping can
# only be shown to work by a fixture that would expose them being wrong, and the starvation the
# per-table pass exists to fix is only visible when two tables carry the SAME concept profile.
_COLUMNS = {
    TABLE: {
        "CIF_ID": ("customer_id", "varchar", "Customer Identifier"),
        "ACCT_ID": ("account_id", "varchar", "Account Identifier"),
        "TXN_AMT": ("monetary_flow", "decimal", "Transaction Amount"),
        "BAL_AMT": ("monetary_stock", "decimal", "Account Balance"),
        "AS_OF_DT": ("as_of_date", "date", "As Of Date"),
        "TXN_TS": ("event_timestamp", "timestamp", "Transaction Timestamp"),
        "TXN_CNT": ("count", "integer", "Transaction Count"),
        "CUST_HOLD": ("custody_holding", "decimal", "Custody Holding"),
        "SETL_STAT": ("settlement_status", "varchar", "Settlement Status"),
    },
    OTHER_TABLE: {
        "BOOK_ID": ("book_id", "varchar", "Trading Book Identifier"),
        "VAR_AMT": ("var", "decimal", "Value At Risk"),
        "RISK_DT": ("as_of_date", "date", "Risk As Of Date"),
    },
    SIBLING_TABLE: {
        "LOAN_CIF": ("customer_id", "varchar", "Borrower Customer Identifier"),
        "LOAN_ACCT": ("account_id", "varchar", "Loan Account Identifier"),
        "REPAY_AMT": ("monetary_flow", "decimal", "Repayment Amount"),
        "PRIN_BAL": ("monetary_stock", "decimal", "Principal Balance"),
        "DUE_DT": ("as_of_date", "date", "Repayment Due Date"),
        "POST_TS": ("event_timestamp", "timestamp", "Repayment Posting Timestamp"),
        "REPAY_CNT": ("count", "integer", "Repayment Count"),
    },
}

# ── the JOIN-WIDENING catalog: TWO tables, and the split is the whole point ───────────────────────
# The measure table carries amounts but NO customer key; the entity table carries the customer key
# but no amount. "Average/trend of a balance PER CUSTOMER" is therefore unbuildable on either table
# alone and buildable only across the join — the exact feature per-table grounding made invisible.
_JOIN_SOURCE = "p4_suggestions_join_ftr"
_MEASURE_TABLE = "txn_ledger"
_ENTITY_TABLE = "cust_master"
_JOIN_COLUMNS = {
    _MEASURE_TABLE: {
        "LEDGER_ACCT": ("account_id", "varchar", "Ledger Account Identifier"),
        "TXN_AMT": ("monetary_flow", "decimal", "Ledger Transaction Amount"),
        "BAL_AMT": ("monetary_stock", "decimal", "Ledger Balance"),
        "TXN_TS": ("event_timestamp", "timestamp", "Ledger Posting Timestamp"),
        "LEDGER_DT": ("as_of_date", "date", "Ledger As Of Date"),
        # account-anchored concepts, so the ledger's UNWIDENED screen is not empty: the widening has
        # to be shown ADDING cards to a working screen, not rescuing a blank one.
        "CUST_HOLD": ("custody_holding", "decimal", "Ledger Custody Holding"),
        "SETL_STAT": ("settlement_status", "varchar", "Ledger Settlement Status"),
    },
    _ENTITY_TABLE: {
        "MASTER_CIF": ("customer_id", "varchar", "Master Customer Identifier"),
        "MASTER_ACCT": ("account_id", "varchar", "Master Account Identifier"),
        "MASTER_DT": ("as_of_date", "date", "Master As Of Date"),
    },
}
_JOIN_FROM = f"public.{_MEASURE_TABLE}.ledger_acct"
_JOIN_TO = f"public.{_ENTITY_TABLE}.master_acct"

_SERVICE = IdentityEnvelope(subject="featuregen-overlay-enrichment", actor_kind="service",
                            authenticated=True, auth_method="internal", role_claims=())
_UPLOADER = IdentityEnvelope(subject="upload", actor_kind="human", authenticated=True,
                             auth_method="oidc", role_claims=("data_owner",))


class _Catalog(NamedTuple):
    source: str
    table: str


def _seal() -> None:
    register_overlay_config(OverlayConfig(
        ttl_default=timedelta(days=180), ttl_min=timedelta(days=30), ttl_max=timedelta(days=365),
        ttl_jitter_fraction=0.1, renewal_grace=timedelta(days=14),
        drift_scan_interval=timedelta(minutes=15), drift_freshness_sla=timedelta(hours=24),
        profiler_require_restricted_role=False))


def _ftr_csv(columns: dict) -> str:
    return _HDR + "".join(
        _row(source_row=str(n), fqn=f"{_FQN_PREFIX}{table.upper()}.{col}", term_name=term,
             definition=f'"{term}, recorded on {table}."', data_type=declared)
        for n, (table, col, (_concept, declared, term)) in enumerate(
            ((t, c, spec) for t, cols in columns.items() for c, spec in cols.items()), start=1))


def _ingest(conn, source: str, columns: dict) -> None:
    """Build ``source`` through the REAL FTR path, tagged with the concepts the enrichment stage
    would have proposed. Grounding is the router, so those concepts are what decide which template
    families surface."""
    upload = to_glossary_upload(read_ftr_glossary(_ftr_csv(columns), source=source))
    good = validate_rows(upload.rows, source, profile=FTR_GLOSSARY_PROFILE).good
    concept_of = {col: spec[0] for cols in columns.values() for col, spec in cols.items()}
    concepts = {content_hash(r): concept_of[r.column.upper()] for r in good}
    client = FakeLLM(script={
        "overlay.enrich.concept": FakeResponse(output={"results": [
            {"ref": h, "concept": c} for h, c in concepts.items()]}),
        "overlay.enrich.definition": FakeResponse(output={"results": []}),
        "overlay.enrich.domain": FakeResponse(output={"results": [
            {"ref": t, "domain": "payments"} for t in columns]}),
        "overlay.enrich.synonyms": FakeResponse(output={"results": []}),
        # The measure-unit stage only DISPATCHES once a run is big enough to batch, so the small
        # fixtures here never reached it. The hub fixture below does. It proposes NOTHING: an AI
        # unit is `llm/proposed` evidence that can never clear UNIT_CONSISTENT anyway, so an empty
        # result keeps every fixture in this module on exactly the units its file declares.
        "overlay.enrich.unit": FakeResponse(output={"results": []}),
    })
    res = ingest_upload(conn, source, upload.rows, actor=_UPLOADER, now=NOW,
                        client=client, glossary=upload)
    assert res.status == "ingested", res.status


def _govern_table_facts(conn, table: str, grain_column: str, as_of_column: str,
                        source: str = SOURCE) -> None:
    """Grain + availability through the REAL governance path: the service enrichment actor proposes,
    a platform admin confirms, the confirm-time bridge projects onto ``graph_node``."""
    admin = mint_test_identity(subject="user:admin", role_claims=("platform-admin",))
    ref = table_ref(source, table)
    for fact_type, value in (("grain", {"columns": [grain_column], "is_unique": True}),
                             ("availability_time", {"column": as_of_column, "basis": "posted_at"})):
        res = propose_fact(conn, Command(
            "propose_fact", "overlay_fact", None,
            {"ref": ref, "fact_type": fact_type, "proposed_value": value},
            _SERVICE, proposal_fingerprint(value)))
        assert res.accepted, res.denied_reason
        ctx = load_table_fact_confirmation_context(conn, fact_key(ref, fact_type))
        res = confirm_fact(conn, Command(
            "confirm_fact", "overlay_fact", None,
            {"ref": ctx["ref"], "fact_type": ctx["fact_type"], "use_case": ctx["use_case"],
             "target_event_id": ctx["target_event_id"]},
            admin, f"confirm-{ctx['target_event_id']}"))
        assert res.accepted, res.denied_reason
        assert project_verified_table_fact(conn, source, ref, fact_type, now=NOW) == "projected"


@pytest.fixture
def ftr_catalog(overlay_conn):
    _seal()
    _ingest(overlay_conn, SOURCE, _COLUMNS)
    _govern_table_facts(overlay_conn, TABLE, "cif_id", "as_of_dt")
    _govern_table_facts(overlay_conn, OTHER_TABLE, "book_id", "risk_dt")
    _govern_table_facts(overlay_conn, SIBLING_TABLE, "loan_cif", "due_dt")
    return _Catalog(source=SOURCE, table=TABLE)


def test_suggests_features_for_a_table_without_any_hypothesis(overlay_conn, ftr_catalog):
    """The whole point: no intent, no hypothesis, no LLM — just the catalog."""
    out = suggest_features_for_table(
        overlay_conn, catalog_source=ftr_catalog.source, table=ftr_catalog.table)
    assert out["summary"]["suggested"] >= 1
    # counts are the REAL tri-state, not invented
    assert out["summary"]["clean_ready"] + out["summary"]["needs_review"] == out["summary"]["suggested"]
    s = out["groups"][0]["suggestions"][0]
    assert s["description"]          # Template.intent, a real SME sentence
    assert s["validation_status"] in ("DESIGN_CHECKED", "NEEDS_EXTERNAL_VALIDATION")
    assert s["uses"]                 # the columns it binds


def _names(out: dict) -> set[str]:
    return {s["name"] for g in out["groups"] for s in g["suggestions"]}


def test_only_this_tables_suggestions_are_returned(overlay_conn, ftr_catalog):
    """The catalog holds TWO governed tables, so dropping the per-table filter is visible: without a
    second table's features in the engine's catalog-wide result there is nothing to leak."""
    out = suggest_features_for_table(
        overlay_conn, catalog_source=ftr_catalog.source, table=ftr_catalog.table)
    other = suggest_features_for_table(
        overlay_conn, catalog_source=ftr_catalog.source, table=OTHER_TABLE)
    assert _names(other)                            # the fixture really does ground on both tables
    assert not (_names(out) & _names(other))
    for g in out["groups"]:
        for s in g["suggestions"]:
            assert s["grain_table"] == ftr_catalog.table


def test_grouped_by_entity(overlay_conn, ftr_catalog):
    """Two DISTINCT source entities on the ONE table: the recipes anchored on ``customer_id`` bind
    ``cif_id``, the custody / settlement recipes anchor on ``account_id`` and bind ``acct_id``. The
    table's single ``is_grain`` column is ``cif_id``, so a grouping taken from the table grain files
    the account-grained features under the customer heading — this is what that would look like."""
    out = suggest_features_for_table(
        overlay_conn, catalog_source=ftr_catalog.source, table=ftr_catalog.table)
    by_label = {g["entity_label"]: g for g in out["groups"]}
    assert set(by_label) == {"customer", "account"}
    assert out["summary"]["entities"] == 2
    assert by_label["account"]["entity_ref"] == f"public.{TABLE}.acct_id"
    assert by_label["customer"]["entity_ref"] == f"public.{TABLE}.cif_id"
    # custody_holding_dynamics is bound to acct_id (account), NOT to the table's cif_id grain
    assert "custody_holding_dynamics_90d" in {s["name"] for s in by_label["account"]["suggestions"]}
    assert "balance_trend_90d" in {s["name"] for s in by_label["customer"]["suggestions"]}


def test_a_table_this_catalog_does_not_hold_is_a_distinct_state(overlay_conn, ftr_catalog):
    """The truthfulness fix: an unknown table and a table with no concepts both produce zero
    suggestions, so without ``table_known`` the screen diagnoses a NONEXISTENT table as "your
    columns don't carry business concepts" — a confident, false claim about the catalog."""
    out = suggest_features_for_table(
        overlay_conn, catalog_source=ftr_catalog.source, table="no_such_table")
    assert out["table_known"] is False
    assert out["summary"] == {"suggested": 0, "clean_ready": 0, "needs_review": 0, "entities": 0}
    assert out["groups"] == [] and out["rejections"] == []
    # a table it DOES hold is the other state, on the same payload key
    assert suggest_features_for_table(
        overlay_conn, catalog_source=ftr_catalog.source, table=ftr_catalog.table)["table_known"]


def test_the_schema_qualified_table_ref_resolves_to_the_same_table(overlay_conn, ftr_catalog):
    """A deep link naturally carries the table's ``object_ref`` (``public.comp_fin_tran``) while the
    engine keys on the bare name. The ref is unique per catalog, so it resolves — and the payload
    echoes the name the engine actually filtered on."""
    bare = suggest_features_for_table(
        overlay_conn, catalog_source=ftr_catalog.source, table=ftr_catalog.table)
    qualified = suggest_features_for_table(
        overlay_conn, catalog_source=ftr_catalog.source, table=f"public.{ftr_catalog.table}")
    assert qualified["table_known"] and qualified["table"] == ftr_catalog.table
    assert _names(qualified) == _names(bare)


def test_each_entity_appears_in_exactly_one_group(overlay_conn, ftr_catalog):
    """Groups are keyed on the entity REF alone. Keying on (ref, label) let one column open two
    groups — which the screen renders under the same React key."""
    out = suggest_features_for_table(
        overlay_conn, catalog_source=ftr_catalog.source, table=ftr_catalog.table)
    refs = [g["entity_ref"] for g in out["groups"]]
    assert len(refs) == len(set(refs))


def test_the_unlabelled_group_sorts_last(overlay_conn, ftr_catalog, monkeypatch):
    """A group whose entity could not be NAMED renders no heading, so sorting it first stacks
    headingless cards above the named groups and they read as the page's lead."""
    calls: list[str] = []

    def _alternating(idea, *_a, **_k):
        """Half the ideas resolve an entity, half do not — the fixture names no un-nameable entity,
        so the only way to see the ordering is to make one."""
        calls.append(idea.name)
        return ("", "") if len(calls) % 2 else (f"public.{TABLE}.acct_id", "account")

    monkeypatch.setattr(suggestions_module, "_entity_of", _alternating)
    out = suggest_features_for_table(
        overlay_conn, catalog_source=ftr_catalog.source, table=ftr_catalog.table)
    refs = [g["entity_ref"] for g in out["groups"]]
    assert "" in refs and len(refs) > 1
    assert refs[-1] == ""


def test_the_rejection_list_is_the_engines_own_per_table_list(overlay_conn, ftr_catalog):
    """The rejections needed a SECOND catalog-wide grounding pass to rebuild ``name -> grain
    table(s)``, because the engine's rejection entries carry only ``{name, reason, code}`` and the
    engine's list was catalog-wide. Under per-table grounding every rejected candidate was grounded
    on THIS table's columns alone, so the list is already this table's and the second pass — with its
    re-attribution guesswork and its "kept because the second pass could not place it" fallback — is
    gone. Pinned directly against the engine so a silent divergence cannot creep back."""
    engine = _template_candidates(overlay_conn, catalog_source=SOURCE, roles=(), target_ref=None,
                                  now=None, table=TABLE)[1]
    assert engine, "the fixture rejects nothing on this table — the pin would be vacuous"
    out = suggest_features_for_table(overlay_conn, catalog_source=SOURCE, table=TABLE)
    assert out["rejections"] == engine


def test_one_page_view_reads_the_catalog_columns_once(overlay_conn, ftr_catalog, monkeypatch):
    """The page used to ground TWICE — once for the candidates and once to re-attribute the
    rejections — over the WHOLE catalog. It now grounds once, over one table."""
    calls: list[tuple[str, tuple[str, ...]]] = []
    real = templates_module._load_columns

    def _spy(conn, catalog_source, roles):
        calls.append((catalog_source, tuple(roles)))
        return real(conn, catalog_source, roles)

    monkeypatch.setattr(templates_module, "_load_columns", _spy)
    suggest_features_for_table(overlay_conn, catalog_source=SOURCE, table=TABLE)
    assert calls == [(SOURCE, ())]


def test_rejections_are_this_tables_only(overlay_conn, ftr_catalog):
    """A rejection is a claim ABOUT this table's catalog readiness, but the engine's rejection list is
    catalog-wide and carries no grain — an unfiltered pass-through shows one table's rejects on another."""
    out = suggest_features_for_table(
        overlay_conn, catalog_source=ftr_catalog.source, table=ftr_catalog.table)
    other = suggest_features_for_table(
        overlay_conn, catalog_source=ftr_catalog.source, table=OTHER_TABLE)
    assert out["rejections"]                        # this table really does reject something
    assert not ({r["name"] for r in out["rejections"]} & {r["name"] for r in other["rejections"]})


# ── per-table grounding: the starvation fix ──────────────────────────────────────────────────────
def _catalog_wide_ideas(conn) -> list[FeatureIdea]:
    """The FEATURE-GENERATION engine's own pass — ``_template_candidates`` called exactly as
    ``build_considered_set`` calls it, with no table narrowing."""
    return _template_candidates(conn, catalog_source=SOURCE, roles=(), target_ref=None, now=None)[0]


def _uses(out: dict) -> set[str]:
    return {ref for g in out["groups"] for s in g["suggestions"] for ref in s["uses"]}


def test_a_table_starved_by_catalog_wide_grounding_gets_its_own_suggestions(
        overlay_conn, ftr_catalog):
    """THE HEADLINE. ``ground_all_outcomes`` yields AT MOST ONE candidate per template catalog-wide,
    and ties break on table name — so ``comp_fin_tran`` wins every shared recipe and its identically
    shaped sibling ``loan_repay`` shows an EMPTY screen ("your columns carry no business concepts"),
    which is a confident, false claim about a table that carries seven tagged columns.

    Grounding the registry against ONLY this table's columns is what the screen was always asking
    for. The recipes and the bound columns are asserted specifically: a non-empty count would also
    be satisfied by leaking another table's cards in."""
    # the starvation, pinned on the engine's own catalog-wide pass
    assert not [i for i in _catalog_wide_ideas(overlay_conn) if i.grain_table == SIBLING_TABLE]

    out = suggest_features_for_table(
        overlay_conn, catalog_source=SOURCE, table=SIBLING_TABLE)
    assert out["table_known"] is True
    names = _names(out)
    assert {"balance_trend_90d", "dormancy_days"} <= names, sorted(names)
    cards = {s["name"]: s for g in out["groups"] for s in g["suggestions"]}
    # the SIBLING's own balance / own event clock — not comp_fin_tran's
    assert f"public.{SIBLING_TABLE}.prin_bal" in cards["balance_trend_90d"]["uses"]
    assert f"public.{SIBLING_TABLE}.post_ts" in cards["dormancy_days"]["uses"]
    assert cards["balance_trend_90d"]["recipe"] == "trend_90d(prin_bal) BY loan_cif OVER 90d [due_dt]"
    assert out["summary"]["suggested"] == len(cards) >= 5


def test_no_suggestion_binds_a_column_of_another_table(overlay_conn, ftr_catalog):
    """No cross-table leakage: every operand of every card belongs to the table that was asked for.
    There is no governed join in this catalog, so a cross-table operand would be a bug — and under
    catalog-wide grounding it happened (a candidate grained on one table reaching for another's
    measure, which the gauntlet then rejected NO_JOIN_PATH against the wrong table)."""
    for table in (TABLE, OTHER_TABLE, SIBLING_TABLE):
        out = suggest_features_for_table(overlay_conn, catalog_source=SOURCE, table=table)
        assert out["summary"]["suggested"] >= 1, table
        assert all(ref.startswith(f"public.{table}.") for ref in _uses(out)), table
        assert {s["grain_table"] for g in out["groups"] for s in g["suggestions"]} == {table}


def test_no_table_shows_the_empty_screen_any_more(overlay_conn, ftr_catalog):
    """The measurement's headline number: every governed table in this catalog now suggests
    something, where catalog-wide grounding left the sibling with nothing at all."""
    per_table = {t: suggest_features_for_table(
        overlay_conn, catalog_source=SOURCE, table=t)["summary"]["suggested"]
        for t in (TABLE, OTHER_TABLE, SIBLING_TABLE)}
    assert all(per_table.values()), per_table
    assert sum(per_table.values()) > len(_catalog_wide_ideas(overlay_conn))


def test_the_feature_generation_path_is_still_catalog_wide(overlay_conn, ftr_catalog):
    """The SCOPE guarantee. ``_template_candidates`` is also the engine behind the hypothesis-driven
    feature-generation flow (Workbench -> considered set), which answers a different question — what
    can this CATALOG produce — so the narrowing must be OPT-IN. Called the way that flow calls it,
    the pass is exactly what it was: still one candidate per template, still starving the sibling."""
    default = _template_candidates(overlay_conn, catalog_source=SOURCE, roles=(),
                                   target_ref=None, now=None)
    explicit = _template_candidates(overlay_conn, catalog_source=SOURCE, roles=(),
                                    target_ref=None, now=None, table=None)
    assert [(i.name, i.grain_table, i.recipe_id) for i in default[0]] == [
        (i.name, i.grain_table, i.recipe_id) for i in explicit[0]]
    assert default[1:] == explicit[1:]          # rejections + every id/context map the flow consumes
    grains = {i.grain_table for i in default[0]}
    assert TABLE in grains and SIBLING_TABLE not in grains
    # one candidate per template — the catalog-wide invariant this change must not touch
    assert len({i.recipe_id for i in default[0]}) == len(default[0])


# ── widening the grounding set across a CLEARING join ────────────────────────────────────────────
# Per-table grounding fixed starvation but could no longer produce a cross-table candidate AT ALL —
# not even one a governed join legitimately authorises. The widening reuses `join_path`'s own rule
# (`clearing_reachable_tables`), so the columns a screen may reach and the gauntlet's JOIN_CONNECTIVITY
# disposition are decided by ONE piece of machinery and cannot drift apart.
@pytest.fixture
def join_catalog(overlay_conn):
    """Two tables, split so that the interesting feature is ONLY buildable across the join."""
    _seal()
    _ingest(overlay_conn, _JOIN_SOURCE, _JOIN_COLUMNS)
    _govern_table_facts(overlay_conn, _MEASURE_TABLE, "ledger_acct", "ledger_dt",
                        source=_JOIN_SOURCE)
    _govern_table_facts(overlay_conn, _ENTITY_TABLE, "master_cif", "master_dt",
                        source=_JOIN_SOURCE)
    return _Catalog(source=_JOIN_SOURCE, table=_MEASURE_TABLE)


def _join_edge(conn, *, fact_key: str | None, status: str | None) -> None:
    """One operational `joins` edge between the two tables. ``fact_key=None`` is a FILE-DECLARED edge;
    a fact key with ``VERIFIED`` is a governed-verified one — `join_path` treats both as CLEARING."""
    conn.execute(
        "INSERT INTO graph_edge (catalog_source, kind, from_ref, to_ref, cardinality, authority, "
        "approved_join_fact_key, approved_join_status) "
        "VALUES (%s, 'joins', %s, %s, 'N:1', 'operational', %s, %s)",
        (_JOIN_SOURCE, _JOIN_FROM, _JOIN_TO, fact_key, status))


def _screen(conn, table: str = _MEASURE_TABLE, roles=()) -> dict:
    return suggest_features_for_table(conn, catalog_source=_JOIN_SOURCE, table=table, roles=roles)


def test_a_verified_join_widens_the_grounding_set_across_tables(overlay_conn, join_catalog):
    """THE HEADLINE. ``balance_trend_90d`` needs a customer key and a balance. The balance lives on
    ``txn_ledger``, the customer key only on ``cust_master`` — so on the ledger's screen the recipe is
    UNBUILDABLE under per-table grounding. A VERIFIED join makes ``cust_master`` reachable, the
    grounding set widens to both tables' columns, and the card appears with operands SPANNING both."""
    before = _screen(overlay_conn)
    assert "balance_trend_90d" not in _names(before)              # per-table: invisible
    assert all(ref.startswith(f"public.{_MEASURE_TABLE}.") for ref in _uses(before))

    _join_edge(overlay_conn, fact_key="ajf-verified", status="VERIFIED")
    after = _screen(overlay_conn)
    assert _names(before) < _names(after)                          # strictly more, nothing lost
    cards = {s["name"]: s for g in after["groups"] for s in g["suggestions"]}
    card = cards["balance_trend_90d"]
    # the SPECIFIC bound columns span both tables — a mere non-empty count would not show this
    assert f"public.{_MEASURE_TABLE}.bal_amt" in card["uses"]
    assert f"public.{_ENTITY_TABLE}.master_cif" in card["uses"]
    # and it clears the join gauntlet outright — a VERIFIED path raises no external-validation need
    assert "JOIN_CONNECTIVITY" not in {r["code"] for r in card["requirements"]}
    # every card still touches THIS table: a purely cust_master candidate is not the ledger's card
    for s in cards.values():
        assert any(ref.startswith(f"public.{_MEASURE_TABLE}.") for ref in s["uses"]), s["name"]


def test_a_file_declared_join_also_widens(overlay_conn, join_catalog):
    """`join_path`'s clearing rule is ``fact_key is None or status == 'VERIFIED'`` — a file-declared
    edge clears too, and the widening must use that rule, not a re-implementation of half of it."""
    _join_edge(overlay_conn, fact_key=None, status=None)
    assert "balance_trend_90d" in _names(_screen(overlay_conn))


def test_an_unverified_join_does_not_widen(overlay_conn, join_catalog):
    """The SCOPE DECISION. An authorized-but-unverified edge would re-admit exactly the candidates
    per-table grounding removed — they would surface carrying a JOIN_CONNECTIVITY requirement or be
    rejected NO_JOIN_PATH. So it does not widen, and the screen is byte-identical to no join at all."""
    before = _screen(overlay_conn)
    _join_edge(overlay_conn, fact_key="ajf-draft", status="DRAFT")
    after = _screen(overlay_conn)
    assert after == before                                        # not one card, not one rejection
    assert "balance_trend_90d" not in _names(after)
    assert not [r for r in after["rejections"]
                if r["code"] in ("NO_JOIN_PATH", "JOIN_DENIED")]
    assert not [s for g in after["groups"] for s in g["suggestions"]
                if "JOIN_CONNECTIVITY" in {r["code"] for r in s["requirements"]}]


def test_no_join_leaves_the_per_table_screen_exactly_as_it_was(overlay_conn, join_catalog):
    """No join edge at all -> today's per-table behaviour, unchanged: every operand is this table's."""
    out = _screen(overlay_conn)
    assert out["summary"]["suggested"] >= 1
    assert all(ref.startswith(f"public.{_MEASURE_TABLE}.") for ref in _uses(out))
    assert {s["grain_table"] for g in out["groups"] for s in g["suggestions"]} == {_MEASURE_TABLE}


def test_a_caller_who_cannot_see_the_join_endpoint_gets_no_widening(overlay_conn, join_catalog):
    """READ-SCOPING — the critical risk. The widening reuses `classify_join_path`'s visibility rule:
    an edge is usable only when BOTH endpoint columns are visible under the caller's read scope. Hide
    ONE endpoint and the blind caller must reach NOTHING of the far table — not merely miss the hidden
    column — while the privileged caller crosses the same edge."""
    overlay_conn.execute(
        "UPDATE graph_node SET sensitivity = 'restricted' "
        "WHERE catalog_source = %s AND object_ref = %s", (_JOIN_SOURCE, _JOIN_TO))
    _join_edge(overlay_conn, fact_key="ajf-verified", status="VERIFIED")

    blind = _screen(overlay_conn, roles=())
    privileged = _screen(overlay_conn, roles=("restricted_reader",))

    # the blind caller: no far-table column at all, and certainly not the restricted endpoint
    assert all(ref.startswith(f"public.{_MEASURE_TABLE}.") for ref in _uses(blind)), _uses(blind)
    assert _JOIN_TO not in _uses(blind)
    assert "balance_trend_90d" not in _names(blind)
    # the privileged caller crosses it — non-vacuous proof the fixture really does widen
    assert "balance_trend_90d" in _names(privileged)
    assert f"public.{_ENTITY_TABLE}.master_cif" in _uses(privileged)


def test_widening_never_binds_a_column_the_caller_could_not_already_see(overlay_conn, join_catalog):
    """The general statement: whatever the widening admits is a SUBSET of this caller's own read-scoped
    column list. A join can only ever make a VISIBLE column reachable — never make one visible."""
    _join_edge(overlay_conn, fact_key="ajf-verified", status="VERIFIED")
    overlay_conn.execute(
        "UPDATE graph_node SET sensitivity = 'pii' "
        "WHERE catalog_source = %s AND object_ref = %s",
        (_JOIN_SOURCE, f"public.{_ENTITY_TABLE}.master_cif"))
    for roles in ((), ("pii_reader",)):
        visible = {c.object_ref
                   for c in templates_module._load_columns(overlay_conn, _JOIN_SOURCE, roles)}
        for table in (_MEASURE_TABLE, _ENTITY_TABLE):
            assert _uses(_screen(overlay_conn, table=table, roles=roles)) <= visible, (roles, table)


# ── Task 0C defect 2: table EXISTENCE is a read-scoped fact. `_resolve_table` used to treat the
# world-visible table node as proof, so a caller whose scope could see NO column of a table still
# learned the table exists through `table_known` (and would then see its rejections, neighbourhood
# metadata and counts). Existence now derives from at least one caller-VISIBLE column
# (`visible_requires`), like every other read on this surface. ──────────────────────────────────


def _hide_every_column(conn, table: str) -> None:
    conn.execute(
        "UPDATE graph_node SET sensitivity = 'restricted' "
        "WHERE catalog_source = %s AND kind = 'column' AND table_name = %s",
        (_JOIN_SOURCE, table))


def test_an_all_hidden_table_does_not_exist_for_the_blind_caller(overlay_conn, join_catalog):
    """The all-hidden fixture. Every column of ``cust_master`` is restricted: to a caller with no
    reader roles the table must be INDISTINGUISHABLE from one this catalog does not hold — the
    byte-identical unknown-table payload, so nothing leaks through ``table_known``, rejections,
    neighbourhood metadata or counts. The restricted fixture is the same probe under
    ``restricted_reader``: the table exists again, through the same door."""
    _hide_every_column(overlay_conn, _ENTITY_TABLE)
    blind = _screen(overlay_conn, table=_ENTITY_TABLE, roles=())
    assert blind["table_known"] is False
    unknown = _screen(overlay_conn, table="no_such_table", roles=())
    assert blind == {**unknown, "table": _ENTITY_TABLE}   # only the echoed request string differs
    privileged = _screen(overlay_conn, table=_ENTITY_TABLE, roles=("restricted_reader",))
    assert privileged["table_known"] is True
    assert privileged["table"] == _ENTITY_TABLE


def test_the_schema_qualified_ref_of_an_all_hidden_table_is_also_unknown(overlay_conn,
                                                                         join_catalog):
    """The deep-link spelling must not be a side door: probing the table's own ``object_ref``
    (``public.cust_master``) reveals exactly as little as the bare name — and the unknown payload
    echoes the caller's requested string verbatim, per the frozen two-case rule (0F-11)."""
    _hide_every_column(overlay_conn, _ENTITY_TABLE)
    out = _screen(overlay_conn, table=f"public.{_ENTITY_TABLE}", roles=())
    assert out["table_known"] is False
    assert out["table"] == f"public.{_ENTITY_TABLE}"


def test_one_visible_column_keeps_the_table_resolving_exactly_as_before(overlay_conn,
                                                                        join_catalog):
    """The public/restricted split, and the compatibility half of the fix: hide every column BUT one
    untagged (public) column, and the no-roles caller still resolves the table — both spellings,
    same resolved bare name. A caller who can see at least one column sees no behaviour change."""
    overlay_conn.execute(
        "UPDATE graph_node SET sensitivity = 'restricted' "
        "WHERE catalog_source = %s AND kind = 'column' AND table_name = %s "
        "AND column_name <> 'master_dt'",
        (_JOIN_SOURCE, _ENTITY_TABLE))
    bare = _screen(overlay_conn, table=_ENTITY_TABLE, roles=())
    qualified = _screen(overlay_conn, table=f"public.{_ENTITY_TABLE}", roles=())
    assert bare["table_known"] is True and bare["table"] == _ENTITY_TABLE
    assert qualified["table_known"] is True and qualified["table"] == _ENTITY_TABLE


def test_the_catalog_wide_path_never_widens(overlay_conn, join_catalog):
    """The SCOPE guarantee for the feature-generation flow: widening is meaningless without an anchor
    table, so the catalog-wide default is inert to it — argument-for-argument the pass it always was."""
    _join_edge(overlay_conn, fact_key="ajf-verified", status="VERIFIED")
    default = _template_candidates(overlay_conn, catalog_source=_JOIN_SOURCE, roles=(),
                                   target_ref=None, now=None)
    inert = _template_candidates(overlay_conn, catalog_source=_JOIN_SOURCE, roles=(),
                                 target_ref=None, now=None, table=None,
                                 also_tables=(_MEASURE_TABLE, _ENTITY_TABLE))
    assert [(i.name, i.grain_table, i.recipe_id) for i in default[0]] == [
        (i.name, i.grain_table, i.recipe_id) for i in inert[0]]
    assert default[1:] == inert[1:]


def test_the_widened_screen_still_writes_nothing(overlay_conn, join_catalog):
    """Widening adds ONE read (the join-edge fetch) and no write — including to ``graph_edge``."""
    _join_edge(overlay_conn, fact_key="ajf-verified", status="VERIFIED")
    tables = ("graph_node", "graph_edge", "field_decision_event", "contract_intent")
    def fingerprint():
        return tuple(overlay_conn.execute(
            f"SELECT count(*), md5(coalesce(string_agg(t::text, '|' ORDER BY t::text), '')) "
            f"FROM {t} t").fetchone() for t in tables)
    before = fingerprint()
    assert _screen(overlay_conn)["summary"]["suggested"] >= 1
    assert fingerprint() == before


def test_one_page_view_still_reads_the_catalog_columns_once(overlay_conn, join_catalog, monkeypatch):
    """Widening must not reintroduce a second grounding pass: the read-scoped column list is still
    loaded ONCE per page view, and the join neighbourhood is resolved in ONE further statement."""
    _join_edge(overlay_conn, fact_key="ajf-verified", status="VERIFIED")
    calls: list[tuple[str, tuple[str, ...]]] = []
    real = templates_module._load_columns

    def _spy(conn, catalog_source, roles):
        calls.append((catalog_source, tuple(roles)))
        return real(conn, catalog_source, roles)

    monkeypatch.setattr(templates_module, "_load_columns", _spy)
    _screen(overlay_conn)
    assert calls == [(_JOIN_SOURCE, ())]


def _idea(**kw) -> FeatureIdea:
    """A bare FeatureIdea carrying only the typed computation operands the renderer reads."""
    return FeatureIdea(name="f", description="", derives_from=[], aggregation=None,
                       grain_table=TABLE, **kw)


def test_recipe_renders_the_real_operation_label_not_an_invented_sql_verb():
    """``operation_kind`` is a DOMAIN label (~152 values: trend / inflow_outflow / frequency_trend),
    NOT a SQL verb. Printing ``AVG(...)`` for an operation the system calls ``trend`` would be a
    confident-looking invention, so the line carries the label the engine actually bound."""
    line = render_recipe(_idea(
        operation_kind="trend_90d",
        measure_refs=((SOURCE, f"public.{TABLE}.bal_amt"), (SOURCE, f"public.{TABLE}.cif_id"),
                      (SOURCE, f"public.{TABLE}.as_of_dt")),
        grain_ref=(SOURCE, f"public.{TABLE}.cif_id"),
        time_ref=(SOURCE, f"public.{TABLE}.as_of_dt"), window="90d"), "")
    assert line == "trend_90d(bal_amt) BY cif_id OVER 90d [as_of_dt]"


def test_recipe_does_not_render_the_grain_or_time_column_as_a_measure():
    """``measure_refs`` carries EVERY bound pair, grain and point-in-time included. A card that lists
    the grain column as a measure claims the feature aggregates its own key."""
    idea = _idea(operation_kind="inflow_outflow_30d",
                 measure_refs=((SOURCE, f"public.{TABLE}.cif_id"),
                               (SOURCE, f"public.{TABLE}.txn_amt"),
                               (SOURCE, f"public.{TABLE}.as_of_dt")),
                 grain_ref=(SOURCE, f"public.{TABLE}.cif_id"),
                 time_ref=(SOURCE, f"public.{TABLE}.as_of_dt"), window="30d")
    assert render_recipe(idea, "") == "inflow_outflow_30d(txn_amt) BY cif_id OVER 30d [as_of_dt]"


def test_recipe_omits_the_clauses_that_do_not_apply():
    """An idea with no window and no point-in-time column must render cleanly — no dangling ``OVER``,
    no empty ``[]``, no trailing space."""
    line = render_recipe(_idea(
        operation_kind="product_breadth",
        measure_refs=((SOURCE, f"public.{TABLE}.setl_stat"), (SOURCE, f"public.{TABLE}.cif_id")),
        grain_ref=(SOURCE, f"public.{TABLE}.cif_id")), "")
    assert line == "product_breadth(setl_stat) BY cif_id"
    assert "OVER" not in line and "[" not in line
    assert line == line.strip()


def test_recipe_renders_multiple_measures_in_a_stable_order():
    """Two real measures, rendered in the order the engine bound them — same idea, same line."""
    idea = _idea(operation_kind="payment_ratio",
                 measure_refs=((SOURCE, f"public.{TABLE}.txn_amt"),
                               (SOURCE, f"public.{TABLE}.bal_amt"),
                               (SOURCE, f"public.{TABLE}.cif_id")),
                 grain_ref=(SOURCE, f"public.{TABLE}.cif_id"))
    assert render_recipe(idea, "") == "payment_ratio(txn_amt, bal_amt) BY cif_id"
    assert render_recipe(idea, "") == render_recipe(idea, "")


def test_every_card_carries_a_recipe_and_its_parts(overlay_conn, ftr_catalog):
    """The card's recipe line and the structured pieces behind it — on EVERY suggestion, from the
    real grounded engine, not a hand-built idea."""
    out = suggest_features_for_table(
        overlay_conn, catalog_source=ftr_catalog.source, table=ftr_catalog.table)
    cards = [s for g in out["groups"] for s in g["suggestions"]]
    assert cards
    for s in cards:
        assert s["recipe"] and s["recipe"] == s["recipe"].strip()
        parts = s["recipe_parts"]
        assert parts["operation"] and parts["measures"]
        assert parts["grain"] not in parts["measures"]      # the grain is never a measure
        assert parts["time"] not in parts["measures"]
        if not parts["window"]:
            assert "OVER" not in s["recipe"]
        if not parts["time"]:
            assert "[" not in s["recipe"]


def test_the_recipe_by_clause_names_the_cards_own_entity(overlay_conn, ftr_catalog):
    """The heading and the line must agree. The card's entity is the recipe's BOUND entity, but the
    ``BY`` clause was taken from the TABLE grain — so an account-grained card read "per account" over
    ``custody_holding_dynamics_90d(cust_hold, acct_id) BY cif_id ...``, naming a key it is not
    computed per AND listing its own key as a measure. The account group is the proof: its entity
    (``acct_id``) is NOT the table's single ``is_grain`` column (``cif_id``)."""
    out = suggest_features_for_table(
        overlay_conn, catalog_source=ftr_catalog.source, table=ftr_catalog.table)
    account = next(g for g in out["groups"] if g["entity_label"] == "account")
    assert "custody_holding_dynamics_90d" in {s["name"] for s in account["suggestions"]}
    for group in out["groups"]:
        column = group["entity_ref"].rsplit(".", 1)[-1]
        for s in group["suggestions"]:
            assert s["recipe_parts"]["grain"] == column
            assert f" BY {column}" in s["recipe"]
            assert column not in s["recipe_parts"]["measures"]


def test_writes_nothing(overlay_conn, ftr_catalog):
    """v1 is strictly read-only — the load-bearing guarantee. A row COUNT cannot see an IN-PLACE write
    (``UPDATE graph_node SET is_grain = false`` keeps the cardinality), so fingerprint the row CONTENT."""
    tables = ("field_evidence", "field_decision_event", "graph_node", "contract_intent")
    def fingerprint():
        return tuple(overlay_conn.execute(
            f"SELECT count(*), md5(coalesce(string_agg(t::text, '|' ORDER BY t::text), '')) "
            f"FROM {t} t").fetchone() for t in tables)
    before = fingerprint()
    suggest_features_for_table(overlay_conn, catalog_source=ftr_catalog.source,
                               table=ftr_catalog.table)
    assert fingerprint() == before


# ── the ONE-HOP CAP: the widening's resource bound ───────────────────────────────────────────────
# The widening above was correct on a small catalog and UNBOUNDED on a large one: it walked the
# clearing-join graph TRANSITIVELY (a full BFS closure), and on a real catalog almost every table
# reaches the customer/account hub — so opening a hub table ground the registry against most of the
# catalog. That is not a slow page, it is a page with NO PREDICTABLE RESOURCE BOUND. The cap is
# therefore a DEFECT fix, not an optimisation: one hop, a table cap and a column budget inside that
# hop, a deterministic order before truncating, and honest metadata saying what was left out.
_HUB_SOURCE = "p4_suggestions_hub_ftr"
_HUB_TABLE = "cust_hub"
_FAR_TABLE = "far_custody"                     # TWO hops out — joined to a spoke, never to the hub
_SPOKES = tuple(f"spoke_{n:02d}" for n in range(40))     # >> MAX_NEIGHBOUR_TABLES, on purpose
_SPOKE_COLUMNS = 5

_HUB_COLUMNS: dict[str, dict] = {
    _HUB_TABLE: {
        "CIF_ID": ("customer_id", "varchar", "Hub Customer Identifier"),
        "ACCT_ID": ("account_id", "varchar", "Hub Account Identifier"),
        "AS_OF_DT": ("as_of_date", "date", "Hub As Of Date"),
        "TXN_TS": ("event_timestamp", "timestamp", "Hub Event Timestamp"),
        "TXN_CNT": ("count", "integer", "Hub Transaction Count"),
    },
}
for _i, _spoke in enumerate(_SPOKES):
    _HUB_COLUMNS[_spoke] = {
        f"S{_i:02d}_ACCT": ("account_id", "varchar", f"Spoke {_i} Account"),
        f"S{_i:02d}_AMT": ("monetary_flow", "decimal", f"Spoke {_i} Amount"),
        f"S{_i:02d}_DT": ("as_of_date", "date", f"Spoke {_i} As Of Date"),
        f"S{_i:02d}_CNT": ("count", "integer", f"Spoke {_i} Count"),
        f"S{_i:02d}_TS": ("event_timestamp", "timestamp", f"Spoke {_i} Timestamp"),
    }
_HUB_COLUMNS[_FAR_TABLE] = {
    "FAR_ACCT": ("account_id", "varchar", "Far Account"),
    "FAR_BAL": ("monetary_stock", "decimal", "Far Balance"),
}


def _clearing_edge(conn, source: str, from_ref: str, to_ref: str) -> None:
    """A FILE-DECLARED operational `joins` edge — `join_path`'s clearing rule, no fact needed."""
    conn.execute(
        "INSERT INTO graph_edge (catalog_source, kind, from_ref, to_ref, cardinality, authority, "
        "approved_join_fact_key, approved_join_status) "
        "VALUES (%s, 'joins', %s, %s, 'N:1', 'operational', NULL, NULL)",
        (source, from_ref, to_ref))


@pytest.fixture
def hub_catalog(overlay_conn):
    """A HUB: 40 tables joined DIRECTLY to it, and one table two hops out behind the first spoke.

    The hub's own grain/availability go through the REAL governance path. The spokes' ``is_grain`` /
    ``is_as_of`` flags are set DIRECTLY — they are the projected OUTCOME of that same path, and this
    fixture needs 40 of them purely to be COST-REPRESENTATIVE (an is_grain candidate column is what
    makes grounding issue an ``effective_entity`` query per need, which is the cost this cap bounds).
    Governing 40 tables through propose/confirm/project would make a cost fixture minutes long and
    would assert nothing this suite does not already assert elsewhere."""
    _seal()
    _ingest(overlay_conn, _HUB_SOURCE, _HUB_COLUMNS)
    _govern_table_facts(overlay_conn, _HUB_TABLE, "cif_id", "as_of_dt", source=_HUB_SOURCE)
    for i, spoke in enumerate(_SPOKES):
        overlay_conn.execute(
            "UPDATE graph_node SET is_grain = true WHERE catalog_source = %s AND object_ref = %s",
            (_HUB_SOURCE, f"public.{spoke}.s{i:02d}_acct"))
        overlay_conn.execute(
            "UPDATE graph_node SET is_as_of = true WHERE catalog_source = %s AND object_ref = %s",
            (_HUB_SOURCE, f"public.{spoke}.s{i:02d}_dt"))
        _clearing_edge(overlay_conn, _HUB_SOURCE,
                       f"public.{_HUB_TABLE}.acct_id", f"public.{spoke}.s{i:02d}_acct")
    overlay_conn.execute(
        "UPDATE graph_node SET is_grain = true WHERE catalog_source = %s AND object_ref = %s",
        (_HUB_SOURCE, f"public.{_FAR_TABLE}.far_acct"))
    _clearing_edge(overlay_conn, _HUB_SOURCE,
                   f"public.{_SPOKES[0]}.s00_acct", f"public.{_FAR_TABLE}.far_acct")
    return _Catalog(source=_HUB_SOURCE, table=_HUB_TABLE)


def _statements(conn, monkeypatch) -> list[int]:
    """Count EVERY statement this connection issues — the page's real resource cost."""
    cls = type(conn)
    real = cls.execute
    counter = [0]

    def _exec(self, *a, **kw):
        counter[0] += 1
        return real(self, *a, **kw)

    monkeypatch.setattr(cls, "execute", _exec)
    return counter


def _grounding_set(monkeypatch) -> list[list]:
    """Capture the COLUMNS actually handed to grounding — the literal grounding set, not a proxy."""
    seen: list[list] = []
    real = templates_module.ground_template_outcome

    def _spy(conn, template, *, columns=None, **kw):
        if columns is not None and not seen:
            seen.append(list(columns))
        return real(conn, template, columns=columns, **kw)

    monkeypatch.setattr(templates_module, "ground_template_outcome", _spy)
    return seen


def _hub(conn, **kw) -> dict:
    return suggest_features_for_table(conn, catalog_source=_HUB_SOURCE, table=_HUB_TABLE, **kw)


#: The concrete ceilings these two pages must stay under.
#:
#: HUB, measured on this fixture: 6_284 statements capped, against 12_710 with the bounds lifted —
#: and the capped figure did NOT move when the fixture grew from 24 direct neighbours to 40, which is
#: the property being bought. The uncapped one rises with every join anyone ever adds.
#: ORDINARY (the two-table `join_catalog`): 813 statements, against 812 before this cap existed —
#: the ONE extra statement is the aggregate that prices the column budget.
#: Both carry a little headroom so an unrelated one-off query does not fail them.
_HUB_STATEMENT_CEILING = 7_000
_ORDINARY_STATEMENT_CEILING = 850


def test_a_hub_tables_page_has_a_bounded_statement_cost(overlay_conn, hub_catalog, monkeypatch):
    """THE DEFECT. 40 tables join straight to this hub and a 41st sits behind one of them. Opening the
    hub used to ground against ALL of them — the closure, whose size is a property of the CATALOG, not
    of the request. Now the page grounds against the hub plus at most ``MAX_NEIGHBOUR_TABLES`` of its
    DIRECT neighbours, so its cost has a ceiling that a new join cannot raise."""
    counter = _statements(overlay_conn, monkeypatch)
    counter[0] = 0
    out = _hub(overlay_conn)
    assert counter[0] <= _HUB_STATEMENT_CEILING, counter[0]
    # non-vacuous: the unbounded closure really would have reached every table in this catalog
    assert len(clearing_reachable_tables(overlay_conn, _HUB_SOURCE, _HUB_TABLE)) == len(_SPOKES) + 2
    assert out["neighbourhood"]["tables_considered"] == join_path.MAX_NEIGHBOUR_TABLES


def test_removing_the_cap_is_what_costs_the_statements(overlay_conn, hub_catalog, monkeypatch):
    """The cap is load-bearing, not incidental: with the SAME fixture and the SAME request, lifting
    the bounds costs materially more — and the gap grows with the catalog, which is the whole defect."""
    counter = _statements(overlay_conn, monkeypatch)
    counter[0] = 0
    _hub(overlay_conn)
    capped = counter[0]
    monkeypatch.setattr(join_path, "MAX_NEIGHBOUR_TABLES", 10_000)
    monkeypatch.setattr(join_path, "MAX_COLUMNS_CONSIDERED", 10_000_000)
    counter[0] = 0
    _hub(overlay_conn, max_hops=99)
    assert counter[0] > capped * 1.9, (capped, counter[0])


def test_a_two_hop_tables_columns_are_not_considered(overlay_conn, hub_catalog, monkeypatch):
    """ONE HOP. ``far_custody`` is reachable — over a spoke — and lexically sorts FIRST among the
    neighbours, so nothing but the hop bound keeps it out. Asserted on the literal column list handed
    to grounding, because a card-level assertion could pass for the wrong reason."""
    seen = _grounding_set(monkeypatch)
    _hub(overlay_conn)
    tables = {col.table for col in seen[0]}
    assert _FAR_TABLE not in tables
    assert _HUB_TABLE in tables
    assert tables == {_HUB_TABLE, *_SPOKES[:join_path.MAX_NEIGHBOUR_TABLES]}


def test_the_kept_neighbours_are_the_documented_order_and_are_stable(overlay_conn, hub_catalog,
                                                                     monkeypatch):
    """DETERMINISM. Truncation keeps neighbours by (hop distance, then table name) — never by DB row
    order — so the same request always shows the same tables. Asserted BOTH ways: the kept set is
    exactly the documented prefix, and two identical requests agree."""
    seen = _grounding_set(monkeypatch)
    first = _hub(overlay_conn)
    kept = sorted({col.table for col in seen[0]} - {_HUB_TABLE})
    assert kept == list(_SPOKES[:join_path.MAX_NEIGHBOUR_TABLES])
    seen.clear()
    second = _hub(overlay_conn)
    assert sorted({col.table for col in seen[0]}) == sorted(kept + [_HUB_TABLE])
    assert first == second


def test_the_metadata_tells_the_truth_when_truncated(overlay_conn, hub_catalog):
    """The screen may not silently show a subset. 24 neighbours are reachable in one hop, 20 were
    used, and the reason is the table cap — every field is the reality, not a constant."""
    meta = _hub(overlay_conn)["neighbourhood"]
    assert meta == {"tables_considered": join_path.MAX_NEIGHBOUR_TABLES,
                    "tables_available": len(_SPOKES),
                    "truncated": True, "max_hops": 1, "limit_reason": "table_cap"}


def test_the_column_budget_can_bite_before_the_table_cap(overlay_conn, hub_catalog, monkeypatch):
    """A SINGLE wide neighbour can blow the budget on its own, so the tables cap alone is not a
    resource bound. With the budget lowered, fewer tables are admitted and the metadata names the
    budget — not the table cap — as the constraint that bit."""
    monkeypatch.setattr(join_path, "MAX_COLUMNS_CONSIDERED",
                        len(_HUB_COLUMNS[_HUB_TABLE]) + 11 * _SPOKE_COLUMNS)
    meta = _hub(overlay_conn)["neighbourhood"]
    assert meta["tables_considered"] == 11
    assert meta["tables_available"] == len(_SPOKES)
    assert meta["truncated"] is True and meta["limit_reason"] == "column_budget"


def test_the_metadata_is_honest_when_nothing_was_truncated(overlay_conn, join_catalog):
    """The untruncated case is a claim too: one reachable neighbour, one used, nothing left out."""
    _join_edge(overlay_conn, fact_key="ajf-verified", status="VERIFIED")
    assert _screen(overlay_conn)["neighbourhood"] == {
        "tables_considered": 1, "tables_available": 1,
        "truncated": False, "max_hops": 1, "limit_reason": None}


def test_an_ordinary_tables_page_costs_what_it_did(overlay_conn, join_catalog, monkeypatch):
    """The other half of the contract: an ordinary table's small neighbourhood is NOT changed by the
    cap. Its statement count moves only by the ONE aggregate that prices the neighbourhood."""
    counter = _statements(overlay_conn, monkeypatch)
    _join_edge(overlay_conn, fact_key="ajf-verified", status="VERIFIED")
    counter[0] = 0
    widened = _screen(overlay_conn)
    with_join = counter[0]
    assert "balance_trend_90d" in _names(widened)          # still widens across the one join
    assert with_join <= _ORDINARY_STATEMENT_CEILING, with_join
    assert widened["neighbourhood"]["truncated"] is False


def test_a_caller_who_cannot_see_a_neighbour_never_has_it_counted(overlay_conn, join_catalog):
    """READ-SCOPING IS UNTOUCHABLE, and the metadata must not leak either: a caller who cannot see the
    join's endpoint must not learn from ``tables_available`` that a neighbour is there at all."""
    overlay_conn.execute(
        "UPDATE graph_node SET sensitivity = 'restricted' "
        "WHERE catalog_source = %s AND object_ref = %s", (_JOIN_SOURCE, _JOIN_TO))
    _join_edge(overlay_conn, fact_key="ajf-verified", status="VERIFIED")

    blind = _screen(overlay_conn, roles=())["neighbourhood"]
    privileged = _screen(overlay_conn, roles=("restricted_reader",))["neighbourhood"]
    assert blind["tables_available"] == 0 and blind["tables_considered"] == 0
    assert blind["truncated"] is False
    assert privileged["tables_available"] == 1 and privileged["tables_considered"] == 1


def test_truncation_only_ever_narrows_what_a_caller_can_see(overlay_conn, hub_catalog, monkeypatch):
    """The cap may only NARROW. Whatever survives truncation — at any budget, for any caller — is a
    SUBSET of the columns that caller could already see, and tightening the budget can only ever
    remove columns, never add or substitute one the caller may not read."""
    visible = {c.object_ref for c in templates_module._load_columns(overlay_conn, _HUB_SOURCE, ())}
    kept: list[set] = []
    for budget in (10_000, 60, 20):
        monkeypatch.setattr(join_path, "MAX_COLUMNS_CONSIDERED", budget)
        seen = _grounding_set(monkeypatch)
        _hub(overlay_conn)
        columns = {col.object_ref for col in seen[0]}
        assert columns <= visible                        # never more than the read scope allows
        kept.append(columns)
    assert kept[0] > kept[1] > kept[2]                   # tightening strictly removes, never swaps


def test_an_explicit_max_hops_reaches_further_than_the_page_default(overlay_conn, hub_catalog,
                                                                    monkeypatch):
    """NOT a kill switch. A DELIBERATE request may ask for a wider neighbourhood — the opt-in changes
    which tables are ELIGIBLE, never how many are admitted, so the budget still bounds the cost.
    (The table cap is raised here only because 40 one-hop spokes would otherwise fill it before the
    second hop is reached — which is the documented order doing its job.)"""
    assert _hub(overlay_conn)["neighbourhood"]["tables_available"] == len(_SPOKES)
    assert _hub(overlay_conn, max_hops=2)["neighbourhood"]["tables_available"] == len(_SPOKES) + 1

    monkeypatch.setattr(join_path, "MAX_NEIGHBOUR_TABLES", len(_SPOKES) + 1)
    seen = _grounding_set(monkeypatch)
    _hub(overlay_conn, max_hops=2)
    assert _FAR_TABLE in {col.table for col in seen[0]}
    assert _hub(overlay_conn, max_hops=2)["neighbourhood"]["max_hops"] == 2


def test_the_cap_writes_nothing_and_leaves_the_catalog_wide_path_alone(overlay_conn, hub_catalog):
    """Two pins in one: the capped page is still strictly read-only, and the catalog-wide
    feature-generation pass — which never widened — is argument-for-argument what it always was."""
    tables = ("graph_node", "graph_edge", "field_decision_event", "contract_intent")

    def fingerprint():
        return tuple(overlay_conn.execute(
            f"SELECT count(*), md5(coalesce(string_agg(t::text, '|' ORDER BY t::text), '')) "
            f"FROM {t} t").fetchone() for t in tables)

    before = fingerprint()
    _hub(overlay_conn)
    assert fingerprint() == before
    default = _template_candidates(overlay_conn, catalog_source=_HUB_SOURCE, roles=(),
                                   target_ref=None, now=None)
    explicit = _template_candidates(overlay_conn, catalog_source=_HUB_SOURCE, roles=(),
                                    target_ref=None, now=None, table=None)
    assert [(i.name, i.grain_table, i.recipe_id) for i in default[0]] == [
        (i.name, i.grain_table, i.recipe_id) for i in explicit[0]]
    assert default[1:] == explicit[1:]


def test_the_unbounded_closure_helper_still_answers_its_own_question(overlay_conn, hub_catalog):
    """``clearing_reachable_tables`` keeps its contract for any deliberate caller: the FULL transitive
    closure, in ONE statement. It is simply no longer what an automatic page load uses."""
    reachable = clearing_reachable_tables(overlay_conn, _HUB_SOURCE, _HUB_TABLE)
    assert reachable == {_HUB_TABLE, _FAR_TABLE, *_SPOKES}
