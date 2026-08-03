"""Feature-context v4 — the shared semantic bundle reaches feature generation (semantic Task 8).

What this pins, in the order the review raised it:

* **The D8 rollback ladder.** Flag off -> v1 (the thin menu, byte-for-byte). Flag on +
  `FEATUREGEN_FEATURE_CONTEXT_VERSION=3` -> today's SHIPPED v3. Flag on -> v4. The env override
  exists precisely so v3 stays reachable; a rollback to the v1 thin menu would be a functional
  regression dressed as a safety valve.
* **D10 registration is a PRECONDITION.** Every version `_feature_schema_version()` can return has
  a registered body, and `_require_schema` refuses to dispatch a pair it cannot resolve — the trap
  that made feature generation silently return nothing with the flag on.
* **Every new key is egress-classified**, with a golden payload driven through the real
  `sanitize_feature_context`. Unclassified stays blocked, and now loudly.
* **`llm_proposed` labeling rides the D2 triple**, not the `governed|hint` wrapper, which means
  something else entirely (operational influence).
* **Validator invariants survive the richer context**: a BIC is never a numeric measure, an amount
  is never a join key, and a mixed-currency aggregation is refused.
* **The worked acceptance** — "total outgoing counterparty amount by customer, 30 days" — runs as a
  FakeLLM PROMPT-SHAPE test: the context must nominate the customer grain, the time anchor, the
  monetary measure and the currency while keeping the BIC an optional grouping dimension, never
  the customer join key.
"""
from __future__ import annotations

import json

import pytest

from featuregen.intake.llm import FakeLLM, FakeResponse
from featuregen.overlay.field_evidence import field_input_hash, record_field_evidence
from featuregen.overlay.upload import feature_assist as fa
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.enrich_llm import sanitize_feature_context
from featuregen.overlay.upload.feature_assist import (
    RejectCode,
    _candidate_columns,
    _context_column,
    _feature_schema_version,
    _menu,
    recommend_features,
)
from featuregen.overlay.upload.graph import build_graph

_SRC = "bank"


@pytest.fixture
def v4(monkeypatch):
    monkeypatch.setenv("FEATUREGEN_FEATURE_CONTEXT", "1")
    monkeypatch.delenv(fa.FEATURE_CONTEXT_VERSION_ENV, raising=False)
    return monkeypatch


def _bank_graph(db):
    """The worked-acceptance shape: a customer dimension, a payments fact with an amount in a
    declared currency, a posting timestamp, and a counterparty BIC."""
    rows = [
        CanonicalRow(_SRC, "customer", "cif_id", "text", is_grain=True,
                     definition="Customer information file identifier."),
        CanonicalRow(_SRC, "payments", "cust_num", "text", joins_to="customer.cif_id",
                     cardinality="N:1", definition="Customer reference on the payment."),
        CanonicalRow(_SRC, "payments", "tran_amt", "numeric", additivity="additive",
                     unit="currency", currency="AED", definition="Outgoing transaction amount."),
        CanonicalRow(_SRC, "payments", "pstd_date", "timestamp", as_of=True,
                     definition="Posting date of the transaction."),
        CanonicalRow(_SRC, "payments", "counter_party_bic", "text",
                     definition="Bank identifier code of the counterparty institution."),
    ]
    build_graph(db, _SRC, rows)
    db.execute("UPDATE graph_node SET grain_fact_event_id = 'fe_grain' "
               "WHERE object_ref = 'public.customer.cif_id'")
    db.execute("UPDATE graph_node SET availability_fact_event_id = 'fe_avail' "
               "WHERE object_ref = 'public.payments.pstd_date'")
    db.execute("UPDATE graph_node SET concept = 'monetary_flow', declared_type = 'decimal' "
               "WHERE object_ref = 'public.payments.tran_amt'")
    db.execute("UPDATE graph_node SET concept = 'bank_identifier_code', declared_type = 'varchar' "
               "WHERE object_ref = 'public.payments.counter_party_bic'")
    db.execute("UPDATE graph_node SET concept = 'customer_id', entity = 'customer' "
               "WHERE object_ref = 'public.customer.cif_id'")
    # An LLM-PROPOSED concept: the exact case whose authority must stay legible.
    record_field_evidence(
        db, logical_ref=f"{_SRC}::public.payments.tran_amt", field_name="concept",
        proposed_value="monetary_flow", producer="llm", strength="proposed",
        producer_ref="pass-a", source_snapshot_id="snap",
        input_hash=field_input_hash(logical_ref=f"{_SRC}::public.payments.tran_amt",
                                    field_name="concept", material="monetary_flow:llm"))


def _column(db, object_ref: str) -> dict:
    cols = _candidate_columns(db, _SRC, roles=())
    row = next(c for c in cols if c["object_ref"] == object_ref)
    return _context_column(db, row, roles=())


# ── D8: the rollback ladder ─────────────────────────────────────────────────────────────────────


def test_flag_off_is_v1_and_the_thin_menu_byte_for_byte(db, monkeypatch):
    monkeypatch.delenv("FEATUREGEN_FEATURE_CONTEXT", raising=False)
    _bank_graph(db)
    assert _feature_schema_version() == 1
    cols = _candidate_columns(db, _SRC, roles=())
    assert all(set(m) == {"object_ref", "table", "column", "concept", "domain"}
               for m in _menu(cols))


def test_flag_on_defaults_to_v4(db, v4):
    assert _feature_schema_version() == 4


def test_the_env_override_keeps_v3_reachable_not_the_v1_thin_menu(db, v4, monkeypatch):
    """The whole reason the override exists (D8). A rollback that dropped to v1 would delete the
    shipped rich context, which is a regression, not a safety valve."""
    monkeypatch.setenv(fa.FEATURE_CONTEXT_VERSION_ENV, "3")
    assert _feature_schema_version() == 3
    _bank_graph(db)
    payload = _column(db, "public.payments.tran_amt")
    # v3's shape: no bundle keys.
    assert "concept_path" not in payload and "semantic_authority" not in payload
    assert payload["additivity"] == {"value": "additive", "authority": "hint"}


def test_an_unrenderable_version_falls_back_to_the_default_rather_than_downgrading(db, v4,
                                                                                  monkeypatch):
    """A typo in a deploy manifest must not silently ship a different contract."""
    for raw in ("2", "99", "", "  ", "four"):
        monkeypatch.setenv(fa.FEATURE_CONTEXT_VERSION_ENV, raw)
        assert _feature_schema_version() == 4, raw


# ── D10: registration before a version may be requested ─────────────────────────────────────────


def test_every_version_the_ladder_can_return_is_registered(db, v4, monkeypatch):
    from featuregen.overlay.upload.enrich_llm import _SCHEMAS

    ids = ("feature_ideas", "feature_recipe", "leakage", "feature_set_rec")
    for version in (1, 3, 4):
        if version == 1:
            monkeypatch.delenv("FEATUREGEN_FEATURE_CONTEXT", raising=False)
        else:
            monkeypatch.setenv("FEATUREGEN_FEATURE_CONTEXT", "1")
            monkeypatch.setenv(fa.FEATURE_CONTEXT_VERSION_ENV, str(version))
        stamped = _feature_schema_version()
        assert stamped == version
        for schema_id in ids:
            assert (schema_id, stamped) in _SCHEMAS, (schema_id, stamped)


def test_an_unregistered_version_is_a_loud_refusal_not_an_unenforced_call(db):
    """The fail-loud registry proving the D10 rule: requesting a version with no registered body
    RAISES at dispatch instead of sending `output_schema=None` and failing repair downstream."""
    from featuregen.documents.registry import DocumentSchemaRegistry
    from featuregen.overlay.upload.enrich_llm import SchemaUnregisteredError, _require_schema

    with pytest.raises(SchemaUnregisteredError) as exc:
        _require_schema(db, DocumentSchemaRegistry(db), "feature_ideas", 99)
    assert "feature_ideas" in str(exc.value) and "v99" in str(exc.value)


# ── the v4 payload ──────────────────────────────────────────────────────────────────────────────


def test_v4_carries_the_shared_bundle_keys(db, v4):
    _bank_graph(db)
    payload = _column(db, "public.payments.tran_amt")
    for key in ("object_ref", "table", "column", "concept", "definition", "concept_path",
                "semantic_authority", "missing_context", "additivity", "currency", "unit",
                "data_type", "declared_type", "is_grain", "is_as_of"):
        assert key in payload, key
    assert payload["concept_path"][0] == "monetary_flow"
    # The governed|hint wrapper still means operational influence and nothing else.
    assert payload["additivity"] == {"value": "additive", "authority": "hint"}


def test_the_menu_ref_stays_the_public_flattened_graph_ref(db, v4):
    """The bundle deliberately re-keys by the SCHEMA-PRESERVING logical ref; grounding matches the
    CANDIDATE set. Emitting the logical form would make every ref the model faithfully copied back
    ungrounded — the whole menu would generate nothing, silently, with the flag on."""
    _bank_graph(db)
    payload = _column(db, "public.payments.tran_amt")
    assert payload["object_ref"] == "public.payments.tran_amt"
    assert "::" not in payload["object_ref"]
    assert (payload["table"], payload["column"]) == ("payments", "tran_amt")


def test_llm_proposed_rides_the_d2_triple_never_the_governed_hint_wrapper(db, v4):
    _bank_graph(db)
    payload = _column(db, "public.payments.tran_amt")
    # The D2 axes ON THE WIRE (D10): (producer, strength), never the derived display label.
    assert payload["semantic_authority"]["concept"] == "llm/proposed"
    assert "llm_proposed" not in json.dumps(payload)
    # And the fact wrapper is untouched: `authority` is still governed|hint.
    for key in ("additivity", "unit", "currency", "data_type", "is_grain", "is_as_of"):
        assert payload[key]["authority"] in ("governed", "hint")


def test_missing_context_codes_are_the_closed_vocabulary(db, v4):
    from featuregen.overlay.upload.semantic_context import MISSING_CONTEXT_CODES

    _bank_graph(db)
    payload = _column(db, "public.payments.tran_amt")
    assert payload["missing_context"]
    assert set(payload["missing_context"]) <= MISSING_CONTEXT_CODES


def test_empty_values_are_omitted_rather_than_sent_as_nulls(db, v4):
    _bank_graph(db)
    payload = _column(db, "public.customer.cif_id")
    assert None not in payload.values()
    assert "" not in payload.values()


# ── egress: every new key classified, golden ────────────────────────────────────────────────────


def test_every_v4_key_survives_the_real_egress_adapter(db, v4):
    """The golden egress test D10 requires. The adapter fails CLOSED on any unclassified key, so a
    payload that comes back whole is proof every key has an explicit classification."""
    _bank_graph(db)
    cols = _candidate_columns(db, _SRC, roles=())
    columns = [_context_column(db, c, roles=()) for c in cols]
    safe, _pii, _samples, _version = sanitize_feature_context({"columns": columns})
    assert safe is not None, "a v4 key is unclassified — egress blocked the whole menu"
    for before, after in zip(columns, safe["columns"], strict=True):
        assert set(before) == set(after)


def test_an_unclassified_key_still_blocks_the_whole_menu(db, v4):
    _bank_graph(db)
    payload = _column(db, "public.payments.tran_amt")
    payload["surprise"] = "something nobody classified"
    safe, _pii, _samples, _version = sanitize_feature_context({"columns": [payload]})
    assert safe is None


def test_the_new_collection_keys_are_bounded(db, v4):
    """Shape and length together: an unbounded list would walk past the byte budget into the
    scanner. The classification refuses it rather than trusting the producer."""
    _bank_graph(db)
    payload = _column(db, "public.payments.tran_amt")
    payload["concept_path"] = ["x"] * 200
    assert sanitize_feature_context({"columns": [payload]})[0] is None

    payload = _column(db, "public.payments.tran_amt")
    payload["identifier_namespace"] = {"scheme": "bic", "surprise": "no"}
    assert sanitize_feature_context({"columns": [payload]})[0] is None

    payload = _column(db, "public.payments.tran_amt")
    payload["relationships"] = [{"relationship_ref": "r", "invented": "x"}]
    assert sanitize_feature_context({"columns": [payload]})[0] is None


# ── validator invariants survive the richer context ─────────────────────────────────────────────


def _recommend(db, features):
    client = FakeLLM(script={"overlay.feature.recommend": FakeResponse(
        output={"features": features})})
    return recommend_features(db, "predict outflow", client, catalog_source=_SRC)


def test_a_bic_never_becomes_a_numeric_measure(db, v4):
    """Richer context must not soften the gauntlet: a declared varchar rejects a numeric op with
    NON_NUMERIC, whatever the model was shown."""
    from featuregen.overlay.upload.feature_assist import recommend_features_report

    client = FakeLLM(script={"overlay.feature.recommend": FakeResponse(output={"features": [
        {"name": "bic_sum_30d", "description": "sum of counterparty bic",
         "derives_from": ["public.payments.counter_party_bic"], "aggregation": "sum_30d",
         "grain_table": "customer"}]})})
    _bank_graph(db)
    report = recommend_features_report(db, "predict outflow", client, catalog_source=_SRC)
    assert report.ideas == []
    assert any(a["code"] == RejectCode.NON_NUMERIC for a in report.rejections), report.rejections


def test_a_mixed_currency_aggregation_is_refused(db, v4):
    from featuregen.overlay.upload.feature_assist import recommend_features_report

    from featuregen.overlay.upload.graph import add_column_row

    _bank_graph(db)
    # A second monetary column in the SAME table, declared in a DIFFERENT currency. Summing the
    # two is arithmetic on incommensurable quantities and is silently wrong, not merely unproven.
    add_column_row(db, _SRC, CanonicalRow(_SRC, "payments", "fee_amt", "numeric",
                                          currency="USD", additivity="additive",
                                          definition="Fee charged on the transaction."))
    client = FakeLLM(script={"overlay.feature.recommend": FakeResponse(output={"features": [
        {"name": "mixed_sum_30d", "description": "sums two currencies",
         "derives_from": ["public.payments.tran_amt", "public.payments.fee_amt"],
         "aggregation": "sum_30d", "grain_table": "customer"}]})})
    report = recommend_features_report(db, "predict outflow", client, catalog_source=_SRC)
    assert report.ideas == []
    assert any(a["code"] == RejectCode.MIXED_CURRENCY for a in report.rejections), (
        report.rejections)


def test_an_amount_is_never_a_join_key(db, v4):
    """A join path is DETERMINISTIC — found from declared join edges — so a monetary amount cannot
    become one however the model describes the feature. `tran_amt` has no join edge; the only path
    from payments to customer runs through `cust_num`."""
    from featuregen.overlay.upload.join_path import find_join_path

    _bank_graph(db)
    steps = find_join_path(db, _SRC, "customer", "payments")
    assert steps, "the deterministic join path must exist for this fixture"
    refs = {s.from_ref for s in steps} | {s.to_ref for s in steps}
    assert "public.payments.tran_amt" not in refs
    assert "public.payments.cust_num" in refs


# ── the worked acceptance (FakeLLM prompt shape) ────────────────────────────────────────────────


class _CapturingLLM:
    """Records every request it is asked to serve, then delegates to a real FakeLLM — so the
    responses go through the same LLMResult path production does and only the INPUTS are under
    inspection. Prompt SHAPE is the assertion; no live model is involved."""

    def __init__(self, output: dict) -> None:
        self.requests: list = []
        self._inner = FakeLLM(script={"overlay.feature.recommend": FakeResponse(output=output)})

    def call(self, request):
        self.requests.append(request)
        return self._inner.call(request)


def test_worked_acceptance_total_outgoing_counterparty_amount_by_customer_30_days(db, v4):
    """The plan's worked request, as a PROMPT-SHAPE assertion (a live model is out of scope here).

    The context that egresses must nominate: the CUSTOMER grain, the TIME anchor, the monetary
    MEASURE and its CURRENCY — and must present the counterparty BIC as an ordinary dimension
    column, never as the customer join key."""
    _bank_graph(db)
    client = _CapturingLLM({"features": [
        {"name": "outgoing_amount_30d", "description": "total outgoing amount",
         "derives_from": ["public.payments.tran_amt"], "aggregation": "sum_30d",
         "grain_table": "customer"}]})
    ideas = recommend_features(
        db, "total outgoing counterparty amount by customer for the last 30 days", client,
        catalog_source=_SRC)
    assert [i.name for i in ideas] == ["outgoing_amount_30d"]

    request = next(r for r in client.requests if r.task == "overlay.feature.recommend")
    by_ref = {c["object_ref"]: c for c in _menu_columns_of(request)}

    # GRAIN: the customer identifier, governed-confirmed, is in the context and marked as grain.
    grain = by_ref["public.customer.cif_id"]
    assert grain["is_grain"] == {"value": "true", "authority": "governed"}
    # TIME: the posting date is the governed as-of anchor.
    anchor = by_ref["public.payments.pstd_date"]
    assert anchor["is_as_of"] == {"value": "true", "authority": "governed"}
    # MEASURE + CURRENCY: the amount arrives with its additivity, unit and currency.
    measure = by_ref["public.payments.tran_amt"]
    assert measure["currency"]["value"] == "AED"
    assert measure["additivity"]["value"] == "additive"
    assert measure["concept"] == "monetary_flow"
    # …and its concept is an AI PROPOSAL, legibly so.
    assert measure["semantic_authority"]["concept"] == "llm/proposed"

    # BIC: present as an ordinary dimension column — NOT grain, NOT as-of, and not carrying any
    # claim that it identifies the customer.
    bic = by_ref["public.payments.counter_party_bic"]
    assert bic["is_grain"]["value"] in (None, "false")
    assert bic["is_as_of"]["value"] in (None, "false")
    assert bic.get("entity") in (None, {"value": None, "authority": "hint"})
    # It is a groupable dimension: it carries meaning (its concept and definition), no measure
    # annotation, and no claim to be an identifier of the CUSTOMER — the join-key confusion the
    # acceptance names. Its own namespace, if any, is the BIC scheme, never the customer's.
    assert bic["concept"] == "bank_identifier_code"
    assert bic.get("unit", {}).get("value") is None
    assert bic.get("currency", {}).get("value") is None
    namespace = bic.get("identifier_namespace") or {}
    assert namespace.get("scheme") != by_ref["public.customer.cif_id"].get(
        "identifier_namespace", {}).get("scheme") or namespace == {}

    # The ONE table_context block per table, carrying the grain and time anchors — a table profile,
    # not a per-column repetition.
    blocks = _table_context_of(request)
    payments = next(b for b in blocks if b["table"] == "payments")
    assert payments["as_of_column"] == "pstd_date"
    customer = next(b for b in blocks if b["table"] == "customer")
    assert customer["grain_columns"] == ["cif_id"]


def _metadata_of(request) -> dict:
    """The catalog METADATA block of a captured request (never the user turn)."""
    for value in request.inputs.values():
        if isinstance(value, dict) and isinstance(value.get("columns"), list):
            return value
    raise AssertionError(f"no column menu on the request inputs: {sorted(request.inputs)}")


def _menu_columns_of(request) -> list[dict]:
    return [c for c in _metadata_of(request)["columns"] if isinstance(c, dict)]


def _table_context_of(request) -> list[dict]:
    return [b for b in _metadata_of(request).get("table_context", []) if isinstance(b, dict)]
