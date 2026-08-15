"""Spec §10.3 — the DEPLOYMENT's publication swap: ONE metastore operation, over the same session.

:class:`~featuregen.materialize.publish.PublicationSwap` is the seam; this is the implementation the
deployment supplies for
:attr:`~featuregen.materialize.publish.PublishMechanism.VERSIONED_POINTER`, and it is the ONLY
mechanism it can perform — the one the renderer emits an entry for
(``render.publish.RENDERABLE_MECHANISMS``) and the one the probe attempts. A swap that could perform
a mechanism the renderer cannot emit would publish on a capability nothing demonstrated by a second
route.

**WHAT THE MECHANISM IS, in one sentence, and why the statement is exactly one.** §10.3's preferred
first-slice mechanism is *"immutable versioned physical outputs with ONE reader-visible pointer/view
switch"*. ``render.publish`` renders the immutable versioned output: every generation writes parquet
under its own staging root, and ``errorifexists`` protects it. What remains is the pointer, and this
module moves it with a single ``CREATE OR REPLACE VIEW`` naming the generation's location — one
statement the engine commits in the metastore as a unit, which is the act §10.3's probe watches
readers through. A sequence — drop then create, or create-if-absent then alter — would be two
operations with an observable state between them, and the attestation would be evidence about
neither. So the mutation count is not an implementation detail: it is the property, and
``test_publish_sql`` asserts it by counting statements.

**THE READ-BACK, and the failure that makes it necessary rather than decorative.** After the
mutation this module asks the engine what the object now IS, and refuses (
:class:`PublicationSwapUnconfirmed`) unless the answer names this generation's location. That is
this repository's own discipline, learned twice on the kind sandbox and written into
``deploy/kind/sandbox/up.sh``: *"A write succeeding proves nothing: Spark falls back to an embedded
Derby metastore silently, and a second SEQUENTIAL session still sees the tables."* A DDL statement
that returns without error against a session-local catalog is precisely the swap that half-happened
— the plane would record a pointer no reader can follow — and the read-back is what makes it
detectable. It reads metadata only; it never selects a row of the published table.

**IDEMPOTENCY.** ``CREATE OR REPLACE VIEW`` is idempotent by construction: re-running a completed
swap emits the same statement, leaves the same definition and returns. That is the no-op half of
the seam's law, and it is the honest half here — the *refusal* against re-publication lives one
level up, where :func:`~featuregen.materialize.publish.publish_generation` records the pointer row
BEFORE swapping and migration 1055's trigger refuses a ``seq`` that does not strictly extend the
group. This module cannot see that history and must not invent a second opinion about it.

**WHAT IT DELIBERATELY DOES NOT DO.** It computes nothing (the staged output is what §9's gates were
run against), it copies no data, it deletes no previous generation (an immutable versioned output is
the evidence a prior publication was gated, and reclaiming space is an operator's decision with its
own retention policy), and it opens no connection of its own —
:class:`~featuregen.materialize.metastore_sql.MetastoreSession` is shared with the metadata adapter
so the questions L1 asked and the swap G-3 performs cannot be answered by two clients seeing two
different worlds.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import ClassVar

from featuregen.materialize.metastore_sql import MetastoreSession, quoted_identifier
from featuregen.materialize.publish import PublishMechanism
from featuregen.materialize.render.publish import RENDERABLE_MECHANISMS, published_output_location

__all__ = [
    "PublicationSwapUnconfirmed",
    "SqlPublicationSwap",
]

#: Characters that would end the quoted location and let the rest of it be parsed as SQL. A path is
#: not an identifier and cannot be validated as one, so the check is that it cannot LEAVE its quotes
#: — and it refuses rather than escaping, for `quoted_identifier`'s reason.
_PATH_BREAKERS = ("`", "'", '"', "\\", ";", "\n", "\r", "\0")


class PublicationSwapUnconfirmed(Exception):
    """The mutation returned without error and the engine does not show the new state.

    Raised AFTER the statement, so it does not mean "nothing happened" — it means *this process
    cannot confirm what happened*, which is the one thing a publication may not assume. It
    propagates out of :func:`~featuregen.materialize.publish.publish_generation`, whose transaction
    then rolls the active-revision row back: the plane claims nothing rather than recording a
    pointer no reader can follow.
    """


@dataclass(frozen=True, slots=True)
class SqlPublicationSwap:
    """:class:`~featuregen.materialize.publish.PublicationSwap` for ``VERSIONED_POINTER``.

    One field: the session. Everything else about a swap — which object, which generation, which
    columns, which staging root — arrives as an argument, because each of them is the RUN's fact and
    a deployment that could state one would be choosing what a governed group publishes.
    """

    session: MetastoreSession

    #: The mechanism this implementation performs. Not a parameter: an object that could be asked to
    #: perform ``SET_LOCATION`` would publish on a form the renderer never emitted and no
    #: attestation covers.
    mechanism: ClassVar[PublishMechanism] = PublishMechanism.VERSIONED_POINTER

    def swap(self, *, published_object: str, generation_id: str, columns: Sequence[str],
             staging_root: str) -> None:
        """Make ``generation_id``'s staged output the reader-visible state of ``published_object``.

        Args:
            published_object: the derived publication target (``binding.physical_target_for``),
                dotted and possibly catalog-qualified. Every segment is validated as an identifier.
            generation_id: whose output this is. Present on the record and in the location; not
                interpolated into the statement on its own, because the location already names it.
            columns: the plan's ``expected_schema`` IN THE PLAN'S ORDER — §10.3 step 5's
                schema-evolution question is about exactly this list, and naming them in the view is
                what makes the published schema the plan's rather than the parquet writer's.
            staging_root: §9's per-generation root the run's gates were run against.

        Raises:
            ValueError: an identifier, a column or a path this module will not interpolate.
            PublicationSwapUnconfirmed: the engine does not show the new state afterwards.
            ClusterUnreachable / MetastoreFaultError: from the session, unchanged — a swap that
                could not be attempted is not a swap that failed to be atomic.
        """
        if self.mechanism not in RENDERABLE_MECHANISMS:      # pragma: no cover - a guard on a law
            raise ValueError(
                f"{self.mechanism.value} is not one of "
                f"{sorted(m.value for m in RENDERABLE_MECHANISMS)}: this swap must perform the "
                f"same mechanism the renderer emitted an entry for, or a run would publish through "
                f"a form nothing attested")
        target = _quoted_object(published_object)
        if not isinstance(generation_id, str) or not generation_id.strip():
            raise ValueError(
                f"the generation id is blank ({generation_id!r}): the location a swap points at is "
                f"generation-scoped, and a blank one would point at the staging base itself")
        selected = _quoted_columns(columns)
        location = _quoted_path(
            published_output_location(published_object, _required(staging_root, "staging_root")))

        # THE ONE MUTATION. `CREATE OR REPLACE VIEW` carries the definition AND the schema in a
        # single metastore commit, which is why it can cover §10.3 step 5 (adding a feature column)
        # where a partition-location swap cannot: a reader sees the old view or the new one.
        self.session.statement(
            f"CREATE OR REPLACE VIEW {target} AS SELECT {selected} FROM parquet.{location}")

        _names, rows = self.session.table(f"SHOW CREATE TABLE {target}")
        shown = " ".join(str(cell) for row in rows for cell in row)
        if location.strip("`") not in shown:
            raise PublicationSwapUnconfirmed(
                f"the swap of {published_object} to generation {generation_id} returned without "
                f"error, and the engine does not show {location.strip('`')} as its definition "
                f"(it shows {shown[:400]!r}). A DDL statement that succeeds against a session-local "
                f"catalog is exactly the swap that half-happened — deploy/kind/sandbox/up.sh "
                f"records this failure twice — so the pointer row is rolled back rather than "
                f"recorded against a state no reader can follow")


def _required(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(
            f"{field_name} is blank ({value!r}): a swap points readers at ONE location, and a "
            f"blank part of it points at a different one")
    return value.strip()


def _quoted_object(published_object: str) -> str:
    """``sandbox_feature.txn_features`` → ```sandbox_feature`.`txn_features```, every segment
    validated. The target is DERIVED upstream (``physical_target_for``); this refuses to interpolate
    one that does not look derived."""
    segments = _required(published_object, "published_object").split(".")
    if len(segments) < 2:
        raise ValueError(
            f"the published object {published_object!r} names no namespace: §10.1 derives it as "
            f"<namespace>.<table>, and a bare name would create a view wherever the session's "
            f"current database happened to point")
    return ".".join(quoted_identifier(segment, field_name="published_object segment")
                    for segment in segments)


def _quoted_columns(columns: Sequence[str]) -> str:
    quoted = [quoted_identifier(column, field_name="published column") for column in columns]
    if not quoted:
        raise ValueError(
            "the publication names no columns: the view's SELECT list IS the published schema "
            "(§10.3 step 5), and a view over no columns publishes nothing while looking published")
    return ", ".join(quoted)


def _quoted_path(location: str) -> str:
    """The location, back-quoted for ``parquet.`…``` — refused if it could leave its quotes."""
    breaker = next((character for character in _PATH_BREAKERS if character in location), None)
    if breaker is not None:
        raise ValueError(
            f"the staged location {location!r} contains {breaker!r}: a path is not an identifier "
            f"and cannot be validated as one, so this module refuses a path that could end its own "
            f"quoting rather than escaping one it cannot vouch for")
    return f"`{location}`"
