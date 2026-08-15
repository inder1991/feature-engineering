"""§10.3 step 2 — the LIVE publication probe: the half that needs a cluster, and decides nothing.

:func:`~featuregen.materialize.publish.assess_probe_observations` is the deciding half and it needs
no cluster; this is the driver that gives it something to decide about. DEFERRED-WORK A.26 states
the constraint on this module in one sentence — *"the driver collects observations and calls
``assess_probe_observations``; it has no way to state a different verdict"* — and everything below
is an arrangement to make that structural rather than a rule somebody is asked to honour:

* **This module never constructs a ``ProbeResult``.** Not "does not today": a test reads this
  module's AST and asserts that the only ``ProbeResult`` in it is the one the assessor returns. A
  driver that could build one could set ``passed=True``, which is the Rev-3 back door §10.3 exists
  to have removed.
* **Every reading is handed over, including the ones that looked bad.** There is no filtering step
  and no retry-until-clean loop. A torn read is the single most valuable observation a probe can
  make — it is what tells ``PUBLISH_MECHANISM_UNSUPPORTED`` ("the design must change") from
  ``CAPABILITY_UNPROVEN`` ("nobody has run the probe") — and a driver that dropped it would turn a
  demonstrated failure into a missing one.
* **A probe that observed nothing cannot pass**, and the guard is not here. It is in
  ``_derive_passed``: no observations, or fewer than two distinct generation markers, is ``False``.
  This module could not overrule it if it tried, which is why the vacuity test in ``test_probe.py``
  drives a target whose readers see nothing and asserts the verdict rather than asserting a branch.

**WHAT THE SEQUENCE IS, and why it runs twice.** §10.3 steps 1–4 are one swap watched by readers:
publish generation A, poll every reader, publish generation B, poll every reader. Step 5 says the
whole sequence is REPEATED while adding a feature column, because a partition-location swap does not
atomically change table *schema* — an attestation that never saw a wider schema has proved nothing
about publishing a group whose shape changed. So :func:`probe_publication_capability` runs the
sequence twice, the second time over ``columns + (feature_column,)``, and hands all four rounds'
readings to the assessor as ONE evidence set. Splitting them into two probes would produce two
attestations neither of which covers schema evolution.

**NOTHING IS GENERATED HERE**, matching :mod:`featuregen.materialize.publish`'s own law: no clock
and no id factory. ``probe_id`` is supplied because the attestation is recorded under it — a probe
that minted its own id would record when it was *told*, not what ran — and ``clock`` is supplied
because every instant on the evidence is the caller's one.

**THIS MODULE TOUCHES NO CLUSTER ITSELF.** :class:`PublicationTarget` is the seam, and it is a
Protocol rather than an implementation for the reason ``src/`` has no ``pyspark`` import anywhere:
the control plane does not acquire the artifact's engines. The operator's driver implements it
against the real environment, and running the probe against a live cluster is an OPERATOR ACTION
requiring an explicit go — it spends cluster time and produces a durable governance record.
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Protocol, runtime_checkable

from featuregen.materialize.inventory import EngineVersions
from featuregen.materialize.publish import (
    ProbeObservation,
    ProbeResult,
    PublishMechanism,
    assess_probe_observations,
)
from featuregen.materialize.render.publish import RENDERABLE_MECHANISMS

__all__ = [
    "PublicationTarget",
    "probe_publication_capability",
]


@runtime_checkable
class PublicationTarget(Protocol):
    """The cluster, as this probe needs it: publish a generation, and let a reader look.

    Two methods, and the split between them is §10.3's own. ``publish`` is the act under test;
    ``observe`` is a reader watching while it happens. A seam with one method — "swap and tell me if
    it worked" — would put the verdict inside the thing being probed, which is exactly the shape
    §10.3 forbids.

    :meth:`observe` returns a :class:`~featuregen.materialize.publish.ProbeObservation` because only
    the cluster can know its five values: §10.2's ``__generation_id`` marker read *out of the data*,
    the column list in the order the reader saw it, the row count and a content digest. It may
    return ``None`` for a reader that could not read at all — a session that failed mid-swap is a
    reader who saw nothing, which is different from one who saw a torn state, and inventing an
    observation for it would be inventing evidence.
    """

    @property
    def environment_id(self) -> str:
        """The environment this target publishes into. The attestation is scoped to it exactly."""
        ...

    def publish(self, *, generation_id: str, columns: Sequence[str]) -> None:
        """Make ``generation_id`` the reader-visible state, with exactly these columns."""
        ...

    def observe(self, *, reader_id: str) -> ProbeObservation | None:
        """ONE reader's ONE look at the publication target, or ``None`` if it could not read."""
        ...


def probe_publication_capability(
    cluster: PublicationTarget,
    *,
    mechanism: PublishMechanism,
    engine_versions: EngineVersions,
    probe_id: str,
    readers: Sequence[str],
    columns: Sequence[str],
    feature_column: str,
    clock: Callable[[], str],
) -> ProbeResult:
    """Run §10.3's sequence against ``cluster`` and return the verdict its readings support.

    Args:
        cluster: the environment under test (:class:`PublicationTarget`).
        mechanism: which mechanism to point the probe at. Only ``VERSIONED_POINTER`` is attempted —
            see the refusal below.
        engine_versions: what the environment is running WHILE the probe runs. Part of the claim,
            not context around it: a mechanism proven on Spark 3.4 is not proven on 3.5.
        probe_id: the id the resulting attestation is recorded under. Supplied, never minted.
        readers: who polls. Each one is asked after every publish, so the observation count is
            ``4 × len(readers)`` minus whichever readers could not read.
        columns: the published table's columns for the first sequence.
        feature_column: the column ADDED for the second sequence — §10.3 step 5's whole point.
        clock: an offset-aware ISO 8601 instant. The probe mints none.

    Returns:
        Whatever :func:`~featuregen.materialize.publish.assess_probe_observations` returns for the
        readings collected. This function has no other exit and no way to adjust the verdict.

    Raises:
        ValueError: ``mechanism`` is not in ``RENDERABLE_MECHANISMS``, ``readers`` or ``columns`` is
            empty, or ``feature_column`` is already among ``columns``. Every one is a call assembled
            wrongly rather than a verdict about an environment, which is why none is a §14 code.
    """
    if mechanism not in RENDERABLE_MECHANISMS:
        raise ValueError(
            f"the probe was pointed at {mechanism.value}, and this platform can render a catalog "
            f"entry only for {sorted(m.value for m in RENDERABLE_MECHANISMS)}: an attestation for a "
            f"mechanism the renderer cannot emit would let select_publisher return a selection that "
            f"render_publish then refuses with PUBLISH_MECHANISM_UNSUPPORTED — evidence with no "
            f"consumer, bought with cluster time. Extending the set is a renderer change "
            f"(RENDERABLE_MECHANISMS and publish_entry_body, DEFERRED-WORK A.26), not a probe one")
    if not readers:
        raise ValueError(
            "the probe was given no readers: '§10.3's readers polling continuously' IS the proof, "
            "and a run with nobody watching would collect no observations and then be assessed as "
            "a failure — reporting a demonstrated absence where nothing was demonstrated")
    if not columns:
        raise ValueError(
            "the probe was given no columns: a reader that records no schema cannot witness the "
            "schema change §10.3 step 5 requires, and ProbeObservation refuses an empty column list")
    if feature_column in tuple(columns):
        raise ValueError(
            f"the added feature column {feature_column!r} is already among the published columns: "
            f"the second sequence would then publish the SAME schema as the first, and "
            f"covers_schema_evolution — which requires one generation's column set to be a strict "
            f"superset of another's — would be False with nothing in the evidence saying why")

    observations: list[ProbeObservation] = []
    for round_index, schema in enumerate((tuple(columns), (*columns, feature_column))):
        for half in ("a", "b"):
            cluster.publish(generation_id=f"{probe_id}-{round_index}{half}", columns=schema)
            observations.extend(_look(cluster, readers))
    return assess_probe_observations(
        observations, probe_id=probe_id, environment_id=cluster.environment_id,
        mechanism=mechanism, engine_versions=engine_versions, completed_at=clock())


def _look(cluster: PublicationTarget, readers: Sequence[str]) -> list[ProbeObservation]:
    """Every reader's look after one publish — kept whole, in the order they were asked.

    There is deliberately no filtering, no de-duplication and no "retry until they agree". Two
    readers disagreeing about one generation marker is a TORN READ, which is the observation this
    whole exercise exists to be able to make; a driver that smoothed it away would report a pass on
    an environment it had just watched fail.

    A reader returning ``None`` contributes nothing rather than a fabricated reading. That lowers
    the observation count and can therefore only make the verdict WEAKER — which is the right
    direction for an absence, and is why it needs no branch of its own downstream.
    """
    return [observation for reader_id in readers
            if (observation := cluster.observe(reader_id=reader_id)) is not None]
