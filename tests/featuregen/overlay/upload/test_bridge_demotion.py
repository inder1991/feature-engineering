"""A confirmed cross-catalog bridge must stop being CONFIRMED the moment it leaves VERIFIED.

`demote_bridge_edges` had NO production caller before this suite's sibling (Task 2's reject route).
The three exits from VERIFIED each carry a demotion hook that dispatches on `fact_type` —
`reject_fact` (confirmation_commands.py), `_apply_expiry` (expiry.py) and `_stale_one`
(catalog_changes.py) — and every one of them handled `approved_join` and the semantic bindings and
fell through for `entity_bridge`. So a human-confirmed bridge that drift STALEd or whose confirmation
EXPIRED kept its `entity_bridge_edge` row and went on being read as a live, human-approved sanction.

`projection.py:50` cannot be the home for this: a two-source fact does not fit the
single-`catalog_source` overlay read models, and its bridge branch is deliberately limited to the
dependency index. The demotion belongs where its two siblings already live — beside them, in the
lifecycle hooks that own each exit from VERIFIED. That also keeps it SYNCHRONOUS with the append; a
projector-driven demote would leave the link readable as confirmed for the whole drain window, which
is the very latency the sibling hooks were written to close.

WHAT ACTUALLY LEAKS, precisely — because the two exits differ:

* EXPIRY -> REVERIFY has NO second gate anywhere. `cross_catalog_links._blocked` folds the stream and
  suppresses REJECTED and STALE, and REVERIFY is in neither set, so the planner kept crossing an
  expired bridge AS CONFIRMED, at the top of the strength ranking (+100, "approved by a person").
  This is the arm the traversability tests below turn on.
* DRIFT -> STALE is blocked from `active_bridges` by `_blocked`, so the assembly planner already
  stopped crossing it. The leak there is on every reader that goes to `entity_bridge_edge` DIRECTLY,
  bypassing that fold: `analysis/grounding.py:210` (which suppresses JOIN_IDENTITY_UNCONFIRMED, so
  an answer built on a drift-invalidated link is presented WITHOUT the "no human confirmed this"
  warning) and `contract/invalidation.py:214`, whose docstring already ASSERTS the invariant this
  suite implements: "a reject / expire / stale DELETEs the row".

Test strength: `test_the_planner_does_not_cross_a_drift_staled_bridge` PASSES without the fix
(`_blocked` carries it) and is kept only as a defence-in-depth pin — it is NOT the proof. The
load-bearing assertions are the read-gate signature (stale) and the planner crossings (expiry).
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from tests.featuregen._helpers import mint_test_identity
from tests.featuregen.overlay.upload.test_bridge_candidates import _load

from featuregen.contracts.envelopes import Command, IdentityEnvelope
from featuregen.overlay._lifecycle import _cas_target
from featuregen.overlay.catalog_changes import detect_catalog_changes
from featuregen.overlay.commands import confirm_fact
from featuregen.overlay.expiry import fire_due_overlay_expiries
from featuregen.overlay.identity import EntityBridgeRef, fact_key
from featuregen.overlay.projection import OverlayProjection
from featuregen.overlay.state import fold_overlay_state
from featuregen.overlay.store import load_fact
from featuregen.overlay.upload.bridge_candidates import derive_bridge_candidates
from featuregen.overlay.upload.bridge_projection import active_bridges, project_verified_bridge
from featuregen.overlay.upload.bridge_propose import propose_bridge
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.contract.invalidation import _MISSING, _bridge_fact_signature
from featuregen.overlay.upload.enrich_llm import _ENRICH_ACTOR
from featuregen.overlay.upload.planner.assembly import _Position, reposition_bridges
from featuregen.overlay.upload.planner.contracts import CatalogScopeV1
from featuregen.overlay.upload.upload_catalog import UploadCatalog, ensure_upload_catalog_adapter
from featuregen.projections.runner import run_projection

_NOW = datetime(2026, 7, 29, 12, 0, tzinfo=UTC)
_CORE_TABLE = "public.customer_master"

#: The bridge's crm endpoint. Drift drops it from the UPLOAD (which is what stales the fact) while
#: `graph_node` is left intact on purpose: the planner must stop crossing because the SANCTION was
#: withdrawn, not because the column vanished from under it.
_CRM_ID = CanonicalRow("crm", "customers", "customer_id", "integer", is_grain=True)


def _actor() -> IdentityEnvelope:
    return IdentityEnvelope(subject="upload", actor_kind="human", authenticated=True,
                            auth_method="oidc", role_claims=("data_owner",))


def _two_catalog_customer(db) -> None:
    _load(db, "core", [
        (CanonicalRow("core", "customer_master", "customer_id", "integer", is_grain=True),
         "customer_id"),
        (CanonicalRow("core", "customer_master", "segment", "text"), "categorical"),
    ])
    _load(db, "crm", [(_CRM_ID, "customer_id")])


def _verified_bridge(db) -> tuple[EntityBridgeRef, str]:
    """A real PROPOSE -> CONFIRM -> PROJECT bridge: the shape the whole surface is about."""
    ensure_upload_catalog_adapter()
    _two_catalog_customer(db)
    cand = derive_bridge_candidates(db)[0]
    propose_bridge(db, cand, actor=_ENRICH_ACTOR, now=_NOW)
    ref = EntityBridgeRef(cand.entity_id, cand.left_ref, cand.right_ref)
    key = fact_key(ref, "entity_bridge")
    admin = mint_test_identity(subject="user:admin1", role_claims=("platform-admin",))
    target = _cas_target(fold_overlay_state(load_fact(db, key)))
    res = confirm_fact(db, Command(
        "confirm_fact", "overlay_fact", None,
        {"ref": ref, "fact_type": "entity_bridge", "use_case": None, "target_event_id": target},
        admin, f"confirm-{target}"))
    assert res.accepted, res.denied_reason
    # The bridge's dependency index (projection.py:50) is what drift walks to find it.
    run_projection(db, OverlayProjection())
    assert project_verified_bridge(db, ref, now=_NOW) == "projected"
    return ref, key


def _snapshot_crm(db, *, rows: list[CanonicalRow]) -> list:
    return detect_catalog_changes(db, UploadCatalog("crm", rows), actor=_actor(), now=_NOW,
                                  open_reverify=False)


def _drift_stale(db) -> None:
    """STALE the bridge through the REAL drift scan (`detect_catalog_changes` -> `_stale_one`).

    Deliberately NOT an appended OVERLAY_FACT_STALED: the hook under test hangs off `_stale_one`, so
    a test that hand-appends the event would exercise the fold and never the production path.
    """
    _snapshot_crm(db, rows=[_CRM_ID])                       # establish the prior snapshot
    changes = _snapshot_crm(db, rows=[])                    # the endpoint column is gone
    assert any(c.kind == "drop" for c in changes), changes


def _expire(db) -> None:
    """Fire the expiry timer `confirm_fact` armed. VERIFIED -> REVERIFY."""
    fired = fire_due_overlay_expiries(db, now=datetime.now(UTC) + timedelta(days=4000))
    assert fired >= 1, "no expiry timer fired — the bridge confirm never armed one"


def _scope() -> CatalogScopeV1:
    return CatalogScopeV1(
        scope_id="s-demote", authorized_catalog_sources=("core", "crm"), catalog_state_stamps=(),
        omitted_catalog_sources=(), read_scope_policy_version="1.0.0",
        role_resolution_version="unknown", resolved_at="2026-07-29T00:00:00Z",
        catalog_consideration_truncated=False)


def _crossings(db) -> tuple:
    """What the PLANNER can actually do from the core customer table: the real cross-catalog move
    generator (`assembly.reposition_bridges`), not a query against the projection."""
    return reposition_bridges(db, _Position("customer", "core", _CORE_TABLE), _scope())


def _planner_link(db, key: str):
    """The bridge as the planner's own active set reports it, or None if it is not in it."""
    return next((b for b in active_bridges(db) if b.fact_key == key), None)


def _edge_rows(db, key: str) -> int:
    return db.execute(
        "SELECT count(*) FROM entity_bridge_edge WHERE fact_key = %s", (key,)).fetchone()[0]


# ── MUST-SURVIVE control: none of this may fire while the bridge is still VERIFIED ───────────────

def test_control_a_still_verified_bridge_keeps_its_edge_and_is_crossed_as_confirmed(db):
    """The no-op control. A demote wired to fire unconditionally — or a harness that stales/expires
    by accident — passes every other test in this file and fails this one."""
    _, key = _verified_bridge(db)

    assert _edge_rows(db, key) == 1
    link = _planner_link(db, key)
    assert link is not None and link.status == "confirmed"
    assert link.strength >= 100, "a human confirmation must dominate the ranking"
    assert [m.bridge_fact_key for m in _crossings(db)] == [key]
    assert _bridge_fact_signature(db, f"bridgefact:{key}") != _MISSING


# ── drift -> STALE ───────────────────────────────────────────────────────────────────────────────

def test_drift_stale_demotes_the_projected_bridge_edge(db):
    _, key = _verified_bridge(db)
    _drift_stale(db)

    assert fold_overlay_state(load_fact(db, key)).status == "STALE"
    assert _edge_rows(db, key) == 0


def test_a_drift_staled_bridge_no_longer_reads_as_a_live_governed_sanction(db):
    """THE decisive stale assertion. `contract/invalidation` reads `entity_bridge_edge` DIRECTLY,
    bypassing the fold that `cross_catalog_links` applies — its own docstring states the invariant
    ("a reject / expire / stale DELETEs the row") that nothing implemented. Until the row goes, a
    contract crossing a drift-invalidated bridge keeps hashing to a live VERIFIED sanction and stays
    PROMOTABLE instead of drifting. `analysis/grounding.py:210` reads the same row the same way."""
    _, key = _verified_bridge(db)
    marker = f"bridgefact:{key}"
    assert _bridge_fact_signature(db, marker) != _MISSING

    _drift_stale(db)

    assert _bridge_fact_signature(db, marker) == _MISSING


def test_the_planner_does_not_cross_a_drift_staled_bridge(db):
    """Defence in depth only — this PASSES without the demote, because `_blocked` already suppresses
    a STALE fact from `cross_catalog_links`. Kept so a future change to that fold cannot reopen the
    traversal without failing something. The stale proof is the read-gate test above."""
    _, key = _verified_bridge(db)
    assert [m.bridge_fact_key for m in _crossings(db)] == [key]

    _drift_stale(db)

    assert _planner_link(db, key) is None
    assert _crossings(db) == ()


# ── expiry -> REVERIFY ───────────────────────────────────────────────────────────────────────────

def test_expiry_demotes_the_projected_bridge_edge(db):
    _, key = _verified_bridge(db)
    _expire(db)

    assert fold_overlay_state(load_fact(db, key)).status == "REVERIFY"
    assert _edge_rows(db, key) == 0


def test_the_planner_stops_crossing_an_expired_bridge_as_human_confirmed(db):
    """THE decisive expiry assertion, on planner traversability.

    REVERIFY is in neither of `_blocked`'s suppressed statuses, so nothing but the edge row decides
    whether the planner sees this link as human-approved. Before: crossed as `confirmed`, ranked
    above every derived candidate. After: the crossing survives as a `proposed` candidate — which is
    correct and deliberate, since confirmation annotates rather than gates — but it has lost the
    human sanction it no longer has, and its ranking with it.
    """
    _, key = _verified_bridge(db)
    before = _planner_link(db, key)
    assert before is not None and before.status == "confirmed" and before.strength >= 100
    assert [m.bridge_fact_key for m in _crossings(db)] == [key]

    _expire(db)

    after = _planner_link(db, key)
    assert after is not None, "an expired confirmation retires the SANCTION, not the candidate"
    assert after.status == "proposed"
    assert after.strength < 100, "an expired confirmation must not keep a confirmed link's rank"


def test_the_planner_stops_crossing_an_expired_edge_only_bridge_entirely(db):
    """The literal "no longer crosses" proof, on the edge-ONLY bridge shape.

    A governed bridge can exist with no candidate-evidence row at all — `propose_bridge` returns
    early on an already-governed fact without writing the ledger, and `cross_catalog_links` has an
    explicit branch for verified edges with no candidate row. There the projected edge is the SOLE
    source of traversability, so the demote is the whole difference between a planner that crosses
    an expired link and one that does not.
    """
    _, key = _verified_bridge(db)
    db.execute("DELETE FROM entity_bridge_candidate_evidence WHERE fact_key = %s", (key,))
    assert [m.bridge_fact_key for m in _crossings(db)] == [key]

    _expire(db)

    assert _planner_link(db, key) is None
    assert _crossings(db) == (), "an expired bridge is still traversable by the planner"
