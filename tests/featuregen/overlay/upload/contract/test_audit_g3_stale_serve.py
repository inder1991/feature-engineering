"""Program-audit G3 (stale-serve composition) — additive F3/F5 verification.

Complements the [3]/[4]/[5] tests in ``test_drift_invalidation.py`` / ``test_projection.py`` with the
pieces those don't pin:

* F3 ORDERING — the per-source ingest locks must be held BEFORE ``validate_minimum`` runs (a lock
  taken after validate would still leave the validated-pre-drift / baselined-post-drift window). The
  probe runs INSIDE validate, from a second session, for every derives AND join-path catalog.
* F5 ROUND-TRIP — the audit's key scenario: correct a governed narrative away and back again. The
  recomputed dependency hash equals the confirm baseline once more (the per-read gate is blind), so
  ONLY the durable INVALIDATED emitted by the correction keeps the promoted stamp demoted until a
  real re-assessment.
* F5 DEMOTE HOOK — the async reject/expire/drift demotion (``demote_semantic_binding``) restores the
  file entity, a dependency-state change that must also emit a durable INVALIDATED.
"""
from __future__ import annotations

import psycopg
from psycopg.types.json import Jsonb
from tests.featuregen._helpers import mint_test_identity
from tests.featuregen.overlay._helpers import seed_verified_via_command

from featuregen.aggregates.ids import mint_id
from featuregen.overlay.identity import CatalogObjectRef
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.contract.author import ContractDraft
from featuregen.overlay.upload.contract.govern import confirm_contract, contract_read_status
from featuregen.overlay.upload.contract.invalidation import dependencies_drifted
from featuregen.overlay.upload.field_correction import apply_field_correction, read_field_cas
from featuregen.overlay.upload.graph import build_graph
from featuregen.overlay.upload.ingest import ingest_source_lock_key
from featuregen.overlay.upload.semantic_bindings.projection import demote_semantic_binding

ADMIN_A = mint_test_identity(subject="user:priya", role_claims=("platform-admin",))
ADMIN_B = mint_test_identity(subject="user:sam", role_claims=("platform-admin",))


# ── fixtures ─────────────────────────────────────────────────────────────────────────────────────
def _bank(db, entity=""):
    build_graph(db, "bank", [
        CanonicalRow("bank", "accounts", "id", "integer", is_grain=True),
        CanonicalRow("bank", "accounts", "balance", "numeric", definition="net of fees",
                     entity=entity),
        CanonicalRow("bank", "accounts", "posted_at", "timestamp", as_of=True)])


def _draft(name="avg_balance_90d"):
    return ContractDraft(name, "Average 90-day ledger balance.", "accounts", "avg_90d", "posted_at",
                         ["public.accounts.balance"],
                         derives_pairs=(("bank", "public.accounts.balance"),))


def _ensure_promoted(db, contract_id):
    """Reach a PROMOTED (design_checked) stamp whatever the draft shape yields: externally pass any
    blocking requirements (⟶ DATA-CHECKED); a requirement-free confirm is already DESIGN-CHECKED."""
    from featuregen.overlay.upload.feature_validation_projection import catch_up
    req_ids = [r[0] for r in db.execute(
        "SELECT requirement_id FROM feature_validation_requirement "
        "WHERE contract_id = %s AND blocking", (contract_id,)).fetchall()]
    for rid in req_ids:
        db.execute(
            "INSERT INTO feature_contract_validation_event "
            "(event_id, contract_id, event_type, payload) VALUES (%s, %s, 'EXTERNAL_PASSED', %s)",
            (mint_id("fcve"), contract_id, Jsonb({"requirement_id": rid})))
    if req_ids:
        catch_up(db)
    status, verif = contract_read_status(db, contract_id)
    assert status == "design_checked" and verif in ("DESIGN-CHECKED", "DATA-CHECKED"), (
        f"precondition: expected a promoted stamp, got {(status, verif)}")


def _invalidated_reasons(db, contract_id):
    return [r[0] for r in db.execute(
        "SELECT payload->>'reason' FROM feature_contract_validation_event "
        "WHERE contract_id = %s AND event_type = 'INVALIDATED' ORDER BY seq",
        (contract_id,)).fetchall()]


def _correct(db, field, action, actor, idem, **kw):
    cas = read_field_cas(db, source="bank", object_ref="public.accounts.balance", field=field)
    res = apply_field_correction(
        db, source="bank", object_ref="public.accounts.balance", field=field, action=action,
        actor=actor, idempotency_key=idem,
        expected_latest_decision_id=cas["latest_decision_id"],
        expected_evidence_set_hash=cas["evidence_set_hash"],
        expected_policy_version=cas["policy_version"], **kw)
    assert res["accepted"] is True, res
    return res


def _four_eyes_override(db, field, value, idem_prefix):
    """A full two-party correction: A proposes the override, B confirms + projects it."""
    _correct(db, field, "propose_override", ADMIN_A, f"{idem_prefix}-p", replacement_value=value)
    return _correct(db, field, "confirm_override", ADMIN_B, f"{idem_prefix}-c",
                    replacement_value=value)


def _graph_value(db, col, object_ref="public.accounts.balance"):
    return db.execute(
        f"SELECT {col} FROM graph_node WHERE catalog_source = 'bank' AND object_ref = %s",
        (object_ref,)).fetchone()[0]


def _probe_locks(dsn, catalogs):
    """From a SECOND session: which of ``catalogs``' ingest source locks are FREE right now."""
    with psycopg.connect(dsn) as probe:
        free = {cs: probe.execute("SELECT pg_try_advisory_xact_lock(%s)",
                                  (ingest_source_lock_key(cs),)).fetchone()[0]
                for cs in catalogs}
        probe.rollback()
    return free


# ── F3 — the source locks are held BEFORE validate_minimum (ordering, not just presence) ────────
def test_f3_source_lock_is_held_before_validate_runs(db, request, monkeypatch):
    """The whole validate→baseline window must sit INSIDE the source lock: probe at validate time."""
    _bank(db)
    dsn = request.getfixturevalue("_dsn")

    import featuregen.overlay.upload.contract.govern as govern
    real_validate = govern.validate_minimum
    probes: dict[str, bool] = {}

    def probing_validate(conn, draft, **kw):
        probes.update(_probe_locks(dsn, ["bank"]))
        return real_validate(conn, draft, **kw)

    monkeypatch.setattr(govern, "validate_minimum", probing_validate)
    confirm_contract(db, _draft(), actor="ds1")
    assert probes == {"bank": False}, (
        "confirm_contract ran validate_minimum WITHOUT already holding the per-source ingest lock — "
        "a same-source ingest/correction can commit mid-confirm and poison the drift baseline")


def test_f3_every_derives_and_join_catalog_is_locked_before_validate(db, request, monkeypatch):
    """A CROSS-CATALOG draft must hold the lock of EVERY derives + join-path catalog at validate
    time (each is a graph the baseline hashes read)."""
    _bank(db)
    dsn = request.getfixturevalue("_dsn")
    draft = ContractDraft(
        "multi_src_feature", "d", "accounts", "avg", "posted_at", ["public.accounts.balance"],
        derives_pairs=(("crm", "public.customers.id"), ("bank", "public.accounts.balance")),
        join_path=({"catalog_source": "risk", "ref": "public.x.y"}, {"ref": "public.no.catalog"}))

    import featuregen.overlay.upload.contract.govern as govern
    real_validate = govern.validate_minimum
    probes: dict[str, bool] = {}

    def probing_validate(conn, d, **kw):
        probes.update(_probe_locks(dsn, ["bank", "crm", "risk"]))
        return real_validate(conn, d, **kw)

    monkeypatch.setattr(govern, "validate_minimum", probing_validate)
    # The crm/risk columns are not in the graph, so the MCV may legitimately refuse the draft — the
    # locks must ALREADY be held when validate runs either way.
    try:
        confirm_contract(db, draft, actor="ds1")
    except Exception:  # noqa: BLE001 — only the lock probes matter here
        pass
    assert probes == {"bank": False, "crm": False, "risk": False}, (
        f"confirm did not hold the ingest source lock for every draft catalog: free={probes}")


# ── F5 — a correction ROUND-TRIP must not resurrect the promoted stamp ──────────────────────────
def test_f5_correction_roundtrip_cannot_resurrect_the_promoted_stamp(db):
    _bank(db)
    c = confirm_contract(db, _draft(), actor="ds1")
    _ensure_promoted(db, c.contract_id)             # the promoted stamp the scenario resurrects

    # Four-eyes correction of the governed narrative: 'net of fees' -> 'gross of fees'.
    _four_eyes_override(db, "definition", "gross of fees", "r1")
    assert _graph_value(db, "definition") == "gross of fees"
    assert "METADATA_CORRECTED" in _invalidated_reasons(db, c.contract_id), (
        "a projecting correction mutated H2c dependency state without emitting "
        "invalidate_contracts_for — the demotion is transient and unexplained")
    assert contract_read_status(db, c.contract_id) == ("needs_external_validation", "UNVERIFIED")

    # ROUND-TRIP: retire the correction and restore the original wording (two-party each step).
    active_ids = [r[0] for r in db.execute(
        "SELECT evidence_id FROM field_evidence WHERE logical_ref = %s AND field_name = %s "
        "AND lifecycle = 'active'", ("bank::public.accounts.balance", "definition")).fetchall()]
    _correct(db, "definition", "reject", ADMIN_B, "r2-reject", selected_evidence_ids=active_ids)
    _four_eyes_override(db, "definition", "net of fees", "r3")
    assert _graph_value(db, "definition") == "net of fees"          # byte-identical to the baseline
    assert dependencies_drifted(db, c.contract_id) is False         # the hash comparison is blind

    # The excursion is DURABLE: only a new assessment may re-clear — never a silent resurrection.
    assert contract_read_status(db, c.contract_id) == ("needs_external_validation", "UNVERIFIED"), (
        "a value round-trip resurrected the promoted stamp with no re-assessment and no event")


# ── F5 — the ASYNC demote hook (reject/expire/drift) emits the durable invalidation ─────────────
def test_f5_entity_demote_hook_emits_invalidated(db):
    _bank(db, entity="account")
    # Govern the entity via the REAL command chain (its sync projection sets entity='customer')…
    ref = CatalogObjectRef("bank", "column", "public", "accounts", "balance")
    key, _confirmed = seed_verified_via_command(
        db, ref=ref, fact_type="entity_assignment", value={"entity_id": "customer"},
        owner="user:alice")
    assert _graph_value(db, "entity") == "customer"
    # …then confirm a contract whose baseline hashes entity='customer' (a clean promoted stamp).
    c = confirm_contract(db, _draft(), actor="ds1")
    _ensure_promoted(db, c.contract_id)

    # The async demotion (reject/expire/drift closer) restores the file entity — a real state change.
    demote_semantic_binding(db, fact_key=key, fact_type="entity_assignment", status="REJECTED")
    assert _graph_value(db, "entity") == "account"
    assert _invalidated_reasons(db, c.contract_id), (
        "the E3 entity demotion rewrote graph_node.entity (H2c dependency state) without emitting "
        "invalidate_contracts_for — the downgrade would be transient and unexplained")
    assert contract_read_status(db, c.contract_id) == ("needs_external_validation", "UNVERIFIED")
