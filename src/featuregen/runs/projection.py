"""Run projections (spec §12): DERIVED from existing stores — the spine records no lifecycle."""
from __future__ import annotations

from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.materialize.generation_lane import generation_enabled
from featuregen.runs.pin import PRE_PIN_REASON_CODE, pin_exists
from featuregen.runs.read_policy import visibility_where


def list_runs(conn, identity: IdentityEnvelope, *, limit: int = 25,
              cursor: str | None = None) -> dict:
    """One page of runs the caller may see, grouped by intent WITHIN the page.

    Pagination is a flat keyset over `(created_at DESC, generation_run_id DESC)` — both columns
    immutable — so a concurrent insert can never shift a later page's contents the way an OFFSET
    would. The tie-breaker is not decoration: rows written in one transaction share `now()`, so
    `created_at` alone is not a key.

    Grouping runs over the PAGE, not over the query, which is what keeps the two properties
    compatible: a single intent's runs may split across a page boundary, and the UI tolerates
    that. Grouping globally would require reading past the page to know a group had ended.

    Visibility is applied INSIDE the query (spec §11), before the LIMIT, so the page is a page of
    rows this caller may see rather than a filtered remnant of someone else's page."""
    frag, params = visibility_where(identity)
    cursor_sql, cursor_params = "", []
    if cursor:
        created_at, _, run_id = cursor.partition("|")
        # ROW comparison, not two ANDed columns: `(a, b) < (x, y)` is the keyset predicate, and
        # writing it out by hand is where keyset pagination usually loses or repeats rows.
        cursor_sql = "AND (fgr.created_at, fgr.generation_run_id) < (%s::timestamptz, %s)"
        cursor_params = [created_at, run_id]
    # `visibility_where`'s params bind at the splice point, which here is FIRST — before the
    # cursor's and the limit's.
    rows = conn.execute(
        f"""SELECT fgr.generation_run_id, fgr.intent_id, ci.hypothesis, fgr.created_at,
                   fri.generation_run_id IS NOT NULL AS has_identity,
                   COALESCE(fri.owner_subject, fgr.actor->>'subject') AS owner_subject,
                   frp.display_name
            FROM feature_generation_run fgr
            LEFT JOIN feature_run_identity fri USING (generation_run_id)
            LEFT JOIN feature_run_profile  frp USING (generation_run_id)
            LEFT JOIN contract_intent      ci  ON ci.intent_id = fgr.intent_id
            WHERE ({frag}) {cursor_sql}
            ORDER BY fgr.created_at DESC, fgr.generation_run_id DESC
            LIMIT %s""",
        (*params, *cursor_params, limit + 1)).fetchall()
    # Read one MORE than the page: whether a next page exists is a fact about the data, never a
    # guess from a full page.
    page, extra = rows[:limit], rows[limit:]
    groups: list[dict] = []
    for run_id, intent_id, hypothesis, created_at, has_identity, owner, display in page:
        # Groups break on a CHANGE of intent in sort order, so one intent whose runs are
        # interleaved with another's opens two groups on the same page — the same tolerance a
        # split-across-pages group needs. Adjacent intent-less runs share the single None-keyed
        # group: the "no intent" bucket, not a claim that they share an intent.
        if not groups or groups[-1]["intent_id"] != intent_id:
            # An intent-less run has no hypothesis to show; the LEFT JOIN already yields NULL
            # (NULL = NULL never matches), and this states the intent explicitly.
            groups.append({"intent_id": intent_id,
                           "hypothesis": hypothesis if intent_id else None, "runs": []})
        groups[-1]["runs"].append({
            "generation_run_id": run_id, "display_name": display,
            "pre_spine": not has_identity, "owner_subject": owner,
            "created_at": created_at.isoformat()})
    next_cursor = None
    if extra:
        last = page[-1]
        next_cursor = f"{last[3].isoformat()}|{last[0]}"
    return {"groups": groups, "next_cursor": next_cursor}


#: TOTAL over 1090's CHECK — the exhaustiveness test pins it, so a tenth draft state cannot appear
#: without a mapping decision (the `ACTIVATION_BLOCKER_DISPOSITIONS` pattern). BLOCKED stays
#: BLOCKED rather than folding into FAILED: 1090 draws that line because they send different
#: people to different remedies.
RAIL_FROM_DRAFT_STATE = {
    "REQUESTED": "IN_PROGRESS", "AUTHORING": "IN_PROGRESS", "CRITIC_REVIEW": "IN_PROGRESS",
    "VALIDATING": "IN_PROGRESS", "ADMISSION": "IN_PROGRESS",
    "READY": "SUCCEEDED", "BLOCKED": "BLOCKED", "FAILED": "FAILED", "CANCELLED": "CANCELLED",
}

#: Worst-of order for the AUTHOR_FORMULA fold (spec §12): BLOCKED outranks everything. Explicit,
#: never alphabetical — alphabetically CANCELLED precedes FAILED and IN_PROGRESS, which would
#: report a run as cancelled while another candidate is still being authored. It ranks only the
#: values RAIL_FROM_DRAFT_STATE can produce; a new rail value reaching this fold raises rather than
#: sorting to an arbitrary place.
_AUTHOR_SEVERITY = ["BLOCKED", "FAILED", "IN_PROGRESS", "CANCELLED", "SUCCEEDED"]

#: Five sockets (spec §7): stages whose machinery does not exist, each labelled with WHY. Not
#: "not started" — that is reserved for stages that could actually run.
_SOCKETS = (
    ("EXECUTE_SANDBOX", "WORKER_NOT_IMPLEMENTED"),
    ("PUBLISH_SANDBOX", "WORKER_NOT_IMPLEMENTED"),
    ("MATERIALIZE_PRODUCTION", "STATE_MACHINE_NOT_BUILT"),
    ("PUBLISH_PRODUCTION", "STATE_MACHINE_NOT_BUILT"),
    ("TRAIN_MODEL", "SUBSYSTEM_NOT_BUILT"),
)


def _generate_preview_stage(conn) -> dict:
    """`GENERATE_PREVIEW`'s availability, folded from BOTH conditions its only entrance imposes.

    That entrance is `POST /build-sets`, and it is shut two independent ways: the deployment switch
    (`generation_enabled`, a router-level dependency that 404s every path on the surface) and the
    1101 pin (a 409 raised inside each producer). Availability is DERIVED from the deployment, so
    the rail must fold BOTH — spec §7 [R3.1] forbids a rail that reads NOT_STARTED over an entrance
    that refuses.

    Reading the pin alone was exactly that false rail, and not in some exotic deployment: 1101 is a
    committed migration and the switch is default-OFF, so EVERY test and dev database sat in the
    combination the old code called NOT_STARTED while the POST answered 404.

    PRECEDENCE follows the route's own order, and the order carries meaning rather than tidiness.
    The switch is answered first because a switched-off deployment does not have this surface AT ALL
    — the entrance is not shut, it is absent — and telling a person the pin is missing there would
    send them to an operator who would apply a migration and change nothing. Only where the surface
    exists does the pin decide; only where both hold can the stage honestly read NOT_STARTED.

    The pin is probed only under an enabled switch: a deployment that does not run V2 generation
    pays no query for a fact that cannot change its answer.
    """
    if not generation_enabled():
        # This deployment does not run V2 generation, so `GENERATION_DISABLED` names the remedy
        # honestly: a deployment decision to reverse, not a schema to migrate.
        return {"stage": "GENERATE_PREVIEW", "state": "UNAVAILABLE",
                "reason_code": "GENERATION_DISABLED"}
    if not pin_exists(conn):
        return {"stage": "GENERATE_PREVIEW", "state": "UNAVAILABLE",
                "reason_code": PRE_PIN_REASON_CODE}
    return {"stage": "GENERATE_PREVIEW", "state": "NOT_STARTED", "reason_code": None}


def run_detail(conn, identity: IdentityEnvelope, run_id: str) -> dict | None:
    """One run, projected from the stores that already hold the evidence (spec §12).

    Returns None both when the run does not exist and when this caller may not see it — the route
    maps both to 404, so absence and denial are indistinguishable and a probe cannot enumerate
    other people's runs.

    Current state, not a fabricated timeline (spec §6.6): every field is read now, and nothing is
    reconstructed from what a row must once have been.

    `visibility_where`'s params bind AT THE SPLICE POINT, which here is SECOND — after the run id.
    The fragment is parenthesized on splice (its own invariant): today's single comparison binds
    tighter than the surrounding AND, but a fragment that grows an OR would silently widen this
    WHERE into "this run OR anything that clause matches" — a visibility hole, not a syntax error.
    """
    frag, params = visibility_where(identity)
    row = conn.execute(
        f"""SELECT fgr.generation_run_id, fgr.intent_id, ci.hypothesis,
                   fri.generation_run_id IS NOT NULL, fri.run_identity_hash,
                   fri.considered_revision_id, fri.metadata_snapshot_id,
                   COALESCE(fri.owner_subject, fgr.actor->>'subject'),
                   frp.display_name, frp.description
            FROM feature_generation_run fgr
            LEFT JOIN feature_run_identity fri USING (generation_run_id)
            LEFT JOIN feature_run_profile  frp USING (generation_run_id)
            LEFT JOIN contract_intent      ci  ON ci.intent_id = fgr.intent_id
            WHERE fgr.generation_run_id = %s AND ({frag})""",
        (run_id, *params)).fetchone()
    if row is None:
        return None
    (_, intent_id, hypothesis, has_identity, idh, ccr_id, snap_id,
     owner, display, description) = row
    choices = conn.execute(
        "SELECT option_id, considered_revision_id, chosen_at "
        "FROM contract_gate1_choice_revision WHERE generation_run_id = %s "
        "ORDER BY chosen_at", (run_id,)).fetchall()
    # Drafts reach the run through their CANDIDATE: `formula_draft` carries no run id, and the
    # considered revision is what ties one to this run (1090's subject is a candidate, parent
    # §0.1.4). The LEFT JOIN to the retirement is the eligibility axis — an absent row is the
    # honest "still current", not a missing value.
    drafts = conn.execute(
        """SELECT d.formula_draft_id, d.option_id, d.state, r.reason
           FROM formula_draft d
           JOIN contract_considered_revision ccr
             ON ccr.considered_revision_id = d.considered_revision_id
           LEFT JOIN formula_draft_retirement r USING (formula_draft_id)
           WHERE ccr.generation_run_id = %s ORDER BY d.formula_draft_id""",
        (run_id,)).fetchall()
    # Two axes, never one field (spec §6.7): `state`/`rail_state` is the immutable historical
    # outcome, `eligibility` is derived at read time. Rewriting a succeeded-then-retired draft to
    # BLOCKED would destroy the history; dropping the retirement would leave an unusable output
    # looking current.
    authoring = [{
        "formula_draft_id": fid, "option_id": opt, "state": state,
        "rail_state": RAIL_FROM_DRAFT_STATE[state],
        "eligibility": "withdrawn" if reason else "current",
        "retirement_reason": reason,
    } for fid, opt, state, reason in drafts]
    rail = [
        {"stage": "CHOOSE_CANDIDATES",
         "state": "SUCCEEDED" if choices else "NOT_STARTED", "reason_code": None},
        {"stage": "AUTHOR_FORMULA",
         "state": (min((d["rail_state"] for d in authoring), key=_AUTHOR_SEVERITY.index)
                   if authoring else "NOT_STARTED"),
         "reason_code": None},
        # Nothing can write a binding in the foundation, so this milestone has no evidence to read.
        {"stage": "BIND_SELECTIONS", "state": "NOT_STARTED", "reason_code": None},
        # One helper, one entry: state and reason are decided together, so they cannot disagree.
        _generate_preview_stage(conn),
        *({"stage": s, "state": "UNAVAILABLE", "reason_code": code} for s, code in _SOCKETS),
    ]
    return {
        "generation_run_id": run_id, "pre_spine": not has_identity, "owner_subject": owner,
        "display_name": display, "description": description,
        "intent": {"intent_id": intent_id, "hypothesis": hypothesis} if intent_id else None,
        "identity": ({"run_identity_hash": idh, "considered_revision_id": ccr_id,
                      "metadata_snapshot_id": snap_id} if has_identity else None),
        "milestones": {
            "choose_candidates": [{"option_id": o, "considered_revision_id": c,
                                   "chosen_at": t.isoformat()} for o, c, t in choices],
            "bind_selections": []},
        "authoring": authoring,
        "rail": rail,
    }
