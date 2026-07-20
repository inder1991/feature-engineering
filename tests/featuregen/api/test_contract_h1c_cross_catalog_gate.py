"""Task H1c — the cross-catalog governance gate in the new H1 confirm flow.

A candidate whose SELECTED inputs span more than one catalog_source is REFUSED at the governing write
with the umbrella reason ``CROSS_CATALOG_GROUNDING_NOT_ENABLED`` unless 3C.2 is GENUINELY enabled — ALL
of: a governed ``plan_envelope`` (the selected physical plan), the durable live-activation interlock
(flag + persisted PASS enablement + APPROVE + version vector, via ``live_activation``), AND a valid
signed 3C gate artifact (ed25519 detached signature, via ``planner.signing``). Any doubt → fail closed.
The governed confirm path NEVER rides the permissive ``find_cross_catalog_path`` (both live bindings are
replaced with a recorder that raises; an empty recorder list at the end IS the structural proof).

These reuse the EXISTING interlock + verifier + author guards — this task is wiring + a rejection reason,
not a rebuild. The confirm-route tests follow the ``chosen_feature`` monkeypatch pattern already used by
``test_confirm_route_rechecks_freshness_and_maps_stale_to_409`` (real DB, real /contract/confirm route,
real H1c gate): a genuine single-catalog draft records the Gate-1 choice, then the SERVER-reconstructed
chosen feature is overridden to a MULTI-catalog candidate so the span gate fires.
"""
from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

from tests.featuregen.api._helpers import AUTH, DEPOSITS_CSV, upload_csv
from tests.featuregen.api.test_contract import _fake, _intent_id

from featuregen.overlay.upload.contract.govern import Contract
from featuregen.overlay.upload.contract.live_activation import (
    CROSS_CATALOG_GROUNDING_NOT_ENABLED,
    cross_catalog_grounding_enabled,
    is_live_cross_catalog_enabled,
    record_decision,
    record_evaluation,
    signed_gate_artifact_valid,
)
from featuregen.overlay.upload.feature_assist import FeatureIdea
from featuregen.overlay.upload.planner import signing
from featuregen.overlay.upload.planner.contracts import ReplayFreshness
from featuregen.overlay.upload.planner.plan_envelope import PlanEnvelopeV1

FLAG = "FEATUREGEN_INTENT_LIVE_CROSS_CATALOG"
DEP = "FEATUREGEN_DEPLOYMENT_ID"
KEY = "FEATUREGEN_INTENT_GATE_PUBLIC_KEY"
ART = "FEATUREGEN_INTENT_GATE_ARTIFACT"
_NOW = datetime(2026, 7, 18, tzinfo=UTC)

# The candidate's SELECTED inputs span TWO catalogs — the whole point of the cross-catalog gate.
MULTI = (("deposits", "public.accounts.balance"), ("cards", "public.card_accounts.spend"))

# A signing authority's keypair. The PRIVATE half signs the artifact (as CI/ops would); only the PUBLIC
# half is a verifier config input (FEATUREGEN_INTENT_GATE_PUBLIC_KEY). Generated once for the module.
_PRIV_PEM, _PUBLIC_PEM = signing.generate_keypair()


# ── shared helpers ───────────────────────────────────────────────────────────────────────────────────
def _approve(conn) -> None:
    """A PASS evaluation + an APPROVE decision for the CURRENT deployment — the REAL activation interlock
    (nothing stubbed). Caller must set FEATUREGEN_DEPLOYMENT_ID first (record_decision reads it)."""
    eid = record_evaluation(conn, telemetry_window={}, population_report={}, gold_set_result={},
                            stability_result={}, result="PASS", evaluated_at=_NOW)
    record_decision(conn, evaluation_id=eid, decision="APPROVE", decided_by="admin", reason="go",
                    decided_at=_NOW)


def _permissive_recorder(monkeypatch) -> list:
    """Replace BOTH live ``find_cross_catalog_path`` bindings with a recorder that raises. An empty list
    at the end of a test IS the structural guarantee that the governed confirm path never rode it."""
    calls: list = []

    def _boom(*a, **k):
        calls.append(a)
        raise AssertionError("find_cross_catalog_path must never run in the governed confirm path")

    monkeypatch.setattr("featuregen.overlay.upload.entity.find_cross_catalog_path", _boom)
    monkeypatch.setattr("featuregen.overlay.upload.contract.author.find_cross_catalog_path", _boom)
    return calls


def _multi_env() -> PlanEnvelopeV1:
    """A governed cross-catalog plan envelope spanning deposits + cards (mirrors test_contract's
    ``_fresh_envelope`` shape). Its ``ordered_path`` is the governed bridge the confirm must author from."""
    return PlanEnvelopeV1(
        recipe_id="r", physical_plan_id="bp_multi", generation_run_id="run",
        catalog_sources=("deposits", "cards"),
        ordered_path=("deposits:direct_catalog:", "cards:entity_bridge:Customer"),
        contract_id="c1", contract_resolution_status="resolved", contract_reason_codes=(),
        catalog_fingerprint={"deposits": "fp", "cards": "fp2"},
        compiler_version={"plan_contract": "1.0.0"},
        input_stamps=({"catalog_source": "deposits", "compiler_input_fingerprint": "fp",
                       "head_seq": 1, "projection_checkpoint": 1},
                      {"catalog_source": "cards", "compiler_input_fingerprint": "fp2",
                       "head_seq": 1, "projection_checkpoint": 1}))


def _configure_valid_artifact(monkeypatch, tmp_path) -> None:
    """Write a signed 3C gate artifact + its detached ``.sig`` sidecar and point config at it. The
    verifier is signature-over-bytes (content-agnostic; signing.py has its own artifact-shape tests), so
    canonical stand-in bytes suffice to prove the PRONG gates enablement."""
    report_bytes = b'{"gate_passed":true,"producer_cohort":"deploy-sha"}'
    p = tmp_path / "gate_artifact.json"
    p.write_bytes(report_bytes)
    signing.write_signature_sidecar(p, signing.sign_report(report_bytes, _PRIV_PEM))
    monkeypatch.setenv(KEY, _PUBLIC_PEM)
    monkeypatch.setenv(ART, str(p))


def _prepare_confirm_body(client) -> tuple[str, dict]:
    """A genuine single-catalog draft (records the Gate-1 choice), returned as a confirm body whose
    ``derives_pairs`` is REWRITTEN to the multi-catalog set — the confirm match-check compares the body's
    pairs to the SERVER-reconstructed chosen feature's, so both must be the multi-catalog set."""
    upload_csv(client, "deposits", DEPOSITS_CSV)
    intent_id = _intent_id(client)
    dr = client.post("/contract/draft", json={
        "intent_id": intent_id, "chosen_source": "anchor",
        "chosen_option_id": "avg_balance_90d", "why": ""}, headers=AUTH)
    assert dr.status_code == 200, dr.text
    draft = dr.json()["draft"]
    draft["intent_id"] = intent_id
    draft["derives_pairs"] = [list(p) for p in MULTI]
    return intent_id, draft


def _install_multi_chosen(monkeypatch, draft, *, envelope) -> None:
    """Override the SERVER-reconstructed chosen feature with a MULTI-catalog candidate matching the
    confirm body's name/derives_pairs/aggregation (so the match-check passes and the H1c span gate fires).
    ``envelope`` present ⟹ a governed candidate; None ⟹ an ungoverned cross-catalog candidate."""
    def _chosen(*a, **k):
        return FeatureIdea(
            draft["feature_name"], "", list(draft["derives_from"]), draft["aggregation"], None,
            derives_pairs=MULTI, plan_envelope=envelope,
            origin="governed_planner" if envelope is not None else "llm",
            path_authority="governed_cross_catalog" if envelope is not None else "single_or_llm")

    monkeypatch.setattr("featuregen.api.routes.contract.chosen_feature", _chosen)


# ═══════════════ 1. multi-catalog refused while 3C.2 disabled (flag off, no envelope) ═══════════════
def test_multi_catalog_no_envelope_flag_off_refused_not_enabled(make_client, conn, monkeypatch):
    """The hole this task closes: a multi-catalog candidate with NO governed plan envelope, live flag
    OFF, previously fell THROUGH the confirm to ``confirm_contract`` on the client-supplied permissive
    join_path. Now it is refused ``CROSS_CATALOG_GROUNDING_NOT_ENABLED``; no contract is authored; the
    permissive ``find_cross_catalog_path`` is provably never invoked."""
    monkeypatch.delenv(FLAG, raising=False)
    client = make_client(_fake())
    _, draft = _prepare_confirm_body(client)
    calls = _permissive_recorder(monkeypatch)
    _install_multi_chosen(monkeypatch, draft, envelope=None)
    before = conn.execute("SELECT count(*) FROM contract").fetchone()[0]

    cr = client.post("/contract/confirm", json=draft, headers=AUTH)
    assert cr.status_code == 422, cr.text
    assert CROSS_CATALOG_GROUNDING_NOT_ENABLED in cr.json()["detail"]
    assert "governed plan envelope" in cr.json()["detail"]   # names the missing prerequisite
    assert conn.execute("SELECT count(*) FROM contract").fetchone()[0] == before   # nothing authored
    assert calls == []


# ═══════════════ 2. no governed plan ⟹ never governed, even flag-on-approved ═══════════════
def test_multi_catalog_no_envelope_flag_on_approved_still_refused(make_client, conn, monkeypatch):
    """A cross-catalog candidate with NO governed plan envelope cannot be governed even when the
    deployment IS live-activation-approved — there is no governed physical plan to author from, so it is
    refused unconditionally (the strongest fail-closed), never a permissive governing write."""
    monkeypatch.setenv(FLAG, "1")
    monkeypatch.setenv(DEP, "d1")
    _approve(conn)
    client = make_client(_fake())
    _, draft = _prepare_confirm_body(client)
    calls = _permissive_recorder(monkeypatch)
    _install_multi_chosen(monkeypatch, draft, envelope=None)

    cr = client.post("/contract/confirm", json=draft, headers=AUTH)
    assert cr.status_code == 422, cr.text
    assert CROSS_CATALOG_GROUNDING_NOT_ENABLED in cr.json()["detail"]
    assert calls == []


# ═══════════════ 3. envelope present but enablement lapsed (flag off) ⟹ fail closed ═══════════════
def test_multi_catalog_with_envelope_but_not_live_refused(make_client, conn, monkeypatch):
    """A governed cross-catalog candidate (envelope present, plan FRESH) is STILL refused when live
    cross-catalog grounding is not enabled at the governing write — the interlock could have been
    revoked / the flag turned off between draft and confirm. Freshness alone does not admit it."""
    monkeypatch.delenv(FLAG, raising=False)   # activation not live
    monkeypatch.setattr("featuregen.api.routes.contract.recheck_plan_freshness",
                        lambda *a, **k: ReplayFreshness.current)
    client = make_client(_fake())
    _, draft = _prepare_confirm_body(client)
    calls = _permissive_recorder(monkeypatch)
    _install_multi_chosen(monkeypatch, draft, envelope=_multi_env())

    cr = client.post("/contract/confirm", json=draft, headers=AUTH)
    assert cr.status_code == 422, cr.text
    assert CROSS_CATALOG_GROUNDING_NOT_ENABLED in cr.json()["detail"]
    assert "not enabled" in cr.json()["detail"]
    assert calls == []


# ═══════════════ 4. the FULL interlock admits — authored from the governed envelope path ═══════════════
def test_full_interlock_admits_and_authors_from_envelope_path(make_client, conn, monkeypatch, tmp_path):
    """Envelope present + fresh + the live-activation interlock holds + a VALID signed 3C gate artifact:
    the candidate is ADMITTED and the governing write receives the join path RE-DERIVED from the
    envelope's ``ordered_path`` (governed) — never a permissive path, which stays provably un-invoked."""
    _configure_valid_artifact(monkeypatch, tmp_path)
    monkeypatch.setenv(FLAG, "1")
    monkeypatch.setenv(DEP, "d1")
    _approve(conn)
    assert signed_gate_artifact_valid() is True
    assert cross_catalog_grounding_enabled(conn) is True
    env = _multi_env()
    monkeypatch.setattr("featuregen.api.routes.contract.recheck_plan_freshness",
                        lambda *a, **k: ReplayFreshness.current)
    monkeypatch.setattr("featuregen.api.routes.contract.confirmed_role_bindings", lambda *a, **k: [])
    seen: dict = {}

    def _spy_confirm(_conn, draft, **k):
        seen["join_path"] = draft.join_path
        return Contract(contract_id="c1", feature_id="f1", feature_name=draft.feature_name, version=1)

    monkeypatch.setattr("featuregen.api.routes.contract.confirm_contract", _spy_confirm)
    client = make_client(_fake())
    _, draft = _prepare_confirm_body(client)
    calls = _permissive_recorder(monkeypatch)
    _install_multi_chosen(monkeypatch, draft, envelope=env)

    cr = client.post("/contract/confirm", json=draft, headers=AUTH)
    assert cr.status_code == 200, cr.text
    assert tuple(s["segment"] for s in seen["join_path"]) == env.ordered_path   # governed envelope path
    assert calls == []


# ═══════════════ 5. signed artifact absent (but configured) ⟹ fail closed though activation holds ═══════
def test_signed_artifact_absent_fails_closed_even_when_activation_holds(make_client, conn, monkeypatch):
    """The signed-artifact prong is INDEPENDENT of the activation interlock: with a trusted public key
    CONFIGURED (the gate is deployed) but NO artifact to verify, the multi-catalog governed candidate is
    refused even though the live-activation interlock itself holds — fail closed on the third prong."""
    monkeypatch.setenv(KEY, _PUBLIC_PEM)        # the signed gate is DEPLOYED (trusted key configured)
    monkeypatch.delenv(ART, raising=False)      # …but there is no artifact to verify → fail closed
    monkeypatch.setenv(FLAG, "1")
    monkeypatch.setenv(DEP, "d1")
    _approve(conn)
    assert is_live_cross_catalog_enabled(conn) is True     # activation interlock DOES hold …
    assert signed_gate_artifact_valid() is False           # … but the signed-artifact prong does not
    assert cross_catalog_grounding_enabled(conn) is False
    monkeypatch.setattr("featuregen.api.routes.contract.recheck_plan_freshness",
                        lambda *a, **k: ReplayFreshness.current)
    client = make_client(_fake())
    _, draft = _prepare_confirm_body(client)
    calls = _permissive_recorder(monkeypatch)
    _install_multi_chosen(monkeypatch, draft, envelope=_multi_env())

    cr = client.post("/contract/confirm", json=draft, headers=AUTH)
    assert cr.status_code == 422, cr.text
    assert CROSS_CATALOG_GROUNDING_NOT_ENABLED in cr.json()["detail"]
    assert calls == []


# ═══════════════ 6. single-catalog confirm is unaffected (no spurious rejection) ═══════════════
def test_single_catalog_confirm_unaffected(make_client, conn, monkeypatch):
    """A single-catalog candidate confirms normally — the span gate does not fire (its inputs span ONE
    catalog), so no ``CROSS_CATALOG_GROUNDING_NOT_ENABLED`` and no enablement query is consulted."""
    monkeypatch.delenv(FLAG, raising=False)
    client = make_client(_fake())
    upload_csv(client, "deposits", DEPOSITS_CSV)
    intent_id = _intent_id(client)
    dr = client.post("/contract/draft", json={
        "intent_id": intent_id, "chosen_source": "anchor",
        "chosen_option_id": "avg_balance_90d", "why": ""}, headers=AUTH)
    assert dr.status_code == 200, dr.text
    draft = dr.json()["draft"]
    draft["intent_id"] = intent_id

    cr = client.post("/contract/confirm", json=draft, headers=AUTH)
    assert cr.status_code == 200, cr.text   # no spurious H1c rejection on a single-catalog feature
    assert cr.json()["version"] == 1


# ═══════════════ signed-artifact prong — unit coverage (no DB) ═══════════════
def test_signed_prong_inert_when_no_public_key(monkeypatch):
    """No trusted key configured ⟹ the deployment has not opted into signed-gate enforcement, so the
    prong is INERT (the activation interlock alone governs) — keeps the flag-off path byte-identical."""
    monkeypatch.delenv(KEY, raising=False)
    monkeypatch.delenv(ART, raising=False)
    assert signed_gate_artifact_valid() is True


def test_signed_prong_fail_closed_key_but_no_artifact(monkeypatch):
    monkeypatch.setenv(KEY, _PUBLIC_PEM)
    monkeypatch.delenv(ART, raising=False)
    assert signed_gate_artifact_valid() is False


def test_signed_prong_true_for_valid_signed_artifact(monkeypatch, tmp_path):
    _configure_valid_artifact(monkeypatch, tmp_path)
    assert signed_gate_artifact_valid() is True


def test_signed_prong_fail_closed_for_tampered_artifact(monkeypatch, tmp_path):
    _configure_valid_artifact(monkeypatch, tmp_path)
    p = Path(os.environ[ART])
    p.write_bytes(p.read_bytes() + b"tampered")   # payload no longer matches the sidecar signature
    assert signed_gate_artifact_valid() is False


def test_signed_prong_fail_closed_for_wrong_trusted_key(monkeypatch, tmp_path):
    _configure_valid_artifact(monkeypatch, tmp_path)
    _, other_pub = signing.generate_keypair()
    monkeypatch.setenv(KEY, other_pub)            # a DIFFERENT trusted key than the signer's
    assert signed_gate_artifact_valid() is False
