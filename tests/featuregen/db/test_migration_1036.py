"""1036 — converge cross-catalog bridge rows onto ONE endpoint orientation.

A migration's audit is blind to the data it will actually meet: ``apply_migrations`` always runs on
a FRESH database in CI, so a repair that aborts on a legacy shape passes every test and fails on the
one database that matters. These tests seed the legacy shapes FIRST and then re-apply the migration
SQL exactly as the runner does — the only way to exercise the branches that only exist for old rows.

The shapes that matter:

* the duplicate pair — one fact_key, two rows, contradictory evidence (what was observed live);
* a lone non-canonical row with no twin, which must be turned round rather than deleted;
* the hostile inputs the repair must survive rather than abort on: a non-object ``evidence_json``,
  a missing ``type_basis``, and a case-variant twin whose primary key does NOT match.
"""
from __future__ import annotations

import json
from pathlib import Path

import featuregen.db.migrations as _migrations

_CORE = ("core", "public.customer_master.customer_id")
_CRM = ("crm", "public.customers.customer_id")
_KEY = "a" * 64


def _migration_1036_sql() -> str:
    return (Path(_migrations.__file__).resolve().parent / "migrations"
            / "1036_bridge_endpoint_canonical_orientation.sql").read_text(encoding="utf-8")


def _row(db, *, left, right, key=_KEY, entity="customer", family="text", evidence="{}"):
    l_src, l_ref = left
    r_src, r_ref = right
    db.execute(
        "INSERT INTO entity_bridge_candidate_evidence (entity_id, left_catalog_source, "
        "  left_object_ref, right_catalog_source, right_object_ref, candidate_id, fact_key, "
        "  data_type_family, evidence_json, derivation_version, updated_at) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'legacy',now())",
        (entity, l_src, l_ref, r_src, r_ref, f"c-{l_src}-{r_src}", key, family, evidence))


def _ledger(db):
    return db.execute(
        "SELECT left_catalog_source, left_object_ref, right_catalog_source, right_object_ref, "
        "       data_type_family, evidence_json FROM entity_bridge_candidate_evidence "
        "ORDER BY left_catalog_source").fetchall()


def _evidence(*, basis, left_grain, right_grain) -> str:
    return json.dumps({"type_basis": basis, "left_is_grain": left_grain,
                       "right_is_grain": right_grain})


def test_1036_collapses_a_contradictory_pair_into_the_canonical_row(db):
    _row(db, left=_CORE, right=_CRM, family="text",
         evidence=_evidence(basis="attested", left_grain=True, right_grain=False))
    # named the other way round, so canonically this row says CRM's side is the key
    _row(db, left=_CRM, right=_CORE, family="uuid",
         evidence=_evidence(basis="declared", left_grain=True, right_grain=False))

    db.execute(_migration_1036_sql())

    rows = _ledger(db)
    assert len(rows) == 1
    left_src, left_ref, right_src, right_ref, family, ev = rows[0]
    assert (left_src, left_ref, right_src, right_ref) == (*_CORE, *_CRM)
    # each row knew about a different side's key — read canonically, both facts survive the merge
    assert ev["left_is_grain"] is True and ev["right_is_grain"] is True
    # a contradicted "attested" must not survive as attested
    assert ev["type_basis"] == "declared"
    assert family == "text"          # deterministic, not "whichever row was written last"


def test_1036_is_insensitive_to_which_row_was_written_first(db):
    # named the other way round, so canonically this row says CRM's side is the key
    _row(db, left=_CRM, right=_CORE, family="uuid",
         evidence=_evidence(basis="declared", left_grain=True, right_grain=False))
    _row(db, left=_CORE, right=_CRM, family="text",
         evidence=_evidence(basis="attested", left_grain=True, right_grain=False))

    db.execute(_migration_1036_sql())

    assert _ledger(db) == [(*_CORE, *_CRM, "text",
                            {"type_basis": "declared", "left_is_grain": True,
                             "right_is_grain": True})]


def test_1036_turns_a_lone_non_canonical_row_round_with_its_flags(db):
    """No twin to merge with — the row is re-oriented, not dropped, and the per-side flags move with
    the endpoints they describe."""
    _row(db, left=_CRM, right=_CORE,
         evidence=_evidence(basis="attested", left_grain=False, right_grain=True))

    db.execute(_migration_1036_sql())

    rows = _ledger(db)
    assert len(rows) == 1
    assert rows[0][:4] == (*_CORE, *_CRM)
    assert rows[0][5]["left_is_grain"] is True and rows[0][5]["right_is_grain"] is False
    assert rows[0][5]["type_basis"] == "attested"   # uncontradicted, so it survives


def test_1036_leaves_an_already_canonical_row_alone(db):
    ev = _evidence(basis="attested", left_grain=True, right_grain=False)
    _row(db, left=_CORE, right=_CRM, evidence=ev)
    before = _ledger(db)

    db.execute(_migration_1036_sql())

    assert _ledger(db) == before


def test_1036_is_re_runnable(db):
    _row(db, left=_CORE, right=_CRM, family="text",
         evidence=_evidence(basis="attested", left_grain=True, right_grain=False))
    # named the other way round, so canonically this row says CRM's side is the key
    _row(db, left=_CRM, right=_CORE, family="uuid",
         evidence=_evidence(basis="declared", left_grain=True, right_grain=False))

    db.execute(_migration_1036_sql())
    once = _ledger(db)
    db.execute(_migration_1036_sql())
    assert _ledger(db) == once


def test_1036_survives_evidence_json_it_cannot_interpret(db):
    """The repair runs against databases nobody has inspected. A non-object evidence_json, and a
    missing type_basis, must leave it degraded — never aborted."""
    _row(db, left=_CRM, right=_CORE, evidence='"a string, not an object"')
    _row(db, left=("zeta", "public.z.z"), right=("alpha", "public.a.a"), key="b" * 64,
         evidence="{}")

    db.execute(_migration_1036_sql())   # must not raise

    rows = _ledger(db)
    assert {r[0] for r in rows} == {"alpha", "core"}    # both turned round
    assert rows[1][5] == "a string, not an object"      # the value it could not merge, preserved


def test_1036_does_not_abort_on_a_case_variant_twin(db):
    """Step 1 matches twins case-INSENSITIVELY while the primary key is case-SENSITIVE, so a swap
    could collide. The guarded UPDATE declines to move such a row rather than raising a unique
    violation — the read-time merge covers it."""
    _row(db, left=("CORE", _CORE[1]), right=_CRM)
    _row(db, left=_CRM, right=_CORE)

    db.execute(_migration_1036_sql())   # must not raise

    # whatever it chose to do, no row was lost and none was left duplicated under one PK
    rows = _ledger(db)
    assert 1 <= len(rows) <= 2
    assert len({r[:4] for r in rows}) == len(rows)


def test_1036_reorients_the_verified_edge_projection(db):
    db.execute(
        "INSERT INTO entity_bridge_edge (fact_key, entity_id, left_catalog_source, "
        "  left_object_ref, right_catalog_source, right_object_ref, status) "
        "VALUES (%s,'customer',%s,%s,%s,%s,'VERIFIED')", (_KEY, *_CRM, *_CORE))

    db.execute(_migration_1036_sql())

    assert db.execute(
        "SELECT left_catalog_source, left_object_ref, right_catalog_source, right_object_ref "
        "FROM entity_bridge_edge WHERE fact_key = %s", (_KEY,)).fetchone() == (*_CORE, *_CRM)


def test_1036_does_not_touch_two_genuinely_different_bridges(db):
    _row(db, left=_CORE, right=_CRM, key=_KEY)
    _row(db, left=("core", "public.accounts.account_id"), right=("crm", "public.acct.acct_id"),
         key="c" * 64, entity="account")
    before = _ledger(db)

    db.execute(_migration_1036_sql())

    assert _ledger(db) == before
