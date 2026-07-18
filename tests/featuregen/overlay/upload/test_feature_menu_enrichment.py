"""Slice 3a-iii Tasks 1+2 — `_candidate_columns` widened with feature-correctness fields +
table-node context (single scoped query), and the flag-gated ENRICHED menu that wraps every
governed/hint fact in an OperationalColumnFacts `{value, authority}` pair via read_column_facts.
The thin `_menu` projection stays byte-identical: nothing new egresses while
FEATUREGEN_FEATURE_CONTEXT is off."""
from featuregen.overlay.field_decision import FieldDecisionEventType, record_field_decision
from featuregen.overlay.field_evidence import canonical_hash
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.column_authority import logical_ref_of
from featuregen.overlay.upload.feature_assist import (
    _candidate_columns,
    _enriched_menu,
    _menu,
    feature_context_enabled,
)
from featuregen.overlay.upload.graph import build_graph


def _bank_graph(db):
    rows = [
        CanonicalRow("bank", "transactions", "amount", "numeric", definition="txn amount",
                     additivity="additive", unit="dollars", currency="USD", entity="Account"),
        CanonicalRow("bank", "transactions", "txn_date", "timestamp", as_of=True),
        CanonicalRow("bank", "accounts", "account_id", "integer", is_grain=True, entity="Account"),
    ]
    build_graph(db, "bank", rows)
    db.execute("UPDATE graph_node SET declared_type='numeric', semantic_terms='payment amount' "
               "WHERE object_ref='public.transactions.amount'")
    db.execute("UPDATE graph_node SET grain_fact_event_id='fe_grain1' "
               "WHERE object_ref='public.accounts.account_id'")
    db.execute("UPDATE graph_node SET availability_fact_event_id='fe_avail1' "
               "WHERE object_ref='public.transactions.txn_date'")
    db.execute("UPDATE graph_node SET definition='Accounts master', primary_entity='Account' "
               "WHERE kind='table' AND table_name='accounts'")


def test_candidate_columns_carries_feature_correctness_and_table_fields(db):
    _bank_graph(db)
    cols = _candidate_columns(db, "bank", roles=())
    by_ref = {c["object_ref"]: c for c in cols}
    amount = by_ref["public.transactions.amount"]
    assert amount["declared_type"] == "numeric"
    assert amount["semantic_terms"] == "payment amount"
    assert amount["additivity"] == "additive"
    assert amount["unit"] == "dollars"
    assert amount["currency"] == "USD"
    assert amount["entity"] == "Account"
    assert amount["is_grain"] is False
    acct = by_ref["public.accounts.account_id"]
    assert acct["is_grain"] is True
    assert acct["grain_fact_event_id"] == "fe_grain1"
    assert acct["table_definition"] == "Accounts master"
    assert acct["table_primary_entity"] == "Account"
    txn_date = by_ref["public.transactions.txn_date"]
    assert txn_date["is_as_of"] is True
    assert txn_date["availability_fact_event_id"] == "fe_avail1"


def test_read_scope_filter_still_excludes_restricted_columns(db):
    # The widened SELECT must not widen the authorization surface: a sensitivity-tagged column is
    # still excluded for a caller without the granting role, and visible with it.
    _bank_graph(db)
    db.execute("UPDATE graph_node SET sensitivity='restricted' "
               "WHERE object_ref='public.transactions.amount'")
    unprivileged = {c["object_ref"] for c in _candidate_columns(db, "bank", roles=())}
    assert "public.transactions.amount" not in unprivileged
    assert "public.accounts.account_id" in unprivileged
    privileged = {c["object_ref"]
                  for c in _candidate_columns(db, "bank", roles=("restricted_reader",))}
    assert "public.transactions.amount" in privileged


def test_thin_menu_unchanged_after_widening(db):
    _bank_graph(db)
    cols = _candidate_columns(db, "bank", roles=())
    menu = _menu(cols)
    # The thin menu still projects EXACTLY the five structural keys — flag-off byte-identity.
    assert all(set(m.keys()) == {"object_ref", "table", "column", "concept", "domain"}
               for m in menu)
    amount = next(m for m in menu if m["object_ref"] == "public.transactions.amount")
    assert amount == {"object_ref": "public.transactions.amount", "table": "transactions",
                      "column": "amount", "concept": None, "domain": None}


def _govern_additivity(db, logical_ref, value):
    """Record a load-bearing RESOLVED decision so is_feature_eligible(logical_ref, 'additivity')
    is True — the ONLY path to authority='governed' for a decision-governed field."""
    record_field_decision(
        db, logical_ref=logical_ref, field_name="additivity",
        event_type=FieldDecisionEventType.RESOLVED, selected_evidence_ids=[],
        evidence_set_hash=canonical_hash([]), display_value_hash=canonical_hash(value),
        load_bearing_value_hash=canonical_hash(value), conflict_status="resolved",
        reason_codes=[], field_policy_version="upload-field-policy-v1",
        resolver_version="upload-resolve-and-project-v1", actor_ref=None,
        supersedes_event_id=None)


def test_feature_context_enabled_reads_env(monkeypatch):
    # RF-C3: the ONE public flag helper. Default OFF; truthy set is {1, true, yes, on},
    # case-insensitive and whitespace-tolerant; everything else is OFF.
    monkeypatch.delenv("FEATUREGEN_FEATURE_CONTEXT", raising=False)
    assert feature_context_enabled() is False
    for raw in ("1", "true", "yes", "on", " TRUE ", "Yes", "ON"):
        monkeypatch.setenv("FEATUREGEN_FEATURE_CONTEXT", raw)
        assert feature_context_enabled() is True, raw
    for raw in ("", "0", "false", "no", "off", "enabled", "2"):
        monkeypatch.setenv("FEATUREGEN_FEATURE_CONTEXT", raw)
        assert feature_context_enabled() is False, raw


def test_enriched_menu_wraps_governed_fields_and_flag_gates(db, monkeypatch):
    _bank_graph(db)
    # Govern amount.additivity via the decision log (display value stays the flat column).
    _govern_additivity(db, logical_ref_of("bank", "public.transactions.amount"), "additive")

    monkeypatch.delenv("FEATUREGEN_FEATURE_CONTEXT", raising=False)
    assert feature_context_enabled() is False
    monkeypatch.setenv("FEATUREGEN_FEATURE_CONTEXT", "1")
    assert feature_context_enabled() is True

    cols = _candidate_columns(db, "bank", roles=())
    menu = _enriched_menu(db, cols)
    amount = next(m for m in menu if m["object_ref"] == "public.transactions.amount")
    # Structural identity stays bare; definition/semantic_terms are free-text strings
    # (sanitized at egress in Task 4).
    assert amount["table"] == "transactions"
    assert amount["definition"] == "txn amount"
    assert amount["semantic_terms"] == "payment amount"
    # Every fact field is a {value, authority} wrapper, never a bare value.
    for field in ("data_type", "declared_type", "entity", "additivity", "unit", "currency",
                  "is_grain", "is_as_of"):
        assert set(amount[field].keys()) == {"value", "authority"}, field
        assert amount[field]["authority"] in ("governed", "hint"), field
    # Decision-governed additivity carries authority='governed' with the flat display value.
    assert amount["additivity"] == {"value": "additive", "authority": "governed"}
    # Hint fields carry the flat value verbatim.
    assert amount["declared_type"] == {"value": "numeric", "authority": "hint"}
    assert amount["unit"] == {"value": "dollars", "authority": "hint"}
    assert amount["currency"] == {"value": "USD", "authority": "hint"}
    # Booleans render as strings (RF-I7); a False flag with no fact event is a hint.
    assert amount["is_grain"] == {"value": "false", "authority": "hint"}
    # Fact-event-governed grain / as-of columns (flag true AND *_fact_event_id non-null).
    acct = next(m for m in menu if m["object_ref"] == "public.accounts.account_id")
    assert acct["is_grain"] == {"value": "true", "authority": "governed"}
    txn_date = next(m for m in menu if m["object_ref"] == "public.transactions.txn_date")
    assert txn_date["is_as_of"] == {"value": "true", "authority": "governed"}


def test_flag_off_menu_content_is_byte_identical_thin_projection(db, monkeypatch):
    # With the flag OFF (unset or an explicit falsy value) the menu CONTENT is exactly the thin
    # 5-key projection — no wrappers, no enrichment keys — even though the widened candidate rows
    # carry the new fields. (The route-level serializer split lands in 3a-iv.)
    _bank_graph(db)
    for off in (None, "0"):
        if off is None:
            monkeypatch.delenv("FEATUREGEN_FEATURE_CONTEXT", raising=False)
        else:
            monkeypatch.setenv("FEATUREGEN_FEATURE_CONTEXT", off)
        assert feature_context_enabled() is False
        menu = _menu(_candidate_columns(db, "bank", roles=()))
        assert sorted(m["object_ref"] for m in menu) == [
            "public.accounts.account_id", "public.transactions.amount",
            "public.transactions.txn_date"]
        for m in menu:
            assert set(m.keys()) == {"object_ref", "table", "column", "concept", "domain"}
            assert all(not isinstance(v, dict) for v in m.values())
        amount = next(m for m in menu if m["object_ref"] == "public.transactions.amount")
        assert amount == {"object_ref": "public.transactions.amount", "table": "transactions",
                          "column": "amount", "concept": None, "domain": None}


def test_table_context_from_authorized_rows_requires_fact_event_id(db):
    import featuregen.overlay.upload.feature_assist as fa
    rows = [
        CanonicalRow("bank", "accounts", "account_id", "integer", is_grain=True),
        CanonicalRow("bank", "accounts", "region", "text", is_grain=True),  # is_grain but no fact id
        CanonicalRow("bank", "transactions", "txn_date", "timestamp", as_of=True),
    ]
    build_graph(db, "bank", rows)
    db.execute("UPDATE graph_node SET grain_fact_event_id='fe_grain1' "
               "WHERE object_ref='public.accounts.account_id'")
    db.execute("UPDATE graph_node SET availability_fact_event_id='fe_avail1' "
               "WHERE object_ref='public.transactions.txn_date'")
    db.execute("UPDATE graph_node SET definition='Accounts master', primary_entity='Account' "
               "WHERE kind='table' AND table_name='accounts'")

    cols = fa._candidate_columns(db, "bank", roles=())
    ctx = {b["table"]: b for b in fa._table_context(cols)}
    assert ctx["accounts"]["table_definition"] == "Accounts master"
    assert ctx["accounts"]["primary_entity"] == "Account"
    # Only the fact-event-linked grain column is confirmed; the file-declared one is excluded.
    assert ctx["accounts"]["grain_columns"] == ["account_id"]
    assert "as_of_column" not in ctx["accounts"]
    assert ctx["transactions"]["as_of_column"] == "txn_date"
    assert "grain_columns" not in ctx["transactions"]


def test_table_context_skips_read_scope_excluded_table(db):
    import featuregen.overlay.upload.feature_assist as fa
    rows = [
        CanonicalRow("bank", "accounts", "balance", "numeric", definition="ledger balance"),
        CanonicalRow("bank", "secrets", "ssn", "text", sensitivity="pii", definition="cust SSN"),
    ]
    build_graph(db, "bank", rows)
    cols = fa._candidate_columns(db, "bank", roles=())  # no pii role
    tables = {b["table"] for b in fa._table_context(cols)}
    assert "accounts" in tables
    assert "secrets" not in tables  # every column excluded -> no block


def test_table_context_never_leaks_excluded_column_into_partially_visible_table(db):
    # Read-scope invariant at COLUMN grain: when only SOME columns of a table are excluded, the
    # block still exists but the excluded column must not surface anywhere in it — not via
    # grain_columns and not via as_of_column, even though both carry governed fact events.
    import featuregen.overlay.upload.feature_assist as fa
    rows = [
        CanonicalRow("bank", "accounts", "account_id", "integer", is_grain=True),
        CanonicalRow("bank", "accounts", "owner_tax_id", "text", is_grain=True,
                     sensitivity="pii"),
        CanonicalRow("bank", "accounts", "opened_at", "timestamp", as_of=True,
                     sensitivity="pii"),
    ]
    build_graph(db, "bank", rows)
    db.execute("UPDATE graph_node SET grain_fact_event_id='fe_g1' "
               "WHERE object_ref IN ('public.accounts.account_id', "
               "'public.accounts.owner_tax_id')")
    db.execute("UPDATE graph_node SET availability_fact_event_id='fe_a1' "
               "WHERE object_ref='public.accounts.opened_at'")

    cols = fa._candidate_columns(db, "bank", roles=())  # no pii role
    blocks = fa._table_context(cols)
    assert [b["table"] for b in blocks] == ["accounts"]
    block = blocks[0]
    assert block["grain_columns"] == ["account_id"]
    assert "as_of_column" not in block  # the only as-of column is read-scope-excluded
    assert "owner_tax_id" not in repr(block) and "opened_at" not in repr(block)
