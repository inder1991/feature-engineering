"""FULL-STACK end-to-end: CSV upload -> Pass C discovery -> dual-admin confirm -> traversal ->
dashboard — every state change driven through the REAL HTTP routes (FastAPI TestClient), never a
direct `ingest_upload`/`propose_fact`/`confirm_fact` call.

THE CHAIN (the seam no per-stage suite covers — every governance test seeds its proposal with a
raw `propose_fact`, never via a `POST /uploads` that ran Pass C):

1. `POST /uploads` a technical CSV under OVERLAY_PASS_C=1 whose column pair scores STRONG, so the
   REAL ingest runs Pass C and proposes a governed approved_join (service actor, DRAFT).
2. `GET /sources/{source}/governance/joins` lists that upload-born proposal WITH its pre-minted
   Pass C evidence (score / signals / grain status).
3. Two DISTINCT platform-admins confirm over HTTP -> PARTIALLY_CONFIRMED -> VERIFIED. The second
   confirm runs under the SEALED production config, so the SP-1.5 referent gate validates the
   join's endpoints against the graph_node rows the REAL upload built (never hand-seeded rows) —
   the exact integration seam a per-stage test can't reach — and then synchronously projects the
   operational graph_edge (the drift watermark is fresh because the SAME upload request ran
   detect_catalog_changes minutes — here milliseconds — earlier, exactly like production).
4. `find_join_path` traverses the projected operational edge (feature construction unlocked).
5. Both governance dashboards count the VERIFIED join under the uploaded source.

CSV DESIGN — why `customer` + `customers` (not `transactions` + `customers`): Pass C weights make
a technical upload's ceiling same_column_name(30) + same_column_entity(25) +
one_side_confirmed_grain(10) = 65 < the 80 strong threshold; the corroboration signals
(term/synonym/BIAN/FIBO/domain) exist only on the GLOSSARY path, and a glossary CSV can't declare
`is_grain` (glossary_reader emits no grain), so its candidates are all forced weak. The ONLY
HTTP-reachable strong technical candidate also fires compatible_phase2_entity(+15): both tables
must share a phase-2 table entity (the grain row's declared entity, else the table name — here the
raw `customer` feed table name matches the mastered `customers` grain entity) = exactly 80.
This calibration ceiling is documented as a product finding in the e2e report.
"""
from __future__ import annotations

import pytest

from featuregen.overlay.catalog import _clear_catalog_adapter
from featuregen.overlay.config import (
    _clear_overlay_config,
    overlay_config_from_env,
    register_overlay_config,
)
from featuregen.overlay.state import fold_overlay_state
from featuregen.overlay.store import load_fact
from featuregen.overlay.upload.join_path import JoinStep, find_join_path

# A raw `customer` feed table referencing the mastered `customers` dimension: one shared
# single-column identifier (cust_id, entity-tagged both sides), the dimension side grain-declared.
# Scores strong (exactly 80): same_column_name + same_column_entity + compatible_phase2_entity
# (table name 'customer' == the customers grain entity) + one_side_confirmed_grain.
E2E_CSV = """\
source,table,column,type,is_grain,entity
catx,customer,txn_id,text,,
catx,customer,cust_id,text,,customer
catx,customer,amount,numeric,,
catx,customers,cust_id,text,true,customer
catx,customers,full_name,text,,
"""

# The same catalog EXTENDED with a new account/accounts pair that itself scores strong — a
# re-upload shape whose NEW proposal used to take the already-VERIFIED join offline (see the
# re-upload regression test below).
E2E_CSV_V2 = E2E_CSV + """\
catx,account,acct_id,text,,account
catx,account,cust_id,text,,customer
catx,accounts,acct_id,text,true,account
"""

UPLOADER = {"X-User": "tester", "X-Roles": "platform_admin"}   # catalog:write (functional bundle)
ADMIN1 = {"X-User": "priya", "X-Roles": "platform-admin"}      # raw confirmer claim (hyphen)
ADMIN2 = {"X-User": "rahman", "X-Roles": "platform-admin"}     # DISTINCT second confirmer
VIEWER = {"X-User": "v", "X-Roles": "catalog_viewer"}          # catalog:read (dashboards)


@pytest.fixture(autouse=True)
def _clean_process_globals():
    """The routes self-register the upload-context adapter and the app lifespan seals an overlay
    config — both PROCESS globals. Clear them after every test in this module so nothing leaks
    into a suite that expects the fail-closed RuntimeError."""
    yield
    _clear_catalog_adapter()
    _clear_overlay_config()


@pytest.fixture
def sealed_config(client):
    """Seal the process-wide OverlayConfig from an EMPTY env — the production gate, immune to
    ambient OVERLAY_* vars. MUST depend on `client`: the app lifespan seals its own env-based
    config at TestClient startup and `register_overlay_config` is last-writer-wins, so sealing
    before the client exists would be silently overwritten."""
    register_overlay_config(overlay_config_from_env({}))
    yield
    _clear_overlay_config()


def _upload(client, source: str, csv_text: str):
    return client.post(
        "/uploads", data={"source": source},
        files={"file": (f"{source}.csv", csv_text.encode(), "text/csv")}, headers=UPLOADER)


def _graph_refs(conn, source: str) -> set[str]:
    return {r[0] for r in conn.execute(
        "SELECT object_ref FROM graph_node WHERE catalog_source = %s", (source,)).fetchall()}


# ── The full chain, flag ON ───────────────────────────────────────────────────────────────────────


def test_full_chain_upload_discover_confirm_traverse_dashboard(
        client, sealed_config, conn, monkeypatch):
    monkeypatch.setenv("OVERLAY_PASS_C", "1")   # ingest reads the env per call — set before POST

    # ── Stage 1: POST /uploads runs the REAL ingest (Pass A facts + graph + Pass C) ──────────────
    r = _upload(client, "catx", E2E_CSV)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ingested"
    assert body["quarantined"] == 0
    assert body["asserted"] == 1                      # the customers grain fact
    assert "first upload" in (body["flagged"] or "")  # soft brake note, not an error
    # The upload built the graph — the structural truth the referent gate will validate against.
    assert {"public.customer", "public.customers",
            "public.customer.cust_id", "public.customers.cust_id"} <= _graph_refs(conn, "catx")
    # Pass C persisted its candidate ledger row AND stamped the governed proposal onto it.
    (ledger,) = conn.execute(
        "SELECT bucket, fact_key FROM pass_c_candidate_evidence WHERE catalog_source='catx'"
    ).fetchall()
    assert ledger[0] == "strong" and ledger[1] is not None

    # ── Stage 2: the governance queue surfaces the upload-born proposal with its evidence ────────
    r = client.get("/sources/catx/governance/joins", headers=ADMIN1)
    assert r.status_code == 200, r.text
    listing = r.json()
    assert listing["source"] == "catx"
    (p,) = listing["proposals"]
    key = p["fact_key"]
    assert key == ledger[1]                           # the queue shows the ledger-stamped fact
    assert p["status"] == "PROPOSED"
    assert p["from"] == {"table": "customer", "column": "cust_id"}
    assert p["to"] == {"table": "customers", "column": "cust_id"}
    assert p["cardinality"] == "N:1"
    assert p["proposed_direction"] == "customer.cust_id -> customers.cust_id"
    assert {t["side"] for t in p["tasks"]} == {"from", "to"}   # dual side-labelled gate tasks
    assert p["evidence_parse_status"] == "parsed"
    assert p["evidence"]["score"] == 80
    assert p["evidence"]["grain_status"] == "inferred_from_confirmed_grain"
    assert {s["signal_name"] for s in p["evidence"]["positive_signals"]} == {
        "same_column_name", "same_column_entity", "compatible_phase2_entity",
        "one_side_confirmed_grain"}
    # Pre-confirm the join is NOT operational: nothing declared, nothing projected.
    assert find_join_path(conn, "catx", "customer", "customers") is None

    # ── Stage 3: dual-admin confirm over HTTP under the SEALED config ─────────────────────────────
    r = client.post(f"/governance/joins/{key}/confirm", json={"note": "raw feed key checks out"},
                    headers=ADMIN1)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["governance_status"] == "PARTIALLY_CONFIRMED"
    assert body["operational_projection"] == "not_applicable"
    assert [a["note"] for a in body["approvals"]] == ["raw feed key checks out"]

    # The KEY untested seam: the second confirm's SP-1.5 referent gate validates the join's
    # endpoints against the graph_node rows the REAL upload built; then the synchronous
    # drain-then-project makes the join operational IN THIS REQUEST (fresh watermark — the same
    # upload request ran detect_catalog_changes).
    r = client.post(f"/governance/joins/{key}/confirm", json={}, headers=ADMIN2)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["governance_status"] == "VERIFIED"
    assert body["operational_projection"] == "projected"
    assert len(body["approvals"]) == 2
    assert fold_overlay_state(load_fact(conn, key)).status == "VERIFIED"

    # ── Stage 4: the join is operationally traversable (feature construction unlocked) ───────────
    rows = conn.execute(
        "SELECT authority, approved_join_fact_key, approved_join_status FROM graph_edge"
        " WHERE kind = 'joins' AND catalog_source = 'catx'").fetchall()
    assert rows == [("operational", key, "VERIFIED")]
    assert find_join_path(conn, "catx", "customer", "customers") == \
        [JoinStep("public.customer.cust_id", "public.customers.cust_id", "N:1")]

    # A verified join has left the open queue.
    r = client.get("/sources/catx/governance/joins", headers=ADMIN2)
    assert r.json()["proposals"] == []

    # ── Stage 5: both governance dashboards reflect the confirmed join ────────────────────────────
    r = client.get("/governance/dashboard", headers=VIEWER)
    assert r.status_code == 200, r.text
    dash = r.json()
    by_type = {ft["fact_type"]: ft for ft in dash["fact_types"]}
    assert by_type["approved_join"]["confirmed"] >= 1
    (summary,) = [s for s in dash["sources"] if s["source"] == "catx"]
    assert summary["confirmed"] >= 1
    r = client.get("/sources/catx/governance/dashboard", headers=VIEWER)
    assert r.status_code == 200, r.text
    per_source = {ft["fact_type"]: ft for ft in r.json()["fact_types"]}
    assert per_source["approved_join"]["confirmed"] >= 1


# ── Regression (REAL BUG found by this e2e, fixed in ingest.py): a re-upload that proposes a NEW
# candidate must not take an already-VERIFIED join offline ─────────────────────────────────────────


def test_reupload_with_new_candidate_keeps_verified_join_operational(
        client, sealed_config, conn, monkeypatch):
    """Pre-fix: the governed seams (Pass B/C, joins_to routing) append their PROPOSED events AFTER
    ingest's last projection drain, so the end-of-ingest lag guards (ingest.py) skipped BOTH
    re-projection blocks on every upload that proposed anything. build_graph wipes every edge on a
    re-upload — so a re-upload that discovered a NEW strong candidate deleted the previously
    VERIFIED join's operational graph_edge and never restored it: `find_join_path` returned None
    (feature construction dark) until the NEXT caught-up ingest of the source. Fixed by draining
    the self-appended events before the guards (the project_verified_join drain-then-project
    pattern); the guards now fire only on a genuine poison-halt."""
    monkeypatch.setenv("OVERLAY_PASS_C", "1")
    assert _upload(client, "catx", E2E_CSV).json()["status"] == "ingested"
    r = client.get("/sources/catx/governance/joins", headers=ADMIN1)
    (p,) = r.json()["proposals"]
    key = p["fact_key"]
    for headers in (ADMIN1, ADMIN2):
        r = client.post(f"/governance/joins/{key}/confirm", json={}, headers=headers)
        assert r.status_code == 200, r.text
    assert r.json()["operational_projection"] == "projected"
    assert find_join_path(conn, "catx", "customer", "customers") is not None

    # Re-upload the EXTENDED catalog: build_graph wipes every edge; Pass C proposes the NEW
    # account<->accounts candidate (appending events after the drain — the pre-fix trigger).
    r2 = _upload(client, "catx", E2E_CSV_V2)
    assert r2.status_code == 200 and r2.json()["status"] == "ingested", r2.text

    # The VERIFIED join stays VERIFIED **and operational**: its edge was re-projected in the same
    # request, so feature construction never went dark.
    assert fold_overlay_state(load_fact(conn, key)).status == "VERIFIED"
    assert find_join_path(conn, "catx", "customer", "customers") == \
        [JoinStep("public.customer.cust_id", "public.customers.cust_id", "N:1")]
    # And the new candidate is queued for governance like any other discovery.
    r = client.get("/sources/catx/governance/joins", headers=ADMIN1)
    (p2,) = r.json()["proposals"]
    assert p2["from"] == {"table": "account", "column": "acct_id"}
    assert p2["to"] == {"table": "accounts", "column": "acct_id"}


# ── Negative probe: the sealed referent gate is ARMED on this exact HTTP posture ──────────────────


def test_vanished_endpoint_denies_second_confirm_over_http(
        client, sealed_config, conn, monkeypatch):
    """Prove the happy path's green is not a silently-skipped gate: after admin1's confirm, delete
    the to-side column node (the unit-suite drift probe — test_sealed_join_confirm_referents), and
    the second HTTP confirm must be DENIED with the referent-gap reason, the fact left
    PARTIALLY_CONFIRMED, and no operational edge written."""
    monkeypatch.setenv("OVERLAY_PASS_C", "1")
    assert _upload(client, "catx", E2E_CSV).json()["status"] == "ingested"
    r = client.get("/sources/catx/governance/joins", headers=ADMIN1)
    (p,) = r.json()["proposals"]
    key = p["fact_key"]

    r = client.post(f"/governance/joins/{key}/confirm", json={}, headers=ADMIN1)
    assert r.status_code == 200 and r.json()["governance_status"] == "PARTIALLY_CONFIRMED"

    conn.execute("DELETE FROM graph_node WHERE catalog_source='catx' "
                 "AND object_ref='public.customers.cust_id'")
    r = client.post(f"/governance/joins/{key}/confirm", json={}, headers=ADMIN2)
    assert r.status_code == 409, r.text
    assert "join referent missing from graph: catx.public.customers.cust_id" in \
        r.json()["detail"]
    assert fold_overlay_state(load_fact(conn, key)).status == "PARTIALLY_CONFIRMED"
    assert conn.execute(
        "SELECT count(*) FROM graph_edge WHERE catalog_source='catx' AND kind='joins'"
    ).fetchone()[0] == 0


# ── The same upload, flag OFF: byte-for-byte no governed-join machinery ───────────────────────────


def test_flag_off_same_upload_proposes_nothing(client, sealed_config, conn, monkeypatch):
    monkeypatch.delenv("OVERLAY_PASS_C", raising=False)
    monkeypatch.delenv("OVERLAY_GOVERNED_JOINS", raising=False)

    r = _upload(client, "catx", E2E_CSV)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "ingested"
    assert {"public.customer.cust_id", "public.customers.cust_id"} <= _graph_refs(conn, "catx")

    # No candidate ledger rows, no gate tasks, no open proposals — Pass C never ran.
    assert conn.execute(
        "SELECT count(*) FROM pass_c_candidate_evidence WHERE catalog_source='catx'"
    ).fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM human_tasks").fetchone()[0] == 0
    r = client.get("/sources/catx/governance/joins", headers=ADMIN1)
    assert r.status_code == 200
    assert r.json()["proposals"] == []
    # No join was declared in the file, so no join edge exists either way.
    assert find_join_path(conn, "catx", "customer", "customers") is None
