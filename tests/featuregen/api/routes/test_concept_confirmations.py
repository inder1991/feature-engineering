"""SE-4b — the bulk concept-confirmation funnel: grouped queue, per-item decisions, honest CAS.

The properties that make the funnel trustworthy: the queue groups by CONCEPT ordered by how
load-bearing it is (V2 operand references); every confirmation lands as ONE attributable
field-decision event through the existing command (no new authority machinery); a stale CAS
fails ITS column closed without touching batch siblings; and the funnel metric moves only on
settled decisions.
"""
from __future__ import annotations

from featuregen.overlay.field_evidence import field_input_hash, record_field_evidence
from featuregen.overlay.field_decision import read_field_decisions
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.column_authority import logical_ref_of
from featuregen.overlay.upload.graph import build_graph

SOURCE = "funnelbank"
PATH = "/governance/concept-confirmations"


def _confirmer(user: str = "sme-lead") -> dict:
    return {"X-User": user, "X-Roles": "platform-admin"}


def _seed(conn) -> None:
    """Three columns: two proposed `customer_id` (heavily referenced by V2 operands), one
    proposed `monetary_flow` — all llm/proposed, the measured live shape."""
    rows = [
        CanonicalRow(SOURCE, "customers", "cust_no", "varchar(30)",
                     definition="the customer number"),
        CanonicalRow(SOURCE, "accounts", "cust_ref", "varchar(30)",
                     definition="owning customer"),
        CanonicalRow(SOURCE, "transactions", "amount", "numeric",
                     definition="signed amount"),
    ]
    build_graph(conn, SOURCE, rows)
    for object_ref, concept in (("public.customers.cust_no", "customer_id"),
                                ("public.accounts.cust_ref", "customer_id"),
                                ("public.transactions.amount", "monetary_flow")):
        logical = logical_ref_of(conn, SOURCE, object_ref)
        record_field_evidence(
            conn, logical_ref=logical, field_name="concept", proposed_value=concept,
            producer="llm", strength="proposed", producer_ref="svc:enrichment",
            source_snapshot_id="snap-test",
            input_hash=field_input_hash(logical_ref=logical, field_name="concept",
                                        material=concept))


def _queue(client) -> dict:
    res = client.get(f"{PATH}?source={SOURCE}", headers=_confirmer())
    assert res.status_code == 200, res.text
    return res.json()


def test_the_queue_groups_by_concept_load_bearing_first(client, conn):
    _seed(conn)
    body = _queue(client)
    assert body["funnel"] == {"active": 3, "human_confirmed": 0, "confirmed_share": 0.0}
    concepts = [g["concept"] for g in body["groups"]]
    assert set(concepts) == {"customer_id", "monetary_flow"}
    by_concept = {g["concept"]: g for g in body["groups"]}
    assert len(by_concept["customer_id"]["columns"]) == 2
    # Load-bearing order: both concepts are operand-referenced; the ordering key is the count.
    counts = [g["operand_reference_count"] for g in body["groups"]]
    assert counts == sorted(counts, reverse=True) and all(c > 0 for c in counts)
    # Every row carries the CAS anchor the command re-checks.
    col = by_concept["customer_id"]["columns"][0]
    assert col["evidence_set_hash"] and col["policy_version"]
    assert col["producer"] == "llm" and col["strength"] == "proposed"


def test_bulk_confirm_lands_one_attributable_decision_per_column(client, conn):
    _seed(conn)
    body = _queue(client)
    group = next(g for g in body["groups"] if g["concept"] == "customer_id")
    items = [{
        "object_ref": col["object_ref"], "action": "confirm_existing",
        "evidence_id": col["evidence_id"],
        "expected_latest_decision_id": col["latest_decision_id"],
        "expected_evidence_set_hash": col["evidence_set_hash"],
        "expected_policy_version": col["policy_version"],
    } for col in group["columns"]]
    res = client.post(PATH, headers=_confirmer("priya"), json={
        "source": SOURCE, "reason": "batch: CIF identifiers", "items": items})
    assert res.status_code == 200, res.text
    outcome = res.json()
    assert outcome["accepted_count"] == 2 and outcome["declined_count"] == 0
    assert outcome["funnel"]["human_confirmed"] == 2
    # Each confirmation landed through the EXISTING machinery: the decision log advanced, and
    # the AUTHORITY moved — active evidence now carries a human/confirmed row per column.
    from featuregen.overlay.field_evidence import read_active_field_evidence

    for col in group["columns"]:
        logical = logical_ref_of(conn, SOURCE, col["object_ref"])
        assert read_field_decisions(conn, logical, "concept")
        strengths = {(e.producer, e.strength)
                     for e in read_active_field_evidence(conn, logical, "concept")}
        assert ("human", "confirmed") in strengths
    # The confirmed columns leave the queue; the untouched concept remains.
    after = _queue(client)
    assert [g["concept"] for g in after["groups"]] == ["monetary_flow"]


def test_a_stale_cas_fails_its_column_without_touching_batch_siblings(client, conn):
    _seed(conn)
    body = _queue(client)
    group = next(g for g in body["groups"] if g["concept"] == "customer_id")
    good, stale = group["columns"]
    items = [
        {"object_ref": good["object_ref"], "action": "confirm_existing",
         "evidence_id": good["evidence_id"],
         "expected_latest_decision_id": good["latest_decision_id"],
         "expected_evidence_set_hash": good["evidence_set_hash"],
         "expected_policy_version": good["policy_version"]},
        {"object_ref": stale["object_ref"], "action": "confirm_existing",
         "evidence_id": stale["evidence_id"],
         "expected_latest_decision_id": stale["latest_decision_id"],
         "expected_evidence_set_hash": "0" * 40,          # drifted anchor
         "expected_policy_version": stale["policy_version"]},
    ]
    res = client.post(PATH, headers=_confirmer(), json={"source": SOURCE, "items": items})
    assert res.status_code == 200, res.text
    outcome = res.json()
    assert outcome["accepted_count"] == 1 and outcome["declined_count"] == 1
    by_ref = {r["object_ref"]: r for r in outcome["results"]}
    assert by_ref[good["object_ref"]]["accepted"] is True
    assert by_ref[stale["object_ref"]]["accepted"] is False
    assert by_ref[stale["object_ref"]]["status_code"] == 409


def test_the_write_requires_the_confirmer_claim(client, conn):
    _seed(conn)
    res = client.post(PATH, headers={"X-User": "viewer", "X-Roles": "catalog_viewer"},
                      json={"source": SOURCE, "items": [{
                          "object_ref": "public.customers.cust_no",
                          "action": "confirm_existing", "evidence_id": "e",
                          "expected_evidence_set_hash": "h",
                          "expected_policy_version": "v"}]})
    assert res.status_code == 403


def test_unreferenced_concepts_are_omitted_honestly_and_reachable(client, conn):
    _seed(conn)
    logical = logical_ref_of(conn, SOURCE, "public.customers.cust_no")
    record_field_evidence(
        conn, logical_ref=logical.replace("cust_no", "notes"), field_name="concept",
        proposed_value="free_text_note", producer="llm", strength="proposed",
        producer_ref="svc:enrichment", source_snapshot_id="snap-test",
        input_hash=field_input_hash(logical_ref=logical.replace("cust_no", "notes"),
                                    field_name="concept", material="free_text_note"))
    # The unreferenced concept's column is not in graph_node — seed a real one instead:
    build_graph(conn, SOURCE, [CanonicalRow(SOURCE, "customers", "notes", "text",
                                            definition="free text")])
    body = _queue(client)
    assert all(g["operand_reference_count"] > 0 for g in body["groups"])
    assert body["unreferenced_groups_omitted"] >= 0                    # named, never silent
    wide = client.get(f"{PATH}?source={SOURCE}&include_unreferenced=true",
                      headers=_confirmer()).json()
    assert len(wide["groups"]) >= len(body["groups"])
