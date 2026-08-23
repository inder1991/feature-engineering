"""Run projections (spec §12): DERIVED from existing stores — the spine records no lifecycle."""
from __future__ import annotations

from featuregen.contracts.envelopes import IdentityEnvelope
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
            WHERE {frag} {cursor_sql}
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
