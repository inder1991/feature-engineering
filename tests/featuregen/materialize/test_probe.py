"""§10.3 step 2's driver — DEFERRED-WORK A.26's constraint, tested as a property.

The one thing this module must never be able to do is state a verdict, so the first test is
structural rather than behavioural: it reads `probe.py`'s AST and asserts the module never
constructs a `ProbeResult` at all. A behavioural test could only ever prove that the verdict was
right for the inputs it happened to try; the AST test proves there is nowhere for a different one to
come from.

The LIVE probe against the kind cluster is an OPERATOR ACTION and is not run here. Nothing in this
file touches a cluster, a subprocess or `kubectl` — `_Cluster` is the seam, scripted per test.
"""
from __future__ import annotations

import ast
import inspect
import pathlib

import pytest
from tests.featuregen.materialize import fixtures

from featuregen.materialize import probe as probe_module
from featuregen.materialize.probe import PublicationTarget, probe_publication_capability
from featuregen.materialize.publish import ProbeObservation, ProbeResult, PublishMechanism
from featuregen.materialize.render.publish import RENDERABLE_MECHANISMS

ENV = "hdfc-local"
VERSIONS = fixtures.ENGINE_VERSIONS
_BASE = ("cif_id", "business_dt")
_FEATURE = "total_debit_amount_30d"


class _Cluster:
    """A publication target that behaves. Every reader sees the complete state most recently
    published, which is what an ATOMIC swap looks like from outside."""

    environment_id = ENV

    def __init__(self) -> None:
        self.published: list[tuple[str, tuple[str, ...]]] = []
        self.looks = 0

    def publish(self, *, generation_id: str, columns) -> None:
        self.published.append((generation_id, tuple(columns)))

    def observe(self, *, reader_id: str) -> ProbeObservation | None:
        self.looks += 1
        generation_id, columns = self.published[-1]
        return ProbeObservation(
            reader_id=reader_id, observed_at=f"2026-08-15T10:00:{self.looks:02d}+00:00",
            generation_id=generation_id, column_names=columns, row_count=len(columns) * 3,
            content_digest=f"digest-{generation_id}")


class _BlindCluster(_Cluster):
    """Every reader fails to read. A session that died mid-swap saw nothing, which is a different
    fact from seeing a torn state — and inventing an observation for it would invent evidence."""

    def observe(self, *, reader_id: str) -> ProbeObservation | None:
        self.looks += 1
        return None


class _TornCluster(_Cluster):
    """Readers disagree about the content behind ONE generation marker: the half-written state
    §10.3 step 4 says the marker alone cannot rule out."""

    def observe(self, *, reader_id: str) -> ProbeObservation | None:
        observation = super().observe(reader_id=reader_id)
        assert observation is not None
        if reader_id.endswith("-2"):
            return ProbeObservation(
                reader_id=observation.reader_id, observed_at=observation.observed_at,
                generation_id=observation.generation_id, column_names=observation.column_names,
                row_count=observation.row_count, content_digest="digest-half-written")
        return observation


def _probe(cluster, *, mechanism=PublishMechanism.VERSIONED_POINTER, readers=("r-1", "r-2"),
           columns=_BASE, feature_column=_FEATURE, probe_id="probe-live-1") -> ProbeResult:
    return probe_publication_capability(
        cluster, mechanism=mechanism, engine_versions=VERSIONS, probe_id=probe_id,
        readers=readers, columns=columns, feature_column=feature_column,
        clock=lambda: "2026-08-15T10:05:00+00:00")


# ══ 1. the driver states no verdict of its own ═══════════════════════════════════════════════════


def test_the_driver_states_no_verdict_of_its_own() -> None:
    """A.26, as a property of the source rather than of the paths a test happened to take.

    `ProbeResult`'s three verdict fields are re-derived at construction, so a module that could
    construct one could construct it from observations it chose — the filtered subset that looked
    good. There is exactly one legitimate `ProbeResult` in this driver's world and it comes back
    from `assess_probe_observations`, so the name must not be CALLED anywhere in the module.

    The import is allowed and is checked separately below: it is what types the return value, and a
    module that could not name the type could not annotate what it hands back.
    """
    source = pathlib.Path(inspect.getfile(probe_module)).read_text()
    called = {
        node.func.id for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "ProbeResult" not in called, "the driver constructs a verdict of its own"
    assert "assess_probe_observations" in called, "the driver never asks the assessor at all"


def test_the_driver_has_no_parameter_that_could_carry_a_verdict() -> None:
    """The signature half. A `passed=` or `covers_schema_evolution=` parameter would let the CALLER
    state what the readings should say, which is the Rev-3 back door one layer out."""
    params = set(inspect.signature(probe_publication_capability).parameters)
    assert not params & {"passed", "covers_schema_evolution", "evidence_hash", "result"}


def test_every_reading_reaches_the_assessor_including_the_bad_ones(monkeypatch) -> None:
    """The filtering that must not exist. A torn read is the most valuable thing a probe can
    observe — it is what distinguishes a demonstrated failure from a missing probe — so the driver
    must hand it over rather than retry until the readings agree."""
    seen: list[tuple[ProbeObservation, ...]] = []
    assessor = probe_module.assess_probe_observations

    def _capture(observations, **kwargs):
        seen.append(tuple(observations))
        return assessor(observations, **kwargs)

    monkeypatch.setattr(probe_module, "assess_probe_observations", _capture)

    result = _probe(_TornCluster())

    assert len(seen) == 1
    assert len(seen[0]) == 8, "four publishes x two readers, every one handed over"
    assert any(o.content_digest == "digest-half-written" for o in seen[0])
    assert result.passed is False


# ══ 2. vacuity — the guard is in the TYPE, and the driver cannot overrule it ══════════════════════


def test_a_probe_that_observed_nothing_cannot_pass() -> None:
    """A.26's vacuity guard, from the driver's side. Every reader failed to read, so the evidence is
    empty — and an empty evidence set is `passed=False`, never "nothing went wrong"."""
    cluster = _BlindCluster()

    result = _probe(cluster)

    assert result.observations == ()
    assert result.passed is False
    assert result.covers_schema_evolution is False
    assert cluster.looks == 8, "the driver still ASKED — it did not skip a probe it expected to fail"


def test_a_probe_that_watched_no_swap_cannot_pass() -> None:
    """The other vacuity: readers who only ever saw ONE generation watched nothing happen. The
    driver cannot produce it against a real target — it publishes four generations — so this is the
    assessor's law, restated here because a future driver that published once would hit it."""
    class _Frozen(_Cluster):
        def publish(self, *, generation_id: str, columns) -> None:
            super().publish(generation_id="only-one", columns=columns)

    result = _probe(_Frozen())

    assert {o.generation_id for o in result.observations} == {"only-one"}
    assert result.passed is False


# ══ 3. only VERSIONED_POINTER is attempted ═══════════════════════════════════════════════════════


def test_only_VERSIONED_POINTER_is_attempted() -> None:
    """A.26: an attestation says what the CLUSTER does; `RENDERABLE_MECHANISMS` says what the
    RENDERER can write down. Attesting a mechanism the renderer cannot emit buys cluster time for
    evidence no selection could ever be rendered from — `render_publish` refuses it with
    `PUBLISH_MECHANISM_UNSUPPORTED` at the other end."""
    assert RENDERABLE_MECHANISMS == frozenset({PublishMechanism.VERSIONED_POINTER})

    for refused in (PublishMechanism.EXCHANGE_PARTITION, PublishMechanism.SET_LOCATION):
        cluster = _Cluster()
        with pytest.raises(ValueError, match="VERSIONED_POINTER"):
            _probe(cluster, mechanism=refused)
        assert cluster.published == [], "a refused mechanism must not have touched the cluster"


def test_the_refusal_reads_the_ONE_definition_of_what_is_renderable(monkeypatch) -> None:
    """Not a second copy of the set. If the renderer ever learns another mechanism, this driver must
    follow without an edit — and if it kept its own literal, the two would disagree silently."""
    monkeypatch.setattr(probe_module, "RENDERABLE_MECHANISMS",
                        frozenset({PublishMechanism.SET_LOCATION}))

    cluster = _Cluster()
    _probe(cluster, mechanism=PublishMechanism.SET_LOCATION)

    assert cluster.published, "the driver did not follow RENDERABLE_MECHANISMS"


# ══ 4. the sequence: §10.3 steps 1-5, run twice, one evidence set ════════════════════════════════


def test_the_sequence_is_repeated_while_ADDING_a_feature_column() -> None:
    """§10.3 step 5. A partition-location swap does not atomically change table SCHEMA, so an
    attestation that never saw a wider schema has proved nothing about publishing a group whose
    shape changed. Two rounds, four publishes, the second pair one column wider — and ONE evidence
    set, because two probes would produce two attestations neither of which covers evolution."""
    cluster = _Cluster()

    result = _probe(cluster)

    assert [columns for _generation, columns in cluster.published] == [
        _BASE, _BASE, (*_BASE, _FEATURE), (*_BASE, _FEATURE)]
    assert len({generation for generation, _columns in cluster.published}) == 4
    assert result.passed is True
    assert result.covers_schema_evolution is True


def test_the_result_is_scoped_to_the_TARGET_environment_not_a_parameter() -> None:
    """A probe attests capability for an EXACT environment, and the environment is the target's own
    fact. A parameter would let a probe run against one cluster and be recorded against another."""
    assert "environment_id" not in inspect.signature(probe_publication_capability).parameters

    result = _probe(_Cluster())

    assert result.environment_id == ENV
    assert result.engine_versions == VERSIONS
    assert result.probe_id == "probe-live-1"
    assert result.completed_at == "2026-08-15T10:05:00+00:00"


def test_nothing_is_generated_here() -> None:
    """`publish.py`'s law, inherited: no clock and no id factory. Both are parameters, so a probe
    cannot record when it was TOLD rather than what ran, and two probes cannot attest under one id
    by accident."""
    params = inspect.signature(probe_publication_capability).parameters
    assert "probe_id" in params and params["probe_id"].default is inspect.Parameter.empty
    assert "clock" in params and params["clock"].default is inspect.Parameter.empty


# ══ 5. calls assembled wrongly are ValueErrors, never verdicts ═══════════════════════════════════


@pytest.mark.parametrize(("kwargs", "match"), [
    ({"readers": ()}, "no readers"),
    ({"columns": ()}, "no columns"),
    ({"feature_column": "cif_id"}, "already among the published columns"),
])
def test_a_call_that_could_not_prove_anything_is_refused_before_any_cluster_time(
        kwargs, match) -> None:
    """Each of these would run the whole sequence and then be assessed as a FAILURE — reporting a
    demonstrated absence where nothing was demonstrated, at the price of a live cluster run. §14 has
    no member for 'this call was assembled wrongly', so each is a ValueError."""
    cluster = _Cluster()

    with pytest.raises(ValueError, match=match):
        _probe(cluster, **kwargs)

    assert cluster.published == []


def test_the_target_is_a_runtime_checkable_seam_with_no_row_returning_method() -> None:
    """The control plane never reads feature data (§14). `observe` returns a marker, a column list,
    a count and a DIGEST; a seam with a row-returning method would make that a review convention."""
    assert isinstance(_Cluster(), PublicationTarget)
    assert {name for name in dir(PublicationTarget) if not name.startswith("_")} == {
        "environment_id", "publish", "observe"}
