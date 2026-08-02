"""Release-A profile Task 1 — vocabularies, policies, set_advisory, effective-profile assembly.

Proves the review-D/A6 pitfalls are handled:
  * `crosswalk` is an INPUT alias only — `TABLE_ROLE_ENUM` (prompt-interpolated) is untouched and
    legacy `bridge` evidence displays as CROSSWALK without a rewrite;
  * RECOMMENDATION fields report `display_only` as their NORMAL state (never failure-shaped), and
    "nobody said anything" is the distinct `undecided:no_evidence`;
  * LLM / HUMAN-PROPOSED values on `authority_role`/`temporal_storage_model` are DISPLAYED but never
    load-bearing; only the existing four-eyes propose→confirm (distinct subjects) is;
  * `set_advisory` is hard-allowlisted to `business_context` — extending it to `definition`/
    `concept` (or any operational field) raises;
  * `dataset_profile_hash` is stable and sensitive to every meaning-bearing input (evidence,
    authority, fact heads, current narrative revision id) and to nothing else.
"""
from __future__ import annotations

import pytest
from tests.featuregen._helpers import mint_test_identity

from featuregen.overlay.evidence import AssertionStrength, EvidenceProducer
from featuregen.overlay.field_evidence import field_input_hash, record_field_evidence
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.dataset_profiles import build_dataset_profile
from featuregen.overlay.upload.field_correction import (
    SET_ADVISORY_FIELDS,
    FieldCorrectionError,
    _assert_set_advisory_allowlist,
    apply_field_correction,
    read_field_cas,
)
from featuregen.overlay.upload.field_resolution import (
    is_feature_eligible,
    resolve_and_project,
)
from featuregen.overlay.upload.graph import build_graph
from featuregen.overlay.upload.object_ref import normalize_ref
from featuregen.overlay.upload.profile_vocab import (
    DataRole,
    data_role_from_table_role,
    dataset_profiles_enabled,
    normalize_authority_role,
    normalize_temporal_storage_model,
)
from featuregen.overlay.upload.semantic_context import (
    UNRESOLVED_AUTHORITY_INSUFFICIENT,
    UNRESOLVED_CONFLICT,
    UNRESOLVED_NO_EVIDENCE,
    UNRESOLVED_PENDING_REVALIDATION,
    UNRESOLVED_PENDING_REVIEW,
    UNRESOLVED_REASON_FAMILIES,
    UNRESOLVED_REASONS,
    unresolved_family,
    unresolved_label,
)
from featuregen.overlay.upload.table_vocab import TABLE_ROLE_ENUM, normalize_table_role

ADMIN_A = mint_test_identity(subject="user:priya", role_claims=("platform-admin",))
ADMIN_B = mint_test_identity(subject="user:sam", role_claims=("platform-admin",))

_SRC = "bank"
_TABLE_OBJECT_REF = "public.orders"
_TABLE_REF = normalize_ref(_SRC, None, "orders")


def _seed_graph(db):
    build_graph(db, _SRC, [CanonicalRow(_SRC, "orders", "amount", "numeric")])


def _seed(db, field, value, producer, strength):
    record_field_evidence(
        db, logical_ref=_TABLE_REF, field_name=field, proposed_value=value,
        producer=producer, strength=strength, producer_ref="test-producer",
        source_snapshot_id="snap-1",
        input_hash=field_input_hash(logical_ref=_TABLE_REF, field_name=field,
                                    material=f"{value}:{producer.value}:{strength.value}"))


def _profile(db, revision_id=None):
    return build_dataset_profile(db, source=_SRC, dataset_logical_ref=_TABLE_REF,
                                 catalog_profile_revision_id=revision_id)


def _correct(db, field, action, actor, idem, **kw):
    cas = read_field_cas(db, source=_SRC, object_ref=_TABLE_OBJECT_REF, field=field)
    return apply_field_correction(
        db, source=_SRC, object_ref=_TABLE_OBJECT_REF, field=field, action=action, actor=actor,
        idempotency_key=idem, expected_latest_decision_id=cas["latest_decision_id"],
        expected_evidence_set_hash=cas["evidence_set_hash"],
        expected_policy_version=cas["policy_version"], **kw)


# ── §6.1 vocabularies ────────────────────────────────────────────────────────────────────────────


def test_crosswalk_is_an_input_alias_to_canonical_bridge():
    assert normalize_table_role("crosswalk", event_or_snapshot=None) == "bridge"
    assert normalize_table_role(" Crosswalk ", event_or_snapshot=None) == "bridge"


def test_table_role_enum_is_not_extended_by_the_alias():
    """TABLE_ROLE_ENUM is interpolated into the Pass-B prompt; the alias must NOT re-version it."""
    assert "crosswalk" not in TABLE_ROLE_ENUM


def test_data_role_adapts_the_one_table_role_normalizer():
    assert data_role_from_table_role("bridge") is DataRole.CROSSWALK
    assert data_role_from_table_role("crosswalk") is DataRole.CROSSWALK
    assert data_role_from_table_role("dim") is DataRole.DIMENSION
    assert data_role_from_table_role("fact", event_or_snapshot="event") is DataRole.EVENT_FACT
    assert data_role_from_table_role("fact", event_or_snapshot="snapshot") is DataRole.SNAPSHOT_FACT
    assert data_role_from_table_role("fact") is DataRole.FACT
    assert data_role_from_table_role("nonsense") is DataRole.UNKNOWN
    assert data_role_from_table_role(None) is DataRole.UNKNOWN


def test_authority_and_temporal_normalizers_are_closed():
    assert normalize_authority_role("system_of_record") == "system_of_record"
    assert normalize_authority_role(" SCD2 ") is None
    assert normalize_authority_role("banana") is None
    assert normalize_temporal_storage_model("scd2") == "scd2"
    assert normalize_temporal_storage_model("banana") is None
    assert normalize_temporal_storage_model(7) is None


def test_unresolved_reasons_map_every_member_to_exactly_one_of_three_families():
    """The vocabulary is the CANONICAL one (`semantic_context`, D5) — this module owns no copy."""
    assert set(UNRESOLVED_REASONS.values()) <= UNRESOLVED_REASON_FAMILIES
    assert len(UNRESOLVED_REASON_FAMILIES) == 3   # no fourth family, ever
    # Every member the profile read model can emit is IN that closed vocabulary.
    for member in (UNRESOLVED_NO_EVIDENCE, UNRESOLVED_PENDING_REVIEW,
                   UNRESOLVED_AUTHORITY_INSUFFICIENT, UNRESOLVED_CONFLICT,
                   UNRESOLVED_PENDING_REVALIDATION):
        assert member in UNRESOLVED_REASONS


def test_profile_wire_labels_are_the_canonical_members_split_in_two():
    """The `unresolved_reason`/`unresolved_family` pair on the wire IS one canonical member: the
    family-free label plus its family. Pinned as LITERALS because both are published — renaming a
    canonical member's suffix is a wire change, and this test is where that surfaces."""
    assert (unresolved_label(UNRESOLVED_NO_EVIDENCE),
            unresolved_family(UNRESOLVED_NO_EVIDENCE)) == ("no_evidence", "undecided")
    assert (unresolved_label(UNRESOLVED_PENDING_REVIEW),
            unresolved_family(UNRESOLVED_PENDING_REVIEW)) == ("pending_review", "undecided")
    assert (unresolved_label(UNRESOLVED_AUTHORITY_INSUFFICIENT),
            unresolved_family(UNRESOLVED_AUTHORITY_INSUFFICIENT)) == (
                "authority_insufficient", "undecided")
    assert (unresolved_label(UNRESOLVED_CONFLICT),
            unresolved_family(UNRESOLVED_CONFLICT)) == ("conflict", "needs_data_check")
    assert (unresolved_label(UNRESOLVED_PENDING_REVALIDATION),
            unresolved_family(UNRESOLVED_PENDING_REVALIDATION)) == (
                "pending_revalidation", "needs_data_check")
    with pytest.raises(KeyError):
        unresolved_label("undecided:invented_by_a_caller")


def test_dataset_profiles_flag_uses_widened_truthy_set(monkeypatch):
    for truthy in ("1", "true", "YES", " on "):
        monkeypatch.setenv("FEATUREGEN_DATASET_PROFILES", truthy)
        assert dataset_profiles_enabled() is True
    for falsy in ("", "0", "off", "no"):
        monkeypatch.setenv("FEATUREGEN_DATASET_PROFILES", falsy)
        assert dataset_profiles_enabled() is False
    monkeypatch.delenv("FEATUREGEN_DATASET_PROFILES")
    assert dataset_profiles_enabled() is False


# ── set_advisory: hard allowlist + advisory-only semantics ───────────────────────────────────────


def test_set_advisory_allowlist_is_exactly_business_context():
    assert SET_ADVISORY_FIELDS == frozenset({"business_context"})
    _assert_set_advisory_allowlist()   # the live constant passes its own guard


@pytest.mark.parametrize("field", ["definition", "concept", "domain", "authority_role",
                                   "temporal_storage_model", "unit"])
def test_extending_the_set_advisory_allowlist_fails_loudly(field):
    with pytest.raises(ValueError):
        _assert_set_advisory_allowlist(frozenset({"business_context", field}))


def test_set_advisory_writes_confirmed_business_context_and_projects_immediately(db):
    _seed_graph(db)
    res = _correct(db, "business_context", "set_advisory", ADMIN_A, "sa-1",
                   replacement_value="Retail orders booked by branch staff.")
    assert res["accepted"] is True and res["body"]["outcome"] == "confirmed"
    row = db.execute(
        "SELECT producer, strength FROM field_evidence WHERE logical_ref = %s "
        "AND field_name = 'business_context'", (_TABLE_REF,)).fetchone()
    assert row == ("human", "confirmed")
    # Advisory forever: the RECOMMENDATION ceiling bars a load-bearing value even when confirmed.
    assert is_feature_eligible(db, _TABLE_REF, "business_context") is False
    profile = _profile(db)
    assert profile.business_context.display.value == "Retail orders booked by branch staff."
    assert profile.business_context.state == "display_only"
    assert profile.business_context.unresolved_reason is None   # NORMAL, not unresolved


def test_set_advisory_refuses_every_other_field(db):
    _seed_graph(db)
    for field in ("definition", "domain"):   # human_editable, yet NOT advisory-writable
        with pytest.raises(FieldCorrectionError) as exc:
            _correct(db, field, "set_advisory", ADMIN_A, f"sa-{field}",
                     replacement_value="text")
        assert exc.value.status_code == 403


def test_set_advisory_enforces_bounds(db):
    _seed_graph(db)
    with pytest.raises(FieldCorrectionError) as exc:
        _correct(db, "business_context", "set_advisory", ADMIN_A, "sa-long",
                 replacement_value="x" * 4001)
    assert exc.value.status_code == 400


# ── authority_role / temporal_storage_model: display lenient, load-bearing strict ────────────────


def test_llm_proposal_is_displayed_but_never_load_bearing(db):
    _seed_graph(db)
    _seed(db, "authority_role", "derived", EvidenceProducer.LLM, AssertionStrength.PROPOSED)
    profile = _profile(db)
    f = profile.authority_role
    assert f.display is not None and f.display.value == "derived"
    assert f.display.evidence[0].producer == "llm" and f.display.evidence[0].strength == "proposed"
    assert f.load_bearing is None
    assert f.state == "display_only"
    assert f.unresolved_reason == "authority_insufficient"
    assert f.unresolved_family == "undecided"
    assert is_feature_eligible(db, _TABLE_REF, "authority_role") is False


def test_human_proposed_is_displayed_but_not_load_bearing(db):
    _seed_graph(db)
    res = _correct(db, "temporal_storage_model", "propose_override", ADMIN_A, "ts-p",
                   replacement_value="scd2")
    assert res["accepted"] is True
    profile = _profile(db)
    f = profile.temporal_storage_model
    assert f.display is not None and f.display.value == "scd2"
    assert f.display.evidence[0].producer == "human" and f.display.evidence[0].strength == "proposed"
    assert f.load_bearing is None
    assert is_feature_eligible(db, _TABLE_REF, "temporal_storage_model") is False


def test_four_eyes_confirm_makes_the_value_load_bearing(db):
    _seed_graph(db)
    _correct(db, "authority_role", "propose_override", ADMIN_A, "ar-p",
             replacement_value="system_of_record")
    res = _correct(db, "authority_role", "confirm_override", ADMIN_B, "ar-c",
                   replacement_value="system_of_record")
    assert res["accepted"] is True
    assert is_feature_eligible(db, _TABLE_REF, "authority_role") is True
    profile = _profile(db)
    f = profile.authority_role
    assert f.state == "load_bearing"
    assert f.load_bearing.value == "system_of_record"
    assert f.load_bearing.evidence[0].producer == "human" and f.load_bearing.evidence[0].strength == "confirmed"
    assert f.unresolved_reason is None


def test_four_eyes_is_not_weakened_for_operational_profile_fields(db):
    """The proposer can never confirm their own operational proposal (D12.7)."""
    _seed_graph(db)
    _correct(db, "authority_role", "propose_override", ADMIN_A, "fe-p",
             replacement_value="system_of_record")
    res = _correct(db, "authority_role", "confirm_override", ADMIN_A, "fe-c",
                   replacement_value="system_of_record")
    assert res["accepted"] is False and res["status_code"] == 403
    assert is_feature_eligible(db, _TABLE_REF, "authority_role") is False


def test_profiler_attested_is_not_load_bearing_for_profile_classifications(db):
    """F8: the plan's bar for this release is source-attested / human-confirmed ONLY — the
    profiler/attested leaf was removed from the operational rules (no producer emits PROFILER
    field evidence for these fields today; the Hive/ODS deterministic slice re-proposes it
    through policy review)."""
    _seed_graph(db)
    _seed(db, "authority_role", "system_of_record", EvidenceProducer.PROFILER,
          AssertionStrength.ATTESTED)
    resolve_and_project(db, source=_SRC, logical_refs=[_TABLE_REF], fields=["authority_role"])
    assert is_feature_eligible(db, _TABLE_REF, "authority_role") is False
    f = _profile(db).authority_role
    assert f.load_bearing is None
    assert f.display is None       # profiler/attested clears no display rule either


def test_off_vocabulary_values_are_refused_before_any_write(db):
    _seed_graph(db)
    with pytest.raises(FieldCorrectionError) as exc:
        _correct(db, "temporal_storage_model", "propose_override", ADMIN_A, "ov-p",
                 replacement_value="bitemporal-ish")
    assert exc.value.status_code == 400
    assert db.execute("SELECT count(*) FROM field_evidence WHERE field_name = "
                      "'temporal_storage_model'").fetchone()[0] == 0


def test_same_subject_re_propose_supersedes_their_prior_proposal(db):
    """F1 at the decisions path: the generic propose_override behaves exactly like the profile
    PUT — re-proposing a different value retires the SAME subject's prior ACTIVE proposal to
    `superseded` (same transaction), so a typo correction never ties with itself."""
    _seed_graph(db)
    _correct(db, "authority_role", "propose_override", ADMIN_A, "rp-1",
             replacement_value="derived")
    _correct(db, "authority_role", "propose_override", ADMIN_A, "rp-2",
             replacement_value="system_of_record")
    rows = db.execute(
        "SELECT proposed_value, lifecycle FROM field_evidence WHERE field_name = "
        "'authority_role' ORDER BY created_at, evidence_id").fetchall()
    assert [r[0] for r in rows if r[1] == "active"] == ["system_of_record"]
    assert ("derived", "superseded") in rows
    f = _profile(db).authority_role
    assert f.display is not None and f.display.value == "system_of_record"


def test_competing_subjects_report_undecided_pending_review_not_conflict(db):
    """F2: a top-strength tie among UNREVIEWED proposals is "nobody decided yet" — family
    `undecided`, reason `pending_review` — never the needs_data_check conflict reserved for
    contradictions at load-bearing-capable strengths. Neither subject's row is retired."""
    _seed_graph(db)
    _correct(db, "authority_role", "propose_override", ADMIN_A, "cp-1",
             replacement_value="derived")
    _correct(db, "authority_role", "propose_override", ADMIN_B, "cp-2",
             replacement_value="system_of_record")
    active = db.execute(
        "SELECT count(*) FROM field_evidence WHERE field_name = 'authority_role' "
        "AND lifecycle = 'active'").fetchone()[0]
    assert active == 2
    f = _profile(db).authority_role
    assert f.display is None
    assert f.state == "undecided"
    assert f.unresolved_reason == "pending_review"
    assert f.unresolved_family == "undecided"


def test_attested_tie_still_reports_the_needs_data_check_conflict(db):
    """F2 boundary: a tie at a load-bearing-capable strength (two source attestations) IS a
    genuine cross-authority contradiction — conflict/needs_data_check is reserved for it."""
    _seed_graph(db)
    _seed(db, "authority_role", "derived", EvidenceProducer.SOURCE, AssertionStrength.ATTESTED)
    _seed(db, "authority_role", "system_of_record", EvidenceProducer.SOURCE,
          AssertionStrength.ATTESTED)
    f = _profile(db).authority_role
    assert f.state == "conflict"
    assert f.unresolved_reason == "conflict"
    assert f.unresolved_family == "needs_data_check"


# ── effective profile assembly ───────────────────────────────────────────────────────────────────


def test_empty_table_profile_is_honestly_undecided_everywhere(db):
    _seed_graph(db)
    profile = _profile(db)
    assert profile is not None
    for f in (profile.description, profile.business_context, profile.domains, profile.data_role,
              profile.primary_entity, profile.authority_role, profile.temporal_storage_model,
              profile.event_or_snapshot):
        assert f.state == "no_evidence"
        assert f.unresolved_reason == "no_evidence"
        assert f.unresolved_family == "undecided"
    assert profile.grain_fact is None and profile.availability_fact is None
    assert "catalog_narrative:absent" in profile.missing_context
    assert "description:no_evidence" in profile.missing_context


def test_recommendation_display_is_the_normal_state_not_unresolved(db):
    """A source-attested definition on a RECOMMENDATION field: display_only, NO unresolved reason,
    never failure-shaped — the §6.3 amendment."""
    _seed_graph(db)
    _seed(db, "definition", "Orders booked.", EvidenceProducer.SOURCE, AssertionStrength.ATTESTED)
    f = _profile(db).description
    assert f.state == "display_only"
    assert f.display.value == "Orders booked."
    assert f.display.evidence[0].producer == "source" and f.display.evidence[0].strength == "attested"
    assert f.unresolved_reason is None and f.unresolved_family is None


def test_conflicting_evidence_reports_the_needs_data_check_family(db):
    _seed_graph(db)
    _seed(db, "definition", "Orders booked.", EvidenceProducer.SOURCE, AssertionStrength.ATTESTED)
    _seed(db, "definition", "Order events.", EvidenceProducer.SOURCE, AssertionStrength.ATTESTED)
    f = _profile(db).description
    assert f.state == "conflict"
    assert f.unresolved_reason == "conflict"
    assert f.unresolved_family == "needs_data_check"


def test_data_role_is_derived_and_legacy_bridge_displays_as_crosswalk(db):
    _seed_graph(db)
    _seed(db, "table_role", "bridge", EvidenceProducer.LLM, AssertionStrength.PROPOSED)
    profile = _profile(db)
    f = profile.data_role
    assert f.display.value == "crosswalk"          # display vocabulary
    assert f.load_bearing is None                  # advisory: never operational
    # The evidence stream still says `bridge` — no rewrite happened.
    stored = db.execute("SELECT proposed_value FROM field_evidence WHERE field_name = "
                        "'table_role'").fetchone()[0]
    assert stored == "bridge"


def test_technical_catalog_business_context_is_the_only_table_text(db):
    """Technical ingestion writes NO source-attested table narrative: description stays honestly
    undecided:no_evidence, and a set_advisory business_context is the only table prose."""
    _seed_graph(db)
    _correct(db, "business_context", "set_advisory", ADMIN_A, "tc-1",
             replacement_value="Branch-booked retail orders.")
    profile = _profile(db)
    assert profile.description.state == "no_evidence"
    assert profile.description.unresolved_reason == "no_evidence"
    assert profile.business_context.display.value == "Branch-booked retail orders."


def test_no_displayable_evidence_is_undecided_not_display_only(db):
    """F9: `state="display_only"` with `display=null` was a lying state. Evidence below every
    display rule (the ordinary profiler default) now reports the honest family-shaped
    `undecided` — display_only is reserved for fields that actually display something."""
    _seed_graph(db)
    _seed(db, "authority_role", "derived", EvidenceProducer.PROFILER,
          AssertionStrength.SUPPORTED)
    f = _profile(db).authority_role
    assert f.display is None
    assert f.state == "undecided"
    assert f.unresolved_reason == "authority_insufficient"
    assert f.unresolved_family == "undecided"


def test_pending_proposal_below_the_display_bar_is_undecided(db):
    """F9 on a RECOMMENDATION field: a lone HUMAN/PROPOSED definition is below `description`'s
    display rule (human evidence renders only at CONFIRMED there), so nothing displays — the
    state is honestly `undecided`, never display_only-with-nothing."""
    _seed_graph(db)
    _seed(db, "definition", "A pending def.", EvidenceProducer.HUMAN,
          AssertionStrength.PROPOSED)
    f = _profile(db).description
    assert f.display is None
    assert f.state == "undecided"
    assert f.unresolved_reason == "authority_insufficient"
    assert f.unresolved_family == "undecided"


def test_profile_returns_none_for_an_unknown_table(db):
    assert build_dataset_profile(db, source=_SRC,
                                 dataset_logical_ref=normalize_ref(_SRC, None, "ghost")) is None


def test_profile_refuses_a_column_ref(db):
    with pytest.raises(ValueError):
        build_dataset_profile(
            db, source=_SRC,
            dataset_logical_ref=normalize_ref(_SRC, None, "orders", "amount"))


# ── dataset_profile_hash: stable + sensitive to every meaning-bearing input ──────────────────────


def test_hash_is_stable_across_rebuilds(db):
    _seed_graph(db)
    _seed(db, "definition", "Orders booked.", EvidenceProducer.SOURCE, AssertionStrength.ATTESTED)
    assert _profile(db).dataset_profile_hash == _profile(db).dataset_profile_hash


def test_hash_moves_on_every_meaning_bearing_input(db):
    _seed_graph(db)
    seen = {_profile(db).dataset_profile_hash}

    def _assert_moved():
        h = _profile(db).dataset_profile_hash
        assert h not in seen, "a meaning-bearing change did not move dataset_profile_hash"
        seen.add(h)

    # 1. new evidence (display resolution changes)
    _seed(db, "definition", "Orders booked.", EvidenceProducer.SOURCE, AssertionStrength.ATTESTED)
    _assert_moved()
    # 2. authority change on the same value (proposed -> confirmed via four-eyes)
    _correct(db, "authority_role", "propose_override", ADMIN_A, "h-p",
             replacement_value="system_of_record")
    _assert_moved()
    _correct(db, "authority_role", "confirm_override", ADMIN_B, "h-c",
             replacement_value="system_of_record")
    _assert_moved()
    # 3. a governed fact head appears (the graph stamp is the identity)
    db.execute("UPDATE graph_node SET grain_fact_event_id = 'evt-grain-1' "
               "WHERE catalog_source = %s AND object_ref = %s AND kind = 'table'",
               (_SRC, _TABLE_OBJECT_REF))
    _assert_moved()
    # 4. the current catalog narrative revision id (D12.10: narrative edits re-key profiles)
    h_rev_a = _profile(db, revision_id="cpr_a").dataset_profile_hash
    assert h_rev_a not in seen
    # ...but the SAME revision id twice is byte-stable (deterministic revision ids make a
    # byte-identical re-authoring resolve to the same id, so it cannot churn the hash).
    assert _profile(db, revision_id="cpr_a").dataset_profile_hash == h_rev_a


def test_projection_lag_explains_the_fact_head_hash_movement(db, monkeypatch):
    """F4: the fact heads read the TABLE-node graph stamps, and a lagged ingest REBUILDS the graph
    but SKIPS restoring them (`ingest.py` table_fact_projection "lagged" branch) — so a governed
    fact silently vanishes from the profile and `dataset_profile_hash` moves with no explanation.
    The identity is NOT re-keyed (still the graph stamp); the builder names the lag as its own
    missing-context code so the movement is explained."""
    from featuregen.overlay.upload import dataset_profiles as dp

    _seed_graph(db)
    db.execute("UPDATE graph_node SET grain_fact_event_id = 'evt-grain-1' "
               "WHERE catalog_source = %s AND object_ref = %s AND kind = 'table'",
               (_SRC, _TABLE_OBJECT_REF))
    steady = _profile(db)
    assert steady.grain_fact.confirmed_event_id == "evt-grain-1"   # identity: the graph stamp
    assert "fact_heads:projection_lagged" not in steady.missing_context

    # A lagged ingest: build_graph wiped the stamp and project_table_facts was skipped.
    monkeypatch.setattr(dp, "projection_lag", lambda conn, name: 1)
    db.execute("UPDATE graph_node SET grain_fact_event_id = NULL "
               "WHERE catalog_source = %s AND object_ref = %s AND kind = 'table'",
               (_SRC, _TABLE_OBJECT_REF))
    lagged = _profile(db)
    assert lagged.grain_fact is None
    assert "grain_fact:absent" in lagged.missing_context
    assert "fact_heads:projection_lagged" in lagged.missing_context
    assert lagged.dataset_profile_hash != steady.dataset_profile_hash


def test_projection_lag_code_is_absent_when_the_projection_is_caught_up(db):
    """F4 boundary: the code is emitted ONLY under real lag — an ordinary caught-up build never
    carries it (so it cannot churn the hash of every profile)."""
    _seed_graph(db)
    assert "fact_heads:projection_lagged" not in _profile(db).missing_context


def test_hash_ignores_wall_clock_and_row_identity_noise(db):
    """Two builds straddling unrelated writes (a different table's evidence) do not move."""
    _seed_graph(db)
    before = _profile(db).dataset_profile_hash
    other = normalize_ref(_SRC, None, "customers")
    record_field_evidence(
        db, logical_ref=other, field_name="definition", proposed_value="Customers.",
        producer=EvidenceProducer.SOURCE, strength=AssertionStrength.ATTESTED,
        producer_ref="test-producer", source_snapshot_id="snap-2",
        input_hash=field_input_hash(logical_ref=other, field_name="definition", material="c"))
    assert _profile(db).dataset_profile_hash == before
