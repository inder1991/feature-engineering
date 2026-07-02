from featuregen.intake import commands as ic
from featuregen.intake.banking_catalog import IntakeClassification, IntakeOutcome, classify_intent


def test_classifier_override_round_trips_and_resets():
    # R10: the LLM / redactor / catalog seams are P3/P2-owned canonical module-globals — tested in
    # their own phases. Phase 4 owns only the local classifier override of P2's `classify_intent`.
    ic.reset_intake_seams()
    assert ic._current_classifier() is classify_intent  # production default until a test pins one
    def stub(intent, *, product=None, region=None, catalog=None):
        return IntakeClassification(outcome=IntakeOutcome.CLEAR, catalog_version="bdc-2026.1")

    ic.register_intake_classifier(stub)
    assert ic._current_classifier() is stub
    ic.reset_intake_seams()
    assert ic._current_classifier() is classify_intent


def test_append_seam_is_imported_from_store_not_redefined():
    # R1: `append_fc_event` in `commands` is the SAME object as `intake.store.append_feature_contract_event`
    # (aliased import) — NOT a local redefinition.
    from featuregen.intake.store import append_feature_contract_event

    assert ic.append_fc_event is append_feature_contract_event


def test_register_sp2_commands_is_idempotent():
    ic.register_sp2_commands()
    ic.register_sp2_commands()  # must not raise on the second call
