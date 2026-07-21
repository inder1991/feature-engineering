"""Delivery H2c — reverse-dependency rows + INVALIDATED on drift + the read-time SECOND fail-closed gate.

`confirm_contract` now records what catalog state each contract depended on
(`contract_metadata_dependency`, write-once). `invalidate_contracts_for` appends an INVALIDATED
validation event (append-only, idempotent) for every contract version whose dependency drifted — the C4
projection ALREADY demotes a prior DATA-CHECKED when it folds an INVALIDATED (this task only EMITs). And
`contract_read_status` adds the SECOND, read-time fail-closed gate: it recomputes each dependency's
current hash and HARD-downgrades a promoted stamp if ANY item drifted — so a stale DATA-CHECKED can never
be served even while the projection state row still reads DATA-CHECKED (projection lag).
"""
from __future__ import annotations

import psycopg
import pytest
from psycopg.types.json import Jsonb

from featuregen.aggregates.ids import mint_id
from featuregen.overlay.upload.canonical import UNKNOWN_TYPE, CanonicalRow
from featuregen.overlay.upload.contract.author import ContractDraft
from featuregen.overlay.upload.contract.govern import (
    confirm_contract,
    contract_read_status,
    feature_detail,
)
from featuregen.overlay.upload.contract.invalidation import (
    REASON_CATALOG_RETYPED,
    ChangedRef,
    dependencies_drifted,
    invalidate_contracts_for,
)
from featuregen.overlay.upload.feature_validation_projection import catch_up, read_state
from featuregen.overlay.upload.graph import build_graph


# ── fixtures ─────────────────────────────────────────────────────────────────────────────────────────
def _bank(db, source="bank"):
    build_graph(db, source, [
        CanonicalRow(source, "accounts", "id", "integer", is_grain=True),
        CanonicalRow(source, "accounts", "balance", "numeric"),
        CanonicalRow(source, "accounts", "posted_at", "timestamp", as_of=True)])


def _bank_nev(db):
    """`balance` is operationally-unknown (UNKNOWN_TYPE + a numeric declared_type) so the confirm-time
    MCV re-run derives a BLOCKING requirement — the seed for a DATA-CHECKED promotion."""
    build_graph(db, "bank", [
        CanonicalRow("bank", "accounts", "id", "integer", is_grain=True),
        CanonicalRow("bank", "accounts", "balance", UNKNOWN_TYPE, definition="end-of-day balance"),
        CanonicalRow("bank", "accounts", "posted_at", "timestamp", as_of=True)],
        declared_types={"public.accounts.balance": "numeric"})


def _draft(name="avg_balance_90d", source="bank"):
    return ContractDraft(name, "Average 90-day ledger balance.", "accounts", "avg_90d", "posted_at",
                         ["public.accounts.balance"],
                         derives_pairs=((source, "public.accounts.balance"),))


def _deps(db, contract_id):
    return db.execute(
        "SELECT catalog_source, logical_ref, item_hash FROM contract_metadata_dependency "
        "WHERE contract_id = %s ORDER BY logical_ref", (contract_id,)).fetchall()


def _invalidated_count(db, contract_id):
    return db.execute(
        "SELECT count(*) FROM feature_contract_validation_event "
        "WHERE contract_id = %s AND event_type = 'INVALIDATED'", (contract_id,)).fetchone()[0]


def _promote_all_blocking(db, contract_id):
    """Drive the contract to DATA-CHECKED: emit an EXTERNAL_PASSED for every blocking requirement, then
    fold. DATA-CHECKED requires a current pass on EVERY blocking requirement (projection fold)."""
    req_ids = [r[0] for r in db.execute(
        "SELECT requirement_id FROM feature_validation_requirement "
        "WHERE contract_id = %s AND blocking", (contract_id,)).fetchall()]
    assert req_ids, "fixture must produce >= 1 blocking requirement to reach DATA-CHECKED"
    for rid in req_ids:
        db.execute(
            "INSERT INTO feature_contract_validation_event "
            "(event_id, contract_id, event_type, payload) VALUES (%s, %s, 'EXTERNAL_PASSED', %s)",
            (mint_id("fcve"), contract_id, Jsonb({"requirement_id": rid})))
    catch_up(db)


# ── TEST 1 — confirming writes write-once reverse-dep rows for each check-clearing/input item ─────────
def test_confirm_writes_reverse_dependency_rows_write_once(db):
    _bank(db)
    c = confirm_contract(db, _draft(), actor="ds1")

    rows = _deps(db, c.contract_id)
    refs = {r[1] for r in rows}
    # one row per check-clearing / input-binding item: the derives measure, the grain TABLE, the
    # grain-KEY column (is_grain — H2 C-1), and the as-of column — each PUBLIC-FLATTENED to its graph
    # object_ref and each carrying an item_hash.
    assert refs == {"public.accounts.balance", "public.accounts", "public.accounts.id",
                    "public.accounts.posted_at"}
    assert all(r[0] == "bank" for r in rows)
    assert all(r[2] for r in rows)                         # every row has a non-empty item_hash

    # write-once: the 1011 no-mutation trigger rejects UPDATE and DELETE (savepoint per attempt).
    with pytest.raises(psycopg.errors.RaiseException), db.transaction():
        db.execute("UPDATE contract_metadata_dependency SET logical_ref = 'x' WHERE contract_id = %s",
                   (c.contract_id,))
    with pytest.raises(psycopg.errors.RaiseException), db.transaction():
        db.execute("DELETE FROM contract_metadata_dependency WHERE contract_id = %s", (c.contract_id,))
    assert len(_deps(db, c.contract_id)) == 4              # rows survived both rejected mutations


# ── TEST 2 — invalidate_contracts_for emits INVALIDATED (demotes) and is idempotent ──────────────────
def test_invalidate_contracts_for_emits_invalidated_and_is_idempotent(db):
    _bank(db)
    c = confirm_contract(db, _draft(), actor="ds1")
    assert _invalidated_count(db, c.contract_id) == 0

    ref = ChangedRef(catalog_source="bank", reason=REASON_CATALOG_RETYPED,
                     object_ref="public.accounts.balance")
    appended = invalidate_contracts_for(db, changed=[ref])
    assert appended == 1
    assert _invalidated_count(db, c.contract_id) == 1

    # the projection folded the INVALIDATED: the effective stamp is demoted to needs-revalidation.
    st = read_state(db, c.contract_id)
    assert st["validation_status"] == "needs_external_validation"
    assert st["effective_verification"] == "UNVERIFIED"

    # idempotent: re-invalidating for the SAME (reason, ref) appends NO duplicate event.
    assert invalidate_contracts_for(db, changed=[ref]) == 0
    assert _invalidated_count(db, c.contract_id) == 1


# ── TEST 3 — the KEY test: the read gate fails closed on drift WITHOUT any INVALIDATED (projection lag)
def test_read_gate_downgrades_stale_data_checked_without_invalidated(db):
    _bank_nev(db)
    c = confirm_contract(db, _draft(), actor="ds1")
    _promote_all_blocking(db, c.contract_id)

    # the projection now serves a genuine DATA-CHECKED, and the read gate serves it too (no drift yet).
    st = read_state(db, c.contract_id)
    assert (st["validation_status"], st["effective_verification"]) == ("design_checked", "DATA-CHECKED")
    assert contract_read_status(db, c.contract_id) == ("design_checked", "DATA-CHECKED")

    # DRIFT the derives column's declared type IN PLACE, emitting NO INVALIDATED event (projection lag).
    db.execute("UPDATE graph_node SET declared_type = 'text' "
               "WHERE catalog_source = 'bank' AND object_ref = 'public.accounts.balance'")

    # the PROJECTION ALONE still says DATA-CHECKED (no INVALIDATED was folded)...
    st_lagged = read_state(db, c.contract_id)
    assert st_lagged["effective_verification"] == "DATA-CHECKED"
    assert _invalidated_count(db, c.contract_id) == 0
    # ...but dependency drift is detected, and the READ GATE HARD-downgrades: a stale DATA-CHECKED can
    # NEVER be served.
    assert dependencies_drifted(db, c.contract_id) is True
    assert contract_read_status(db, c.contract_id) == ("needs_external_validation", "UNVERIFIED")


# ── TEST 3b — the read gate also fails closed when the dependency's node is DROPPED (missing) ─────────
def test_read_gate_downgrades_when_dependency_node_dropped(db):
    _bank_nev(db)
    c = confirm_contract(db, _draft(), actor="ds1")
    _promote_all_blocking(db, c.contract_id)
    assert contract_read_status(db, c.contract_id) == ("design_checked", "DATA-CHECKED")

    # DROP the derives column's graph_node (a dropped column) — no INVALIDATED emitted.
    db.execute("DELETE FROM graph_node WHERE catalog_source = 'bank' "
               "AND object_ref = 'public.accounts.balance'")
    assert read_state(db, c.contract_id)["effective_verification"] == "DATA-CHECKED"   # projection lag
    assert contract_read_status(db, c.contract_id) == ("needs_external_validation", "UNVERIFIED")


# ── TEST 4 — PRIMARY drift wiring: a two-upload ingest that drops a column INVALIDATES the contract ───
_RETIRE_SRC = "h2c_retire_src"


def _retire_actor():
    from featuregen.contracts.envelopes import IdentityEnvelope
    return IdentityEnvelope(subject="upload", actor_kind="human", authenticated=True,
                            auth_method="oidc", role_claims=("data_owner",))


def _retire_glossary_csv(balance_def):
    header = ("physical_name,business_term,description_business_definition,data_domain,"
              "bian_path,fibo_path\n")
    return (header
            + f'public.accounts.balance,Account Balance,"{balance_def}",Deposits,,\n')


def _retire_ingest(db, csv_text):
    from datetime import UTC, datetime

    from featuregen.overlay.upload.glossary_reader import read_glossary
    from featuregen.overlay.upload.ingest import ingest_upload
    upload = read_glossary(csv_text, source=_RETIRE_SRC)
    res = ingest_upload(db, _RETIRE_SRC, upload.rows, actor=_retire_actor(),
                        now=datetime(2026, 7, 20, tzinfo=UTC), client=None, glossary=upload)
    assert res.status == "ingested"


_PROFILED = ("The ledger balance. The sample profile is NUMERIC, with representative values such as "
             "1250.00; 9.99; 42.50, which supports interpretation.")
_PLAIN = "The ledger balance (revised, no sample profile)."


def test_two_upload_drop_invalidates_dependent_contract_via_ingest_wire(db):
    """Upload 1 makes balance's parsed shape load-bearing; a contract is confirmed on it; upload 2 drops
    the sample facet -> `_retire_dropped_field_decisions` retires that decision -> the wired
    `invalidate_contracts_for` appends an INVALIDATED to the dependent contract."""
    from datetime import timedelta

    from featuregen.overlay.config import OverlayConfig, register_overlay_config
    register_overlay_config(OverlayConfig(
        ttl_default=timedelta(days=180), ttl_min=timedelta(days=30), ttl_max=timedelta(days=365),
        ttl_jitter_fraction=0.1, renewal_grace=timedelta(days=14),
        drift_scan_interval=timedelta(minutes=15), drift_freshness_sla=timedelta(hours=24),
        profiler_require_restricted_role=False))

    _retire_ingest(db, _retire_glossary_csv(_PROFILED))     # upload 1

    draft = ContractDraft("h2c_bal_count", "Count of balances.", None, "count", None,
                          ["public.accounts.balance"],
                          derives_pairs=((_RETIRE_SRC, "public.accounts.balance"),))
    c = confirm_contract(db, draft, actor="ds1")
    assert _invalidated_count(db, c.contract_id) == 0       # no drift yet

    _retire_ingest(db, _retire_glossary_csv(_PLAIN))        # upload 2 — drops the facet

    # the ingest's `_retire_dropped_field_decisions -> invalidate_contracts_for` wire fired.
    assert _invalidated_count(db, c.contract_id) >= 1
    # and the effective stamp is demoted (both the eager fold AND the read gate agree).
    assert contract_read_status(db, c.contract_id) == ("needs_external_validation", "UNVERIFIED")


def _two_col_retire_glossary(balance_def, amount_def):
    header = ("physical_name,business_term,description_business_definition,data_domain,"
              "bian_path,fibo_path\n")
    return (header
            + f'public.accounts.balance,Account Balance,"{balance_def}",Deposits,,\n'
            + f'public.accounts.amount,Txn Amount,"{amount_def}",Deposits,,\n')


def test_multi_column_drift_defers_to_a_single_end_of_ingest_invalidation(db, monkeypatch):
    """[13] A re-upload dropping the load-bearing facet of TWO columns must invalidate BOTH dependent
    contracts, but emit the invalidation ONCE at the END of ingest_upload — not per-retire mid-ingest
    (where `invalidate_contracts_for`'s feature_validation checkpoint FOR UPDATE lock contended with
    the D4 LLM stages). Discriminating: the OLD per-retire wire called `invalidate_contracts_for` once
    per dropped column (twice here); the deferred wire batches both ChangedRefs into one call."""
    from datetime import timedelta

    import featuregen.overlay.upload.ingest as ingest_mod
    from featuregen.overlay.config import OverlayConfig, register_overlay_config
    register_overlay_config(OverlayConfig(
        ttl_default=timedelta(days=180), ttl_min=timedelta(days=30), ttl_max=timedelta(days=365),
        ttl_jitter_fraction=0.1, renewal_grace=timedelta(days=14),
        drift_scan_interval=timedelta(minutes=15), drift_freshness_sla=timedelta(hours=24),
        profiler_require_restricted_role=False))

    _retire_ingest(db, _two_col_retire_glossary(_PROFILED, _PROFILED))   # upload 1: both load-bearing

    c_bal = confirm_contract(db, ContractDraft(
        "h2c_bal_multi", "Count of balances.", None, "count", None, ["public.accounts.balance"],
        derives_pairs=((_RETIRE_SRC, "public.accounts.balance"),)), actor="ds1")
    c_amt = confirm_contract(db, ContractDraft(
        "h2c_amt_multi", "Count of amounts.", None, "count", None, ["public.accounts.amount"],
        derives_pairs=((_RETIRE_SRC, "public.accounts.amount"),)), actor="ds1")
    assert _invalidated_count(db, c_bal.contract_id) == 0
    assert _invalidated_count(db, c_amt.contract_id) == 0

    calls: list[list] = []
    real = ingest_mod.invalidate_contracts_for

    def _spy(conn, *, changed):
        changed = list(changed)
        calls.append(changed)
        return real(conn, changed=changed)

    monkeypatch.setattr(ingest_mod, "invalidate_contracts_for", _spy)

    _retire_ingest(db, _two_col_retire_glossary(_PLAIN, _PLAIN))        # upload 2: drops BOTH facets

    # DEFERRED: exactly ONE invalidate_contracts_for call, at the end of ingest_upload...
    assert len(calls) == 1
    # ...carrying BOTH drifted refs (batched, not one call per retired column)...
    refs = {cr.object_ref for cr in calls[0]}
    assert {"public.accounts.balance", "public.accounts.amount"} <= refs
    # ...and correctness is preserved: BOTH dependent contracts are invalidated.
    assert _invalidated_count(db, c_bal.contract_id) >= 1
    assert _invalidated_count(db, c_amt.contract_id) >= 1


# ── TEST 5 — an unaffected contract (depends only on ref Y) is NOT invalidated by a drift on ref X ────
def test_unaffected_contract_is_not_invalidated(db):
    _bank(db)
    a = confirm_contract(db, _draft(name="feat_balance"), actor="ds1")   # derives public.accounts.balance
    draft_b = ContractDraft("feat_id", "Count of ids.", None, "count", None,
                            ["public.accounts.id"], derives_pairs=(("bank", "public.accounts.id"),))
    b = confirm_contract(db, draft_b, actor="ds1")                       # derives public.accounts.id ONLY

    invalidate_contracts_for(db, changed=[ChangedRef(
        catalog_source="bank", reason=REASON_CATALOG_RETYPED, object_ref="public.accounts.balance")])

    assert _invalidated_count(db, a.contract_id) == 1       # X drifted -> A invalidated
    assert _invalidated_count(db, b.contract_id) == 0       # B depends only on Y -> untouched


# ══════════════ H2 fail-closed hardening (review C-1 / C-2 / I-1fc / I-2fc / M-c) ══════════════════════

# ── C-1: the grain-KEY column (is_grain) is a recorded dependency; flipping it downgrades ─────────────
def test_c1_read_gate_downgrades_when_grain_key_is_grain_flipped(db):
    """C-1: GRAIN_IS_UNIQUE is cleared by the grain-KEY column's is_grain (public.accounts.id), which was
    NOT a recorded dependency before this fix. Confirm to DATA-CHECKED, then flip is_grain off (the grain
    fact being re-projected away) WITHOUT emitting INVALIDATED -> the read gate must downgrade."""
    _bank_nev(db)                                 # id is_grain=True (file-declared)
    c = confirm_contract(db, _draft(), actor="ds1")   # grain "accounts"
    _promote_all_blocking(db, c.contract_id)
    assert "public.accounts.id" in {r[1] for r in _deps(db, c.contract_id)}   # grain-key now recorded
    assert contract_read_status(db, c.contract_id) == ("design_checked", "DATA-CHECKED")

    db.execute("UPDATE graph_node SET is_grain = false "
               "WHERE catalog_source = 'bank' AND object_ref = 'public.accounts.id'")
    assert _invalidated_count(db, c.contract_id) == 0                          # no eager INVALIDATED
    assert dependencies_drifted(db, c.contract_id) is True
    assert contract_read_status(db, c.contract_id) == ("needs_external_validation", "UNVERIFIED")


# ── C-1: the clearing join EDGE is a recorded dependency; dropping it downgrades ──────────────────────
def _bank_joined(db):
    """One catalog, two tables, an OPERATIONAL file-declared join edge (flag-off default) clearing
    JOIN_CONNECTIVITY: accounts.customer_id -> customers.id. The feature is grained on accounts and
    derives customers.tenure_days, so the edge is load-bearing."""
    build_graph(db, "bank", [
        CanonicalRow("bank", "accounts", "id", "integer", is_grain=True),
        CanonicalRow("bank", "accounts", "customer_id", "integer",
                     joins_to="customers.id", cardinality="N:1"),
        CanonicalRow("bank", "accounts", "posted_at", "timestamp", as_of=True),
        CanonicalRow("bank", "customers", "id", "integer"),
        CanonicalRow("bank", "customers", "tenure_days", UNKNOWN_TYPE, definition="tenure days")],
        declared_types={"public.customers.tenure_days": "numeric"})


def _joined_draft():
    return ContractDraft(
        "cust_tenure", "Sum of customer tenure per account.", "accounts", "sum", "posted_at",
        ["public.customers.tenure_days"],
        derives_pairs=(("bank", "public.customers.tenure_days"),),
        join_path=({"kind": "join", "from": "public.accounts.customer_id",
                    "to": "public.customers.id", "cardinality": "N:1"},))


def test_c1_read_gate_downgrades_when_clearing_join_edge_dropped(db):
    """C-1: JOIN_CONNECTIVITY is cleared by a graph_edge (+approved-join status), which no layer hashed
    before. Confirm to DATA-CHECKED, then DROP the clearing edge WITHOUT INVALIDATED -> downgrade."""
    _bank_joined(db)
    c = confirm_contract(db, _joined_draft(), actor="ds1")
    _promote_all_blocking(db, c.contract_id)
    assert any(r[1].startswith("joinedge:") for r in _deps(db, c.contract_id))   # edge now recorded
    assert contract_read_status(db, c.contract_id) == ("design_checked", "DATA-CHECKED")

    db.execute("DELETE FROM graph_edge WHERE catalog_source = 'bank' AND kind = 'joins'")
    assert _invalidated_count(db, c.contract_id) == 0
    assert dependencies_drifted(db, c.contract_id) is True
    assert contract_read_status(db, c.contract_id) == ("needs_external_validation", "UNVERIFIED")


# ── C-2 (b): a dependency UNRESOLVABLE at confirm fails closed (no self-matching MISSING baseline) ────
def test_c2_unresolvable_dependency_at_confirm_fails_closed(db):
    """C-2: a check-clearing dependency that resolves to MISSING at confirm (a cross-catalog / display-
    string join step) must NOT self-match forever. It is POISONED at confirm, so the promoted stamp is
    never servable even with NO drift event and even while the projection reads DATA-CHECKED."""
    _bank_nev(db)
    draft = ContractDraft(
        "avg_balance_90d", "Average 90-day ledger balance per account.", "accounts", "avg_90d",
        "posted_at", ["public.accounts.balance"],
        derives_pairs=(("bank", "public.accounts.balance"),),
        # an entity/bridge step whose endpoints are NON-resolvable display strings (not graph refs).
        join_path=({"kind": "entity", "from": "bank.accounts",
                    "to": "othercat.othertable", "via": "Customer"},))
    c = confirm_contract(db, draft, actor="ds1")
    _promote_all_blocking(db, c.contract_id)

    # the projection ALONE would serve DATA-CHECKED...
    assert read_state(db, c.contract_id)["effective_verification"] == "DATA-CHECKED"
    # ...but the unresolvable dep is poisoned at confirm -> a promoted stamp is NEVER served.
    assert dependencies_drifted(db, c.contract_id) is True
    assert contract_read_status(db, c.contract_id) == ("needs_external_validation", "UNVERIFIED")


# ── C-2 (a): a cross-catalog grain dep carries the RESOLVABLE catalog (holder), not sorted[0] ─────────
def test_c2_cross_catalog_grain_dep_is_resolvable_not_misattributed(db):
    """C-2: the grain catalog is resolved to the source that HOLDS the grain-table node (zeta), never
    sorted(catalogs)[0]=alpha. So the grain dep row RESOLVES (not a dead MISSING self-match) and a later
    drop of the grain table is DETECTED — previously that dep was inert."""
    build_graph(db, "alpha", [CanonicalRow("alpha", "events", "amount", "numeric")])
    build_graph(db, "zeta", [
        CanonicalRow("zeta", "grain_tbl", "id", "integer", is_grain=True),
        CanonicalRow("zeta", "grain_tbl", "ts", "timestamp", as_of=True)])
    draft = ContractDraft(
        "xcat_feature", "Cross-catalog sum.", "grain_tbl", "sum", "ts",
        ["public.events.amount", "public.grain_tbl.id"],
        derives_pairs=(("alpha", "public.events.amount"), ("zeta", "public.grain_tbl.id")))
    c = confirm_contract(db, draft, actor="ds1")

    deps = {(r[0], r[1]) for r in _deps(db, c.contract_id)}   # (catalog_source, logical_ref)
    assert ("zeta", "public.grain_tbl") in deps               # attributed to the HOLDER, not alpha
    assert not any(cs == "alpha" and ref == "public.grain_tbl" for cs, ref in deps)
    assert dependencies_drifted(db, c.contract_id) is False   # resolvable baseline, no drift yet

    db.execute("DELETE FROM graph_node WHERE catalog_source = 'zeta' "
               "AND object_ref = 'public.grain_tbl'")
    assert dependencies_drifted(db, c.contract_id) is True    # the drop is now DETECTED (was inert)


# ── I-1fc: a PROMOTED contract with ZERO dependency rows fails closed; legacy_unassessed unaffected ───
def test_i1fc_promoted_with_zero_dependency_rows_fails_closed(db):
    """I-1fc: the pre-H2c cohort (promoted state row, no dep rows) was gate-blind. A promoted stamp with
    ZERO dependency rows downgrades; a legacy_unassessed (non-promoted) contract is unaffected."""
    _bank_nev(db)
    c = confirm_contract(db, _draft(), actor="ds1")
    _promote_all_blocking(db, c.contract_id)
    assert contract_read_status(db, c.contract_id) == ("design_checked", "DATA-CHECKED")

    # simulate the pre-H2c cohort: strip the dependency rows (WORM -> replica-scoped delete).
    db.execute("SET session_replication_role = replica")
    db.execute("DELETE FROM contract_metadata_dependency WHERE contract_id = %s", (c.contract_id,))
    db.execute("SET session_replication_role = origin")
    assert _deps(db, c.contract_id) == []
    assert contract_read_status(db, c.contract_id) == ("needs_external_validation", "UNVERIFIED")

    # a legacy_unassessed contract (no projected state row) is non-promoted -> zero-dep rule doesn't bite.
    leg = confirm_contract(db, _draft(name="legacy_feat"), actor="ds1")
    db.execute("DELETE FROM feature_contract_validation_state WHERE contract_id = %s", (leg.contract_id,))
    db.execute("SET session_replication_role = replica")
    db.execute("DELETE FROM contract_metadata_dependency WHERE contract_id = %s", (leg.contract_id,))
    db.execute("SET session_replication_role = origin")
    assert contract_read_status(db, leg.contract_id) == ("legacy_unassessed", "UNVERIFIED")


# ── I-2fc: the feature-360 verification is routed through the pointer + read gate (double authority) ──
def test_i2fc_feature_360_verification_routed_through_read_gate(db):
    """I-2fc: feature_detail must serve the GATED effective verification (pointer + contract_read_status),
    never the mutable feature.verification stamp. A drifted current contract downgrades the 360 stamp."""
    _bank_nev(db)
    c = confirm_contract(db, _draft(), actor="ds1")
    _promote_all_blocking(db, c.contract_id)

    detail = feature_detail(db, c.feature_id)
    assert detail["verification"] == "DATA-CHECKED"                          # gated, not the mutable stamp
    assert detail["effective_verification"] == "DATA-CHECKED"
    assert detail["contract"]["effective_verification"] == "DATA-CHECKED"

    # drift a dependency WITHOUT an INVALIDATED (projection lag) -> the 360 stamp DOWNGRADES.
    db.execute("UPDATE graph_node SET declared_type = 'text' "
               "WHERE catalog_source = 'bank' AND object_ref = 'public.accounts.balance'")
    detail2 = feature_detail(db, c.feature_id)
    assert detail2["verification"] == "UNVERIFIED"                           # NOT DESIGN-/DATA-CHECKED
    assert detail2["effective_validation_status"] == "needs_external_validation"
    assert detail2["contract"]["effective_verification"] == "UNVERIFIED"


# ── M-c: a drift that RECURS after a re-clear re-invalidates (tail-scoped dedup, not all-time) ────────
def test_mc_recurred_drift_after_reclear_reinvalidates(db):
    _bank_nev(db)
    c = confirm_contract(db, _draft(), actor="ds1")
    _promote_all_blocking(db, c.contract_id)
    ref = ChangedRef(catalog_source="bank", reason=REASON_CATALOG_RETYPED,
                     object_ref="public.accounts.balance")

    assert invalidate_contracts_for(db, changed=[ref]) == 1
    assert _invalidated_count(db, c.contract_id) == 1
    assert invalidate_contracts_for(db, changed=[ref]) == 0        # idempotent WITHIN the epoch

    _promote_all_blocking(db, c.contract_id)                       # re-clear: moves the epoch past it
    assert invalidate_contracts_for(db, changed=[ref]) == 1        # the RECURRED drift re-invalidates
    assert _invalidated_count(db, c.contract_id) == 2


# ══════════════ Composition-audit (stale-serve cluster) — findings [3] / [4] / [5] ════════════════════
from datetime import UTC, datetime  # noqa: E402

from featuregen.contracts.envelopes import IdentityEnvelope  # noqa: E402
from featuregen.overlay.upload.features import (  # noqa: E402
    FeatureSpec,
    list_features,
    register_feature,
)
from featuregen.overlay.upload.field_correction import (  # noqa: E402
    apply_field_correction,
    read_field_cas,
)
from featuregen.overlay.upload.ingest import ingest_source_lock_key  # noqa: E402
from featuregen.overlay.upload.lineage import lineage_graph  # noqa: E402


@pytest.fixture
def probe_conn(_dsn):
    """A SECOND autocommit session on the same DB — each ``pg_try_advisory_xact_lock`` probe is its own
    single-statement tx, so a successful probe releases immediately (mirrors test_ingest_concurrency)."""
    with psycopg.connect(_dsn, autocommit=True) as c:
        yield c


def _try_lock(probe, key: int) -> bool:
    return probe.execute("SELECT pg_try_advisory_xact_lock(%s)", (key,)).fetchone()[0]


# ── [3] confirm_contract source-locks the drift baseline (single + cross-catalog) ─────────────────────
def test_confirm_contract_holds_the_per_source_lock_across_the_baseline(db, probe_conn):
    """[3]: confirm captures its H2c drift baseline on READ COMMITTED. It must hold the SAME per-source
    ingest lock ``ingest_upload`` takes, so a concurrent same-source ingest cannot COMMIT a drift between
    validate_minimum and the dependency-hash writes. The confirm tx stays open (test conn commits
    nothing), so a second session's try-lock on the derives catalog's key reports CONTENDED."""
    _bank(db)
    confirm_contract(db, _draft(), actor="ds1")
    assert _try_lock(probe_conn, ingest_source_lock_key("bank")) is False   # derives catalog: HELD
    assert _try_lock(probe_conn, ingest_source_lock_key("other")) is True   # different source: free
    db.rollback()                                                           # tx-scoped: released at end
    assert _try_lock(probe_conn, ingest_source_lock_key("bank")) is True


def test_confirm_contract_source_locks_every_derives_join_catalog(db, probe_conn):
    """[3]: a CROSS-catalog draft must lock EVERY catalog in its derives/join set (sorted, before the
    feature lock) — both alpha and zeta are held across the confirm, not just one."""
    build_graph(db, "alpha", [CanonicalRow("alpha", "events", "amount", "numeric")])
    build_graph(db, "zeta", [
        CanonicalRow("zeta", "grain_tbl", "id", "integer", is_grain=True),
        CanonicalRow("zeta", "grain_tbl", "ts", "timestamp", as_of=True)])
    draft = ContractDraft(
        "xcat_lockset", "Cross-catalog sum.", "grain_tbl", "sum", "ts",
        ["public.events.amount", "public.grain_tbl.id"],
        derives_pairs=(("alpha", "public.events.amount"), ("zeta", "public.grain_tbl.id")))
    confirm_contract(db, draft, actor="ds1")
    assert _try_lock(probe_conn, ingest_source_lock_key("alpha")) is False   # both derives catalogs HELD
    assert _try_lock(probe_conn, ingest_source_lock_key("zeta")) is False


# ── [4] list_features + lineage serve the READ-GATED stamp, never the mutable feature row ─────────────
def test_list_features_serves_read_gated_stamp_not_mutable_feature_row(db):
    """[4]: GET /features (list_features) must serve the pointer's contract routed through the read gate,
    not ``feature.verification`` (never demoted by drift). A drifted current contract lists DOWNGRADED."""
    _bank_nev(db)
    c = confirm_contract(db, _draft(), actor="ds1")
    _promote_all_blocking(db, c.contract_id)

    listed = {f["feature_id"]: f for f in list_features(db)}[c.feature_id]
    assert listed["verification"] == "DATA-CHECKED"                 # gated, not the mutable stamp
    assert listed["effective_verification"] == "DATA-CHECKED"

    # DRIFT a dependency WITHOUT an INVALIDATED (projection lag) — the mutable feature row stays the
    # promoted DESIGN-CHECKED, but the LIST must serve the gated (downgraded) stamp.
    db.execute("UPDATE graph_node SET declared_type = 'text' "
               "WHERE catalog_source = 'bank' AND object_ref = 'public.accounts.balance'")
    assert db.execute("SELECT verification FROM feature WHERE feature_id = %s",
                      (c.feature_id,)).fetchone()[0] == "DESIGN-CHECKED"   # mutable stamp NOT demoted
    listed2 = {f["feature_id"]: f for f in list_features(db)}[c.feature_id]
    assert listed2["verification"] == "UNVERIFIED"                  # DOWNGRADED, not DESIGN-/DATA-CHECKED
    assert listed2["effective_validation_status"] == "needs_external_validation"


def test_list_features_directly_registered_feature_keeps_honest_stamp(db):
    """[4]: a feature with NO governing contract keeps its honest ``feature`` stamp (UNVERIFIED) and
    carries no gated effective fields — the gate only overrides a governed feature."""
    fid = register_feature(db, FeatureSpec(name="direct_only_feat"))
    listed = {f["feature_id"]: f for f in list_features(db)}[fid]
    assert listed["verification"] == "UNVERIFIED"
    assert "effective_verification" not in listed


def test_lineage_feature_stamp_serves_read_gated_stamp_not_mutable(db):
    """[4]: the lineage graph's feature node stamp is routed through the pointer + read gate, so a
    drifted feature shows a DOWNGRADED stamp on the lineage surface — matching Feature 360 + the list."""
    _bank_nev(db)
    c = confirm_contract(db, _draft(), actor="ds1")
    _promote_all_blocking(db, c.contract_id)

    def _feat_stamp():
        g = lineage_graph(db, "bank", "public.accounts.balance", now=datetime(2026, 7, 20, tzinfo=UTC),
                          direction="both", depth=2, layers={"features"}, roles=())
        node = next(n for n in g["nodes"]
                    if n.get("kind") == "feature" and n.get("feature_id") == c.feature_id)
        return node["verification"]

    assert _feat_stamp() == "DATA-CHECKED"                          # gated
    db.execute("UPDATE graph_node SET declared_type = 'text' "
               "WHERE catalog_source = 'bank' AND object_ref = 'public.accounts.balance'")
    assert _feat_stamp() == "UNVERIFIED"                            # DOWNGRADED on the lineage graph
    assert db.execute("SELECT verification FROM feature WHERE feature_id = %s",
                      (c.feature_id,)).fetchone()[0] == "DESIGN-CHECKED"   # mutable stamp NOT demoted


# ── [5] a projecting F3 correction emits INVALIDATED on a dependent confirmed contract ────────────────
def _admin(subject):
    return IdentityEnvelope(subject=subject, actor_kind="human", authenticated=True,
                            auth_method="oidc", role_claims=("platform-admin",))


def _correct(db, action, actor, idem, **body):
    cas = read_field_cas(db, source="bank", object_ref="public.accounts.balance", field="definition")
    return apply_field_correction(
        db, source="bank", object_ref="public.accounts.balance", field="definition", action=action,
        actor=actor, idempotency_key=idem, expected_latest_decision_id=cas["latest_decision_id"],
        expected_evidence_set_hash=cas["evidence_set_hash"],
        expected_policy_version=cas["policy_version"], **body)


def test_field_correction_projecting_emits_invalidated_on_dependent_contract(db):
    """[5]: a four-eyes correction (propose_override then confirm_override) that re-projects a
    graph_node display column a confirmed contract depends on must emit a DURABLE + AUDITED INVALIDATED
    on that contract — consistent with the ingest dropped-field wire (not a silent per-read downgrade)."""
    _bank(db)
    c = confirm_contract(db, _draft(), actor="ds1")           # derives public.accounts.balance
    assert _invalidated_count(db, c.contract_id) == 0

    p = _correct(db, "propose_override", _admin("human_a"), "p1", replacement_value="net of fees")
    assert p["accepted"] is True
    r = _correct(db, "confirm_override", _admin("human_b"), "c1", replacement_value="net of fees")
    assert r["accepted"] is True                             # projected the corrected definition

    # the correction's eager wire appended an INVALIDATED (durable + auditable on /contracts)...
    assert _invalidated_count(db, c.contract_id) >= 1
    reasons = [r_[0] for r_ in db.execute(
        "SELECT payload->>'reason' FROM feature_contract_validation_event "
        "WHERE contract_id = %s AND event_type = 'INVALIDATED'", (c.contract_id,)).fetchall()]
    assert "METADATA_CORRECTED" in reasons
    # ...and the read gate agrees (the definition drifted vs the confirm baseline).
    assert contract_read_status(db, c.contract_id) == ("needs_external_validation", "UNVERIFIED")


def test_field_correction_propose_only_does_not_invalidate(db):
    """[5]: propose_override is NON-projecting (it only surfaces a pending proposal), so it must NOT
    invalidate a dependent contract — only a projecting confirm/reject does."""
    _bank(db)
    c = confirm_contract(db, _draft(), actor="ds1")
    p = _correct(db, "propose_override", _admin("human_a"), "p1", replacement_value="net of fees")
    assert p["accepted"] is True
    assert _invalidated_count(db, c.contract_id) == 0        # a bare proposal never invalidates
