"""S1B-3 — the telemetry PRODUCER half: the request path persists a work item and plans nothing.

The whole design rests on one claim, and this suite is where it is proved rather than asserted: a
run with telemetry ON must serve the human EXACTLY what a run with telemetry OFF serves, and must
pay a CONSTANT, countable price to do it. A telemetry feature that can change an answer is a
telemetry feature nobody may turn on in production; one whose cost grows with the eligible recipe
set is one that gets turned off again the first busy afternoon.

So the two pins here are:

* **byte identity** — the served payload, serialized, is identical on both sides of the flag;
* **a query delta of exactly two** — one scope-material read and one outbox INSERT. No planning, no
  provider call, no per-recipe query.

Both are driven from the SAME starting state: each build runs inside a savepoint that is rolled
back, so the second build cannot be reading rows the first one wrote.
"""
from __future__ import annotations

import inspect
import json
import re
from datetime import UTC, datetime

import psycopg
from tests.featuregen.overlay.upload.contract.test_gate1 import EXTRACTED_INTENT

import featuregen.overlay.upload.contract.gate1 as gate1
from featuregen.intake.llm import FakeLLM, FakeResponse
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.contract.gate1 import (
    _public_considered_snapshot,
    build_considered_set,
)
from featuregen.overlay.upload.contract.intake import submit_intent
from featuregen.overlay.upload.enrich import content_hash
from featuregen.overlay.upload.feature_metadata_snapshot import ensure_generation_run
from featuregen.overlay.upload.graph import build_graph
from featuregen.overlay.upload.taxonomy.applicability import ConfirmedScope

NOW = datetime(2026, 7, 10, tzinfo=UTC)
CHURN = "customer.relationship_attrition.churn"
HYPOTHESIS = "customers churn when their balance drops over 30 days"
RUN_ID = "grun_s1b3_enqueue"
#: A real V2 registry recipe — the eligible set the route threads is the V2 universe.
V2_RECIPE = "posted_transaction_average_amount"
FLAG = "FEATUREGEN_INTENT_SHADOW_TELEMETRY"


class _Rollback(Exception):
    """Unwinds a build's savepoint so the next build starts from the identical state."""


# ── harness ───────────────────────────────────────────────────────────────────────────────────


def _bank_churn(db) -> None:
    """The concept-tagged churn catalog the engine lens actually grounds on (test_gate1's)."""
    catalog = [
        (CanonicalRow("bank", "accounts", "customer_id", "integer", is_grain=True,
                      entity="Customer"), "customer_id"),
        (CanonicalRow("bank", "accounts", "balance", "numeric", additivity="semi_additive",
                      currency="USD"), "monetary_stock"),
        (CanonicalRow("bank", "accounts", "as_of_date", "timestamp", as_of=True), "as_of_date"),
        (CanonicalRow("bank", "accounts", "amount", "numeric", additivity="additive",
                      currency="USD"), "monetary_flow"),
        (CanonicalRow("bank", "accounts", "event_ts", "timestamp"), "event_timestamp"),
        (CanonicalRow("bank", "accounts", "churned", "boolean"), "outcome_label"),
    ]
    build_graph(db, "bank", [r for r, _ in catalog],
                concepts={content_hash(r): c for r, c in catalog})
    db.execute(
        "INSERT INTO overlay_drift_watermark (catalog_source, last_completed_at, last_run_id, "
        "head_seq) VALUES ('bank', %s, 'r', 0) "
        "ON CONFLICT (catalog_source) DO UPDATE SET last_completed_at = %s", (NOW, NOW))


def _client() -> FakeLLM:
    return FakeLLM(script={
        "overlay.feature.intents": FakeResponse(output=EXTRACTED_INTENT),
        "overlay.feature.recommend_set": FakeResponse(output={
            "recommended_lens": "monetary", "reasoning": "monetary fits the hypothesis"}),
    })


class _CountingCursor:
    def __init__(self, cursor, owner):
        self._cursor = cursor
        self._owner = owner

    def execute(self, *args, **kwargs):
        self._owner.queries += 1
        return self._cursor.execute(*args, **kwargs)

    def executemany(self, *args, **kwargs):
        self._owner.queries += 1
        return self._cursor.executemany(*args, **kwargs)

    def __enter__(self):
        self._cursor.__enter__()
        return self

    def __exit__(self, *exc):
        return self._cursor.__exit__(*exc)

    def __iter__(self):
        return iter(self._cursor)

    def __getattr__(self, name):
        return getattr(self._cursor, name)


class _CountingConn:
    """Counts every statement the builder issues, through either entry point."""

    def __init__(self, conn):
        self._conn = conn
        self.queries = 0

    def execute(self, *args, **kwargs):
        self.queries += 1
        return self._conn.execute(*args, **kwargs)

    def cursor(self, *args, **kwargs):
        return _CountingCursor(self._conn.cursor(*args, **kwargs), self)

    def __getattr__(self, name):
        return getattr(self._conn, name)


def _intent():
    return submit_intent(hypothesis=HYPOTHESIS, actor="ds1")


def _build(conn, intent, **kwargs):
    return build_considered_set(
        conn, intent, _client(), catalog_source="bank",
        target_ref="public.accounts.churned", now=NOW,
        scope=ConfirmedScope(primary=CHURN), target_entity="Customer",
        generation_run_id=RUN_ID, **kwargs)


def _build_in_a_savepoint(db, intent, *, count: bool = False, **kwargs) -> dict:
    """Run one build, capture what it served and what it queued, then ROLL IT BACK.

    Both sides of the flag therefore run against byte-identical starting state — without this, the
    second build would be reading the semantic-candidate and revision rows the first one wrote, and
    a byte-identity claim over the two would be measuring the wrong thing."""
    captured: dict = {}
    conn = _CountingConn(db) if count else db
    try:
        with db.transaction():
            cs = _build(conn, intent, **kwargs)
            captured["payload"] = json.dumps(_public_considered_snapshot(db, cs), sort_keys=True,
                                             default=str)
            captured["outbox"] = db.execute(
                "SELECT work_item_id, generation_run_id, intent_id, frozen_inputs, status "
                "FROM governed_telemetry_outbox ORDER BY work_item_id").fetchall()
            captured["queries"] = conn.queries if count else 0
            raise _Rollback
    except _Rollback:
        pass
    return captured


def _run_bound_to(db, intent) -> None:
    ensure_generation_run(db, RUN_ID, {"subject": "ds1"}, {"intake_mode": "hypothesis"},
                          intent_id=intent.intent_id)


def _ready(db):
    """Seed the catalog and bind a run to a fresh intent — the state every build starts from."""
    _bank_churn(db)
    intent = _intent()
    gate1.persist_intent(db, intent)
    _run_bound_to(db, intent)
    return intent


#: TWO things in a served payload move between two IDENTICAL builds, both by design and both
#: predating this task. The pin canonicalizes exactly these two and compares everything else byte
#: for byte; ``test_two_identical_builds_differ_only_in_their_minted_ids`` is the control that
#: proves the canonicalization removes only that much.
#:
#: 1. **Minted ids** (``mint_id`` is random). They reach the payload through the option id:
#:    ``_with_option_ids`` hashes each candidate's frozen decision facts, which carry the semantic
#:    observation id this build minted.
#: 2. **Two UNPREFIXED ids**, matched by KEY rather than by shape so no content hash is caught in
#:    the net: an ``intent_id`` (a bare uuid4 hex) and an LLM intent's ``source_definition_id`` (a
#:    64-hex content hash over the whole intent body — which includes
#:    ``GenerationProvenanceV1.call_ref``, the audited call that produced it, so an intent from a
#:    different call genuinely IS a different intent).
#:
#: Everything is replaced by its ORDER OF FIRST APPEARANCE, so a payload with a different NUMBER of
#: ids, ids in different positions, or any different value anywhere else still differs.
#: ``mint_id`` produces Crockford base32 (upper) and some ids are lowercase hex; the body charset
#: admits either, and never a mixed-case word, so a plain identifier can never match.
_MINTED_ID = re.compile(r"\b([a-z][a-z0-9]{1,7})_(?:[0-9A-Z]{16,}|[0-9a-f]{16,})\b")
_KEYED_ID = re.compile(r'("(?:intent_id|source_definition_id)": ")([0-9a-f]{32,64})(")')


def _canonical_ids(payload: str) -> str:
    seen: dict[str, str] = {}

    def _token(prefix: str, value: str) -> str:
        if value not in seen:
            seen[value] = f"{prefix}#{len(seen)}"
        return seen[value]

    payload = _MINTED_ID.sub(lambda m: _token(f"{m.group(1)}_", m.group(0)), payload)
    return _KEYED_ID.sub(
        lambda m: f"{m.group(1)}{_token('id_', m.group(2))}{m.group(3)}", payload)


# ── 1. the flag ───────────────────────────────────────────────────────────────────────────────


def test_flag_off_queues_nothing(db):
    intent = _ready(db)
    captured = _build_in_a_savepoint(db, intent, v2_eligible_ids=frozenset({V2_RECIPE}))
    assert captured["outbox"] == []


def test_the_builder_never_reads_the_environment(db, monkeypatch):
    """``FEATUREGEN_INTENT_SHADOW_TELEMETRY`` is the ROUTE's to read, exactly like the two shadow
    flags beside it. A builder that reads its own env cannot be tested, cannot be driven by a
    caller, and quietly becomes a second place the deployment is configured."""
    monkeypatch.setenv(FLAG, "1")
    intent = _ready(db)
    captured = _build_in_a_savepoint(db, intent)
    assert captured["outbox"] == []
    # the docstring NAMES the flag (it says whose job reading it is); the CODE must not touch it
    body = inspect.getsource(gate1.build_considered_set).replace(
        gate1.build_considered_set.__doc__ or "", "")
    assert "environ" not in body and "getenv" not in body and FLAG not in body


# ── 2. what the item carries ──────────────────────────────────────────────────────────────────


def test_the_enqueue_freezes_this_runs_inputs(db):
    db.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ   # the production C0 isolation
    intent = _ready(db)

    captured = _build_in_a_savepoint(
        db, intent, telemetry_enabled=True,
        v2_eligible_ids=frozenset({V2_RECIPE, "not_a_recipe"}))

    assert len(captured["outbox"]) == 1
    work_item_id, run_id, item_intent, frozen, status = captured["outbox"][0]
    assert work_item_id.startswith("gto_")
    assert (run_id, item_intent, status) == (RUN_ID, intent.intent_id, "queued")

    assert frozen["frozen_inputs_version"] == "1"
    assert frozen["target_entity"] == "Customer"
    assert frozen["anchor_catalog_source"] == "bank"
    assert frozen["redacted_hypothesis"] == intent.redacted_hypothesis
    # the C0 snapshot the set was authored against — the worker replans what THIS run saw
    assert frozen["metadata_snapshot_id"] is not None
    assert frozen["metadata_snapshot_id"].startswith("snap")

    material = frozen["catalog_scope_material"]
    assert material["authorized_catalog_sources"] == ["bank"]
    assert material["target_entity"] == "Customer"
    assert material["read_scope_policy_version"]
    assert "scope_id" not in material and "catalog_state_stamps" not in material

    frozen_ids = [ref["canonical_definition_id"] for ref in frozen["requests"]]
    assert V2_RECIPE in frozen_ids     # the eligible recipe, as a reference
    assert "not_a_recipe" not in frozen_ids       # an id this build does not carry is skipped
    recipe_ref = next(r for r in frozen["requests"]
                      if r["canonical_definition_id"] == V2_RECIPE)
    assert recipe_ref["origin"] == "recipe_v2" and recipe_ref["planning_request_hash"]
    # every intent this run's engine proposed rides along WHOLE — there is no registry to
    # re-derive one from, so the worker replans the bytes this run saw and calls no provider
    intents = [ref for ref in frozen["requests"] if ref["origin"] == "llm_intent"]
    assert intents and all("payload" in ref for ref in intents)


def test_the_snapshot_id_is_honestly_absent_off_the_feature_generation_connection(db):
    """A READ COMMITTED caller takes no C0 snapshot at all. The item still queues — with a NULL
    snapshot id, which is the honest answer, not a fabricated pointer."""
    intent = _ready(db)
    captured = _build_in_a_savepoint(db, intent, telemetry_enabled=True)
    assert captured["outbox"][0][3]["metadata_snapshot_id"] is None


# ── 3. the two pins ───────────────────────────────────────────────────────────────────────────


def test_the_served_payload_is_byte_identical_on_both_sides_of_the_flag(db):
    intent = _ready(db)
    eligible = frozenset({V2_RECIPE})

    off = _build_in_a_savepoint(db, intent, v2_eligible_ids=eligible)
    on = _build_in_a_savepoint(db, intent, telemetry_enabled=True, v2_eligible_ids=eligible)

    assert _canonical_ids(on["payload"]) == _canonical_ids(off["payload"])
    assert off["outbox"] == [] and len(on["outbox"]) == 1


def test_two_identical_builds_differ_only_in_their_minted_ids(db):
    """The control for the pin above: it is comparing a real payload, and the canonicalization is
    removing exactly the randomness ``mint_id`` introduces — nothing else."""
    intent = _ready(db)
    first = _build_in_a_savepoint(db, intent)
    second = _build_in_a_savepoint(db, intent)
    assert first["payload"] != second["payload"]                       # minted ids move
    assert _canonical_ids(first["payload"]) == _canonical_ids(second["payload"])


def test_the_enqueue_costs_a_constant_two_statements(db):
    """ONE scope-material read and ONE outbox INSERT — and nothing that scales with the eligible
    set. The recipe references are minted from the IN-CODE registry, so a run eligible for three
    hundred recipes queries exactly as often as a run eligible for one."""
    intent = _ready(db)
    small = frozenset({V2_RECIPE})
    large = frozenset(t.id for t in gate1.ALL_TEMPLATES)

    off = _build_in_a_savepoint(db, intent, count=True, v2_eligible_ids=small)
    on = _build_in_a_savepoint(db, intent, count=True, telemetry_enabled=True,
                               v2_eligible_ids=small)
    on_large = _build_in_a_savepoint(db, intent, count=True, telemetry_enabled=True,
                                     v2_eligible_ids=large)

    assert on["queries"] - off["queries"] == 2
    assert on_large["queries"] == on["queries"]


# ── 4. fail-soft ──────────────────────────────────────────────────────────────────────────────


def test_a_poisoned_enqueue_never_fails_the_build(db, monkeypatch):
    """Telemetry is never worth a 500. A broken enqueue is logged and the human still gets the
    considered set — and the savepoint means the poisoned write cannot leave the request's
    transaction unusable either."""
    intent = _ready(db)
    baseline = _build_in_a_savepoint(db, intent)

    def _explode(conn, **kwargs):
        conn.execute("SELECT 1 FROM a_table_that_does_not_exist")

    monkeypatch.setattr(
        "featuregen.overlay.upload.governed_observation_store.enqueue_governed_telemetry",
        _explode)
    poisoned = _build_in_a_savepoint(db, intent, telemetry_enabled=True)

    assert _canonical_ids(poisoned["payload"]) == _canonical_ids(baseline["payload"])
    assert poisoned["outbox"] == []


def test_an_unbound_run_does_not_queue(db):
    """1121's lineage trigger refuses a work item whose intent is not the run's own. That refusal
    must show up as a missing telemetry row and a served considered set — never as a 500."""
    _bank_churn(db)
    intent = _intent()
    gate1.persist_intent(db, intent)
    other = submit_intent(hypothesis="a different question entirely", actor="ds2")
    gate1.persist_intent(db, other)
    ensure_generation_run(db, RUN_ID, {"subject": "ds1"}, {}, intent_id=other.intent_id)

    captured = _build_in_a_savepoint(db, intent, telemetry_enabled=True)
    assert captured["outbox"] == []
    assert captured["payload"]
