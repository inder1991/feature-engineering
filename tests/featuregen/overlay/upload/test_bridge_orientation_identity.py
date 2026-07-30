"""A bridge has ONE identity, whichever way round its two endpoints are named.

``EntityBridgeRef`` is an UNORDERED pair: ``(left, right)`` and ``(right, left)`` denote the same
bridge, and ``fact_key`` has always canonicalized the endpoints so both fold onto one governed
stream. The ``proposal_fingerprint`` did not — it hashed the ORDERED ``{entity_id, left_ref,
right_ref}`` payload — so the swapped orientation of a bridge produced a DIFFERENT fingerprint under
the SAME ``fact_key``.

That is a governance failure, not a cosmetic one. ``propose_fact``'s sticky-reject guard denies a
re-proposal whose fingerprint was previously rejected. A second fingerprint slipped straight past
it: the fact folded back to DRAFT, and DRAFT is in ``cross_catalog_links.AVAILABLE_STATUSES``, so
the link a human had refused was traversable again by feature generation and the data agents. A
rejection is the one governance action that must be durable; this made it revocable by an upload.

These tests are behavioural — they drive the production commands and read the production fold.
"""
from __future__ import annotations

from dataclasses import asdict, replace
from datetime import UTC, datetime

from tests.featuregen._helpers import mint_test_identity
from tests.featuregen.overlay.upload.test_bridge_candidates import _load, _two_catalog_customer

from featuregen.contracts.envelopes import Command
from featuregen.overlay._lifecycle import _cas_target
from featuregen.overlay.commands import propose_fact, reject_fact
from featuregen.overlay.identity import (
    CatalogObjectRef,
    EntityBridgeRef,
    fact_key,
    proposal_fingerprint,
)
from featuregen.overlay.state import fold_overlay_state
from featuregen.overlay.store import load_fact
from featuregen.overlay.upload.bridge_candidates import derive_bridge_candidates
from featuregen.overlay.upload.bridge_propose import propose_bridge
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.cross_catalog_links import cross_catalog_links
from featuregen.overlay.upload.enrich_llm import _ENRICH_ACTOR
from featuregen.overlay.upload.upload_catalog import ensure_upload_catalog_adapter

_T0 = datetime(2026, 7, 29, 12, 0, tzinfo=UTC)
_ADMIN = mint_test_identity(subject="user:admin1", role_claims=("platform-admin",))


def swap(candidate):
    """The SAME bridge, named the other way round — endpoints and their per-side flags together."""
    return replace(
        candidate,
        left_ref=candidate.right_ref, right_ref=candidate.left_ref,
        left_is_grain=candidate.right_is_grain, right_is_grain=candidate.left_is_grain)


def reject(db, ref: EntityBridgeRef, key: str) -> None:
    target = _cas_target(fold_overlay_state(load_fact(db, key)))
    res = reject_fact(db, Command(
        "reject_fact", "overlay_fact", None,
        {"ref": ref, "fact_type": "entity_bridge", "use_case": None, "target_event_id": target,
         "reason": "these two columns are not the same customer"},
        _ADMIN, f"reject-{target}"))
    assert res.accepted, res.denied_reason


def bridge_candidate(db):
    ensure_upload_catalog_adapter()
    _two_catalog_customer(db)
    return derive_bridge_candidates(db)[0]


# ── the one that matters: a rejection survives the swapped orientation ───────────────────────────

def test_rejecting_a_bridge_also_rejects_its_swapped_orientation(db):
    """propose -> reject -> propose the SWAPPED orientation -> the sticky guard fires and the fact
    stays REJECTED. Before canonicalization the swap produced a different fingerprint under the same
    fact_key, the guard missed it, and the bridge folded back to DRAFT — traversable again."""
    cand = bridge_candidate(db)
    key = propose_bridge(db, cand, actor=_ENRICH_ACTOR, now=_T0)
    ref = EntityBridgeRef(cand.entity_id, cand.left_ref, cand.right_ref)
    reject(db, ref, key)
    assert fold_overlay_state(load_fact(db, key)).status == "REJECTED"

    swapped = swap(cand)
    # The same governed fact — the swap is a naming choice, not a new bridge.
    assert propose_bridge(db, swapped, actor=_ENRICH_ACTOR, now=_T0) == key
    assert fold_overlay_state(load_fact(db, key)).status == "REJECTED"


def test_a_rejected_bridge_is_not_traversable_after_the_swapped_proposal(db):
    """The consequence the fold is a proxy for: `cross_catalog_links` is what feature generation and
    the data agents read, and a REJECTED bridge must be absent from it — before AND after the
    swapped orientation is proposed."""
    cand = bridge_candidate(db)
    key = propose_bridge(db, cand, actor=_ENRICH_ACTOR, now=_T0)
    assert key in {link.fact_key for link in cross_catalog_links(db)}
    reject(db, EntityBridgeRef(cand.entity_id, cand.left_ref, cand.right_ref), key)
    assert cross_catalog_links(db) == ()

    propose_bridge(db, swap(cand), actor=_ENRICH_ACTOR, now=_T0)
    assert cross_catalog_links(db) == ()


def test_the_swapped_proposal_is_denied_by_the_sticky_guard_itself(db):
    """The same property one layer down, through `propose_fact` with a hand-built value: the guard
    must DENY, naming stickiness — not merely fail to change the state by luck of another gate."""
    cand = bridge_candidate(db)
    key = propose_bridge(db, cand, actor=_ENRICH_ACTOR, now=_T0)
    reject(db, EntityBridgeRef(cand.entity_id, cand.left_ref, cand.right_ref), key)

    res = propose_fact(db, Command(
        "propose_fact", "overlay_fact", None,
        {"ref": EntityBridgeRef(cand.entity_id, cand.right_ref, cand.left_ref),
         "fact_type": "entity_bridge",
         "proposed_value": {"entity_id": cand.entity_id,
                            "left_ref": asdict(cand.right_ref),
                            "right_ref": asdict(cand.left_ref)},
         "evidence_ref": None},
        _ENRICH_ACTOR, "swapped"))
    assert not res.accepted
    assert "previously rejected" in (res.denied_reason or "")
    assert fold_overlay_state(load_fact(db, key)).status == "REJECTED"


def test_the_denied_swap_writes_no_second_evidence_row(db):
    """`propose_bridge` stamps the candidate ledger only on an ACCEPTED proposal, and the ledger's
    primary key is the ORDERED five-tuple — so the swap slipping past the guard was also how one
    fact_key came to own two rows with contradictory evidence."""
    cand = bridge_candidate(db)
    key = propose_bridge(db, cand, actor=_ENRICH_ACTOR, now=_T0)
    reject(db, EntityBridgeRef(cand.entity_id, cand.left_ref, cand.right_ref), key)
    propose_bridge(db, swap(cand), actor=_ENRICH_ACTOR, now=_T0)
    assert db.execute("SELECT count(*) FROM entity_bridge_candidate_evidence "
                      "WHERE fact_key = %s", (key,)).fetchone()[0] == 1


# ── the fingerprint itself, and the blast radius of changing it ──────────────────────────────────

def test_the_fingerprint_is_orientation_independent(db):
    """The unit fact behind the guard: one bridge, one fingerprint — derived from the same canonical
    form the fact_key is."""
    left = CatalogObjectRef("core", "column", "public", "customer_master", "customer_id")
    right = CatalogObjectRef("crm", "column", "public", "customers", "customer_id")
    forward = {"entity_id": "customer", "left_ref": asdict(left), "right_ref": asdict(right)}
    backward = {"entity_id": "customer", "left_ref": asdict(right), "right_ref": asdict(left)}
    assert proposal_fingerprint(forward) == proposal_fingerprint(backward)
    assert (fact_key(EntityBridgeRef("customer", left, right), "entity_bridge")
            == fact_key(EntityBridgeRef("customer", right, left), "entity_bridge"))


def test_an_already_canonical_value_keeps_the_fingerprint_it_always_had():
    """Stickiness is stored as a HASH, so a fingerprint that changes shape silently un-sticks every
    rejection already recorded. Canonicalization reorders ONLY the orientation that was producing a
    second identity — a canonically-ordered value hashes byte-identically to before, which is what
    keeps existing REJECTED bridges rejected."""
    left = CatalogObjectRef("core", "column", "public", "customer_master", "customer_id")
    right = CatalogObjectRef("crm", "column", "public", "customers", "customer_id")
    value = {"entity_id": "customer", "left_ref": asdict(left), "right_ref": asdict(right)}
    # the pre-fix digest: the plain payload, hashed exactly as `proposal_fingerprint` used to
    import hashlib
    import json
    legacy = hashlib.sha256(json.dumps(
        {"value": dict(value), "profile_version": None, "thresholds": None},
        sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert proposal_fingerprint(value) == legacy


def test_canonicalization_leaves_non_bridge_values_alone():
    """`proposal_fingerprint` serves every fact type. Only the bridge SHAPE is reordered, so no
    other fact's stickiness is re-keyed by this change."""
    plain = {"classification": "confidential", "left_ref": "not-a-ref"}
    assert proposal_fingerprint(plain) == proposal_fingerprint(dict(plain))
    # an approved_join IS directional (from -> to) and must never be collapsed onto its reverse
    join = {"from_ref": {"catalog_source": "z"}, "to_ref": {"catalog_source": "a"}}
    assert proposal_fingerprint(join) != proposal_fingerprint(
        {"from_ref": join["to_ref"], "to_ref": join["from_ref"]})


def test_the_ref_itself_carries_one_orientation():
    """Canonicalizing at the TYPE is what makes the set of derivation sites complete: `fact_key`,
    `display_object_ref`, the stored `asdict(ref)` payload and any value built from `ref.left_ref` /
    `ref.right_ref` all read the same orientation without each having to remember to sort."""
    left = CatalogObjectRef("core", "column", "public", "customer_master", "customer_id")
    right = CatalogObjectRef("crm", "column", "public", "customers", "customer_id")
    assert EntityBridgeRef("customer", right, left) == EntityBridgeRef("customer", left, right)
    assert EntityBridgeRef("customer", right, left).left_ref == left


# ── a genuinely different bridge is untouched ────────────────────────────────────────────────────

def test_a_different_bridge_is_unaffected_by_another_bridges_rejection(db):
    """Canonicalization must not alias two DIFFERENT bridges onto one identity: rejecting one leaves
    the other proposable, with its own fact_key."""
    ensure_upload_catalog_adapter()
    _two_catalog_customer(db)
    _load(db, "cards", [
        (CanonicalRow("cards", "card_accounts", "customer_id", "integer", is_grain=True),
         "customer_id"),
    ])
    cands = derive_bridge_candidates(db)
    keys = {c.candidate_id: propose_bridge(db, c, actor=_ENRICH_ACTOR, now=_T0) for c in cands}
    assert len(set(keys.values())) == len(cands) >= 2

    victim = cands[0]
    reject(db, EntityBridgeRef(victim.entity_id, victim.left_ref, victim.right_ref),
           keys[victim.candidate_id])
    assert fold_overlay_state(load_fact(db, keys[victim.candidate_id])).status == "REJECTED"
    for c in cands[1:]:
        assert fold_overlay_state(load_fact(db, keys[c.candidate_id])).status == "DRAFT"
    survivors = {link.fact_key for link in cross_catalog_links(db)}
    assert keys[victim.candidate_id] not in survivors
    assert survivors == {keys[c.candidate_id] for c in cands[1:]}
