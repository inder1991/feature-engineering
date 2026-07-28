"""A declared varchar is not a measure — stop asking the data a question we can already answer.

`cust_num` is `varchar(150)`. Its readiness reported:

    external:TYPE_IS_NUMERIC · review · "operational type not established; a numeric-type check is
    required before this column can serve as a measure"

which surfaced as "Measure — needs a data check". We do not need to profile 111 columns to discover
that a varchar customer number is not a numeric measure; the file said so.

The gap is the same one behind the additivity guard: a glossary upload leaves the OPERATIONAL type
`unknown` on purpose ("a business glossary is not the physical-type authority"), and the readiness
check reads only that, never the DECLARED type sitting beside it. So every glossary column — all 237
in this catalog — claims a numeric check is pending.

Kept deliberately narrow, exactly like the additivity guard: a declared type is a HINT, so it is
only allowed to settle a PROVABLE contradiction (declared text cannot be numeric). It never
manufactures a pass — a declared `numeric` still leaves the check outstanding, because a spreadsheet
saying "numeric" is not a measurement of the data.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.overlay.upload.canonical import UNKNOWN_TYPE, CanonicalRow
from featuregen.overlay.upload.column_readiness import column_readiness
from featuregen.overlay.upload.column_usability import Usability, column_usability
from featuregen.overlay.upload.ingest import ingest_upload

_NOW = datetime(2026, 7, 28, tzinfo=UTC)
_REF = "public.t.c"


@pytest.fixture
def seeded(db):
    def _seed(declared: str | None):
        # UNKNOWN_TYPE is what a GLOSSARY upload produces — the shape this is about. A technical
        # upload with a real operational type already blocks correctly via the non-numeric branch.
        ingest_upload(db, "cib",
                      [CanonicalRow(source="cib", table="t", column="c", type=UNKNOWN_TYPE)],
                      actor=IdentityEnvelope(subject="o", actor_kind="human", authenticated=True,
                                             auth_method="oidc", role_claims=("data_owner",)),
                      now=_NOW)
        db.execute("UPDATE graph_node SET declared_type = %s WHERE catalog_source='cib' "
                   "AND kind='column' AND column_name='c'", (declared,))
        return column_readiness(db, source="cib", object_ref=_REF)
    return _seed


def _measure_reqs(readiness):
    return {r.requirement_id: r for r in readiness.as_measure.requirements}


# ── the defect ───────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("declared", ["varchar(150)", "text", "char(2)", "date", "timestamp(0)"])
def test_a_declared_non_numeric_type_settles_the_measure_question(seeded, declared):
    """No data check is proposed for something the file already answered."""
    reqs = _measure_reqs(seeded(declared))
    assert "external:TYPE_IS_NUMERIC" not in reqs, reqs.keys()


@pytest.mark.parametrize("declared", ["varchar(150)", "text", "date"])
def test_the_column_reports_NOT_SUITABLE_as_a_measure(seeded, declared):
    """And it says so plainly, rather than implying a pending check might yet make it a measure."""
    roles = {r.role: r for r in column_usability(seeded(declared)).roles}
    assert roles["as_measure"].state is Usability.NOT_SUITABLE
    assert declared.split("(")[0] in roles["as_measure"].detail


# ── the guard stays narrow: it never manufactures a PASS ─────────────────────────────────────────

@pytest.mark.parametrize("declared", ["numeric(18,2)", "integer", "double"])
def test_a_declared_NUMERIC_type_still_leaves_the_check_outstanding(seeded, declared):
    """A spreadsheet saying "numeric" is not a measurement of the data. The declared type may settle
    a contradiction; it may never substitute for the check that would confirm the column IS numeric."""
    assert "external:TYPE_IS_NUMERIC" in _measure_reqs(seeded(declared))


def test_no_declared_type_leaves_todays_behaviour_untouched(seeded):
    """Absent a declared type there is nothing to reason from, so the check stands — a source that
    omits the column must not be silently downgraded."""
    assert "external:TYPE_IS_NUMERIC" in _measure_reqs(seeded(None))


# ── the other roles are unaffected ───────────────────────────────────────────────────────────────

def test_a_varchar_is_still_perfectly_good_as_a_key(seeded):
    """This is about MEASURE only. A varchar customer number is exactly what a join or entity key
    looks like, and narrowing the measure verdict must not touch them."""
    roles = {r.role: r for r in column_usability(seeded("varchar(150)")).roles}
    assert roles["as_join_key"].state is not Usability.NOT_SUITABLE
    assert roles["as_entity_key"].state is not Usability.NOT_SUITABLE
