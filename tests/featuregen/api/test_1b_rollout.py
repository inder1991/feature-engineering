"""Phase-1B Task 9 — feature-flag neutrality + emergency-rollback proof.

``FEATUREGEN_INTENT_SCOPED_APPLICABILITY`` (default OFF) was the single emergency-rollback point for
Phase-1B scoped grounding: OFF → ``build_considered_set`` grounded ``ALL_TEMPLATES`` even when a
confirmed scope/applicability was supplied. The other two Phase-1B flags (``intent_confirmation_ui`` /
``intent_disposition_lens``) are FRONTEND concerns (Task 8) and have no backend read.

The E4 cutover (2026-08-14) moved the ground under scenarios 2 and 3. A request carrying a
``confirmed_scope`` is now served by the SEMANTIC ENGINE (lenses ``engine`` / ``actionable``), whose
eligibility is decided by the confirmed scope itself inside the engine — the legacy template-grounding
pass that ``_templates_to_ground`` narrowed does not run on that path at all. So this flag can no
longer widen or narrow what a scoped call serves: it survives as a recorded rollout dimension on the
generation-run manifest and as the emergency ``legacy_unscoped`` lever, nothing more. The tests below
say exactly that rather than asserting a difference the code can no longer produce. (The flag's real
remaining effect — narrowing the legacy template universe when the builder is called directly with an
``ApplicabilityResult`` — is covered at the builder level in
``tests/featuregen/overlay/upload/contract/test_gate1_scoped.py``.)

Three scenarios:

1. **All-off neutrality** — flag unset (default off) + a no-scope call → the response is byte-identical
   to a pre-1B considered set (exact key set, no dispositions/run/scope fields) and NO recognition-attempt
   / confirmed-scope row is written.
2. **Emergency rollback** — flag OFF + a *scoped* call (as if the UI is still sending confirmed scopes):
   the engine still serves (rollback no longer reaches grounding), the scope row is STILL persisted, and
   — separately — a ``/contract/recognitions`` call still writes its recognition-attempt row (recognition
   telemetry retained during rollback).
3. **Flag neutrality on the scoped path** — the SAME scoped call flag-ON serves the IDENTICAL candidate
   set, while the run manifest records which way the flag was set.

The catalog is a TWO-family (churn + credit) upload so the unscoped legacy lens and the scoped engine
lens are both non-trivially populated, mirroring ``tests/featuregen/api/test_contract_scoped.py``.
"""
from datetime import UTC, datetime

from tests.featuregen.api._helpers import AUTH

from featuregen.intake.llm import FakeLLM, FakeResponse
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.contract.gate1 import _intent_scoped_applicability_enabled
from featuregen.overlay.upload.contract.scope_records import scope_for_run
from featuregen.overlay.upload.enrich import content_hash
from featuregen.overlay.upload.graph import build_graph
from featuregen.overlay.upload.taxonomy.applicability import ConfirmedScope
from featuregen.overlay.upload.taxonomy.recognizer import RECOGNIZER_TASK

FLAG = "FEATUREGEN_INTENT_SCOPED_APPLICABILITY"
CHURN = "customer.relationship_attrition.churn"
HYPOTHESIS = "customers churn when their balance drops"
TARGET = "public.accounts.churned"
PRE_1B_KEYS = {"intent_id", "anchor", "alternatives", "recommendation", "rejections"}

# A classified recognizer response — the telemetry the recognition endpoint persists (mirrors
# test_contract_recognitions.py). Used only to prove the recognition-attempt row still writes on rollback.
_CLASSIFIED = FakeResponse(output={
    "status": "classified",
    "candidates": [{
        "use_case_id": CHURN, "relationship": "primary", "confidence": "high",
        "evidence_spans": ["churn"], "rationale": "the hypothesis is about customers leaving"}],
    "ambiguity_note": None})


def _fake() -> FakeLLM:
    """The generation tasks ``build_considered_set`` drives (no recognizer entry — recognition is a
    separate API step). Only the ADVISORY set-recommendation pass is scripted: the free-form
    ``overlay.feature.recommend`` generator was deleted in the E4 cutover (2026-08-14), and the
    intents task is left unscripted on purpose — the intent lens fails soft, so the recipe half of
    the engine serves alone and both scenarios below stay deterministic."""
    return FakeLLM(script={
        "overlay.feature.recommend_set": FakeResponse(output={
            "recommended_lens": "monetary", "reasoning": "monetary fits the balance-drop hypothesis"}),
    })


def _recognizer() -> FakeLLM:
    """A recognizer-scripted client for the /contract/recognitions telemetry call (one LLM call)."""
    return FakeLLM(script={RECOGNIZER_TASK: _CLASSIFIED})


def _bank_multi(conn) -> None:
    """A TWO-family catalog: an ``accounts`` table the retail_churn recipes ground on, PLUS a
    ``facilities`` table (a credit-limit grain) the credit recipes ground on. A full (unscoped) grounding
    surfaces BOTH families; a churn-scoped grounding surfaces only the churn recipes — the direct 'fewer
    template candidates' signal. Mirrors test_contract_scoped's catalog."""
    # Fresh as of the test run — the route grounds against the real wall clock, so a hardcoded past
    # date rots the freshness gate once that date passes.
    now = datetime.now(UTC)
    catalog = [
        # ── accounts → the retail_churn recipes ──
        (CanonicalRow("bank", "accounts", "customer_id", "integer", is_grain=True, entity="Customer"),
         "customer_id"),
        (CanonicalRow("bank", "accounts", "balance", "numeric", additivity="semi_additive",
                      currency="USD"), "monetary_stock"),
        (CanonicalRow("bank", "accounts", "as_of_date", "timestamp", as_of=True), "as_of_date"),
        (CanonicalRow("bank", "accounts", "amount", "numeric", additivity="additive", currency="USD"),
         "monetary_flow"),
        (CanonicalRow("bank", "accounts", "event_ts", "timestamp"), "event_timestamp"),
        (CanonicalRow("bank", "accounts", "churned", "boolean"), "outcome_label"),
        # ── facilities → the credit-utilisation (limit) recipes: a NON-churn family, out of scope for a
        #    churn narrowing but grounded under a full/unscoped run ──
        (CanonicalRow("bank", "facilities", "facility_id", "integer", is_grain=True, entity="Facility"),
         "facility_id"),
        (CanonicalRow("bank", "facilities", "drawn", "numeric", additivity="semi_additive",
                      currency="USD"), "monetary_stock"),
        (CanonicalRow("bank", "facilities", "credit_limit", "numeric", currency="USD"), "limit"),
        (CanonicalRow("bank", "facilities", "asof2", "timestamp", as_of=True), "as_of_date"),
    ]
    rows = [r for r, _ in catalog]
    concepts = {content_hash(r): c for r, c in catalog}
    build_graph(conn, "bank", rows, concepts=concepts)
    conn.execute(
        "INSERT INTO overlay_drift_watermark (catalog_source, last_completed_at, last_run_id, head_seq) "
        "VALUES ('bank', %s, 'r', 0) ON CONFLICT (catalog_source) DO UPDATE SET last_completed_at = %s",
        (now, now))


def _served(body: dict) -> dict[str, list[str]]:
    """Every served lens and the names in it. Post-cutover a scoped call answers from the engine
    (``engine`` / ``actionable``) and the legacy unscoped call from ``templates``, so the lens NAMES
    are themselves the evidence of which pipeline answered."""
    return {s["lens"]: sorted(f["name"] for f in s["features"]) for s in body["alternatives"]}


def _run_flags(conn, run_id: str) -> dict:
    """The rollout dimensions stamped on the durable generation-run manifest."""
    return conn.execute(
        "SELECT flags FROM feature_generation_run WHERE generation_run_id = %s",
        (run_id,)).fetchone()[0]


def _post(client, **extra) -> dict:
    payload = {"hypothesis": HYPOTHESIS, "objective": "predict churn",
               "catalog_source": "bank", "target_ref": TARGET, **extra}
    res = client.post("/contract/considered-set", json=payload, headers=AUTH)
    assert res.status_code == 200, res.text
    return res.json()


def _scoped_body() -> dict:
    """The exact scoped payload used flag-off AND flag-on, so the flag is the ONLY thing that varies."""
    return {"primary": CHURN, "confirmation_source": "user_confirmed"}


# ── Scenario 1: all-off neutrality — a no-scope call is byte-identical to pre-1B ──────────────────────
def test_all_off_no_scope_is_byte_identical_to_pre_1b(make_client, conn, monkeypatch):
    monkeypatch.delenv(FLAG, raising=False)   # the flag is UNSET → default off
    assert _intent_scoped_applicability_enabled() is False   # the single backend flag reads OFF
    _bank_multi(conn)

    body = _post(make_client(_fake()))   # the pre-1B body: hypothesis + objective, NO confirmed_scope

    # Exactly the pre-1B key set — no dispositions / generation_run_id / scope_id / in_scope_count.
    assert set(body) == PRE_1B_KEYS
    # And NO Phase-1B side-effect rows: the no-scope path writes neither a recognition attempt nor a scope.
    assert conn.execute("SELECT count(*) FROM intent_recognition_attempt").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM confirmed_generation_scope").fetchone()[0] == 0


# ── Scenario 2: emergency rollback — flag OFF, scope still sent → the engine still serves; scope +
#    telemetry kept ─────────────────────────────────────────────────────────────────────────────────────
def test_emergency_rollback_retains_scope_and_recognition_while_the_engine_still_serves(
        make_client, conn, monkeypatch):
    """After the E4 cutover this flag is no longer a grounding rollback. A scoped call is answered by
    the ONE engine whether the flag is set or not — there is no legacy template lens left on that path
    to fall back to — so the honest rollback guarantees are the two DURABLE ones: the confirmed scope is
    still captured, and recognition telemetry is still written."""
    monkeypatch.delenv(FLAG, raising=False)   # EMERGENCY ROLLBACK: the historical lever, default off
    assert _intent_scoped_applicability_enabled() is False
    _bank_multi(conn)

    # The UI is still sending confirmed scopes.
    scoped = _post(make_client(_fake()), confirmed_scope=_scoped_body())
    # A no-scope call on the SAME catalog is the legacy emergency path, for contrast.
    unscoped = _post(make_client(_fake()))

    # ROLLBACK TRUTH #1 — the flag does NOT reach grounding any more: the scoped run is served by the
    # semantic engine (its lens names say so) while only the legacy unscoped route still grounds
    # templates. Rolling the flag back cannot resurrect the old scoped-grounding pass.
    assert set(_served(scoped)) <= {"engine", "actionable"}
    assert any(_served(scoped).values()), "the engine still serves under rollback"
    assert set(_served(unscoped)) == {"templates"}   # the legacy emergency path, unchanged

    # ROLLBACK PROOF #2 — the scope row is STILL persisted (rollback disables grounding, not scope capture).
    run = scoped["generation_run_id"]
    parent = conn.execute(
        "SELECT scope_id, scope_mode FROM confirmed_generation_scope WHERE generation_run_id = %s",
        (run,)).fetchone()
    assert parent is not None and parent[1] == "scoped"
    assert (CHURN, "primary") in conn.execute(
        "SELECT use_case_id, relationship FROM confirmed_scope_use_case WHERE scope_id = %s",
        (parent[0],)).fetchall()
    assert scope_for_run(conn, run) == ConfirmedScope(primary=CHURN)   # governing scope by run id

    # ROLLBACK PROOF #3 — recognition telemetry is RETAINED during rollback: the generate path itself
    # wrote no attempt, yet a /contract/recognitions call still persists its append-only attempt row.
    assert conn.execute("SELECT count(*) FROM intent_recognition_attempt").fetchone()[0] == 0
    rec = make_client(_recognizer()).post(
        "/contract/recognitions", json={"hypothesis": HYPOTHESIS, "objective": "predict churn"},
        headers=AUTH)
    assert rec.status_code == 200, rec.text
    intent_id = rec.json()["intent_id"]
    n = conn.execute("SELECT count(*) FROM intent_recognition_attempt WHERE intent_id = %s",
                     (intent_id,)).fetchone()[0]
    assert n == 1


# ── Scenario 3: flag neutrality on the scoped path — the SAME scoped call serves the SAME set ─────────
def test_flag_on_serves_the_identical_scoped_set_and_is_recorded_on_the_run(
        make_client, conn, monkeypatch):
    """The flag's post-cutover truth: flipping it changes NOTHING a scoped caller sees, because the
    engine — not ``_templates_to_ground`` — decides that request's candidate universe from the confirmed
    scope. What it still does is get RECORDED on the generation-run manifest, so a run stays auditable
    against the rollout dimensions it executed under."""
    _bank_multi(conn)

    monkeypatch.delenv(FLAG, raising=False)
    off = _post(make_client(_fake()), confirmed_scope=_scoped_body())
    monkeypatch.setenv(FLAG, "1")   # the ONLY change vs the call above
    assert _intent_scoped_applicability_enabled() is True
    on = _post(make_client(_fake()), confirmed_scope=_scoped_body())   # identical scoped payload

    assert _served(on) == _served(off), "the flag no longer selects the served candidate set"
    assert _run_flags(conn, off["generation_run_id"])["scoped_applicability"] is False
    assert _run_flags(conn, on["generation_run_id"])["scoped_applicability"] is True
