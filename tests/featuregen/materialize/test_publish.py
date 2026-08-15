"""Spec A Task 16 (step 1) — §10.3: an attestation can only exist by ingesting a probe result.

**Why several tests here read a signature instead of exercising behaviour.** They assert that
something is *impossible to express*, and no behavioural test can do that. A behavioural test can
show that `passed=True` was not stored on some particular call; only `inspect.signature` can show
there is no way to ask for it. Three properties are pinned that way, and each one is a defect the
spec records having actually shipped in an earlier revision:

* `record_attestation(conn, probe_result)` — **no `passed=True` back door.** Rev 3 allowed one, so
  the live test proved that somebody had stored a boolean rather than that publication is atomic.
* `adds_feature` is **not** a parameter of `select_publisher`. Rev 3 let the caller supply it, so
  passing `False` skipped the schema-evolution requirement entirely.
* `render_publish` takes a **selection**, never a mechanism. A mechanism is a name anybody can
  spell; a selection is the evidence a mechanism was proven for this environment at these engine
  versions.

The signature tests are paired with behavioural ones wherever behaviour *can* show it — but the
signature test is the one that fails if a future caller gains a way to smuggle capability in.

**The plan's snippet for the first refusal is wrong and is corrected here.**
`CAPABILITY_UNPROVEN` is a `PublicationRefusalCode`, not a `CompilationRefusalCode`
(`codes.py:93`), and the plan's line carries the exact raw-string fallback
(`or r.code == "CAPABILITY_UNPROVEN"`) that the enum's own docstring names as the fossil of typing
publication decisions as compilation ones. The typed member is asserted and the fallback is gone —
a `StrEnum` member compares equal to its own value anyway, so the fallback never discriminated
anything except a bug.
"""
from __future__ import annotations

import dataclasses
import inspect
import re

import psycopg
import pytest
from tests.featuregen.materialize import fixtures
from tests.featuregen.materialize.test_group_plan import (
    COUNT_90D,
    GROUP,
    SUM_30D,
    _feature,
    _plan,
)

from featuregen.materialize import binding
from featuregen.materialize import publish as publish_module
from featuregen.materialize.canonical import materialize_hash
from featuregen.materialize.codes import (
    CompilationRefusalCode,
    MaterializationRefused,
    PublicationRefusalCode,
)
from featuregen.materialize.group_plan import expected_schema
from featuregen.materialize.inventory import EngineVersions
from featuregen.materialize.publish import (
    ProbeObservation,
    ProbeResult,
    PublicationCapabilityAttestation,
    PublisherSelection,
    PublishMechanism,
    adds_feature_for,
    assess_probe_observations,
    read_attestations,
    record_attestation,
    select_publisher,
)
from featuregen.materialize.render.project import REQUIRED_RUN_PARAMETERS
from featuregen.materialize.render.publish import (
    RENDERABLE_MECHANISMS,
    publish_entry_body,
    render_publish,
)

ENV = "hdfc-local"
OTHER_ENV = "hdfc-uat"
T0 = "2026-07-28T09:00:00+00:00"

#: What `hdfc-local` runs. `fixtures.ENGINE_VERSIONS` is spark 3.5.1 / hive 3.1.3 / metastore 3.1.3.
VERSIONS = fixtures.ENGINE_VERSIONS
#: The same cluster after a Spark upgrade — the drift case. Only `spark` moves, so a test that
#: passes here cannot be passing because two unrelated triples differ in every field.
VERSIONS_SPARK_3_4 = dataclasses.replace(VERSIONS, spark="3.4.1")


# ── probe evidence, built the way the live probe will build it ───────────────────────────────────


def _observation(reader: str, generation: str, *, columns: tuple[str, ...],
                 rows: int = 10, digest: str | None = None) -> ProbeObservation:
    return ProbeObservation(
        reader_id=reader, observed_at=T0, generation_id=generation, column_names=columns,
        row_count=rows, content_digest=digest or f"digest-{generation}")


_NARROW = ("cif_id", "business_dt", SUM_30D, "__generation_id")
_WIDE = (*_NARROW[:-1], COUNT_90D, "__generation_id")


def _clean_swap(columns: tuple[str, ...] = _NARROW, *, tag: str = "") -> list[ProbeObservation]:
    """Readers polling across an A→B swap, every one of them seeing a complete state."""
    return [
        _observation(f"reader-1{tag}", f"gen-A{tag}", columns=columns),
        _observation(f"reader-2{tag}", f"gen-A{tag}", columns=columns),
        _observation(f"reader-3{tag}", f"gen-B{tag}", columns=columns, rows=12),
        _observation(f"reader-4{tag}", f"gen-B{tag}", columns=columns, rows=12),
    ]


def _schema_evolution_run() -> list[ProbeObservation]:
    """§10.3 step 5: the whole sequence, repeated while ADDING a feature column."""
    return [*_clean_swap(_NARROW), *_clean_swap(_WIDE, tag="-evo")]


def _result(observations, *, probe_id="probe-1", environment_id=ENV,
            mechanism=PublishMechanism.VERSIONED_POINTER, engine_versions=None) -> ProbeResult:
    return assess_probe_observations(
        observations, probe_id=probe_id, environment_id=environment_id, mechanism=mechanism,
        engine_versions=engine_versions or VERSIONS, completed_at=T0)


def _record(db, observations, **kwargs) -> PublicationCapabilityAttestation:
    return record_attestation(db, _result(observations, **kwargs))


def _select(db, *, environment_id=ENV, engine_versions=None,
            mechanism=PublishMechanism.VERSIONED_POINTER, group_plan=None, published_schema=None):
    plan = _plan() if group_plan is None else group_plan
    return select_publisher(
        db, environment_id=environment_id, engine_versions=engine_versions or VERSIONS,
        mechanism=mechanism, group_plan=plan, published_schema=published_schema)


def _published_columns(plan) -> list[str]:
    """Exactly what the plan expects — the "nothing is being added" case."""
    return [column.name for column in expected_schema(plan)]


# ══ 1. the back door that must not exist ═════════════════════════════════════════════════════════


def test_record_attestation_accepts_ONLY_a_probe_result() -> None:
    """No `passed=True`, no `mechanism=`, no `environment_id=`. Rev 3's defect, made unspellable."""
    params = inspect.signature(record_attestation).parameters
    assert set(params) - {"conn"} == {"probe_result"}


def test_no_parameter_anywhere_in_the_module_can_assert_capability() -> None:
    """Stronger than the one signature: NOTHING public here takes a bare capability claim.

    `record_attestation` could be clean while a sibling writer took `passed=`, and the attestation
    would be back — recorded by the other door. Not even `assess_probe_observations` is exempt:
    it DERIVES the verdict from the readings it is given, so it has no reason to accept one, and
    exempting it would leave the one function whose whole job is the verdict able to be handed it.
    """
    offending = {"passed", "covers_schema_evolution", "adds_feature", "evidence_hash"}
    for name in publish_module.__all__:
        member = getattr(publish_module, name)
        if not inspect.isfunction(member):
            continue
        assert not (set(inspect.signature(member).parameters) & offending), (
            f"{name} takes a capability claim as a parameter")


def test_a_probe_result_cannot_CLAIM_a_pass_its_observations_do_not_support() -> None:
    """The structural half: `record_attestation`'s signature is only meaningful if a `ProbeResult`
    cannot be hand-built with a lie in it."""
    honest = _result(_clean_swap())
    with pytest.raises(ValueError, match="derived from"):
        dataclasses.replace(honest, passed=False)
    vacuous = _result([])
    assert vacuous.passed is False
    with pytest.raises(ValueError, match="derived from"):
        dataclasses.replace(vacuous, passed=True)


def test_a_probe_that_observed_NOTHING_cannot_pass() -> None:
    """§10.3's own step-2 test says so: `assert result.observations` — it would pass vacuously."""
    assert _result([]).passed is False


def test_readers_that_only_ever_saw_ONE_generation_watched_no_swap() -> None:
    """Every observation is a complete A state, and that proves nothing about the transition."""
    only_a = [_observation("reader-1", "gen-A", columns=_NARROW),
              _observation("reader-2", "gen-A", columns=_NARROW)]
    assert _result(only_a).passed is False


def test_a_TORN_read_fails_the_probe() -> None:
    """Two readers carrying the same generation marker over different content: one saw a half-
    written state. §10.3 step 4 — schema and row count alone can coincide, so the marker is the
    discriminator and the content check is what proves it was not attached to a partial state."""
    torn = _clean_swap()
    torn[1] = _observation("reader-2", "gen-A", columns=_NARROW, digest="digest-half-written")
    assert _result(torn).passed is False


def test_a_torn_read_is_caught_by_ROW_COUNT_and_by_SCHEMA_too() -> None:
    for field, value in (("rows", 99), ("columns", (*_NARROW, "extra_col"))):
        torn = _clean_swap()
        torn[1] = _observation("reader-2", "gen-A", columns=_NARROW if field == "rows" else value,
                               rows=value if field == "rows" else 10)
        assert _result(torn).passed is False, field


def test_the_evidence_hash_is_over_the_observations_AND_the_verdict() -> None:
    """Hashing the observations alone would let a real failing probe's readings be relabelled."""
    result = _result(_clean_swap())
    assert result.evidence_hash == materialize_hash({
        "environment_id": ENV,
        "mechanism": "VERSIONED_POINTER",
        "engine_versions": VERSIONS.identity_payload(),
        "observations": [o.identity_payload() for o in result.observations],
        "passed": True,
        "covers_schema_evolution": False,
    })
    with pytest.raises(ValueError, match="evidence_hash"):
        dataclasses.replace(result, evidence_hash="sha256-of-nothing")


def test_the_evidence_hash_moves_with_a_SINGLE_observation() -> None:
    one = _result(_clean_swap())
    changed = _clean_swap()
    changed[0] = _observation("reader-1", "gen-A", columns=_NARROW, rows=11, digest="digest-gen-A")
    assert _result(changed).evidence_hash != one.evidence_hash


def test_record_attestation_refuses_anything_that_is_not_a_probe_result(db) -> None:
    """A duck-typed stand-in is a way to supply the verdict directly."""
    class LooksLikeOne:
        probe_id, environment_id, passed = "p", ENV, True
        covers_schema_evolution, evidence_hash, completed_at = True, "e", T0
        mechanism, engine_versions = PublishMechanism.VERSIONED_POINTER, VERSIONS

    with pytest.raises(TypeError, match="ProbeResult"):
        record_attestation(db, LooksLikeOne())  # type: ignore[arg-type]


# ══ 2. §10.3's schema-evolution coverage ═════════════════════════════════════════════════════════


def test_a_swap_that_never_widened_the_schema_does_not_cover_schema_evolution() -> None:
    """A partition-location swap does not atomically change table SCHEMA."""
    result = _result(_clean_swap())
    assert result.passed is True
    assert result.covers_schema_evolution is False


def test_repeating_the_sequence_while_ADDING_a_column_covers_it() -> None:
    result = _result(_schema_evolution_run())
    assert result.passed is True
    assert result.covers_schema_evolution is True


def test_coverage_cannot_be_claimed_by_a_probe_that_did_not_pass() -> None:
    torn = _schema_evolution_run()
    torn[1] = _observation("reader-2", "gen-A", columns=_NARROW, digest="digest-half-written")
    assert _result(torn).covers_schema_evolution is False


def test_coverage_cannot_be_hand_set_on_a_result() -> None:
    with pytest.raises(ValueError, match="covers_schema_evolution"):
        dataclasses.replace(_result(_clean_swap()), covers_schema_evolution=True)


# ══ 3. selection — no attestation, wrong environment, drifted versions ═══════════════════════════


def test_no_attestation_means_no_publisher(db) -> None:
    """CORRECTED from the plan: `CAPABILITY_UNPROVEN` is a `PublicationRefusalCode`, and the
    raw-string fallback the plan carries is the fossil of typing it as a compilation refusal."""
    refusal = _select(db)
    assert isinstance(refusal, MaterializationRefused)
    assert refusal.code is PublicationRefusalCode.CAPABILITY_UNPROVEN
    assert not isinstance(refusal.code, CompilationRefusalCode)
    assert "CAPABILITY_UNPROVEN" not in {code.value for code in CompilationRefusalCode}


def test_attestation_for_another_environment_does_not_count(db) -> None:
    """And the refusal must NAME the environment that was asked about, or an operator reading it
    would go and look at the cluster that already has one."""
    _record(db, _schema_evolution_run(), environment_id=OTHER_ENV)
    refusal = _select(db, environment_id=ENV)
    assert isinstance(refusal, MaterializationRefused)
    assert refusal.code is PublicationRefusalCode.CAPABILITY_UNPROVEN
    assert ENV in refusal.detail
    assert OTHER_ENV not in refusal.detail


def test_the_other_environments_attestation_is_still_selectable_FOR_IT(db) -> None:
    """The scoping is by environment, not a blanket refusal — otherwise the previous test would
    pass against a `select_publisher` that never selects anything."""
    _record(db, _schema_evolution_run(), environment_id=OTHER_ENV)
    selection = _select(db, environment_id=OTHER_ENV)
    assert isinstance(selection, PublisherSelection)
    assert selection.environment_id == OTHER_ENV


def test_engine_version_drift_invalidates_the_attestation(db) -> None:
    """A mechanism proven on Spark 3.4 is not proven on 3.5."""
    _record(db, _schema_evolution_run(), engine_versions=VERSIONS_SPARK_3_4)
    refusal = _select(db, engine_versions=VERSIONS)
    assert isinstance(refusal, MaterializationRefused)
    assert refusal.code is PublicationRefusalCode.CAPABILITY_UNPROVEN
    assert "3.4.1" in refusal.detail and "3.5.1" in refusal.detail


def test_drift_is_UNPROVEN_and_not_a_demonstrated_failure(db) -> None:
    """Nobody probed 3.5 at all, so reporting `PUBLISH_MECHANISM_UNSUPPORTED` would be inventing an
    observation — and would tell an operator to change the design instead of running the probe."""
    _record(db, _schema_evolution_run(), engine_versions=VERSIONS_SPARK_3_4)
    refusal = _select(db, engine_versions=VERSIONS)
    assert refusal.code is not PublicationRefusalCode.PUBLISH_MECHANISM_UNSUPPORTED


@pytest.mark.parametrize("field", ["hive", "spark", "metastore"])
def test_drift_in_ANY_of_the_three_keyed_versions_invalidates(db, field: str) -> None:
    _record(db, _schema_evolution_run(),
            engine_versions=dataclasses.replace(VERSIONS, **{field: "0.0.0-probed"}))
    refusal = _select(db, engine_versions=VERSIONS)
    assert isinstance(refusal, MaterializationRefused), field


@pytest.mark.parametrize("field", ["python", "java", "pyspark", "kedro", "kedro_datasets"])
def test_the_five_PROJECT_pins_are_not_part_of_the_capability_key(db, field: str) -> None:
    """The attestation is keyed on hive/spark/metastore (migration 1034). The other five describe
    what the GENERATED PROJECT pins (§7), and re-probing a cluster because a kedro pin moved would
    make the probe un-runnable in practice."""
    _record(db, _schema_evolution_run(),
            engine_versions=dataclasses.replace(VERSIONS, **{field: "0.0.0-probed"}))
    assert isinstance(_select(db, engine_versions=VERSIONS), PublisherSelection), field


def test_a_matching_attestation_selects(db) -> None:
    attestation = _record(db, _schema_evolution_run())
    selection = _select(db)
    assert isinstance(selection, PublisherSelection)
    assert selection.capability_attestation_id == attestation.attestation_id
    assert selection.mechanism is PublishMechanism.VERSIONED_POINTER
    assert selection.engine_versions == VERSIONS


def test_an_attestation_for_ANOTHER_MECHANISM_does_not_count(db) -> None:
    _record(db, _schema_evolution_run(), mechanism=PublishMechanism.SET_LOCATION)
    refusal = _select(db, mechanism=PublishMechanism.VERSIONED_POINTER)
    assert isinstance(refusal, MaterializationRefused)
    assert refusal.code is PublicationRefusalCode.CAPABILITY_UNPROVEN


# ══ 4. the two refusal codes are genuinely different answers ═════════════════════════════════════


def test_a_FAILED_probe_is_recorded_rather_than_discarded(db) -> None:
    """It is the only evidence that can tell "go run the probe" from "the design must change"."""
    torn = _clean_swap()
    torn[1] = _observation("reader-2", "gen-A", columns=_NARROW, digest="digest-half-written")
    attestation = _record(db, torn)
    assert attestation.passed is False
    assert read_attestations(
        db, environment_id=ENV, mechanism=PublishMechanism.VERSIONED_POINTER) == (attestation,)


def test_a_probe_that_RAN_and_failed_is_PUBLISH_MECHANISM_UNSUPPORTED(db) -> None:
    torn = _clean_swap()
    torn[1] = _observation("reader-2", "gen-A", columns=_NARROW, digest="digest-half-written")
    _record(db, torn)
    refusal = _select(db)
    assert isinstance(refusal, MaterializationRefused)
    assert refusal.code is PublicationRefusalCode.PUBLISH_MECHANISM_UNSUPPORTED
    assert refusal.code is not PublicationRefusalCode.CAPABILITY_UNPROVEN


def test_a_later_PASSING_probe_overrides_a_STRICTLY_older_failure(db) -> None:
    """The cluster was fixed. A refusal that outlived the evidence would be a stored verdict.

    "Later" means a strictly newer `recorded_at`. Two probes recorded in production land in two
    transactions with two `now()` stamps; the suite's single rolled-back transaction would tie
    them at one, which is the ambiguity the tie guard refuses. The plane is append-only (its
    trigger forbids UPDATE), so the strictly-older failure is INSERTed with the explicit
    `recorded_at` an earlier transaction would have stamped — same table, same columns, one
    minute older."""
    db.execute(
        "INSERT INTO publication_capability_attestation (attestation_id, environment_id, "
        "hive_version, spark_version, metastore_version, mechanism, passed, "
        "covers_schema_evolution, evidence_hash, attested_at, recorded_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, false, false, %s, %s, now() - interval '1 minute')",
        ("probe-failed", ENV, VERSIONS.hive, VERSIONS.spark, VERSIONS.metastore,
         PublishMechanism.VERSIONED_POINTER.value, "digest-half-written", T0))
    _record(db, _schema_evolution_run(), probe_id="probe-passed")
    assert isinstance(_select(db), PublisherSelection)


@pytest.mark.parametrize("order", ["fail_then_pass", "pass_then_fail"])
def test_a_pass_and_fail_that_TIE_on_recorded_at_refuse_in_both_orders(db, order: str) -> None:
    """I4 (final review): `now()` is fixed at the TRANSACTION in Postgres, so a pass and a fail
    ingested in one transaction share one `recorded_at` — and `ORDER BY recorded_at` over a tie
    returns whichever physical order the heap happens to hold. "The newest evidence" is then a
    claim the ordering cannot support, in EITHER insertion order, so the tie fails closed: a pass
    supersedes a failure only when it is STRICTLY newer."""
    torn = _clean_swap()
    torn[1] = _observation("reader-2", "gen-A", columns=_NARROW, digest="digest-half-written")
    if order == "fail_then_pass":
        _record(db, torn, probe_id="probe-failed")
        _record(db, _schema_evolution_run(), probe_id="probe-passed")
    else:
        _record(db, _schema_evolution_run(), probe_id="probe-passed")
        _record(db, torn, probe_id="probe-failed")
    first, second = read_attestations(
        db, environment_id=ENV, mechanism=PublishMechanism.VERSIONED_POINTER)
    assert first.recorded_at == second.recorded_at   # the tie is a fact, not an assumption
    refused = _select(db)
    assert isinstance(refused, MaterializationRefused)
    assert refused.code is PublicationRefusalCode.PUBLISH_MECHANISM_UNSUPPORTED
    assert "probe-failed" in refused.detail


def test_a_later_FAILING_probe_defeats_an_earlier_pass(db) -> None:
    """The mirror of the test above: the most recent evidence on IDENTICAL engine versions says
    the mechanism is broken, so publication must not proceed on stale success. The earlier pass
    here even covers schema evolution — the covering branch must not resurrect it either."""
    _record(db, _schema_evolution_run(), probe_id="probe-passed")
    torn = _clean_swap()
    torn[1] = _observation("reader-2", "gen-A", columns=_NARROW, digest="digest-half-written")
    _record(db, torn, probe_id="probe-failed")
    refused = _select(db)
    assert isinstance(refused, MaterializationRefused)
    assert refused.code is PublicationRefusalCode.PUBLISH_MECHANISM_UNSUPPORTED
    assert "probe-failed" in refused.detail


def test_the_newest_failure_refuses_even_when_nothing_is_being_added(db) -> None:
    """Same inversion on the no-schema-change branch (`adds_feature=False`): the stale pass would
    be `passing[-1]` there, so the newest-evidence check must sit ahead of BOTH selection arms."""
    plan = _plan()
    _record(db, _clean_swap(), probe_id="probe-passed")
    torn = _clean_swap()
    torn[1] = _observation("reader-2", "gen-A", columns=_NARROW, digest="digest-half-written")
    _record(db, torn, probe_id="probe-failed")
    refused = _select(db, group_plan=plan, published_schema=_published_columns(plan))
    assert isinstance(refused, MaterializationRefused)
    assert refused.code is PublicationRefusalCode.PUBLISH_MECHANISM_UNSUPPORTED
    assert "probe-failed" in refused.detail


def test_one_probe_can_support_at_most_one_attestation(db) -> None:
    result = _result(_schema_evolution_run())
    record_attestation(db, result)
    with pytest.raises(psycopg.errors.UniqueViolation):
        record_attestation(db, result)


# ══ 5. adds_feature is DERIVED, never passed ═════════════════════════════════════════════════════


def test_adds_feature_is_DERIVED_not_passed() -> None:
    """A caller who could pass it could lie about it (spec §10.3, Rev 3's defect)."""
    assert "adds_feature" not in inspect.signature(select_publisher).parameters


def test_select_publisher_takes_EXACTLY_the_six_inputs_it_derives_from() -> None:
    """Pins the parameter list, so `adds_feature` cannot return under another name either."""
    assert set(inspect.signature(select_publisher).parameters) == {
        "conn", "environment_id", "engine_versions", "mechanism", "group_plan", "published_schema"}


def test_published_schema_has_no_default_so_it_cannot_be_forgotten() -> None:
    """Omitting it would silently inherit an answer about what is published."""
    param = inspect.signature(select_publisher).parameters["published_schema"]
    assert param.default is inspect.Parameter.empty


def test_a_published_schema_that_already_has_every_column_adds_nothing() -> None:
    plan = _plan()
    assert adds_feature_for(plan, _published_columns(plan)) is False


def test_a_plan_carrying_a_column_the_table_lacks_ADDS_a_feature() -> None:
    plan = _plan(_feature(SUM_30D), _feature(COUNT_90D))
    published = [c for c in _published_columns(plan) if c != COUNT_90D]
    assert adds_feature_for(plan, published) is True


def test_a_MISSING_SYSTEM_column_is_a_schema_change_too() -> None:
    """§10.2's three columns are part of `expected_schema`, so a table without them is a table the
    publication would have to widen — which a partition swap does not do atomically."""
    plan = _plan()
    published = [c for c in _published_columns(plan) if not c.startswith("__")]
    assert adds_feature_for(plan, published) is True


def test_column_names_are_case_folded_because_unquoted_hive_identifiers_fold() -> None:
    plan = _plan()
    assert adds_feature_for(plan, [c.upper() for c in _published_columns(plan)]) is False


def test_NO_published_schema_fails_CLOSED(db) -> None:
    """`None` is also what a caller who never looked would pass. §10.3 step 5 requires the probe to
    cover adding a column anyway, so the safe reading costs nothing a real attestation lacks."""
    plan = _plan()
    assert adds_feature_for(plan, None) is True
    _record(db, _schema_evolution_run())
    selection = _select(db, group_plan=plan, published_schema=None)
    assert isinstance(selection, PublisherSelection)
    assert selection.adds_feature is True


def test_adding_a_feature_needs_schema_evolution_coverage(db) -> None:
    plan = _plan(_feature(SUM_30D), _feature(COUNT_90D))
    _record(db, _clean_swap())  # passes, but never widened a schema
    refusal = _select(db, group_plan=plan,
                      published_schema=[c for c in _published_columns(plan) if c != COUNT_90D])
    assert isinstance(refusal, MaterializationRefused)
    assert refusal.code is PublicationRefusalCode.CAPABILITY_UNPROVEN
    assert "schema" in refusal.detail.lower()


def test_the_SAME_attestation_publishes_fine_when_nothing_is_added(db) -> None:
    """The control that makes the previous test about schema evolution rather than about the
    attestation being unusable for anything."""
    plan = _plan(_feature(SUM_30D), _feature(COUNT_90D))
    _record(db, _clean_swap())
    selection = _select(db, group_plan=plan, published_schema=_published_columns(plan))
    assert isinstance(selection, PublisherSelection)
    assert selection.adds_feature is False


def test_a_covering_attestation_lets_the_added_column_publish(db) -> None:
    plan = _plan(_feature(SUM_30D), _feature(COUNT_90D))
    _record(db, _schema_evolution_run())
    selection = _select(db, group_plan=plan,
                        published_schema=[c for c in _published_columns(plan) if c != COUNT_90D])
    assert isinstance(selection, PublisherSelection)
    assert selection.adds_feature is True


def test_selection_prefers_a_COVERING_attestation_when_a_bare_one_also_exists(db) -> None:
    """A non-covering pass recorded later must not shadow the covering one."""
    plan = _plan(_feature(SUM_30D), _feature(COUNT_90D))
    covering = _record(db, _schema_evolution_run(), probe_id="probe-covering")
    _record(db, _clean_swap(), probe_id="probe-bare")
    selection = _select(db, group_plan=plan,
                        published_schema=[c for c in _published_columns(plan) if c != COUNT_90D])
    assert isinstance(selection, PublisherSelection)
    assert selection.capability_attestation_id == covering.attestation_id


# ══ 6. the renderer consumes a SELECTION ═════════════════════════════════════════════════════════


@pytest.fixture
def selection() -> PublisherSelection:
    return PublisherSelection(
        environment_id=ENV, mechanism=PublishMechanism.VERSIONED_POINTER,
        capability_attestation_id="att-16", engine_versions=VERSIONS, adds_feature=False)


def test_render_publish_consumes_a_SELECTION_not_a_mechanism() -> None:
    params = inspect.signature(render_publish).parameters
    assert "selection" in params
    assert "mechanism" not in params


def test_no_render_entry_point_anywhere_accepts_a_bare_mechanism() -> None:
    """Same reasoning as the module-wide back-door test: `render_publish` could be clean while
    `publish_entry_body` took a mechanism, and the un-evidenced entry would be back."""
    for func in (render_publish, publish_entry_body):
        assert "mechanism" not in inspect.signature(func).parameters, func.__name__


def test_render_publish_refuses_a_mechanism_it_was_not_given_evidence_for() -> None:
    with pytest.raises(TypeError, match="PublisherSelection"):
        render_publish(_plan(), selection=PublishMechanism.VERSIONED_POINTER)  # type: ignore[arg-type]


def test_no_insert_overwrite_anywhere(selection: PublisherSelection) -> None:
    assert "INSERT OVERWRITE" not in render_publish(_plan(), selection=selection).upper()


def test_no_write_mode_the_spec_bans_appears_in_the_rendered_entry(
        selection: PublisherSelection) -> None:
    """`INSERT OVERWRITE` is the banned SQL; `overwrite` / `append` / `upsert` are the dataset
    modes that would achieve the same un-attested replacement through Kedro."""
    rendered = render_publish(_plan(), selection=selection)
    body = "\n".join(line for line in rendered.splitlines() if not line.lstrip().startswith("#"))
    for banned in ("overwrite", "append", "upsert", "insert overwrite"):
        assert banned not in body.lower(), banned


def test_the_rendered_entry_names_the_ATTESTATION_it_rests_on(
        selection: PublisherSelection) -> None:
    rendered = render_publish(_plan(), selection=selection)
    assert "att-16" in rendered
    assert "VERSIONED_POINTER" in rendered
    assert ENV in rendered


def test_the_rendered_target_is_GENERATION_SCOPED(selection: PublisherSelection) -> None:
    """This is what lifts the blocker: `errorifexists` no longer blocks a re-run, because
    `staging_root` resolves to `<base>/<generation_id>` and each generation writes somewhere new."""
    rendered = render_publish(_plan(), selection=selection)
    assert "${runtime_params:staging_root}" in rendered
    assert 'mode: "errorifexists"' in rendered


def test_the_rendered_entry_introduces_NO_new_run_parameter(
        selection: PublisherSelection) -> None:
    """The rendered hooks refuse a run carrying an unexpected runtime parameter, so a publication
    entry that interpolated one nobody prepared would fail every run at `before_pipeline_run`."""
    rendered = render_publish(_plan(), selection=selection)
    referenced = set(re.findall(r"\$\{runtime_params:([a-z_]+)\}", rendered))
    assert referenced <= set(REQUIRED_RUN_PARAMETERS), sorted(referenced)


def test_the_publication_target_is_DERIVED_from_the_plan_not_passed(
        selection: PublisherSelection) -> None:
    assert "published_target" not in inspect.signature(render_publish).parameters
    rendered = render_publish(_plan(), selection=selection)
    assert f"sandbox_feature.{GROUP}" in rendered


def test_a_dotted_namespace_keeps_the_filepath_tail_a_BARE_table(
        selection: PublisherSelection, monkeypatch) -> None:
    """`split(".", 1)` on `lake.sandbox_feature.cif_daily` yields the tail
    `sandbox_feature.cif_daily` — a path segment carrying half the namespace. The LAST dot
    separates namespace from table: the sandbox namespace may itself be catalog-qualified, and
    the group name (a hive identifier) never carries a dot."""
    monkeypatch.setattr(binding, "SANDBOX_NAMESPACE", "lake.sandbox_feature")
    rendered = render_publish(_plan(), selection=selection)
    filepath = next(line for line in rendered.splitlines()
                    if line.lstrip().startswith("filepath:"))
    assert filepath.rstrip().endswith(f'/{GROUP}"'), filepath
    assert f"sandbox_feature.{GROUP}" not in filepath, filepath


def test_a_mechanism_with_no_attested_rendering_is_refused(db) -> None:
    """A probe attests what the CLUSTER does; it does not attest what this renderer knows how to
    emit. Both `EXCHANGE_PARTITION` and `SET_LOCATION` are selectable in principle and neither has
    a rendered form, so rendering one would publish on an assumed capability by another route."""
    for mechanism in set(PublishMechanism) - RENDERABLE_MECHANISMS:
        unrenderable = PublisherSelection(
            environment_id=ENV, mechanism=mechanism, capability_attestation_id="att-x",
            engine_versions=VERSIONS, adds_feature=False)
        with pytest.raises(MaterializationRefused) as caught:
            render_publish(_plan(), selection=unrenderable)
        assert caught.value.code is PublicationRefusalCode.PUBLISH_MECHANISM_UNSUPPORTED


# ══ 7. the vocabulary, and the shapes ════════════════════════════════════════════════════════════


def test_INSERT_OVERWRITE_is_not_a_member_of_the_mechanism_vocabulary() -> None:
    """§10 rejects it outright, so there must be nothing for a probe to be pointed at."""
    members = {member.value for member in PublishMechanism}
    assert not any("OVERWRITE" in member for member in members), members


def test_the_three_publication_refusal_codes_are_the_ones_this_task_routes() -> None:
    assert {code.value for code in PublicationRefusalCode} == {
        "CAPABILITY_UNPROVEN", "GROUP_BINDING_CONFLICT", "PUBLISH_MECHANISM_UNSUPPORTED"}


@pytest.mark.parametrize("record", [ProbeObservation, ProbeResult,
                                    PublicationCapabilityAttestation, PublisherSelection])
def test_every_record_is_a_frozen_slotted_dataclass(record) -> None:
    """Frozen + slotted, not pydantic — the package-wide shape (§14)."""
    assert dataclasses.is_dataclass(record)
    assert record.__dataclass_params__.frozen, f"{record.__name__} is not frozen"
    assert getattr(record, "__slots__", None) is not None, f"{record.__name__} is not slotted"


def test_an_observation_carries_no_field_a_data_VALUE_could_occupy() -> None:
    """The control plane never reads feature data. A digest discriminates two complete states
    without being a sample of either."""
    assert {f.name for f in dataclasses.fields(ProbeObservation)} == {
        "reader_id", "observed_at", "generation_id", "column_names", "row_count", "content_digest"}


def test_an_attestation_carries_exactly_migration_1034s_columns() -> None:
    assert [f.name for f in dataclasses.fields(PublicationCapabilityAttestation)] == [
        "attestation_id", "environment_id", "hive_version", "spark_version", "metastore_version",
        "mechanism", "passed", "covers_schema_evolution", "evidence_hash", "attested_at",
        "recorded_at"]


def test_an_attestation_read_back_is_the_one_that_was_written(db) -> None:
    written = _record(db, _schema_evolution_run())
    assert read_attestations(
        db, environment_id=ENV, mechanism=PublishMechanism.VERSIONED_POINTER) == (written,)


def test_a_raw_mechanism_string_cannot_reach_a_probe_result_or_a_selection() -> None:
    with pytest.raises(TypeError, match="PublishMechanism"):
        ProbeResult(probe_id="p", environment_id=ENV, mechanism="VERSIONED_POINTER",  # type: ignore[arg-type]
                    engine_versions=VERSIONS, observations=(), passed=False,
                    covers_schema_evolution=False, evidence_hash="x", completed_at=T0)
    with pytest.raises(TypeError, match="PublishMechanism"):
        PublisherSelection(environment_id=ENV, mechanism="VERSIONED_POINTER",  # type: ignore[arg-type]
                           capability_attestation_id="a", engine_versions=VERSIONS,
                           adds_feature=False)


def test_select_publisher_refuses_a_raw_mechanism_or_a_loose_version_mapping(db) -> None:
    plan = _plan()
    with pytest.raises(TypeError, match="PublishMechanism"):
        select_publisher(db, environment_id=ENV, engine_versions=VERSIONS,
                         mechanism="VERSIONED_POINTER", group_plan=plan,  # type: ignore[arg-type]
                         published_schema=None)
    with pytest.raises(TypeError, match="EngineVersions"):
        select_publisher(db, environment_id=ENV, engine_versions={"spark": "3.5.1"},  # type: ignore[arg-type]
                         mechanism=PublishMechanism.VERSIONED_POINTER, group_plan=plan,
                         published_schema=None)


def test_the_module_writes_only_by_INSERT() -> None:
    """The plane is append-only, and this module writes TWO of its tables since G-3 —
    `publication_capability_attestation` and migration 1055's `feature_active_revision`. A
    publication path that learned to UPDATE could turn a failed attestation into a passing one
    without any probe running, or move a group's active-revision pointer without a run.

    Deliberately a substring scan over the WHOLE source rather than over the SQL literals: a
    statement assembled from fragments would evade a literal-only check, and the cost of the crude
    form is only that prose here must not use the three words either.
    """
    source = inspect.getsource(publish_module).upper()
    for forbidden in ("UPDATE ", "DELETE ", "TRUNCATE "):
        assert forbidden not in source, forbidden
    assert source.count("INSERT INTO ") == 2


def test_engine_versions_comparison_is_EXACT_and_not_an_ordering() -> None:
    """"3.5 is newer than 3.4, so it is fine" is a compatibility judgement nobody made."""
    attestation = PublicationCapabilityAttestation(
        attestation_id="a", environment_id=ENV, hive_version="3.1.3", spark_version="3.4.1",
        metastore_version="3.1.3", mechanism=PublishMechanism.VERSIONED_POINTER, passed=True,
        covers_schema_evolution=True, evidence_hash="e", attested_at=T0)
    assert attestation.matches(VERSIONS_SPARK_3_4) is True
    assert attestation.matches(VERSIONS) is False
    assert attestation.matches(dataclasses.replace(VERSIONS, spark="3.4.0")) is False


def test_no_pyspark_import_reaches_src() -> None:
    for module in (publish_module, __import__(
            "featuregen.materialize.render.publish", fromlist=["publish"])):
        assert "import pyspark" not in inspect.getsource(module)


def test_an_engine_versions_value_is_still_required_end_to_end() -> None:
    """A blank version pins nothing, and the attestation key would be a key over empty strings."""
    with pytest.raises(ValueError, match="blank"):
        EngineVersions(hive="", spark="3.5.1", metastore="3.1.3", python="3.11.9",
                       java="11.0.22", pyspark="3.5.1", kedro="0.19.9", kedro_datasets="4.1.0")


