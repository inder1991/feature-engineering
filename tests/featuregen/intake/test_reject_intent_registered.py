from featuregen.commands.registry import clear_registry, get_command
from featuregen.intake.commands import register_sp2_commands, reject_intent


def test_reject_intent_is_registered():
    clear_registry()
    register_sp2_commands()
    assert get_command("reject_intent") is reject_intent
    # idempotent re-registration must not raise (register_command raises on duplicate)
    register_sp2_commands()
    assert get_command("reject_intent") is reject_intent
