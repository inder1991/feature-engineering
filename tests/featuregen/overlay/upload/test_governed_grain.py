"""The ONE governed-`GRAIN` reader (``overlay.upload.governed_grain``).

Every grain here is established through the REAL four-eyes governance flow (service ``propose_fact``
-> platform-admin ``confirm_fact`` -> drain), and every projection through the REAL
``table_fact_projection``. A test that manufactured a VERIFIED row, or that asserted a grain the
reader did not read out of the governed store, would be a failure of THIS file rather than a pass.

The reader's contract is deliberately narrow and every branch of it is pinned below: it answers
"these columns, ALL of them, identify at most one row of this table RIGHT NOW", or it says why it
cannot. Its consumers (``materialize.spine``'s key-uniqueness refusal and the contract compiler's
bridge-hop cardinality) both depend on the refusals being exhaustive, because both fail OPEN if a
narrower, staler or non-unique grain is served as if it were the whole current one.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from tests.featuregen.overlay.upload.conftest import _confirm_grain, _drain, _reject_grain

from featuregen.contracts.envelopes import Command
from featuregen.overlay.commands import propose_fact
from featuregen.overlay.identity import fact_key
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.enrich import content_hash
from featuregen.overlay.upload.governed_grain import (
    GovernedGrain,
    GrainRefusal,
    GrainUnattested,
    governed_grain_columns,
    read_governed_grain,
)
from featuregen.overlay.upload.graph import build_graph
from featuregen.overlay.upload.table_fact_projection import project_table_facts_for_ref
from featuregen.overlay.upload.upload_catalog import ensure_upload_catalog_adapter, table_ref

_NOW = datetime(2026, 7, 26, tzinfo=UTC)


# ── seeding through the real write paths ─────────────────────────────────────────────────────────
def _graph(db, source: str, table: str, columns: dict[str, bool]) -> None:
    """``graph_node`` for one table via the REAL ``build_graph``. ``columns`` maps a column name to
    the FILE-DECLARED ``is_grain`` flag the upload asserts — an uploader's claim about their own
    file, which is exactly what the reader must never treat as an attestation."""
    rows = [CanonicalRow(source, table, name, "integer", is_grain=grain)
            for name, grain in columns.items()]
    build_graph(db, source, rows,
                concepts={content_hash(r): "customer_id" for r in rows})


def _propose_grain(db, source: str, table: str, columns: list[str], *, actor,
                   is_unique: bool = True) -> None:
    ref = table_ref(source, table)
    res = propose_fact(db, Command(
        "propose_fact", "overlay_fact", None,
        {"ref": ref, "fact_type": "grain",
         "proposed_value": {"columns": columns, "is_unique": is_unique}},
        actor, f"propose-grain-{source}-{table}-{is_unique}"))
    assert res.accepted, res.denied_reason


def _verified_grain(db, source: str, table: str, columns: list[str], *,
                    service_actor, human_actor) -> None:
    """A VERIFIED grain fact via the real four-eyes flow. ``is_unique`` is True — the reader's
    uniqueness branch has its own helper below, because the confirm helper hard-codes True."""
    _propose_grain(db, source, table, columns, actor=service_actor)
    _confirm_grain(db, source, table, columns, actor=human_actor)


def _verified_grain_not_unique(db, source: str, table: str, columns: list[str], *,
                               service_actor, human_actor) -> None:
    """A VERIFIED grain fact whose ``is_unique`` is FALSE — the shape ``profiler_heuristics`` emits
    for a near-unique candidate ("proposed, but with ``is_unique=False`` so a human decides"), and
    the shape a table whose key is unique only within some scope has to take, since the governed
    grain value has nowhere to record the scoping predicate."""
    from featuregen.overlay.commands import confirm_fact
    from tests.featuregen.overlay.upload.conftest import _open_grain_task

    value = {"columns": columns, "is_unique": False}
    _propose_grain(db, source, table, columns, actor=service_actor, is_unique=False)
    _task, target, ref = _open_grain_task(db, source, table, actor=human_actor)
    res = confirm_fact(db, Command(
        "confirm_fact", "overlay_fact", None,
        {"ref": ref, "fact_type": "grain", "target_event_id": target, "value": value},
        human_actor, f"confirm-nonunique-{target}"))
    assert res.accepted, res.denied_reason
    _drain(db)


@pytest.fixture
def seeded(db, service_actor, human_actor):
    """``cust`` (customer_id, region, name) with a FILE-DECLARED grain on customer_id and NO
    governed fact yet — the starting state every case below moves from."""
    ensure_upload_catalog_adapter()
    _graph(db, "crm", "cust", {"customer_id": True, "region": False, "name": False})
    return db


# ── 1) the positive: a VERIFIED, unique, complete grain reads back exactly ───────────────────────
def test_verified_unique_grain_reads_back_complete(seeded, service_actor, human_actor):
    _verified_grain(seeded, "crm", "cust", ["customer_id"],
                    service_actor=service_actor, human_actor=human_actor)
    grain = read_governed_grain(seeded, "crm", "cust", now=_NOW)
    assert isinstance(grain, GovernedGrain)
    assert grain.columns == ("customer_id",)
    assert grain.fact_event_id                      # the confirmation the answer came from
    # and a COMPOSITE grain comes back whole — never one member of it (the caller compares SETS).
    _graph(seeded, "crm", "acct", {"acct_id": True, "asof": False})
    _verified_grain(seeded, "crm", "acct", ["acct_id", "asof"],
                    service_actor=service_actor, human_actor=human_actor)
    composite = read_governed_grain(seeded, "crm", "acct", now=_NOW)
    assert isinstance(composite, GovernedGrain)
    assert composite.columns == ("acct_id", "asof")


# ── 2) FAIL-CLOSED: no VERIFIED grain fact ───────────────────────────────────────────────────────
def test_file_declared_is_grain_alone_is_not_a_grain(seeded):
    """THE mutation control for the whole reader. ``build_graph`` wrote ``is_grain = true`` on
    customer_id straight from the upload, and ``table_fact_projection`` deliberately SPARES such
    file-declared flags when it clears — so an implementation that enumerated on the flag alone
    would answer ``("customer_id",)`` here. Nobody has confirmed anything."""
    assert seeded.execute(
        "SELECT is_grain, grain_fact_event_id FROM graph_node WHERE catalog_source = 'crm' "
        "AND column_name = 'customer_id'").fetchone() == (True, None)
    out = read_governed_grain(seeded, "crm", "cust", now=_NOW)
    assert isinstance(out, GrainUnattested)
    assert out.refusal is GrainRefusal.no_verified_grain_fact
    # …and the two-step projection read grants nothing from the flag either (that is its whole point)
    assert governed_grain_columns(seeded, "crm", "public", "cust") == ()


def test_a_grain_on_another_table_is_not_evidence_about_this_one(
        seeded, service_actor, human_actor):
    """Evidence is per table. A confirmed grain on ``crm.other`` says nothing about ``crm.cust`` —
    the fact_key is derived from the table ref, so there is no way to borrow one."""
    _graph(seeded, "crm", "other", {"customer_id": True})
    _verified_grain(seeded, "crm", "other", ["customer_id"],
                    service_actor=service_actor, human_actor=human_actor)
    out = read_governed_grain(seeded, "crm", "cust", now=_NOW)
    assert isinstance(out, GrainUnattested)
    assert out.refusal is GrainRefusal.no_verified_grain_fact
    assert isinstance(read_governed_grain(seeded, "crm", "other", now=_NOW), GovernedGrain)


def test_a_proposed_but_unconfirmed_grain_is_not_a_grain(seeded, service_actor):
    """A proposal is not a confirmation: with only ``propose_fact`` run there is no
    ``overlay_fact_state`` row at all (the projection writes it on CONFIRMED only)."""
    _propose_grain(seeded, "crm", "cust", ["customer_id"], actor=service_actor)
    _drain(seeded)
    out = read_governed_grain(seeded, "crm", "cust", now=_NOW)
    assert isinstance(out, GrainUnattested)
    assert out.refusal is GrainRefusal.no_verified_grain_fact


def test_a_rejected_grain_is_not_a_grain(seeded, service_actor, human_actor):
    _propose_grain(seeded, "crm", "cust", ["customer_id"], actor=service_actor)
    _reject_grain(seeded, "crm", "cust", actor=human_actor)
    out = read_governed_grain(seeded, "crm", "cust", now=_NOW)
    assert isinstance(out, GrainUnattested)
    assert out.refusal in (GrainRefusal.no_verified_grain_fact, GrainRefusal.fact_not_verified)


# ── 3) FAIL-CLOSED: stale / expired ──────────────────────────────────────────────────────────────
def test_a_grain_past_its_expiry_is_refused_before_the_poller_runs(
        seeded, service_actor, human_actor):
    """The read-time expiry guard, mirroring ``resolve_fact``'s: between ``expires_at`` passing and
    the async poller STALEing the fact, the read model still says VERIFIED. A reader that trusted
    the status alone would serve a grain whose re-verification is overdue."""
    _verified_grain(seeded, "crm", "cust", ["customer_id"],
                    service_actor=service_actor, human_actor=human_actor)
    (expires_at,) = seeded.execute(
        "SELECT expires_at FROM overlay_fact_state WHERE fact_key = %s",
        (fact_key(table_ref("crm", "cust"), "grain"),)).fetchone()
    assert expires_at is not None, "the confirm flow must arm an expiry for this guard to mean anything"
    assert isinstance(read_governed_grain(seeded, "crm", "cust", now=_NOW), GovernedGrain)

    after = expires_at + timedelta(seconds=1)
    out = read_governed_grain(seeded, "crm", "cust", now=after)
    assert isinstance(out, GrainUnattested)
    assert out.refusal is GrainRefusal.fact_expired
    # exactly AT expiry is already too late (`exp <= now`, as resolve_fact has it)
    assert read_governed_grain(seeded, "crm", "cust", now=expires_at).refusal \
        is GrainRefusal.fact_expired


def test_a_grain_the_poller_moved_off_verified_is_refused(seeded, service_actor, human_actor):
    """Once the expiry poller has run the fact sits in REVERIFY, still carrying its last VERIFIED
    value as read-only context. VERIFIED is the only servable status."""
    _verified_grain(seeded, "crm", "cust", ["customer_id"],
                    service_actor=service_actor, human_actor=human_actor)
    seeded.execute(
        "UPDATE overlay_fact_state SET status = 'REVERIFY' WHERE fact_key = %s",
        (fact_key(table_ref("crm", "cust"), "grain"),))
    out = read_governed_grain(seeded, "crm", "cust", now=_NOW)
    assert isinstance(out, GrainUnattested)
    assert out.refusal is GrainRefusal.fact_not_verified


def test_a_stale_column_projection_that_disagrees_with_the_fact_is_refused(
        seeded, service_actor, human_actor):
    """The two-step projection read as CONTRADICTION DETECTOR. The fact says the grain is
    (customer_id, region); the governed column projection still says (customer_id) alone, because it
    was written for an earlier confirmation. One of the two is stale and neither can be trusted — a
    grain that is wider than its projection looks is the exact shape of an unnoticed fan-out."""
    _verified_grain(seeded, "crm", "cust", ["customer_id"],
                    service_actor=service_actor, human_actor=human_actor)
    project_table_facts_for_ref(seeded, source="crm", table="cust", now=_NOW)
    assert governed_grain_columns(seeded, "crm", "public", "cust") == ("customer_id",)

    # the fact stream moves on to a WIDER grain; the column projection is not re-run
    seeded.execute(
        """UPDATE overlay_fact_state
           SET value = '{"columns": ["customer_id", "region"], "is_unique": true}'::jsonb
           WHERE fact_key = %s""", (fact_key(table_ref("crm", "cust"), "grain"),))
    out = read_governed_grain(seeded, "crm", "cust", now=_NOW)
    assert isinstance(out, GrainUnattested)
    assert out.refusal is GrainRefusal.fact_projection_disagreement


def test_an_unrun_column_projection_is_not_counter_evidence(seeded, service_actor, human_actor):
    """The other side of that coin, and the one that is easy to get wrong. The second projection is
    written only by end-of-ingest and the table-fact confirm route, so a grain confirmed by any other
    path is VERIFIED with no link yet. That is a cache being behind, not evidence against the fact —
    refusing here would withdraw a genuinely governed grain."""
    _verified_grain(seeded, "crm", "cust", ["customer_id"],
                    service_actor=service_actor, human_actor=human_actor)
    assert governed_grain_columns(seeded, "crm", "public", "cust") == ()   # never projected
    grain = read_governed_grain(seeded, "crm", "cust", now=_NOW)
    assert isinstance(grain, GovernedGrain) and grain.columns == ("customer_id",)


# ── 4) FAIL-CLOSED: uniqueness attested only within a scope ──────────────────────────────────────
def test_a_grain_without_attested_uniqueness_is_refused(seeded, service_actor, human_actor):
    """``is_unique=False`` means "these are the grain columns, but nothing attests they identify at
    most one row" — the shape a key that is unique only inside some scope (current rows of an SCD
    table, one partition) has to take, because ``FACT_VALUE_SCHEMAS[GRAIN]`` is closed and has
    nowhere to record the scoping predicate. Without the predicate the safe join cannot be written,
    so the claim cannot be relied on.

    Test strength: the fact IS VERIFIED, complete, unexpired and consistent with the projection —
    every other branch of the reader passes. Only uniqueness fails."""
    _verified_grain_not_unique(seeded, "crm", "cust", ["customer_id"],
                               service_actor=service_actor, human_actor=human_actor)
    row = seeded.execute(
        "SELECT status, value FROM overlay_fact_state WHERE fact_key = %s",
        (fact_key(table_ref("crm", "cust"), "grain"),)).fetchone()
    assert row[0] == "VERIFIED" and row[1]["is_unique"] is False
    out = read_governed_grain(seeded, "crm", "cust", now=_NOW)
    assert isinstance(out, GrainUnattested)
    assert out.refusal is GrainRefusal.uniqueness_not_attested


# ── 5) FAIL-CLOSED: a value that is not a governed grain ─────────────────────────────────────────
@pytest.mark.parametrize(
    ("payload", "why"),
    [('{"columns": [], "is_unique": true}', "empty column list (schema minItems 1)"),
     ('{"columns": ["customer_id"]}', "no is_unique at all"),
     ('"customer_id"', "not an object")],
    ids=["empty_columns", "missing_is_unique", "not_an_object"])
def test_a_value_that_is_not_a_governed_grain_is_refused(
        seeded, service_actor, human_actor, payload, why):
    _verified_grain(seeded, "crm", "cust", ["customer_id"],
                    service_actor=service_actor, human_actor=human_actor)
    seeded.execute(
        "UPDATE overlay_fact_state SET value = %s::jsonb WHERE fact_key = %s",
        (payload, fact_key(table_ref("crm", "cust"), "grain")))
    out = read_governed_grain(seeded, "crm", "cust", now=_NOW)
    assert isinstance(out, GrainUnattested), why
    assert out.refusal is GrainRefusal.fact_value_invalid, why


def test_a_grain_naming_a_column_the_catalog_does_not_have_is_refused(
        seeded, service_actor, human_actor):
    """Membership, mirroring ``b_source_grain``'s: a fact describing a table shape this catalog does
    not carry cannot be compared to anything the compiler binds."""
    _verified_grain(seeded, "crm", "cust", ["customer_id"],
                    service_actor=service_actor, human_actor=human_actor)
    seeded.execute(
        """UPDATE overlay_fact_state
           SET value = '{"columns": ["customer_id", "ghost_col"], "is_unique": true}'::jsonb
           WHERE fact_key = %s""", (fact_key(table_ref("crm", "cust"), "grain"),))
    out = read_governed_grain(seeded, "crm", "cust", now=_NOW)
    assert isinstance(out, GrainUnattested)
    assert out.refusal is GrainRefusal.grain_columns_absent


# ── 6) the shared two-step still behaves as ``spine`` needs it to ────────────────────────────────
def test_the_two_step_grants_only_a_flagged_and_linked_column(seeded, service_actor, human_actor):
    """``governed_grain_columns`` is ``spine._governed_grain_columns`` moved, so its own contract is
    pinned here: the flag enumerates, C1 decides. A flagged column with no fact-event link is a file
    declaration and grants nothing; the same column grants once the real projection links it."""
    assert governed_grain_columns(seeded, "crm", "public", "cust") == ()
    _verified_grain(seeded, "crm", "cust", ["customer_id"],
                    service_actor=service_actor, human_actor=human_actor)
    assert governed_grain_columns(seeded, "crm", "public", "cust") == ()   # fact alone: no link yet
    project_table_facts_for_ref(seeded, source="crm", table="cust", now=_NOW)
    assert governed_grain_columns(seeded, "crm", "public", "cust") == ("customer_id",)
    # and dropping the link alone withdraws it again (the link IS the governed half)
    seeded.execute("UPDATE graph_node SET grain_fact_event_id = NULL WHERE catalog_source = 'crm'")
    assert governed_grain_columns(seeded, "crm", "public", "cust") == ()
