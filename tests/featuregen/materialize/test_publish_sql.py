"""SUCCESSOR 2 increment 2 — the real ``PublicationSwap``: ONE metastore operation, confirmed.

**The two laws under test, both from ``publish.py``'s own docstrings.** *"One method, deliberately.
§10.3's probe demonstrates that ONE operation makes a whole new generation visible atomically; a
seam with a separate 'write the metadata' and 'flip the pointer' would be two operations, and the
attestation would be evidence about neither."* — so the tests below COUNT the mutating statements
rather than asserting that a swap "worked". And *"the cluster takes no part in the transaction"* —
so a swap this process cannot confirm must refuse, leaving
:func:`~featuregen.materialize.publish.publish_generation`'s transaction to roll the pointer row
back, rather than letting the plane record a pointer no reader can follow.

The transport is the same DB-API double the metastore adapter's suite uses, for the same reason: no
socket, no driver, no JVM.
"""
from __future__ import annotations

import pytest
from tests.featuregen.materialize.test_metastore_sql import ENDPOINT, _Engine

from featuregen.materialize.metastore_sql import MetastoreSession
from featuregen.materialize.publish import PublishMechanism
from featuregen.materialize.publish_sql import PublicationSwapUnconfirmed, SqlPublicationSwap
from featuregen.materialize.render.publish import (
    RENDERABLE_MECHANISMS,
    published_output_location,
)

OBJECT = "sandbox_feature.txn_features"
GENERATION = "gen-2026-08-15-001"
COLUMNS = ("cif_id", "business_dt", "total_debit_amount_30d")
STAGING_ROOT = "/warehouse/staging/gen-2026-08-15-001"
LOCATION = "/warehouse/staging/gen-2026-08-15-001/published/txn_features"

#: "let the double apply the DDL and report it back", as distinct from stocking a canned answer.
_APPLIED = object()


def _swap(*, shown: str | None = _APPLIED):
    """A swap over the DB-API double.

    By default nothing is stocked, so the double APPLIES the ``CREATE OR REPLACE VIEW`` and reports
    it back — a real round-trip through the statement the swap actually emitted, rather than a
    canned confirmation that would agree with any swap at all. ``shown`` stages the two failures
    that matter: an engine reporting a DIFFERENT location, and one reporting nothing.
    """
    stocked = {} if shown is _APPLIED else {"SHOW CREATE TABLE": (
        ("createtab_stmt",),
        () if shown is None else ((f"CREATE VIEW `sandbox_feature`.`txn_features` AS SELECT "
                                   f"`cif_id` FROM parquet.`{shown}`",),))}
    engine = _Engine(stocked)
    return SqlPublicationSwap(MetastoreSession(ENDPOINT, connect=engine.connect)), engine


def _mutations(engine: _Engine) -> list[str]:
    return [statement for statement in engine.statements
            if not statement.startswith(("SHOW", "DESCRIBE"))]


# ── the mutation, and how many there are ─────────────────────────────────────────────────────────


def test_the_swap_is_exactly_ONE_mutating_statement() -> None:
    """The property, not a detail. A drop-then-create or a create-then-alter would have a state
    between them that a reader can observe, and §10.3's attestation would be evidence about
    neither half."""
    swap, engine = _swap()

    swap.swap(published_object=OBJECT, generation_id=GENERATION, columns=COLUMNS,
              staging_root=STAGING_ROOT)

    assert _mutations(engine) == [
        "CREATE OR REPLACE VIEW `sandbox_feature`.`txn_features` AS SELECT "
        "`cif_id`, `business_dt`, `total_debit_amount_30d` FROM parquet."
        f"`{LOCATION}`"]


def test_the_pointer_is_moved_to_the_ONE_definition_of_the_generation_s_location() -> None:
    """The location is not spelled here: it is `render.publish.published_output_location`, the same
    function the rendered catalog entry writes into the project. Two spellings of one derived path
    are two paths, and the second finds an empty directory rather than an error."""
    assert published_output_location(OBJECT, STAGING_ROOT) == LOCATION

    swap, engine = _swap()
    swap.swap(published_object=OBJECT, generation_id=GENERATION, columns=COLUMNS,
              staging_root=STAGING_ROOT)

    assert f"parquet.`{published_output_location(OBJECT, STAGING_ROOT)}`" in _mutations(engine)[0]


def test_the_view_names_the_PLAN_S_columns_in_the_PLAN_S_order() -> None:
    """§10.3 step 5's schema-evolution question is about exactly this list, and a `SELECT *` would
    publish whatever the parquet writer happened to emit."""
    swap, engine = _swap()

    swap.swap(published_object=OBJECT, generation_id=GENERATION,
              columns=("z_last", "a_first"), staging_root=STAGING_ROOT)

    assert "SELECT `z_last`, `a_first` FROM" in _mutations(engine)[0]


def test_the_mechanism_is_the_one_the_RENDERER_emits() -> None:
    """A swap that could perform `SET_LOCATION` would publish through a form the renderer never
    emitted and no attestation covers."""
    assert SqlPublicationSwap.mechanism is PublishMechanism.VERSIONED_POINTER
    assert SqlPublicationSwap.mechanism in RENDERABLE_MECHANISMS


def test_it_satisfies_the_PublicationSwap_protocol() -> None:
    from featuregen.materialize.publish import PublicationSwap

    swap, _engine = _swap()
    assert isinstance(swap, PublicationSwap)


# ── idempotency ──────────────────────────────────────────────────────────────────────────────────


def test_RE_RUNNING_a_completed_swap_is_a_NO_OP() -> None:
    """`CREATE OR REPLACE VIEW` is idempotent by construction: the same arguments emit the same
    statement and leave the same definition. The REFUSAL against re-publication is one level up —
    `publish_generation` records the 1055 pointer BEFORE swapping, and its trigger refuses a `seq`
    that does not strictly extend the group — and this module must not invent a second opinion
    about a history it cannot see."""
    swap, engine = _swap()

    swap.swap(published_object=OBJECT, generation_id=GENERATION, columns=COLUMNS,
              staging_root=STAGING_ROOT)
    first = list(_mutations(engine))
    swap.swap(published_object=OBJECT, generation_id=GENERATION, columns=COLUMNS,
              staging_root=STAGING_ROOT)

    assert _mutations(engine) == first * 2, "the second swap emitted a different statement"
    assert engine.connections == 1, "a swap opened a second connection to the same metastore"


# ── the read-back ────────────────────────────────────────────────────────────────────────────────


def test_a_swap_the_engine_does_not_CONFIRM_is_refused() -> None:
    """The half-completed swap this repository has actually met: `deploy/kind/sandbox/up.sh` records
    it twice — Spark falls back to an embedded Derby metastore SILENTLY, the DDL returns without
    error, and a second sequential session even sees the object. `up.sh`'s own answer is to assert
    the state in the shared catalog rather than to trust the write, and this is that assertion."""
    swap, engine = _swap(shown="/warehouse/staging/gen-SOMETHING-ELSE/published/txn_features")

    with pytest.raises(PublicationSwapUnconfirmed) as raised:
        swap.swap(published_object=OBJECT, generation_id=GENERATION, columns=COLUMNS,
                  staging_root=STAGING_ROOT)

    assert LOCATION in str(raised.value)
    assert len(_mutations(engine)) == 1, "it retried the mutation instead of refusing"


def test_an_object_the_engine_shows_NOTHING_for_is_refused() -> None:
    swap, _engine = _swap(shown=None)

    with pytest.raises(PublicationSwapUnconfirmed):
        swap.swap(published_object=OBJECT, generation_id=GENERATION, columns=COLUMNS,
                  staging_root=STAGING_ROOT)


def test_the_read_back_reads_METADATA_and_never_a_row() -> None:
    """§14: the control plane does not read feature data, including to confirm its own swap."""
    swap, engine = _swap()

    swap.swap(published_object=OBJECT, generation_id=GENERATION, columns=COLUMNS,
              staging_root=STAGING_ROOT)

    assert [statement for statement in engine.statements
            if statement.startswith("SHOW")] == [
        "SHOW CREATE TABLE `sandbox_feature`.`txn_features`"]
    assert not any("SELECT" in statement and statement.startswith("SELECT")
                   for statement in engine.statements)


# ── what will not be interpolated ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("hostile", [
    "sandbox_feature.txn`; DROP DATABASE sandbox_feature; --",
    "txn_features",                       # no namespace: it would land wherever the session points
    " ",
])
def test_a_published_object_that_does_not_look_DERIVED_is_refused(hostile) -> None:
    swap, engine = _swap()

    with pytest.raises(ValueError):
        swap.swap(published_object=hostile, generation_id=GENERATION, columns=COLUMNS,
                  staging_root=STAGING_ROOT)

    assert engine.statements == []


def test_a_hostile_COLUMN_never_reaches_the_view() -> None:
    swap, engine = _swap()

    with pytest.raises(ValueError):
        swap.swap(published_object=OBJECT, generation_id=GENERATION,
                  columns=("cif_id", "x` , (SELECT secret FROM vault) AS y --"),
                  staging_root=STAGING_ROOT)

    assert engine.statements == []


def test_a_PATH_that_could_end_its_own_quoting_is_refused() -> None:
    """A path is not an identifier and cannot be validated as one, so the check is that it cannot
    LEAVE its quotes."""
    swap, engine = _swap()

    with pytest.raises(ValueError, match="could end its own quoting"):
        swap.swap(published_object=OBJECT, generation_id=GENERATION, columns=COLUMNS,
                  staging_root="/warehouse/`; DROP TABLE x; --")

    assert engine.statements == []


@pytest.mark.parametrize("blank", ["", "   "])
def test_a_BLANK_generation_or_staging_root_is_refused(blank) -> None:
    """The location a swap points at is generation-scoped; a blank part of it points somewhere
    else — the staging base itself, which holds every generation."""
    swap, _engine = _swap()

    with pytest.raises(ValueError):
        swap.swap(published_object=OBJECT, generation_id=blank, columns=COLUMNS,
                  staging_root=STAGING_ROOT)
    with pytest.raises(ValueError):
        swap.swap(published_object=OBJECT, generation_id=GENERATION, columns=COLUMNS,
                  staging_root=blank)


def test_a_publication_with_NO_columns_is_refused() -> None:
    swap, _engine = _swap()

    with pytest.raises(ValueError, match="publishes nothing"):
        swap.swap(published_object=OBJECT, generation_id=GENERATION, columns=(),
                  staging_root=STAGING_ROOT)
