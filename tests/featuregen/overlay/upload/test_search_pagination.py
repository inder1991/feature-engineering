"""Paging past the first 20 results.

`search()` capped hits at `limit` and reported an honest `total` of every match, but offered no way
to ask for the rows beyond the cap — so with two catalogs loaded (237 columns) the screen could say
"237 results" and show 20, permanently.

The correctness question for offset paging is ORDERING. The hits query already orders by
``score DESC, object_ref, catalog_source``, and that pair is unique, so the order is TOTAL — no ties
are left for the database to break arbitrarily. That is what makes a row neither repeat on one page
nor vanish between two, and it is what these tests pin: without a total order, `OFFSET` silently
returns overlapping or gapped pages instead of failing.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.ingest import ingest_upload
from featuregen.overlay.upload.search import search

_NOW = datetime(2026, 7, 28, tzinfo=UTC)
_FRESH = timedelta(days=3650)


@pytest.fixture
def many(db):
    """25 columns in one table — enough to need two pages at the default 20."""
    rows = [CanonicalRow(source="wide", table="t", column=f"c{i:02d}", type="text")
            for i in range(25)]
    ingest_upload(db, "wide", rows, actor=IdentityEnvelope(
        subject="o", actor_kind="human", authenticated=True, auth_method="oidc",
        role_claims=("data_owner",)), now=_NOW)
    return db


def _page(db, *, limit=20, offset=0):
    return search(db, "", now=_NOW, fresh_within=_FRESH, limit=limit, offset=offset)


def _refs(res):
    return [(h.catalog_source, h.object_ref) for h in res.hits]


# ── the gap this closes ──────────────────────────────────────────────────────────────────────────

def test_the_second_page_returns_the_rows_the_first_page_could_not(many):
    first, second = _page(many), _page(many, offset=20)
    assert len(first.hits) == 20
    assert second.hits, "the rows past the cap were unreachable before offset existed"
    assert _refs(first)[:1] != _refs(second)[:1]


def test_total_counts_every_match_on_every_page(many):
    """`total` describes the whole result set, not the page — otherwise a Next control cannot know
    whether another page exists."""
    for offset in (0, 20):
        assert _page(many, offset=offset).total == _page(many).total


def test_paging_past_the_end_is_empty_not_an_error(many):
    beyond = _page(many, offset=10_000)
    assert beyond.hits == []
    assert beyond.total == _page(many).total


# ── the ordering property that makes offset paging correct ───────────────────────────────────────

def test_no_row_appears_on_two_pages_and_none_is_skipped(many):
    """THE pagination invariant. A non-total ORDER BY lets the database break ties differently per
    query, which shows up exactly here: as a duplicate across pages or a row no page ever returns."""
    seen = _refs(_page(many, limit=10)) + _refs(_page(many, limit=10, offset=10)) \
        + _refs(_page(many, limit=10, offset=20))
    assert len(seen) == len(set(seen)), "a row was returned by two different pages"
    assert len(seen) == _page(many).total, "a row was skipped by every page"


def test_walking_in_pages_reproduces_one_big_page_exactly(many):
    """Same rows, same order — so paging is a WINDOW over one stable result, not a re-ranking."""
    whole = _refs(search(many, "", now=_NOW, fresh_within=_FRESH, limit=100))
    walked = []
    for offset in range(0, len(whole), 7):
        walked += _refs(_page(many, limit=7, offset=offset))
    assert walked == whole


def test_a_page_is_stable_when_asked_for_twice(many):
    assert _refs(_page(many, offset=20)) == _refs(_page(many, offset=20))


# ── the defaults and bounds ──────────────────────────────────────────────────────────────────────

def test_offset_defaults_to_the_first_page(many):
    """Every existing caller omits it, so the default must be today's behaviour unchanged."""
    assert _refs(search(many, "", now=_NOW, fresh_within=_FRESH, limit=20)) == _refs(_page(many))


def test_facets_describe_the_whole_set_not_the_page(many):
    """Facet counts are computed over all matches; paging must not shrink them, or the counts would
    contradict `total` and disagree page to page."""
    assert _page(many, offset=20).facets == _page(many).facets
