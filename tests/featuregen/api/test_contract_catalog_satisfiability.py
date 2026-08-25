"""T5 at the route — the AML brief on ``cib`` is refused with directions, and leaves no trace.

The live run this pins is the one the 2026-08-24 audit dissected: an AML brief, ``catalog_source``
set to the customer master, ``ftr`` sitting unplanned beside it, 135 cards served and 0 kept. The
refusal is DATA (a typed code plus the facts); the rendering is T9's.

Placed with ``SEMANTIC_REQUIRES_CATALOG_SOURCE``'s tests on purpose: same route, same shape, same
law — a refused generation must leave no ``feature_generation_run`` and no
``confirmed_generation_scope`` behind, because an orphan pair reads to anyone auditing the store
like a generation that produced nothing.
"""
from __future__ import annotations

from datetime import UTC, datetime

from tests.featuregen.api._helpers import AUTH

from featuregen.intake.llm import FakeLLM, FakeResponse
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.enrich import content_hash
from featuregen.overlay.upload.graph import build_graph

CIB = "cib"
FTR = "ftr"
AML = "aml_cft.suspicious_transaction_monitoring"
HYPOTHESIS = "customers structure cash deposits below the reporting threshold over 90 days"


def _fake() -> FakeLLM:
    return FakeLLM(script={
        "overlay.feature.recommend_set": FakeResponse(output={
            "recommended_lens": "monetary", "reasoning": "fits"}),
        "overlay.feature.intents": FakeResponse(output={"intents": []}),
    })


def _watermark(conn, source: str) -> None:
    now = datetime.now(UTC)
    conn.execute(
        "INSERT INTO overlay_drift_watermark (catalog_source, last_completed_at, "
        "last_run_id, head_seq) VALUES (%s, %s, 'r', 0) "
        "ON CONFLICT (catalog_source) DO UPDATE SET last_completed_at = %s",
        (source, now, now))


def _two_catalogs(conn) -> None:
    """``cib`` — the customer master the run actually named: identity, one event timestamp that is
    a CONSENT date, attributes, and no monetary column. ``ftr`` — the transactions beside it."""
    cib = [
        (CanonicalRow(CIB, "customer", "cust_num", "integer", is_grain=True, entity="Customer"),
         "customer_id"),
        (CanonicalRow(CIB, "customer", "consent_ts", "timestamp"), "event_timestamp"),
        (CanonicalRow(CIB, "customer", "segment_code", "text"), "category_code"),
        (CanonicalRow(CIB, "customer", "cust_susp_flg", "boolean"), "boolean_flag"),
    ]
    ftr = [
        (CanonicalRow(FTR, "txn", "cust_num", "integer", is_grain=True, entity="Customer"),
         "customer_id"),
        (CanonicalRow(FTR, "txn", "amount", "numeric", additivity="additive", currency="USD"),
         "monetary_flow"),
        (CanonicalRow(FTR, "txn", "dc_flag", "text"), "debit_credit_indicator"),
        (CanonicalRow(FTR, "txn", "booked_ts", "timestamp"), "event_timestamp"),
        (CanonicalRow(FTR, "txn", "acct_ref", "integer", entity="Account"), "account_id"),
    ]
    build_graph(conn, CIB, [r for r, _ in cib], concepts={content_hash(r): c for r, c in cib})
    build_graph(conn, FTR, [r for r, _ in ftr], concepts={content_hash(r): c for r, c in ftr})
    _watermark(conn, CIB)
    _watermark(conn, FTR)


def _post(client, *, catalog_source: str):
    return client.post("/contract/considered-set", json={
        "hypothesis": HYPOTHESIS, "objective": "flag suspicious transaction behaviour",
        "catalog_source": catalog_source, "contract_version": 2,
        "confirmed_scope": {"primary": AML, "secondary": [], "expansion": "exact",
                            "confirmation_source": "user_confirmed"},
    }, headers=AUTH)


def _broaden(client, *, catalog_source: str):
    """The BROADEN action — the same route, the same confirmed-scope path, with
    ``unscoped: true``. ``v2_applicability`` fails OPEN on it, so the eligible corpus is the whole
    317-recipe registry and the floor is 158."""
    return client.post("/contract/considered-set", json={
        "hypothesis": HYPOTHESIS, "objective": "flag suspicious transaction behaviour",
        "catalog_source": catalog_source, "contract_version": 2,
        "confirmed_scope": {"unscoped": True, "confirmation_source": "user_broadened"},
    }, headers=AUTH)


def test_the_aml_brief_on_cib_is_refused_and_the_refusal_names_ftr(make_client, conn):
    """THE pin: the audit's exact arrangement answers with a typed refusal that names the
    unsatisfiable concept, how many eligible recipes need it, and where it lives."""
    _two_catalogs(conn)
    res = _post(make_client(_fake()), catalog_source=CIB)

    assert res.status_code == 422, res.text
    detail = res.json()["detail"]
    assert detail["code"] == "CATALOG_CANNOT_SATISFY_SCOPE"
    assert detail["catalog_source"] == CIB
    assert detail["eligible_recipes"] == 15
    assert detail["majority_floor"] == 7
    (unsatisfiable,) = detail["unsatisfiable_classes"]
    assert unsatisfiable["operand_class"] == "measure"
    assert unsatisfiable["required_by"] == 8
    assert len(unsatisfiable["recipe_ids"]) == 8
    (concept,) = unsatisfiable["concepts"]
    assert concept["concept"] == "monetary_flow"
    assert concept["required_by"] == 8
    assert concept["available_in"] == [{"catalog_source": FTR, "columns": 1}]
    # ▲ The half `semantic_projection` structurally cannot say: WHICH catalog to aim at.
    assert detail["satisfying_catalog_sources"] == [FTR]
    assert "ftr" in detail["message"]


def test_the_refusal_leaves_no_run_and_no_scope_row(make_client, conn):
    """Same law as ``SEMANTIC_REQUIRES_CATALOG_SOURCE``'s: a refused request is not a generation
    that produced nothing, so it writes neither the run nor the confirmed scope."""
    _two_catalogs(conn)
    runs_before = conn.execute("SELECT count(*) FROM feature_generation_run").fetchone()[0]
    scopes_before = conn.execute(
        "SELECT count(*) FROM confirmed_generation_scope").fetchone()[0]

    res = _post(make_client(_fake()), catalog_source=CIB)
    assert res.status_code == 422, res.text

    assert conn.execute(
        "SELECT count(*) FROM feature_generation_run").fetchone()[0] == runs_before
    assert conn.execute(
        "SELECT count(*) FROM confirmed_generation_scope").fetchone()[0] == scopes_before
    assert conn.execute("SELECT count(*) FROM contract_intent").fetchone()[0] == 0


def test_the_same_brief_aimed_at_ftr_is_served(make_client, conn):
    """The other half of the pin, and the one that keeps it from being a blanket refusal: aim the
    identical brief at the catalog that carries the semantics and the route plans it."""
    _two_catalogs(conn)
    res = _post(make_client(_fake()), catalog_source=FTR)
    assert res.status_code == 200, res.text
    assert conn.execute(
        "SELECT count(*) FROM feature_generation_run").fetchone()[0] == 1


def test_no_provider_call_is_spent_on_a_refused_brief(make_client, conn):
    """The refusal sits before the generation LLM is required, so a mis-aimed brief costs zero
    provider calls — the same posture the projection-readiness gate takes one line above it."""
    _two_catalogs(conn)
    fake = _fake()
    res = _post(make_client(fake), catalog_source=CIB)
    assert res.status_code == 422, res.text
    assert sum(fake._calls.values()) == 0


# ── BROADEN is governed identically — the owner's ruling, both directions ───────────────────────

def test_a_broaden_onto_a_mis_aimed_catalog_is_refused_with_the_same_directions(
        make_client, conn):
    """▲ THE BROADEN RULING, pinned. ``confirmed_scope.unscoped=true`` reaches this same
    confirmed-scope path with a scope ``v2_applicability`` fails OPEN on — all 317 recipes, floor
    158 — and it is governed identically. No exemption.

    The one law does not care how wide the scope is: a mis-aimed catalog refuses with directions
    whether the human asked for one use-case leaf or for everything. Clause 5 already protects the
    case with nowhere to point, and on an exploratory gesture "aim at ftr instead" is MORE useful
    than a page of setup work, not less.

    Measured: at broaden width the ``measure`` class is required by 175 of the 317 (55%), above
    the 158 floor, and this customer master carries nothing that can serve one."""
    _two_catalogs(conn)
    res = _broaden(make_client(_fake()), catalog_source=CIB)

    assert res.status_code == 422, res.text
    detail = res.json()["detail"]
    assert detail["code"] == "CATALOG_CANNOT_SATISFY_SCOPE"
    assert detail["eligible_recipes"] == 317          # the whole registry: broaden fails open
    assert detail["majority_floor"] == 158
    (unsatisfiable,) = detail["unsatisfiable_classes"]
    assert unsatisfiable["operand_class"] == "measure"
    assert unsatisfiable["required_by"] == 175
    assert detail["satisfying_catalog_sources"] == [FTR]

    # Same law as the scoped refusal: nothing durable is written for a refused broaden.
    assert conn.execute("SELECT count(*) FROM feature_generation_run").fetchone()[0] == 0
    assert conn.execute(
        "SELECT count(*) FROM confirmed_generation_scope").fetchone()[0] == 0


def test_a_broaden_onto_a_satisfying_catalog_is_served(make_client, conn):
    """The other direction, and the one that keeps the ruling from being a ban on broadening: the
    identical exploratory gesture aimed at the catalog that carries the semantics is planned.

    ▲ At broaden width the floor is genuinely hard to breach — the ``measure`` class asks for
    ~90 different concepts across the 317 recipes, so ONE column of almost any magnitude covers
    it. That is the floor behaving as designed (it refuses a catalog that carries no magnitude at
    all, not one that is merely narrow), and it is why the refusal above needs a customer master
    with no numeric measure on it."""
    _two_catalogs(conn)
    res = _broaden(make_client(_fake()), catalog_source=FTR)
    assert res.status_code == 200, res.text
    assert conn.execute(
        "SELECT count(*) FROM feature_generation_run").fetchone()[0] == 1
