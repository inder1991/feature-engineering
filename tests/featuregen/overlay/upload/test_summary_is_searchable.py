"""A drafted summary must actually become FINDABLE.

Discovery is half the reason the summary exists: a search for "new to bank" matched nothing, though
two CIB columns are exactly that, because the only indexed text was a description shared by twelve
columns.

This exists because the wiring failed SILENTLY. `_search_doc_params` grew an `ai_summary` parameter
with a `= None` default, so `rebuild_search_doc` — which never selected the column — kept passing
nothing and indexed an empty string. Every test still passed, the projection wrote the value, and
search would simply never have found it. A default made a missing wire look like a working one.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.graph import rebuild_search_doc
from featuregen.overlay.upload.ingest import ingest_upload
from featuregen.overlay.upload.search import search

_NOW = datetime(2026, 7, 28, tzinfo=UTC)
_FRESH = timedelta(days=3650)
_SUMMARY = "Whether the customer is currently classified as new to bank."


@pytest.fixture
def seeded(db):
    """One column whose DECLARED definition is the useless bucket sentence — the real shape — and
    whose summary carries the only distinguishing words."""
    rows = [CanonicalRow(source="cib", table="bo_cib_customer", column="cust_curr_ntb_flg",
                         type="text",
                         definition="Status or indicator used to classify customer condition.")]
    ingest_upload(db, "cib", rows, actor=IdentityEnvelope(
        subject="o", actor_kind="human", authenticated=True, auth_method="oidc",
        role_claims=("data_owner",)), now=_NOW)
    return db


def _hits(db, q):
    return [h.column for h in search(db, q, now=_NOW, fresh_within=_FRESH).hits]


def test_the_declared_description_alone_does_not_make_it_findable(seeded):
    """The precondition — this is the problem, reproduced."""
    assert _hits(seeded, "new to bank") == []


def test_a_projected_summary_makes_the_column_findable(seeded):
    """THE property. Writing the summary onto the node and rebuilding the doc must put its words in
    the index — the step that was silently doing nothing."""
    seeded.execute("UPDATE graph_node SET ai_summary = %s WHERE column_name = %s",
                   (_SUMMARY, "cust_curr_ntb_flg"))
    rebuild_search_doc(seeded, "cib", "public.bo_cib_customer.cust_curr_ntb_flg")
    assert "cust_curr_ntb_flg" in _hits(seeded, "new to bank")


def test_the_rebuild_does_not_drop_the_definition_from_the_index(seeded):
    """Adding a slot must not displace one: the source's own words stay searchable."""
    seeded.execute("UPDATE graph_node SET ai_summary = %s WHERE column_name = %s",
                   (_SUMMARY, "cust_curr_ntb_flg"))
    rebuild_search_doc(seeded, "cib", "public.bo_cib_customer.cust_curr_ntb_flg")
    assert "cust_curr_ntb_flg" in _hits(seeded, "classify customer condition")


def test_a_node_with_no_summary_still_rebuilds(seeded):
    """The common case — a technical upload has no summary at all — must not break the doc."""
    rebuild_search_doc(seeded, "cib", "public.bo_cib_customer.cust_curr_ntb_flg")
    assert "cust_curr_ntb_flg" in _hits(seeded, "classify customer condition")
