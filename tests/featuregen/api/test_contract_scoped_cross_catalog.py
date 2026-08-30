"""C2a at the ROUTE — POST /contract/considered-set with a catalog_source reaches the governed lens.

The companion suite (``overlay/upload/contract/test_gate1_scoped_governed_lens``) proves what the
lens DOES. This one proves the two things only the HTTP surface can answer:

* **inactive is byte-identical.** With the cross-catalog verdict false the lens is never consulted
  and the served JSON is exactly the JSON this endpoint served before C2a existed — asserted by
  comparing a flag-off response against a flag-on one, field for field, over everything the engine
  produced. A wire-up that changed a flag-off response would be a change to every deployment.
* **active is ADDITIVE, against the REAL recipe registry.** No monkeypatched recipe set, no
  injected request: the run plans the registry's own eligible primaries. What that produces today
  is governed REFUSALS rather than governed options — the cross-catalog frontier (G3) is open, and
  ``governed_lens``'s header says so — and that is exactly what makes this a real proof of the
  wire-up: those refusals exist only because a catalog-scoped request now reaches a lens it could
  not reach at all before, and they are absent from the byte-identical flag-off response beside it.
"""
from __future__ import annotations

from tests.featuregen.api._helpers import AUTH
from tests.featuregen.api.test_contract_live_cross_catalog import (
    DEP,
    FLAG,
    _approve,
    _catalog_scoped_body,
    _flow_llm,
)
from tests.featuregen.api.test_contract_scoped import _bank_multi

import featuregen.overlay.upload.contract.gate1 as gate1
from featuregen.overlay.upload.contract.gate1 import GOVERNED_CROSS_CATALOG_LENS


def _post(client, body=None):
    return client.post("/contract/considered-set",
                       json=body or _catalog_scoped_body(), headers=AUTH)


def _without_ids(value):
    """The response with every per-run identity removed, so two runs of the SAME request can be
    compared for the only thing at issue here: whether the wire-up changed what is served.

    Option ids are minted over the run's own generation identity, so they differ between any two
    runs by construction and say nothing about content."""
    if isinstance(value, dict):
        return {key: _without_ids(inner) for key, inner in value.items() if key != "option_id"}
    if isinstance(value, list):
        return [_without_ids(item) for item in value]
    return value


def _engine_alternatives(payload):
    return _without_ids([feature_set for feature_set in payload["alternatives"]
                         if feature_set["lens"] != GOVERNED_CROSS_CATALOG_LENS])


def _governed_rejections(payload):
    return [rejection for rejection in payload["rejections"]
            if rejection.get("lens") == "governed"]


# ── inactive: the lens is never consulted and nothing about the response moves ─────────────────
def test_an_inactive_deployment_never_consults_the_governed_lens(make_client, conn, monkeypatch):
    """Not "serves no governed options" — never CALLS the lens. The planner it drives costs real
    time inside a user's request, and a deployment that has not turned cross-catalog on must not
    pay it, log it, or write a row for it."""
    monkeypatch.delenv(FLAG, raising=False)
    _bank_multi(conn)

    def _boom(*args, **kwargs):
        raise AssertionError("the governed lens must not run on an inactive deployment")

    monkeypatch.setattr(gate1, "_scoped_governed_cross_catalog_lens", _boom)
    res = _post(make_client(_flow_llm()))
    assert res.status_code == 200, res.text
    payload = res.json()
    assert all(feature_set["lens"] != GOVERNED_CROSS_CATALOG_LENS
               for feature_set in payload["alternatives"])
    assert _governed_rejections(payload) == []


# ── active: the request reaches the lens, and everything the engine served is unchanged ────────
def test_a_catalog_scoped_request_reaches_the_governed_lens_and_stays_additive(
        make_client, conn, monkeypatch):
    """THE wire-up, end to end over HTTP and over the real registry.

    The same request is served twice — once inactive, once flag-on-and-activation-approved. The
    engine's half of the payload must be identical field for field; the governed lens's work must
    appear ONLY in the active run. Before C2a the second run was indistinguishable from the first:
    the governed lens's one door required ``catalog_source is None``, which this route refuses at
    the boundary with 422, so no catalog-scoped request could reach it however the deployment was
    configured."""
    _bank_multi(conn)
    client = make_client(_flow_llm())

    monkeypatch.delenv(FLAG, raising=False)
    inactive = _post(client)
    assert inactive.status_code == 200, inactive.text

    monkeypatch.setenv(FLAG, "1")
    monkeypatch.setenv(DEP, "d1")
    _approve(conn)
    active = _post(client)
    assert active.status_code == 200, active.text

    # 1. the governed lens RAN, and it ran over the shipped registry
    governed = _governed_rejections(active.json())
    assert governed, "a catalog-scoped active request must reach the governed cross-catalog lens"
    assert _governed_rejections(inactive.json()) == []
    # a refusal served to a caller carries THREE keys and no more — the planner's evidence (bridge
    # fact keys, physical object refs) is server-private and must never widen this list
    assert all(set(rejection) == {"lens", "reason", "recipe_id"} for rejection in governed)

    # 2. ADDITIVE: everything the engine produced is byte-for-byte what the inactive run served
    assert _engine_alternatives(active.json()) == _engine_alternatives(inactive.json())
    assert _without_ids(active.json()["anchor"]) == _without_ids(inactive.json()["anchor"])
    # …and the governed refusals are strictly ADDED, never a replacement for what was there
    inactive_rejections = _without_ids(inactive.json()["rejections"])
    active_rejections = _without_ids(active.json()["rejections"])
    assert all(rejection in active_rejections for rejection in inactive_rejections)
    assert len(active_rejections) > len(inactive_rejections)


# ── the entity-only door is still shut, and for the reason it always was ──────────────────────
def test_the_entity_only_refusal_is_unchanged_by_the_new_door(make_client, conn, monkeypatch):
    """C2a opened the CATALOG-SCOPED door; it did not open the entity-only one. A request naming no
    catalog is still refused with the same typed 422, active or not — the semantic engine still
    plans over one frozen catalog context, and that is what the refusal is about."""
    from tests.featuregen.api.test_contract_live_cross_catalog import (
        SEMANTIC_REQUIRES_CATALOG_SOURCE,
        _entity_scoped_body,
    )

    _bank_multi(conn)
    monkeypatch.setenv(FLAG, "1")
    monkeypatch.setenv(DEP, "d1")
    _approve(conn)
    res = _post(make_client(_flow_llm()), _entity_scoped_body())
    assert res.status_code == 422, res.text
    assert res.json()["detail"]["code"] == SEMANTIC_REQUIRES_CATALOG_SOURCE
