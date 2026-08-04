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


def _m_feature_gen_reads_the_thin_menu() -> None:
    """Feature generation serves the v1 thin menu even with the context flag ON."""
    from featuregen.overlay.upload import feature_assist as fa

    fa.feature_context_enabled = lambda: False


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
    Mutation(
        mutation_id="feature_gen_reads_the_thin_menu",
        kind=MUST_DIE,
        invariant="with the flag on, feature generation gets v4, not the v1 thin menu",
        target="feature_assist:feature_context_enabled",
        victims=(
            f"{_V4}::test_flag_on_defaults_to_v4",
            f"{_V4}::test_v4_carries_the_shared_bundle_keys",
        ),
        apply=_m_feature_gen_reads_the_thin_menu,
        expect_failure_contains="where 1 = _feature_schema_version()",
    ),
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
        ),
        apply=_m_gate_ignores_policy_revocation,
        expect_failure_contains="assert rej is not None",
        notes="D14's other half. Approving a policy is only safe because revoking it takes effect "
              "immediately; a gate that reads the pointer but not the status would let the "
              "governance screen say `revoked` while the feature flow kept building.",
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
