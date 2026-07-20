"""Delivery F — the generic scalar field-correction command.

POST /catalog/assets/{source}/{object_ref}/fields/{field}/decisions over a `human_editable`
`field_evidence`-governed field. Proves the governance invariants against a REAL DB + route:
four-eyes on a load-bearing confirm, CAS-409 (fail-closed, INCLUDING a concurrent evidence append
with an unchanged decision head), the `human_editable` opt-in (specialized facts excluded),
append-only evidence (never overwrites / never trusts a client authority label), and idempotent
replay. Seeding uses the SAME real paths the asset-detail tests use (`build_graph` + direct
`record_field_evidence`).
"""
from __future__ import annotations

import pytest

from featuregen.overlay.catalog import _clear_catalog_adapter
from featuregen.overlay.config import _clear_overlay_config
from featuregen.overlay.evidence import AssertionStrength, EvidenceProducer
from featuregen.overlay.field_evidence import field_input_hash, record_field_evidence
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.field_correction import read_field_cas
from featuregen.overlay.upload.field_resolution import resolve_and_project
from featuregen.overlay.upload.graph import build_graph
from featuregen.overlay.upload.object_ref import normalize_ref

# Two DISTINCT platform-admin confirmers (the raw `platform-admin` claim require_confirmer gates on);
# subjects are `user:<X-User>`. A third caller WITHOUT the claim proves the route gate.
ADMIN_A = {"X-User": "priya", "X-Roles": "platform-admin"}
ADMIN_B = {"X-User": "sam", "X-Roles": "platform-admin"}
NON_ADMIN = {"X-User": "vic", "X-Roles": "catalog_viewer"}


@pytest.fixture(autouse=True)
def _clean_process_globals():
    yield
    _clear_catalog_adapter()
    _clear_overlay_config()


def _seed_column(conn, source, table, column, data_type, **cols):
    build_graph(conn, source, [CanonicalRow(source, table, column, data_type)])
    if cols:
        assignments = ", ".join(f"{k} = %s" for k in cols)
        conn.execute(
            f"UPDATE graph_node SET {assignments} WHERE catalog_source = %s AND object_ref = %s",
            [*cols.values(), source, f"public.{table}.{column}"])


def _seed_evidence(conn, ref, field, value, producer, strength, producer_ref):
    return record_field_evidence(
        conn, logical_ref=ref, field_name=field, proposed_value=value, producer=producer,
        strength=strength, producer_ref=producer_ref, source_snapshot_id="snap-seed",
        input_hash=field_input_hash(logical_ref=ref, field_name=field, material=value))


def _cas(conn, source, object_ref, field):
    return read_field_cas(conn, source=source, object_ref=object_ref, field=field)


def _post(client, source, object_ref, field, headers, cas, action, *, idem, **body):
    payload = {
        "action": action, "idempotency_key": idem,
        "expected_latest_decision_id": cas["latest_decision_id"],
        "expected_evidence_set_hash": cas["evidence_set_hash"],
        "expected_policy_version": cas["policy_version"], **body,
    }
    return client.post(
        f"/catalog/assets/{source}/{object_ref}/fields/{field}/decisions",
        json=payload, headers=headers)


def _graph_value(conn, source, object_ref, col):
    row = conn.execute(
        f"SELECT {col} FROM graph_node WHERE catalog_source = %s AND object_ref = %s",
        (source, object_ref)).fetchone()
    return row[0] if row else None


def _human_count(conn, ref, field):
    return conn.execute(
        "SELECT count(*) FROM field_evidence WHERE logical_ref = %s AND field_name = %s "
        "AND producer = 'human'", (ref, field)).fetchone()[0]


# ── (1) confirm_existing: a DIFFERENT subject confirms + projects; the proposer is refused ─────────


def test_confirm_existing_four_eyes_and_projection(client, conn):
    _seed_column(conn, "src1", "accounts", "balance", "numeric")
    ref = normalize_ref("src1", None, "accounts", "balance")
    # priya proposed this concept earlier (a HUMAN proposal — the four-eyes bar bites on it).
    eid = _seed_evidence(conn, ref, "concept", "monetary_stock", EvidenceProducer.HUMAN,
                         AssertionStrength.PROPOSED, producer_ref="user:priya")
    cas = _cas(conn, "src1", "public.accounts.balance", "concept")

    # The proposer (priya) confirming her OWN evidence → four-eyes refusal, nothing projected.
    r_self = _post(client, "src1", "public.accounts.balance", "concept", ADMIN_A, cas,
                   "confirm_existing", idem="c-self", selected_evidence_ids=[eid])
    assert r_self.status_code == 403, r_self.text
    assert "four_eyes" in r_self.json()["detail"]
    assert _graph_value(conn, "src1", "public.accounts.balance", "concept") is None

    # A DIFFERENT admin (sam) confirms → projects the display value.
    r_ok = _post(client, "src1", "public.accounts.balance", "concept", ADMIN_B, cas,
                 "confirm_existing", idem="c-ok", selected_evidence_ids=[eid])
    assert r_ok.status_code == 200, r_ok.text
    assert r_ok.json()["projected"] is True and r_ok.json()["outcome"] == "confirmed"
    assert _graph_value(conn, "src1", "public.accounts.balance", "concept") == "monetary_stock"


# ── (2) propose_override → non-load-bearing, not projected; confirm_override by a different subject ─


def test_propose_then_confirm_override_projects(client, conn):
    _seed_column(conn, "src2", "accounts", "balance", "numeric", definition="old def")
    cas0 = _cas(conn, "src2", "public.accounts.balance", "definition")

    r_prop = _post(client, "src2", "public.accounts.balance", "definition", ADMIN_A, cas0,
                   "propose_override", idem="p1", replacement_value="corrected definition")
    assert r_prop.status_code == 200, r_prop.text
    assert r_prop.json()["projected"] is False and r_prop.json()["outcome"] == "proposed"
    # NOT projected: the display column is untouched by a bare proposal.
    assert _graph_value(conn, "src2", "public.accounts.balance", "definition") == "old def"

    # The proposer cannot confirm their own override (four-eyes).
    cas1 = _cas(conn, "src2", "public.accounts.balance", "definition")
    r_self = _post(client, "src2", "public.accounts.balance", "definition", ADMIN_A, cas1,
                   "confirm_override", idem="co-self", replacement_value="corrected definition")
    assert r_self.status_code == 403 and "four_eyes" in r_self.json()["detail"]

    # A DIFFERENT admin confirms the override → projects.
    r_ok = _post(client, "src2", "public.accounts.balance", "definition", ADMIN_B, cas1,
                 "confirm_override", idem="co-ok", replacement_value="corrected definition")
    assert r_ok.status_code == 200, r_ok.text
    assert r_ok.json()["projected"] is True
    assert _graph_value(conn, "src2", "public.accounts.balance",
                        "definition") == "corrected definition"


# ── (3) CAS/409: concurrent evidence (decision unchanged) 409s; a stale decision/policy 409s ───────


def test_cas_409_on_concurrent_evidence_and_stale_anchors(client, conn):
    _seed_column(conn, "src3", "accounts", "balance", "numeric")
    ref = normalize_ref("src3", None, "accounts", "balance")
    cas0 = _cas(conn, "src3", "public.accounts.balance", "definition")
    assert cas0["latest_decision_id"] is None

    # A CONCURRENT evidence append (new active evidence, NO new decision) moves the set hash → 409.
    _seed_evidence(conn, ref, "definition", "sneaky", EvidenceProducer.LLM,
                   AssertionStrength.PROPOSED, producer_ref="enrich")
    r_conc = _post(client, "src3", "public.accounts.balance", "definition", ADMIN_A, cas0,
                   "propose_override", idem="x1", replacement_value="mine")
    assert r_conc.status_code == 409, r_conc.text
    assert _human_count(conn, ref, "definition") == 0   # refused before any write

    # A stale expected_latest_decision_id → 409 (fresh anchor, bogus decision id).
    cas1 = _cas(conn, "src3", "public.accounts.balance", "definition")
    bad_decision = {**cas1, "latest_decision_id": "fde_bogus"}
    r_dec = _post(client, "src3", "public.accounts.balance", "definition", ADMIN_A, bad_decision,
                  "propose_override", idem="x2", replacement_value="mine")
    assert r_dec.status_code == 409

    # A stale expected_policy_version → 409.
    bad_policy = {**cas1, "policy_version": "not-the-current-policy"}
    r_pol = _post(client, "src3", "public.accounts.balance", "definition", ADMIN_A, bad_policy,
                  "propose_override", idem="x3", replacement_value="mine")
    assert r_pol.status_code == 409
    assert _human_count(conn, ref, "definition") == 0


# ── (4) reject: appends a rejection, writes NO operational replacement ─────────────────────────────


def test_reject_writes_no_operational_replacement(client, conn):
    _seed_column(conn, "src4", "accounts", "balance", "numeric")
    ref = normalize_ref("src4", None, "accounts", "balance")
    _seed_evidence(conn, ref, "concept", "monetary_stock", EvidenceProducer.LLM,
                   AssertionStrength.PROPOSED, producer_ref="enrich")
    resolve_and_project(conn, source="src4", logical_refs=[ref])   # a display-only 'proposed' concept
    cas = _cas(conn, "src4", "public.accounts.balance", "concept")

    # reject is single-reviewer (no four-eyes needed).
    r = _post(client, "src4", "public.accounts.balance", "concept", ADMIN_A, cas, "reject",
              idem="rej1", reason="not this concept")
    assert r.status_code == 200, r.text
    assert r.json()["outcome"] == "rejected" and r.json()["projected"] is False

    # The latest decision is REJECTED and carries NO load-bearing value (no operational replacement).
    row = conn.execute(
        "SELECT event_type, load_bearing_value_hash FROM field_decision_event "
        "WHERE logical_ref = %s AND field_name = 'concept' ORDER BY created_at DESC, "
        "decision_event_id DESC LIMIT 1", (ref,)).fetchone()
    assert row[0] == "rejected" and row[1] is None
    # No active HUMAN/CONFIRMED evidence was written by a reject.
    n = conn.execute(
        "SELECT count(*) FROM field_evidence WHERE logical_ref = %s AND field_name = 'concept' "
        "AND producer = 'human' AND strength = 'confirmed' AND lifecycle = 'active'",
        (ref,)).fetchone()[0]
    assert n == 0


# ── (5) bounds / registry / human_editable opt-in ────────────────────────────────────────────────


def test_out_of_bounds_value_refused_before_write(client, conn):
    _seed_column(conn, "src5", "accounts", "balance", "numeric")
    ref = normalize_ref("src5", None, "accounts", "balance")
    cas = _cas(conn, "src5", "public.accounts.balance", "domain")

    over = "x" * 600   # domain bound is 512
    r_over = _post(client, "src5", "public.accounts.balance", "domain", ADMIN_A, cas,
                   "propose_override", idem="b1", replacement_value=over)
    assert r_over.status_code == 400, r_over.text
    r_empty = _post(client, "src5", "public.accounts.balance", "domain", ADMIN_A, cas,
                    "propose_override", idem="b2", replacement_value="   ")
    assert r_empty.status_code == 400
    assert _human_count(conn, ref, "domain") == 0   # nothing written


def test_unregistered_field_refused(client, conn):
    _seed_column(conn, "src5b", "accounts", "balance", "numeric")
    cas = {"latest_decision_id": None, "evidence_set_hash": "x", "policy_version": "y"}
    r = _post(client, "src5b", "public.accounts.balance", "not_a_field", ADMIN_A, cas,
              "propose_override", idem="u1", replacement_value="v")
    assert r.status_code == 400, r.text
    assert "unknown field" in r.json()["detail"]


@pytest.mark.parametrize("field", ["sensitivity", "data_type"])
def test_human_editable_false_field_refused(client, conn, field):
    """A specialized fact (sensitivity classification / physical type) is NOT generically editable —
    it keeps its dedicated command; the generic route refuses it (403)."""
    _seed_column(conn, "src5c", "accounts", "balance", "numeric")
    cas = {"latest_decision_id": None, "evidence_set_hash": "x", "policy_version": "y"}
    r = _post(client, "src5c", "public.accounts.balance", field, ADMIN_A, cas,
              "propose_override", idem="he1", replacement_value="restricted")
    assert r.status_code == 403, r.text
    assert "dedicated command" in r.json()["detail"]


# ── (6) append-only + never trusts a client authority label ───────────────────────────────────────


def test_append_only_never_overwrites_existing_evidence(client, conn):
    _seed_column(conn, "src6", "accounts", "balance", "numeric")
    ref = normalize_ref("src6", None, "accounts", "balance")
    eid = _seed_evidence(conn, ref, "concept", "monetary_stock", EvidenceProducer.LLM,
                         AssertionStrength.PROPOSED, producer_ref="enrich")
    before = conn.execute("SELECT count(*) FROM field_evidence WHERE logical_ref = %s "
                          "AND field_name = 'concept'", (ref,)).fetchone()[0]
    cas = _cas(conn, "src6", "public.accounts.balance", "concept")

    r = _post(client, "src6", "public.accounts.balance", "concept", ADMIN_A, cas,
              "confirm_existing", idem="ap1", selected_evidence_ids=[eid])
    assert r.status_code == 200, r.text

    # The ORIGINAL row is untouched (same value/producer/lifecycle); a NEW row was APPENDED.
    orig = conn.execute(
        "SELECT proposed_value, producer, lifecycle FROM field_evidence WHERE evidence_id = %s",
        (eid,)).fetchone()
    assert orig == ("monetary_stock", "llm", "active")
    after = conn.execute("SELECT count(*) FROM field_evidence WHERE logical_ref = %s "
                         "AND field_name = 'concept'", (ref,)).fetchone()[0]
    assert after == before + 1   # append-only: exactly one new row


def test_forged_client_authority_label_is_ignored(client, conn):
    """A non-admin who FORGES an authority label in the body still 403s — authority is the
    server-rechecked platform-admin claim, never a client field."""
    _seed_column(conn, "src6b", "accounts", "balance", "numeric")
    ref = normalize_ref("src6b", None, "accounts", "balance")
    cas = _cas(conn, "src6b", "public.accounts.balance", "definition")
    r = client.post(
        "/catalog/assets/src6b/public.accounts.balance/fields/definition/decisions",
        json={"action": "propose_override", "idempotency_key": "forge1",
              "replacement_value": "v", "expected_latest_decision_id": cas["latest_decision_id"],
              "expected_evidence_set_hash": cas["evidence_set_hash"],
              "expected_policy_version": cas["policy_version"],
              "authority": "platform-admin", "role": "platform-admin"},  # forged, ignored
        headers=NON_ADMIN)
    assert r.status_code == 403, r.text
    assert _human_count(conn, ref, "definition") == 0


# ── (7) idempotency: the same key replays; a reused key with different params 409s ─────────────────


def test_idempotent_replay_no_double_evidence(client, conn):
    _seed_column(conn, "src7", "accounts", "balance", "numeric")
    ref = normalize_ref("src7", None, "accounts", "balance")
    cas = _cas(conn, "src7", "public.accounts.balance", "definition")

    r1 = _post(client, "src7", "public.accounts.balance", "definition", ADMIN_A, cas,
               "propose_override", idem="same-key", replacement_value="v1")
    assert r1.status_code == 200, r1.text
    assert _human_count(conn, ref, "definition") == 1

    # A REPLAY with the SAME key — carrying the now-STALE anchor — replays success (idempotency runs
    # BEFORE the CAS), and appends NO second row.
    r2 = _post(client, "src7", "public.accounts.balance", "definition", ADMIN_A, cas,
               "propose_override", idem="same-key", replacement_value="v1")
    assert r2.status_code == 200, r2.text
    assert r2.json()["replayed"] is True
    assert _human_count(conn, ref, "definition") == 1   # no double evidence

    # The SAME key with DIFFERENT parameters is a conflict (409) — the key can't be repurposed.
    r3 = _post(client, "src7", "public.accounts.balance", "definition", ADMIN_A, cas,
               "propose_override", idem="same-key", replacement_value="v2-different")
    assert r3.status_code == 409, r3.text
    assert _human_count(conn, ref, "definition") == 1


# ── (8) the returned actions include only what the caller can execute ─────────────────────────────


def test_returned_actions_reflect_caller_authz(client, conn):
    _seed_column(conn, "src8", "accounts", "balance", "numeric")
    ref = normalize_ref("src8", None, "accounts", "balance")
    # A SERVICE (LLM) proposal exists — priya may confirm it (not her own).
    _seed_evidence(conn, ref, "definition", "llm def", EvidenceProducer.LLM,
                   AssertionStrength.PROPOSED, producer_ref="enrich")
    cas = _cas(conn, "src8", "public.accounts.balance", "definition")

    r = _post(client, "src8", "public.accounts.balance", "definition", ADMIN_A, cas,
              "propose_override", idem="a1", replacement_value="priya def")
    assert r.status_code == 200, r.text
    actions = r.json()["actions"]
    # priya may confirm the SERVICE proposal (confirm_existing) but NOT her own override
    # (confirm_override is barred — four-eyes: she is its sole proposer).
    assert "confirm_existing" in actions
    assert "confirm_override" not in actions
    assert "propose_override" in actions and "reject" in actions
