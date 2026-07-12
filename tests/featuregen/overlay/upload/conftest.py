"""Shared fixtures/helpers for the upload overlay suites (Phase 2 / Pass B, Tasks 7-12).

Sits under ``tests/featuregen/overlay/conftest.py`` (which autoregisters the overlay commands +
event types per test and owns the ``catalog``/StubCatalog fixture). This conftest adds what the
Pass B tasks need: the upload-context catalog adapter (``overlay_conn``), the service proposer /
platform-admin confirmer pair the four-eyes flow needs, a seeded physical graph, and
confirm/reconfirm/reject helpers that dispatch the REAL gate commands against the open grain task
and DRAIN THE PROJECTION afterwards — ``resolve_fact`` reads the ``overlay_fact_state`` read model,
which only the projection populates (``confirm_fact`` does not), so a helper that skipped the drain
would leave later ``resolve_fact``-based assertions reading a stale model.
"""
from __future__ import annotations

import pytest

from featuregen.contracts.envelopes import Command, IdentityEnvelope
from featuregen.overlay.catalog import _clear_catalog_adapter
from featuregen.overlay.identity import fact_key
from featuregen.overlay.projection import OverlayProjection
from featuregen.overlay.upload.upload_catalog import ensure_upload_catalog_adapter, table_ref
from featuregen.projections.runner import run_projection


@pytest.fixture(autouse=True)
def _clear_upload_adapter():
    """Task-1 carry-forward: ``ingest_upload``/``ensure_upload_catalog_adapter`` registers a
    PROCESS-GLOBAL adapter and leaves it behind; clear it after every test in this package so it
    never leaks into a test that expects ``current_catalog_adapter()`` to fail closed. Double-clear
    is a no-op, so this coexists with the overlay ``catalog`` fixture's own teardown."""
    yield
    _clear_catalog_adapter()


@pytest.fixture
def overlay_conn(db):
    """The ephemeral-PG connection (``db``: writes roll back on teardown) with the upload-context
    adapter registered, so propose/confirm/expiry resolve an adapter instead of RuntimeError."""
    ensure_upload_catalog_adapter()
    return db


@pytest.fixture
def service_actor():
    """A NON-human service proposer (mirrors ``enrich_llm._ENRICH_ACTOR``) so four-eyes holds when
    a human later confirms what the service proposed (proposer != confirmer)."""
    return IdentityEnvelope(subject="featuregen-overlay-enrichment", actor_kind="service",
                            authenticated=True, auth_method="internal", role_claims=())


@pytest.fixture
def human_actor():
    """A platform-admin HUMAN confirmer. MUST hold ``platform-admin``: grain/availability route to
    the platform-admin GOVERNANCE queue (``UploadContextAdapter.owner_of`` -> None), so a
    data_owner-role confirmer would be DENIED by the authority check."""
    from tests.featuregen._helpers import mint_test_identity
    return mint_test_identity(subject="user:admin", role_claims=("platform-admin",))


@pytest.fixture
def seeded_graph(db):
    """``graph_node`` rows for source ``src``, table ``txn``, columns id/amt/txn_id
    (kind='column', is_grain=false) — the physical columns later tasks reason over."""
    for col in ("id", "amt", "txn_id"):
        db.execute(
            "INSERT INTO graph_node (catalog_source, object_ref, kind, table_name, column_name,"
            " is_grain, is_as_of) VALUES ('src', %s, 'column', 'txn', %s, false, false)",
            (f"public.txn.{col}", col))
    return db


def _drain(conn) -> None:
    """Run the overlay projection until caught up (one pass caps at 500 events). Every
    confirm/reconfirm/reject helper MUST end here: ``resolve_fact`` reads ``overlay_fact_state``,
    which is populated ONLY by the projection."""
    while run_projection(conn, OverlayProjection()) >= 500:
        pass


def _open_grain_task(conn, source, table, *, actor, fact_type="grain"):
    """``(task_id, target_event_id, ref)`` for the open gate task on this table's ``fact_type``
    fact (opened by ``propose_fact`` or the expiry poller). The CAS target is read through
    ``get_task_proposal`` — the authorized, task-scoped read a real confirmer uses."""
    from featuregen.overlay.task_read import get_task_proposal

    ref = table_ref(source, table)
    key = fact_key(ref, fact_type)
    row = conn.execute(
        "SELECT task_id FROM human_tasks WHERE fact_key=%s AND status='open'"
        " ORDER BY created_at DESC LIMIT 1",
        (key,),
    ).fetchone()
    assert row is not None, f"no open {fact_type} gate task for {source}.{table}"
    task_id = row[0]
    proposal = get_task_proposal(conn, task_id, actor)
    return task_id, proposal["target_event_id"], ref


def _confirm_grain(conn, source, table, columns, *, actor):
    """Confirm the open grain task -> VERIFIED with ``{columns, is_unique: True}``; drain the
    projection so ``resolve_fact`` sees VERIFIED. ``actor`` must be the platform-admin human."""
    from featuregen.overlay.commands import confirm_fact

    _task_id, target_event_id, ref = _open_grain_task(conn, source, table, actor=actor)
    res = confirm_fact(conn, Command(
        "confirm_fact", "overlay_fact", None,
        {"ref": ref, "fact_type": "grain", "target_event_id": target_event_id,
         "value": {"columns": columns, "is_unique": True}},
        actor, f"confirm-{target_event_id}"))
    assert res.accepted, res.denied_reason
    _drain(conn)
    return res


def _reject_grain(conn, source, table, *, actor):
    """Reject the open grain task -> REJECTED (sticky fingerprint); drain the projection."""
    from featuregen.overlay.commands import reject_fact

    _task_id, target_event_id, ref = _open_grain_task(conn, source, table, actor=actor)
    res = reject_fact(conn, Command(
        "reject_fact", "overlay_fact", None,
        {"ref": ref, "fact_type": "grain", "target_event_id": target_event_id,
         "reason": "not the grain"},
        actor, f"reject-{target_event_id}"))
    assert res.accepted, res.denied_reason
    _drain(conn)
    return res


def _reconfirm_grain(conn, source, table, columns, *, actor):
    """Drive a VERIFIED grain to a NEW VERIFIED value via the expiry/re-verify OVERRIDE path:
    fire the armed ``overlay_expiry`` timer far in the future (VERIFIED -> REVERIFY + a fresh gate
    task), then confirm with the override ``value`` — ``confirm_fact`` validates the FINAL value
    and would otherwise re-affirm ``state.prior_value`` (confirmation_commands.py). Drains the
    projection. NOTE: fires EVERY due overlay_expiry timer in the test database."""
    from datetime import UTC, datetime, timedelta

    from featuregen.overlay.commands import confirm_fact
    from featuregen.overlay.expiry import fire_due_overlay_expiries

    fired = fire_due_overlay_expiries(conn, now=datetime.now(UTC) + timedelta(days=4000))
    assert fired >= 1, f"no overlay_expiry timer fired for {source}.{table} — confirm it first"
    _drain(conn)  # surface EXPIRED to the read model before re-confirming
    _task_id, target_event_id, ref = _open_grain_task(conn, source, table, actor=actor)
    res = confirm_fact(conn, Command(
        "confirm_fact", "overlay_fact", None,
        {"ref": ref, "fact_type": "grain", "target_event_id": target_event_id,
         "value": {"columns": columns, "is_unique": True}},
        actor, f"reconfirm-{target_event_id}"))
    assert res.accepted, res.denied_reason
    _drain(conn)
    return res


# ── Task 12 fixtures: the whole-phase integration inputs (glossary + technical uploads and the
# fake LLM client that serves EVERY task ingest_upload can dispatch when a client is passed). ──

# A Phase-1-shaped FTR glossary whose txn table carries a REAL txn_id column — the column the canned
# Pass B synthesis proposes as the grain (make_ref_accept rejects a grain column the table lacks).
_GLOSSARY_CSV = (
    "physical_name,business_term,description_business_definition,data_domain,bian_path,fibo_path\n"
    "public.txn.txn_id,Transaction ID,Unique identifier assigned to each posted transaction.,"
    "Payments,Payment/Transaction,fibo-fbc:TransactionIdentifier\n"
    "public.txn.amt,Transaction Amount,The monetary amount of the posted transaction.,"
    "Payments,Payment/Transaction,fibo-fbc:MonetaryAmount\n")


@pytest.fixture
def glossary_rows():
    """The glossary upload for the Phase-2 integration test, built by the REAL Phase-1 reader
    (``read_glossary``). Returns the ``GlossaryUpload``: pass ``.rows`` as the ingest rows AND the
    object itself as ``glossary=`` — a glossary's ``type=unknown`` rows only pass validation under
    the glossary profile, which ``ingest_upload`` selects from the sidecar."""
    from featuregen.overlay.upload.glossary_reader import read_glossary
    return read_glossary(_GLOSSARY_CSV, source="src")


@pytest.fixture
def technical_rows():
    """A TECHNICAL upload declaring ``is_grain`` on ``id`` — a legitimate SOURCE attestation (§16)
    that ``_assert_fact`` auto-confirms VERIFIED at ingest. It ALSO carries a ``txn_id`` column so
    the canned Pass B synthesis (grain=["txn_id"]) is a REAL-but-DIFFERENT column: it passes
    ``make_ref_accept`` and reaches ``_propose_table_facts``, where the VERIFIED fact must win."""
    from featuregen.overlay.upload.canonical import CanonicalRow
    return [CanonicalRow("src", "txn", "id", "integer", is_grain=True),
            CanonicalRow("src", "txn", "txn_id", "varchar"),
            CanonicalRow("src", "txn", "amt", "numeric")]


@pytest.fixture
def fake_synth_client(glossary_rows, technical_rows):
    """A FakeLLM that answers EVERY task ``ingest_upload`` dispatches with a client: the Pass A
    stages (concept / definition / domain) in BOTH execution modes, and the Pass B ``table_synth``
    batch. Pass A must get schema-valid responses or its savepointed stages log+skip (fail-soft) —
    the integration must exercise the REAL loop, not the degraded one.

    * Constructor task-key entries serve the BATCH shapes (``{"results": [{"ref", <out_key>}]}``,
      refs = the rows' content hashes / table names) — matched when a ``*_MODE=batch`` env selects
      the batch seam. Extra refs are classified EXTRA and ignored, so one script covers both
      fixtures' row sets.
    * Finer ``.script(task, prompt_id)`` entries serve the SINGLE (default-mode) flat shapes — the
      FakeLLM resolves these BEFORE the task-key fallback, so each mode sees its own valid shape.

    The Pass B synthesis proposes grain=["txn_id"] and ABSTAINS on as-of (an as_of column absent
    from the table would invalidate the WHOLE synthesis in make_ref_accept, silently dropping the
    grain proposal too)."""
    from featuregen.intake.llm import FakeLLM, FakeResponse
    from featuregen.overlay.upload.enrich import content_hash

    hashes = [content_hash(r) for r in [*glossary_rows.rows, *technical_rows]]
    synthesis = {"grain_columns": ["txn_id"], "as_of_column": None, "as_of_basis": None,
                 "table_role": "fact", "primary_entity": "transaction",
                 "event_or_snapshot": "event"}
    client = FakeLLM(script={
        "table_synth": FakeResponse(output={"results": [{"ref": "txn", "synthesis": synthesis}]}),
        "overlay.enrich.concept": FakeResponse(output={"results": [
            {"ref": h, "concept": "monetary_stock"} for h in hashes]}),
        "overlay.enrich.definition": FakeResponse(output={"results": [
            {"ref": h, "definition": "A one-line business definition."} for h in hashes]}),
        "overlay.enrich.domain": FakeResponse(output={"results": [
            {"ref": "txn", "domain": "payments"}]}),
    })
    client.script(task="overlay.enrich.concept", prompt_id="overlay_concept_v1",
                  responses=[FakeResponse(output={"concept": "monetary_stock"})])
    client.script(task="overlay.enrich.definition", prompt_id="overlay_definition_v1",
                  responses=[FakeResponse(output={"definition": "A one-line business definition."})])
    client.script(task="overlay.enrich.domain", prompt_id="overlay_domain_v1",
                  responses=[FakeResponse(output={"domain": "payments"})])
    return client
