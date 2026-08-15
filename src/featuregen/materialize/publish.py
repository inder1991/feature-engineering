"""Spec §10.3 — publication capability, which exists ONLY as ingested probe evidence.

**The property this module is built to make unrepresentable.** Rev 3 of the spec let
``record_attestation`` store ``passed=True`` directly, so the live test proved that somebody had
stored a boolean — not that publication is atomic. Every shape here is chosen so that the same
mistake cannot be re-made by a *caller*, rather than being forbidden by a rule a caller is asked to
remember:

* :func:`record_attestation` takes ``(conn, probe_result)`` and nothing else. There is no ``passed``
  parameter, no ``mechanism`` parameter and no ``environment_id`` parameter, so an attestation
  cannot be *asserted* — only ingested.
* :class:`ProbeResult`'s three verdict fields (``passed``, ``covers_schema_evolution``,
  ``evidence_hash``) are **re-derived from ``observations`` at construction** and any disagreement
  is refused. They are stored rather than computed on read because the row round-trips through the
  control plane, and re-deriving on the way back in is what makes the stored row checkable.
* :func:`select_publisher` has no ``adds_feature`` parameter. Rev 3 let the caller supply it, so
  passing ``False`` bypassed the schema-evolution requirement entirely; it is DERIVED here by
  comparing the currently published schema against the group plan's expected schema.
* :func:`~featuregen.materialize.render.publish.render_publish` takes a
  :class:`PublisherSelection`, never a :class:`PublishMechanism`. A mechanism is a name anybody can
  spell; a selection is the evidence that this mechanism was probed, passed, and was probed on the
  versions the target environment is running now.

**What "derived from the observations" actually decides.** A probe publishes generation A, polls
readers continuously, publishes generation B, and records what each reader saw (§10.3 steps 1–4).
From those observations alone:

* the run **passed** when there is at least one observation, at least **two distinct generation
  markers** among them, and every observation carrying a given marker agrees with every other
  observation carrying that marker about schema, row count and content digest. Two markers matter
  because readers that only ever saw A watched no swap at all — that is the vacuous pass §10.3's
  own test warns about. Agreement-within-a-marker is what a *torn* read fails: §10.3 step 4 says
  schema and row count alone can coincide, so the marker is the discriminator and the content
  check is what proves the marker was not attached to a half-written state.
* it **covers schema evolution** when it passed AND one generation's observed column set is a
  strict superset of another's — i.e. the sequence really was repeated while ADDING a feature
  column (§10.3 step 5). A partition-location swap does not atomically change table *schema*, so an
  attestation that never saw a wider schema has not proved anything about adding a feature.

**A FAILED probe is evidence too, and it is the whole difference between the two refusal codes.**
``CAPABILITY_UNPROVEN`` means nobody has run the probe for this environment at these versions — the
answer is "go run it". ``PUBLISH_MECHANISM_UNSUPPORTED`` means the probe DID run and demonstrated
this mechanism does not give atomic visibility here — the answer is "the design must change". So
``record_attestation`` records a failing probe as readily as a passing one; refusing to store the
failure would erase the only thing that can tell the two apart.

**Engine versions are part of the claim, not context around it.** A mechanism proven on Spark 3.4
is not proven on 3.5, and the attestation table is keyed on ``hive``/``spark``/``metastore`` for
that reason. :func:`select_publisher` compares the three against the environment's CURRENT versions
and treats drift as *unproven* rather than as *failed*: nobody demonstrated a failure on the new
versions either, and reporting one would be inventing an observation.

**Nothing is generated here.** No clock, no id factory — matching
:mod:`featuregen.materialize.control_plane` and :mod:`featuregen.materialize.binding`. The
attestation is recorded under the probe's own id, so one probe yields at most one attestation and a
second ingestion of the same probe is a ``UniqueViolation`` rather than two attestations claiming
one piece of evidence.

**No feature data, ever.** A :class:`ProbeObservation` carries a generation marker, a column-name
list, a row count and a content *digest*. The digest is what §10.3's "content check" is allowed to
be in a control plane: a value would make the observation a sample of the published table.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from featuregen.contracts.db import DbConn
from featuregen.materialize.canonical import materialize_hash
from featuregen.materialize.codes import MaterializationRefused, PublicationRefusalCode
from featuregen.materialize.group_plan import FeatureGroupPlanV1, expected_schema
from featuregen.materialize.inventory import EngineVersions

__all__ = [
    "ActiveRevision",
    "ProbeObservation",
    "ProbeResult",
    "PublicationCapabilityAttestation",
    "PublicationSwap",
    "PublishMechanism",
    "PublisherSelection",
    "adds_feature_for",
    "assess_probe_observations",
    "next_revision_seq",
    "publish_generation",
    "read_active_revision",
    "read_attestations",
    "record_active_revision",
    "record_attestation",
    "select_publisher",
]


class PublishMechanism(StrEnum):
    """The publish mechanisms §10.3 names as CANDIDATES — closed, and ``INSERT OVERWRITE`` is not one.

    §10 rejects ``INSERT OVERWRITE`` outright, so there is deliberately no member for it: a closed
    vocabulary whose members are the things a probe may be pointed at must not contain the one
    thing no probe result could ever make selectable.

    Membership here is not a claim that a mechanism works. It is the opposite — every member is a
    hypothesis the probe exists to settle, and two of them carry known problems §10.3 states:
    ``EXCHANGE_PARTITION`` cannot exchange into a destination where the partition already exists and
    requires matching schemas, and ``SET_LOCATION`` is documented DDL that does not by itself prove
    atomic visibility across Spark, Hive, cached sessions and other readers.
    """

    #: §10.3's preferred first-slice mechanism: immutable versioned physical outputs with ONE
    #: reader-visible pointer/view switch. Still requiring demonstration.
    VERSIONED_POINTER = "VERSIONED_POINTER"
    #: Hive ``ALTER TABLE … EXCHANGE PARTITION``.
    EXCHANGE_PARTITION = "EXCHANGE_PARTITION"
    #: Hive ``ALTER TABLE … SET LOCATION``.
    SET_LOCATION = "SET_LOCATION"


def _text(value: object, *, field: str, why: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} is blank ({value!r}): {why}")
    return value


@dataclass(frozen=True, slots=True)
class ProbeObservation:
    """ONE reader's ONE look at the publication target while the swap was in flight.

    ``generation_id`` is §10.2's ``__generation_id`` system column read out of the data itself — the
    marker moves in the same atomic state as the rows, which is the entire reason §10.2 puts it
    there rather than in side metadata.

    ``content_digest`` is §10.3's "content check". It is a DIGEST rather than any sampled value
    because this record reaches the control plane, which never reads feature data (§14); a digest
    still discriminates two complete states that happen to share a schema and a row count, which is
    exactly the coincidence §10.3 step 4 warns the marker alone cannot rule out.

    ``column_names`` is carried in the ORDER the reader saw, because adding a feature is observed as
    a change in this list and column order is part of the published schema (§9).
    """

    reader_id: str
    observed_at: str
    generation_id: str
    column_names: tuple[str, ...]
    row_count: int
    content_digest: str

    def __post_init__(self) -> None:
        _text(self.reader_id, field="reader_id",
              why="an observation nobody can attribute to a reader cannot be counted as a reader "
                  "having looked, and 'readers polling continuously' is the whole proof")
        _text(self.observed_at, field="observed_at",
              why="an observation without an instant cannot be placed inside the swap window")
        _text(self.generation_id, field="generation_id",
              why="§10.2's marker is what discriminates a complete A state from a complete B "
                  "state, and a blank one discriminates nothing")
        _text(self.content_digest, field="content_digest",
              why="§10.3 step 4 requires a content check alongside the marker, because schema and "
                  "row count alone can coincide between two different complete states")
        if not isinstance(self.column_names, tuple) or not self.column_names:
            raise ValueError(
                f"observation {self.reader_id!r} saw no columns ({self.column_names!r}): a reader "
                f"that recorded no schema cannot witness the schema change §10.3 step 5 requires")
        for name in self.column_names:
            _text(name, field="column_names",
                  why="a blank column name is not a column the reader could have seen")
        if not isinstance(self.row_count, int) or isinstance(self.row_count, bool) \
                or self.row_count < 0:
            raise ValueError(
                f"observation {self.reader_id!r} has row_count {self.row_count!r}: a count is a "
                f"non-negative integer, and anything else is not a reading of a table")

    #: What the observation says the table WAS, independent of who looked and when. Two observations
    #: of one complete state must agree on every element of this; that is the torn-read check.
    def state_payload(self) -> dict[str, Any]:
        return {"generation_id": self.generation_id, "column_names": list(self.column_names),
                "row_count": self.row_count, "content_digest": self.content_digest}

    def identity_payload(self) -> dict[str, Any]:
        """Everything, including who looked and when — the evidence hash covers all of it."""
        return {"reader_id": self.reader_id, "observed_at": self.observed_at,
                **self.state_payload()}


def _states_by_generation(
    observations: Sequence[ProbeObservation],
) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for observation in observations:
        grouped.setdefault(observation.generation_id, []).append(observation.state_payload())
    return grouped


def _derive_passed(observations: Sequence[ProbeObservation]) -> bool:
    """§10.3 step 4, decided from the evidence and nothing else. See the module docstring."""
    if not observations:
        return False
    grouped = _states_by_generation(observations)
    if len(grouped) < 2:
        return False
    return all(all(state == states[0] for state in states) for states in grouped.values())


def _derive_covers_schema_evolution(observations: Sequence[ProbeObservation]) -> bool:
    """§10.3 step 5: the sequence was repeated while ADDING a feature column, and it was SEEN."""
    if not _derive_passed(observations):
        return False
    schemas = {frozenset(observation.column_names) for observation in observations}
    return any(one > other for one in schemas for other in schemas)


def _evidence_payload(
    observations: Sequence[ProbeObservation],
    *,
    environment_id: str,
    mechanism: PublishMechanism,
    engine_versions: EngineVersions,
    passed: bool,
    covers_schema_evolution: bool,
) -> dict[str, Any]:
    """What ``evidence_hash`` is taken over — the observations AND the claim made about them.

    Hashing the observations alone would let a real failing probe's observations be re-labelled
    ``passed=True`` under an evidence hash that still verified. The hash is an integrity check, not
    a signature: it cannot prove who produced the evidence, and the structural guarantee is
    :class:`ProbeResult`'s re-derivation, not this digest.
    """
    return {
        "environment_id": environment_id,
        "mechanism": mechanism.value,
        "engine_versions": engine_versions.identity_payload(),
        "observations": [observation.identity_payload() for observation in observations],
        "passed": passed,
        "covers_schema_evolution": covers_schema_evolution,
    }


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """What the executable probe actually observed, and the verdict THOSE observations support.

    ``passed``, ``covers_schema_evolution`` and ``evidence_hash`` are all re-derived in
    ``__post_init__`` and a disagreement is refused, so there is no way to construct a
    ``ProbeResult`` claiming a pass its observations do not carry — which is what makes
    :func:`record_attestation`'s single-parameter signature meaningful rather than decorative.

    ``probe_id`` is SUPPLIED, like every id in :mod:`featuregen.materialize.control_plane`: a probe
    that minted its own would record when it was *told*, not what ran. The attestation is recorded
    under it, so one probe yields at most one attestation.
    """

    probe_id: str
    environment_id: str
    mechanism: PublishMechanism
    engine_versions: EngineVersions
    observations: tuple[ProbeObservation, ...]
    passed: bool
    covers_schema_evolution: bool
    evidence_hash: str
    completed_at: str

    def __post_init__(self) -> None:
        _text(self.probe_id, field="probe_id",
              why="the attestation is recorded under it, and a blank one would let two probes "
                  "attest under one identity")
        _text(self.environment_id, field="environment_id",
              why="§10.3 attests capability for an EXACT environment; a probe that names none "
                  "proved something about nowhere")
        _text(self.completed_at, field="completed_at",
              why="the attestation records when the capability was demonstrated")
        if not isinstance(self.mechanism, PublishMechanism):
            raise TypeError(
                f"a probe result needs a PublishMechanism, got {type(self.mechanism).__name__}: a "
                f"raw string would let an attestation be recorded for a mechanism no probe could "
                f"be pointed at — including the INSERT OVERWRITE §10 rejects outright")
        if not isinstance(self.engine_versions, EngineVersions):
            raise TypeError(
                f"a probe result needs an EngineVersions, got "
                f"{type(self.engine_versions).__name__}: a mechanism proven on Spark 3.4 is not "
                f"proven on 3.5, so the versions are part of the claim rather than context")
        if not isinstance(self.observations, tuple):
            raise TypeError(
                f"observations must be a tuple, got {type(self.observations).__name__}: the record "
                f"is frozen and a mutable sequence would let the evidence change under the hash "
                f"that was taken over it")
        for observation in self.observations:
            if not isinstance(observation, ProbeObservation):
                raise TypeError(
                    f"observations must be ProbeObservation values, got "
                    f"{type(observation).__name__}: an untyped reading carries no marker and no "
                    f"content check, so it could not be judged complete or torn")

        expected_passed = _derive_passed(self.observations)
        if self.passed != expected_passed:
            raise ValueError(
                f"probe {self.probe_id!r} claims passed={self.passed} while its "
                f"{len(self.observations)} observation(s) support passed={expected_passed}: §10.3 "
                f"exists because a stored boolean proves nothing, so the verdict is derived from "
                f"the evidence and a claim that disagrees with it is refused rather than recorded")
        expected_covers = _derive_covers_schema_evolution(self.observations)
        if self.covers_schema_evolution != expected_covers:
            raise ValueError(
                f"probe {self.probe_id!r} claims covers_schema_evolution="
                f"{self.covers_schema_evolution} while its observations support {expected_covers}: "
                f"a partition-location swap does not atomically change table SCHEMA, so coverage "
                f"means a reader actually observed the wider schema (§10.3 step 5)")
        expected_hash = materialize_hash(_evidence_payload(
            self.observations, environment_id=self.environment_id, mechanism=self.mechanism,
            engine_versions=self.engine_versions, passed=self.passed,
            covers_schema_evolution=self.covers_schema_evolution))
        if self.evidence_hash != expected_hash:
            raise ValueError(
                f"probe {self.probe_id!r} carries evidence_hash {self.evidence_hash!r} but its "
                f"evidence hashes to {expected_hash!r}: the attestation stores this value as the "
                f"pointer back to what was observed, and one that does not match points at "
                f"evidence nobody has")


def assess_probe_observations(
    observations: Sequence[ProbeObservation],
    *,
    probe_id: str,
    environment_id: str,
    mechanism: PublishMechanism,
    engine_versions: EngineVersions,
    completed_at: str,
) -> ProbeResult:
    """Turn what the readers SAW into the verdict those readings support (§10.3 steps 4–6).

    This is the half of the probe that needs no cluster, and it is deliberately the half that
    decides. ``probe_publication_capability(cluster, *, mechanism, engine_versions)`` — the live
    driver — publishes A, polls, publishes B, repeats while adding a feature column, and then calls
    this function with the observations it collected. It cannot reach a different verdict, because
    it has no way to state one: every field this returns is derived here.

    Args:
        observations: every reading, from every reader, across the whole probe — including the
            repetition that adds a feature column. Passing only the readings that looked good would
            be the vacuous pass §10.3 names.
        probe_id: the id the resulting attestation is recorded under.
        environment_id: the environment the probe ran against.
        mechanism: the mechanism it was pointed at.
        engine_versions: what that environment was running while it ran.
        completed_at: when the probe finished.

    Returns:
        A :class:`ProbeResult` whose verdict is the observations', not the caller's.
    """
    ordered = tuple(observations)
    passed = _derive_passed(ordered)
    covers = _derive_covers_schema_evolution(ordered)
    return ProbeResult(
        probe_id=probe_id,
        environment_id=environment_id,
        mechanism=mechanism,
        engine_versions=engine_versions,
        observations=ordered,
        passed=passed,
        covers_schema_evolution=covers,
        evidence_hash=materialize_hash(_evidence_payload(
            ordered, environment_id=environment_id, mechanism=mechanism,
            engine_versions=engine_versions, passed=passed, covers_schema_evolution=covers)),
        completed_at=completed_at)


@dataclass(frozen=True, slots=True)
class PublicationCapabilityAttestation:
    """§10.3's record: this environment, at these versions, was PROBED for this mechanism.

    The three versions are flattened out of :class:`EngineVersions` rather than carried whole,
    because they are what the attestation is KEYED on (migration ``1034``) and a nested object
    cannot be indexed. The other five engine versions describe the generated project's pins (§7),
    not the cluster's publish capability, so they are not part of the key.

    ``recorded_at`` is the DATABASE's stamp (``DEFAULT now()``), not the probe's: ``attested_at``
    says when the probe finished, ``recorded_at`` says when this plane learned of it, and the
    newest-evidence ordering in :func:`select_publisher` runs on the latter — evidence supersedes
    in the order it was recorded, not the order probes claim to have completed. It carries the
    value read back from the row (``record_attestation`` RETURNINGs it), so an attestation read
    from the table compares equal to the one recorded.
    """

    attestation_id: str
    environment_id: str
    hive_version: str
    spark_version: str
    metastore_version: str
    mechanism: PublishMechanism
    passed: bool
    covers_schema_evolution: bool
    evidence_hash: str
    attested_at: str
    recorded_at: datetime | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.mechanism, PublishMechanism):
            object.__setattr__(self, "mechanism", PublishMechanism(self.mechanism))

    def matches(self, engine_versions: EngineVersions) -> bool:
        """Whether what was PROBED is what the environment is running NOW.

        Exact string equality on all three, deliberately: there is no version ordering here and
        there must not be one. "3.5 is newer than 3.4, so it is fine" is a compatibility judgement
        nobody made, and the whole point of §10.3 is that capability is demonstrated rather than
        reasoned about.
        """
        return (self.hive_version == engine_versions.hive
                and self.spark_version == engine_versions.spark
                and self.metastore_version == engine_versions.metastore)


def record_attestation(
    conn: DbConn,
    probe_result: ProbeResult,
) -> PublicationCapabilityAttestation:
    """Ingest a probe result as §10.3's attestation. **This is the only way one comes to exist.**

    There is no ``passed`` parameter, no ``mechanism`` parameter and no ``environment_id``
    parameter — every one of them comes off ``probe_result``, whose own construction re-derived the
    verdict from the observations. That is what makes "an attestation with no probe evidence cannot
    exist" a property of the API rather than a rule a caller is asked to honour.

    A FAILING probe is recorded exactly as readily as a passing one. It is the only evidence that
    can distinguish ``PUBLISH_MECHANISM_UNSUPPORTED`` (the probe ran, this mechanism does not give
    atomic visibility here) from ``CAPABILITY_UNPROVEN`` (nobody has run it), and discarding it
    would collapse the two into "go run the probe" forever.

    Args:
        conn: the caller's open transaction.
        probe_result: what the probe observed, and the verdict those observations support.

    Returns:
        The attestation as recorded, under ``probe_result.probe_id``.

    D0's guard is GONE, with its cause. Between D0 and G-3 this function refused a PASSING result,
    because recording one made ``select_publisher`` return a selection that ``compile/chain.py`` had
    no publish step to honour — every run of that environment turned from its truthful refusal into
    a platform error. G-3 built the step, so the refusal has nothing left to protect and keeping it
    would be a guard against the platform working.

    Raises:
        TypeError: ``probe_result`` is not a :class:`ProbeResult` — a duck-typed stand-in would be
            a way to supply the verdict directly, which is the whole thing this refuses.
        psycopg.errors.UniqueViolation: this probe has already been ingested. One probe is one
            piece of evidence and cannot support two attestations.
    """
    if not isinstance(probe_result, ProbeResult):
        raise TypeError(
            f"record_attestation ingests a ProbeResult, got {type(probe_result).__name__}: §10.3 "
            f"says an attestation may be created ONLY by ingesting a probe result, and accepting "
            f"anything shaped like one would restore the `passed=True` back door it removed")
    attestation = PublicationCapabilityAttestation(
        attestation_id=probe_result.probe_id,
        environment_id=probe_result.environment_id,
        hive_version=probe_result.engine_versions.hive,
        spark_version=probe_result.engine_versions.spark,
        metastore_version=probe_result.engine_versions.metastore,
        mechanism=probe_result.mechanism,
        passed=probe_result.passed,
        covers_schema_evolution=probe_result.covers_schema_evolution,
        evidence_hash=probe_result.evidence_hash,
        attested_at=probe_result.completed_at)
    # RETURNING the database's own `recorded_at` stamp (DEFAULT now()), so the value returned here
    # is the one `read_attestations` will read back — never a second clock's opinion.
    inserted = conn.execute(
        "INSERT INTO publication_capability_attestation (attestation_id, environment_id, "
        "hive_version, spark_version, metastore_version, mechanism, passed, "
        "covers_schema_evolution, evidence_hash, attested_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING recorded_at",
        (attestation.attestation_id, attestation.environment_id, attestation.hive_version,
         attestation.spark_version, attestation.metastore_version, attestation.mechanism.value,
         attestation.passed, attestation.covers_schema_evolution, attestation.evidence_hash,
         attestation.attested_at)).fetchone()
    return replace(attestation,
                   recorded_at=inserted[0] if inserted is not None else None)


def read_attestations(
    conn: DbConn,
    *,
    environment_id: str,
    mechanism: PublishMechanism,
) -> tuple[PublicationCapabilityAttestation, ...]:
    """Every attestation recorded for one environment and one mechanism, oldest row first.

    Scoped by environment in the QUERY rather than filtered afterwards, so a selection can never be
    reached by an attestation belonging to another cluster (§10.3: *the exact environment*).
    """
    rows = conn.execute(
        "SELECT attestation_id, environment_id, hive_version, spark_version, metastore_version, "
        "mechanism, passed, covers_schema_evolution, evidence_hash, attested_at, recorded_at "
        "FROM publication_capability_attestation WHERE environment_id = %s AND mechanism = %s "
        "ORDER BY recorded_at",
        (environment_id, mechanism.value)).fetchall()
    return tuple(PublicationCapabilityAttestation(*row) for row in rows)


@dataclass(frozen=True, slots=True)
class PublisherSelection:
    """The evidence that a mechanism was PROVEN for this environment at these versions.

    The renderer consumes this and never a bare :class:`PublishMechanism`, so rendered publication
    code cannot exist without the selection having succeeded — a mechanism is a name anybody can
    spell, and a selection names the attestation it rests on.

    ``adds_feature`` is carried because it is what :func:`select_publisher` DERIVED, and recording
    the derivation is how an operator can see which requirement the selection had to satisfy. It is
    an output of selection, never an input to it.
    """

    environment_id: str
    mechanism: PublishMechanism
    capability_attestation_id: str
    engine_versions: EngineVersions
    adds_feature: bool

    def __post_init__(self) -> None:
        _text(self.environment_id, field="environment_id",
              why="a selection is made FOR an environment, and a blank one selects for nowhere")
        _text(self.capability_attestation_id, field="capability_attestation_id",
              why="the selection's whole content is the attestation it rests on; a blank id rests "
                  "on nothing and would let rendered publication code exist without evidence")
        if not isinstance(self.mechanism, PublishMechanism):
            raise TypeError(
                f"a selection needs a PublishMechanism, got {type(self.mechanism).__name__}: a raw "
                f"string is exactly the un-evidenced mechanism §10.3 keeps out of the renderer")
        if not isinstance(self.engine_versions, EngineVersions):
            raise TypeError(
                f"a selection needs an EngineVersions, got {type(self.engine_versions).__name__}: "
                f"the selection asserts the attestation matched what the environment RUNS")


def adds_feature_for(
    group_plan: FeatureGroupPlanV1,
    published_schema: Sequence[str] | None,
) -> bool:
    """Whether publishing this plan introduces a column the published table does not have.

    DERIVED, and exported so the derivation has one definition rather than one per caller. §10.3:
    *"Rev 3 let the caller supply it, so passing ``False`` bypassed the schema-evolution requirement
    entirely."*

    ``published_schema is None`` means **no table is published yet**, and it is read as
    ``True`` — fail closed. Publishing where nothing exists introduces every column at once, and
    more importantly ``None`` is also what a caller who never looked would pass. The safe reading
    costs nothing real: §10.3 step 5 requires the probe to cover adding a feature anyway, so a
    genuine attestation already satisfies it.

    Comparison is over the FULL expected schema (§9's landing keys, ``business_dt``, the feature
    columns and §10.2's three system columns), not the feature columns alone: any expected column
    the published table lacks is a schema change a partition swap does not make atomically. Names
    are case-folded, because unquoted Hive identifiers fold and a case difference is not a new
    column.
    """
    if published_schema is None:
        return True
    published = {name.strip().lower() for name in published_schema}
    return any(column.name.strip().lower() not in published
               for column in expected_schema(group_plan))


def select_publisher(
    conn: DbConn,
    *,
    environment_id: str,
    engine_versions: EngineVersions,
    mechanism: PublishMechanism,
    group_plan: FeatureGroupPlanV1,
    published_schema: Sequence[str] | None,
) -> PublisherSelection | MaterializationRefused:
    """Select a publisher for ``mechanism`` in ``environment_id``, or refuse with a typed code.

    Note what is NOT a parameter: ``adds_feature``. It is derived from ``published_schema`` against
    ``group_plan`` (:func:`adds_feature_for`), because a caller who could pass it could lie about
    it and skip the schema-evolution requirement entirely.

    ``published_schema`` has no default. A caller must state what is published — ``None`` for "no
    table yet" — rather than silently inherit the lenient answer by omission.

    The refusal split (§10.3, and the enum's own docstring):

    * **no attestation at all** for this environment and mechanism ⇒ ``CAPABILITY_UNPROVEN``;
      "go run the probe".
    * **attestations exist but all were probed on other versions** ⇒ ``CAPABILITY_UNPROVEN``.
      Drift is *unproven*, not *failed*: a mechanism proven on Spark 3.4 is not proven on 3.5, and
      no probe demonstrated a failure on 3.5 either.
    * **a matching attestation exists and it FAILED** ⇒ ``PUBLISH_MECHANISM_UNSUPPORTED``. The probe
      ran and demonstrated this mechanism does not satisfy atomic visibility here; the design must
      change rather than the claim. The NEWEST matching attestation carries the verdict: an older
      pass does not outlive a later probe that demonstrated failure on identical versions (the
      mirror of a later pass overriding an earlier failure). A TIE on ``recorded_at`` between a
      pass and a failure — two probe results ingested in one transaction — refuses too: a pass
      supersedes a failure only when it is STRICTLY newer. Scoped to the mechanism asked about —
      this function is asked about one, and reporting on mechanisms nobody enquired after would be
      a verdict about capability nobody probed for.
    * **the publication adds a column and the passing attestation does not cover schema
      evolution** ⇒ ``CAPABILITY_UNPROVEN``. A partition-location swap does not atomically change
      table schema, so what was proven is not what is about to happen.

    Returns:
        A :class:`PublisherSelection`, or a :class:`MaterializationRefused` carrying a
        :class:`~featuregen.materialize.codes.PublicationRefusalCode`. It is RETURNED rather than
        raised, exactly as :func:`~featuregen.materialize.binding.bind_group` returns its
        ``GROUP_BINDING_CONFLICT``: a publication verdict is an answer, not an exception.
    """
    if not isinstance(mechanism, PublishMechanism):
        raise TypeError(
            f"select_publisher needs a PublishMechanism, got {type(mechanism).__name__}: the "
            f"attestation lookup is keyed on it, and a raw string would look up a mechanism no "
            f"probe can be pointed at")
    if not isinstance(engine_versions, EngineVersions):
        raise TypeError(
            f"select_publisher needs an EngineVersions, got {type(engine_versions).__name__}: the "
            f"selection's central claim is that the attestation was probed on what the environment "
            f"runs now, and a loose mapping would compare whatever a caller happened to spell")
    if not isinstance(group_plan, FeatureGroupPlanV1):
        raise TypeError(
            f"select_publisher needs a FeatureGroupPlanV1, got {type(group_plan).__name__}: "
            f"`adds_feature` is derived from the plan's expected schema, and without one there is "
            f"nothing to derive it from")
    _text(environment_id, field="environment_id",
          why="§10.3 attests capability for an EXACT environment, and a blank one names none")

    adds_feature = adds_feature_for(group_plan, published_schema)
    attestations = read_attestations(conn, environment_id=environment_id, mechanism=mechanism)
    if not attestations:
        return MaterializationRefused(
            PublicationRefusalCode.CAPABILITY_UNPROVEN,
            f"no capability attestation exists for environment {environment_id!r} and mechanism "
            f"{mechanism.value}: §10.3 forbids publishing on an assumed capability, so the "
            f"executable probe must be run against {environment_id!r} before anything may publish "
            f"there")

    matching = [attestation for attestation in attestations if attestation.matches(engine_versions)]
    if not matching:
        probed = sorted({f"hive {attestation.hive_version} / spark {attestation.spark_version} / "
                         f"metastore {attestation.metastore_version}"
                         for attestation in attestations})
        return MaterializationRefused(
            PublicationRefusalCode.CAPABILITY_UNPROVEN,
            f"environment {environment_id!r} runs hive {engine_versions.hive} / spark "
            f"{engine_versions.spark} / metastore {engine_versions.metastore}, and the "
            f"{len(attestations)} attestation(s) recorded for mechanism {mechanism.value} were "
            f"probed on {probed}: a mechanism proven on one engine version is not proven on "
            f"another, so the probe must be re-run on what the environment runs now")

    passing = [attestation for attestation in matching if attestation.passed]
    if not passing:
        return MaterializationRefused(
            PublicationRefusalCode.PUBLISH_MECHANISM_UNSUPPORTED,
            f"the probe ran against environment {environment_id!r} on hive "
            f"{engine_versions.hive} / spark {engine_versions.spark} / metastore "
            f"{engine_versions.metastore} and mechanism {mechanism.value} did not give atomic "
            f"visibility there ({len(matching)} failing attestation(s)): this is a demonstrated "
            f"absence rather than a missing probe, so re-running it will not change the answer")

    # The newest-evidence guard, with its TIE failing closed. `read_attestations` returns rows
    # ordered by `recorded_at` and `matching` preserves that order, so `matching[-1]` carries the
    # newest `recorded_at` on exactly these versions — but "newest" is only a verdict when the
    # ordering can actually support it. A FAILED attestation sharing that maximum (two probes
    # ingested in one transaction share `now()`, so neither is strictly older) means no pass is
    # strictly newer than the failure, and reading the pass as superseding it would be a
    # tiebreak nobody demonstrated. Ambiguous evidence refuses, exactly as a strictly-newest
    # failure does (the same recorded_at-tie discipline `read_validation_reports` states).
    newest_recorded_at = matching[-1].recorded_at
    undefeated_failures = [attestation for attestation in matching
                           if not attestation.passed
                           and attestation.recorded_at == newest_recorded_at]
    if undefeated_failures:
        return MaterializationRefused(
            PublicationRefusalCode.PUBLISH_MECHANISM_UNSUPPORTED,
            f"the most recent evidence on these engine versions includes a FAILED probe "
            f"({', '.join(a.attestation_id for a in undefeated_failures)}) that no passing "
            f"attestation strictly supersedes: passing evidence that is not strictly newer than "
            f"a demonstrated failure is stale or ambiguous either way, re-run the probe")

    if adds_feature:
        covering = [attestation for attestation in passing if attestation.covers_schema_evolution]
        if not covering:
            return MaterializationRefused(
                PublicationRefusalCode.CAPABILITY_UNPROVEN,
                f"publishing {group_plan.logical_group_name!r} into environment "
                f"{environment_id!r} adds at least one column the published table does not have, "
                f"and none of the {len(passing)} passing attestation(s) for mechanism "
                f"{mechanism.value} covers schema evolution: a partition-location swap does not "
                f"atomically change table SCHEMA, so the probe must be repeated while ADDING a "
                f"feature column before a group whose shape changed may publish")
        chosen = covering[-1]
    else:
        chosen = passing[-1]

    return PublisherSelection(
        environment_id=environment_id,
        mechanism=mechanism,
        capability_attestation_id=chosen.attestation_id,
        engine_versions=engine_versions,
        adds_feature=adds_feature)


# ── G-3: the publish step ────────────────────────────────────────────────────────────────────────


@runtime_checkable
class PublicationSwap(Protocol):
    """The metastore write and the reader-visible pointer switch — the act §10.3's probe PROVES.

    One method, deliberately. §10.3's probe demonstrates that *one* operation makes a whole new
    generation visible atomically; a seam with a separate "write the metadata" and "flip the
    pointer" would be two operations, and the attestation would be evidence about neither.

    It is a Protocol and not an implementation for the reason ``src/`` imports no ``pyspark``
    anywhere: the control plane does not acquire the artifact's engines. The deployment supplies it
    (``chain.RunExecution.swap``), and ``None`` there means this deployment cannot publish — which
    the chain records as the run's outcome, never as a skipped step.

    ``columns`` is the plan's ``expected_schema`` in the plan's own order, because §10.3 step 5's
    schema-evolution question is about exactly this list. ``staging_root`` is where §9's gates left
    the assembled output; the swap makes THAT the published state and computes nothing.
    """

    def swap(self, *, published_object: str, generation_id: str, columns: Sequence[str],
             staging_root: str) -> None:
        """Make ``generation_id``'s staged output the reader-visible state of ``published_object``."""
        ...


@dataclass(frozen=True, slots=True)
class ActiveRevision:
    """Migration 1055's row: which generation a reader of one group is CURRENTLY seeing.

    Append-only and per-GROUP ordered, because publication is atomic per group (§10.1) and a
    feature-scoped pointer would let two columns of one table be visible at two generations. There
    is no ``is_current`` flag: the newest ``seq`` for a group is what holds now, and a mutable
    current-pointer column is a record that can be rewritten.

    ``published_object`` is COPIED from the binding at swap time rather than joined, because this
    row is a claim about what was made visible and a later rebinding must not restate history.
    ``capability_attestation_id`` is on the row so an auditor can answer "on what evidence was this
    swap performed?" from the pointer itself — a reconstruction from "whichever attestation was
    newest" would move as later probes are ingested.

    ``recorded_at`` is the DATABASE's stamp; ``activated_at`` is the caller's instant, matching
    ``materialization_run_event.occurred_at``. The plane mints no timestamps.
    """

    revision_id: str
    logical_group_name: str
    generation_id: str
    run_id: str
    published_object: str
    capability_attestation_id: str
    mechanism: PublishMechanism
    seq: int
    activated_at: str
    recorded_at: datetime | None = None

    def __post_init__(self) -> None:
        for field, value in (("revision_id", self.revision_id),
                             ("logical_group_name", self.logical_group_name),
                             ("generation_id", self.generation_id),
                             ("run_id", self.run_id),
                             ("published_object", self.published_object),
                             ("capability_attestation_id", self.capability_attestation_id),
                             ("activated_at", self.activated_at)):
            _text(value, field=field,
                  why="an active revision names the exact state a reader is seeing, and a blank "
                      "field names a state nobody could find")
        if not isinstance(self.mechanism, PublishMechanism):
            object.__setattr__(self, "mechanism", PublishMechanism(self.mechanism))
        if not isinstance(self.seq, int) or isinstance(self.seq, bool) or self.seq < 0:
            raise ValueError(
                f"active revision {self.revision_id!r} has seq {self.seq!r}: the sequence is what "
                f"orders a group's publications, and a value that is not a whole number orders "
                f"nothing")


def read_active_revision(conn: DbConn, logical_group_name: str) -> ActiveRevision | None:
    """The revision a reader of this group is seeing NOW, or ``None`` when it has never published.

    The newest ``seq``, read through the ``(logical_group_name, seq DESC)`` index. ``None`` is the
    ordinary answer for a group whose runs all terminated ``PUBLICATION_REFUSED`` — it means *"not
    published yet"*, never *"published, target unknown"*, and any surface reporting it must say so
    rather than inventing a table name.
    """
    row = conn.execute(
        "SELECT revision_id, logical_group_name, generation_id, run_id, published_object, "
        "capability_attestation_id, publication_mechanism, seq, activated_at, recorded_at "
        "FROM feature_active_revision WHERE logical_group_name = %s ORDER BY seq DESC LIMIT 1",
        (logical_group_name,)).fetchone()
    return None if row is None else ActiveRevision(*row)


def next_revision_seq(conn: DbConn, logical_group_name: str) -> int:
    """The ``seq`` the next publication of this group must carry.

    Read-then-write, and that is safe here for the reason ``append_run_event``'s caller-supplied seq
    is safe: migration 1055's BEFORE-INSERT trigger refuses any value that does not strictly extend
    the group, so two publishers that both computed the same next value cannot both win — the loser
    raises and its whole transaction rolls back. Computing it inside the INSERT would hide that
    check behind a value the database chose for itself.
    """
    row = conn.execute(
        "SELECT max(seq) FROM feature_active_revision WHERE logical_group_name = %s",
        (logical_group_name,)).fetchone()
    return 0 if row is None or row[0] is None else int(row[0]) + 1


def record_active_revision(conn: DbConn, revision: ActiveRevision) -> ActiveRevision:
    """Write migration 1055's pointer — INSERT only, because the table is append-only.

    Raises:
        psycopg.errors.RaiseException: ``seq`` does not extend the group — a stale publisher writing
            after a newer one, or two writers for one swap. Both leave the table unable to say what
            a reader sees, and the append-only guards make that unrepairable.
        psycopg.errors.UniqueViolation: this revision id was already recorded. One swap is one
            pointer.
    """
    if not isinstance(revision, ActiveRevision):
        raise TypeError(
            f"record_active_revision writes an ActiveRevision, got {type(revision).__name__}: the "
            f"row is the only durable statement that a feature was ever readable, and a duck-typed "
            f"stand-in would be a way to write one without its own validation")
    inserted = conn.execute(
        "INSERT INTO feature_active_revision (revision_id, logical_group_name, generation_id, "
        "run_id, published_object, capability_attestation_id, publication_mechanism, seq, "
        "activated_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING recorded_at",
        (revision.revision_id, revision.logical_group_name, revision.generation_id,
         revision.run_id, revision.published_object, revision.capability_attestation_id,
         revision.mechanism.value, revision.seq, revision.activated_at)).fetchone()
    return replace(revision, recorded_at=inserted[0] if inserted is not None else None)


def publish_generation(
    conn: DbConn,
    swap: PublicationSwap,
    *,
    selection: PublisherSelection,
    group_plan: FeatureGroupPlanV1,
    generation_id: str,
    run_id: str,
    published_object: str,
    staging_root: str,
    revision_id: str,
    activated_at: str,
) -> ActiveRevision:
    """G-3's publish step: RECORD the pointer, then PERFORM the swap. In that order, and not the
    other.

    **THE ORDER IS THE WHOLE DESIGN.** Both writes are inside the chain's one transaction, and only
    one of them can be rolled back:

    * *Record, then swap.* A swap that fails rolls the record back with it, the terminal event is
      never appended, and the plane has claimed nothing. A record that fails — 1055's ordering
      trigger firing because a newer revision already exists — means the swap never happens, so a
      stale publisher cannot overwrite a pointer that moved past it.
    * *Swap, then record.* A record that then failed would leave a cluster whose readers are seeing
      a generation the plane has no row for, and the append-only plane has no repair path for the
      row it did not write. That is the one ordering that can produce a published table nobody can
      audit.

    The irreducible residue is stated plainly: the cluster takes no part in the transaction, so a
    swap that SUCCEEDED and was then rolled back by a later failure leaves readers on a generation
    whose pointer row is gone. That window is one statement wide (the terminal append), it fails
    closed — the request lands non-terminal and the reconciler reads real plane evidence — and
    closing it properly needs a two-phase protocol against the metastore, which no probe has
    attested and §10.3 forbids assuming.

    ``selection`` is required and typed: a publish step that took a bare ``PublishMechanism`` would
    publish on a mechanism nobody probed, which is the whole thing §10.3's shapes exist to prevent.

    Returns:
        The recorded :class:`ActiveRevision`, carrying the database's own ``recorded_at``.
    """
    if not isinstance(selection, PublisherSelection):
        raise TypeError(
            f"publish_generation needs a PublisherSelection, got {type(selection).__name__}: §10.3 "
            f"says publication rests on evidence that this mechanism was probed and passed on the "
            f"versions the environment runs now, and a bare mechanism is a name anybody can spell")
    columns = tuple(column.name for column in expected_schema(group_plan))
    revision = record_active_revision(conn, ActiveRevision(
        revision_id=revision_id,
        logical_group_name=group_plan.logical_group_name,
        generation_id=generation_id,
        run_id=run_id,
        published_object=published_object,
        capability_attestation_id=selection.capability_attestation_id,
        mechanism=selection.mechanism,
        seq=next_revision_seq(conn, group_plan.logical_group_name),
        activated_at=activated_at))
    swap.swap(published_object=published_object, generation_id=generation_id, columns=columns,
              staging_root=staging_root)
    return revision
