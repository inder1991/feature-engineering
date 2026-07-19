"""Delivery F0 Task 2 — GET /catalog/assets/{source}/{object_ref:path} read model.

Proves the bounded sections are assembled from REAL ingest state (no hardcoded values) under the
caller's read-scope: a hidden anchor 404s indistinguishably from a missing one, a hidden sibling is
omitted from relationships with NO count/name leak, the effective_metadata authority reflects C1
(governed vs hint), the readiness section carries the F0-T1 capability matrix, and history reflects
the ingestion_run_object provenance for the ref. The semantic-relationships subsection is
`unavailable` in F0.

Seeding uses BOTH real paths: a genuine `POST /uploads` (the technical DEPOSITS_CSV → a real ingest
that builds the graph, the join edges, and the run→object provenance), and — for the governed/hint
authority and the read-scope leak assertions — a direct `build_graph` + real resolver
(`resolve_and_project`) seed for full control, exactly the shape test_column_readiness uses.
"""
from __future__ import annotations

import json

import pytest
from tests.featuregen.api._helpers import AUTH, DEPOSITS_CSV, PII_AUTH, upload_csv

from featuregen.overlay.catalog import _clear_catalog_adapter
from featuregen.overlay.config import _clear_overlay_config
from featuregen.overlay.evidence import AssertionStrength, EvidenceProducer
from featuregen.overlay.field_evidence import field_input_hash, record_field_evidence
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.field_resolution import resolve_and_project
from featuregen.overlay.upload.graph import build_graph
from featuregen.overlay.upload.object_ref import normalize_ref

# A caller with catalog:read but NO data-sensitivity role (AUTH) can't see pii; PII_AUTH adds
# pii_reader. access_admin holds ONLY iam:manage — no catalog:read.
ACCESS_ADMIN = {"X-User": "a", "X-Roles": "access_admin"}


@pytest.fixture(autouse=True)
def _clean_process_globals():
    """The upload route self-registers the upload-context adapter and the app lifespan seals an
    overlay config — both PROCESS globals. Clear them after every test so nothing leaks into a
    suite that expects the fail-closed RuntimeError."""
    yield
    _clear_catalog_adapter()
    _clear_overlay_config()


def _asset(client, source, ref, *, headers=AUTH, **params):
    return client.get(f"/catalog/assets/{source}/{ref}", headers=headers, params=params)


def _seed_column(conn, source, table, column, data_type, **cols):
    """Build a one-column graph for `source` and set flat display columns (sensitivity/unit/...)."""
    build_graph(conn, source, [CanonicalRow(source, table, column, data_type)])
    if cols:
        assignments = ", ".join(f"{k} = %s" for k in cols)
        conn.execute(
            f"UPDATE graph_node SET {assignments} WHERE catalog_source = %s AND object_ref = %s",
            [*cols.values(), source, f"public.{table}.{column}"])


# ── (1) A REAL ingest → the versioned sections, built from real data ─────────────────────────────


def test_asset_detail_sections_built_from_real_ingest(client):
    assert upload_csv(client, "deposits", DEPOSITS_CSV).status_code == 200

    r = _asset(client, "deposits", "public.accounts.balance")
    assert r.status_code == 200, r.text
    body = r.json()

    # Versioned shape + consistency token echoed on the ETag header.
    assert body["version"] == "asset-detail/v1"
    assert body["consistency_token"]
    assert r.headers["ETag"] == f'"{body["consistency_token"]}"'
    assert set(body["included_sections"]) == {
        "identity", "effective_metadata", "evidence", "relationships", "readiness", "history",
        "actions"}

    # identity — from the REAL graph_node the ingest built (no hardcoded values).
    ident = body["identity"]
    assert ident["source"] == "deposits"
    assert ident["table"] == "accounts"
    assert ident["column"] == "balance"
    assert ident["kind"] == "column"
    assert ident["operational_type"] == "numeric"
    assert ident["logical_ref"] == "deposits::public.accounts.balance"

    # effective_metadata — the display values are the REAL declared CSV metadata.
    fields = body["effective_metadata"]["fields"]
    assert fields["additivity"]["value"] == "semi_additive"
    assert fields["unit"]["value"] == "dollars"
    assert fields["currency"]["value"] == "USD"
    assert fields["entity"]["value"] == "Account"

    # relationships — containment lists the REAL sibling columns; semantic is F0-unavailable.
    cols = {c["column"] for c in body["relationships"]["containment"]["columns"]}
    assert {"id", "posted_at", "cust_id"} <= cols
    assert "balance" not in cols                      # the anchor is not its own sibling
    assert body["relationships"]["semantic"] == {"status": "unavailable", "available_in": "F1"}
    assert "relationships.semantic" in body["unavailable_sections"]

    # actions — F0 keeps this empty (the real correction command is F0-T4).
    assert body["actions"] == []


# ── (2) Read-scope: hidden anchor → 404 (no existence leak); hidden sibling omitted, no leak ──────


def test_read_scope_hides_anchor_and_sibling_without_leak(client, conn):
    build_graph(conn, "scoped", [
        CanonicalRow("scoped", "t", "keep1", "text"),
        CanonicalRow("scoped", "t", "keep2", "text"),
        CanonicalRow("scoped", "t", "ssn", "text", sensitivity="pii"),
    ])

    # A hidden anchor is INDISTINGUISHABLE from a missing one: both 404 (no existence leak).
    assert _asset(client, "scoped", "public.t.ssn", headers=AUTH).status_code == 404
    # The same object is visible to a pii_reader — proving the 404 above was the scope, not absence.
    assert _asset(client, "scoped", "public.t.ssn", headers=PII_AUTH).status_code == 200

    # A visible sibling anchor: the hidden 'ssn' column is omitted from containment; visible ones stay.
    r = _asset(client, "scoped", "public.t.keep1", headers=AUTH)
    assert r.status_code == 200, r.text
    body = r.json()
    cols = {c["column"] for c in body["relationships"]["containment"]["columns"]}
    assert cols == {"keep2"}                          # keep1 is the anchor; ssn is hidden

    # NO LEAK: the hidden column's name appears nowhere, and there is no total/hidden count field.
    blob = json.dumps(body)
    assert "ssn" not in blob
    assert body["unavailable_sections"] == ["relationships.semantic"]   # no permission-count leak


# ── (2b) F1: the readiness TABLE diagnostic is read-scoped — no hidden-column leak ───────────────


def _seed_proposed_concept(conn, ref):
    """A NON-load-bearing (proposed) concept decision for `ref`: an LLM/PROPOSED concept does NOT
    satisfy concept's SOURCE-or-HUMAN operational rule, so resolve_and_project records a display-only
    'proposed' decision — which surfaces as a NAMED `field:<ref>:concept` review requirement in the
    UNSCOPED table diagnostic (the leak vector). Real field_decision_event rows, not a stub."""
    record_field_evidence(
        conn, logical_ref=ref, field_name="concept", proposed_value="monetary_stock",
        producer=EvidenceProducer.LLM, strength=AssertionStrength.PROPOSED, producer_ref="enrich",
        source_snapshot_id="snap-1",
        input_hash=field_input_hash(logical_ref=ref, field_name="concept",
                                    material="monetary_stock"))
    resolve_and_project(conn, source="hr", logical_refs=[ref])


def test_readiness_table_diagnostic_read_scoped_no_hidden_column_leak(client, conn):
    """The MERGE BLOCKER (F1): the asset's readiness.table_diagnostic must be computed under the
    caller's read-scope, so a hidden sensitivity-restricted sibling column that HAS decision rows
    never leaks its name/logical_ref/count via a `field:...` requirement, an advisory gap, or a
    summary count — for a `catalog:read` caller WITHOUT pii_reader viewing a VISIBLE sibling."""
    from featuregen.overlay.upload.readiness import ReadinessScopeType, compute_readiness

    build_graph(conn, "hr", [
        CanonicalRow("hr", "employees", "salary", "numeric"),
        CanonicalRow("hr", "employees", "national_id", "text", sensitivity="pii"),
    ])
    visible_ref = "hr::public.employees.salary"
    hidden_ref = "hr::public.employees.national_id"
    _seed_proposed_concept(conn, visible_ref)
    _seed_proposed_concept(conn, hidden_ref)   # the hidden column HAS real decision rows

    # NON-VACUITY: the UNSCOPED diagnostic (roles=None) DOES name the hidden column — proving the
    # leak is real and that read-scope is what removes it, not an empty diagnostic.
    unscoped = compute_readiness(conn, source="hr", scope=ReadinessScopeType.TABLE,
                                 subset="employees")
    unscoped_ids = [rq.requirement_id for rq in
                    (*unscoped.blocking_requirements, *unscoped.review_requirements)]
    assert any("national_id" in rid for rid in unscoped_ids), unscoped_ids

    # A caller WITHOUT pii_reader GETs the VISIBLE sibling.
    r = _asset(client, "hr", "public.employees.salary", headers=AUTH)
    assert r.status_code == 200, r.text
    body = r.json()

    # NO LEAK ANYWHERE: grep the WHOLE payload (readiness requirements/gaps/scores + relationships).
    assert "national_id" not in json.dumps(body)

    # And specifically the read-scoped table diagnostic keeps the VISIBLE ref, drops the hidden one.
    diag = body["readiness"]["table_diagnostic"]
    named = [rq["requirement_id"] for rq in
             diag["blocking_requirements"] + diag["review_requirements"]] + list(diag["advisory_gaps"])
    assert any("salary" in n for n in named), named          # visible sibling still reported
    assert not any("national_id" in n for n in named)        # hidden sibling gone

    # SANITY: a pii_reader (who MAY see national_id) does get its concept requirement — the scope,
    # not an empty diagnostic, is what hid it above.
    pii_diag = _asset(client, "hr", "public.employees.salary",
                      headers=PII_AUTH).json()["readiness"]["table_diagnostic"]
    pii_named = [rq["requirement_id"] for rq in
                 pii_diag["blocking_requirements"] + pii_diag["review_requirements"]]
    assert any("national_id" in n for n in pii_named), pii_named


# ── (2c) F4: as_join_key confirms a TO-side (dimension) join, consistent with relationships ───────


def test_as_join_key_confirms_to_side_join_consistent_with_relationships(client, conn):
    """F4: a VERIFIED approved_join A.cust_id -> B.id; GETting the asset for B.id (the to_ref, a
    dimension key) must show readiness.as_join_key CONFIRMED — consistent with
    relationships.approved_joins in the SAME payload, which lists the join. Pre-fix the readiness
    matched from_ref only, so the to-side key showed 'no verified approved_join yet'."""
    build_graph(conn, "joins_src", [
        CanonicalRow("joins_src", "a", "cust_id", "integer"),
        CanonicalRow("joins_src", "b", "id", "integer"),
    ])
    conn.execute(
        "INSERT INTO graph_edge (catalog_source, kind, from_ref, to_ref, approved_join_status, "
        "authority) VALUES ('joins_src', 'joins', 'public.a.cust_id', 'public.b.id', 'VERIFIED', "
        "'display_only')")

    body = _asset(client, "joins_src", "public.b.id").json()

    caps = body["readiness"]["column_capabilities"]
    join_cap = caps["as_join_key"]
    jr = next(r for r in join_cap["requirements"] if r["requirement_id"] == "join_connectivity")
    assert jr["status"] == "confirmed"
    assert jr["reason"] == "verified_approved_join"

    # Consistency: the SAME payload's relationships lists the very join readiness just confirmed.
    approved = body["relationships"]["approved_joins"]
    assert any(j["to_ref"] == "public.b.id" and j["status"] == "VERIFIED" for j in approved), approved


# ── (2d) F5: the history section is bounded + batches its stage reads ─────────────────────────────


def test_history_section_bounded_and_batched(conn):
    """F5: a daily-ingested ref would otherwise return an unbounded run list + a per-run stage
    round trip (1 + N queries). The section caps the run list at _HISTORY_RUN_LIMIT (flagging
    `truncated`) and fetches ALL stages in ONE `= ANY(...)` query — a constant 2 queries."""
    from featuregen.overlay.upload.asset_detail import _HISTORY_RUN_LIMIT, _history_section

    ref = "public.t.c"
    conn.execute(
        "INSERT INTO graph_node (catalog_source, object_ref, kind, table_name, column_name, "
        "data_type) VALUES ('runs', %s, 'column', 't', 'c', 'text')", (ref,))
    n = _HISTORY_RUN_LIMIT + 5
    for i in range(n):
        run_id = f"run-{i:03d}"
        conn.execute(
            "INSERT INTO ingestion_run (id, origin_type, catalog_source, actor_subject, status, "
            "started_at, completed_at, heartbeat_at) VALUES (%s, 'upload', 'runs', 'tester', "
            "'ingested', now(), now(), now())", (run_id,))
        conn.execute(
            "INSERT INTO ingestion_run_object (ingestion_run_id, catalog_source, object_ref, "
            "relation, at) VALUES (%s, 'runs', %s, 'observed', now() + (%s || ' seconds')::interval)",
            (run_id, ref, i))
        conn.execute(
            "INSERT INTO ingestion_run_stage (ingestion_run_id, stage, attempt, state) "
            "VALUES (%s, 'validate', 1, 'succeeded')", (run_id,))

    class _CountingConn:
        def __init__(self, real):
            self._real = real
            self.executes: list[str] = []

        def execute(self, sql, params=None):
            self.executes.append(sql)
            return self._real.execute(sql, params) if params is not None else self._real.execute(sql)

    counting = _CountingConn(conn)
    section = _history_section(counting, "runs", ref)

    assert section["truncated"] is True
    assert len(section["runs"]) == _HISTORY_RUN_LIMIT           # bounded, not N
    assert all(run["stages"] for run in section["runs"])       # stages present for each
    # CONSTANT query count: one run query + one batched stage query, regardless of N runs.
    assert len(counting.executes) == 2, counting.executes


# ── (3) effective_metadata authority reflects C1 (governed vs hint) ──────────────────────────────


def test_effective_metadata_authority_reflects_c1(client, conn):
    _seed_column(conn, "seedsrc", "accounts", "balance", "numeric", unit="dollars")
    ref = normalize_ref("seedsrc", None, "accounts", "balance")
    # A GOVERNED additivity via the REAL resolver (record evidence → resolve_and_project projects it).
    record_field_evidence(
        conn, logical_ref=ref, field_name="additivity", proposed_value="additive",
        producer=EvidenceProducer.SOURCE, strength=AssertionStrength.ATTESTED, producer_ref="t",
        source_snapshot_id="snap-1",
        input_hash=field_input_hash(logical_ref=ref, field_name="additivity", material="additive"))
    resolve_and_project(conn, source="seedsrc", logical_refs=[ref])

    r = _asset(client, "seedsrc", "public.accounts.balance")
    assert r.status_code == 200, r.text
    fields = r.json()["effective_metadata"]["fields"]

    # additivity is a C1-governed decision field → governed authority + provenance.
    assert fields["additivity"]["value"] == "additive"
    assert fields["additivity"]["authority"] == "governed"
    assert fields["additivity"]["c1_status"] == "resolved"
    assert fields["additivity"]["provenance"]              # a decision_event_id
    # unit is a hint (a flat value with no governed decision) → hint authority.
    assert fields["unit"]["value"] == "dollars"
    assert fields["unit"]["authority"] == "hint"


# ── (4) readiness section carries the F0-T1 capability matrix + the parent-table diagnostic ───────


def test_readiness_section_carries_capability_matrix(client):
    assert upload_csv(client, "deposits", DEPOSITS_CSV).status_code == 200

    body = _asset(client, "deposits", "public.accounts.balance").json()
    caps = body["readiness"]["column_capabilities"]
    assert {"as_measure", "as_entity_key", "as_event_time", "as_grain_key", "as_join_key"} <= set(caps)
    for use in ("as_measure", "as_grain_key"):
        assert caps[use]["use"] == use
        assert caps[use]["operational_status"] in ("ready", "blocked")
        assert caps[use]["requirements"]                  # a non-empty requirement list
    # The parent-table blocker diagnostic (compute_readiness TABLE scope).
    assert body["readiness"]["table_diagnostic"]["scope"] == "table"


# ── (5) history reflects the ingestion_run_object provenance for the ref ──────────────────────────


def test_history_reflects_ingestion_run_object(client):
    assert upload_csv(client, "deposits", DEPOSITS_CSV).status_code == 200

    body = _asset(client, "deposits", "public.accounts.balance").json()
    runs = body["history"]["runs"]
    assert runs, "the ingest run that observed this ref is recorded"
    top = runs[0]
    assert top["relation"] == "observed"
    assert top["status"] == "ingested"
    assert top["ingestion_run_id"]
    assert isinstance(top["stages"], list) and top["stages"]   # per-run stage outcomes surfaced


# ── (6) A nonexistent ref (or source) → 404 ───────────────────────────────────────────────────────


def test_nonexistent_ref_returns_404(client):
    assert upload_csv(client, "deposits", DEPOSITS_CSV).status_code == 200
    assert _asset(client, "deposits", "public.accounts.nope").status_code == 404
    assert _asset(client, "no-such-source", "public.x.y").status_code == 404


# ── (7) include selects sections; RBAC requires catalog:read ─────────────────────────────────────


def test_include_selects_sections(client):
    assert upload_csv(client, "deposits", DEPOSITS_CSV).status_code == 200
    body = _asset(client, "deposits", "public.accounts.balance",
                  include=["identity", "readiness"]).json()
    assert set(body["included_sections"]) == {"identity", "readiness"}
    assert "evidence" not in body and "history" not in body
    assert body["unavailable_sections"] == []          # relationships not built → nothing unavailable


def test_asset_route_requires_catalog_read(client):
    assert upload_csv(client, "deposits", DEPOSITS_CSV).status_code == 200
    # access_admin holds ONLY iam:manage — no catalog:read → 403 (before any assembly).
    assert _asset(client, "deposits", "public.accounts.balance",
                  headers=ACCESS_ADMIN).status_code == 403
