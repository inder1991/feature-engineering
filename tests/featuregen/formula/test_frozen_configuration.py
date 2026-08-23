from __future__ import annotations

import pytest

from featuregen.formula.author import AUTHOR_INSTRUCTION, AUTHOR_PROMPT_ID
from featuregen.formula.frozen_configuration import ConfigurationDrifted, verify_provider_contract
from featuregen.formula.frozen_configuration_v1 import (
    freeze_current_configuration,
    verify_frozen_configuration,
)
from featuregen.formula.turns import AUTHOR_TURN_V1_SCHEMA


def _frozen():
    return freeze_current_configuration(
        generation_settings={
            "provider": "anthropic",
            "model": "claude-test",
            "max_tokens": 4096,
            "thinking": False,
        },
        author_instruction=AUTHOR_INSTRUCTION,
        author_prompt_id=AUTHOR_PROMPT_ID,
    )


def test_configuration_hash_is_deterministic_and_covers_complete_contract():
    first = _frozen()
    second = _frozen()
    assert first == second
    assert first.author.prompt_content_hash
    assert first.author.schema_content_hash
    assert first.tool_registry_hash
    assert first.operation_grammar_hash
    assert first.configuration_hash


@pytest.mark.parametrize(
    ("settings", "instruction", "schema"),
    [
        (
            {"provider": "anthropic", "model": "changed", "max_tokens": 4096,
             "thinking": False},
            AUTHOR_INSTRUCTION,
            AUTHOR_TURN_V1_SCHEMA,
        ),
        (
            {"provider": "anthropic", "model": "claude-test", "max_tokens": 4096,
             "thinking": False},
            AUTHOR_INSTRUCTION + " changed",
            AUTHOR_TURN_V1_SCHEMA,
        ),
        (
            {"provider": "anthropic", "model": "claude-test", "max_tokens": 4096,
             "thinking": False},
            AUTHOR_INSTRUCTION,
            {**AUTHOR_TURN_V1_SCHEMA, "title": "changed"},
        ),
    ],
)
def test_provider_contract_verification_rejects_content_drift(settings, instruction, schema):
    frozen = _frozen()
    with pytest.raises(ConfigurationDrifted):
        verify_provider_contract(
            frozen.author,
            generation_settings=settings,
            instruction=instruction,
            output_schema=schema,
        )


def test_provider_contract_verification_accepts_exact_frozen_content():
    frozen = _frozen()
    verify_provider_contract(
        frozen.author,
        generation_settings=frozen.author.generation_settings(),
        instruction=frozen.author.instruction_utf8.decode("utf-8"),
        output_schema=frozen.author.output_schema(),
    )


def test_complete_configuration_verification_covers_policy_material(monkeypatch):
    frozen = _frozen()
    verify_frozen_configuration(
        frozen,
        generation_settings=frozen.author.generation_settings(),
        author_instruction=AUTHOR_INSTRUCTION,
        author_prompt_id=AUTHOR_PROMPT_ID,
    )
    monkeypatch.setattr(
        "featuregen.formula.frozen_configuration_v1._operation_grammar_material",
        lambda: {"changed": True},
    )
    with pytest.raises(ConfigurationDrifted):
        verify_frozen_configuration(
            frozen,
            generation_settings=frozen.author.generation_settings(),
            author_instruction=AUTHOR_INSTRUCTION,
            author_prompt_id=AUTHOR_PROMPT_ID,
        )
