"""The mutation gate: every registered break must be caught, and the no-op control must not be.

    uv run --extra dev pytest -q -m eval tests/eval/mutation/

Each must-die mutation gets one parametrized test that (a) proves the victims PASS unmutated and
(b) requires at least one to FAIL under the mutation. The must-survive control requires the victims
to keep passing — without it a harness that broke everything unconditionally would report a perfect
kill rate.

Also here: the literal test-count baseline for the NAMED release-gate suites (D9 — never the whole
repository, whose ~82 order/environment-dependent failures are recorded in DEFERRED-WORK §C).
"""
from __future__ import annotations

import pytest
from tests.eval.mutation import harness, mutations

pytestmark = pytest.mark.eval

# Each case runs two child pytest sessions; the outer per-test timeout must allow for that.
_TIMEOUT = 600


@pytest.fixture(scope="session")
def dsn(_dsn) -> str:
    """The parent session's already-migrated database, handed to every child."""
    return _dsn


@pytest.mark.timeout(_TIMEOUT)
@pytest.mark.parametrize("mutation", mutations.must_die(), ids=lambda m: m.mutation_id)
def test_the_mutation_is_caught(mutation: mutations.Mutation, dsn: str) -> None:
    control = harness.baseline(mutation, dsn=dsn)
    harness.assert_ran(control, what=f"{mutation.mutation_id} baseline")
    assert control.all_passed, (
        f"{mutation.mutation_id}: its victims do not pass UNMUTATED, so a failure under the "
        f"mutation would prove nothing.\n{control.tail}")

    mutated = harness.run_victims(mutation.victims, dsn=dsn, mutation_id=mutation.mutation_id)
    harness.assert_ran(mutated, what=f"{mutation.mutation_id} mutated")
    assert mutated.any_failed, (
        f"{mutation.mutation_id} SURVIVED. Nothing noticed that "
        f"'{mutation.invariant}' stopped holding — the invariant is claimed but not tested.\n"
        f"target: {mutation.target}\nvictims: {list(mutation.victims)}\n{mutated.tail}")

    # A kill is the EXPECTED failure, not any failure. Two independent checks, because "the run went
    # red" is satisfied by a broken import, a malformed query or an unrelated fixture blowing up —
    # none of which is evidence that anything noticed the invariant break.
    named = [node for node in mutated.failed_node_ids
             if any(node.startswith(victim) for victim in mutation.victims)]
    assert named, (
        f"{mutation.mutation_id}: the run failed, but none of the NAMED victims did — the failures "
        f"are collateral.\nfailed: {list(mutated.failed_node_ids)}\n"
        f"victims: {list(mutation.victims)}\n{mutated.tail}")
    assert mutation.expect_failure_contains in mutated.output, (
        f"{mutation.mutation_id}: its victims failed, but not with the failure this mutation is "
        f"supposed to cause. Expected to find {mutation.expect_failure_contains!r} in the output. "
        f"Either the mutation is now killing for an unrelated reason (a broken import, a malformed "
        f"query), or the victim was rewritten and the registry must be re-derived from a real "
        f"run.\nfailed: {list(mutated.failed_node_ids)}\n{mutated.tail}")


@pytest.mark.timeout(_TIMEOUT)
@pytest.mark.parametrize("mutation", mutations.must_survive(), ids=lambda m: m.mutation_id)
def test_the_no_op_control_survives(mutation: mutations.Mutation, dsn: str) -> None:
    """The harness must be able to PASS. A semantically neutral change kills nothing."""
    mutated = harness.run_victims(mutation.victims, dsn=dsn, mutation_id=mutation.mutation_id)
    harness.assert_ran(mutated, what=f"{mutation.mutation_id} control")
    assert mutated.all_passed, (
        f"{mutation.mutation_id} was supposed to be a NO-OP and killed its victims. Every must-die "
        f"result from this harness is now suspect: the harness reports noise.\n{mutated.tail}")


def test_the_registry_covers_every_invariant_the_plans_named() -> None:
    """The plans name the mutations by hand; this pins the list so one cannot quietly vanish."""
    required = {
        "vocab_fingerprint_drops_hint",
        "issuer_leaves_namespace_identity",
        "entity_reenters_the_blocking_key",
        "accept_off_registry_concept",
        "disable_stale_value_retirement",
        "feature_gen_reads_the_thin_menu",
        "drop_per_kind_truncation_reporting",
        "review_status_implies_executable",
        "collapse_data_and_authority_role",
        "catalog_authority_inherits_onto_a_dataset",
        "llm_authority_becomes_load_bearing",
        "search_drops_profile_context",
        "feature_gen_drops_profile_context",
        "retrieval_drops_profile_context",
        "graph_projection_read_as_authority",
        "profile_hash_omits_business_context",
        "disable_the_feature_use_gate",
        # D14's other half, and the one the whole allow-policy surface rests on: approving a policy
        # is only safe because REVOKING it takes effect immediately. Pinned as REQUIRED because the
        # gate-disabled entry above does not cover it — a gate that runs, reads the pointer and
        # ignores the status passes every check `disable_the_feature_use_gate` makes.
        "gate_ignores_policy_revocation",
        # Release C Task 13 names these eight by hand. Every one of them produces a silently WRONG
        # NUMBER rather than an error — a mapping table joined without its row rule, one leg
        # dropped, a direction gated on the other direction's evidence — so a pipeline that runs
        # and publishes is the failure mode, not a stack trace.
        "crosswalk_label_treated_as_executable",
        "crosswalk_renders_endpoint_equality",
        "crosswalk_leg_omitted_from_read_authorization",
        "uniqueness_measured_before_time_filtering",
        "crosswalk_cardinality_inverted",
        "crosswalk_deduplicates_instead_of_refusing",
        "review_substitutes_for_crosswalk_safety",
        "crosswalk_identity_omits_mapping_revision",
    }
    registered = {m.mutation_id for m in mutations.REGISTRY}
    missing = required - registered
    assert missing == set(), f"required mutations absent from the registry: {sorted(missing)}"
    assert mutations.must_survive(), "a harness with no must-survive control cannot prove it passes"
    for m in mutations.dropped():
        assert m.dropped_reason, f"{m.mutation_id} is dropped with no recorded reason"


def test_every_mutation_names_at_least_one_victim() -> None:
    for m in mutations.REGISTRY:
        assert m.victims, f"{m.mutation_id} names no victim, so it can neither die nor survive"
        assert m.invariant, f"{m.mutation_id} does not say what guarantee it breaks"


def test_every_must_die_mutation_declares_the_failure_it_expects() -> None:
    """No must-die entry may fall back to 'any failure counts'.

    A mutation with no expected failure is scored by exit code alone, so a broken import or a
    malformed query reads as a caught invariant. This keeps the field mandatory for the kind of
    mutation that is graded on failing.
    """
    for m in mutations.must_die():
        assert m.expect_failure_contains, (
            f"{m.mutation_id} declares no expect_failure_contains, so ANY failure would count as a "
            f"kill. Run its victims under the mutation and record a substring of the real failure.")


# ── the literal baseline count for the NAMED release-gate suites (D9) ────────────────────────────

#: The Task-0 seventeen, plus the suites this evaluation step added. NAMED, never the whole repo.
RELEASE_GATE_SUITES: tuple[str, ...] = (
    "tests/featuregen/overlay/upload/test_enrich.py",
    "tests/featuregen/overlay/upload/test_enrich_llm.py",
    "tests/featuregen/overlay/upload/attest/test_concept_critic.py",
    "tests/featuregen/overlay/upload/test_concept_critic_acceptance.py",
    "tests/featuregen/overlay/upload/test_feature_menu_enrichment.py",
    "tests/featuregen/overlay/upload/test_asset_detail_dossier.py",
    "tests/featuregen/overlay/upload/test_entity_map.py",
    "tests/featuregen/analysis/test_retrieval.py",
    "tests/featuregen/overlay/upload/test_field_resolution.py",
    "tests/featuregen/overlay/upload/test_table_synth_assemble.py",
    "tests/featuregen/overlay/upload/test_table_synth_propose.py",
    "tests/featuregen/overlay/upload/test_table_synth_wide.py",
    "tests/featuregen/overlay/upload/test_search.py",
    "tests/featuregen/overlay/upload/test_feature_assist.py",
    "tests/featuregen/overlay/upload/test_feature_metadata_snapshot.py",
    "tests/featuregen/materialize/test_joins.py",
    "tests/featuregen/data_agent/test_analysis_ir.py",
)

#: Measured on this branch, 2026-08-03. The Task-0 record put the same seventeen at 238; the
#: Release-A integration added 31. A DROP here is a deleted guard and must be explained, not
#: rebaselined silently.
RELEASE_GATE_BASELINE = 277  # rebaselined 2026-08-05 (Release C Task 13): +4 crosswalk adapter
# tests in test_joins.py — `plan_crosswalk_join` lives in that module and had NO test there at all
# (its coverage was entirely in test_crosswalk_ir.py, which drives it THROUGH the IR). The four go
# at the adapter directly and are the victims for three of Task 13's eight required mutations:
# endpoint-equality collapse, inverted directional cardinality, and refuse-rather-than-deduplicate.
# A deliberate rise, per the gate's contract. Previous: 273 (2026-08-03), 238 (Task-0 baseline).

#: The suites this evaluation step owns, and their literal counts. Asserted below, like
#: RELEASE_GATE_BASELINE — an unread literal in a test module is a claim nobody checks, and this one
#: was exactly that until the review of 2026-08-03.
EVAL_SUITES: dict[str, int] = {
    "tests/eval/test_gold_sets_are_consistent.py": 63,
    "tests/eval/test_release_a_eval.py": 4,
    # 16 passed. It was 15 passed + 1 strict xfail until the USE gate landed and promoted bar 4 to
    # a real bar; the COUNT is unchanged because a strict xfail was always collected.
    "tests/eval/test_release_a_bars.py": 16,
}


@pytest.mark.timeout(_TIMEOUT)
@pytest.mark.parametrize("suite", sorted(EVAL_SUITES), ids=lambda s: s.rsplit("/", 1)[-1])
def test_the_eval_suites_hold_their_literal_count(suite: str, dsn: str) -> None:
    """The evaluation's OWN suites, held to the same literal-count rule as the seventeen.

    The bars file is `eval`-marked, so it is deselected by `addopts` in a default run; `run_victims`
    clears the marker filter, which is why this can assert a real count rather than a zero. The
    strict xfail (bar 4) is counted as collected — it is a recorded finding, not an absent test.
    """
    result = harness.run_victims((suite,), dsn=dsn)
    harness.assert_ran(result, what=f"{suite} baseline")
    assert result.all_passed, result.tail
    assert result.collected == EVAL_SUITES[suite], (
        f"{suite} collected {result.collected}, baseline {EVAL_SUITES[suite]}. A DROP is a deleted "
        f"guard; a RISE is fine but must be rebaselined deliberately.\n{result.tail}")


@pytest.mark.timeout(_TIMEOUT)
def test_the_named_release_gate_suites_hold_their_literal_count(dsn: str) -> None:
    result = harness.run_victims(RELEASE_GATE_SUITES, dsn=dsn)
    harness.assert_ran(result, what="release-gate baseline")
    assert result.all_passed, result.tail
    assert result.collected == RELEASE_GATE_BASELINE, (
        f"the named release-gate suites collected {result.collected}, baseline "
        f"{RELEASE_GATE_BASELINE}. A DROP is a deleted guard; a RISE is fine but must be "
        f"rebaselined deliberately.\n{result.tail}")
