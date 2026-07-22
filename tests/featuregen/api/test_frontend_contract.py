"""THE FRONTEND CONTRACT (#23): pins every wire shape ``frontend/src/api.ts`` declares.

Frontend unit tests MOCK this API and the backend suites stop at HTTP/Postgres, so nothing used
to catch contract drift between the two sides — the backend-``id``-vs-frontend-``run_id`` field
mismatch, the dropped ``X-Ingestion-Run-Id`` response header, un-normalized mixed-case sources,
and the connector preview omitting ``collisions``/``dropped_joins`` all shipped green. Every test
here drives the REAL HTTP routes on the same TestClient + real-Postgres harness as
test_full_ingestion_e2e.py (migrations applied, auth-stub headers) and asserts the response
SHAPE: tight key-SET equality wherever the shape is fixed, so an ADDED or REMOVED backend field
fails this suite and forces the matching frontend change in the same commit.

MUST BE UPDATED IN LOCKSTEP WITH ``frontend/src/api.ts``: every assertion names the TypeScript
interface it pins. If a change here is needed, the same change is needed there — and vice versa.
"""
from __future__ import annotations

import copy

import pytest
from tests.featuregen.api._helpers import AUTH, DEPOSITS_CSV, OWNER, VIEWER, upload_csv
from tests.featuregen.connectors._fixtures import (
    CARDS_SERVICE,
    CARDS_TAG_MAP,
    fixture_fetch,
    fixture_pages,
)

from featuregen.overlay.catalog import _clear_catalog_adapter
from featuregen.overlay.config import _clear_overlay_config

RUN_HEADER = "X-Ingestion-Run-Id"           # api.ts: requestWithResponse / ApiError.ingestionRunId
CONFIRMER = {"X-User": "priya", "X-Roles": "platform-admin"}   # require_confirmer (governance)

# ── the TS shapes, transcribed (each constant names the api.ts interface it mirrors) ─────────────

# IngestResult (api.ts): the POST /uploads JSON body. `ingestion_run_id` is CLIENT-attached from
# the response header ("never a body field") — so it must NOT appear in this set.
INGEST_RESULT_KEYS = {"status", "reason", "asserted", "changed_objects", "quarantined", "flagged",
                      "objects_stored", "tables", "columns", "containment_edges", "facts_asserted",
                      "join_candidates", "passb_proposed", "passb_abstained",
                      "semantic_binding_candidates", "semantic_binding_proposed",
                      "semantic_binding_abstained", "semantic_binding_failed"}

# IngestionRun (api.ts): the fields the client reads off GET /ingestion-runs/{id}. Keyed `id` —
# NOT `run_id` — the exact contract the #15 bug violated.
INGESTION_RUN_TS_KEYS = {
    "id", "origin_type", "catalog_source", "filename", "actor_subject", "actor_role_claims",
    "authorization_decision", "status", "row_count", "quarantined_count", "started_at",
    "completed_at", "redacted_failure_code", "status_history", "stages"}
# api.ts documents that the wire carries a few more columns than the client declares ("declare
# only what the client reads"). Pin the WHOLE wire set anyway: a backend add/remove/rename must
# fail here and force a deliberate lockstep look at api.ts.
INGESTION_RUN_WIRE_KEYS = INGESTION_RUN_TS_KEYS | {
    "file_sha256", "pre_source_fingerprint", "post_source_fingerprint",
    "fingerprint_algo_version", "effective_config", "heartbeat_at", "objects", "facts",
    # Delivery B item 9 (source-profile provenance): wire-only until the client reads them
    "source_type", "profile_version"}
# IngestionStage (api.ts)
INGESTION_STAGE_KEYS = {"stage", "attempt", "state", "reason_code", "detail",
                        "started_at", "completed_at"}
# IngestionStatusEvent (api.ts)
STATUS_EVENT_KEYS = {"status", "at", "reason_code"}

# SearchHit / FacetBucket / SearchResult / SEARCH_FACET_KEYS (api.ts)
SEARCH_HIT_KEYS = {
    "object_ref", "table", "column", "kind", "data_type", "definition", "is_grain", "is_as_of",
    "catalog_source", "concept", "domain", "sensitivity", "additivity", "unit", "currency",
    "entity", "score"}
SEARCH_RESULT_KEYS = {"hits", "facets", "total"}
FACET_BUCKET_KEYS = {"value", "count"}
SEARCH_FACET_KEYS = {"source", "domain", "sensitivity", "additivity", "entity", "kind"}

# QuarantineItem (api.ts)
QUARANTINE_ITEM_KEYS = {"row_index", "raw", "reason"}

# listJoinProposals return / JoinProposal / JoinTask / JoinEvidence / JoinSignal (api.ts)
JOINS_LISTING_KEYS = {"source", "proposals", "divergences", "next_cursor"}
JOIN_PROPOSAL_KEYS = {
    "fact_key", "tasks", "from", "to", "cardinality", "proposed_direction", "status",
    "approvals", "evidence", "evidence_version", "evidence_parse_status"}
JOIN_TASK_KEYS = {"task_id", "side", "status"}
JOIN_EVIDENCE_KEYS = {
    "score", "positive_signals", "negative_signals", "namespace_compatibility",
    "namespace_reason_codes", "grain_status", "grain_evidence", "explanation", "warnings"}
JOIN_SIGNAL_KEYS = {"signal_name", "score_delta", "evidence_refs", "explanation"}

# SemanticsPendingItem (api.ts)
SEMANTICS_PENDING_KEYS = {"object_ref", "table", "column", "data_type", "missing"}

# SyncPreview + its nested shapes (api.ts): TagMapEntry / PreviewTable / FoldCollision /
# DroppedJoin / AsOfSuggestion. `collisions` + `dropped_joins` are the #1 loss panels the UI
# must render — always present, both empty only on a clean pull.
SYNC_PREVIEW_KEYS = {
    "summary", "tag_map", "tables", "collisions", "dropped_joins", "brake",
    "as_of_suggestions", "snapshot_hash", "local_baseline_hash"}
PREVIEW_SUMMARY_KEYS = {"tables", "columns", "new", "changed", "unchanged", "removed",
                        "would_quarantine", "semantics_pending"}
TAG_MAP_ENTRY_KEYS = {"om_tag", "mapped_to", "unmapped", "count"}
PREVIEW_TABLE_KEYS = {"table", "status", "columns", "quarantine", "changes"}
FOLD_COLLISION_KEYS = {"table", "fqns"}
DROPPED_JOIN_KEYS = {"table", "columns", "referred", "reason"}
AS_OF_SUGGESTION_KEYS = {"table", "column", "hint"}
BRAKE_KEYS = {"would_hold", "reason"}
# SyncImportResult (api.ts): wraps the standard IngestResult; run id rides the header here too.
SYNC_IMPORT_RESULT_KEYS = {"result", "import_id", "semantics_pending"}


@pytest.fixture(autouse=True)
def _clean_process_globals():
    """Same hygiene as test_full_ingestion_e2e.py: the routes self-register the upload-context
    adapter and the app lifespan seals an overlay config — both PROCESS globals. Clear them after
    every test so nothing leaks into a suite that expects the fail-closed RuntimeError."""
    yield
    _clear_catalog_adapter()
    _clear_overlay_config()


# ── 1. POST /uploads: body is EXACTLY IngestResult; the run id is a HEADER, never a body key ─────


def test_upload_body_is_exactly_ingest_result_and_run_id_rides_the_header(client):
    res = upload_csv(client, "deposits", DEPOSITS_CSV)
    assert res.status_code == 200, res.text
    body = res.json()
    # IngestResult (api.ts): exact body key set — an added or removed field must fail here.
    assert set(body) == INGEST_RESULT_KEYS
    # `ingestion_run_id` is client-attached from the header — the body must NOT carry the run id
    # under any name (the header is the one channel uploadFile() reads it from).
    assert "ingestion_run_id" not in body
    assert "run_id" not in body and "id" not in body
    assert body["status"] in {"ingested", "held", "rejected"}          # IngestResult.status union
    assert body["reason"] is None or isinstance(body["reason"], str)   # IngestResult.reason
    assert body["flagged"] is None or isinstance(body["flagged"], str)  # IngestResult.flagged
    for count_key in ("asserted", "changed_objects", "quarantined"):
        assert isinstance(body[count_key], int)                        # IngestResult counters
    # The X-Ingestion-Run-Id RESPONSE HEADER is present (requestWithResponse reads it).
    assert res.headers[RUN_HEADER].startswith("ingrun_")


# ── 2. a FAILED upload still carries the run-id header (#5 — failed runs stay inspectable) ───────


def test_failed_upload_error_response_still_carries_run_id_header(client):
    res = client.post("/uploads", data={"source": "deposits"},
                      files={"file": ("notes.txt", b"hello", "text/plain")}, headers=AUTH)
    assert res.status_code == 400
    # ApiError.ingestionRunId (api.ts): the client lifts the header off every post-open 4xx/5xx —
    # if the backend drops it, a failed ingest's run record becomes unreachable from the UI.
    run_id = res.headers[RUN_HEADER]
    assert run_id.startswith("ingrun_")
    # And the id it names must resolve: the failed attempt is inspectable via getIngestionRun.
    run = client.get(f"/ingestion-runs/{run_id}", headers=AUTH)
    assert run.status_code == 200
    assert run.json()["status"] == "rejected"


# ── 3. GET /ingestion-runs/{id}: keyed `id`, NOT `run_id` (the exact #15 contract) ───────────────


def test_ingestion_run_record_matches_ingestion_run_interface(client):
    res = upload_csv(client, "deposits", DEPOSITS_CSV)
    run_id = res.headers[RUN_HEADER]
    run = client.get(f"/ingestion-runs/{run_id}", headers=AUTH).json()
    # IngestionRun (api.ts): the record is keyed `id` — a backend rename to `run_id` (or a
    # frontend that still reads `run_id`) must fail RIGHT HERE.
    assert "id" in run and "run_id" not in run
    assert run["id"] == run_id
    # Tight equality on the FULL wire key set (api.ts declares the subset it reads; pinning the
    # whole wire forces any backend field add/remove through this file + api.ts together).
    assert set(run) == INGESTION_RUN_WIRE_KEYS
    assert INGESTION_RUN_TS_KEYS <= set(run)
    assert run["origin_type"] == "upload"              # IngestionRun.origin_type
    assert run["catalog_source"] == "deposits"         # IngestionRun.catalog_source
    assert isinstance(run["status"], str)              # IngestionRun.status (open string)
    assert isinstance(run["actor_role_claims"], list)  # IngestionRun.actor_role_claims
    # IngestionStage (api.ts): every stage row carries exactly the seven declared fields.
    assert run["stages"], "a completed upload must report at least the parse stage"
    for stage in run["stages"]:
        assert set(stage) == INGESTION_STAGE_KEYS
        assert isinstance(stage["stage"], str) and isinstance(stage["state"], str)
        assert isinstance(stage["attempt"], int)
        assert stage["detail"] is None or isinstance(stage["detail"], dict)
    # IngestionStatusEvent (api.ts): append-only history rows, exactly three fields each.
    assert run["status_history"], "every run records at least opened -> terminal"
    for event in run["status_history"]:
        assert set(event) == STATUS_EVENT_KEYS


# ── 4. source normalization (#11): mixed-case 'Sales' is ONE catalog, reachable either way ───────

SALES_CSV = DEPOSITS_CSV.replace("deposits,", "sales,") + "sales,accounts,opened_at,\n"


def test_mixed_case_source_normalizes_to_one_catalog_across_every_surface(client):
    res = client.post("/uploads", data={"source": "Sales"},
                      files={"file": ("sales.csv", SALES_CSV.encode(), "text/csv")}, headers=AUTH)
    assert res.status_code == 200, res.text
    assert res.json()["quarantined"] == 1              # the type-less opened_at row
    # The created run's catalog_source is the NORMALIZED id (IngestionRun.catalog_source).
    run = client.get(f"/ingestion-runs/{res.headers[RUN_HEADER]}", headers=AUTH).json()
    assert run["catalog_source"] == "sales"
    # The uploaded columns are searchable, attributed to the normalized source (SearchHit).
    body = client.get("/search", params={"q": "balance"}, headers=AUTH).json()
    assert set(body) == SEARCH_RESULT_KEYS             # SearchResult (api.ts)
    hit = next(h for h in body["hits"] if h["object_ref"] == "public.accounts.balance")
    assert set(hit) == SEARCH_HIT_KEYS                 # SearchHit (api.ts): exact key set
    assert hit["catalog_source"] == "sales"
    # SearchResult.facets: the six SEARCH_FACET_KEYS groups plus the grain/as_of flag buckets.
    assert set(body["facets"]) == SEARCH_FACET_KEYS | {"grain", "as_of"}
    for buckets in body["facets"].values():
        for bucket in buckets:
            assert set(bucket) == FACET_BUCKET_KEYS    # FacetBucket (api.ts)
    # listQuarantine('Sales') — the UI passes the ORIGINAL-case source (#11) and must resolve to
    # the same data as the normalized id.
    items = client.get("/sources/Sales/quarantine", headers=AUTH).json()
    assert len(items) == 1
    assert set(items[0]) == QUARANTINE_ITEM_KEYS       # QuarantineItem (api.ts)
    assert items[0]["raw"]["column"] == "opened_at"
    assert items[0] == client.get("/sources/sales/quarantine", headers=AUTH).json()[0]


# ── 5. GET /sources/{source}/governance/joins: the governance listing the review screen renders ──

# The e2e's calibrated strong pair (see test_full_ingestion_e2e.py's CSV-design note): a raw
# `customer` feed referencing the mastered `customers` grain scores exactly 80 under Pass C.
PASS_C_CSV = """\
source,table,column,type,is_grain,entity
catx,customer,txn_id,text,,
catx,customer,cust_id,text,,customer
catx,customer,amount,numeric,,
catx,customers,cust_id,text,true,customer
catx,customers,full_name,text,,
"""


def test_join_governance_listing_matches_join_proposal_interfaces(client, monkeypatch):
    monkeypatch.setenv("OVERLAY_PASS_C", "1")     # ingest reads the env per call — set before POST
    res = client.post("/uploads", data={"source": "catx"},
                      files={"file": ("catx.csv", PASS_C_CSV.encode(), "text/csv")}, headers=AUTH)
    assert res.status_code == 200 and res.json()["status"] == "ingested", res.text

    res = client.get("/sources/catx/governance/joins", headers=CONFIRMER)
    assert res.status_code == 200, res.text
    listing = res.json()
    # listJoinProposals return type (api.ts): {source, proposals, divergences, next_cursor}.
    assert set(listing) == JOINS_LISTING_KEYS
    assert listing["source"] == "catx"
    assert isinstance(listing["divergences"], list)    # JoinDivergence[] (empty on first upload)
    (p,) = listing["proposals"]
    # JoinProposal (api.ts): exact key set.
    assert set(p) == JOIN_PROPOSAL_KEYS
    assert set(p["from"]) == set(p["to"]) == {"table", "column"}   # JoinProposal.from / .to
    assert p["status"] in {"PROPOSED", "PARTIALLY_CONFIRMED"}      # JoinProposal.status union
    assert isinstance(p["fact_key"], str) and isinstance(p["proposed_direction"], str)
    assert p["approvals"] == []                                    # JoinApproval[] — none yet
    assert p["tasks"], "a dual-confirm proposal always lists its side tasks"
    for task in p["tasks"]:
        assert set(task) == JOIN_TASK_KEYS                         # JoinTask (api.ts)
    # JoinEvidence (api.ts): a parsed Pass C record carries every declared field.
    assert p["evidence_parse_status"] == "parsed"   # 'parsed'|'partial'|'missing'|'invalid' union
    assert set(p["evidence"]) == JOIN_EVIDENCE_KEYS
    assert p["evidence"]["positive_signals"], "a strong candidate has positive signals"
    for signal in p["evidence"]["positive_signals"] + p["evidence"]["negative_signals"]:
        assert set(signal) == JOIN_SIGNAL_KEYS                     # JoinSignal (api.ts)


# ── 6. connector preview + import: the loss panels (#1) and the wrapped IngestResult ─────────────


@pytest.fixture
def om_seam_with_losses(monkeypatch):
    """The integrations-suite OM seam (fixture pages through `_build_fetch`, no network), with the
    recorded pull EXTENDED to induce both known-loss panels: a composite FK the translation drops
    and two distinct upstream tables that fold to one name — so `dropped_joins` and `collisions`
    are non-empty and their ITEM shapes are pinnable, exactly what the UI must render (#1)."""
    from featuregen.api.routes import integrations as routes

    monkeypatch.setenv("FEATUREGEN_OM_TOKEN__CORP_OM", "secret-bot-token-v-9")
    monkeypatch.setenv("FEATUREGEN_OM_ALLOWED_HOSTS", "om.internal.test")
    page1, page2 = fixture_pages()
    page1["data"].append({   # composite FK -> one DroppedJoin
        "name": "payments", "service": {"name": CARDS_SERVICE},
        "databaseSchema": {"name": "public"},
        "columns": [{"name": "acct", "dataType": "BIGINT"},
                    {"name": "ccy", "dataType": "VARCHAR"}],
        "tableConstraints": [{
            "constraintType": "FOREIGN_KEY", "columns": ["acct", "ccy"],
            "referredColumns": ["mysql_prod.cards_db.public.balances.acct",
                                "mysql_prod.cards_db.public.balances.ccy"]}]})
    for schema in ("sales", "finance"):   # distinct FQNs folding to 'account' -> one FoldCollision
        page1["data"].append({
            "name": "account", "fullyQualifiedName": f"mysql_prod.cards_db.{schema}.account",
            "service": {"name": CARDS_SERVICE}, "database": {"name": "cards_db"},
            "databaseSchema": {"name": schema},
            "columns": [{"name": "id", "dataType": "BIGINT"}]})
    monkeypatch.setattr(routes, "_build_fetch",
                        lambda base_url, token: fixture_fetch(copy.deepcopy(page1),
                                                              copy.deepcopy(page2)))


def _make_sync(client) -> str:
    integ = client.post("/integrations",
                        json={"name": "corp om", "base_url": "https://om.internal.test",
                              "tag_map": CARDS_TAG_MAP}, headers=OWNER)
    assert integ.status_code == 200, integ.text
    sync = client.post(f"/integrations/{integ.json()['integration_id']}/syncs",
                       json={"service_name": CARDS_SERVICE, "target_source": "cards"},
                       headers=OWNER)
    assert sync.status_code == 200, sync.text
    return sync.json()["sync_id"]


def test_sync_preview_and_import_match_sync_preview_interfaces(client, om_seam_with_losses):
    sync_id = _make_sync(client)
    res = client.post(f"/syncs/{sync_id}/preview", headers=VIEWER)
    assert res.status_code == 200, res.text
    preview = res.json()
    # SyncPreview (api.ts): exact key set — `collisions` and `dropped_joins` MUST be present
    # (the #1 drift: build_preview emitted them but the TS interface omitted them, so the UI
    # never rendered the data loss a human was approving).
    assert set(preview) == SYNC_PREVIEW_KEYS
    assert set(preview["summary"]) == PREVIEW_SUMMARY_KEYS       # SyncPreview.summary
    for entry in preview["tag_map"]:
        assert set(entry) == TAG_MAP_ENTRY_KEYS                  # TagMapEntry (api.ts)
    assert preview["tables"]
    for table in preview["tables"]:
        assert set(table) == PREVIEW_TABLE_KEYS                  # PreviewTable (api.ts)
        assert table["status"] in {"new", "changed", "unchanged", "removed"}
        for q in table["quarantine"]:
            assert set(q) == {"column", "reason"}                # PreviewTable.quarantine items
    (collision,) = preview["collisions"]
    assert set(collision) == FOLD_COLLISION_KEYS                 # FoldCollision (api.ts)
    assert isinstance(collision["fqns"], list) and len(collision["fqns"]) == 2
    (dropped,) = preview["dropped_joins"]
    assert set(dropped) == DROPPED_JOIN_KEYS                     # DroppedJoin (api.ts)
    assert isinstance(dropped["columns"], list) and isinstance(dropped["referred"], list)
    assert set(preview["brake"]) == BRAKE_KEYS                   # SyncPreview.brake
    for suggestion in preview["as_of_suggestions"]:
        assert set(suggestion) == AS_OF_SUGGESTION_KEYS          # AsOfSuggestion (api.ts)
    assert isinstance(preview["snapshot_hash"], str)             # SyncPreview.snapshot_hash
    assert isinstance(preview["local_baseline_hash"], str)       # SyncPreview.local_baseline_hash

    # importSync (api.ts): the wrapped IngestResult + the run-id header on the SAME vehicle.
    res = client.post(f"/syncs/{sync_id}/import",
                      json={"snapshot_hash": preview["snapshot_hash"],
                            "local_baseline_hash": preview["local_baseline_hash"]},
                      headers=OWNER)
    assert res.status_code == 200, res.text
    body = res.json()
    assert set(body) == SYNC_IMPORT_RESULT_KEYS                  # SyncImportResult (api.ts)
    assert set(body["result"]) == INGEST_RESULT_KEYS             # nested IngestResult, body-only
    assert isinstance(body["semantics_pending"], int)
    assert res.headers[RUN_HEADER].startswith("ingrun_")         # header here too (#5)


# ── 7. GET /sources/{source}/semantics-pending: the owner-completion queue items ─────────────────

# Semantics-blank columns (no as-of/additivity/unit/currency/entity) land in the pending queue.
LEDGER_CSV = """\
source,table,column,type,is_grain,as_of,definition,sensitivity,joins_to,cardinality,additivity,unit,currency,entity
ledger,balances,acct_id,integer,y,,primary key,,,,,,,
ledger,balances,balance,numeric,,,end-of-day ledger balance,,,,,,,
"""


def test_semantics_pending_items_match_semantics_pending_item_interface(client):
    upload_csv(client, "ledger", LEDGER_CSV)
    res = client.get("/sources/ledger/semantics-pending", headers=AUTH)
    assert res.status_code == 200
    items = res.json()
    assert items, "semantics-blank columns must be queued"
    for item in items:
        assert set(item) == SEMANTICS_PENDING_KEYS               # SemanticsPendingItem (api.ts)
        assert isinstance(item["missing"], list) and item["missing"]
        # the backend's field vocabulary (api.ts documents the set; kept open strings there)
        assert set(item["missing"]) <= {"as_of", "additivity", "unit", "currency", "entity"}
        assert item["data_type"] is None or isinstance(item["data_type"], str)
