"""The catalog-LISTING read model (`list_visible_catalogs`) — the pick-list behind `GET /catalogs`.

There is no catalog-level visibility in this system. The ONLY mechanism is column-level: migration
1032's ``visible_requires <@ allowed_classes(roles)`` on ``graph_node`` (the same predicate
asset_detail / search / readiness apply). So catalog visibility here is DERIVED: a catalog exists for
a caller iff the caller can see at least one column in it. A caller who can see none must not learn
it exists — no name, no count, no row. That is what these tests pin.

Why ``graph_node`` and not ``overlay_proposal``: ``governance_analytics.list_source_governance_summaries``
enumerates catalogs via ``SELECT DISTINCT catalog_source FROM overlay_proposal``, which structurally
never sees an entity bridge (``overlay/projection.py`` skips bridge facts — a two-source fact cannot
live in a single-``catalog_source`` read model). ``graph_node`` is the catalog itself and carries the
visibility column, so it is both the complete universe and the only leak-free one.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.overlay.config import OverlayConfig, register_overlay_config
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.catalogs import list_visible_catalogs
from featuregen.overlay.upload.ingest import ingest_upload

_NOW = datetime(2026, 7, 30, tzinfo=UTC)


def _actor() -> IdentityEnvelope:
    return IdentityEnvelope(subject="upload", actor_kind="human", authenticated=True,
                            auth_method="oidc", role_claims=("data_owner",))


def _seal() -> None:
    register_overlay_config(OverlayConfig(
        ttl_default=timedelta(days=180), ttl_min=timedelta(days=30), ttl_max=timedelta(days=365),
        ttl_jitter_fraction=0.1, renewal_grace=timedelta(days=14),
        drift_scan_interval=timedelta(minutes=15), drift_freshness_sla=timedelta(hours=24),
        profiler_require_restricted_role=False))


@pytest.fixture
def two_catalogs(db):
    """The live shape: two catalogs, `cib` and `ftr`, plus a third (`vault`) whose EVERY column is
    pii-tagged — the existence oracle. `ftr` deliberately mixes one open and one pii column so a
    count can be checked against what the caller may actually see."""
    _seal()
    assert ingest_upload(db, "cib", [
        CanonicalRow("cib", "customers", "cif_id", "text", is_grain=True),
        CanonicalRow("cib", "customers", "opened_on", "date"),
        CanonicalRow("cib", "accounts", "acct_id", "text", is_grain=True),
    ], actor=_actor(), now=_NOW).status == "ingested"
    assert ingest_upload(db, "ftr", [
        CanonicalRow("ftr", "transfers", "tran_id", "text", is_grain=True),
        CanonicalRow("ftr", "transfers", "sender_name", "text", sensitivity="pii"),
    ], actor=_actor(), now=_NOW).status == "ingested"
    assert ingest_upload(db, "vault", [
        CanonicalRow("vault", "identities", "eida_num", "text", sensitivity="pii"),
        CanonicalRow("vault", "identities", "passport_no", "text", sensitivity="pii"),
    ], actor=_actor(), now=_NOW).status == "ingested"
    return db


def _by_source(catalogs) -> dict:
    return {c.source: c for c in catalogs}


def test_lists_the_catalogs_the_caller_can_see(two_catalogs):
    """Both open catalogs are offered, by slug, sorted — no guessing required."""
    listed = list_visible_catalogs(two_catalogs, roles=())
    assert [c.source for c in listed][:2] == ["cib", "ftr"]
    assert {"cib", "ftr"} <= set(_by_source(listed))


def test_a_catalog_with_no_visible_column_is_absent_entirely(two_catalogs):
    """THE EXISTENCE ORACLE. `vault` is pii top to bottom. A caller without `pii_reader` must not
    get a row for it at all — not a row with zero counts, not a name, nothing. Absence, not an
    empty entry."""
    open_caller = list_visible_catalogs(two_catalogs, roles=())
    assert "vault" not in _by_source(open_caller)
    assert not any("vault" in repr(c) for c in open_caller)

    # ... and the SAME query with the granting role does return it, so the absence above is the
    # scope predicate at work and not a seeding accident.
    privileged = list_visible_catalogs(two_catalogs, roles={"pii_reader"})
    assert "vault" in _by_source(privileged)
    assert _by_source(privileged)["vault"].columns == 2


def test_counts_are_scoped_to_what_the_caller_may_see(two_catalogs):
    """`ftr` has two columns, one of them pii. An unprivileged caller must be told 1, not 2 —
    a count over hidden columns is itself a leak."""
    open_ftr = _by_source(list_visible_catalogs(two_catalogs, roles=()))["ftr"]
    assert (open_ftr.tables, open_ftr.columns) == (1, 1)

    priv_ftr = _by_source(list_visible_catalogs(two_catalogs, roles={"pii_reader"}))["ftr"]
    assert (priv_ftr.tables, priv_ftr.columns) == (1, 2)

    # `cib` is fully open, so its counts are role-invariant and match the seeded shape.
    for roles in ((), {"pii_reader"}):
        cib = _by_source(list_visible_catalogs(two_catalogs, roles=roles))["cib"]
        assert (cib.tables, cib.columns) == (2, 3)


def test_a_table_whose_every_column_is_hidden_is_not_counted(db):
    """The table universe is derived from VISIBLE COLUMNS, never from `kind = 'table'` nodes — a
    table node carries no sensitivity of its own (`visible_requires = {}`), so counting those would
    hand an unprivileged caller the number of tables it cannot see."""
    _seal()
    assert ingest_upload(db, "mixed", [
        CanonicalRow("mixed", "open_t", "plain", "text"),
        CanonicalRow("mixed", "secret_t", "ssn", "text", sensitivity="pii"),
    ], actor=_actor(), now=_NOW).status == "ingested"
    # Both tables have a 'table' graph_node row, both world-visible; only one has a visible column.
    assert db.execute("SELECT count(*) FROM graph_node WHERE catalog_source = 'mixed' "
                      "AND kind = 'table'").fetchone()[0] == 2
    open_view = _by_source(list_visible_catalogs(db, roles=()))["mixed"]
    assert (open_view.tables, open_view.columns) == (1, 1)


def test_no_catalogs_at_all_is_an_empty_list_not_an_error(db):
    assert list_visible_catalogs(db, roles=()) == ()
