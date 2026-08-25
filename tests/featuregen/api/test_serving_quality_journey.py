"""T10 — THE CLOSING JOURNEY. The owner's AML story, replayed against the finished system.

This is the last artifact of the 2026-08-24 serving-quality remediation, and it exists to answer
one question end to end: *what happens now when the person who filed the 135-candidate quality
audit repeats what they did?* Every other test in this program pins one seam. This one walks the
story through the HTTP routes in the order a person walks it — a brief aimed at the wrong catalog,
a broaden, a re-aim, a target proposal, and a window the catalog cannot cover — and asserts what
the platform says at each step.

The program's one law is the thing under test: **the platform must never present confidence it
does not have.** A card with unbound required operands, a ``DESIGN-CHECKED`` badge over a
``FORMULA_BLOCKED`` recipe, a proxy target without a proxy label, and a schema that accepts what
the parser refuses were all the same defect, and the journey below is the shape of their absence.

Two sections are deliberately NOT celebrations:

* ``test_a_window_the_source_cannot_cover_serves_nothing_and_that_is_AS_DESIGNED`` pins the
  program's one knowingly **served-less** case — the C9 reverse — as DESIGNED, not as a win.
* ``test_KNOWN_OPEN_...`` pins an open DEFECT: an account-anchored counterparty recipe still binds
  its counterparty to another entity's master key and serves a card. It is written so that the day
  the grain law lands, this test FAILS and gets inverted. A pin that demands its own future
  inversion is the honest shape for a defect nobody has fixed yet.

No production code is touched by this module. It only reads.
"""
from __future__ import annotations

from datetime import UTC, datetime

from tests.featuregen.api._helpers import AUTH, DEPOSITS_CSV, upload_csv

from featuregen.intake.llm import FakeLLM, FakeResponse
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.enrich import content_hash
from featuregen.overlay.upload.graph import build_graph
from featuregen.overlay.upload.recipe_planning_lens import v2_applicability
from featuregen.overlay.upload.recipe_registry_v2 import V2_RECIPES
from featuregen.overlay.upload.taxonomy.applicability import ConfirmedScope

# ── the audit's own arrangement, by name ────────────────────────────────────────────────────────
CIB = "cib"                    # the customer master the live AML run actually named
FTR = "ftr"                    # the transaction catalog that sat unplanned beside it
AML = "aml_cft.suspicious_transaction_monitoring"
HYPOTHESIS = "customers structure cash deposits below the reporting threshold over 90 days"
OBJECTIVE = "flag suspicious transaction behaviour"


def _fake() -> FakeLLM:
    """The generation path's two governed calls, scripted. The journey is about what the ENGINE
    serves, so the intent stream is deliberately empty — every card below is a registry recipe."""
    return FakeLLM(script={
        "overlay.feature.recommend_set": FakeResponse(output={
            "recommended_lens": "monetary", "reasoning": "fits"}),
        "overlay.feature.intents": FakeResponse(output={"intents": []}),
    })


def _watermark(conn, source: str) -> None:
    now = datetime.now(UTC)
    conn.execute(
        "INSERT INTO overlay_drift_watermark (catalog_source, last_completed_at, "
        "last_run_id, head_seq) VALUES (%s, %s, 'r', 0) "
        "ON CONFLICT (catalog_source) DO UPDATE SET last_completed_at = %s",
        (source, now, now))


def _two_catalogs(conn) -> None:
    """The audit's estate: ``cib`` is the customer master the run named — identity, one event
    timestamp that is a CONSENT date, two attributes, and no monetary column anywhere. ``ftr`` is
    the transaction catalog beside it, carrying the semantics the brief actually needed.

    Same shape as ``test_contract_catalog_satisfiability``'s fixture, on purpose: this journey
    must replay the audit's arrangement, not a friendlier one.
    """
    cib = [
        (CanonicalRow(CIB, "customer", "cust_num", "integer", is_grain=True, entity="Customer"),
         "customer_id"),
        (CanonicalRow(CIB, "customer", "consent_ts", "timestamp"), "event_timestamp"),
        (CanonicalRow(CIB, "customer", "segment_code", "text"), "category_code"),
        (CanonicalRow(CIB, "customer", "cust_susp_flg", "boolean"), "boolean_flag"),
    ]
    ftr = [
        (CanonicalRow(FTR, "txn", "cust_num", "integer", is_grain=True, entity="Customer"),
         "customer_id"),
        (CanonicalRow(FTR, "txn", "amount", "numeric", additivity="additive", currency="USD"),
         "monetary_flow"),
        (CanonicalRow(FTR, "txn", "dc_flag", "text"), "debit_credit_indicator"),
        (CanonicalRow(FTR, "txn", "booked_ts", "timestamp"), "event_timestamp"),
        (CanonicalRow(FTR, "txn", "acct_ref", "integer", entity="Account"), "account_id"),
    ]
    build_graph(conn, CIB, [r for r, _ in cib], concepts={content_hash(r): c for r, c in cib})
    build_graph(conn, FTR, [r for r, _ in ftr], concepts={content_hash(r): c for r, c in ftr})
    _watermark(conn, CIB)
    _watermark(conn, FTR)


def _scoped(client, *, catalog_source: str, hypothesis: str = HYPOTHESIS):
    """The considered-set POST a person makes from the confirmed-scope screen."""
    return client.post("/contract/considered-set", json={
        "hypothesis": hypothesis, "objective": OBJECTIVE,
        "catalog_source": catalog_source, "contract_version": 2,
        "confirmed_scope": {"primary": AML, "secondary": [], "expansion": "exact",
                            "confirmation_source": "user_confirmed"},
    }, headers=AUTH)


def _broadened(client, *, catalog_source: str):
    """The BROADEN gesture — the same route, ``unscoped: true``. ``v2_applicability`` fails OPEN
    on it, so the eligible corpus is the whole registry and the floor moves with it."""
    return client.post("/contract/considered-set", json={
        "hypothesis": HYPOTHESIS, "objective": OBJECTIVE,
        "catalog_source": catalog_source, "contract_version": 2,
        "confirmed_scope": {"unscoped": True, "confirmation_source": "user_broadened"},
    }, headers=AUTH)


# ── the whole-schema census, the T5 reviewer's method ───────────────────────────────────────────
#
# "Nothing durable was written" is a claim about the STORE, not about the three tables somebody
# remembered to check. The reviewer who closed T5 verified it by counting every row of every table
# in `public` at both ends — which is also the only form of the assertion that survives a future
# migration adding a table this journey has never heard of.

def _row_census(conn) -> dict[str, int]:
    tables = [row[0] for row in conn.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' "
        "AND table_type = 'BASE TABLE'").fetchall()]
    assert tables, "an empty census would make every no-trace assertion below pass vacuously"
    return {name: conn.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0]
            for name in tables}


def _eligible_and_floor(scope: ConfirmedScope) -> tuple[int, int]:
    """The registry's own numbers, measured. Hard-coding them would pin the REGISTRY's size, which
    grows (43 → 317 during this very program) — what this journey pins is the RELATION between the
    eligible corpus and the floor the refusal applies to it."""
    eligible = len(v2_applicability(scope).eligible_ids)
    return eligible, eligible // 2       # a class must be required by strictly MORE than the floor


# ══ 1. THE WRONG CATALOG REFUSES WITH DIRECTIONS ═════════════════════════════════════════════════

def test_the_aml_brief_on_the_customer_master_is_refused_and_the_refusal_points_at_ftr(
        make_client, conn):
    """STEP ONE of the story. The person files the audit's exact brief against the audit's exact
    catalog. Before this program that produced a run, 135 cards and 0 keeps. Now it produces a
    typed 422 that names the operand class nothing on this catalog can serve, the concepts the
    eligible corpus asks for in it, and — the half the projection structurally cannot say — WHICH
    catalog carries them.
    """
    _two_catalogs(conn)
    eligible, floor = _eligible_and_floor(ConfirmedScope(primary=AML))
    res = _scoped(make_client(_fake()), catalog_source=CIB)

    assert res.status_code == 422, res.text
    detail = res.json()["detail"]
    assert detail["code"] == "CATALOG_CANNOT_SATISFY_SCOPE"
    assert detail["catalog_source"] == CIB
    assert (detail["eligible_recipes"], detail["majority_floor"]) == (eligible, floor)

    (unsatisfiable,) = detail["unsatisfiable_classes"]
    assert unsatisfiable["operand_class"] == "measure"
    assert unsatisfiable["required_by"] > floor, \
        "the refusal fires on a MAJORITY of the eligible corpus, never on a minority gap"
    assert len(unsatisfiable["recipe_ids"]) == unsatisfiable["required_by"]

    # Per-concept `available_in` is the direction itself: not "something is missing" but "this
    # concept, asked for by this many recipes, lives over there".
    assert unsatisfiable["concepts"], "a refusal with no concepts named gives no directions"
    for concept in unsatisfiable["concepts"]:
        assert concept["concept"] and concept["required_by"] >= 1
        assert concept["available_in"] == [{"catalog_source": FTR, "columns": 1}]
    assert [c["concept"] for c in unsatisfiable["concepts"]] == ["monetary_flow"]

    assert detail["satisfying_catalog_sources"] == [FTR]
    assert FTR in detail["message"]


def test_the_refusal_writes_nothing_anywhere_in_the_schema(make_client, conn):
    """The leave-no-trace law, asserted over the WHOLE store rather than over the three tables a
    reader would think to name. A refused generation is not a generation that produced nothing:
    an orphan run row reads to anyone auditing the store like a run that served zero cards, which
    is precisely the confidence-without-warrant this program removes."""
    _two_catalogs(conn)
    before = _row_census(conn)

    res = _scoped(make_client(_fake()), catalog_source=CIB)
    assert res.status_code == 422, res.text

    after = _row_census(conn)
    assert after == before, {
        name: (before.get(name), after[name])
        for name in after if before.get(name) != after[name]}


# ══ 2. THE BROADEN DIRECTION ═════════════════════════════════════════════════════════════════════

def test_broadening_over_the_same_mis_aimed_catalog_is_refused_identically(make_client, conn):
    """STEP TWO. The person's next instinct is to widen — "show me everything you can do here".
    The owner's ruling (2026-08-25) is that broaden is governed IDENTICALLY: the one law is
    scope-width-blind, and on an exploratory gesture "aim at ftr instead" is MORE useful than a
    page of setup work, not less.

    Both numbers are measured, not typed: the unscoped scope fails OPEN in ``v2_applicability``,
    so the eligible corpus is the whole registry and the floor is half of it."""
    _two_catalogs(conn)
    eligible, floor = _eligible_and_floor(ConfirmedScope(primary=None, unscoped=True))
    assert eligible == len(V2_RECIPES), "unscoped fails OPEN — the corpus is the whole registry"
    before = _row_census(conn)

    res = _broadened(make_client(_fake()), catalog_source=CIB)

    assert res.status_code == 422, res.text
    detail = res.json()["detail"]
    assert detail["code"] == "CATALOG_CANNOT_SATISFY_SCOPE"
    assert (detail["eligible_recipes"], detail["majority_floor"]) == (eligible, floor)
    (unsatisfiable,) = detail["unsatisfiable_classes"]
    assert unsatisfiable["operand_class"] == "measure"
    assert unsatisfiable["required_by"] > floor
    assert detail["satisfying_catalog_sources"] == [FTR]

    assert _row_census(conn) == before, "a refused broaden writes exactly as little as a refused " \
                                        "scoped brief — the law does not care how wide the ask was"


def test_broadening_onto_the_catalog_that_carries_the_semantics_mints_the_run(make_client, conn):
    """The other direction, and the one that keeps the ruling from being a ban on broadening. The
    identical exploratory gesture, aimed at the catalog that holds the transaction semantics, is
    planned and the run is minted."""
    _two_catalogs(conn)
    res = _broadened(make_client(_fake()), catalog_source=FTR)
    assert res.status_code == 200, res.text
    assert conn.execute("SELECT count(*) FROM feature_generation_run").fetchone()[0] == 1



# ══ 3. RE-AIMED AND HONEST ═══════════════════════════════════════════════════════════════════════
#
# STEP THREE. The person follows the directions and aims the identical brief at `ftr`. This is the
# section that replaces the audit's headline number: 43 eligible recipes became 135 cards, 0 of
# which an SME kept. What the same gesture produces now is asserted below, in four parts — the
# collapse, the badge, the rationale, and the lane that holds back everything that did not bind.


def _served(make_client, conn):
    _two_catalogs(conn)
    res = _scoped(make_client(_fake()), catalog_source=FTR)
    assert res.status_code == 200, res.text
    body = res.json()
    cards = [f for section in body["alternatives"] for f in section["features"]]
    return body, cards


def test_the_re_aimed_run_serves_one_entry_per_recipe_and_never_a_variant_fan_out(
        make_client, conn):
    """T6's COLLAPSE, at the route, on the story's own arrangement.

    The audit's shape was a fan-out: every authored parameterization of every eligible recipe
    became its own candidate, its own card, its own option id and its own row in every store —
    ``transaction_amount_percentile`` alone contributed 9. The lens now assembles the PRIMARY
    variant only, and the siblings ride the surviving card as ``param_alternatives``.

    The assertion is over the WHOLE outcome, not just the served lane, because the collapse is a
    property of the candidate stream: every eligible recipe produces exactly one entry, and that
    entry is either a card or a needs-setup row. Restricting it to the cards would make it vacuous
    here (this five-column fixture serves one), and vacuous is how a collapse pin dies.
    """
    body, cards = _served(make_client, conn)
    eligible = len(v2_applicability(ConfirmedScope(primary=AML)).eligible_ids)
    needs_setup = body["needs_setup"]

    assert len(cards) + len(needs_setup) == eligible, \
        "one entry per eligible recipe — under the pre-T6 lens this was one per authored VARIANT"
    recipe_ids = [c["recipe_id"] for c in cards] + [e["recipe_id"] for e in needs_setup]
    assert len(set(recipe_ids)) == len(recipe_ids), "no recipe appears twice, in any lane"
    assert len(cards) <= eligible

    # The chosen parameterization rides every entry's identity, and the card names the axis it
    # was chosen from with the choice bracketed. "90 days" in the brief is what picked it.
    windowed = [entry for entry in needs_setup if "@window=" in entry["source_definition_id"]]
    assert windowed, "the fixture must exercise a windowed recipe or this half says nothing"
    assert all(entry["source_definition_id"].endswith("@window=90") for entry in windowed), \
        "the brief said 90 days, so 90 is the variant every windowed recipe was planned at"
    (card,) = cards
    assert card["source_definition_id"] == f"{card['recipe_id']}@window=90"
    assert card["param_alternatives"] == "window: 30/[90]/180", \
        "the siblings are NAMED on the card, not minted as cards of their own"


def test_the_served_card_wears_the_badge_the_corpus_earns_and_no_better(make_client, conn):
    """T3's BADGE, at the route. 132 of the audit's 135 cards read ``DESIGN-CHECKED`` over recipes
    the registry marks ``FORMULA_BLOCKED`` — a green stamp asserting a design review that had not
    happened, on the one screen where a person decides what to trust.

    Measured on today's registry: 295 of 317 recipes are ``FORMULA_BLOCKED``, 19
    ``CONCEPTUAL_ONLY``, and exactly 3 ``FORMULA_AUTHORABLE``. So ``DESIGN-CHECKED`` is now
    genuinely rare, and this journey's one served card is one of the 295 — which makes the
    assertion below load-bearing rather than lucky.

    The badge is not gone for ever: it moves back on its own the moment a recipe earns a reviewed
    formula expectation. Nothing here has to be re-run to restore it.
    """
    from featuregen.overlay.upload.recipe_registry_v2 import v2_recipe_by_id

    _body, cards = _served(make_client, conn)
    blocked = [c for c in cards
               if v2_recipe_by_id(c["recipe_id"]).readiness == "FORMULA_BLOCKED"]
    assert blocked, "the pin is only worth anything while a FORMULA_BLOCKED recipe is served"
    for card in blocked:
        assert card["verification"] != "DESIGN-CHECKED", (
            f"{card['recipe_id']} is FORMULA_BLOCKED — a design-checked badge over it is the "
            "audit's headline defect")
        assert card["verification"] == "UNVERIFIED"


def test_the_served_card_carries_a_rationale_taken_byte_for_byte_from_the_corpus(
        make_client, conn):
    """T4's RATIONALE, at the route. The audit's cards carried an EMPTY rationale — the projection
    read ``conceptual_reason``, which the recipe contract FORBIDS on an executable candidate
    (0 of 43 populated), while ``business_definition`` sat populated on every single recipe and
    unread.

    Asserted by DERIVATION, not by quoting a sentence into the test: the card's rationale is the
    registry's own two authored fields joined. A test that pasted the words would pass over a
    rationale that had drifted from the definition it claims to be quoting."""
    from featuregen.overlay.upload.recipe_registry_v2 import v2_recipe_by_id

    _body, cards = _served(make_client, conn)
    for card in cards:
        recipe = v2_recipe_by_id(card["recipe_id"])
        assert recipe.business_definition.strip()
        assert card["rationale"] == (
            f"{recipe.business_definition.strip()} — {recipe.decision_context.strip()}")
        assert card["rationale"], "a card that explains nothing is the defect T4 closed"
        # T4's other two: the recipe's OWN declared operation, and never the forbidden field.
        assert card["aggregation"] == recipe.formula.result_class


def test_the_needs_setup_lane_speaks_each_operand_s_own_status_in_its_own_words(
        make_client, conn):
    """T2's LANE, at the route, and the reason it is not called "missing".

    Fourteen of the fifteen eligible recipes cannot compute on this catalog. Before this program
    they were served as cards anyway. Now they are held back and each one says WHY, per operand,
    in the words that operand's binder verdict earns — and the two statuses this arrangement
    produces say two genuinely different things with two different remedies:

    * ``unresolved`` — the one condition that IS an absence, and the only one allowed to say so.
    * ``blocked`` — a column matched and the binding was REFUSED. Here that is T8 firing: the
      counterparty legs of ``fan_in_fan_out`` and ``rapid_movement_passthrough`` would have bound
      the population's own key, and a party is never its own counterparty. The sentence NAMES the
      column it refused, because a refusal nobody can see is a dead end.

    ``ambiguous`` (several columns carry the concept, nobody has adjudicated) does not arise here
    and the test does not pretend it does: this fixture carries exactly one column per concept, so
    there is nothing to be ambiguous between. It is measured, not assumed — the census below is
    computed from the response.
    """
    body, _cards = _served(make_client, conn)
    needs_setup = body["needs_setup"]
    assert needs_setup, "the held-back candidates are NAMED, never silently dropped"

    census: dict[str, int] = {}
    for entry in needs_setup:
        assert entry["catalog_source"] == FTR, "the lane speaks only of the catalog it planned over"
        assert entry["unbound_concepts"], "every entry names the concepts that did not bind"
        assert entry["sentence"] == "; ".join(o["sentence"] for o in entry["unbound_operands"])
        for operand in entry["unbound_operands"]:
            census[operand["status"]] = census.get(operand["status"], 0) + 1
            if operand["status"] == "unresolved":
                assert operand["sentence"] == (
                    f"no read-scoped column carries {operand['concept']}")
                assert operand["tied_refs"] == [], "an absence has no column to name"
            elif operand["status"] == "blocked":
                assert operand["tied_refs"], \
                    "a blocked binding matched a column — the refusal must name it"
                listed = ", ".join(operand["tied_refs"])
                codes = ", ".join(operand["reason_codes"])
                assert operand["sentence"] == (
                    f"{operand['concept']} is carried by {listed} and the binding is "
                    f"blocked ({codes})")
            else:                                   # pragma: no cover — see the docstring
                raise AssertionError(
                    f"this arrangement produced an unexpected status: {operand['status']!r}")

    assert set(census) == {"unresolved", "blocked"}, census
    assert census["blocked"] >= 1, \
        "T8's refusal reaches the lane on this arrangement — that is what makes the branch real"


# ══ 4. THE TARGET STORY ══════════════════════════════════════════════════════════════════════════
#
# The same run's OTHER post-mortem. The person's goal text said "in the next 90 days"; the platform
# proposed `cust_susp_flg` as the target, at confidence HIGH, with no disclosure that the registry
# calls it label-ADJACENT and not the label — and recorded a window of 0 days beside a goal that
# stated 90. Three separate assertions the platform had no warrant for, on one screen.

_TARGET_REF = "public.accounts.balance"

#: The goal text from the audited run, in the shape the post-mortem quotes it.
_GOAL = "Customers likely to be flagged for AML review in the next 90 days."

#: The acknowledgment the confirm gate asks for on ANY target the registry does not certify as the
#: outcome label. It asserts only what it says — never a correlation.
_ACK = {"target_not_outcome_acknowledged": True}


def _target_fake(*, window_days: int) -> FakeLLM:
    """The upload path's enrichment plus the intake ticket, scripted.

    Every column enriches to ``restriction_status`` — a REGISTERED concept the registry marks
    ``near_label``, and the concept ``cust_susp_flg`` actually carried on the audited run. So this
    catalog's only outcome-ish column is a proxy, which is the arrangement under test.
    """
    from featuregen.overlay.upload.contract.intake_ticket import INTAKE_TICKET_TASK

    fake = FakeLLM(script={
        "overlay.enrich.concept": FakeResponse(output={"concept": "restriction_status"}),
        "overlay.enrich.definition": FakeResponse(output={"definition": "a column"}),
        "overlay.enrich.domain": FakeResponse(output={"domain": "Deposits"}),
        INTAKE_TICKET_TASK: FakeResponse(output={
            "target_ref": _TARGET_REF, "target_window_days": window_days,
            "target_type": "binary_classification", "business_domain": [],
            "confidence": "high",
            "runner_up_refs": ["public.transactions.amount"]})})
    fake._task_fallback["overlay.enrich.concept"] = [
        FakeResponse(output={"concept": "restriction_status"})]
    return fake


def _intake(client, *, goal: str = _GOAL):
    return client.post("/contract/intake", json={
        "hypothesis": goal, "catalog_source": "deposits"}, headers=AUTH)


def _recorded(conn, intent_id: str) -> tuple:
    return conn.execute(
        "SELECT target_ref, target_provenance FROM contract_intent WHERE intent_id = %s",
        (intent_id,)).fetchone()


def test_the_target_proposal_abstains_names_the_proxy_and_types_the_window_contradiction(
        make_client, conn):
    """T7 (a) and (b), at the route, on the audited goal text.

    Three separate honesty moves, all visible on the one response:

    * **Abstention.** The catalog holds no outcome-family concept, so nothing auto-commits — the
      model's own ``confidence: high`` does not survive contact with the registry.
    * **The proxy is NAMED, not hidden.** ``target_is_proxy`` with the class and the concept that
      earned it. The audit's complaint was never that ``cust_susp_flg`` was offered; it was that it
      was offered as if it were the outcome.
    * **The window contradiction is TYPED.** The goal states 90 days; the model's ticket says 0.
      A platform that recorded 0 there would be asserting a horizon the person never gave it, so
      the window is refused rather than reconciled, and ``target_window_days`` comes back None.
    """
    client = make_client(_target_fake(window_days=0))
    upload_csv(client, "deposits", DEPOSITS_CSV)
    body = _intake(client).json()
    ticket = body["ticket"]

    assert ticket["confidence"] == "abstain", \
        "no outcome-family concept exists here, so nothing commits itself"
    assert ticket["target_is_proxy"] is True
    assert ticket["target_leakage_class"] == "near_label"
    assert ticket["target_concept"] == "restriction_status"
    assert ticket["proxy_candidates"][0]["ref"] == _TARGET_REF
    assert ticket["proxy_candidates"][0]["concept"] == "restriction_status"
    assert ticket["outcome_candidates"] == [], \
        "and it says so plainly when the catalog holds no label at all"

    refusal = ticket["window_refusal"]
    assert refusal["code"] == "WINDOW_CONTRADICTS_GOAL"
    assert (refusal["stated_days"], refusal["ticket_days"]) == (90, 0)
    assert ticket["target_window_days"] is None, \
        "a contradicted window is refused, never silently reconciled to one side"
    assert ticket["window_source"] == "contradicted"
    assert _recorded(conn, body["intent_id"])[1] is None, \
        "and a draft is not a decision — nothing durable yet"


def test_confirming_the_proxy_target_needs_the_acknowledgment_and_then_records_it(
        make_client, conn):
    """T7 (c), at the route, both directions.

    The refusal is per TIER because the registry says different things per tier, and this one is
    ``near_label``: the registry positively asserts label-adjacency, so the word PROXY is EARNED
    here and the registry's own warrant is quoted rather than paraphrased. (A ``standard`` concept
    gets a refusal that claims no correlation at all; an unregistered one gets "absence is not an
    assertion". Those tiers have their own byte-level pins in ``test_contract_intake``.)

    The acknowledgment asserts exactly one thing — "I know the registry does not certify this as
    the outcome label" — and it is the ONLY door: behind the refusal, nothing is written.
    """
    client = make_client(_target_fake(window_days=0))
    upload_csv(client, "deposits", DEPOSITS_CSV)
    intent_id = _intake(client).json()["intent_id"]

    unacknowledged = client.post("/contract/intake/target", json={
        "intent_id": intent_id, "decision": "confirmed", "target_ref": _TARGET_REF,
        "catalog_source": "deposits"}, headers=AUTH)
    assert unacknowledged.status_code == 422, unacknowledged.text
    detail = unacknowledged.json()["detail"]
    assert "restriction_status" in detail and "near_label" in detail
    assert "PROXY" in detail, "the registry asserts adjacency here, so the word is warranted"
    assert "BORDER" in detail, "and the warrant is QUOTED — a paraphrase is a second assertion"
    assert "target_not_outcome_acknowledged" in detail, \
        "the refusal names the field the client must send, so it is actionable"
    assert _recorded(conn, intent_id)[1] is None, "nothing is recorded behind a refusal"

    acknowledged = client.post("/contract/intake/target", json={
        "intent_id": intent_id, "decision": "confirmed", "target_ref": _TARGET_REF,
        "catalog_source": "deposits", **_ACK}, headers=AUTH)
    assert acknowledged.status_code == 200, acknowledged.text
    assert acknowledged.json()["target_is_proxy"] is True, \
        "acknowledged is not un-labelled — the answer still says what this column is"
    assert acknowledged.json()["target_leakage_class"] == "near_label"
    assert _recorded(conn, intent_id) == (_TARGET_REF, "human_confirmed")



# ══ 5. THE C9 REVERSE — the one place this program knowingly serves LESS ══════════════════════════

def _declare_history_depth(conn, *, days: str) -> None:
    """The transaction source declares how far back it actually keeps rows. C9's history-depth law
    reads it and refuses a window the source cannot cover."""
    from featuregen.overlay.evidence import AssertionStrength, EvidenceProducer
    from featuregen.overlay.field_evidence import field_input_hash, record_field_evidence
    from featuregen.overlay.upload.object_ref import normalize_ref

    logical = normalize_ref(FTR, "public", "txn", None)
    record_field_evidence(
        conn, logical_ref=logical, field_name="history_depth_days", proposed_value=days,
        producer=EvidenceProducer.SOURCE, strength=AssertionStrength.ATTESTED,
        producer_ref="source:test", source_snapshot_id="snap-test",
        input_hash=field_input_hash(logical_ref=logical, field_name="history_depth_days",
                                    material=days))


def test_a_window_the_source_cannot_cover_serves_nothing_and_that_is_AS_DESIGNED(
        make_client, conn):
    """▲ THE PROGRAM'S ONE KNOWINGLY SERVED-LESS CASE, pinned as a DECISION rather than a win.

    C9's history-depth law is the one axis on which the binder is not variant-invariant: it reads
    the variant's own ``window`` and refuses the event-time operand when the source's declared
    depth cannot cover it. T6 assembles the PRIMARY variant only. Put those together and, when the
    hypothesis names a window the source cannot cover, the run serves NOTHING where the pre-T6
    lens served a working sibling — and that is the LIKELIER path, because real hypotheses carry
    window tokens and the audited one did.

    Measured here on the story's own brief ("...over 90 days") against a source declaring 60 days
    of history: **zero cards**, and the lane says exactly why, naming the column and the code.

    Both behaviours are defensible and the choice was deliberate. The pre-T6 lens hid the block
    behind a sibling that answered a question nobody asked — a 30-day figure for a 90-day brief —
    which is the confidence-without-warrant this program exists to remove. This one reports the
    block and offers nothing. But it is a REAL loss of served work, not a neutral cleanup, and it
    belongs on the record beside the 135→43 win.

    The remedy is written down and NOT built: making ``param_alternatives`` actionable, so the
    30-day variant is OFFERED as the alternative the card already names instead of silently
    substituted. Inventing a parameter-override seam is a bigger decision than any task in this
    program took, so it stays unchartered — see the T6 OPERATOR CONSEQUENCES section of
    ``docs/superpowers/plans/2026-08-24-serving-quality-remediation.md``. The companion
    ``test_the_alternatives_are_a_LABEL_...`` below is the executable form of that gap.
    """
    _two_catalogs(conn)
    _declare_history_depth(conn, days="60")

    res = _scoped(make_client(_fake()), catalog_source=FTR)
    assert res.status_code == 200, res.text
    body = res.json()

    cards = [f for section in body["alternatives"] for f in section["features"]]
    assert cards == [], "the primary variant is the blocked one, so there is no card to serve"

    depth_blocked = [
        (entry, operand)
        for entry in body["needs_setup"] for operand in entry["unbound_operands"]
        if "HISTORY_DEPTH_INSUFFICIENT" in operand["reason_codes"]]
    assert depth_blocked, "serving nothing without saying why would be the worse failure"
    for entry, operand in depth_blocked:
        assert entry["source_definition_id"].endswith("@window=90"), \
            "it is the 90-day variant that was refused — the one the brief asked for"
        assert operand["tied_refs"] == ["public.txn.booked_ts"], \
            "the refusal names the clock it refused, not just the fact of refusal"
        assert "reduce the window" in operand["resolution"]
        assert "extend the source's declared history" in operand["resolution"]


def test_the_alternatives_are_a_LABEL_and_the_only_control_is_the_hypothesis(make_client, conn):
    """The unchartered gap, made executable. ``param_alternatives`` renders the whole axis with the
    chosen value bracketed — but choosing a different one is not an action. The ONLY way an
    operator reaches the 30-day variant is to say "30 days" in the hypothesis and re-run.

    Asserted as a CONTRAST on one arrangement, because that is what makes it a statement about the
    control rather than about the catalog: identical estate, identical declared depth, identical
    scope — one word of the brief changed, and the card appears.
    """
    _two_catalogs(conn)
    _declare_history_depth(conn, days="60")

    def _cards(hypothesis: str) -> list[str]:
        res = _scoped(make_client(_fake()), catalog_source=FTR, hypothesis=hypothesis)
        assert res.status_code == 200, res.text
        return [f["source_definition_id"]
                for section in res.json()["alternatives"] for f in section["features"]]

    assert _cards(HYPOTHESIS) == [], "the brief's own 90-day window is beyond the declared depth"
    assert _cards("customers move money over 30 days") == ["round_amount_ratio@window=30"], \
        "re-typing the brief is the parameter control this platform actually has"



# ══ 6. THE KNOWN-OPEN GAP, PINNED AS KNOWN-OPEN ═══════════════════════════════════════════════════

#: The counterparty catalog: a customer master beside an ACCOUNT-grained transaction table. The
#: only customer-identifying column in the whole estate is the master's own grain key.
CPTY = "cpty"

#: The audit's headline recipe. Its population is an ACCOUNT (``account`` / ``account_id``); its
#: counterparty leg asks for ``customer_id``.
CPTY_RECIPE = "new_counterparty_flag"

#: The customer master's grain key — one row per customer, and no counterparty anywhere in sight.
_CUSTOMER_MASTER_KEY = "public.customers.cust_num"


def _counterparty_catalog(conn) -> None:
    rows = [
        (CanonicalRow(CPTY, "customers", "cust_num", "varchar(20)", is_grain=True,
                      entity="Customer", definition="the bank's CIF number for the customer"),
         "customer_id"),
        (CanonicalRow(CPTY, "txn", "acct_ref", "integer", is_grain=True, entity="Account"),
         "account_id"),
        (CanonicalRow(CPTY, "txn", "booked_ts", "timestamp"), "event_timestamp"),
        (CanonicalRow(CPTY, "txn", "book_status", "text"), "booking_status"),
    ]
    build_graph(conn, CPTY, [r for r, _ in rows],
                concepts={content_hash(r): c for r, c in rows})
    _watermark(conn, CPTY)


def test_KNOWN_OPEN_an_account_anchored_counterparty_still_binds_a_customer_master_key(
        make_client, conn):
    """▲▲ THIS TEST PINS A DEFECT. It asserts what the platform does TODAY, which is WRONG, and it
    exists so that the day the fix lands this test FAILS and is INVERTED. Do not "repair" it by
    relaxing an assertion; the repair is in the binder, and it turns this pin into its opposite.

    **What is wrong.** ``new_counterparty_flag`` — the audit's own headline card — asks "did this
    ACCOUNT pay a counterparty it never paid before". Its population is an Account; its
    counterparty leg asks for ``customer_id``. On the estate below, the only column in the world
    carrying ``customer_id`` is the CUSTOMER MASTER's grain key: one row per customer, describing
    the bank's own customers, containing no counterparty of anything. The binder puts it on the
    counterparty leg anyway, every required operand reports bound, and a card is served — a
    feature that would compute "new counterparty" against a table of the bank's own customer list.

    **Why T8 does not catch it, precisely.** T8's rule is structural and deliberately narrow: a
    non-anchor operand whose concept IDENTIFIES THE ANCHOR'S OWN ENTITY must not bind the
    population's grain key. Here the anchor is an Account and the counterparty asks for a Customer,
    so the same-entity condition is not met and the rule correctly declines to fire — the third
    assertion below pins that derivation rather than describing it. T8 covers 6 of the 12
    structurally identifiable counterparty legs in the registry; this is one of the other six.

    **Why T2 does not catch it either.** T2 diverts a candidate with any UNBOUND required operand.
    Every operand here is bound. A wrongly-bound operand is invisible to a rule about unbound ones.

    **What would catch it, and what it is waiting on.** The missing axis is GRAIN — the fact that
    ``public.customers`` is a per-customer master rather than a per-transaction event table, so a
    column on it cannot be a per-event counterparty identity at all. ``SOURCE_GRAIN_MISMATCH``
    exists as a code but cannot fire, because ``table_shape`` is an UNCOMPILED axis: nothing in
    the pipeline turns a table into a declared shape the binder can consult. Compiling it is the
    "grain law", and it is an owner/roadmap item — see the T8 entry in
    ``.superpowers/sdd/2026-08-24-serving-quality-remediation/progress.md`` and the final package.

    **The honest shape of a known defect is a pin that demands its own inversion.** When the grain
    law lands, ``new_counterparty_flag`` must land in ``needs_setup`` with the counterparty leg
    refused and the master key NAMED — exactly as T8's own case already does. At that moment this
    test goes red, and the correct response is to rewrite it as the refusal it should always have
    been, and to strike this section from the known-open list.
    """
    _counterparty_catalog(conn)
    res = make_client(_fake()).post("/contract/considered-set", json={
        "hypothesis": "accounts paying brand new counterparties over 90 days",
        "objective": "spot novel counterparties", "catalog_source": CPTY,
        "contract_version": 2,
        "confirmed_scope": {"primary": "payments.behaviour", "secondary": [],
                            "expansion": "exact", "confirmation_source": "user_confirmed"},
    }, headers=AUTH)
    assert res.status_code == 200, res.text
    body = res.json()
    cards = {f["recipe_id"]: f
             for section in body["alternatives"] for f in section["features"]}

    # 1 — IT SERVES. Today. This is the assertion that must one day fail.
    assert CPTY_RECIPE in cards, (
        "if this fails, the grain law may have landed — read this docstring before touching "
        "anything, then INVERT this test rather than deleting it")
    card = cards[CPTY_RECIPE]
    assert card["source_definition_id"] == f"{CPTY_RECIPE}@window=90"

    # 2 — AND THE COUNTERPARTY LEG IS ON THE CUSTOMER MASTER'S OWN GRAIN KEY.
    bindings = {b["role"]: b["ref"][1] for b in card["input_role_bindings"]}
    assert bindings["counterparty"] == _CUSTOMER_MASTER_KEY, \
        "the wrong binding IS the defect — pin it by role, not by the card's ref list"
    assert bindings["account"] == "public.txn.acct_ref", \
        "the population binds correctly, which is exactly why nothing downstream notices"
    assert _CUSTOMER_MASTER_KEY in card["derives_from"]

    # 3 — T2 CANNOT DIVERT IT: every required operand reported bound, so the lane never sees it.
    assert CPTY_RECIPE not in {entry["recipe_id"] for entry in body["needs_setup"]}
    assert card["option_id"], "it is a full option — saveable, and offered to a person as real"

    # 4 — AND T8 CORRECTLY DECLINES, on its own derivation. The rule is not broken here; it is
    #     narrower than the problem, and the enabling absence is the uncompiled grain axis.
    from featuregen.overlay.upload.feature_planning_contracts import (
        planning_request_from_recipe,
    )
    from featuregen.overlay.upload.recipe_operand_policy import (
        population_anchor_and_distinct_roles,
    )
    from featuregen.overlay.upload.recipe_registry_v2 import v2_recipe_by_id

    recipe = v2_recipe_by_id(CPTY_RECIPE)
    anchor_role, protected = population_anchor_and_distinct_roles(
        planning_request_from_recipe(recipe))
    assert anchor_role == "account", "the population is an ACCOUNT here, not a customer"
    assert "counterparty" not in protected, (
        "T8 protects a leg only when its concept identifies the ANCHOR's entity; account_id and "
        "customer_id are different entities, so the rule declines — correctly, and insufficiently")
    assert {operand.concept for operand in recipe.operands if operand.role == "counterparty"} == \
        {"customer_id"}


def test_KNOWN_OPEN_the_same_gap_is_not_confined_to_one_recipe(make_client, conn):
    """The second half of the honest statement: this is a CLASS, not a one-off. Six account-
    anchored recipes carry the uncovered counterparty leg; on this estate two of them bind and
    serve. Measured from the response rather than listed from memory, so the number cannot rot
    quietly — if a registry change moves it, this test says so.

    Same instruction as above: when the grain law lands, this goes red and gets inverted.
    """
    _counterparty_catalog(conn)
    res = make_client(_fake()).post("/contract/considered-set", json={
        "hypothesis": "accounts paying brand new counterparties over 90 days",
        "objective": "spot novel counterparties", "catalog_source": CPTY,
        "contract_version": 2,
        "confirmed_scope": {"primary": "payments.behaviour", "secondary": [],
                            "expansion": "exact", "confirmation_source": "user_confirmed"},
    }, headers=AUTH)
    assert res.status_code == 200, res.text

    served_on_the_master_key = sorted(
        f["recipe_id"]
        for section in res.json()["alternatives"] for f in section["features"]
        if any(b["role"] in ("counterparty", "payer", "payee")
               and b["ref"][1] == _CUSTOMER_MASTER_KEY
               for b in f.get("input_role_bindings", ())))
    assert served_on_the_master_key == [
        "distinct_transaction_counterparty_count", CPTY_RECIPE], served_on_the_master_key
