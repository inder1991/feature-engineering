"""The mutation registry — one entry per invariant the Release-A gate claims to hold.

A mutation is a small, surgical break of ONE guarantee, plus the NAMED tests that are supposed to
notice. The harness runs those tests in a subprocess with the mutation applied and requires them to
FAIL; the must-survive control requires the opposite. A mutation nothing notices is a guarantee
nobody is actually testing, and that is the finding the whole exercise exists to produce.

WHY EACH MUTATION IS APPLIED WHERE IT IS. Every target below is read at CALL time from its own
module's globals (`policy_for` reads `_POLICIES`, `enrich_concepts` passes `_accept_concept` by
name, `_relationship_dict` looks up `_executable_realization_ids`, and so on), so a `setattr` on
the module before collection genuinely changes what the code under test does. Where a symbol is
bound into another module by `from X import Y`, the mutation patches the CONSUMER, not the
producer — patching the producer would be a mutation that mutates nothing and would then "pass"
by killing its victims for no reason.

NOTHING HERE TOUCHES src/. Every mutation is a runtime patch installed by the plugin at
`pytest_configure` in a throwaway subprocess.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

MUST_DIE = "must_die"
MUST_SURVIVE = "must_survive"


@dataclass(frozen=True)
class Mutation:
    mutation_id: str
    kind: str                        # must_die | must_survive
    invariant: str                   # the guarantee this breaks, in one sentence
    target: str                      # module:symbol, for the report
    victims: tuple[str, ...]         # pytest node ids expected to FAIL (must_die) / PASS (survive)
    apply: Callable[[], None]
    #: A substring of the failure the victim is expected to produce — the assertion's own message
    #: where it has one, otherwise the failing source line as pytest renders it. REQUIRED for
    #: must-die.
    #:
    #: Without it, "the run failed" is the whole kill criterion, and a mutation that merely broke an
    #: import, malformed a query or tripped an unrelated fixture would be scored as a caught
    #: invariant. Every string below was taken from an actual mutated run, not composed. It earned
    #: its keep immediately: `retrieval_drops_profile_context` was killing its victims with
    #: `psycopg.errors.SyntaxError: syntax error at or near "FROM"` — a malformed SELECT, not a
    #: missing harvest — and was rewritten to break the invariant instead of the SQL.
    expect_failure_contains: str = ""
    notes: str = ""
    dropped_reason: str = field(default="")


# ── the mutations ────────────────────────────────────────────────────────────────────────────────


def _m_fingerprint_drops_hint() -> None:
    """The classifier replay fingerprint stops hashing the concept HINT.

    A reworded concept meaning then replays a verdict reached against the OLD meaning forever.
    """
    from featuregen.overlay.field_evidence import canonical_hash
    from featuregen.overlay.upload import enrich

    def _blind() -> str:
        return canonical_hash({
            "vocabulary": sorted(({"name": c["name"], "group": c["group"]}
                                  for c in enrich._CONCEPT_VOCABULARY),
                                 key=lambda e: e["name"]),
            "contract": enrich._classifier_contract_fingerprint(),
        })[:12]

    enrich._vocab_fingerprint = _blind


def _m_issuer_never_resolves() -> None:
    """Issuer leaves the namespace pairing identity: every scope resolves `unresolved`.

    Two banks' `cif` registries are disjoint worlds; without the issuer they read as one.
    """
    from featuregen.overlay.upload.attest import bridge_grounding

    bridge_grounding.resolve_identifier_issuer = lambda *_a, **_k: (None, "unresolved")


def _m_entity_reenters_the_blocking_key() -> None:
    """Entity is folded back into the identifier-pairing block key.

    D12.1-revised: a counterparty MAY be our customer — same cif registry, different entity label.
    Blocking on entity makes that pair unreachable, which is the defect the revision removed.
    """
    from featuregen.overlay.upload import bridge_candidates as bc

    original = bc._identifier_columns

    def _entity_scoped(conn, *, roles):
        from dataclasses import replace
        return [replace(col, namespace=f"{col.namespace}|{col.entity}")
                for col in original(conn, roles=roles)]

    bc._identifier_columns = _entity_scoped


def _m_accept_free_text_concept() -> None:
    """The classifier acceptance seam admits any string as a concept."""
    from featuregen.overlay.upload import enrich

    enrich._accept_concept = lambda raw: ((raw.strip() or None), "valid")


def _m_disable_stale_value_retirement() -> None:
    """LLM stale-value retirement becomes a no-op, so superseded proposals stay live."""
    from featuregen.overlay.upload import enrich

    enrich.stale_all_llm_field_evidence = lambda conn, **_kw: 0


def _m_drop_per_kind_truncation_reporting() -> None:
    """The context section stops reporting WHICH kinds it omitted, keeping only the boolean."""
    from featuregen.overlay.upload import lineage

    lineage.TruncationReportV1.as_dict = lambda self: {"truncated": self.truncated, "omitted": {}}


def _m_review_status_implies_executable() -> None:
    """`executable_now` is answered from the STORED record instead of the revalidating reader."""
    from featuregen.overlay.upload import context_graph

    context_graph._executable_realization_ids = lambda conn, ref: frozenset()

    original = context_graph._relationship_dict

    def _review_is_permission(conn, link):
        payload = original(conn, link)
        approved = link.review_status in {"approved", "verified", "human_verified"}
        payload["executable_now"] = approved or payload["executable_now"]
        for entry, realization in zip(payload["realizations"], link.realizations):
            entry["executable_now"] = (
                entry["executable_now"] or (approved and realization.production_eligible))
        return payload

    context_graph._relationship_dict = _review_is_permission


def _m_collapse_data_and_authority_role() -> None:
    """`authority_role` is resolved from the SAME evidence field as the data role.

    One axis answering two questions: a table whose KIND is declared then reports an AUTHORITY
    nobody asserted.
    """
    from featuregen.overlay.upload import dataset_profiles as dp

    dp._PROFILE_FIELD_SOURCES = {**dp._PROFILE_FIELD_SOURCES, "authority_role": "table_role"}


def _m_catalog_authority_inherits() -> None:
    """The catalog narrative is allowed to carry `authority_role`, so a catalog-level claim can
    reach a dataset."""
    from featuregen.overlay.upload import catalog_profiles as cp

    cp.NARRATIVE_KEYS = frozenset({*cp.NARRATIVE_KEYS, "authority_role"})


def _m_llm_authority_becomes_load_bearing() -> None:
    """The operational rule for the profile classifications admits an LLM proposal."""
    from dataclasses import replace

    from featuregen.overlay import field_authority as fa
    from featuregen.overlay.upload import field_policies as fp

    llm_ok = fa.AnyOf((*fp._SOURCE_OR_HUMAN.conditions, fp._LLM_PROPOSED))
    fp._POLICIES = {
        name: (replace(policy, operational_rule=llm_ok)
               if name in ("authority_role", "temporal_storage_model") else policy)
        for name, policy in fp._POLICIES.items()
    }


def _m_search_drops_profile_context() -> None:
    """Search stops offering the profile facets even with the flag on."""
    from featuregen.overlay.upload import search

    search.column_facets = lambda: dict(search._COLUMN_FACETS)


def _m_feature_gen_drops_profile_context() -> None:
    """The feature-context table block stops carrying the profile advisories."""
    from featuregen.overlay.upload import feature_assist as fa

    fa._profile_advisories = lambda members: {}


def _m_retrieval_drops_profile_context() -> None:
    """Leg 3 stops harvesting the table profile projections.

    The projected columns are replaced by a literal NULL rather than emptied. Emptying the tuple was
    the first shape of this mutation and it killed its victims with
    `psycopg.errors.SyntaxError: syntax error at or near "FROM"` — the harvest query became
    `SELECT catalog_source, table_name,  FROM graph_node`, so the victims died of a malformed
    statement and never reached the question. The NULL keeps the SELECT well-formed and the rows
    read; `_harvest` then discards every falsy value, so what is broken is exactly the invariant:
    leg 3 runs, and harvests no profile vocabulary.
    """
    from featuregen.analysis import retrieval

    retrieval._TABLE_PROFILE_COLUMNS = ("NULL",)


def _m_graph_projection_read_as_authority() -> None:
    """Feature eligibility is read off the flat display projection instead of the decision log."""
    from featuregen.overlay.upload import field_resolution as fr

    def _projection_is_authority(conn, logical_ref: str, field_name: str) -> bool:
        column = fr._DISPLAY_COLUMN.get(field_name)
        if column is None:
            return True
        source = logical_ref.split("::", 1)[0]
        key = fr._graph_key(source, logical_ref)
        row = conn.execute(
            f"SELECT {column} FROM graph_node WHERE catalog_source = %s AND object_ref = %s",
            (source, key)).fetchone()
        return bool(row and row[0])

    fr.is_feature_eligible = _projection_is_authority


def _m_profile_hash_omits_a_meaning_bearing_field() -> None:
    """`dataset_profile_hash` stops covering `business_context`, so a narrative edit is invisible."""
    from featuregen.overlay.upload import dataset_profiles as dp

    dp._EFFECTIVE_FIELDS = tuple(f for f in dp._EFFECTIVE_FIELDS if f != "business_context")


def _m_noop_reorder_registry() -> None:
    """MUST SURVIVE. Reverse the concept registry's declaration order and re-insert every dict key.

    Semantically a no-op: both fingerprints are canonical over SORTED members precisely so that
    moving a `Concept(...)` line cannot invalidate every cached classification on the platform. If
    this kills its victims, the harness is reporting noise and no must-die result from it can be
    trusted.
    """
    from featuregen.overlay.upload import concepts as concepts_mod
    from featuregen.overlay.upload import enrich
    from featuregen.overlay.upload.attest import concept_critic

    registry = dict(concepts_mod.CONCEPT_REGISTRY)
    for key in list(registry):
        registry[key] = registry.pop(key)
    concepts_mod.CONCEPT_REGISTRY = registry
    concepts_mod._ALL = tuple(reversed(concepts_mod._ALL))
    concept_critic.CONCEPT_REGISTRY = registry
    enrich._CONCEPT_VOCABULARY = list(concepts_mod.classification_vocabulary())


def _m_disable_the_feature_use_gate() -> None:
    """The USE gate stops asking, and a visible column is a usable one again.

    This is the Release-A finding restored in one line: `_validate_idea` calls `_use_gate`, which
    returns None immediately when the flag is off, so flipping the reader reproduces exactly the
    state bar 4 was a strict xfail against — a PII column, a protected characteristic, a
    currency-blind amount and a free-text label all accepted as DESIGN_CHECKED with zero
    requirements. Nothing else changes: leakage, staleness, numeric type, additivity, units,
    point-in-time, grain and join authority all run unaltered.

    Patched on the CONSUMER's own module, which is also the producer here (`feature_assist` reads
    its own `feature_use_gate_enabled` through module globals at call time), so the setattr
    genuinely changes what the code under test does.
    """
    from featuregen.overlay.upload import feature_assist

    feature_assist.feature_use_gate_enabled = lambda: False


def _m_gate_ignores_policy_revocation() -> None:
    """The USE gate stops asking whether a data-use policy is still ACTIVE.

    The half of the D14 loop that makes approving one safe. A policy that goes on licensing a
    personal-data operand after it is withdrawn is worse than no policy at all: the governance
    screen would show `revoked`, the feature flow would go on building, and the two surfaces would
    disagree about the same decision with nobody able to see which one was lying.

    Broken by REPLACING the gate's reader with one that resolves the current pointer and returns
    whatever revision it names, status and content verification alike unread — the shape of a
    "simplification" somebody could plausibly write, not a syntax break. Everything else is
    untouched: absence still refuses, so a concept nobody declared is unaffected, and the ONLY
    behavioural difference is that a revoked policy keeps clearing.

    Patched on the CONSUMER's own module: `feature_assist` binds the reader by name at import and
    calls it through its own globals, so this setattr genuinely changes what the code under test
    does. Patching `pii_policy_store` would mutate nothing.
    """
    from featuregen.overlay.upload import feature_assist

    def _any_current(conn, concept_names):
        names = sorted({n.strip().lower() for n in concept_names
                        if isinstance(n, str) and n.strip()})
        if not names:
            return {}
        rows = conn.execute(
            "SELECT r.concept_name, r.revision_id FROM pii_use_policy_current c "
            "JOIN pii_use_policy_revision r ON r.revision_id = c.revision_id "
            "WHERE c.concept_name = ANY(%s)", (names,)).fetchall()
        return dict(rows)

    feature_assist.active_pii_use_policies = _any_current


# ── Release C Task 13: the eight crosswalk mutations the plan names ─────────────────────────────
#
# A crosswalk is the shape where every one of these produces a SILENTLY WRONG NUMBER rather than an
# error: a mapping table joined without its row rule, one leg dropped, a direction gated on the
# other direction's evidence — each yields a pipeline that runs, publishes and is wrong. That is
# why the plan enumerates them by hand and why they are registered as REQUIRED.


def _m_crosswalk_label_treated_as_executable() -> None:
    """A crosswalk's `executable_now` is answered from its KIND instead of from execution wiring.

    The oldest and most tempting error on this surface: the row already says "crosswalk", the
    mapping table is named, both legs are shown — so it looks like something the platform can run.
    It is not, and the extension carries `executable_now` explicitly rather than deriving it from
    any verdict for exactly this reason.

    Patched on `context_graph`'s own `_relationship_dict`, which the section builder calls through
    module globals at request time.
    """
    from featuregen.overlay.upload import context_graph

    original = context_graph._relationship_dict

    def _label_is_capability(conn, link):
        payload = original(conn, link)
        if payload.get("crosswalk") is not None:
            payload["crosswalk"]["executable_now"] = True
            payload["executable_now"] = True
        return payload

    context_graph._relationship_dict = _label_is_capability


def _m_crosswalk_renders_endpoint_equality() -> None:
    """The two legs collapse into ONE direct endpoint-to-endpoint join.

    "These ids are related through this table" becomes "these ids are equal". The mapping table
    disappears from the plan, and `cib.acct_no` is joined straight onto
    `ftr.counter_party_acct_no` — two identifier schemes that were never the same value space. The
    result is empty or nonsense and nothing raises.

    Patched on the CONSUMER (`expression_ir` binds `plan_crosswalk_join` by name at import) AND on
    the producer module, so both the IR path and a direct adapter call see it.
    """
    from dataclasses import replace

    from featuregen.materialize import expression_ir, joins
    from featuregen.materialize.codes import MaterializationRefused

    original = joins.plan_crosswalk_join

    def _one_direct_step(admitted, **kwargs):
        plan = original(admitted, **kwargs)
        if isinstance(plan, MaterializationRefused) or len(plan.steps) != 2:
            return plan
        first, second = plan.steps
        # ONE step, straight from the source endpoint to the target endpoint — the mapping table's
        # own columns dropped out of the pairs entirely.
        collapsed = replace(
            first,
            to_catalog_source=second.to_catalog_source,
            to_ref=second.to_ref,
            column_pairs=((first.column_pairs[0][0], second.column_pairs[0][1]),))
        return replace(plan, steps=(collapsed,))

    joins.plan_crosswalk_join = _one_direct_step
    if hasattr(expression_ir, "plan_crosswalk_join"):
        expression_ir.plan_crosswalk_join = _one_direct_step


def _m_crosswalk_leg_omitted_from_read_authorization() -> None:
    """The PLAN keeps both legs; the READ SET loses the second one.

    The asymmetric failure, and the reason it is the hardest of the eight to notice: the plan is
    intact, the render emits both joins, and the generated run really does read the mapping table's
    second column and the target endpoint — but §1.3's read set, which is the thing Gate 2
    authorizes, never heard about them. So the run reads more than anyone was authorized to see, and
    every visible symptom (a shorter read set, fewer authorized nodes, one less catalog on the gate)
    looks like a tidier plan rather than a hole.

    Two-part, deliberately: `plan_crosswalk_join` is wrapped only to RECORD which refs belong to the
    second leg, and `_ReadSet.add` then drops exactly those. Nothing about the plan changes, which
    is what makes this the read-authorization mutation rather than a plan mutation.

    Scoped to the second leg's MAPPING-side column only. Dropping its endpoint side as well takes
    the grain key out of the read set, the compile refuses outright, and the victim then dies of a
    refusal rather than of the missing authorization — the `retrieval_drops_profile_context` mistake
    this registry already learned once. With just the mapping column gone the IR compiles cleanly
    and the read set is simply short one column the generated join really does read.
    """
    from featuregen.materialize import expression_ir
    from featuregen.materialize.codes import MaterializationRefused

    dropped: set[str] = set()
    original_plan = expression_ir.plan_crosswalk_join

    def _record_second_leg(admitted, **kwargs):
        plan = original_plan(admitted, **kwargs)
        if not isinstance(plan, MaterializationRefused) and len(plan.steps) == 2:
            mapping_ref = admitted.definition.mapping_dataset_ref + "."
            dropped.update(
                ref for pair in plan.steps[1].column_pairs for ref in pair
                if ref.startswith(mapping_ref))
        return plan

    original_add = expression_ir._ReadSet.add

    def _blind_to_the_second_leg(self, logical_ref, role, identity, column, *args, **kwargs):
        if logical_ref in dropped:
            return None
        return original_add(self, logical_ref, role, identity, column, *args, **kwargs)

    expression_ir.plan_crosswalk_join = _record_second_leg
    expression_ir._ReadSet.add = _blind_to_the_second_leg


def _m_uniqueness_measured_before_time_filtering() -> None:
    """The mapping row filter moves AFTER the uniqueness gate instead of before it.

    On an SCD mapping table this is the whole difference between "unique now" and "unique across
    all history" — and history is never unique, so the gate either refuses a correct pipeline or,
    worse, is satisfied by rows the traversal will not use. Nobody made the second claim.

    Implemented by REORDERING the emitted lines: the filtered frame is computed after the gate has
    already scanned the unfiltered one, which is a shape a "tidy the frame assignments" edit could
    plausibly produce.
    """
    from featuregen.materialize.render import nodes_compute

    original = nodes_compute._traversal_lines

    def _gate_then_filter(plan, roles_used):
        lines = original(plan, roles_used)
        out: list[str] = []
        pending: list[str] = []
        for line in lines:
            if "_scoped = " in line and ".where(" in line:
                pending.append(line)          # hold the mapping row filter back
                continue
            if pending and "duplicate_" in line:
                out.append(line)
                continue
            if pending and line.strip().startswith("rows = rows.join("):
                out.extend(pending)           # ... and only apply it at the join
                pending = []
            out.append(line)
        return out + pending

    nodes_compute._traversal_lines = _gate_then_filter


def _m_crosswalk_cardinality_inverted() -> None:
    """A traversal is gated on the OTHER direction's verdict.

    1:1 forward with N:1 reverse is the ordinary mapping-table shape, so inverting the lookup
    refuses every legal forward plan AND admits the illegal reverse one. The refusal half looks like
    a conservative bug; the admission half multiplies the population.

    Patched on the decision contract's own accessor, which `AdmittedCrosswalkV1.verdict_for` and
    the planner both reach at call time.
    """
    from featuregen.overlay.upload.crosswalk_admission import CrosswalkAdmissionDecisionV1
    from featuregen.overlay.upload.crosswalk_observation import SOURCE_TO_TARGET

    def _inverted(self, direction: str):
        return self.reverse if direction == SOURCE_TO_TARGET else self.forward

    CrosswalkAdmissionDecisionV1.verdict = _inverted


def _m_crosswalk_deduplicates_instead_of_refusing() -> None:
    """Two ACTIVE mappings for one endpoint pair are counted as one, so admission never refuses.

    §6.6's `refuse_on_multiple` in one line: with two active mappings the answer depends on which
    row a compiler happened to read, and reporting 1 makes that choice invisible rather than absent.
    Every downstream verdict then describes whichever mapping the query returned first.

    Patched on `crosswalk_assembly`'s own module global, which `assemble_admitted_crosswalk` reads
    at call time.
    """
    from featuregen.overlay.upload import crosswalk_assembly

    crosswalk_assembly.active_mapping_count = lambda conn, definition: 1


def _m_review_substitutes_for_crosswalk_safety() -> None:
    """Production admissibility becomes "somebody has looked at it" instead of "it was validated".

    The rule the whole release rests on (DoD 17): production execution depends on CURRENT
    deterministic, direction-specific evidence and never on review. Broken by widening the
    predicate from DETERMINISTICALLY_VALIDATED to "anything that is not UNASSESSED" — the shape a
    reviewer's sign-off would take in a vocabulary that has no review field, and a change somebody
    could plausibly justify as "we know about this one now".

    Patched on the class property, which every caller reaches through the instance at call time.
    """
    from featuregen.overlay.upload.bridge_realization import SafetyStatus
    from featuregen.overlay.upload.crosswalk_admission import CrosswalkDirectionVerdictV1

    CrosswalkDirectionVerdictV1.production_admissible = property(
        lambda self: self.safety_status is not SafetyStatus.UNASSESSED)


def _m_crosswalk_policy_pin_leaves_the_measurement() -> None:
    """The execution's mapping row rule stops being the one the measurement was taken under.

    THE SHIPPED DEFECT, verbatim in effect. `assemble_admitted_crosswalk` derived the pin from the
    caller's `mapping_row_selection` — the object `AdmittedCrosswalkV1.__post_init__` then validates
    AGAINST that pin — so the "policy pointer moved after the crosswalk was measured" refusal had
    the same value on both sides of its comparison and could never fire. Measured under one rule,
    resolved under another: a clean, production-admissible bundle.

    Reinstated here in its other half: the pin follows the DEFINITION's declaration and ignores the
    measurement, which is the shape that made the second face possible — an execution pinning None
    over a measurement taken WITH a filter, leaving `joins.plan_crosswalk_join` nothing to require
    and the traversal reading the mapping table unfiltered under a verdict measured over the
    filtered rows.

    Patched on the module attribute, which `assemble_admitted_crosswalk` reads through module
    globals at call time — the same seam the fix introduced so this could be a REPLACEABLE
    derivation rather than an expression buried in a constructor call.
    """
    from featuregen.overlay.upload import crosswalk_assembly

    crosswalk_assembly.pinned_mapping_temporal_policy = (
        lambda definition, observation: definition.mapping_temporal_policy_revision_id)


def _m_crosswalk_identity_omits_mapping_revision() -> None:
    """`execution_revision_id` stops covering the mapping binding revision.

    Two executions reading two DIFFERENT mapping tables then collapse onto one id — and everything
    downstream keys on that id: the render's pin tables, the artifact lock, the physical-binding
    drift refusals. The collision does not surface as an error; it serves a verdict measured over
    one table to a traversal that reads another.

    Patched by rebuilding the id from a payload with the field removed, on the contract itself.
    """
    from featuregen.materialize.canonical import materialize_hash
    from featuregen.overlay.upload.crosswalk import (
        CROSSWALK_CONTRACT_VERSION,
        CrosswalkExecutionRevisionV1,
    )

    original = CrosswalkExecutionRevisionV1.__post_init__

    def _blind_to_the_mapping(self) -> None:
        original(self)
        object.__setattr__(self, "execution_revision_id", "cwx_" + materialize_hash({
            "contract_version": CROSSWALK_CONTRACT_VERSION,
            "crosswalk_definition_revision_id": self.crosswalk_definition_revision_id,
            "source_leg": self.source_leg.identity_payload(),
            "target_leg": self.target_leg.identity_payload(),
            "mapping_temporal_policy_revision_id": self.mapping_temporal_policy_revision_id,
            "applicability_scope": self.applicability_scope.identity_payload(),
            "leg_measurement_ids": list(self.leg_measurement_ids),
            "leg_observation_revision_ids": list(self.leg_observation_revision_ids),
            "composition_observation_revision_id": self.composition_observation_revision_id,
            "combined_cardinality": self.combined_cardinality.identity_payload(),
            "safety_status": self.safety_status.value,
        }))

    CrosswalkExecutionRevisionV1.__post_init__ = _blind_to_the_mapping


_SUPERSESSION = "tests/featuregen/overlay/upload/test_supersession_consistency.py"
_REPLAY = "tests/featuregen/overlay/upload/test_enrichment_replay_identity.py"
_SCOPE = "tests/featuregen/overlay/upload/test_identifier_scope.py"
_PROFILES = "tests/featuregen/overlay/upload/test_dataset_profiles.py"
_CATALOG = "tests/featuregen/overlay/upload/test_catalog_profiles.py"
_CONSUME = "tests/featuregen/overlay/upload/test_profile_consumption.py"
_CTXGRAPH = "tests/featuregen/overlay/upload/test_context_graph.py"
_V4 = "tests/featuregen/overlay/upload/test_feature_context_v4.py"
_BARS = "tests/eval/test_release_a_bars.py"
_USEGATE = "tests/featuregen/overlay/upload/test_feature_use_gate.py"
_PIIACCEPT = "tests/featuregen/overlay/upload/test_pii_policy_acceptance.py"
# Release C Task 13
_CWVIS = "tests/featuregen/overlay/upload/test_crosswalk_visibility.py"
_CWASSEM = "tests/featuregen/overlay/upload/test_crosswalk_assembly.py"
_CWCONTRACT = "tests/featuregen/overlay/upload/test_crosswalk_contracts.py"
_CWIR = "tests/featuregen/materialize/test_crosswalk_ir.py"
_CWRENDER = "tests/featuregen/materialize/test_crosswalk_render.py"
_JOINS = "tests/featuregen/materialize/test_joins.py"


REGISTRY: tuple[Mutation, ...] = (
    Mutation(
        mutation_id="vocab_fingerprint_drops_hint",
        kind=MUST_DIE,
        invariant="a reworded concept meaning re-keys the classifier cache",
        target="enrich:_vocab_fingerprint",
        victims=(
            f"{_REPLAY}::test_mutating_a_rendered_registry_field_invalidates_classifier_replay",
        ),
        apply=_m_fingerprint_drops_hint,
        expect_failure_contains="assert enrich._vocab_fingerprint() != before",
        notes="the fingerprint hashes the first-sentence 120-char hint, not the whole description",
    ),
    Mutation(
        mutation_id="issuer_leaves_namespace_identity",
        kind=MUST_DIE,
        invariant="a scheme names a value space only WITHIN an issuer",
        target="attest.bridge_grounding:resolve_identifier_issuer",
        victims=(
            f"{_SCOPE}::test_same_scheme_different_known_issuer_refuses_with_a_code",
            f"{_SCOPE}::test_different_declared_issuers_refuse_cross_catalog_cif_pairs",
        ),
        apply=_m_issuer_never_resolves,
        expect_failure_contains="cif columns issued by two different banks must not pair",
    ),
    Mutation(
        mutation_id="entity_reenters_the_blocking_key",
        kind=MUST_DIE,
        invariant="entity never gates identifier pairing (a counterparty may be our customer)",
        target="bridge_candidates:_identifier_columns",
        victims=(
            f"{_SCOPE}::test_counterparty_pair_fact_key_is_byte_identical_while_display_reads_customer",
            f"{_SCOPE}::test_same_declared_issuer_keeps_the_normal_candidates",
        ),
        apply=_m_entity_reenters_the_blocking_key,
        expect_failure_contains="candidate = by_pair[frozenset({left_col, right_col})]",
    ),
    Mutation(
        mutation_id="accept_off_registry_concept",
        kind=MUST_DIE,
        invariant="only a registry concept or the literal 'unclassified' is ever accepted",
        target="enrich:_accept_concept",
        victims=(
            "tests/featuregen/overlay/upload/test_enrich_batch.py"
            "::test_enrich_concepts_batch_mode_caches_valid_only",
            "tests/featuregen/overlay/upload/test_enrich_batch.py"
            "::test_run_batched_salvages_valid_and_leaves_invalid_uncached",
        ),
        apply=_m_accept_free_text_concept,
        expect_failure_contains="totally_made_up",
    ),
    Mutation(
        mutation_id="disable_stale_value_retirement",
        kind=MUST_DIE,
        invariant="a superseded LLM proposal is retired, not left live beside its replacement",
        target="enrich:stale_all_llm_field_evidence",
        victims=(
            f"{_SUPERSESSION}::test_a_different_value_stales_the_prior_row_and_writes_a_fresh_one",
            f"{_SUPERSESSION}::test_a_same_value_reuse_still_retires_the_llms_other_live_proposal",
        ),
        apply=_m_disable_stale_value_retirement,
        expect_failure_contains="assert len(current) == 1",
    ),
    # RETIRED 2026-08-13 (A0 triage): "feature_gen_reads_the_thin_menu". Its victims
    # (test_flag_on_defaults_to_v4 et al) were deliberately deleted in flag-retirement wave B —
    # FEATUREGEN_FEATURE_CONTEXT is gone, feature_context_enabled() returns constant True with
    # its own reintroduction pin test, and the thin menu is unreachable by construction. The
    # invariant this mutation guarded is now enforced structurally, not behaviorally.
    Mutation(
        mutation_id="drop_per_kind_truncation_reporting",
        kind=MUST_DIE,
        invariant="the context section says WHICH kinds it omitted, not merely that it truncated",
        target="lineage:TruncationReportV1.as_dict",
        victims=(
            f"{_CTXGRAPH}::test_truncation_reports_per_kind_omissions",
            f"{_CTXGRAPH}::test_lineage_truncation_report_distinguishes_a_budget_cut_from_a_prune",
        ),
        apply=_m_drop_per_kind_truncation_reporting,
        expect_failure_contains='assert graph["truncation"]["omitted"]',
    ),
    Mutation(
        mutation_id="review_status_implies_executable",
        kind=MUST_DIE,
        invariant="a review badge is never a permission; executable_now comes from revalidation",
        target="context_graph:_relationship_dict",
        victims=(
            f"{_CTXGRAPH}::test_a_human_verified_review_does_not_make_a_link_executable",
            f"{_BARS}::test_bar_zero_reviewed_but_unsafe_relationships_displayed_executable",
        ),
        apply=_m_review_status_implies_executable,
        expect_failure_contains="an approved review was displayed as executable",
    ),
    Mutation(
        mutation_id="collapse_data_and_authority_role",
        kind=MUST_DIE,
        invariant="what a dataset IS and how authoritative its copy is are two questions",
        target="dataset_profiles:_PROFILE_FIELD_SOURCES",
        victims=(f"{_BARS}::test_bar_data_role_and_authority_role_stay_two_questions",),
        apply=_m_collapse_data_and_authority_role,
        expect_failure_contains="the table KIND leaked into the AUTHORITY axis",
        notes="SURVIVED the whole pre-existing suite. Nothing asserted that a table whose "
              "`table_role` is source-attested still reports its AUTHORITY as undecided, so one "
              "axis answering both questions would have shipped unnoticed. The victim is the bar "
              "written to close that gap.",
    ),
    Mutation(
        mutation_id="catalog_authority_inherits_onto_a_dataset",
        kind=MUST_DIE,
        invariant="a catalog narrative is descriptive; it never defaults a dataset's authority",
        target="catalog_profiles:NARRATIVE_KEYS",
        victims=(
            f"{_BARS}::test_bar_a_catalog_narrative_never_defaults_a_dataset_authority",
        ),
        apply=_m_catalog_authority_inherits,
        expect_failure_contains="assert {'authority_role'} == set()",
        notes="SURVIVED. `test_bounds_are_enforced_before_persistence` proves an UNKNOWN key is "
              "refused, which says nothing about what happens once a key becomes known. The "
              "outcome — narrative authored, dataset authority still undecided — was untested.",
    ),
    Mutation(
        mutation_id="llm_authority_becomes_load_bearing",
        kind=MUST_DIE,
        invariant="an LLM proposal is displayed and never load-bearing, however correct",
        target="field_policies:_POLICIES",
        victims=(
            f"{_PROFILES}::test_llm_proposal_is_displayed_but_never_load_bearing",
            f"{_PROFILES}::test_four_eyes_is_not_weakened_for_operational_profile_fields",
        ),
        apply=_m_llm_authority_becomes_load_bearing,
        expect_failure_contains="assert f.load_bearing is None",
    ),
    Mutation(
        mutation_id="search_drops_profile_context",
        kind=MUST_DIE,
        invariant="profile classifications are facetable in search with the flag on",
        target="search:column_facets",
        victims=(
            f"{_CONSUME}::test_the_derived_data_role_is_facetable_with_the_flag_on",
            f"{_CONSUME}::test_the_authority_and_temporal_axes_are_facetable",
        ),
        apply=_m_search_drops_profile_context,
        expect_failure_contains='assert "data_role" in facets',
    ),
    Mutation(
        mutation_id="feature_gen_drops_profile_context",
        kind=MUST_DIE,
        invariant="the feature-context table block carries the profile as advisories",
        target="feature_assist:_profile_advisories",
        victims=(
            f"{_CONSUME}::test_feature_context_consumes_the_profile_as_advisories",
            f"{_CONSUME}::test_a_snapshot_table_earns_the_time_mismatch_advisory",
        ),
        apply=_m_feature_gen_drops_profile_context,
        expect_failure_contains="KeyError: 'advisories'",
    ),
    Mutation(
        mutation_id="retrieval_drops_profile_context",
        kind=MUST_DIE,
        invariant="leg 3's controlled expansion harvests the table profile with the flag on",
        target="analysis.retrieval:_TABLE_PROFILE_COLUMNS",
        victims=(
            f"{_CONSUME}::test_retrieval_expansion_uses_the_table_profile_with_the_flag_on",
            f"{_BARS}::test_bar_profile_context_lifts_retrieval",
        ),
        apply=_m_retrieval_drops_profile_context,
        expect_failure_contains="the profile harvest added no expansion terms",
    ),
    Mutation(
        mutation_id="graph_projection_read_as_authority",
        kind=MUST_DIE,
        invariant="eligibility comes from the decision log, never the flat display projection",
        target="field_resolution:is_feature_eligible",
        victims=(
            "tests/featuregen/overlay/upload/test_technical_source_authority_gate.py",
            "tests/featuregen/overlay/upload/test_feature_menu_enrichment.py",
        ),
        apply=_m_graph_projection_read_as_authority,
        expect_failure_contains="where False = is_feature_eligible(",
    ),
    Mutation(
        mutation_id="profile_hash_omits_business_context",
        kind=MUST_DIE,
        invariant="every meaning-bearing field moves dataset_profile_hash",
        target="dataset_profiles:_EFFECTIVE_FIELDS",
        victims=(
            f"{_BARS}::test_bar_business_context_moves_the_dataset_profile_hash",
        ),
        apply=_m_profile_hash_omits_a_meaning_bearing_field,
        expect_failure_contains="a business_context change did not move dataset_profile_hash",
        notes="SURVIVED. The pre-existing hash-sensitivity test walks definition, authority_role, "
              "a governed fact head and the narrative revision — every meaning-bearing input "
              "EXCEPT business_context.",
    ),
    Mutation(
        mutation_id="disable_the_feature_use_gate",
        kind=MUST_DIE,
        invariant="sensitivity says who may SEE a column; the USE gate says who may BUILD from it",
        target="feature_assist:feature_use_gate_enabled",
        victims=(
            f"{_BARS}::test_bar_zero_unsafe_gold_features_accepted",
            f"{_USEGATE}::test_personal_data_is_refused_naming_the_MISSING_POLICY_not_the_column",
        ),
        apply=_m_disable_the_feature_use_gate,
        expect_failure_contains="unsafe gold features accepted:",
        notes="Bar 4 was a STRICT XFAIL against exactly this state until the gate landed — of five "
              "unsafe gold classes the platform refused one. The mutation restores the finding, so "
              "the bar has to die on it or the bar is passing for some other reason.",
    ),
    Mutation(
        mutation_id="gate_ignores_policy_revocation",
        kind=MUST_DIE,
        invariant="a WITHDRAWN data-use policy stops licensing the operand it licensed",
        target="feature_assist:active_pii_use_policies",
        victims=(
            f"{_USEGATE}::test_a_REVOKED_policy_refuses_again",
            f"{_PIIACCEPT}::test_revoking_one_anchor_kills_exactly_the_recipes_that_use_it",
        ),
        apply=_m_gate_ignores_policy_revocation,
        expect_failure_contains="assert rej is not None",
        notes="D14's other half. Approving a policy is only safe because revoking it takes effect "
              "immediately; a gate that reads the pointer but not the status would let the "
              "governance screen say `revoked` while the feature flow kept building.",
    ),
    # ── Release C Task 13 ───────────────────────────────────────────────────────────────────────
    Mutation(
        mutation_id="crosswalk_label_treated_as_executable",
        kind=MUST_DIE,
        invariant="a crosswalk's LABEL is never its capability; executable_now is wiring, not kind",
        target="context_graph:_relationship_dict",
        # `test_no_safety_verdict_is_fabricated_anywhere_on_a_crosswalk_payload` was listed here and
        # CANNOT die: this mutation adds `executable_now`, and that test asserts the ABSENCE of
        # `safety_status` and the eligibility keys. Two different lies, and only one of them is
        # being told — so the second victim is dropped rather than overstating the kill.
        victims=(
            f"{_CWVIS}::test_the_context_graph_never_reports_a_crosswalk_as_executable",
        ),
        apply=_m_crosswalk_label_treated_as_executable,
        expect_failure_contains='assert cross["executable_now"] is False',
    ),
    Mutation(
        mutation_id="crosswalk_renders_endpoint_equality",
        kind=MUST_DIE,
        invariant="two legs never collapse into one endpoint-to-endpoint join",
        target="joins:plan_crosswalk_join",
        victims=(
            f"{_JOINS}::test_a_crosswalk_plans_TWO_steps_and_never_collapses_to_endpoint_equality",
            f"{_CWIR}::test_an_admitted_crosswalk_compiles_to_TWO_governed_join_steps",
            f"{_CWIR}::test_the_first_leg_reaches_the_mapping_and_the_second_leaves_it",
        ),
        apply=_m_crosswalk_renders_endpoint_equality,
        expect_failure_contains="assert len(plan.steps) == 2",
        notes="the mapping table leaves the plan entirely, so the two identifier schemes are "
              "joined as if they were one value space",
    ),
    Mutation(
        mutation_id="crosswalk_leg_omitted_from_read_authorization",
        kind=MUST_DIE,
        invariant="both legs' tables enter §1.3's read set, so Gate 2 authorizes everything read",
        target="ir:_union_of",
        victims=(
            f"{_CWIR}::test_the_mapping_dataset_and_its_row_rule_columns_enter_the_read_set",
        ),
        apply=_m_crosswalk_leg_omitted_from_read_authorization,
        expect_failure_contains="assert {cf.MAP_ACCT, cf.MAP_EXT} <= _refs(ir)",
        notes="ASYMMETRIC: dropping a leg makes the plan look smaller and tidier while the run "
              "still reads the table",
    ),
    Mutation(
        mutation_id="uniqueness_measured_before_time_filtering",
        kind=MUST_DIE,
        invariant="the mapping row rule is applied BEFORE the uniqueness gate and the join",
        target="render.nodes_compute:_traversal_lines",
        victims=(
            f"{_CWRENDER}::test_the_mapping_row_filter_precedes_the_uniqueness_gate_and_the_join",
        ),
        apply=_m_uniqueness_measured_before_time_filtering,
        expect_failure_contains="assert filter_line < gate_line < join_line",
        notes="on an SCD mapping table this is 'unique now' versus 'unique across all history', "
              "and nobody made the second claim",
    ),
    Mutation(
        mutation_id="crosswalk_cardinality_inverted",
        kind=MUST_DIE,
        invariant="each direction is gated on its OWN measured verdict, never on the other's",
        target="crosswalk_admission:CrosswalkAdmissionDecisionV1.verdict",
        victims=(
            f"{_JOINS}::test_the_reverse_direction_is_gated_on_its_OWN_measured_cardinality",
            f"{_CWIR}::test_the_reverse_direction_refuses_INDEPENDENTLY_on_its_own_verdict",
        ),
        apply=_m_crosswalk_cardinality_inverted,
        expect_failure_contains=(
            "the source_to_target direction of crosswalk "),
        notes="refuses every legal forward plan AND admits the illegal reverse one",
    ),
    Mutation(
        mutation_id="crosswalk_deduplicates_instead_of_refusing",
        kind=MUST_DIE,
        invariant="two active mappings for one endpoint pair refuse; nothing picks between them",
        target="crosswalk_assembly:active_mapping_count",
        victims=(
            f"{_CWASSEM}::test_two_active_mappings_for_one_endpoint_pair_refuse_rather_than_pick_one",
        ),
        apply=_m_crosswalk_deduplicates_instead_of_refusing,
        expect_failure_contains="assert active_mapping_count(db, revision) == 2",
    ),
    Mutation(
        mutation_id="review_substitutes_for_crosswalk_safety",
        kind=MUST_DIE,
        invariant="production admissibility is deterministic validation, never a human sign-off",
        target="crosswalk_admission:CrosswalkDirectionVerdictV1.production_admissible",
        # `test_a_direction_cannot_claim_production_without_deterministic_validation` was listed
        # here and CANNOT die: it constructs a `CrosswalkDirectionContextV1` (a display projection)
        # directly and never reaches `CrosswalkDirectionVerdictV1`, which is what this mutation
        # patches. A victim that cannot die makes a kill list read stronger than it is, so it is
        # dropped rather than kept as decoration.
        victims=(
            f"{_CWASSEM}::test_the_two_directions_are_admitted_independently_from_the_measured_shape",
            f"{_CWASSEM}::test_confirming_a_crosswalk_changes_no_admission_answer_at_all",
        ),
        apply=_m_review_substitutes_for_crosswalk_safety,
        expect_failure_contains=(
            "a reviewer confirmed a crosswalk and a measured fan-out became executable"),
        notes="DoD 17. Widened from DETERMINISTICALLY_VALIDATED to 'anything not UNASSESSED' — the "
              "shape a sign-off takes in a vocabulary with no review field",
    ),
    Mutation(
        mutation_id="crosswalk_policy_pin_leaves_the_measurement",
        kind=MUST_DIE,
        invariant=(
            "the mapping row rule an execution pins is the one the MEASUREMENT was taken under, "
            "never one the caller supplied or the definition merely declares"),
        target="crosswalk_assembly:pinned_mapping_temporal_policy",
        victims=(
            f"{_CWASSEM}::test_the_pin_is_the_MEASURED_row_rule_even_when_the_definition_declares_none",
            f"{_CWASSEM}::test_a_row_selection_resolved_under_a_MOVED_policy_pointer_refuses",
        ),
        apply=_m_crosswalk_policy_pin_leaves_the_measurement,
        expect_failure_contains=(
            "the execution's mapping row rule stopped following the measurement"),
        notes="the shipped Task-13 defect: the pin was derived from the very row selection "
              "`AdmittedCrosswalkV1.__post_init__` validates against it, so the drift refusal had "
              "one value on both sides and could never fire",
    ),
    Mutation(
        mutation_id="crosswalk_identity_omits_mapping_revision",
        kind=MUST_DIE,
        invariant="which mapping table was measured is INSIDE the execution revision identity",
        target="crosswalk:CrosswalkExecutionRevisionV1.__post_init__",
        victims=(
            f"{_CWCONTRACT}::test_every_mapping_pin_moves_the_execution_revision_id",
        ),
        apply=_m_crosswalk_identity_omits_mapping_revision,
        expect_failure_contains="is not inside the execution identity",
        notes="a collision here serves a verdict measured over one table to a traversal that reads "
              "another, and every downstream pin keys on that id",
    ),
    Mutation(
        mutation_id="noop_reorder_registry_declarations",
        kind=MUST_SURVIVE,
        invariant="declaration and dict order are not meaning; both fingerprints sort first",
        target="concepts:_ALL / CONCEPT_REGISTRY",
        victims=(
            f"{_REPLAY}::test_registry_declaration_order_does_not_invalidate_classifier_replay",
            f"{_REPLAY}::test_critic_fingerprint_is_registry_order_insensitive",
            f"{_REPLAY}::test_mutating_a_rendered_registry_field_invalidates_classifier_replay",
        ),
        apply=_m_noop_reorder_registry,
        notes="the control: if this kills anything, no must-die result from this harness is "
              "trustworthy",
    ),
)

BY_ID: dict[str, Mutation] = {m.mutation_id: m for m in REGISTRY}


def must_die() -> tuple[Mutation, ...]:
    return tuple(m for m in REGISTRY if m.kind == MUST_DIE and not m.dropped_reason)


def must_survive() -> tuple[Mutation, ...]:
    return tuple(m for m in REGISTRY if m.kind == MUST_SURVIVE and not m.dropped_reason)


def dropped() -> tuple[Mutation, ...]:
    return tuple(m for m in REGISTRY if m.dropped_reason)
