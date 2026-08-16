"""C-A3c option (b) — the sealed snapshot tells the truth about ``unit``/``currency``.

The builder used to seal ``status="not_operational"`` for measure annotations UNCONDITIONALLY —
"hint by policy — never governed" — which is a statement about ``read_column_facts``, not about
whether a load-bearing decision exists. ``field_policies`` gives unit/currency
``operational_rule=_SOURCE_OR_HUMAN``, so a source-ATTESTED decision IS load-bearing; the VALUE was
already sealed beside the lie. Every downstream reader gates on ``status``, so a per-row-currency
operand read as NON-MONETARY and the mixed-currency protection never engaged.

Option (b) was chosen deliberately over an additive item kind: an additive kind would leave old
snapshots reading ``current`` while still asserting ``not_operational``, and those could later enter
V3 authoring and bypass the very protection being added. The one-time regeneration is the price of
one meaning per field.
"""
from __future__ import annotations

import psycopg

from featuregen.formula.measure_facts import (
    MEASURE_FIELDS,
    project_measure_read,
)
from featuregen.overlay.evidence import AssertionStrength, EvidenceProducer
from featuregen.overlay.field_evidence import field_input_hash, record_field_evidence
from featuregen.overlay.upload.column_authority import normalize_ref
from featuregen.overlay.upload.feature_metadata_snapshot import (
    _MEASURE_FIELDS,
    ITEM_KIND_COLUMN_FIELD,
    build_metadata_snapshot,
    compare_snapshot_to_current,
    snapshot_item_hash,
)
from featuregen.overlay.upload.field_resolution import resolve_and_project


class _Frozen:
    """A ``FrozenOperationalValue``-shaped read, as the snapshot hands the frozen reader."""

    def __init__(self, status: str, value: object | None = None,
                 conflict_status: str | None = None):
        self.status, self.value, self.conflict_status = status, value, conflict_status


_SRC, _SCHEMA, _TBL, _COL = "ca3c_src", "public", "accounts", "balance"
_OBJ = f"{_SCHEMA}.{_TBL}.{_COL}"
_REF = normalize_ref(_SRC, _SCHEMA, _TBL, _COL)


def _seed_attested_unit(conn, unit: str = "dollars") -> str:
    """One column whose ``unit`` carries SOURCE-ATTESTED evidence, resolved by the real resolver.

    The graph node is inserted directly (the pattern `test_feature_metadata_snapshot` uses) rather
    than through `build_graph`, because the snapshot builder needs a SCHEMA-qualified three-part
    object_ref — a two-part one raises "no graph_node row declares its kind" — while `build_graph`
    emits the bare `table.column` form.
    """
    conn.execute(
        "INSERT INTO graph_node (catalog_source, object_ref, kind, table_name, column_name, "
        "schema_name) VALUES (%s, %s, 'column', %s, %s, %s)",
        (_SRC, _OBJ, _TBL, _COL, _SCHEMA))
    record_field_evidence(
        conn, logical_ref=_REF, field_name="unit", proposed_value=unit,
        producer=EvidenceProducer.SOURCE, strength=AssertionStrength.ATTESTED,
        producer_ref="ca3c-fixture", source_snapshot_id="ca3c-snap",
        input_hash=field_input_hash(logical_ref=_REF, field_name="unit", material=unit))
    resolve_and_project(conn, source=_SRC, logical_refs=[_REF])
    return _REF


def _rr(conn) -> None:
    """Pin REPEATABLE READ BEFORE the first query, exactly as the C0-T2 feature-generation
    connection does — the builder refuses any other level, and psycopg refuses a mid-tx change."""
    conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ


def _snapshot(conn, run: str):
    return build_metadata_snapshot(
        conn, generation_run_id=run, refs=[(_SRC, _OBJ)], read_scope_hash="sha256:ca3c")


# ── the two definitions of "measure field" must not drift ────────────────────────────────────────
def test_the_measure_field_sets_agree():
    """`feature_metadata_snapshot` duplicates the set as literals to avoid an import cycle
    (`operational_facts` imports that module). This is the pin that keeps the copies equal."""
    assert _MEASURE_FIELDS == frozenset(MEASURE_FIELDS)


# ── 8. authority="hint" WITH status="resolved" is the shape of a verified measure annotation ─────
def test_a_verified_measure_seals_hint_authority_with_resolved_status(conn):
    """The combination the governed branch structurally cannot produce, and which is exactly what a
    verified measure ANNOTATION is: `column_authority` still classifies the field as a hint, while
    the DECISION behind it verified."""
    _rr(conn)
    _seed_attested_unit(conn)                       # source-ATTESTED `unit`
    ctx = _snapshot(conn, "genrun_ca3c_8")
    unit = [i for i in ctx.items() if i.field_or_fact_type == "unit"]
    assert unit, "the snapshot must carry a unit item"
    assert unit[0].authority == "hint"                      # unchanged classification
    assert unit[0].status == "resolved"                     # truthful decision status
    assert unit[0].value == "dollars"                       # the value was always there


# ── 1. an OLD sealed snapshot now reads as drift ────────────────────────────────────────────────
def test_a_legacy_measure_item_no_longer_matches_and_so_reads_as_drift(conn):
    """`compare_snapshot_to_current` reports SNAPSHOT_ITEM_DRIFT when a stored item_hash differs
    from the one THIS build recomputes — its comparator IS `_build_item`. So the drift is proved by
    showing the legacy material and the current material hash differently for the same column.

    The stored row cannot simply be rewritten to stage a legacy snapshot: the item table is
    WRITE-ONCE by trigger ("catalog_metadata_snapshot_item records are write-once: UPDATE not
    allowed"), which is the requirement "existing immutable snapshots remain untouched" enforced by
    the database rather than by convention.
    """
    _rr(conn)
    _seed_attested_unit(conn)
    ctx = _snapshot(conn, "genrun_ca3c_1")
    assert compare_snapshot_to_current(conn, ctx.snapshot_id).status == "current"

    unit = next(i for i in ctx.items() if i.field_or_fact_type == "unit")
    material = {
        "catalog_source": _SRC, "graph_ref": _OBJ, "field": "unit",
        "value": unit.value, "authority": unit.authority, "provenance": unit.provenance,
    }
    legacy = snapshot_item_hash(ITEM_KIND_COLUMN_FIELD, {**material, "status": "not_operational"})
    current = snapshot_item_hash(ITEM_KIND_COLUMN_FIELD, {**material, "status": unit.status})

    assert unit.status == "resolved"
    assert legacy != current, (
        "a snapshot sealed by the old builder must NOT match what this build recomputes — that "
        "mismatch is exactly what compare_snapshot_to_current reports as SNAPSHOT_ITEM_DRIFT")
    assert current == unit.item_hash


def test_the_item_table_is_write_once(conn):
    """Pins the guarantee the test above relies on: a sealed item cannot be rewritten, so
    regeneration can only ever create a NEW revision."""
    import psycopg as _pg
    _rr(conn)
    _seed_attested_unit(conn)
    ctx = _snapshot(conn, "genrun_ca3c_wo")
    try:
        conn.execute("UPDATE catalog_metadata_snapshot_item SET item_hash = %s "
                     "WHERE snapshot_id = %s", ("tampered", ctx.snapshot_id))
    except _pg.errors.RaiseException as exc:
        assert "write-once" in str(exc)
    else:
        raise AssertionError("the item table must refuse an UPDATE")


# ── 3. a REGENERATED snapshot is current ────────────────────────────────────────────────────────
def test_a_regenerated_snapshot_is_current(conn):
    """The remedy the drift instructs: regenerate. Never rewrite the old record."""
    _rr(conn)
    _seed_attested_unit(conn)
    old = _snapshot(conn, "genrun_ca3c_3a")
    new = _snapshot(conn, "genrun_ca3c_3b")
    assert old.snapshot_id != new.snapshot_id            # a NEW revision, not a rewrite
    assert compare_snapshot_to_current(conn, new.snapshot_id).status == "current"


# ── 6. an unreadable measure read refuses rather than becoming an empty value ───────────────────
def test_the_frozen_projection_refuses_an_unreadable_measure():
    """`_fact_text` alone turns EVERY non-resolved read into "" while `_hard_failure` recognises
    only three statuses — so conflict / retired / not_operational became the empty fact with no
    attribution. The shared projection is what closes that."""
    for status in ("conflict", "fork", "hash_mismatch", "retired",
                   "not_operational", "projection_unavailable", "some_future_status"):
        text, refusal = project_measure_read(_Frozen(status, "monetary"), "ftr::t.c", "unit")
        assert text == ""
        assert refusal is not None, status
        assert refusal.field == "unit" and refusal.status == status


def test_the_frozen_projection_passes_resolved_and_absent_through():
    """The two honest non-refusals: a verified value, and a column nobody has decided."""
    text, refusal = project_measure_read(_Frozen("resolved", "monetary"), "ftr::t.c", "unit")
    assert (text, refusal) == ("monetary", None)
    for status in ("no_decision", "no_value"):
        text, refusal = project_measure_read(_Frozen(status), "ftr::t.c", "unit")
        assert (text, refusal) == ("", None), status
    # nothing sealed for the field at all is absent, not a refusal
    assert project_measure_read(None, "ftr::t.c", "unit") == ("", None)


# ── 7. V1 and the non-measure fields are untouched ──────────────────────────────────────────────
def test_non_measure_fields_are_sealed_exactly_as_before(conn):
    """The change is scoped to `_MEASURE_FIELDS`. A governed decision field still seals through
    `read_operational_value`, and a non-measure hint still seals `not_operational`."""
    _rr(conn)
    _seed_attested_unit(conn)
    ctx = _snapshot(conn, "genrun_ca3c_7")
    by_field = {i.field_or_fact_type: i for i in ctx.items()}
    assert by_field["declared_type"].status == "not_operational"   # hint-by-policy, unchanged
    assert by_field["declared_type"].authority == "hint"
    assert by_field["additivity"].status != "resolved" or (
        by_field["additivity"].authority == "governed")            # governed branch intact


# ── 2. measure fields now PARTICIPATE in drift detection ────────────────────────────────────────
def test_a_changed_measure_decision_now_drifts_the_snapshot(conn):
    """The behavioural half of "activation returns SNAPSHOT_STALE_REGENERATE".

    Before C-A3c a measure field sealed `status="not_operational"` NO MATTER WHAT its decision said,
    so changing the decision could not move the item hash and the field was invisible to drift
    detection. Now it participates: re-deciding `unit` after sealing makes
    `compare_snapshot_to_current` report SNAPSHOT_ITEM_DRIFT.

    The second half of the chain — drifted snapshot ⇒ the `SNAPSHOT_STALE_REGENERATE` activation
    blocker — is already covered end-to-end by
    `tests/featuregen/api/test_binding_confirmation.py::test_confirm_fails_closed_when_a_binding_column_is_retyped`
    and `::test_confirm_revalidation_fails_closed_on_expired_fact`, which drive it through
    /contract/confirm. That link is drift-cause agnostic, so it is not re-proved here.
    """
    _rr(conn)
    _seed_attested_unit(conn)
    ctx = _snapshot(conn, "genrun_ca3c_2")
    assert compare_snapshot_to_current(conn, ctx.snapshot_id).status == "current"

    # the same column's unit is re-decided to a DIFFERENT attested value
    record_field_evidence(
        conn, logical_ref=_REF, field_name="unit", proposed_value="euros",
        producer=EvidenceProducer.SOURCE, strength=AssertionStrength.ATTESTED,
        producer_ref="ca3c-fixture-2", source_snapshot_id="ca3c-snap-2",
        input_hash=field_input_hash(logical_ref=_REF, field_name="unit", material="euros"))
    resolve_and_project(conn, source=_SRC, logical_refs=[_REF])

    freshness = compare_snapshot_to_current(conn, ctx.snapshot_id)
    assert freshness.status == "drifted"
    assert freshness.reason == "SNAPSHOT_ITEM_DRIFT"


# ── 4 + 5. END TO END on the live path: verified catalog facts reach the resolver and it refuses ─
def test_a_verified_per_row_currency_refuses_through_the_LIVE_reader(conn):
    """The chain D3 depends on, proved without injecting a single fact by hand.

    Seed `unit=monetary` + `currency=per_row` as SOURCE-ATTESTED evidence, run the real
    `_read_c1_facts_v2`, hand its bundle to the real `resolve_output_v2`, and assert
    CURRENCY_CONVERSION_UNDECLARED. Before C-A3c this was unreachable: both facts read as ``""``, the
    operand looked non-monetary, and the tooth never fired — so the only test that reached this
    refusal built `OperandFactsV2` by hand and said so in its docstring.
    """
    import json
    from pathlib import Path

    from featuregen.formula.authoring_v2 import _read_c1_facts_v2
    from featuregen.formula.output_authority_v2 import (
        CURRENCY_CONVERSION_UNDECLARED,
        InvalidOutputV2,
        resolve_output_v2,
    )
    from featuregen.formula.parse_v2 import parse_proposal_v2

    _rr(conn)
    _seed_attested_unit(conn, unit="monetary")      # ONE attested unit — a second, different
    record_field_evidence(                          # attested value would (correctly) be a CONFLICT
        conn, logical_ref=_REF, field_name="currency", proposed_value="per_row",
        producer=EvidenceProducer.SOURCE, strength=AssertionStrength.ATTESTED,
        producer_ref="ca3c-currency", source_snapshot_id="ca3c-currency-snap",
        input_hash=field_input_hash(logical_ref=_REF, field_name="currency", material="per_row"))
    resolve_and_project(conn, source=_SRC, logical_refs=[_REF])

    raw = json.loads((Path(__file__).parents[2] / "formula" / "gold_v2"
                      / "01_avg_txn_amt_90d.json").read_text())["proposal"]
    raw["body"]["expr"]["operand"] = _REF
    raw["body"]["expr"]["source_relation"] = {"table_ref": f"{_SRC}::{_SCHEMA}.{_TBL}"}
    raw["body"]["expr"]["authority_refs"] = None    # NO conversion policy declared
    raw["body"]["expr"]["window"]["event_time_ref"] = normalize_ref(
        _SRC, _SCHEMA, _TBL, "event_ts")           # must sit in the same source_relation
    raw["grain"] = {"entity": "account", "keys": [_REF]}
    proposal = parse_proposal_v2(raw)

    facts_by_ref, failures = _read_c1_facts_v2(conn, proposal)
    assert failures == (), f"a verified measure must not fail closed: {failures}"
    facts = facts_by_ref[_REF]
    assert (facts.unit, facts.currency) == ("monetary", "per_row"), (
        "the LIVE reader must deliver the verified measure facts — this is the read that used to "
        "come back empty")

    verdict = resolve_output_v2(proposal, facts_by_ref)
    assert isinstance(verdict, InvalidOutputV2)
    assert verdict.reason == CURRENCY_CONVERSION_UNDECLARED
