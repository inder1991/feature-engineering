"""Run projections (spec §12): DERIVED from existing stores — the spine records no lifecycle."""
from __future__ import annotations

from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.materialize.action_authorization import ActionV1, action_available
from featuregen.materialize.generation_lane import generation_enabled
from featuregen.materialize.queue_lane import materialization_enabled
from featuregen.materialize.verification_lane import verification_enabled

# The governed refusals the retry derivation can report, IMPORTED rather than spelled: these are the
# substrate's closed vocabulary (SE-4's `REASON_FAMILIES` pins every one), and a copy of the string
# here is a copy that keeps its old spelling on the day the substrate changes its mind.
from featuregen.overlay.upload.semantic_eligibility_reasons import (
    COST_AUTHORIZATION_EXHAUSTED,
    FORMULA_DRAFT_NOT_AN_ANSWER,
    FORMULA_DRAFT_RETIRED,
    LEGACY_REGENERATION_NOT_APPROVED,
)
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

#: The two draft states 1107's money guard EXCLUDES, spelled the way the index spells them. A draft
#: in either one BOUGHT NOTHING — it holds no identity slot, and a governed retry (spec §R4.2) may
#: write another draft against the same identity. Every other state is an ANSWER or a purchase in
#: flight, and 1107 guarantees at most one of those per identity.
_BOUGHT_NOTHING = ("FAILED", "CANCELLED")

#: The TWO sockets that are still literal (spec §7): stages whose machinery genuinely does not
#: exist, each labelled with WHY. Not "not started" — that is reserved for stages that could
#: actually run.
#:
#: ▲ Three others used to live here and were WRONG BY THE TIME THEY WERE READ (spec §R4.3). A
#: stored availability label is a claim about machinery that keeps being built while the label
#: stays put: `EXECUTE_SANDBOX` still said `WORKER_NOT_IMPLEMENTED` after §9.0 shipped its lane,
#: and the production pair still said `STATE_MACHINE_NOT_BUILT` after 1113/1114 built both
#: machines. Availability is DERIVED (§7 [R3.1]), so those three now ASK — see `_sandbox_stage`
#: and `_production_stage`. These two remain literal because nothing exists to ask: there is no
#: sandbox publication worker and no training subsystem, and a derivation over absent machinery
#: would be a longer way of writing the same constant.
_STATIC_SOCKETS = {
    "PUBLISH_SANDBOX": "WORKER_NOT_IMPLEMENTED",
    "TRAIN_MODEL": "SUBSYSTEM_NOT_BUILT",
}

#: The deployment switch's own reason code, named ONCE. The rail reports it for `GENERATE_PREVIEW`
#: and the run→job trigger refuses with it, because they are the same fact seen from two sides: a
#: deployment with V2 generation off has no generation surface, so neither the stage nor the gesture
#: can honestly claim otherwise. Shared the way `runs.pin` shares the pre-pin code — a string copied
#: into a second file is a string that drifts.
GENERATION_DISABLED_REASON_CODE = "GENERATION_DISABLED"

#: The two ways the run→job gesture (spec §R4.4.2) can be unavailable for a reason of the RUN
#: rather than of the deployment. PROJECTION-LOCAL vocabulary, like `GENERATION_DISABLED` and
#: unlike a governed decision code: no registry row defines them, they name a gap in this surface's
#: record, and `prepare_code_availability` is the only thing that emits them (Task 1's review
#: settled that such literals need no registry entry). Constants rather than inline strings because
#: the route quotes them into its 409 and the tests assert them.
PRE_SPINE_NOT_ACTIONABLE = "PRE_SPINE_NOT_ACTIONABLE"
RUN_BUILD_DECLARATION_ABSENT = "RUN_BUILD_DECLARATION_ABSENT"

#: The three ways the RETRY gesture (spec §R4.2) can be unavailable for a reason that is not a
#: governed disposition. PROJECTION-LOCAL, on the same terms as the two codes above: no registry row
#: defines them, none of them duplicates a substrate refusal, and `retry_availability` is the only
#: thing that emits them. Every OTHER code in `retry_blockers` is the SUBSTRATE's own — asked of its
#: own locators and reported verbatim — because a second spelling of a governed refusal is exactly
#: how the page and the entrance start giving two answers to one question.
#:
#: A draft that bought something, or is buying it, already holds the identity a retry would mint
#: (1107's partial index). Re-requesting lands on that row and mints nothing, so a control offered
#: here would be a click that changes nothing.
RETRY_ATTEMPT_ALREADY_LIVE = "RETRY_ATTEMPT_ALREADY_LIVE"
#: The frozen record can no longer name this candidate, so the identity a retry would mint cannot be
#: computed at all. The substrate's own sentence rides the detail.
RETRY_CANDIDATE_UNRESOLVABLE = "RETRY_CANDIDATE_UNRESOLVABLE"
#: 1103's approval store and 1105's ceiling store have not landed in THIS database — `pin_exists`'s
#: rule applied to the governed-retry substrate, and not a hypothetical: the live ledger stands at
#: 1099, where reading either table would take the whole run page down with an `UndefinedTable`.
RETRY_SUBSTRATE_ABSENT = "RETRY_SUBSTRATE_ABSENT"

#: The sentence each blocker renders with. The CODE is the substrate's and the SENTENCE is this
#: surface's — the same division `prepare_code_availability` already draws, and the route quotes
#: both, so a person reading the greyed-out control and a person reading the 409 read one refusal.
#: A code absent from here still renders: the fallback names the class of refusal rather than
#: explaining a code this file does not own.
_RETRY_BLOCKER_DETAIL = {
    FORMULA_DRAFT_RETIRED:
        "somebody withdrew this candidate, and no approved regeneration NAMES that withdrawal. "
        "Withdrawal is a decision rather than a cost, so it refuses both authoring lanes: use the "
        "replacement, or have a governance approval bind the withdrawal before retrying",
    FORMULA_DRAFT_NOT_AN_ANSWER:
        "the previous attempt at this exact formula failed, and a recorded failure is not an "
        "answer this platform bought — re-authoring spends again, so it needs an approved, "
        "cost-confirmed regeneration exception",
    LEGACY_REGENERATION_NOT_APPROVED:
        "a legacy-identity draft already answers for this candidate, and re-authoring it under the "
        "corrected identity would buy the same answer a second time: it needs an approved "
        "regeneration",
    COST_AUTHORIZATION_EXHAUSTED:
        "the approved ceiling for this regeneration has no budget left, and spend is reserved per "
        "provider call — retrying now would stop at the dispatch seam. A fresh approval is the "
        "remedy, and it is a governance act",
    RETRY_SUBSTRATE_ABSENT:
        "this database has no regeneration-approval store and no spend ceiling store, so a "
        "governed retry cannot be recorded here at all. An operator applies the migrations; "
        "nothing a person does to this run can produce them",
}

#: For a strategy the resolver refused: the same sentence `POST .../formula-drafts` answers with,
#: because it is the same refusal reached from a second door.
_STRATEGY_REFUSAL_DETAIL = (
    "the authoring strategy could not be resolved for this candidate, so there is no attempt to "
    "buy again")

#: For anything else the substrate names. Deliberately does NOT explain the code: this surface does
#: not own it, and a sentence invented here could contradict the one that does.
_UNNAMED_BLOCKER_DETAIL = (
    "the authoring decision refuses this candidate by this name; nothing was recorded or spent")


def _static_socket(stage: str) -> dict:
    """One literal socket. The lookup RAISES on a stage that is not one — a rail entry silently
    built with `None` for its reason would read as "unavailable, and nobody will say why"."""
    return {"stage": stage, "state": "UNAVAILABLE", "reason_code": _STATIC_SOCKETS[stage]}


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
                "reason_code": GENERATION_DISABLED_REASON_CODE}
    if not pin_exists(conn):
        return {"stage": "GENERATE_PREVIEW", "state": "UNAVAILABLE",
                "reason_code": PRE_PIN_REASON_CODE}
    return {"stage": "GENERATE_PREVIEW", "state": "NOT_STARTED", "reason_code": None}


def _sandbox_stage() -> dict:
    """`EXECUTE_SANDBOX`'s availability, folded from BOTH switches its only entrance rides.

    That entrance is `POST /feature-execution/verifications`, and — exactly like `POST /build-sets`
    — it is shut two independent ways. The `feature_execution` router carries a dependency on
    `materialization_enabled`, which 404s EVERY path on that surface; and the §9.0 verification lane
    carries its own `verification_enabled`, the switch the worker tick reads before it will process
    a single request. Both default OFF, and they are separate flags, so a deployment can be in any
    of the four combinations.

    Takes no connection: nothing here is a fact about the DATA. Accepting one for symmetry with
    `_generate_preview_stage` would tell every reader a query happens that does not.

    PRECEDENCE is the entrance's own, and carries the same meaning it carries for the preview stage:
    the SURFACE switch is answered first, because a deployment with materialization off does not
    have this surface at all — naming the lane switch there would send a person to flip
    `FEATUREGEN_VERIFICATION_V2_ENABLED` and watch the POST go on answering 404. Only where the
    surface exists does the lane switch decide.

    ▲ **`WORKER_NOT_IMPLEMENTED` was the false label this replaces** (spec §R4.3): the worker EXISTS.
    And NOT_STARTED is honest under both switches even though this deployment configures no
    execution substrate (`verification_lane._EXECUTOR is None`). That absence is not unavailability
    — the entrance accepts the request, the worker claims it, and the attempt ends FAILED with the
    posture named. §7 [R3.1] forbids NOT_STARTED over an entrance that REFUSES; an entrance that
    accepts and then fails visibly is a stage that ran, and reporting its outcome is the attempt's
    affair, not the rail's. The substrate is also not a fact this projection MAY read: `_EXECUTOR`
    is private, mutable module state with no accessor, and a rail that reached into it would move
    with whatever the last test assigned.
    """
    if not materialization_enabled():
        return {"stage": "EXECUTE_SANDBOX", "state": "UNAVAILABLE",
                "reason_code": "MATERIALIZATION_DISABLED"}
    if not verification_enabled():
        return {"stage": "EXECUTE_SANDBOX", "state": "UNAVAILABLE",
                "reason_code": "VERIFICATION_DISABLED"}
    return {"stage": "EXECUTE_SANDBOX", "state": "NOT_STARTED", "reason_code": None}


def _production_stage(action: ActionV1) -> dict:
    """One production act's rail entry, ASKED of the one policy that answers for it.

    `action_available` is the single definition of whether the development policy can authorize an
    act (§0.1.0), and `authorize_action` raises `ActionUnavailable` off the same answer — so the
    rail and the act refuse together by construction rather than by two people remembering to edit
    one string each. The stage name IS the action's name; the `ActionV1` member is passed rather
    than a string so a stage this module invents cannot reach the policy.

    ▲ `STATE_MACHINE_NOT_BUILT` was the false label this replaces (spec §R4.3): 1113 and 1114 built
    both machines. The truthful reason is that no AUTHORIZED PATH exists — `ACTION_UNAVAILABLE`,
    the code the production routes already answer with, so a person reading the rail and a person
    reading the 409 are reading one refusal. And it is asked rather than stored precisely because
    §21.0's production governance will move it: on that day the rail moves with it, unedited.
    """
    if not action_available(action):
        return {"stage": str(action), "state": "UNAVAILABLE", "reason_code": "ACTION_UNAVAILABLE"}
    return {"stage": str(action), "state": "NOT_STARTED", "reason_code": None}


def _bind_selections_milestone(conn, run_id: str,
                               chosen: set[tuple[str, str]]) -> tuple[dict, list[dict]]:
    """The `BIND_SELECTIONS` milestone — its rail entry AND its evidence — from 1101's pins.

    A binding pins one SELECTION to the formula that will be built for it, and it reaches this run
    through the choice it pins: binding → selection → considered revision → run. The hop through
    `feature_selection_revision` is deliberate even though the binding carries a
    `considered_revision_id` of its own: 1101's composite foreign keys force the two to agree, so
    the join costs nothing and reads as "whose choice is this" rather than "which revision did the
    pin copy".

    ▲ THE QUESTION IS MEMBERSHIP, NOT SIZE. `chosen` is the SET of candidates this run's gate-1
    choices name, and the numerator is what the bound candidates and that set have IN COMMON. Two
    sets of the same size are not the same set: comparing counts alone read `SUCCEEDED "2 of 2
    bound"` over a run with two choices and two bindings on entirely different candidates —
    complete, with neither chosen candidate holding a formula. A selection may legitimately exist
    for a candidate no choice covers, so those rows are real and stay in the evidence below; what
    they may not do is count towards choices they do not touch.

    ▲ THE DENOMINATOR IS THE CHOICE SET, and it is the only honest one available. Nothing freezes
    "the selections this run intends to bind" — a `feature_selection_revision` row is written when
    somebody selects, so counting selections would grow the denominator with the numerator and the
    milestone would read complete at every moment of a half-done job. Choices are decided earlier,
    in a store that stands still, so they are what progress is measured against.

    THE STATES, in the order they earn it:

      * no pins at all — NOT_STARTED, and no detail: the state already says it, and "0 of 5 bound"
        would only repeat the choices milestone rendered beside it;
      * pins, but no choices on the record — IN_PROGRESS with the COUNT ALONE. That is the world the
        platform is actually in (`contract_gate1_choice_revision` holds zero live rows). A
        denominator of nothing is not a denominator, and with nothing to be equal to the stage
        cannot claim to be finished;
      * every choice bound — SUCCEEDED. Reachable only through the intersection, so it means each
        chosen candidate has a formula and not merely that some arithmetic matched. The empty run
        cannot arrive here: with no pins the first branch already answered;
      * otherwise — IN_PROGRESS, and the detail carries both numbers. This is where a partially
        bound run sits, and where a run whose pins are all on unchosen candidates sits at `0 of 2`:
        the stage has been worked on, just not on anything a person chose. Never SUCCEEDED — a
        stage reported done while three choices carry no formula sends a person to the next
        entrance, which is the one that refuses.

    PRE-1101 THE MILESTONE CANNOT RUN AT ALL. There is no store a binding could live in, so the
    entry is UNAVAILABLE with the pin's own reason code — the same string `GENERATE_PREVIEW`
    answers with, because it is the same absent table seen from a second surface. NOT_STARTED would
    be the false rail §7 [R3.1] forbids: nothing a person did could ever move it. The probe is also
    what keeps this read from RAISING: the count query below would throw `UndefinedTable` out of a
    read-only projection on a database where 1101 has not landed, and `pin.py` exists precisely
    because such databases are real.

    The pin is probed here rather than shared with `_generate_preview_stage`: that stage answers
    the switch first and pays no query at all on a deployment that does not run V2 generation, and
    threading one eager boolean through both would buy a `to_regclass` lookup at the cost of that.
    """
    if not pin_exists(conn):
        return ({"stage": "BIND_SELECTIONS", "state": "UNAVAILABLE",
                 "reason_code": PRE_PIN_REASON_CODE, "detail": None}, [])
    # ORDER BY `recorded_at` then `binding_id`: bindings written in one transaction share `now()`,
    # so the timestamp alone is not a key and the id is what makes the order total. The id is a
    # content hash, so that tail order carries no meaning — it is stability, not chronology.
    rows = conn.execute(
        """SELECT b.binding_id, b.selection_revision_id, b.formula_draft_id,
                  b.considered_revision_id, b.option_id, b.recorded_at
           FROM selection_formula_binding b
           JOIN feature_selection_revision s ON s.revision_id = b.selection_revision_id
           JOIN contract_considered_revision ccr
             ON ccr.considered_revision_id = s.considered_revision_id
           WHERE ccr.generation_run_id = %s
           ORDER BY b.recorded_at, b.binding_id""", (run_id,)).fetchall()
    bindings = [{"binding_id": bid, "selection_revision_id": sel, "formula_draft_id": draft,
                 "considered_revision_id": ccr_of, "option_id": opt,
                 "recorded_at": at.isoformat()}
                for bid, sel, draft, ccr_of, opt, at in rows]
    # ▲ COUNTED IN CANDIDATES, NOT IN ROWS, because the choices are: 1025's are unique per
    # `(run, considered revision, option)`. 1101's uniqueness is `(selection, draft)`, so one
    # selection pinned to two drafts — two build sets declared either side of a re-authoring — is
    # two rows for ONE choice, and counting rows would report two of five choices bound with four
    # still carrying nothing. The list above keeps both rows; only the numerator folds them.
    bound_candidates = {(b["considered_revision_id"], b["option_id"]) for b in bindings}
    # THE INTERSECTION IS THE NUMERATOR — the choices that actually carry a formula. It can never
    # exceed the denominator, which is why no "more bound than chosen" case appears below: a pin on
    # a candidate nobody chose is simply not one of these choices being bound.
    bound = len(bound_candidates & chosen) if chosen else len(bound_candidates)
    if not bindings:
        state, detail = "NOT_STARTED", None
    elif not chosen:
        state, detail = "IN_PROGRESS", f"{bound} bound"
    elif bound == len(chosen):
        state, detail = "SUCCEEDED", f"{bound} of {len(chosen)} bound"
    else:
        state, detail = "IN_PROGRESS", f"{bound} of {len(chosen)} bound — accumulating"
    # `detail` rides THIS entry only, and every branch of it. A stage with a count to state says the
    # count; the eight others have nothing to put there, and a null on each of them would be a field
    # every client must test for and no stage would ever fill.
    return ({"stage": "BIND_SELECTIONS", "state": state, "reason_code": None,
             "detail": detail}, bindings)


def jobs_store_exists(conn) -> bool:
    """Whether migration 1111's `code_generation_job` has landed in THIS database.

    The `pin_exists` rule, applied to the second file-only store this projection now reads. It is
    not paranoia: 1111 is committed but unapplied on the live database (the ledger stands at 1099),
    so a join written without this probe would turn EVERY run detail into an `UndefinedTable` 500
    the moment it deployed — a read-only projection taken down by a table nobody had applied yet.

    Self-retiring, like the pin: the day 1111 applies, `to_regclass` returns the relation and this
    answers True for ever after with no code change.
    """
    return conn.execute("SELECT to_regclass('code_generation_job')").fetchone()[0] is not None


def run_jobs(conn, run_id: str) -> list[dict]:
    """The code-generation jobs bridged to this run, newest first — a READ-ONLY join (spec §R4.4.2).

    THE BRIDGE IS THE CONSIDERED REVISION. A job names the frozen considered set it builds from
    (1111's FK), and that revision names the run (1021's), so "this run's jobs" is a join and never
    a stored back-reference — the same derivation discipline every other field here follows.

    ▲ **NOT a single `requested_action`.** 1111 rejected that shape in writing: "a single
    `requested_action` with a single authorization could not truthfully cover AUTHOR_FORMULA and
    GENERATE_PREVIEW". One job performs SEVERAL acts and records one row per act, so the acts are
    surfaced as the list they are, each with the state the store holds for it. Collapsing them to
    one string here would re-invent exactly what the migration refused.

    STATUS IS THE STORE'S WORD, verbatim — this projection folds nothing and translates nothing.
    The empty list is not two answers dressed as one: on a pre-1111 database no job HAS ever been
    requested for this run, because there is nowhere one could have been recorded, so "none" is
    true either way. What the reader loses is only the MIGRATION's name, and the rail beside it
    carries that already: 1111 cannot be present where 1101 is absent (the runner applies pending
    migrations in order), so `GENERATE_PREVIEW` and `BIND_SELECTIONS` are both reading
    `BUILD_SET_DECLARATION_WITHHELD_PRE_PIN` on exactly those databases.
    """
    if not jobs_store_exists(conn):
        return []
    # ORDER BY `requested_at` then `job_id`: jobs written in one transaction share `now()`, so the
    # timestamp alone is not an order and the id is what makes it total. The column is a real
    # `timestamptz` here (1111), unlike 1090's TEXT — so this is a chronological sort, not a
    # lexical one that happens to agree.
    rows = conn.execute(
        """SELECT j.job_id, j.status, j.requested_at
           FROM code_generation_job j
           JOIN contract_considered_revision ccr USING (considered_revision_id)
           WHERE ccr.generation_run_id = %s
           ORDER BY j.requested_at DESC, j.job_id DESC""", (run_id,)).fetchall()
    if not rows:
        return []
    # ONE query for every job's acts rather than one per job: the list is unbounded (a run
    # accumulates jobs), and a per-row query here would be an N+1 inside a page render.
    actions: dict[str, list[dict]] = {job_id: [] for job_id, _, _ in rows}
    for job_id, action, state in conn.execute(
            "SELECT job_id, action, state FROM code_generation_job_action "
            "WHERE job_id = ANY(%s) ORDER BY job_id, action",
            ([job_id for job_id, _, _ in rows],)).fetchall():
        actions[job_id].append({"action": action, "state": state})
    return [{"job_id": job_id, "status": status, "requested_at": at.isoformat(),
             "actions": actions[job_id]} for job_id, status, at in rows]


def prepare_code_availability(*, pre_spine: bool, has_job: bool) -> dict:
    """Whether this run's `prepare-code` gesture can run, and — when it cannot — the ONE sentence
    both the affordance and the entrance say (spec §7 [R3.1], applied to a gesture).

    ▲ **THE SAME RULE THE RAIL FOLLOWS, FOR THE SAME REASON.** §7 forbids a stage reading
    NOT_STARTED over an entrance that refuses; a BUTTON offered over an entrance that refuses is
    that defect with a click in it. So availability is derived ONCE, here, from facts the projection
    already holds — the run page disables the control off this answer and the route raises its 409
    off this answer, which is why the two can never drift into "the page offered it and the server
    said no" (or the worse direction: a control greyed out over a gesture that would have worked).

    PRECEDENCE is the rail's own, and each step is a different person's remedy:

      * the deployment switch first — a deployment with V2 generation off has no journey AT ALL, and
        naming a fact about the run there would send someone to fix something that changes nothing;
      * then PRE_SPINE — no identity row means no frozen considered set, and nothing a person does
        to this run can produce one;
      * then the declaration — see `api.routes.feature_runs.prepare_run_code`: the five declarations
        a build freezes are a person's to make, and this platform will not make them for them.

    Takes no connection: every input is a fact the caller already read. Accepting one would tell
    each reader a query happens that does not.
    """
    if not generation_enabled():
        return {"available": False, "reason_code": GENERATION_DISABLED_REASON_CODE,
                "detail": "this deployment does not run V2 generation, so there is no "
                          "code-generation journey to start — a deployment decision to reverse, "
                          "not a fault of this run"}
    if pre_spine:
        return {"available": False, "reason_code": PRE_SPINE_NOT_ACTIONABLE,
                "detail": "this run predates the identity spine, so no frozen considered set was "
                          "recorded for it and there is nothing a build could be a build OF. An "
                          "honest gap in the record, never a failure of the run: start a new run "
                          "to prepare code for these candidates"}
    if not has_job:
        return {"available": False, "reason_code": RUN_BUILD_DECLARATION_ABSENT,
                "detail": "no code-generation job has ever been requested for this run, so the "
                          "five declarations a build freezes — the population, the cadence, the "
                          "availability promise, the operand facts and the policy realizations — "
                          "have never been made for it. Each decides a published number and the "
                          "platform will not choose them on your behalf: declare them once "
                          "through POST /code-generation-jobs, and this gesture then continues "
                          "that build"}
    return {"available": True, "reason_code": None, "detail": None}


def _blocker(code: str, detail: str | None = None) -> dict:
    """One refusal, in the shape both surfaces render: the substrate's code and a sentence."""
    return {"code": code,
            "detail": detail if detail is not None
            else _RETRY_BLOCKER_DETAIL.get(code, _UNNAMED_BLOCKER_DETAIL)}


def retry_substrate_exists(conn) -> bool:
    """Whether 1103's approval store and 1105's ceiling store have landed in THIS database.

    `pin_exists`'s rule, applied to the governed-retry substrate — and for its exact reason. The
    derivation below reads `formula_draft_regeneration_exception`, `formula_draft_retirement_
    tombstone` and the spend ledger; on a database at 1099 none of them exists, and a projection
    that read them would turn every run detail carrying one failed draft into an `UndefinedTable`
    500. The runner applies pending migrations IN ORDER, so 1105 present implies 1103 present;
    both are probed anyway, because "implies" is a fact about the runner and this is a fact about
    the database being served.

    Self-retiring, like the pin: the day the migrations apply, `to_regclass` answers and this is
    True for ever after with no code change.
    """
    row = conn.execute(
        "SELECT to_regclass('formula_draft_regeneration_exception'), "
        "       to_regclass('llm_spend_authorization_revision')").fetchone()
    return row[0] is not None and row[1] is not None


def retry_availability(conn, *, considered_revision_id: str, option_id: str) -> dict:
    """Whether a bought-nothing attempt on this candidate may be bought AGAIN, and why not
    (spec §R4.2). Returns ``{"retryable": bool, "retry_blockers": [{code, detail}]}``.

    ▲ **THIS PROJECTION RE-DERIVES NOTHING.** Every verdict below is ASKED of the substrate's own
    locators — `candidate_governance_blockers` (the ONE composition the plan preview and the
    AUTHOR_FORMULA decision both consult), `covering_tombstones`, `valid_exception_for`,
    `remaining_spend` — and reported. A parallel judgement of "is this coupon valid" would be a
    second answer to a question the store already answers, and the two would disagree on the day
    one of them changed.
    """
    if not retry_substrate_exists(conn):
        return {"retryable": False, "retry_blockers": [_blocker(RETRY_SUBSTRATE_ABSENT)]}

    from featuregen.overlay.upload.formula_draft_service import (
        CandidateUnavailable,
        candidate_governance_blockers,
        current_authoring_config,
        frozen_candidate,
    )
    from featuregen.overlay.upload.formula_draft_store import formula_identity
    from featuregen.overlay.upload.formula_strategy import resolve_formula_strategy
    from featuregen.overlay.upload.formula_strategy_facts import assemble_strategy_facts
    from featuregen.overlay.upload.llm_spend import remaining_spend
    from featuregen.overlay.upload.retirement_scope import (
        approved_ceiling_for,
        covering_tombstones,
        retirement_scope_key,
        valid_exception_for,
    )

    # ▲ THE IDENTITY A RETRY WOULD MINT IS TODAY'S, not the failed row's — the candidate's frozen
    # facts under the CURRENT strategy resolution and the CURRENT provider contract, which is what
    # `request_draft` will check and what `approve_regeneration_for_draft` binds an approval to.
    # Reading the old row's `authoring_config_hash` instead would answer about an identity nobody
    # is about to ask for: move the model and the retry is a different question, admitted free.
    try:
        candidate = frozen_candidate(conn, considered_revision_id, option_id)
    except CandidateUnavailable as exc:
        # The one refusal a read-only projection MUST absorb: legacy revisions cannot name their
        # options, and a run page that 500s on one of them shows nothing at all.
        return {"retryable": False,
                "retry_blockers": [_blocker(RETRY_CANDIDATE_UNRESOLVABLE, exc.detail)]}

    assembled = assemble_strategy_facts(
        conn, considered_revision_id=candidate.considered_revision_id, option_id=option_id,
        idea=candidate.idea, catalog_snapshot_hash=candidate.catalog_snapshot_hash)
    decision = resolve_formula_strategy(assembled.facts)
    if decision.blockers:
        # A conceptual pattern, a governed model output, or a genuine bind failure — the service
        # raises on each of these before any draft exists, so the retry cannot be offered and the
        # resolver's own codes say which.
        return {"retryable": False,
                "retry_blockers": [_blocker(code, _STRATEGY_REFUSAL_DETAIL)
                                   for code in decision.blockers]}

    # ▲ THE LANE DISCRIMINATOR IS THE STORE'S OWN: a reviewed draft folds no provider contract
    # (`current_authoring_config`), calls no provider and spends nothing, and `_request_draft_locked`
    # branches on exactly this value. Owner ruling 2026-08-23 (Option 2): deterministic retries are
    # FREE BY CONSTRUCTION, so the gate that guards a re-spend does not apply to them. Tombstones
    # still refuse both lanes — withdrawal is a decision, not a cost.
    provider_contract, _config_payload, config_hash = current_authoring_config(decision)
    scope_key = retirement_scope_key(
        considered_revision_id=candidate.considered_revision_id, option_id=option_id,
        planning_request_hash=candidate.planning_request_hash,
        catalog_snapshot_hash=candidate.catalog_snapshot_hash,
        definition_revision=candidate.definition_revision)
    identity = formula_identity(
        considered_revision_id=candidate.considered_revision_id, option_id=option_id,
        planning_request_hash=candidate.planning_request_hash,
        catalog_snapshot_hash=candidate.catalog_snapshot_hash,
        authoring_config_hash=config_hash,
        definition_revision=candidate.definition_revision)

    # THE ONE LAW's read (Task 6 round-4), borrowed whole rather than re-implemented: RETIRED iff
    # some covering withdrawal lacks a valid naming coupon, and a warning where every one is named.
    # The run page, the plan preview and the store therefore give ONE answer by construction.
    blockers = [_blocker(code) for code in candidate_governance_blockers(
        conn, candidate=candidate, option_id=option_id, strategy=decision.strategy,
        strategy_identity_hash=decision.strategy_identity_hash,
        provider_contract_hash=provider_contract, config_hash=config_hash,
        scope_key=scope_key)[0]]

    now = conn.execute("SELECT now()").fetchone()[0]
    # The 1107 index's own predicate, spelled from the constant that already states it. A FAILED or
    # CANCELLED row is history and holds nothing; anything else is an answer or a purchase in
    # flight, and the INSERT would land on it.
    live = conn.execute(
        "SELECT formula_draft_id FROM formula_draft "
        " WHERE formula_identity_hash = %s AND NOT (state = ANY(%s)) LIMIT 1",
        (identity, list(_BOUGHT_NOTHING))).fetchone()

    covering = covering_tombstones(conn, scope_key=scope_key, formula_identity_hash=identity)
    if provider_contract is not None and not covering and live is None:
        # `_request_draft_locked`'s not-an-answer gate, asked in the same order it asks it: with
        # nothing covering and a recorded failure at THIS identity, the LLM lane needs the plain
        # retry coupon. Where a withdrawal DOES cover, the coupons that name it are the approval,
        # and the block above has already reported whether they exist.
        bought_nothing = conn.execute(
            "SELECT formula_draft_id FROM formula_draft "
            " WHERE formula_identity_hash = %s AND state = ANY(%s) LIMIT 1",
            (identity, list(_BOUGHT_NOTHING))).fetchone()
        if bought_nothing is not None and valid_exception_for(
                conn, target_formula_identity_hash=identity,
                provider_contract_hash=provider_contract,
                strategy_identity_hash=decision.strategy_identity_hash,
                covering_tombstone_id=None, now=now) is None:
            blockers.append(_blocker(FORMULA_DRAFT_NOT_AN_ANSWER))
    if live is not None:
        blockers.append(_blocker(
            RETRY_ATTEMPT_ALREADY_LIVE,
            f"draft {live[0]} already holds this formula identity — it is an answer, or a purchase "
            f"still in flight — so a retry would mint nothing (migration 1107's money guard covers "
            f"exactly these). Watch that attempt; retry becomes the question again if it fails"))
    if provider_contract is not None:
        # ▲ THE CEILING THE RETRY WOULD ACTUALLY RIDE, read with the ONE locator every consumer
        # shares (`approved_ceiling_for` — the service's preference read and the store's
        # dead-ticket guard call the same function, so the page cannot report a ceiling the mint
        # does not use). The BAR mirrors the guard's exactly: one per-call worst-case reservation
        # (the dispatch seam's own arithmetic), not zero — a zero floor here would offer a Retry
        # the seam refuses one click later. The coupling test
        # (`test_THE_PAGE_AND_THE_SEAM_PICK_THE_SAME_CEILING_OUT_OF_TWO`) is belt-and-braces now
        # that both sides call one function; the sliver test pins the bar.
        ride = approved_ceiling_for(
            conn, target_formula_identity_hash=identity,
            provider_contract_hash=provider_contract,
            strategy_identity_hash=decision.strategy_identity_hash)
        if ride is not None:
            from decimal import Decimal

            left = remaining_spend(conn, ride, now=now)
            max_calls, max_tokens, max_cost = conn.execute(
                "SELECT max_calls, max_tokens, max_cost FROM llm_spend_authorization_revision "
                "WHERE spend_authorization_id = %s", (ride,)).fetchone()
            call_tokens = -(-int(max_tokens) // int(max_calls))
            call_cost = Decimal(str(max_cost)) / int(max_calls)
            if left.calls < 1 or left.tokens < call_tokens or left.cost < call_cost:
                blockers.append(_blocker(COST_AUTHORIZATION_EXHAUSTED))
    return {"retryable": not blockers, "retry_blockers": blockers}


def _current_by_candidate(history: list[dict]) -> list[dict]:
    """The CURRENT answer per candidate, folded from the attempt history (spec §R4.4.1).

    THE PREMISE. Migration 1107 narrowed the money guard to `state NOT IN ('FAILED','CANCELLED')`,
    so one formula identity can now carry many drafts: a failure bought nothing and must not hold
    the slot for ever. "What happened to this candidate" and "where does this candidate stand" were
    the same question while one identity meant one row; since 1107 they are two, and answering the
    second with the first reports a run as broken while its actual answer is a formula.

    THE CANDIDATE, not the identity, is the grouping key — `(considered_revision_id, option_id)`,
    1090's own subject (parent §0.1.4). An identity also folds the planning request, the snapshot,
    the authoring configuration and the definition revision, so ONE candidate can legitimately hold
    several identities: edit the definition and the next draft is a different identity for the same
    candidate. A person reads the candidate they chose, so that is what is folded.

    THE RULE, in the order the states earn it:

      * an ANSWER (or a purchase in flight) is the current reading — READY, BLOCKED, or any live
        state. 1107 guarantees at most one per IDENTITY; per candidate the latest one wins, because
        a second identity for the same candidate is a later question about it;
      * otherwise the most recent attempt stands, flagged ``resolved=False``. Nothing was bought,
        and that is exactly what a person needs to see: an empty current reading here would fold
        the rail to NOT_STARTED and tell them the platform never tried.

    ``resolved`` rides the CURRENT rows only. On a history row it would invite the reader to ask of
    a superseded attempt a question only the latest one answers.

    Never raises: this is a read-only projection, and a candidate that somehow held two live drafts
    must still render. `history` arrives in requested order, so "latest" is simply "last seen".
    """
    current: dict[tuple[str, str], dict] = {}
    for row in history:
        key = (row["considered_revision_id"], row["option_id"])
        resolved = row["state"] not in _BOUGHT_NOTHING
        held = current.get(key)
        # A copy, never the history row itself: `resolved` must not leak back into the other
        # reading, and the two lists are serialized independently.
        if held is None or resolved or not held["resolved"]:
            current[key] = {**row, "resolved": resolved}
    # Insertion order — candidates in the order their first attempt was requested. Deterministic,
    # and it matches the order the same candidates appear in the history table above it.
    return list(current.values())


def run_detail(conn, identity: IdentityEnvelope, run_id: str) -> dict | None:
    """One run, projected from the stores that already hold the evidence (spec §12).

    Returns None both when the run does not exist and when this caller may not see it — the route
    maps both to 404, so absence and denial are indistinguishable and a probe cannot enumerate
    other people's runs.

    Current state, not a fabricated timeline (spec §6.6): every field is read now, and nothing is
    reconstructed from what a row must once have been.

    `authoring` is TWO READINGS of the same rows (spec §R4.4.1) — `{"current": [...],
    "history": [...]}` — because since migration 1107 a candidate can hold both a failure and the
    answer that replaced it. See `_current_by_candidate` for which row is which and why.

    `jobs` is the code-generation journeys bridged to this run (spec §R4.4.2) — see `run_jobs`.

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
    #
    # ORDERED BY `requested_at`, which is the ATTEMPT order — the order a person lived through and
    # the order `_current_by_candidate` folds. It was `formula_draft_id` while one identity meant
    # one row and the order carried no meaning; since 1107 it does. The column is TEXT in 1090, so
    # this is a lexical sort, and it is chronological because every writer stores one spelling of
    # the database's own clock (`str(SELECT now())`) — a cast to timestamptz would be the wrong
    # kind of strict here, raising out of a read-only projection on one malformed legacy value.
    # The draft id tie-breaks, so the order is total and stable rather than the planner's whim.
    drafts = conn.execute(
        """SELECT d.formula_draft_id, d.considered_revision_id, d.option_id, d.state, r.reason
           FROM formula_draft d
           JOIN contract_considered_revision ccr
             ON ccr.considered_revision_id = d.considered_revision_id
           LEFT JOIN formula_draft_retirement r USING (formula_draft_id)
           WHERE ccr.generation_run_id = %s
           ORDER BY d.requested_at, d.formula_draft_id""",
        (run_id,)).fetchall()
    # Two axes, never one field (spec §6.7): `state`/`rail_state` is the immutable historical
    # outcome, `eligibility` is derived at read time. Rewriting a succeeded-then-retired draft to
    # BLOCKED would destroy the history; dropping the retirement would leave an unusable output
    # looking current.
    #
    # BOTH HALVES OF THE CANDIDATE KEY ride each row. The option id alone is not a candidate — it is
    # only meaningful inside the revision that froze it (1090's own words), and a run can hold
    # several considered revisions, so two rows reading `o1` may be two different candidates with
    # two different current answers.
    #
    # `retryable` / `retry_blockers` ride EVERY row and are derived only where the question exists
    # (spec §R4.2). The question is asked of an attempt that BOUGHT NOTHING — 1107's own line, and
    # the same constant the money guard's index is spelled from. A READY or BLOCKED draft is an
    # ANSWER and a working draft is a purchase in flight: both hold the identity slot, so "retry"
    # there is R4.2's still-deferred "request another opinion", and those rows carry `false` with NO
    # blockers — the absence of the question rather than a refusal nobody made.
    #
    # ASKED ONCE PER CANDIDATE, because the answer is about the candidate: the identity a retry
    # would mint is today's, so two failed attempts of one candidate have one answer between them,
    # and a per-row derivation would pay for it twice and could report it two ways.
    retry: dict[tuple[str, str], dict] = {}
    history = []
    for fid, ccr_of, opt, state, reason in drafts:
        if state not in _BOUGHT_NOTHING:
            answer = {"retryable": False, "retry_blockers": []}
        else:
            if (ccr_of, opt) not in retry:
                retry[(ccr_of, opt)] = retry_availability(
                    conn, considered_revision_id=ccr_of, option_id=opt)
            answer = retry[(ccr_of, opt)]
        history.append({
            "formula_draft_id": fid, "considered_revision_id": ccr_of, "option_id": opt,
            "state": state, "rail_state": RAIL_FROM_DRAFT_STATE[state],
            "eligibility": "withdrawn" if reason else "current",
            "retirement_reason": reason, **answer,
        })
    current = _current_by_candidate(history)
    # ONE derivation, two surfaces: the rail entry and the milestone's own rows are the same
    # reading of the same pins, so the count on the rail and the list under it cannot disagree.
    #
    # The CANDIDATES chosen, not how many: what the milestone asks is whether the things a person
    # chose have formulas, and a count cannot answer that — two sets of the same size can be
    # disjoint. 1025 keys a choice by `(run, considered revision, option)`, so the pair is the
    # candidate and the set is duplicate-free by construction.
    bind_stage, bindings = _bind_selections_milestone(
        conn, run_id, {(ccr_of, opt) for opt, ccr_of, _ in choices})
    # Read ONCE and used twice: the list a person watches and the fact that decides whether the
    # gesture beside it can run. Two reads could disagree inside one response.
    jobs = run_jobs(conn, run_id)
    rail = [
        {"stage": "CHOOSE_CANDIDATES",
         "state": "SUCCEEDED" if choices else "NOT_STARTED", "reason_code": None},
        # Worst-of over the CURRENT answers, never over the history (spec §R4.4.1). Folding every
        # attempt lets a failure that has already been retried past shadow the formula that
        # replaced it — the rail would report a broken stage over a candidate that is done.
        {"stage": "AUTHOR_FORMULA",
         "state": (min((d["rail_state"] for d in current), key=_AUTHOR_SEVERITY.index)
                   if current else "NOT_STARTED"),
         "reason_code": None},
        bind_stage,
        # One helper, one entry: state and reason are decided together, so they cannot disagree.
        _generate_preview_stage(conn),
        # The tail is written out rather than splatted from a table because it is no longer one
        # kind of thing: two entries derive, two are literal, and the ORDER is the journey's — the
        # UI renders the rail in the order it is given, so a table that grouped the derived ones
        # together would reorder the stages a person reads.
        _sandbox_stage(),
        _static_socket("PUBLISH_SANDBOX"),
        _production_stage(ActionV1.MATERIALIZE_PRODUCTION),
        _production_stage(ActionV1.PUBLISH_PRODUCTION),
        _static_socket("TRAIN_MODEL"),
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
            # The pins themselves, so a person can see WHICH choices carry a formula rather than
            # only how many do. Empty both when nothing is bound and when 1101 has not landed —
            # the rail entry beside it is what tells those two apart.
            "bind_selections": bindings},
        # TWO READINGS, both stated (spec §R4.4.1): where each candidate stands now, and every
        # attempt that got it there. A single list cannot be both — since 1107 a candidate can have
        # a failure AND an answer, and which one a reader wants depends on the question they asked.
        "authoring": {"current": current, "history": history},
        # The code-generation jobs this run's candidates are being built through (spec §R4.4.2).
        # Derived by the considered-revision bridge like everything else here — the spine stores no
        # link of its own, and the job store's status is the authority the trigger route just adds
        # rows to.
        "jobs": jobs,
        # Whether the gesture that adds to that list can run, and the sentence if it cannot — the
        # SAME answer its entrance refuses with. A button offered over an entrance that refuses is
        # §7 [R3.1]'s false rail with a click in it.
        "prepare_code": prepare_code_availability(
            pre_spine=not has_identity, has_job=bool(jobs)),
        "rail": rail,
    }
