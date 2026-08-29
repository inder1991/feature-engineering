"""A7 — the demand vocabulary extension, the one-writer law (R5), and the satisfaction projection.

Four laws, pinned here rather than described in a header:

* **the vocabulary is CLOSED and it is ONE set.** 1120 declared it; 1132 extends it (1120 is
  immutable as a file — §V fact V7). The Python map and the SQL CHECK are the same closed set,
  and the test proves that by driving every member through SQL rather than by reading either
  side's source. `REALIZATION_ATTACHMENT_DEFECT` is named explicitly as a NON-member: R6 makes it
  an ops alert, and an ops alert that became a demand category would attribute a platform fault
  to somebody's feature.
* **one writer (R5).** Demand records ONCE per planning occurrence, and repeated occurrences stay
  countable. Structural, not conventional: `bridge_demand_observation` is written from exactly one
  module, the identity material has exactly one owner, and the store dedupes within a call as well
  as across calls.
* **satisfaction is a PROJECTION.** History is append-only; a demand leaves the unresolved view
  because a later occurrence of the same governed variant RESOLVED, never because a row changed.
* **provisional is not absent.** A4c's provisional sandbox realization (cardinality UNKNOWN, no
  current pointer) must not read as "no realization exists" — the two are different work items.
"""
from __future__ import annotations

import subprocess
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest
from tests.featuregen.overlay.upload.test_bridge_assessment_contracts import (
    _executable_pair,
    _realization,
)

from featuregen.overlay.upload.bridge_realization import (
    DirectionalCardinalityVerdictV1,
    ExecutionTier,
)
from featuregen.overlay.upload.bridge_store import (
    BridgeDependencyRefV1,
    append_realization_revision,
    bridge_dependency_snapshot_id,
    realization_revisions_for_bridge,
)
from featuregen.overlay.upload.governed_observation_store import (
    CARDINALITY_EVIDENCE_REQUIRED,
    DEMAND_VERDICT_QUEUES,
    demand_identity_hash,
    demand_satisfaction,
    observation_queues,
    record_bridge_demand,
    record_planning_observations,
    resolve_demand_anchor,
)
from featuregen.overlay.upload.governed_telemetry_worker import (
    REALIZATION_EXISTS_UNATTACHED,
    REALIZATION_NONE,
    REALIZATION_PROVISIONAL_UNKNOWN_CARDINALITY,
    demands_for_rejection,
)
from featuregen.overlay.upload.taxonomy.entity_relationships import Cardinality

_QUEUES = ("bridge_demand", "realization_gap", "planner_capacity")
_NOW = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)


# ── seeding ────────────────────────────────────────────────────────────────────────────────────


def _seed_run(conn, suffix: str) -> tuple[str, str]:
    intent_id, run_id = f"a7_int_{suffix}", f"a7_run_{suffix}"
    conn.execute(
        "INSERT INTO contract_intent (intent_id, hypothesis, intake_mode) "
        "VALUES (%s, 'h', 'hypothesis')", (intent_id,))
    conn.execute(
        "INSERT INTO feature_generation_run (generation_run_id, intent_id, actor) "
        "VALUES (%s, %s, '{}'::jsonb)", (run_id, intent_id))
    return intent_id, run_id


def _observation(**overrides) -> dict:
    row = {
        "definition_origin": "recipe_v2",
        "canonical_definition_id": "recipe:txn_count_30d",
        "governed_variant_id": "gvar_" + "a" * 64,
        "planning_request_hash": "p" * 64,
        "target_entity": "party",
        "anchor_catalog_source": "core_banking",
        "resolution_status": "unresolved",
    }
    row.update(overrides)
    return row


def _rejection(**overrides) -> dict:
    rejection = {
        "verdict": "missing_realization",
        "recipe_revision_hash": "r" * 64,
        "relationship_id": "party_owns_account",
        "relationship_version": "1.0.0",
        "from_entity": "party",
        "to_entity": "account",
        "position_catalog": "core_banking",
        "position_table_ref": "public.party",
        "hop_index": -1,
        "realizers": [],
        "near_side_key_refs": ["public.party.party_id"],
        "to_endpoint_hint": "",
    }
    rejection.update(overrides)
    return rejection


def _file(conn, suffix: str, *, mode: str = "live", variant: str | None = None,
          status: str = "unresolved", rejections=None) -> str:
    """One planning OCCURRENCE with its demand children; returns the observation id."""
    intent_id, run_id = _seed_run(conn, suffix)
    observation_id = record_planning_observations(
        conn, generation_run_id=run_id, intent_id=intent_id, observation_mode=mode,
        rows=[_observation(resolution_status=status,
                           **({"governed_variant_id": variant} if variant else {}))])[0]
    if rejections:
        record_bridge_demand(conn, observation_id=observation_id, rejections=rejections)
    return observation_id


def _occurrence_at(conn, suffix: str, *, minutes: int, mode: str = "live",
                   variant: str, status: str, rejections=None) -> str:
    """One occurrence stamped at an EXPLICIT instant, inserted directly.

    Not a shortcut: 1120's ``recorded_at`` defaults to ``now()``, which in Postgres is the
    TRANSACTION timestamp, and every test runs inside ONE rolled-back transaction — so the store's
    own writer cannot produce two distinct instants here however many times it is called. In
    production the two occurrences this projection compares are two different runs in two different
    transactions, and they genuinely differ. The append-only triggers make the alternative (write
    then UPDATE the timestamp) impossible by design, which is the right trade. Every constraint and
    trigger on the table still fires on this INSERT — only the clock is supplied.
    """
    intent_id, run_id = _seed_run(conn, suffix)
    observation_id = f"gpo_{suffix}"
    conn.execute(
        "INSERT INTO governed_planning_observation (observation_id, generation_run_id, intent_id, "
        " observation_mode, definition_origin, canonical_definition_id, governed_variant_id, "
        " planning_request_hash, target_entity, resolution_status, recorded_at) "
        "VALUES (%s, %s, %s, %s, 'recipe_v2', 'recipe:txn_count_30d', %s, %s, 'party', %s, "
        "        now() + make_interval(mins => %s))",
        (observation_id, run_id, intent_id, mode, variant, "p" * 64, status, minutes))
    if rejections:
        record_bridge_demand(conn, observation_id=observation_id, rejections=rejections)
    return observation_id


# ── 1) the extended vocabulary, and the closed set that still refuses ──────────────────────────


def test_the_new_category_is_accepted_and_routes_to_the_realization_gap_queue(conn) -> None:
    """`CARDINALITY_EVIDENCE_REQUIRED` is a demand-QUEUE CATEGORY (R6) — never an action blocker.

    Its queue is `realization_gap` and not a fourth queue: the crossing IS sanctioned and a
    realization DOES exist; what is missing is the cardinality EVIDENCE that makes it costable.
    That is realization work, addressed to the person who already owns the realization."""
    observation_id = _file(conn, "new_category")
    demand_ids = record_bridge_demand(
        conn, observation_id=observation_id,
        rejections=[_rejection(verdict=CARDINALITY_EVIDENCE_REQUIRED)])
    assert len(demand_ids) == 1
    queue, verdict = conn.execute(
        "SELECT demand_queue, verdict FROM bridge_demand_observation WHERE demand_id = %s",
        (demand_ids[0],)).fetchone()
    assert (queue, verdict) == ("realization_gap", CARDINALITY_EVIDENCE_REQUIRED)
    assert DEMAND_VERDICT_QUEUES[CARDINALITY_EVIDENCE_REQUIRED] == "realization_gap"


def test_the_category_is_not_an_action_blocker(conn) -> None:
    """R6, structurally: the demand ledger owns this category and the ACTION vocabulary does not.

    Were it registered as a semantic-eligibility reason it would acquire a six-action disposition
    row and start refusing acts — which is exactly what R6 forbids: unknown cardinality is
    previewable under guards (the owner's matrix), so it may never BLOCK.

    The comparison is CASE-FOLDED on both sides and that is load-bearing, not tidiness: this
    ledger stores the lowercase, ReasonCode-shaped spelling while the action vocabulary is
    UPPERCASE throughout, so a raw `in` against either collection could never fail — the guard
    would have read green through exactly the mistake it exists to catch.
    """
    from featuregen.overlay.upload import semantic_eligibility_reasons as reasons

    registered = {name.upper() for name in reasons.REASON_FAMILIES}
    registered |= {name.upper() for name in reasons.SERVING_CAPABILITY_MATRIX_CODES}
    registered |= {name.upper() for name in vars(reasons) if not name.startswith("_")}
    assert CARDINALITY_EVIDENCE_REQUIRED.upper() not in registered


def test_the_closed_set_still_refuses_a_category_nobody_declared(conn) -> None:
    """Including `REALIZATION_ATTACHMENT_DEFECT` BY NAME (R6): an ops alert is a platform fault,
    and filing it here would attribute it to a feature and rank it beside real bridge work."""
    observation_id = _file(conn, "closed_set")
    for unknown in ("REALIZATION_ATTACHMENT_DEFECT", "cardinality_evidence",
                    "temporal_join_policy_missing", "allocation_policy_required"):
        assert record_bridge_demand(conn, observation_id=observation_id,
                                    rejections=[_rejection(verdict=unknown)]) == ()
        with pytest.raises(psycopg.errors.CheckViolation):
            with conn.transaction():
                conn.execute(
                    "INSERT INTO bridge_demand_observation (demand_id, observation_id, "
                    "demand_queue, demand_identity_hash, recipe_revision_hash, verdict) "
                    "VALUES ('bdo_unknown', %s, 'realization_gap', 'h', 'r', %s)",
                    (observation_id, unknown))


def test_the_python_map_and_the_sql_check_are_one_closed_set(conn) -> None:
    """The structural pin over 1120+1132: every member of the store's map inserts under the queue
    the map names and is REFUSED under every other queue, so a category added to Python without
    the DDL (or routed differently in the two places) fails here rather than in production."""
    observation_id = _file(conn, "one_set")
    for index, (verdict, queue) in enumerate(sorted(DEMAND_VERDICT_QUEUES.items())):
        with conn.transaction():
            conn.execute(
                "INSERT INTO bridge_demand_observation (demand_id, observation_id, demand_queue, "
                "demand_identity_hash, recipe_revision_hash, verdict) "
                "VALUES (%s, %s, %s, %s, 'r', %s)",
                (f"bdo_ok_{index}", observation_id, queue, f"h{index}", verdict))
        for wrong in (name for name in _QUEUES if name != queue):
            with pytest.raises(psycopg.errors.CheckViolation):
                with conn.transaction():
                    conn.execute(
                        "INSERT INTO bridge_demand_observation (demand_id, observation_id, "
                        "demand_queue, demand_identity_hash, recipe_revision_hash, verdict) "
                        "VALUES (%s, %s, %s, %s, 'r', %s)",
                        (f"bdo_bad_{index}", observation_id, wrong, f"x{index}", verdict))


def test_the_sql_check_names_no_verdict_the_store_map_lacks(conn) -> None:
    """The other direction: DDL that widened the set without telling Python would leave a verdict
    only SQL knows, and no writer could ever file it — dead vocabulary wearing a CHECK."""
    definition = conn.execute(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
        " WHERE conname = 'bridge_demand_verdict_chk'").fetchone()[0]
    quoted = {token.strip("'") for token in definition.split("'")[1::2]}
    assert quoted == set(DEMAND_VERDICT_QUEUES)


# ── 2) append-only physics survive the CHECK extension ─────────────────────────────────────────


def test_append_only_still_holds_on_the_demand_tables(conn) -> None:
    """1132 rewrites two CONSTRAINTS and nothing else; the 1120 triggers are untouched and this
    proves it rather than assuming it.

    TRUNCATE is pinned on the CHILD only, and the reason is A4's platform finding rather than an
    omission: `bridge_demand_observation` references `governed_planning_observation`, and Postgres
    refuses a TRUNCATE of a referenced table with `FeatureNotSupported` BEFORE that table's own
    `BEFORE TRUNCATE` raiser can fire. The parent's truncate-refusal is therefore unpinnable by
    the usual idiom; its UPDATE/DELETE raiser is pinned here, and 1120's own suite carries the rest.
    """
    observation_id = _file(conn, "append_only", rejections=[_rejection()])
    statements = ["TRUNCATE bridge_demand_observation"]
    for table in ("bridge_demand_observation", "governed_planning_observation"):
        statements += [f"UPDATE {table} SET recorded_at = now()", f"DELETE FROM {table}"]
    for statement in statements:
        with pytest.raises(psycopg.errors.RaiseException):
            with conn.transaction():
                conn.execute(statement)
    assert conn.execute(
        "SELECT count(*) FROM bridge_demand_observation WHERE observation_id = %s",
        (observation_id,)).fetchone()[0] == 1


# ── 3) the one-writer law (R5) ─────────────────────────────────────────────────────────────────


def test_exactly_one_module_writes_the_demand_table() -> None:
    """The law made STRUCTURAL. A second writer with its own INSERT could compute a different
    `demand_identity_hash` for the same crossing — two rows, one demand, a queue that
    double-counts and a rank nobody can trust. CI is where that is caught, because the unique key
    cannot see it: two different hashes are, to Postgres, two different demands.

    **The check is on MENTIONS, not on a spelling of the INSERT, and the reason is empirical.** The
    first version of this pin grepped the fixed literal ``INSERT INTO bridge_demand_observation``
    and a review probe walked straight through it: a writer whose SQL is a multi-line
    concatenation (``"INSERT INTO " "bridge_demand_observation ..."``) matches no such literal —
    and that is not a contrived shape, it is the shape of THIS module's own INSERT. Any
    case-insensitive regex over one statement form has the same weakness for the next spelling
    nobody predicted, so the pin is stated over the table NAME instead: only the store that writes
    it and the report that reads it may name it at all. A new module that mentions the table fails
    here and gets a deliberate decision, which is the outcome wanted either way.
    """
    root = Path(__file__).resolve().parents[4] / "src"
    assert root.is_dir(), root
    hits = subprocess.run(
        ["grep", "-rlniE", "--include=*.py", "bridge_demand_observation", str(root)],
        capture_output=True, text=True, check=False).stdout.split()
    assert sorted(Path(hit).name for hit in hits) == [
        "governed_lens.py",                  # prose only: names the table to say what it does NOT
                                             # serialize (`governed_lens.py:787`)
        "governed_observation_store.py",     # the ONE writer
        "governed_planning_report.py",       # a reader: per-queue rollups for the wave-1 report
    ]


def test_the_identity_material_has_exactly_one_owner(conn) -> None:
    """`demand_identity_hash` is exported so a would-be second writer must call it instead of
    re-deriving the material. What the store wrote and what the function returns are the same
    string, checked against the row rather than against a copy of the recipe."""
    observation_id = _file(conn, "one_owner")
    rejection = _rejection(verdict=CARDINALITY_EVIDENCE_REQUIRED)
    (demand_id,) = record_bridge_demand(conn, observation_id=observation_id,
                                        rejections=[rejection])
    stored = conn.execute(
        "SELECT demand_identity_hash FROM bridge_demand_observation WHERE demand_id = %s",
        (demand_id,)).fetchone()[0]
    assert stored == demand_identity_hash(rejection, anchor_catalog_source="core_banking")
    assert len(stored) == 64


def test_the_capacity_anchor_is_part_of_the_owned_recipe(conn) -> None:
    """The half that genuinely HAS a second owner, and the one the hop path cannot exercise.

    For a capacity verdict the anchor IS identity material, and a capacity rejection routinely
    carries a blank one — the run's anchor lives on the parent observation. So the anchor is a
    RESOLUTION, not a field, and a second writer passing the rejection's raw empty string mints a
    DIFFERENT hash for the same demand: the identity fork the one-writer law exists to prevent,
    reintroduced one layer above the hash. `resolve_demand_anchor` is exported beside
    `demand_identity_hash` so both halves are callable, and the divergence is demonstrated here
    rather than asserted."""
    observation_id = _file(conn, "capacity_anchor")
    rejection = _rejection(verdict="bounded_out_max_bridges", anchor_catalog_source="")
    (demand_id,) = record_bridge_demand(conn, observation_id=observation_id,
                                        rejections=[rejection])
    stored = conn.execute(
        "SELECT demand_identity_hash, demand_queue FROM bridge_demand_observation "
        " WHERE demand_id = %s", (demand_id,)).fetchone()
    assert stored[1] == "planner_capacity"

    resolved = resolve_demand_anchor(conn, observation_id=observation_id, rejection=rejection)
    assert resolved == "core_banking", "the anchor comes from the parent observation"
    assert stored[0] == demand_identity_hash(rejection, anchor_catalog_source=resolved)
    # ...and the trap the owned resolution closes: the raw field is NOT the anchor.
    assert stored[0] != demand_identity_hash(rejection, anchor_catalog_source="")


def test_the_anchor_resolution_leaves_a_hop_demand_alone(conn) -> None:
    """A non-capacity anchor is not identity material, so resolving one would spend a query to
    influence nothing — and would silently make two hop demands that differ only in their run's
    anchor look like two demands."""
    observation_id = _file(conn, "hop_anchor")
    rejection = _rejection(anchor_catalog_source="")
    assert resolve_demand_anchor(conn, observation_id=observation_id, rejection=rejection) == ""
    assert demand_identity_hash(rejection, anchor_catalog_source="") == \
        demand_identity_hash(rejection, anchor_catalog_source="somewhere_else")


def test_one_occurrence_files_one_countable_record_however_often_it_is_offered(conn) -> None:
    """Same occurrence, same demand, offered twice IN ONE CALL and again in a second call: one
    row, and — the half the unique key never covered — one COUNT. The two writers report
    `len(record_bridge_demand(...))` as "demands filed", so a repeated rejection used to inflate
    the telemetry number even though the ledger stayed correct."""
    observation_id = _file(conn, "one_writer")
    first = record_bridge_demand(conn, observation_id=observation_id,
                                 rejections=[_rejection(), _rejection(to_endpoint_hint="advisory")])
    second = record_bridge_demand(conn, observation_id=observation_id, rejections=[_rejection()])
    assert len(first) == 1, "one occurrence, one demand — the count may not double"
    assert second == first
    assert conn.execute(
        "SELECT count(*) FROM bridge_demand_observation WHERE observation_id = %s",
        (observation_id,)).fetchone()[0] == 1


def test_two_genuine_occurrences_stay_two_countable_records(conn) -> None:
    """The other half of R5: dedupe must NOT erase recurrence. Two runs that both needed the same
    crossing are two demands — that is the signal the queue exists to rank."""
    for suffix in ("recur_a", "recur_b"):
        _file(conn, suffix, rejections=[_rejection()])
    groups = observation_queues(conn)["queues"]["realization_gap"]
    assert len(groups) == 1
    assert groups[0]["demand_rows"] == 2
    assert groups[0]["distinct_runs"] == 2
    assert conn.execute(
        "SELECT count(DISTINCT demand_identity_hash) FROM bridge_demand_observation").fetchone()[0] \
        == 1, "one demand IDENTITY, twice — recurrence is countable, not deduplicated away"


# ── 4) the satisfaction projection ─────────────────────────────────────────────────────────────


def test_a_satisfied_demand_leaves_the_unresolved_view_and_keeps_its_history(conn) -> None:
    """Satisfaction is a PROJECTION: the later resolved occurrence of the same governed variant is
    the evidence, and the demand row it settles is still there afterwards, byte for byte."""
    variant = "gvar_" + "b" * 64
    observation_id = _occurrence_at(conn, "sat_open", minutes=-10, variant=variant,
                                    status="unresolved", rejections=[_rejection()])
    before = demand_satisfaction(conn)
    assert [row["demand_identity_hash"] for row in before["unresolved"]] == [
        conn.execute("SELECT demand_identity_hash FROM bridge_demand_observation "
                     " WHERE observation_id = %s", (observation_id,)).fetchone()[0]]
    assert before["satisfied"] == []

    _file(conn, "sat_done", variant=variant, status="resolved")
    after = demand_satisfaction(conn)
    assert after["unresolved"] == []
    assert len(after["satisfied"]) == 1
    assert after["satisfied"][0]["occurrences"] == 1
    assert after["satisfied"][0]["satisfied_at"] is not None
    assert after["totals"] == {"demand_identities": 1, "unresolved": 0, "satisfied": 1,
                               "unresolved_demand_rows": 0, "satisfied_demand_rows": 1}
    assert conn.execute(
        "SELECT count(*) FROM bridge_demand_observation WHERE observation_id = %s",
        (observation_id,)).fetchone()[0] == 1, "history survives satisfaction"


def test_a_demand_filed_again_after_the_resolution_is_unresolved_again(conn) -> None:
    """The projection is over HISTORY, not a flag: a crossing that broke again is outstanding
    again, and its earlier satisfaction never rewrote anything to say otherwise."""
    variant = "gvar_" + "c" * 64
    _occurrence_at(conn, "reopen_1", minutes=-20, variant=variant, status="unresolved",
                   rejections=[_rejection()])
    _occurrence_at(conn, "reopen_2", minutes=-10, variant=variant, status="resolved")
    _file(conn, "reopen_3", variant=variant, rejections=[_rejection()])
    projection = demand_satisfaction(conn)
    assert projection["satisfied"] == []
    assert len(projection["unresolved"]) == 1
    assert projection["unresolved"][0]["occurrences"] == 2, "both filings stay countable"


def test_satisfaction_never_crosses_the_live_and_telemetry_lanes(conn) -> None:
    """A telemetry replan that succeeded says nothing about what the request path could do. The
    two lanes share a table and never share a verdict (`observation_mode` is in the key)."""
    variant = "gvar_" + "d" * 64
    _occurrence_at(conn, "lane_live", minutes=-10, mode="live", variant=variant,
                   status="unresolved", rejections=[_rejection()])
    _file(conn, "lane_tele", mode="telemetry", variant=variant, status="resolved")
    assert len(demand_satisfaction(conn)["unresolved"]) == 1


def test_the_projection_reads_as_of_and_can_be_filtered_to_one_queue(conn) -> None:
    variant = "gvar_" + "e" * 64
    _occurrence_at(conn, "asof_open", minutes=-10, variant=variant, status="unresolved",
                   rejections=[_rejection()])
    _file(conn, "asof_done", variant=variant, status="resolved")
    past = demand_satisfaction(conn, as_of=_NOW - timedelta(days=3650))
    assert past["unresolved"] == [] and past["satisfied"] == []
    assert demand_satisfaction(conn, queue="planner_capacity")["satisfied"] == []
    assert len(demand_satisfaction(conn, queue="realization_gap")["satisfied"]) == 1
    # An unknown queue must REFUSE, never return an empty projection: "nothing is outstanding" is
    # the most dangerous wrong answer a governance queue can give.
    with pytest.raises(ValueError, match="queue must be one of"):
        demand_satisfaction(conn, queue="realisation_gap")


# ── 5) provisional is not absent ───────────────────────────────────────────────────────────────


def _append_provisional(conn, *, cardinality: Cardinality | None) -> str:
    """One A4c-shaped revision: SANDBOX tier, no current pointer, appended through the append
    half. `cardinality=None` is the producer's UNKNOWN verdict."""
    left, right = _executable_pair()
    base = _realization(
        left, right,
        cardinality=DirectionalCardinalityVerdictV1(cardinality))
    dependencies = (BridgeDependencyRefV1("bridge_fact", base.bridge_fact_key, "head-1"),)
    revision = replace(
        base,
        applicability_scope=replace(base.applicability_scope,
                                    execution_tier=ExecutionTier.SANDBOX,
                                    purposes=("feature_generation",)),
        dependency_snapshot_id=bridge_dependency_snapshot_id(dependencies))
    append_realization_revision(conn, revision, dependencies=dependencies)
    return revision.bridge_fact_key


def _segment(bridge_fact_key: str) -> dict:
    return {"segment_kind": "governed_bridge", "bridge_fact_key": bridge_fact_key,
            "from_entity": "transaction", "to_entity": "account",
            "relationship_id": "", "relationship_version": "",
            "bridge_from_catalog_source": "ops",
            "bridge_from_object_ref": "public.transactions.account_id",
            "bridge_to_catalog_source": "rev",
            "bridge_to_object_ref": "public.accounts.account_id",
            "has_realization_revision": False}


def test_the_store_reads_revisions_that_no_current_pointer_publishes(conn) -> None:
    """A4c writes the revision half and never the CAS-publish half, so every reader that goes
    through `bridge_join_realization_current` is blind to its output."""
    key = _append_provisional(conn, cardinality=None)
    revisions = realization_revisions_for_bridge(conn, bridge_fact_key=key)
    assert len(revisions) == 1
    assert revisions[0].applicability_scope.execution_tier is ExecutionTier.SANDBOX
    assert not revisions[0].cardinality.known
    assert realization_revisions_for_bridge(
        conn, bridge_fact_key=key, execution_tier=ExecutionTier.PRODUCTION) == ()
    assert realization_revisions_for_bridge(
        conn, bridge_fact_key=key, purpose="analysis") == ()


def test_an_unknown_cardinality_realization_does_not_read_as_no_realization(conn) -> None:
    """THE distinction A7 exists to draw. Before it, A4c's provisional revision and an empty store
    produced the identical demand row — "go build a realization" filed against a realization that
    already exists and only needs its cardinality measured."""
    key = _append_provisional(conn, cardinality=None)
    demands = demands_for_rejection(
        conn, {"reason_codes": ["physical_cardinality_unavailable"],
               "anchor_catalog_source": "ops", "evidence": [_segment(key)]},
        recipe_revision_hash="rev-hash")
    assert len(demands) == 1
    assert demands[0]["realizers"][0]["realization_state"] == \
        REALIZATION_PROVISIONAL_UNKNOWN_CARDINALITY
    assert demands[0]["verdict"] == CARDINALITY_EVIDENCE_REQUIRED


def _state_and_verdict(conn, key: str) -> tuple[str, str]:
    demands = demands_for_rejection(
        conn, {"reason_codes": ["physical_cardinality_unavailable"],
               "anchor_catalog_source": "ops", "evidence": [_segment(key)]},
        recipe_revision_hash="rev-hash")
    assert len(demands) == 1
    return demands[0]["realizers"][0]["realization_state"], demands[0]["verdict"]


def test_a_proven_revision_with_no_pointer_is_an_attachment_not_a_measurement(conn) -> None:
    """The branch that came free with the second read, now pinned rather than assumed.

    A revision the CAS-publish half never published is invisible to `executable_bridge_realizations`
    whatever its cardinality — so before A7 a PROVEN, unattached realization also read as "nothing
    exists". It is not a measurement job: the measurement is done, and what remains is attaching."""
    key = _append_provisional(conn, cardinality=Cardinality.MANY_TO_ONE)
    assert realization_revisions_for_bridge(conn, bridge_fact_key=key)[0].cardinality.known
    assert _state_and_verdict(conn, key) == (REALIZATION_EXISTS_UNATTACHED, "missing_realization")


def test_one_proven_revision_outranks_an_earlier_provisional_one(conn) -> None:
    """PRECEDENCE, and it is the normal case rather than a corner: A4c mints an IMMUTABLE revision
    per candidate, so a bridge routinely carries several.

    Testing "any revision is unproven" would let the first provisional permanently outrank every
    later proof — filing "go measure it" against work already done, which is the exact
    mis-addressed ticket this state was added to eliminate. One proof settles the question for the
    bridge; only when EVERY stored revision is unproven is the work a measurement."""
    key = _append_provisional(conn, cardinality=None)
    assert _state_and_verdict(conn, key) == (
        REALIZATION_PROVISIONAL_UNKNOWN_CARDINALITY, CARDINALITY_EVIDENCE_REQUIRED)

    assert _append_provisional(conn, cardinality=Cardinality.MANY_TO_ONE) == key
    assert len(realization_revisions_for_bridge(conn, bridge_fact_key=key)) == 2
    assert _state_and_verdict(conn, key) == (REALIZATION_EXISTS_UNATTACHED, "missing_realization")


def test_an_absent_realization_still_reads_as_no_revision_exists(conn) -> None:
    """The contrast that makes the distinction mean something."""
    demands = demands_for_rejection(
        conn, {"reason_codes": ["physical_cardinality_unavailable"],
               "anchor_catalog_source": "ops", "evidence": [_segment("bfk_absent")]},
        recipe_revision_hash="rev-hash")
    assert demands[0]["realizers"][0]["realization_state"] == REALIZATION_NONE
    assert demands[0]["verdict"] == "missing_realization"


def test_the_provisional_demand_lands_in_the_realization_gap_queue(conn) -> None:
    """End to end: A4c's output produces a demand row the CHECK accepts, in the queue whose
    meaning matches the work (`realization_gap`), distinguishable from a build ticket by verdict."""
    key = _append_provisional(conn, cardinality=None)
    demands = demands_for_rejection(
        conn, {"reason_codes": ["physical_cardinality_unavailable"],
               "anchor_catalog_source": "ops", "evidence": [_segment(key)]},
        recipe_revision_hash="rev-hash")
    observation_id = _file(conn, "provisional_queue", rejections=demands)
    rows = conn.execute(
        "SELECT demand_queue, verdict FROM bridge_demand_observation WHERE observation_id = %s",
        (observation_id,)).fetchall()
    assert rows == [("realization_gap", CARDINALITY_EVIDENCE_REQUIRED)]
