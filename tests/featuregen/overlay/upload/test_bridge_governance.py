"""The entity-bridge governance listing — the read side of the confirmation surface.

Nine cross-catalog links sit PROPOSED on the live catalog and, before this, there was no way to
review them: `overlay_proposal` deliberately excludes entity bridges (`projection.py:50` — a
two-source fact cannot live in a single-`catalog_source` read model), so the semantic-binding
listing pattern returns EMPTY here, and an empty list reads as "nothing pending" when nine are.

Three properties this suite exists to pin:

* **Read-scoping.** `cross_catalog_links` takes no `roles`. Exposed unchanged through a governance
  route it becomes an EXISTENCE ORACLE: a caller without `pii_reader` would learn the count, the
  ids and the existence of bridges on hidden columns. A hidden endpoint drops the WHOLE row.
* **W4 — a governed bridge can exist with NO evidence row.** `propose_bridge` returns early on an
  already-governed fact without writing the ledger row, so the evidence table alone under-reports.
  Silent omission is the failure this redesign exists to remove, and it must fail here.
* **`available_actions` is server-decided.** The UI must never compute four-eyes itself.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import asdict
from datetime import UTC, datetime

from tests.featuregen._helpers import mint_test_identity
from tests.featuregen.overlay.upload.test_bridge_candidates import _load, _two_catalog_customer

from featuregen.contracts.envelopes import Command
from featuregen.overlay._lifecycle import _cas_target
from featuregen.overlay.commands import confirm_fact, reject_fact
from featuregen.overlay.identity import EntityBridgeRef, fact_key
from featuregen.overlay.state import fold_overlay_state
from featuregen.overlay.store import load_fact
from featuregen.overlay.upload.bridge_candidates import derive_bridge_candidates
from featuregen.overlay.upload.bridge_governance import (
    list_bridge_proposals,
    load_bridge_context,
)
from featuregen.overlay.upload.bridge_propose import propose_bridge
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.enrich_llm import _ENRICH_ACTOR
from featuregen.overlay.upload.upload_catalog import ensure_upload_catalog_adapter

_NOW = datetime(2026, 7, 29, 12, 0, tzinfo=UTC)

#: A caller holding neither sensitivity role. Read-scoping must hide anything tagged.
_PLAIN_ROLES = ("platform-admin", "catalog:read")


def _bridge(db) -> tuple[EntityBridgeRef, str]:
    """The `core.customer_master.customer_id <-> crm.customers.customer_id` bridge, PROPOSED under
    the service actor (R1: the system proposes, a human disposes)."""
    ensure_upload_catalog_adapter()
    _two_catalog_customer(db)
    cand = derive_bridge_candidates(db)[0]
    key = propose_bridge(db, cand, actor=_ENRICH_ACTOR, now=_NOW)
    return EntityBridgeRef(cand.entity_id, cand.left_ref, cand.right_ref), key


def _confirm(db, ref: EntityBridgeRef, *, subject: str = "user:admin1") -> None:
    key = fact_key(ref, "entity_bridge")
    admin = mint_test_identity(subject=subject, role_claims=("platform-admin",))
    target = _cas_target(fold_overlay_state(load_fact(db, key)))
    res = confirm_fact(db, Command(
        "confirm_fact", "overlay_fact", None,
        {"ref": ref, "fact_type": "entity_bridge", "use_case": None, "target_event_id": target},
        admin, f"confirm-{target}"))
    assert res.accepted, res.denied_reason


def _hide(db, source: str, table: str, column: str, *, tag: str = "pii") -> None:
    db.execute(
        "UPDATE graph_node SET sensitivity = %s WHERE catalog_source = %s AND object_ref = %s",
        (tag, source, f"public.{table}.{column}"))


# ── the listing exists at all, and does not follow the overlay_proposal pattern ──────────────────

def test_a_proposed_bridge_is_listed_though_overlay_proposal_holds_nothing(db):
    """THE regression guard. `projection.py:50` short-circuits entity bridges, so a listing built on
    `overlay_proposal` returns empty — indistinguishable from "nothing pending"."""
    _bridge(db)
    assert db.execute(
        "SELECT count(*) FROM overlay_proposal WHERE fact_type = 'entity_bridge'").fetchone()[0] == 0
    rows = list_bridge_proposals(db)
    assert len(rows) == 1
    assert rows[0]["status"] == "PROPOSED"


def test_state_is_folded_from_the_event_stream_not_from_a_read_model(db):
    """DRAFT before the confirm, VERIFIED after — in the SAME uncommitted transaction, with no
    projector run in between (W3: the fold reads the stream, so no drain is needed)."""
    ref, _ = _bridge(db)
    assert list_bridge_proposals(db)[0]["status"] == "PROPOSED"
    _confirm(db, ref)
    assert list_bridge_proposals(db)[0]["status"] == "VERIFIED"


def test_a_rejected_bridge_is_not_listed(db):
    """REJECTED is terminal — there is no decision left to make."""
    ref, key = _bridge(db)
    admin = mint_test_identity(subject="user:admin1", role_claims=("platform-admin",))
    target = _cas_target(fold_overlay_state(load_fact(db, key)))
    res = reject_fact(db, Command(
        "reject_fact", "overlay_fact", None,
        {"ref": ref, "fact_type": "entity_bridge", "use_case": None, "target_event_id": target,
         "reason": "branch code is not a branch name"},
        admin, f"reject-{target}"))
    assert res.accepted, res.denied_reason
    assert list_bridge_proposals(db) == []


# ── W4: a governed bridge with NO evidence row must not be silently omitted ──────────────────────

def test_a_governed_bridge_with_no_evidence_row_is_still_surfaced(db):
    """W4. `bridge_propose.py` returns early on an already-governed fact WITHOUT writing the
    evidence row, so the ledger under-reports the governed population. A pending decision nobody
    can see is precisely the failure this redesign removes: enumerating the ledger alone must not
    be enough to pass."""
    _ref, key = _bridge(db)
    # The exact shape the early return leaves behind: the fact is governed, the ledger row is not.
    db.execute("DELETE FROM entity_bridge_candidate_evidence WHERE fact_key = %s", (key,))
    assert db.execute("SELECT count(*) FROM entity_bridge_candidate_evidence").fetchone()[0] == 0

    rows = list_bridge_proposals(db)
    assert len(rows) == 1, "a governed bridge with no ledger row was SILENTLY OMITTED"
    assert rows[0]["fact_key"] == key
    assert rows[0]["status"] == "PROPOSED"
    # The endpoints come from the fact's own payload, so the row is reviewable without the ledger.
    assert rows[0]["left"]["column"] == "customer_id"
    assert rows[0]["right"]["column"] == "customer_id"
    # …and the discrepancy is REPORTED, not papered over: the confirmer is told the derivation
    # evidence behind this bridge is missing.
    assert rows[0]["evidence_present"] is False


def test_evidence_present_is_true_when_the_ledger_row_exists(db):
    """The control for the test above — `evidence_present` must discriminate, not be a constant."""
    _bridge(db)
    assert list_bridge_proposals(db)[0]["evidence_present"] is True


# ── read-scoping: a hidden endpoint drops the WHOLE row ──────────────────────────────────────────

def test_a_bridge_on_a_pii_endpoint_is_dropped_for_a_caller_without_pii_reader(db):
    """The existence-oracle rule (`semantic_binding_governance.py:269`): ABSENCE, not redaction.
    A caller without `pii_reader` must learn no count, no id and no existence."""
    _bridge(db)
    _hide(db, "core", "customer_master", "customer_id", tag="pii")
    assert list_bridge_proposals(db, roles=_PLAIN_ROLES) == []


def test_the_same_bridge_is_visible_to_a_caller_who_holds_pii_reader(db):
    """The control: the drop above must be the ROLES, not the tag making the row unbuildable."""
    _bridge(db)
    _hide(db, "core", "customer_master", "customer_id", tag="pii")
    rows = list_bridge_proposals(db, roles=(*_PLAIN_ROLES, "pii_reader"))
    assert len(rows) == 1


def test_the_far_endpoint_is_scoped_too(db):
    """A bridge names TWO columns. Scoping only the near side leaks the far one."""
    _bridge(db)
    _hide(db, "crm", "customers", "customer_id", tag="pii")
    assert list_bridge_proposals(db, roles=_PLAIN_ROLES) == []


def test_the_governed_restriction_floor_hides_a_bridge_the_raw_tag_does_not(db):
    """Migration 1032's whole point. A glossary attests NO sensitivity — every FTR column has
    `sensitivity = NULL` — while the concept cascade independently ruled 28 of them restricted.
    A scope reading `sensitivity` alone (the pre-1032 predicate, still used by the semantic-binding
    peer) would let this row through."""
    _bridge(db)
    db.execute(
        "UPDATE graph_node SET effective_restriction = 'restricted' "
        "WHERE catalog_source = 'core' AND object_ref = 'public.customer_master.customer_id'")
    assert list_bridge_proposals(db, roles=_PLAIN_ROLES) == []
    assert len(list_bridge_proposals(db, roles=(*_PLAIN_ROLES, "restricted_reader"))) == 1


def test_roles_none_is_the_unscoped_internal_path(db):
    """`roles=None` mirrors the semantic-binding peer: the UNSCOPED legacy path for internal callers
    and tests. The route surface always passes the caller's claims."""
    _bridge(db)
    _hide(db, "core", "customer_master", "customer_id", tag="pii")
    assert len(list_bridge_proposals(db, roles=None)) == 1


def test_an_empty_role_set_still_scopes(db):
    """`roles=()` is a REAL caller holding nothing — it must scope, not fall back to unscoped."""
    _bridge(db)
    _hide(db, "core", "customer_master", "customer_id", tag="pii")
    assert list_bridge_proposals(db, roles=()) == []


# ── source: either endpoint, because bridge identity is unordered ────────────────────────────────

def test_either_endpoint_matches_the_source_filter(db):
    """`EntityBridgeRef` identity is UNORDERED (`identity.py:35` — "fact_key canonicalizes the
    endpoints"), so opening the CRM side must find the same bridge the CORE side does."""
    _bridge(db)
    left = list_bridge_proposals(db, source="core")
    right = list_bridge_proposals(db, source="crm")
    assert len(left) == 1 and len(right) == 1
    assert left[0]["fact_key"] == right[0]["fact_key"]


def test_a_source_that_is_no_endpoint_matches_nothing(db):
    """The control — without this, `source` could be ignored entirely and the test above passes."""
    _bridge(db)
    assert list_bridge_proposals(db, source="cards") == []


def test_source_none_means_every_catalog(db):
    """The cross-catalog case the existing `/sources/{source}/…` routes cannot express."""
    _bridge(db)
    assert len(list_bridge_proposals(db, source=None)) == 1


def test_source_matching_is_case_and_whitespace_insensitive(db):
    _bridge(db)
    assert len(list_bridge_proposals(db, source=" CORE ")) == 1


# ── available_actions is server-decided ──────────────────────────────────────────────────────────

def test_a_draft_bridge_offers_confirm_and_reject(db):
    _bridge(db)
    actor = mint_test_identity(subject="user:admin1", role_claims=("platform-admin",))
    assert list_bridge_proposals(db, actor=actor)[0]["available_actions"] == ["confirm", "reject"]


def test_the_proposer_may_not_confirm_their_own_bridge(db):
    """Four-eyes (`authority.py:215`: `proposed_by != actor.subject`), projected into the read model
    so the UI never computes it. Since R1 a DERIVED bridge is proposed by a service actor, so a
    human normally CAN confirm — the exclusion still has to hold for anything human-proposed."""
    ensure_upload_catalog_adapter()
    _two_catalog_customer(db)
    human = mint_test_identity(subject="user:inder", role_claims=("platform-admin",))
    propose_bridge(db, derive_bridge_candidates(db)[0], actor=human, now=_NOW)
    assert list_bridge_proposals(db, actor=human)[0]["available_actions"] == ["reject"]


def test_a_different_human_may_confirm_a_service_proposed_bridge(db):
    """R1's whole point: the system proposes, a human disposes."""
    _bridge(db)
    someone = mint_test_identity(subject="user:reviewer", role_claims=("platform-admin",))
    assert "confirm" in list_bridge_proposals(db, actor=someone)[0]["available_actions"]


def test_a_verified_bridge_offers_only_reject(db):
    """The only sanctioned action left on a VERIFIED bridge in this design is reject, which demotes
    the edge (Task 2). It must not keep advertising `confirm`."""
    ref, _ = _bridge(db)
    _confirm(db, ref)
    actor = mint_test_identity(subject="user:someone", role_claims=("platform-admin",))
    assert list_bridge_proposals(db, actor=actor)[0]["available_actions"] == ["reject"]


# ── the confirmer's context: type_basis, and the rest of the evidence ────────────────────────────

def test_type_basis_reaches_the_confirmer(db):
    """On the live catalog the customer bridge is `declared` — the type match rests on two glossary
    spreadsheets, not on a read of the physical schema. That is a materially different decision."""
    _bridge(db)
    row = list_bridge_proposals(db)[0]
    assert row["type_basis"] == "attested"          # both sides structural in this fixture
    assert row["data_type_family"] == "integer"
    assert row["entity_id"] == "customer"
    assert sorted(row["catalogs"]) == ["core", "crm"]


def test_a_declared_type_basis_is_reported_as_declared(db):
    """The control — `type_basis` must be read, not hardcoded."""
    _bridge(db)
    db.execute(
        "UPDATE entity_bridge_candidate_evidence "
        "SET evidence_json = jsonb_set(evidence_json, '{type_basis}', '\"declared\"')")
    assert list_bridge_proposals(db)[0]["type_basis"] == "declared"


def test_proposed_by_and_target_event_id_are_carried(db):
    _bridge(db)
    row = list_bridge_proposals(db)[0]
    assert row["proposed_by"] == _ENRICH_ACTOR.subject
    assert row["target_event_id"] is not None
    assert row["proposed_at"] is not None


# ── failure isolation and limits ─────────────────────────────────────────────────────────────────

def _raw_bridge_event(db, key: str, payload: dict) -> None:
    """Append an overlay_fact PROPOSED event BYPASSING the command path, so a payload the schema
    would reject can be planted. This is how a corrupt legacy row is simulated. The actor and
    provenance envelopes are borrowed from a REAL event so the row deserializes — otherwise the
    poison would be the envelope rather than the payload under test."""
    actor, provenance = db.execute(
        "SELECT actor, provenance FROM events WHERE aggregate = 'overlay_fact' LIMIT 1").fetchone()
    db.execute(
        "INSERT INTO events (event_id, aggregate, aggregate_id, overlay_fact_id, stream_version, "
        " type, schema_version, table_version, actor, payload, provenance, occurred_at) "
        "VALUES (%s,'overlay_fact',%s,%s,1,'OVERLAY_FACT_PROPOSED',1,1,%s,%s,%s,%s)",
        (str(uuid.uuid4()), key, key, json.dumps(actor), json.dumps(payload),
         json.dumps(provenance), _NOW))


def test_one_malformed_bridge_does_not_abort_the_list(db):
    """`semantic_binding_governance.py:267`: bad data on one row is SKIPPED, never aborts the queue.
    A single poisoned row must not take down the whole governance surface."""
    _bridge(db)
    _raw_bridge_event(db, "corrupt-fact-key", {
        "catalog_object_ref": {"entity_id": "x", "left_ref": {"nope": 1}, "right_ref": {}},
        "object_ref": "x", "fact_type": "entity_bridge", "proposed_value": {},
        "proposal_fingerprint": "fp", "proposed_by": "svc:x"})
    rows = list_bridge_proposals(db)
    assert len(rows) == 1, "the good bridge must survive its poisoned neighbour"


def test_limit_clamps_to_the_ceiling(db):
    _bridge(db)
    assert len(list_bridge_proposals(db, limit=10_000)) == 1     # clamped, not an error


def test_limit_is_honoured(db):
    """The control — without this, `limit` could be ignored and the clamp test still passes."""
    _bridge(db)
    _load(db, "cards", [
        (CanonicalRow("cards", "customer_master", "customer_id", "integer", is_grain=True),
         "customer_id")])
    for cand in derive_bridge_candidates(db):
        propose_bridge(db, cand, actor=_ENRICH_ACTOR, now=_NOW)
    assert len(list_bridge_proposals(db)) >= 2
    assert len(list_bridge_proposals(db, limit=1)) == 1


def test_limit_zero_clamps_up_to_one(db):
    _bridge(db)
    assert len(list_bridge_proposals(db, limit=0)) == 1


# ── load_bridge_context — what the command layer needs ───────────────────────────────────────────

def test_load_bridge_context_returns_the_command_args(db):
    ref, key = _bridge(db)
    ctx = load_bridge_context(db, key, None)
    assert ctx is not None
    assert isinstance(ctx["ref"], EntityBridgeRef)
    assert asdict(ctx["ref"]) == asdict(ref)
    assert ctx["fact_type"] == "entity_bridge"
    assert ctx["use_case"] is None            # M1: blank on all nine live events
    assert ctx["target_event_id"] == _cas_target(fold_overlay_state(load_fact(db, key)))


def test_load_bridge_context_refuses_a_non_bridge_fact_key(db):
    """The generic-approval hole the semantic-binding peer closes by fact_type-VALIDATING: a
    fact_key naming some OTHER governed fact must not be confirmable through the bridge route."""
    _bridge(db)
    _raw_bridge_event(db, "some-other-fact", {
        "catalog_object_ref": {"catalog_source": "core", "object_kind": "column",
                               "schema": "public", "table": "customer_master",
                               "column": "segment"},
        "object_ref": "public.customer_master.segment", "fact_type": "entity_assignment",
        "proposed_value": {"entity_id": "customer"}, "proposal_fingerprint": "fp",
        "proposed_by": "svc:x"})
    assert load_bridge_context(db, "some-other-fact", None) is None


def test_load_bridge_context_returns_none_for_an_unknown_key(db):
    assert load_bridge_context(db, "no-such-fact", None) is None


def test_load_bridge_context_is_read_scoped(db):
    """The context loader feeds the COMMAND layer. If it answered for a hidden endpoint, the drop in
    the listing would be cosmetic — a caller could confirm what they may not see."""
    _ref, key = _bridge(db)
    _hide(db, "core", "customer_master", "customer_id", tag="pii")
    assert load_bridge_context(db, key, _PLAIN_ROLES) is None
    assert load_bridge_context(db, key, (*_PLAIN_ROLES, "pii_reader")) is not None
