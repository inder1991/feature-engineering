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
    """Flag on, no override. Still named `v4` after the v5 bump (Task 6c) because v5 is the SAME
    INPUT contract — v5 changed only what comes BACK — and this file is about the payload."""
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


def test_flag_on_defaults_to_the_newest_contract(db, v4):
    """v5 since Task 6c — the same v4 INPUT contract, with `grounding` on the way back."""
    assert _feature_schema_version() == 5


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
        assert _feature_schema_version() == 5, raw


# ── D10: registration before a version may be requested ─────────────────────────────────────────


def test_every_version_the_ladder_can_return_is_registered(db, v4, monkeypatch):
    from featuregen.overlay.upload.enrich_llm import _SCHEMAS

    ids = ("feature_ideas", "feature_recipe", "leakage", "feature_set_rec")
    # Read from the ladder itself, never restated: a version added to `_SELECTABLE_CONTEXT_VERSIONS`
    # without a registered body must fail HERE rather than being invisible to a stale literal.
    for version in (1, *fa._SELECTABLE_CONTEXT_VERSIONS):
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


# ── the D13.1/D13.2 classification axes (Task 6) ────────────────────────────────────────────────


def _with_classification_axes(db):
    """Populate the migration-1051 COLUMN axes on the measure and the two TABLE-shape axes on its
    table — the state a real enriched catalog is in and the fixture is not."""
    db.execute(
        "UPDATE graph_node SET sub_domain = %s, bian_path = %s, process_path = %s "
        "WHERE catalog_source = %s AND lower(object_ref) = %s",
        ("Sanctions Screening", "BIAN>Party>Reference", "Onboarding>KYC",
         _SRC, "public.payments.tran_amt"))
    db.execute(
        "UPDATE graph_node SET table_role = %s, event_or_snapshot = %s "
        "WHERE catalog_source = %s AND kind = 'table' AND table_name = %s",
        ("fact", "event", _SRC, "payments"))


_AXES = ("sub_domain", "bian_path", "process_path", "table_role", "event_or_snapshot")


def test_feature_payload_carries_the_classification_axes(db, v4):
    """The second narrowing. The bundle has carried these since Task 5; the payload dropped them,
    so the generator saw `domain` and no refinement, and no answer at all to "is this table an
    event log or a snapshot?" — the question that decides whether a windowed count is arithmetic
    or nonsense."""
    _bank_graph(db)
    _with_classification_axes(db)
    payload = _column(db, "public.payments.tran_amt")
    assert payload["sub_domain"] == "Sanctions Screening"
    assert payload["bian_path"] == "BIAN>Party>Reference"
    assert payload["process_path"] == "Onboarding>KYC"
    assert payload["table_role"] == "fact"
    assert payload["event_or_snapshot"] == "event"


def test_the_classification_axes_are_egress_classified(db, v4, monkeypatch):
    """The landmine. None of the five appears in any `_FEATURE_COLUMN_*` classifier by default and
    the adapter's terminal branch returns None, so emitting one unclassified refuses the WHOLE
    column payload — not the field. The loop is the pin: remove any single axis from the list it
    was classified into and the column is refused again."""
    from featuregen.overlay.upload import enrich_llm

    _bank_graph(db)
    _with_classification_axes(db)
    payload = _column(db, "public.payments.tran_amt")
    assert set(_AXES) <= set(payload)
    safe, _pii, _samples, _version = sanitize_feature_context({"columns": [payload]})
    assert safe is not None, "an axis is unclassified — egress blocked the whole column"
    assert set(safe["columns"][0]) == set(payload)

    # Each axis is pinned against the list it ACTUALLY belongs to, so this fails both if someone
    # drops an axis and if someone silently re-grades one (identity <-> prose): removing
    # `bian_path` from the identity list must NOT refuse the column, because it is not there.
    # `sub_domain` moved identity -> prose at the final branch review (2026-08-09): it is the
    # model's FREE-TEXT refinement of `domain`, drafted from a payload carrying uploader
    # definitions, so at identity grade one echoed PII token killed the whole call.
    for attr, axes in (("_FEATURE_COLUMN_IDENTITY_KEYS",
                        ("table_role", "event_or_snapshot")),
                       ("_FEATURE_COLUMN_PROSE_KEYS",
                        ("sub_domain", "bian_path", "process_path"))):
        original = getattr(enrich_llm, attr)
        for axis in axes:
            monkeypatch.setattr(enrich_llm, attr, original - {axis})
            assert sanitize_feature_context({"columns": [payload]})[0] is None, (attr, axis)
        monkeypatch.setattr(enrich_llm, attr, original)
    assert set(_AXES) == set(enrich_llm._FEATURE_COLUMN_IDENTITY_KEYS
                             | enrich_llm._FEATURE_COLUMN_PROSE_KEYS) & set(_AXES)


def test_the_uploader_authored_taxonomy_paths_are_pii_redacted_not_merely_bounded(db, v4):
    """The two axes the ENRICHMENT seam already grades prose (`_SCALAR_PROSE_META_KEYS`) are graded
    prose HERE too, so the two seams agree — the stated principle of this module's egress header.

    Why it is not enough that they are redacted at ingest: `bian_path` has TWO origins. The FTR
    adapter redacts (`ftr_adapter._redact`), but the GENERIC `read_glossary` — a live upload branch
    (`api/routes/uploads.py`) — builds the same `GlossaryRecord` with a bare `_cell(...)` and no
    redaction at all. Resting this seam's safety on "it was already scrubbed" would rest it on a
    chain that is already broken.

    The failure DIRECTION matters too: at identity grade an un-redacted email here would sail past
    `_structural_ok` and then trip `assert_llm_safe`, which raises `EgressViolation` and kills the
    whole feature-generation call. Prose grade redacts it and the call proceeds."""
    _bank_graph(db)
    _with_classification_axes(db)
    db.execute("UPDATE graph_node SET bian_path = %s, process_path = %s "
               "WHERE catalog_source = %s AND lower(object_ref) = %s",
               ("Party > Contact > john.smith@example.com",
                "Onboarding > KYC > escalate to john.smith@example.com",
                _SRC, "public.payments.tran_amt"))
    payload = _column(db, "public.payments.tran_amt")
    assert "john.smith@example.com" in json.dumps(payload), "the fixture must actually carry PII"

    safe, pii_spans, _samples, version = sanitize_feature_context({"columns": [payload]})
    assert safe is not None, "prose grade REDACTS; it must not fail the column closed"
    assert "john.smith@example.com" not in json.dumps(safe)
    # Redacted, not dropped: the taxonomy path still reaches the generator, minus the PII.
    assert "Party" in safe["columns"][0]["bian_path"]
    assert "KYC" in safe["columns"][0]["process_path"]
    # …and the redaction is REPORTED, so the audit records what left and under which version.
    assert version is not None
    assert {s["key"] for s in pii_spans} >= {"columns[0].bian_path", "columns[0].process_path"}


def test_domain_is_prose_graded_like_the_taxonomy_paths(db, v4):
    """`domain` is UPLOADER-TYPED and the chain that types it never redacts: a CSV satisfying
    `is_glossary_csv` but not `is_glossary_mapping` routes to `read_glossary`
    (`api/routes/uploads.py`), whose `domain=_cell(fmap, raw, "domain")` (`glossary_reader.py`) has
    no redaction step at all; `ingest` persists it as SOURCE evidence, `field_resolution` projects
    it onto `graph_node.domain`, and `for_feature_generation` emits it. At identity grade the ONLY
    check it met on this seam was `_structural_ok` — a type-and-length test with no redactor.

    Move `domain` back into `_FEATURE_COLUMN_IDENTITY_KEYS` and this fails on the second assertion:
    the raw address survives into the payload the provider receives.

    What this deliberately does NOT claim: prose grade adds no DETECTION. `redact_free_text`'s
    `_scan` and `assert_llm_safe`'s `_first_pii` walk the SAME `_PII_PATTERNS`, and no
    `IntentRedactor` is registered anywhere in `src/`, so both seams run `DefaultIntentRedactor`'s
    pattern set. A personal NAME here still egresses. What changes is the FAILURE DIRECTION
    (redact-and-proceed instead of an `EgressViolation` that kills the whole generation call), the
    AUDIT (a `pii_spans` entry keyed to the nested path, with a version), and the DI seam a
    registered NER redactor would later plug into."""
    from featuregen.overlay.upload import enrich_llm

    assert "domain" in enrich_llm._FEATURE_COLUMN_PROSE_KEYS
    assert "domain" not in enrich_llm._FEATURE_COLUMN_IDENTITY_KEYS

    _bank_graph(db)
    db.execute("UPDATE graph_node SET domain = %s "
               "WHERE catalog_source = %s AND lower(object_ref) = %s",
               ("Payments — escalate to john.smith@example.com", _SRC,
                "public.payments.tran_amt"))
    payload = _column(db, "public.payments.tran_amt")
    assert "john.smith@example.com" in payload["domain"], "the fixture must actually carry PII"

    safe, pii_spans, _samples, version = sanitize_feature_context({"columns": [payload]})
    assert safe is not None, "prose grade REDACTS; it must not fail the column closed"
    assert "john.smith@example.com" not in json.dumps(safe)
    # Redacted, not dropped: the domain label still reaches the generator, minus the PII.
    assert "Payments" in safe["columns"][0]["domain"]
    assert version is not None
    assert {s["key"] for s in pii_spans} >= {"columns[0].domain"}


def test_an_axis_longer_than_the_structural_bound_refuses_rather_than_truncates(db, v4):
    """Identity grade is an ACCEPTANCE gate, not a formatter: an over-long path is excluded, never
    silently clipped into a different taxonomy path."""
    from featuregen.overlay.upload.enrich_llm import _FEATURE_STRUCTURAL_MAX_LEN

    _bank_graph(db)
    _with_classification_axes(db)
    payload = _column(db, "public.payments.tran_amt")
    payload["bian_path"] = "x" * (_FEATURE_STRUCTURAL_MAX_LEN + 1)
    assert sanitize_feature_context({"columns": [payload]})[0] is None


def test_an_ai_proposed_axis_is_legible_as_a_proposal(db, v4):
    """No unconfirmed value without its authority. `sub_domain` rides `resolved_semantics` so the
    column loop already covers it; `table_role` lives on `table_context`, which that loop never
    walks — an LLM-proposed table shape would otherwise egress with nothing beside it saying who
    said so."""
    _bank_graph(db)
    _with_classification_axes(db)
    for logical_ref, field_name, value in (
            (f"{_SRC}::public.payments.tran_amt", "sub_domain", "Sanctions Screening"),
            (f"{_SRC}::public.payments", "table_role", "fact")):
        record_field_evidence(
            db, logical_ref=logical_ref, field_name=field_name, proposed_value=value,
            producer="llm", strength="proposed", producer_ref="pass-a",
            source_snapshot_id="snap",
            input_hash=field_input_hash(logical_ref=logical_ref, field_name=field_name,
                                        material=f"{value}:llm"))
    payload = _column(db, "public.payments.tran_amt")
    assert payload["semantic_authority"]["sub_domain"] == "llm/proposed"
    assert payload["semantic_authority"]["table_role"] == "llm/proposed"
    # The table's own `definition`/`domain`/`ai_summary`/`semantic_terms` share their names with the
    # COLUMN's fields; folding those in would relabel the column's own values with the table's
    # authority. Only the two axes the payload emits are folded in.
    assert payload["semantic_authority"].get("definition") != "llm/proposed"


# ── the LLM's proposed measure annotation (Task 6 step 4) ───────────────────────────────────────


def _fee_amt_with_llm_measure(db):
    """A monetary column the source declared NO unit or currency for, plus the LLM's `llm/proposed`
    answer to both. `_MEASURE_ANNOTATION` bars the LLM from BOTH the display and operational rules,
    so `graph_node.unit`/`currency` stay NULL and the proposal can never win resolution."""
    from featuregen.overlay.upload.graph import add_column_row

    add_column_row(db, _SRC, CanonicalRow(_SRC, "payments", "fee_amt", "numeric",
                                          additivity="additive",
                                          definition="Fee charged on the transaction."))
    ref = f"{_SRC}::public.payments.fee_amt"
    for field_name, value in (("unit", "currency"), ("currency", "AED")):
        record_field_evidence(
            db, logical_ref=ref, field_name=field_name, proposed_value=value,
            producer="llm", strength="proposed", producer_ref="pass-a",
            source_snapshot_id="snap",
            input_hash=field_input_hash(logical_ref=ref, field_name=field_name,
                                        material=f"{value}:llm"))


def test_the_payload_carries_a_proposal_it_used_to_only_announce(db, v4):
    """Before Task 6 this payload said `unit: {"value": null}` while `semantic_authority` said
    `unit: "llm/proposed"` — it announced a proposal it did not include. The proposal now rides a
    SEPARATE key: `value` still means "operationally resolved", so nothing reading `.value` can
    mistake a guess for a governed fact."""
    _bank_graph(db)
    _fee_amt_with_llm_measure(db)
    payload = _column(db, "public.payments.fee_amt")
    assert payload["semantic_authority"]["unit"] == "llm/proposed"
    assert payload["semantic_authority"]["currency"] == "llm/proposed"
    assert payload["unit"] == {"value": None, "authority": "hint", "proposed_value": "currency"}
    assert payload["currency"] == {"value": None, "authority": "hint", "proposed_value": "AED"}
    # A fact the operational read model DID answer carries no proposal key at all — the wrapper
    # every existing consumer reads is byte-identical to what it was.
    assert payload["additivity"] == {"value": "additive", "authority": "hint"}


def test_a_fact_that_DID_resolve_never_carries_the_proposal_beside_it(db, v4):
    """The `and value is None` clause, pinned. It is the whole basis of the design claim that
    `value` still means "operationally resolved": where the operational read model HAS an answer,
    the LLM's competing answer must not ride along beside it, or the wrapper starts carrying two
    candidate values and the reader has to arbitrate.

    `additivity` resolves from `graph_node` AND has an `llm/proposed` row here proposing the
    opposite. Delete `and value is None` from `for_feature_generation` and this test fails."""
    _bank_graph(db)
    ref = f"{_SRC}::public.payments.tran_amt"
    record_field_evidence(
        db, logical_ref=ref, field_name="additivity", proposed_value="semi_additive",
        producer="llm", strength="proposed", producer_ref="pass-a", source_snapshot_id="snap",
        input_hash=field_input_hash(logical_ref=ref, field_name="additivity",
                                    material="semi_additive:llm"))
    payload = _column(db, "public.payments.tran_amt")
    # The proposal EXISTS and is legible as one…
    assert payload["semantic_authority"]["additivity"] == "llm/proposed"
    # …but the resolved fact stands alone: exactly two keys, and never the LLM's contradiction.
    assert payload["additivity"] == {"value": "additive", "authority": "hint"}
    assert "semi_additive" not in json.dumps(payload)


def test_the_proposed_value_subkey_is_egress_classified(db, v4, monkeypatch):
    """`_fact_wrapper_ok` refuses any subkey outside `_FEATURE_FACT_SUBKEYS`, so the proposal is the
    same landmine shape as the axes: unclassified, it refuses the whole column."""
    from featuregen.overlay.upload import enrich_llm

    _bank_graph(db)
    _fee_amt_with_llm_measure(db)
    payload = _column(db, "public.payments.fee_amt")
    assert sanitize_feature_context({"columns": [payload]})[0] is not None
    monkeypatch.setattr(enrich_llm, "_FEATURE_FACT_SUBKEYS",
                        enrich_llm._FEATURE_FACT_SUBKEYS - {"proposed_value"})
    assert sanitize_feature_context({"columns": [payload]})[0] is None


def test_an_over_long_proposed_value_is_refused_not_truncated(db, v4):
    from featuregen.overlay.upload.enrich_llm import _FEATURE_STRUCTURAL_MAX_LEN

    _bank_graph(db)
    _fee_amt_with_llm_measure(db)
    payload = _column(db, "public.payments.fee_amt")
    payload["unit"]["proposed_value"] = "x" * (_FEATURE_STRUCTURAL_MAX_LEN + 1)
    assert sanitize_feature_context({"columns": [payload]})[0] is None


# ── the adjudicator's JUDGEMENT signals (Task 6b) ───────────────────────────────────────────────

#: The unclear column of the worked fixture: a counterparty BIC whose `concept` is not a registry
#: member at all, which is exactly what `selection_reasons` refers for adjudication.
_ADJUDICATED = "public.payments.counter_party_bic"


def _adjudicate(db, logical_ref: str, *, alternatives=("bank_bic", "branch_id"),
                confidence_band: str = "medium", catalog_revision: str = "rev1"):
    """Store a CURRENT adjudication for `logical_ref` through the REAL Task-5 machinery — the
    audited dispatch seam, the 1039 structured-result store and the migration-1046 current pointer.
    FakeLLM plays the provider; nothing live runs.

    `unclassified` PLUS two alternatives is the motivating case the plan names: "probably a bank
    BIC, possibly a branch id" is something a generator can weigh, where a bare `unclassified` is a
    dead end.

    THE ALTERNATIVES ARE REGISTRY MEMBERS ON PURPOSE. `adjudication_from_output` keeps only
    `alt in CONCEPT_REGISTRY`, on write AND again on the re-validating read, so a test naming an
    invented concept (`bic_code`, `institution_id`) would be asserting a value this platform cannot
    produce — the assertion would fail for a reason that has nothing to do with this seam."""
    from tests.featuregen._helpers import mint_test_identity

    from featuregen.overlay.upload import semantic_adjudication as adj

    client = FakeLLM(script={adj.SEMANTIC_ADJUDICATION_TASK: FakeResponse(output={
        "selected_concept": "unclassified", "alternatives": list(alternatives),
        "confidence_band": confidence_band, "reason_codes": ["name_only_signal"],
        "missing_context": ["definition_missing"]})})
    target = adj.AdjudicationTargetV1(
        candidate=adj.AdjudicationCandidateV1(
            logical_ref=logical_ref, column_name="counter_party_bic", declared_type="varchar",
            definition="Bank identifier code of the counterparty institution.", concept=None),
        selection_reasons=("unclassified_column",))
    (result,) = adj.adjudicate_targets(
        db, client, [target], catalog_revision=catalog_revision,
        actor=mint_test_identity(subject="user:admin", role_claims=("platform-admin",)))
    assert result.adjudication is not None, result.skipped_reason
    return result


def test_the_v4_payload_carries_the_adjudication_signals(db, v4):
    """The adjudicator produces both signals on every column it reviews and, before this, neither
    reached the model: the band was stored and displayed, and the shortlist lived only on the asset
    dossier. For a generator asked to weigh what it is offered, "medium confidence, and the two
    concepts seriously considered were these" is the weighing input."""
    _bank_graph(db)
    _adjudicate(db, f"{_SRC}::{_ADJUDICATED}")
    payload = _column(db, _ADJUDICATED)
    assert payload["confidence_band"] == "medium"
    assert payload["concept_alternatives"] == ["bank_bic", "branch_id"]


def test_a_column_never_adjudicated_carries_neither_key(db, v4):
    """Absence is the honest signal — `_context_v4_column` already drops empty values, and most
    columns are CLEAR and were never referred, so this is the normal case, not a gap."""
    _bank_graph(db)
    payload = _column(db, "public.payments.tran_amt")
    assert "confidence_band" not in payload
    assert "concept_alternatives" not in payload


def test_an_adjudication_with_no_shortlist_carries_the_band_and_no_empty_list(db, v4):
    """"I considered nothing else" is not the same claim as "here is my shortlist". An empty list
    would egress as noise the scanner still walks and read as a deliberated-and-empty shortlist."""
    _bank_graph(db)
    _adjudicate(db, f"{_SRC}::{_ADJUDICATED}", alternatives=())
    payload = _column(db, _ADJUDICATED)
    assert payload["confidence_band"] == "medium"
    assert "concept_alternatives" not in payload


def test_the_adjudication_is_read_by_the_logical_ref_not_the_flattened_graph_ref(db, v4):
    """The trap that would have made this feature look implemented while delivering nothing.

    The 1046 subject pointer is keyed by the SCHEMA-PRESERVING logical ref, which the BUNDLE
    carries; `c` and the emitted payload carry the PUBLIC-FLATTENED graph ref. Here the column's
    real schema is not `public`, so the two spellings differ in more than the `source::` prefix —
    read it by anything derived from `c["object_ref"]` and the pointer lookup matches no row, the
    keys silently never appear, and every column comes back unadjudicated."""
    _bank_graph(db)
    db.execute("UPDATE graph_node SET schema_name = %s "
               "WHERE catalog_source = %s AND lower(object_ref) = %s",
               ("dpl_eib_compliance", _SRC, _ADJUDICATED))
    _adjudicate(db, f"{_SRC}::dpl_eib_compliance.payments.counter_party_bic")
    payload = _column(db, _ADJUDICATED)
    # The EMITTED ref is still the flattened one the grounding step matches against…
    assert payload["object_ref"] == _ADJUDICATED
    # …and the adjudication was nonetheless found.
    assert payload["confidence_band"] == "medium"
    assert payload["concept_alternatives"] == ["bank_bic", "branch_id"]


def test_the_band_is_advisory_and_moves_nothing_else_in_the_payload(db, v4):
    """`confidence_band` is EXPLANATION, never authority (the Task-5 contract, restated on this
    seam). Nothing here or downstream branches on it, so the same column adjudicated `low` instead
    of `high` must produce a payload that differs in that one key and nowhere else — not a different
    concept, not a different authority, not a dropped alternative."""
    _bank_graph(db)
    _adjudicate(db, f"{_SRC}::{_ADJUDICATED}", confidence_band="high")
    high = _column(db, _ADJUDICATED)
    _adjudicate(db, f"{_SRC}::{_ADJUDICATED}", confidence_band="low", catalog_revision="rev2")
    low = _column(db, _ADJUDICATED)
    assert (high["confidence_band"], low["confidence_band"]) == ("high", "low")
    assert {k: v for k, v in high.items() if k != "confidence_band"} == {
        k: v for k, v in low.items() if k != "confidence_band"}


def test_the_v3_rollback_carries_no_adjudication_signals(db, v4, monkeypatch):
    """The D8 safety valve stays a CLEAN rollback: at v3 an adjudicated column carries exactly the
    shipped v3 shape, so rolling back cannot leave a key behind that the v3 egress classification
    was never argued for."""
    _bank_graph(db)
    _adjudicate(db, f"{_SRC}::{_ADJUDICATED}")
    monkeypatch.setenv(fa.FEATURE_CONTEXT_VERSION_ENV, "3")
    assert _feature_schema_version() == 3
    payload = _column(db, _ADJUDICATED)
    assert "confidence_band" not in payload
    assert "concept_alternatives" not in payload


def test_the_adjudication_signals_are_egress_classified(db, v4, monkeypatch):
    """The landmine that has fired on every payload task here. Neither key appears in any
    `_FEATURE_COLUMN_*` classifier by default and the adapter's terminal branch returns None, so
    emitting one unclassified refuses the WHOLE column — not the field. Each is pinned against the
    list it ACTUALLY belongs to, so this fails both if someone drops one and if someone silently
    re-grades one."""
    from featuregen.overlay.upload import enrich_llm

    _bank_graph(db)
    _adjudicate(db, f"{_SRC}::{_ADJUDICATED}")
    payload = _column(db, _ADJUDICATED)
    assert {"confidence_band", "concept_alternatives"} <= set(payload)
    safe, _pii, _samples, _version = sanitize_feature_context({"columns": [payload]})
    assert safe is not None, "an adjudication key is unclassified — egress blocked the whole column"
    assert set(safe["columns"][0]) == set(payload)

    for attr, key in (("_FEATURE_COLUMN_IDENTITY_KEYS", "confidence_band"),
                      ("_FEATURE_COLUMN_TOKEN_LIST_KEYS", "concept_alternatives")):
        original = getattr(enrich_llm, attr)
        monkeypatch.setattr(enrich_llm, attr, original - {key})
        assert sanitize_feature_context({"columns": [payload]})[0] is None, (attr, key)
        monkeypatch.setattr(enrich_llm, attr, original)
    # …and neither is graded PROSE: both are PLATFORM-generated closed values (a `CONFIDENCE_BANDS`
    # member and registry concept names), never uploader-typed text, so the redactor+audit grade the
    # taxonomy paths carry would buy nothing and would misdocument the egress surface.
    assert not ({"confidence_band", "concept_alternatives"}
                & enrich_llm._FEATURE_COLUMN_PROSE_KEYS)


def test_an_over_long_band_or_alternative_is_refused_not_truncated(db, v4):
    """Both grades are ACCEPTANCE gates, not formatters — a value over the bound excludes the
    column and is audited, never clipped into a different token."""
    from featuregen.overlay.upload.enrich_llm import (
        _FEATURE_COLLECTION_MAX_ITEMS,
        _FEATURE_STRUCTURAL_MAX_LEN,
    )

    _bank_graph(db)
    _adjudicate(db, f"{_SRC}::{_ADJUDICATED}")
    payload = _column(db, _ADJUDICATED)
    over_long = dict(payload, confidence_band="x" * (_FEATURE_STRUCTURAL_MAX_LEN + 1))
    assert sanitize_feature_context({"columns": [over_long]})[0] is None
    over_wide = dict(payload,
                     concept_alternatives=["x"] * (_FEATURE_COLLECTION_MAX_ITEMS + 1))
    assert sanitize_feature_context({"columns": [over_wide]})[0] is None


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
    from featuregen.overlay.upload.enrich_llm import _FEATURE_COLLECTION_MAX_ITEMS

    _bank_graph(db)
    payload = _column(db, "public.payments.tran_amt")
    # Sized off the cap: the 2026-08-06 zero-truncation raise took it 40 -> 256, so a fixed 200-item
    # list is now ADMISSIBLE and this assertion would have inverted.
    payload["concept_path"] = ["x"] * (_FEATURE_COLLECTION_MAX_ITEMS + 1)
    assert sanitize_feature_context({"columns": [payload]})[0] is None

    payload = _column(db, "public.payments.tran_amt")
    payload["identifier_namespace"] = {"scheme": "bic", "surprise": "no"}
    assert sanitize_feature_context({"columns": [payload]})[0] is None

    payload = _column(db, "public.payments.tran_amt")
    payload["relationships"] = [{"relationship_ref": "r", "invented": "x"}]
    assert sanitize_feature_context({"columns": [payload]})[0] is None


# ── the relationship hop's DIRECTIONAL cardinality (Task 6 step 4) ──────────────────────────────


def _stored_link(db):
    """ONE governed cib<->ftr link with ONE current realization running cib.customers ->
    ftr.transactions at many_to_one. The same machinery `test_semantic_context` uses, because a
    `CanonicalRow(joins_to=..., cardinality=...)` declaration is NOT an identifier link — the
    `_bank_graph` fixture's `relationship_context` is empty and cannot exercise this."""
    from tests.featuregen.overlay.upload._bridge_fixtures import govern_bridge_fact
    from tests.featuregen.overlay.upload.test_bridge_assessment_contracts import (
        _executable_pair,
        _realization,
    )

    from featuregen.data_agent.physical import record_binding_revision
    from featuregen.overlay.projection import OverlayProjection
    from featuregen.overlay.upload.bridge_assessment import (
        IdentifierLinkAssessmentV1,
        LinkReviewStatus,
        NamespaceVerdict,
        PopulationRelation,
    )
    from featuregen.overlay.upload.bridge_realization import (
        BridgeRealizationCurrentV1,
        RealizationLifecycle,
        SafetyStatus,
    )
    from featuregen.overlay.upload.bridge_store import (
        BridgeDependencyRefV1,
        record_candidate_assessment,
        record_realization_revision,
    )
    from featuregen.projections.runner import run_projection

    left, right = _executable_pair()
    assessment = IdentifierLinkAssessmentV1(
        left_endpoint=left, right_endpoint=right,
        namespace_verdict=NamespaceVerdict.POSSIBLE,
        governed_population_relation=PopulationRelation.UNKNOWN,
        assessment_version="assessment-v1", bridge_fact_key="bridge-fact-1")
    record_candidate_assessment(db, assessment, expected_pointer_version=0)
    govern_bridge_fact(
        db, "bridge-fact-1", entity="customer", left_source="cib",
        left_ref="public.customers.customer_id", right_source="ftr",
        right_ref="public.transactions.customer_id", status="DRAFT")
    record_binding_revision(db, left.physical_binding)
    record_binding_revision(db, right.physical_binding)
    revision = _realization(left, right)          # from cib::public.customers, many_to_one
    record_realization_revision(
        db, revision,
        BridgeRealizationCurrentV1(
            revision.realization_id, revision.realization_revision_id,
            SafetyStatus.DETERMINISTICALLY_VALIDATED, LinkReviewStatus.UNREVIEWED,
            RealizationLifecycle.ACTIVE, 1),
        dependencies=(BridgeDependencyRefV1("bridge_fact", "bridge-fact-1", "head-1"),))
    build_graph(db, "cib", [CanonicalRow("cib", "customers", "customer_id", "text", is_grain=True)])
    build_graph(db, "ftr", [CanonicalRow("ftr", "transactions", "customer_id", "text")])
    run_projection(db, OverlayProjection())


def _column_of(db, source: str, object_ref: str) -> dict:
    cols = _candidate_columns(db, source, roles=())
    row = next(c for c in cols if c["object_ref"] == object_ref)
    return _context_column(db, row, roles=())


def test_the_relationship_carries_the_cardinality_of_the_hop_away_from_this_table(db, v4):
    """"Does this join fan out?" is the difference between a correct aggregate and a silently
    multiplied one — but it is a DIRECTIONAL question, and this module refuses to answer it as a
    symmetric one (`CrosswalkDirectionContextV1`: "a surface that showed one verdict for the pair
    would either hide a usable direction or imply an unusable one").

    So the key is named for its direction and reports ONE side: the hop away from the anchor's own
    table. The same link, read from both ends, gives two different honest answers."""
    _stored_link(db)
    cib = _column_of(db, "cib", "public.customers.customer_id")
    (out,) = cib["relationships"]
    assert out["outbound_cardinality"] == "many_to_one"

    ftr = _column_of(db, "ftr", "public.transactions.customer_id")
    (back,) = ftr["relationships"]
    assert back["relationship_ref"] == out["relationship_ref"], "the SAME link, other end"
    # Nobody has established what the hop away from THIS table does. None says exactly that; it
    # does not borrow the other direction's verdict, and `plan_join` will refuse the hop later.
    assert back["outbound_cardinality"] is None


def test_the_outbound_cardinality_key_is_egress_classified(db, v4, monkeypatch):
    from featuregen.overlay.upload import enrich_llm

    _stored_link(db)
    payload = _column_of(db, "cib", "public.customers.customer_id")
    assert sanitize_feature_context({"columns": [payload]})[0] is not None
    monkeypatch.setattr(
        enrich_llm, "_FEATURE_COLUMN_DICT_LIST_KEYS",
        {"relationships": enrich_llm._FEATURE_COLUMN_DICT_LIST_KEYS["relationships"]
         - {"outbound_cardinality"}})
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
