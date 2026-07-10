"""GET /graph/lineage — the catalog lineage graph (bounded BFS, read-scoped like search)."""
from tests.featuregen.api._helpers import (
    AUTH,
    DEPOSITS_CSV,
    OWNER,
    PII_AUTH,
    VIEWER,
    upload_csv,
)

# A second catalog sharing the Customer entity with deposits -> cross-catalog entity bridge.
CARDS_CSV = ("source,table,column,type,is_grain,entity\n"
             "cards,card_holders,holder_id,integer,y,Customer\n"
             "cards,card_holders,credit_limit,numeric,,\n")

# A declared join whose target table was never uploaded -> pending stub + resolved=false edge.
GL_CSV = ("source,table,column,type,joins_to,cardinality\n"
          "gl,entries,entry_id,integer,,\n"
          "gl,entries,batch_id,integer,batches.batch_id,N:1\n")


def _lineage(client, headers=AUTH, **params):
    p = {"ref": "public.accounts.balance", "source": "deposits"}
    p.update(params)
    return client.get("/graph/lineage", params=p, headers=headers)


def _ids(body):
    return {n["id"] for n in body["nodes"]}


def _register_feature(client, name="avg_balance", derives=("public.accounts.balance",)):
    res = client.post("/features", json={
        "name": name,
        "derives_from": [{"catalog_source": "deposits", "object_ref": r} for r in derives],
    }, headers=AUTH)
    assert res.status_code == 200
    return res.json()["feature_id"]


# ---- shape + BFS depth ---------------------------------------------------------------------
def test_anchor_column_depth1_returns_table_and_joined_tables(client):
    upload_csv(client, "deposits", DEPOSITS_CSV)
    body = _lineage(client).json()
    ids = _ids(body)
    assert "deposits:public.accounts" in ids                       # the anchor's table
    assert "deposits:public.accounts.balance" in ids               # carried as a column node
    assert "deposits:public.transactions" in ids                   # joined via reverse N:1
    assert "deposits:public.customers" in ids                      # joined via forward N:1
    assert body["truncated"] is False
    # every table carries its columns via contains edges (the UI collapses them)
    contains = {(e["from"], e["to"]) for e in body["edges"] if e["kind"] == "contains"}
    assert ("deposits:public.accounts", "deposits:public.accounts.balance") in contains
    assert ("deposits:public.transactions", "deposits:public.transactions.amount") in contains
    # forward join keeps the declared orientation + cardinality
    joins = {(e["from"], e["to"]): e for e in body["edges"] if e["kind"] == "join"}
    fwd = joins[("deposits:public.accounts.cust_id", "deposits:public.customers.cust_id")]
    assert fwd["cardinality"] == "N:1" and fwd["resolved"] is True and fwd["layer"] == "joins"
    # reverse join is oriented per traversal with the fan INVERTED (M7): accounts -> transactions
    rev = joins[("deposits:public.accounts.id", "deposits:public.transactions.account_id")]
    assert rev["cardinality"] == "1:N"


def test_frontier_join_between_two_boundary_tables_is_not_omitted(client):
    # Map honesty (IMPORTANT 1): a.x->b.x, a.y->c.y, b.z->c.z. Depth=1 anchored on a.x installs
    # b and c at the depth boundary; BFS never expands either, so the declared b<->c join would
    # be silently dropped and two visible tables would look unrelated. The closing pass emits it.
    upload_csv(client, "probe",
               "source,table,column,type,joins_to,cardinality\n"
               "probe,a,x,integer,b.x,N:1\n"
               "probe,a,y,integer,c.y,N:1\n"
               "probe,b,x,integer,,\n"
               "probe,b,z,integer,c.z,N:1\n"
               "probe,c,y,integer,,\n"
               "probe,c,z,integer,,\n")
    body = client.get("/graph/lineage", params={
        "ref": "public.a.x", "source": "probe", "depth": 1}, headers=AUTH).json()
    ids = _ids(body)
    assert "probe:public.b" in ids and "probe:public.c" in ids
    joins = {tuple(sorted((e["from"], e["to"]))) for e in body["edges"] if e["kind"] == "join"}
    assert tuple(sorted(("probe:public.b.z", "probe:public.c.z"))) in joins  # the b<->c edge
    assert body["truncated"] is False   # the closing pass installs edges only, never nodes


def test_same_catalog_join_cycle_terminates_without_duplicate_edges(client):
    # MINOR 9: a true same-catalog cycle a->b->c->a. The seen-set must halt traversal and the
    # symmetric-edge dedup must keep each declared join once, whichever side reaches it first.
    upload_csv(client, "ring",
               "source,table,column,type,joins_to,cardinality\n"
               "ring,a,id,integer,b.id,N:1\n"
               "ring,b,id,integer,c.id,N:1\n"
               "ring,c,id,integer,a.id,N:1\n")
    body = client.get("/graph/lineage", params={
        "ref": "public.a.id", "source": "ring", "depth": 3}, headers=AUTH).json()
    assert _ids(body) >= {"ring:public.a", "ring:public.b", "ring:public.c"}
    joins = [tuple(sorted((e["from"], e["to"]))) for e in body["edges"] if e["kind"] == "join"]
    assert len(joins) == len(set(joins)) == 3    # each of a-b, b-c, c-a exactly once, no dupes


def test_depth_two_reaches_customers_from_transactions(client):
    upload_csv(client, "deposits", DEPOSITS_CSV)
    d1 = _lineage(client, ref="public.transactions.amount").json()
    assert "deposits:public.customers" not in _ids(d1)
    d2 = _lineage(client, ref="public.transactions.amount", depth=2).json()
    assert "deposits:public.customers" in _ids(d2)


def test_anchor_with_no_edges_returns_anchor_alone(client):
    upload_csv(client, "solo", "source,table,column,type\nsolo,widgets,widget_id,integer\n")
    body = client.get("/graph/lineage", params={
        "ref": "public.widgets.widget_id", "source": "solo", "depth": 3}, headers=AUTH)
    assert body.status_code == 200
    graph = body.json()
    assert _ids(graph) == {"solo:public.widgets", "solo:public.widgets.widget_id"}
    assert {e["kind"] for e in graph["edges"]} == {"contains"}
    assert graph["truncated"] is False


def test_unknown_anchor_404(client):
    assert _lineage(client).status_code == 404                     # nothing uploaded at all
    upload_csv(client, "deposits", DEPOSITS_CSV)
    assert _lineage(client, ref="public.accounts.nope").status_code == 404
    assert _lineage(client, source="nowhere").status_code == 404   # wrong catalog


# ---- features layer: direction asymmetry ---------------------------------------------------
def test_direction_up_vs_down_asymmetric_for_features(client):
    upload_csv(client, "deposits", DEPOSITS_CSV)
    fid = _register_feature(client)
    r = client.post(f"/features/{fid}/consumers",
                    json={"model_ref": "churn_risk_model"}, headers=AUTH)
    assert r.status_code == 200
    down = _lineage(client, direction="down", depth=2).json()
    assert f"feature:{fid}" in _ids(down)                          # column -> feature (depth 1)
    assert "consumer:churn_risk_model" in _ids(down)               # feature -> consumer (depth 2)
    edges = {(e["from"], e["to"], e["kind"]) for e in down["edges"]}
    assert ("deposits:public.accounts.balance", f"feature:{fid}", "derives") in edges
    assert (f"feature:{fid}", "consumer:churn_risk_model", "consumes") in edges
    # the column sits UPSTREAM of the feature: direction=up finds no features from a column
    up = _lineage(client, direction="up", depth=2).json()
    assert f"feature:{fid}" not in _ids(up)
    assert "consumer:churn_risk_model" not in _ids(up)
    assert "deposits:public.customers" in _ids(up)                 # joins are structural: still there


def test_feature_expands_upstream_to_its_other_source_tables(client):
    upload_csv(client, "deposits", DEPOSITS_CSV)
    upload_csv(client, "solo", "source,table,column,type\nsolo,widgets,widget_id,integer\n")
    res = client.post("/features", json={
        "name": "mixed", "derives_from": [
            {"catalog_source": "deposits", "object_ref": "public.accounts.balance"},
            {"catalog_source": "solo", "object_ref": "public.widgets.widget_id"}]},
        headers=AUTH)
    fid = res.json()["feature_id"]
    body = _lineage(client, direction="both", depth=2, layers="features").json()
    assert "solo:public.widgets" in _ids(body)                     # via feature -> source column
    edges = {(e["from"], e["to"]) for e in body["edges"] if e["kind"] == "derives"}
    assert ("solo:public.widgets.widget_id", f"feature:{fid}") in edges


# ---- layers param --------------------------------------------------------------------------
def test_layers_param_excludes_edge_classes(client):
    upload_csv(client, "deposits", DEPOSITS_CSV)
    upload_csv(client, "cards", CARDS_CSV)
    _register_feature(client)
    only_joins = _lineage(client, depth=3, layers="joins").json()
    kinds = {e["kind"] for e in only_joins["edges"]}
    assert kinds == {"contains", "join"}
    assert "cards:public.card_holders" not in _ids(only_joins)     # entity layer off
    only_features = _lineage(client, depth=3, layers="features").json()
    kinds = {e["kind"] for e in only_features["edges"]}
    assert "join" not in kinds and "entity_bridge" not in kinds and "derives" in kinds
    assert "contains" in kinds                                     # containment is structural
    assert "deposits:public.transactions" not in _ids(only_features)


def test_layers_param_validated(client):
    upload_csv(client, "deposits", DEPOSITS_CSV)
    assert _lineage(client, layers="joins,nonsense").status_code == 422
    assert _lineage(client, layers="").status_code == 422
    assert _lineage(client, depth=0).status_code == 422
    assert _lineage(client, depth=4).status_code == 422
    assert _lineage(client, direction="sideways").status_code == 422


# ---- entity bridges ------------------------------------------------------------------------
def test_entity_bridge_reaches_cross_catalog_table(client):
    upload_csv(client, "deposits", DEPOSITS_CSV)
    upload_csv(client, "cards", CARDS_CSV)
    body = _lineage(client, ref="public.customers.cust_id").json()
    assert "cards:public.card_holders" in _ids(body)
    assert "cards:public.card_holders.credit_limit" in _ids(body)  # partner carries its columns
    bridge = next(e for e in body["edges"] if e["kind"] == "entity_bridge")
    assert bridge["layer"] == "entity" and bridge["resolved"] is False
    assert bridge["from"] == "deposits:public.customers.cust_id"
    assert bridge["to"] == "cards:public.card_holders.holder_id"
    assert "cardinality" not in bridge                             # a bridge declares no fan


# ---- pending declared joins ----------------------------------------------------------------
def test_declared_unresolved_join_renders_pending(client):
    upload_csv(client, "gl", GL_CSV)
    body = client.get("/graph/lineage", params={
        "ref": "public.entries.batch_id", "source": "gl"}, headers=AUTH).json()
    stub = next(n for n in body["nodes"] if n["resolved"] is False)
    assert stub["id"] == "gl:public.batches.batch_id"
    assert stub["kind"] == "column" and stub["table"] == "batches"
    assert "catalog_source" not in stub                            # not present in any catalog
    edge = next(e for e in body["edges"] if e["kind"] == "join")
    assert edge["resolved"] is False and edge["cardinality"] == "N:1"
    assert edge["to"] == "gl:public.batches.batch_id"


# ---- read-scope ----------------------------------------------------------------------------
def test_pii_column_absent_without_role_and_its_feature_edge_disappears(client):
    upload_csv(client, "deposits", DEPOSITS_CSV)
    fid = _register_feature(client, name="risk_flag",
                            derives=("public.accounts.balance", "public.customers.email"))
    without = _lineage(client, depth=3).json()
    assert "deposits:public.customers.email" not in _ids(without)  # node absent
    assert f"feature:{fid}" in _ids(without)                       # still reachable via balance
    assert not any("email" in e["from"] or "email" in e["to"] for e in without["edges"])
    with_role = _lineage(client, depth=3, headers=PII_AUTH).json()
    assert "deposits:public.customers.email" in _ids(with_role)
    edges = {(e["from"], e["to"]) for e in with_role["edges"]}
    assert ("deposits:public.customers.email", f"feature:{fid}") in edges


def test_sensitive_anchor_absent_without_role(client):
    upload_csv(client, "deposits", DEPOSITS_CSV)
    # absent must be indistinguishable from nonexistent: 404, never a 403 that leaks existence
    assert _lineage(client, ref="public.customers.email").status_code == 404
    assert _lineage(client, ref="public.customers.email", headers=PII_AUTH).status_code == 200


# ---- stale sources are SHOWN, flagged (unlike search's fail-closed list) --------------------
def test_stale_source_shown_and_flagged(client, conn):
    upload_csv(client, "deposits", DEPOSITS_CSV)
    fid = _register_feature(client)
    conn.execute(
        "UPDATE overlay_drift_watermark "
        "SET last_completed_at = last_completed_at - interval '3 days' "
        "WHERE catalog_source = %s", ("deposits",))
    body = _lineage(client, depth=2, direction="down").json()
    by_id = {n["id"]: n for n in body["nodes"]}
    assert by_id["deposits:public.accounts"]["stale"] is True
    assert by_id["deposits:public.accounts.balance"]["stale"] is True
    assert by_id[f"feature:{fid}"]["stale"] is True                # stale source -> stale feature
    fresh = _lineage(client, ref="public.accounts.id", source="deposits")
    assert fresh.status_code == 200                                # shown, never fail-closed


# ---- RBAC/authz, consistent with search ----------------------------------------------------
def test_rbac_consistent_with_search(client):
    upload_csv(client, "deposits", DEPOSITS_CSV)
    assert _lineage(client, headers={}).status_code == 401         # no identity
    norole = {"X-User": "n", "X-Roles": ""}
    assert _lineage(client, headers=norole).status_code == 403     # no catalog:read
    assert _lineage(client, headers=VIEWER).status_code == 200     # catalog_viewer reads


def test_features_layer_requires_feature_read(client):
    upload_csv(client, "deposits", DEPOSITS_CSV)
    _register_feature(client)
    # data_owner holds catalog:read but not feature:read -> the features layer is absent
    owner = _lineage(client, headers=OWNER, depth=2, layers="features").json()
    assert not any(n["kind"] == "feature" for n in owner["nodes"])
    viewer = _lineage(client, headers=VIEWER, depth=2, layers="features").json()
    assert any(n["kind"] == "feature" for n in viewer["nodes"])


# ---- response bound ------------------------------------------------------------------------
def test_node_cap_truncates_but_keeps_units_atomic(client, conn):
    from datetime import UTC, datetime

    from featuregen.overlay.upload.lineage import lineage_graph

    upload_csv(client, "deposits", DEPOSITS_CSV)
    g = lineage_graph(conn, "deposits", "public.accounts.balance",
                      now=datetime.now(UTC), max_nodes=6)
    assert g["truncated"] is True
    ids = {n["id"] for n in g["nodes"]}
    # the anchor unit is complete: accounts + all 4 visible columns, and nothing partial beyond
    assert ids == {"deposits:public.accounts", "deposits:public.accounts.id",
                   "deposits:public.accounts.posted_at", "deposits:public.accounts.balance",
                   "deposits:public.accounts.cust_id"}
    assert {e["kind"] for e in g["edges"]} == {"contains"}         # no dangling join edges


# ---- node metadata completeness (enrichment, provenance, quarantine, feature stamps) --------
def test_column_nodes_carry_concept_domain_and_as_of_basis(client, conn):
    upload_csv(client, "deposits", DEPOSITS_CSV)
    # concept/domain arrive from LLM enrichment (off in tests); set them directly to prove the wire
    # carries them when present and omits them when null.
    conn.execute("UPDATE graph_node SET concept = %s, domain = %s WHERE catalog_source = 'deposits' "
                 "AND object_ref = 'public.accounts.balance'", ("money_amount", "ledger"))
    by_id = {n["id"]: n for n in _lineage(client).json()["nodes"]}
    bal = by_id["deposits:public.accounts.balance"]
    assert bal["concept"] == "money_amount" and bal["domain"] == "ledger"
    assert "as_of_basis" not in bal                                 # not an as-of column
    assert "concept" not in by_id["deposits:public.accounts.id"]    # omitted when null
    # the as-of column carries the basis from the table's availability_time fact (default posted_at)
    posted = by_id["deposits:public.accounts.posted_at"]
    assert posted["as_of"] is True and posted["as_of_basis"] == "posted_at"


def test_table_node_carries_last_vouched_and_quarantine_pending(client):
    upload_csv(client, "q",
               "source,table,column,type,sensitivity\n"
               "q,orders,id,integer,\n"
               "q,orders,secret,text,bogus_level\n")               # bad sensitivity -> quarantined
    body = client.get("/graph/lineage", params={
        "ref": "public.orders.id", "source": "q"}, headers=AUTH).json()
    orders = next(n for n in body["nodes"] if n["id"] == "q:public.orders")
    assert "T" in orders["last_vouched_at"]                         # ISO8601 drift-vouch after upload
    assert orders["quarantine_pending"] == 1                        # the bogus-sensitivity row
    # a clean table vouches but omits the count entirely
    upload_csv(client, "deposits", DEPOSITS_CSV)
    clean = client.get("/graph/lineage", params={
        "ref": "public.accounts.id", "source": "deposits"}, headers=AUTH).json()
    acc = next(n for n in clean["nodes"] if n["id"] == "deposits:public.accounts")
    assert acc["last_vouched_at"] and "quarantine_pending" not in acc


def test_feature_node_carries_verification_and_omits_empty_rationale(client):
    upload_csv(client, "deposits", DEPOSITS_CSV)
    fid = _register_feature(client)
    feat = next(n for n in _lineage(client, direction="down", depth=2).json()["nodes"]
                if n["id"] == f"feature:{fid}")
    # Direct registration (POST /features) is UNVERIFIED under the honest-verification lifecycle
    # (migration 0973); DESIGN-CHECKED is EARNED only via the governed contract flow. The node
    # faithfully carries whatever the feature's registered stamp is.
    assert feat["verification"] == "UNVERIFIED"
    assert "rationale" not in feat                                  # no hypothesis -> omitted


def test_feature_node_carries_rationale_from_its_hypothesis(client, conn):
    upload_csv(client, "deposits", DEPOSITS_CSV)
    fid = _register_feature(client)
    # A feature born from a hypothesis: feature -> latest contract -> contract_intent (Feature 360).
    conn.execute("INSERT INTO contract_intent (intent_id, hypothesis, intake_mode) "
                 "VALUES (%s, %s, %s)", ("int_x", "sharp balance drops precede churn", "hypothesis"))
    conn.execute("INSERT INTO contract (contract_id, feature_id, feature_name, version, intent_id) "
                 "VALUES (%s, %s, %s, %s, %s)", ("con_x", fid, "avg_balance", 1, "int_x"))
    feat = next(n for n in _lineage(client, direction="down", depth=2).json()["nodes"]
                if n["id"] == f"feature:{fid}")
    assert feat["rationale"] == "sharp balance drops precede churn"


# ---- self-join sanity ----------------------------------------------------------------------
def test_declared_self_join_is_well_formed_edge(client):
    # A self-referential join (employees.manager_id -> employees.id): one table unit, both endpoint
    # columns under it, from != to at column level, and no BFS pathology (edge kept exactly once).
    upload_csv(client, "hr",
               "source,table,column,type,joins_to,cardinality\n"
               "hr,employees,id,integer,,\n"
               "hr,employees,manager_id,integer,employees.id,N:1\n")
    body = client.get("/graph/lineage", params={
        "ref": "public.employees.id", "source": "hr", "depth": 3}, headers=AUTH).json()
    assert _ids(body) >= {"hr:public.employees", "hr:public.employees.id",
                          "hr:public.employees.manager_id"}
    joins = [e for e in body["edges"] if e["kind"] == "join"]
    assert len(joins) == 1                                          # kept once, no BFS doubling
    edge = joins[0]
    assert edge["from"] != edge["to"]                              # well-formed at column level
    assert {edge["from"], edge["to"]} == {
        "hr:public.employees.id", "hr:public.employees.manager_id"}
    assert edge["resolved"] is True and edge["cardinality"] == "N:1"
    assert body["truncated"] is False
