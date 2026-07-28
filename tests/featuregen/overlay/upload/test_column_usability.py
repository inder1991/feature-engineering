"""What can I use this column for? — the product view of readiness.

The readiness ENGINE is correct and stays untouched. Its FRAMING was the defect: one red BLOCKED
badge stood for three unrelated situations, and the parent-table panel shipped 341 blocking rows plus
445 review rows to every column page to say one thing.

The sharpest symptom, straight off the screen:

    MISSING  event_time  blocking · authority hint · availability_no_verified_fact

`authority hint` means a proposal EXISTS. The badge beside it says MISSING. We were calling the AI's
answer missing on the same line where we admitted having it.

The product rule (owner's direction, 2026-07-28): the tool uses AI-proposed fields whether or not a
human has reviewed them, so an unreviewed AI value is USABLE, not blocked. Only three states are
real, and they need different responses:

  * nobody has decided yet, but something is proposed -> USABLE, unreviewed
  * we cannot know without looking at the data       -> NEEDS A DATA CHECK
  * nothing is proposed by anyone                    -> NO CANDIDATE

"Blocked" is not among them.
"""
from __future__ import annotations

from featuregen.overlay.upload.column_readiness import (
    ColumnCapability,
    ColumnRequirement,
    ColumnReadiness,
)
from featuregen.overlay.upload.column_usability import Usability, column_usability


def _req(requirement_id: str, *, status: str, blocking: bool, authority: str,
         external_preview: bool = False, reason: str = "") -> ColumnRequirement:
    return ColumnRequirement(
        requirement_id=requirement_id, status=status, blocking=blocking, authority=authority,
        c1_status=None, evidence_ids=(), fact_event_id=None, decision_event_id=None,
        external_preview=external_preview, reason=reason)


def _cap(use: str, status: str, reqs: tuple[ColumnRequirement, ...]) -> ColumnCapability:
    return ColumnCapability(use=use, operational_status=status, requirements=reqs)


def _readiness(*caps: ColumnCapability) -> ColumnReadiness:
    by_use = {c.use: c for c in caps}

    def get(use: str) -> ColumnCapability:
        return by_use.get(use, _cap(use, "ready", (_req(f"{use}:identity", status="confirmed",
                                                        blocking=False, authority="structural"),)))

    return ColumnReadiness(
        source="cib", object_ref="public.bo_cib_customer.cust_aecb_dt",
        logical_ref="cib::bo_dpl_cib.bo_cib_customer.cust_aecb_dt",
        as_measure=get("as_measure"), as_entity_key=get("as_entity_key"),
        as_event_time=get("as_event_time"), as_grain_key=get("as_grain_key"),
        as_join_key=get("as_join_key"))


def _role(result, name):
    return next(r for r in result.roles if r.role == name)


# ── the three states that replace one red badge ──────────────────────────────────────────────────

def test_an_unreviewed_AI_proposal_is_USABLE_not_blocked():
    """THE reframe. `authority hint` means a proposal exists — the old UI called it MISSING and
    painted the role red."""
    caps = _cap("as_event_time", "blocked", (
        _req("event_time", status="missing", blocking=True, authority="hint"),))
    role = _role(column_usability(_readiness(caps)), "as_event_time")
    assert role.state is Usability.AI_PROPOSED
    assert "blocked" not in role.headline.lower()


def test_a_producer_strength_authority_also_counts_as_a_proposal():
    """Taxonomy-derived behaviour from an LLM concept arrives as `taxonomy/proposed`. That is a
    proposal, not an absence — it is the single most common authority in a fresh catalog."""
    caps = _cap("as_measure", "blocked", (
        _req("additivity", status="missing", blocking=True, authority="taxonomy/proposed"),))
    assert _role(column_usability(_readiness(caps)), "as_measure").state is Usability.AI_PROPOSED


def test_an_external_check_is_UNKNOWN_not_a_failure():
    """Grain uniqueness cannot be answered from metadata at all. That is an honest gap, and calling
    it blocked implies someone forgot to do something."""
    caps = _cap("as_grain_key", "ready", (
        _req("external:GRAIN_IS_UNIQUE", status="review", blocking=False,
             authority="external_check", external_preview=True),))
    role = _role(column_usability(_readiness(caps)), "as_grain_key")
    assert role.state is Usability.NEEDS_DATA_CHECK
    assert role.action == "run_data_check"


def test_nothing_proposed_by_anyone_is_NO_CANDIDATE():
    """The only case that genuinely needs someone to act before the column can serve the role."""
    caps = _cap("as_entity_key", "blocked", (
        _req("entity_assignment", status="missing", blocking=True, authority="no_decision"),))
    role = _role(column_usability(_readiness(caps)), "as_entity_key")
    assert role.state is Usability.NOT_SET
    assert role.action is not None


def test_a_confirmed_capability_is_READY():
    caps = _cap("as_join_key", "ready", (
        _req("identity", status="confirmed", blocking=False, authority="governed"),))
    assert _role(column_usability(_readiness(caps)), "as_join_key").state is Usability.CONFIRMED


def test_a_degraded_projection_stays_UNAVAILABLE():
    """The existing third state must survive: no capability can be judged over a projection C1
    refuses to read, and that is distinct from every state above."""
    caps = _cap("as_measure", "unavailable", (
        _req("projection", status="missing", blocking=True, authority="none"),))
    assert _role(column_usability(_readiness(caps)), "as_measure").state is Usability.UNAVAILABLE


# ── a proposal plus an outstanding data check is still usable ────────────────────────────────────

def test_a_proposal_with_a_pending_data_check_reports_usable_and_says_what_to_check():
    """Both facts are true at once and the old UI could only show one. `as measure` literally
    rendered READY while carrying a review saying a type check was required first."""
    caps = _cap("as_event_time", "blocked", (
        _req("event_time", status="missing", blocking=True, authority="hint"),
        _req("external:TEMPORAL_IS_POPULATED", status="review", blocking=False,
             authority="external_check", external_preview=True),))
    role = _role(column_usability(_readiness(caps)), "as_event_time")
    assert role.state is Usability.AI_PROPOSED
    assert "check" in role.detail.lower()


# ── the headline counts what a person cares about ────────────────────────────────────────────────

def test_the_headline_counts_USABLE_roles_not_ready_ones():
    """`2 / 5 ready · 3 blocked` read as failure for a catalog behaving exactly as designed. Usable
    is the number that matters, because usable is what the tool will act on."""
    result = column_usability(_readiness(
        _cap("as_event_time", "blocked",
             (_req("event_time", status="missing", blocking=True, authority="hint"),)),
        _cap("as_entity_key", "blocked",
             (_req("entity_assignment", status="missing", blocking=True, authority="no_decision"),)),
    ))
    assert result.usable_roles == 4      # 3 defaulted-ready + the unreviewed one
    assert result.total_roles == 5
    assert "4 of 5" in result.headline


def test_no_role_ever_reports_the_word_blocked():
    """The vocabulary is retired, not merely de-emphasised."""
    result = column_usability(_readiness(
        _cap("as_entity_key", "blocked",
             (_req("entity_assignment", status="missing", blocking=True, authority="no_decision"),)),
    ))
    for role in result.roles:
        assert "blocked" not in (role.headline + role.detail).lower()


# ── the machine detail is kept, just not shouted ─────────────────────────────────────────────────

def test_the_raw_requirement_ids_are_still_reachable():
    """Nothing is thrown away — an engineer debugging still needs the exact requirement. It moves
    behind a disclosure instead of being the primary content."""
    caps = _cap("as_entity_key", "blocked", (
        _req("entity_assignment", status="missing", blocking=True, authority="no_decision"),))
    assert _role(column_usability(_readiness(caps)), "as_entity_key").outstanding == \
        ("entity_assignment",)
