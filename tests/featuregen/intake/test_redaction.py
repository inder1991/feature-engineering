from dataclasses import dataclass as _dc

import pytest

from featuregen.intake.redaction import (
    INPUT_KEY_CATALOG,
    INPUT_KEY_CLASSIFICATION,
    INPUT_KEY_INTENT,
    INPUT_KEY_REDACTION,
    INPUT_KEY_REDACTION_VERSION,
    DefaultIntentRedactor,
    EgressViolation,
    RedactionResult,
    _scan,
    assert_llm_safe,
    build_llm_inputs,
)


def test_clean_intent_passes_through_stamped():
    r = DefaultIntentRedactor().redact(
        "90-day rolling count of declined card authorizations per customer", "clean"
    )
    assert r.disposition == "ok"
    assert r.text == "90-day rolling count of declined card authorizations per customer"
    assert r.redacted_spans == ()
    assert r.redaction_version == "default-redactor@1"


def test_contains_pii_scrubs_located_spans():
    r = DefaultIntentRedactor().redact(
        "count logins for jane.doe@bank.example and SSN 123-45-6789", "contains_pii"
    )
    assert r.disposition == "ok"
    assert r.text is not None
    # the located PII is gone; placeholders are digit/at-free so a residual scan is clean
    assert "jane.doe@bank.example" not in r.text
    assert "123-45-6789" not in r.text
    assert "[REDACTED:EMAIL]" in r.text and "[REDACTED:SSN]" in r.text
    # spans record TYPE + position only (never the scrubbed value)
    kinds = {s["type"] for s in r.redacted_spans}
    assert kinds == {"EMAIL", "SSN"}
    assert all("start" in s and "end" in s and "value" not in s for s in r.redacted_spans)


# ── N2: wider deterministic PII set (phone / IBAN / bank-account / DOB / postal-address) ───────────────
@pytest.mark.parametrize("raw,label", [
    ("+1-555-123-4567", "PHONE"),
    ("(555) 123-4567", "PHONE"),
    ("+44 20 7946 0958", "PHONE"),
    ("GB82WEST12345698765432", "IBAN"),
    ("account 12345678901", "ACCOUNT"),  # 11-digit run WITH an account cue (PAN needs >=13, so ACCOUNT)
    ("DOB 1990-01-02", "DOB"),           # a date WITH a birth cue (N2a: bare dates no longer match)
    ("born 01/02/1990", "DOB"),
    ("123 Main St", "ADDRESS"),
    ("P.O. Box 12345", "ADDRESS"),
])
def test_default_redactor_scrubs_each_new_pii_class(raw, label):
    # N2: each new class is located, scrubbed, and TYPE-recorded — and the placeholder is digit/at-free
    # so the residual defense-in-depth re-scan stays clean (the redactor emits text, never fails closed).
    r = DefaultIntentRedactor().redact(f"declined-auth count; contact {raw} for details", "contains_pii")
    assert r.disposition == "ok" and r.text is not None
    assert raw not in r.text
    assert f"[REDACTED:{label}]" in r.text
    assert label in {s["type"] for s in r.redacted_spans}


@pytest.mark.parametrize("value", [
    "90-day rolling count of declined card authorizations per customer",
    "90d",
    "window 90",
    "top 100 customers over a 365 day lookback",
    "threshold 0.75 and ratio 1.5",
    "count 5 transactions; 1000000 rows",
    "declined_card_auth_count_90d",
    "card_authorizations.auth_result = 'D'",
])
def test_pii_patterns_do_not_false_match_feature_values(value):
    # N2: the wider set must never fire on legitimate feature literals (windows/counts/thresholds/a bare 90).
    assert _scan(value) == []


# ── N2a: DOB/ACCOUNT are CONTEXT-ANCHORED — bare feature dates / large numbers are NOT PII ─────────
@pytest.mark.parametrize("value", [
    "declined auths as of 2024-01-01",          # a bare ISO as-of date (no birth cue)
    "effective 2023-12-31 through 2024-06-30",  # bare effective/cohort dates
    "cohort start 2022/01/01",
    "1719446400",                               # an epoch-seconds timestamp
    "1000000000 rows",                          # a billion-row threshold
    "count events over threshold 123456789012", # a 12-digit feature threshold
])
def test_bare_dates_and_large_numbers_are_not_pii(value):
    # N2a: a bare ISO/effective date or a large feature number carries NO birth/account context, so
    # the context-anchored DOB/ACCOUNT patterns must NOT fire. FAILS before the N2a fix, where the
    # broad `\d{4}-\d\d-\d\d` / `\d{9,17}` over-matched these legitimate feature literals.
    assert _scan(value) == []


def test_egress_allows_bare_dates_and_large_numbers():
    # N2a companion on the shared egress backstop: a bare as-of date + large feature numbers in the
    # model-facing INTENT are NOT PII and must pass egress (FAILS before the fix — broad DOB/ACCOUNT).
    i = _safe_inputs()
    i[INPUT_KEY_INTENT] = "declined auths as of 2024-01-01 over 1000000000 rows (epoch 1719446400)"
    assert_llm_safe(_Req(i))  # no raise


@pytest.mark.parametrize("raw,label", [
    ("DOB: 1990-05-01", "DOB"),
    ("date of birth 05/01/1990", "DOB"),
    ("born 1990-05-01", "DOB"),
    ("account 1234567890", "ACCOUNT"),   # 10-digit run + cue (below PAN's >=13, so cleanly ACCOUNT)
    ("acct 123456789012", "ACCOUNT"),    # 12-digit run + cue
    ("a/c 987654321", "ACCOUNT"),        # 9-digit run + cue
])
def test_dob_account_with_context_still_redacted(raw, label):
    # N2a preserves fail-closed: a GENUINE birth-date / account reference WITH its cue still scans,
    # redacts, and TYPE-records — context anchoring narrows false positives, never true PII.
    assert label in {s["type"] for s in _scan(raw)}
    r = DefaultIntentRedactor().redact(f"segment customers by {raw} please", "contains_pii")
    assert r.disposition == "ok" and r.text is not None
    assert raw not in r.text and f"[REDACTED:{label}]" in r.text
    assert label in {s["type"] for s in r.redacted_spans}


def test_unscanned_fails_closed_no_text():
    r = DefaultIntentRedactor().redact("anything at all", "unscanned")
    assert r.disposition == "fail_into_clarification"
    assert r.text is None


def test_contains_pii_but_unlocatable_fails_closed():
    # SP-0 says contains_pii, but the default redactor finds no locatable span it can scrub:
    # it cannot prove the text is safe, so it fails closed rather than emit an unsafe payload.
    r = DefaultIntentRedactor().redact("the applicant's maiden name is on file", "contains_pii")
    assert r.disposition == "fail_into_clarification"
    assert r.text is None


def test_build_llm_inputs_assembles_reserved_keys():
    red = RedactionResult(
        text="count declined auths per customer",
        redaction_version="default-redactor@1",
        redacted_spans=(),
        disposition="ok",
    )
    inputs = build_llm_inputs(
        red,
        catalog_metadata={"objects": ["card_authorizations"], "columns": {"auth_result": "text"}},
        raw_input_classification="clean",
    )
    assert inputs[INPUT_KEY_INTENT] == "count declined auths per customer"
    assert inputs[INPUT_KEY_CATALOG]["objects"] == ["card_authorizations"]
    assert inputs[INPUT_KEY_CLASSIFICATION] == "clean"
    assert inputs[INPUT_KEY_REDACTION_VERSION] == "default-redactor@1"
    assert inputs[INPUT_KEY_REDACTION] == {"redacted_spans": []}


def test_build_llm_inputs_refuses_failed_redaction():
    red = RedactionResult(text=None, redaction_version="default-redactor@1",
                          redacted_spans=(), disposition="fail_into_clarification")
    with pytest.raises(EgressViolation):
        build_llm_inputs(red, catalog_metadata={}, raw_input_classification="unscanned")


def test_intent_redactor_seam_registers_and_fails_closed_when_unset():
    # R10 module-global DI seam: current_ fails closed until register_ is called; then round-trips.
    from featuregen.intake import redaction as _rmod
    from featuregen.intake.redaction import current_intent_redactor, register_intent_redactor

    _rmod._INTENT_REDACTOR = None  # ensure unset for a deterministic fail-closed assertion
    with pytest.raises(RuntimeError):
        current_intent_redactor()
    register_intent_redactor(DefaultIntentRedactor())
    assert isinstance(current_intent_redactor(), DefaultIntentRedactor)


@_dc(frozen=True)
class _Req:  # a duck-typed stand-in for LLMRequest (Task 3.3) — the guard reads .inputs only
    inputs: dict


def _safe_inputs():
    return {
        INPUT_KEY_INTENT: "count declined auths per customer",
        INPUT_KEY_CATALOG: {"objects": ["card_authorizations"], "columns": {"auth_result": "text"}},
        INPUT_KEY_CLASSIFICATION: "clean",
        INPUT_KEY_REDACTION_VERSION: "default-redactor@1",
        INPUT_KEY_REDACTION: {"redacted_spans": []},
    }


def test_egress_allows_clean_metadata_only_payload():
    assert_llm_safe(_Req(_safe_inputs()))  # no raise


def test_egress_refuses_unscanned():
    i = _safe_inputs()
    i[INPUT_KEY_CLASSIFICATION] = "unscanned"
    with pytest.raises(EgressViolation):
        assert_llm_safe(_Req(i))


def test_egress_refuses_missing_classification():
    i = _safe_inputs()
    del i[INPUT_KEY_CLASSIFICATION]
    with pytest.raises(EgressViolation):
        assert_llm_safe(_Req(i))


def test_egress_refuses_data_value_keys():
    i = _safe_inputs()
    i["column_values"] = ["D", "A", "R"]  # profiled value set — SP-1/SP-3 territory, never to the LLM
    with pytest.raises(EgressViolation):
        assert_llm_safe(_Req(i))


def test_egress_refuses_contains_pii_without_redaction_version():
    i = _safe_inputs()
    i[INPUT_KEY_CLASSIFICATION] = "contains_pii"
    del i[INPUT_KEY_REDACTION_VERSION]
    with pytest.raises(EgressViolation):
        assert_llm_safe(_Req(i))


def test_egress_refuses_unredacted_pii_in_content():
    i = _safe_inputs()
    i[INPUT_KEY_INTENT] = "count logins for jane.doe@bank.example"  # slipped past redaction
    with pytest.raises(EgressViolation):
        assert_llm_safe(_Req(i))


@pytest.mark.parametrize("raw", [
    "+1-555-123-4567", "GB82WEST12345698765432", "account 12345678901", "DOB 1990-01-02", "123 Main St",
])
def test_egress_backstop_refuses_new_pii_classes(raw):
    # N2: the wider shared set also hardens the egress backstop — un-redacted phone/IBAN/account/DOB/
    # address in the model-facing INTENT is a HARD refuse, not just the redactor's concern.
    i = _safe_inputs()
    i[INPUT_KEY_INTENT] = f"count declined auths for {raw}"
    with pytest.raises(EgressViolation):
        assert_llm_safe(_Req(i))


def test_egress_backstop_allows_feature_value_numbers():
    # N2 companion: numeric feature literals (windows/counts/thresholds) are NOT PII and pass egress.
    i = _safe_inputs()
    i[INPUT_KEY_INTENT] = "90-day rolling count over a 365 day lookback for the top 100 customers"
    assert_llm_safe(_Req(i))  # no raise


# ---- redact_free_text (finding #19): un-classified free text is scanned, never presumed clean ----


def test_redact_free_text_clean_text_is_scanned_then_passes(monkeypatch):
    from featuregen.intake import redaction as _rmod
    from featuregen.intake.redaction import redact_free_text

    monkeypatch.setattr(_rmod, "_INTENT_REDACTOR", None)   # deterministic default path
    r = redact_free_text("The posted ledger balance of the account.")
    assert r.disposition == "ok"
    assert r.text == "The posted ledger balance of the account."
    assert r.redacted_spans == ()


def test_redact_free_text_scrubs_detectable_pii(monkeypatch):
    from featuregen.intake import redaction as _rmod
    from featuregen.intake.redaction import redact_free_text

    monkeypatch.setattr(_rmod, "_INTENT_REDACTOR", None)
    r = redact_free_text("Escalate breaks to jane.doe@bank.example or SSN 123-45-6789.")
    assert r.text is not None
    assert "jane.doe@bank.example" not in r.text and "123-45-6789" not in r.text
    assert "[REDACTED:EMAIL]" in r.text and "[REDACTED:SSN]" in r.text
    assert {s["type"] for s in r.redacted_spans} == {"EMAIL", "SSN"}


def test_redact_free_text_routes_through_registered_redactor(monkeypatch):
    # The NER seam: a REGISTERED redactor supersedes the deterministic default, so name-grade
    # detection layers in via register_intent_redactor without touching the pattern set.
    from featuregen.intake import redaction as _rmod
    from featuregen.intake.redaction import redact_free_text

    class _Ner:
        def redact(self, raw_intent, raw_input_classification):
            return RedactionResult(raw_intent.replace("Jane Doe", "[REDACTED:NAME]"),
                                   "ner@test", (), "ok")

    monkeypatch.setattr(_rmod, "_INTENT_REDACTOR", _Ner())
    r = redact_free_text("Owned by Jane Doe in Finance.")
    assert r.text == "Owned by [REDACTED:NAME] in Finance."
    assert r.redaction_version == "ner@test"
