"""Freezing and verifying the V1 authoring configuration — the V1 half, moved out.

`frozen_configuration.py` was never a V1 module. It holds the ENVELOPE both generations use — the
provider contract, the drift error, the JSON round-trip — plus V2's own freeze and verify. Only
these four functions are V1's, and they were the sole reason that module imported `AggregateFunction`,
`FinalOperation`, `WindowBasis` and `validate_semantics`: `_operation_grammar_material` hashes the V1
grammar's vocabulary into the frozen envelope.

Moving them out leaves `frozen_configuration.py` importing no V1 language at all, which is what lets
four live modules — `formula.author`, `replay_authoring_v2`, `recipe_formula_shadow` and
`recipe_formula_worker` — stop reaching into a V1-named module for the envelope and for
`freeze_current_configuration_v2`.

**These four are NOT dead, and that is why they moved rather than went.** `freeze_current_configuration`
has zero production callers since the shadow's v1 branch was removed, but `verify_frozen_configuration`
is still called by `replay_authoring`, which stays alive because `materialize.resolve` needs
`_restore_terminal_result`. Replay of a historical v1 run re-verifies the envelope it was sealed
under — so this is deletable only once that chain is migrated, not before.

**Moved verbatim.** The material these build is hashed into `frozen_configuration_hash`, and stored
work items were sealed against exactly these bytes; a work item whose envelope no longer verifies
cannot be dispatched.
"""
from __future__ import annotations

import inspect
from collections.abc import Mapping
from typing import Any

from featuregen.formula.author import AUTHOR_PROMPT_VERSION
from featuregen.formula.authoring_result_leaves import (
    DISPOSITION_POLICY_VERSION,
)
from featuregen.formula.capability import CAPABILITY_POLICY_VERSION, classify_formula_capability
from featuregen.formula.critic import (
    CRITIC_INSTRUCTION,
    CRITIC_POLICY_VERSION,
    CRITIC_PROMPT_ID,
    CRITIC_SCHEMA,
    CRITIC_SCHEMA_ID,
    CRITIC_SCHEMA_VERSION,
)
from featuregen.formula.frozen_configuration import (
    FROZEN_CONFIGURATION_POLICY_VERSION,
    ConfigurationDrifted,
    FrozenAuthorCriticConfigurationV1,
    _canonical_bytes,
    _enum_values,
    _hash_bytes,
    _tool_registry_material,
    freeze_provider_contract,
)
from featuregen.formula.output_authority import resolve_formula_output_policy
from featuregen.formula.result import derive_disposition
from featuregen.formula.schema import (
    CANONICALIZATION_VERSION,
    FORMULA_SCHEMA_VERSION,
    OPERATION_GRAMMAR_VERSION,
    OUTPUT_POLICY_VERSION,
    AggregateFunction,
    FinalOperation,
    WindowBasis,
    validate_semantics,
)
from featuregen.formula.schema_leaves import AdditivityClass, WindowUnit
from featuregen.formula.turns import (
    AUTHOR_TURN_SCHEMA_ID,
    AUTHOR_TURN_SCHEMA_VERSION,
    AUTHOR_TURN_V1_SCHEMA,
)

__all__ = ["freeze_current_configuration", "verify_frozen_configuration"]




def _operation_grammar_material() -> dict:
    return {
        "version": OPERATION_GRAMMAR_VERSION,
        "aggregate_functions": _enum_values(AggregateFunction),
        "final_operations": _enum_values(FinalOperation),
        "window_basis": _enum_values(WindowBasis),
        "window_units": _enum_values(WindowUnit),
        "additivity": _enum_values(AdditivityClass),
        "semantic_validator_sha256": _hash_bytes(
            inspect.getsource(validate_semantics).encode("utf-8")),
        "capability_classifier_sha256": _hash_bytes(
            inspect.getsource(classify_formula_capability).encode("utf-8")),
        "output_authority_sha256": _hash_bytes(
            inspect.getsource(resolve_formula_output_policy).encode("utf-8")),
    }


def freeze_current_configuration(
    *,
    generation_settings: Mapping[str, Any],
    author_instruction: str,
    author_prompt_id: str,
    author_prompt_version: int = AUTHOR_PROMPT_VERSION,
) -> FrozenAuthorCriticConfigurationV1:
    """Freeze every byte/policy that can change authoring output under stable labels."""
    author = freeze_provider_contract(
        role="author",
        generation_settings=generation_settings,
        prompt_id=author_prompt_id,
        prompt_version=author_prompt_version,
        instruction=author_instruction,
        output_schema_id=AUTHOR_TURN_SCHEMA_ID,
        output_schema_version=AUTHOR_TURN_SCHEMA_VERSION,
        output_schema=AUTHOR_TURN_V1_SCHEMA,
    )
    critic = freeze_provider_contract(
        role="critic",
        generation_settings=generation_settings,
        prompt_id=CRITIC_PROMPT_ID,
        prompt_version=1,
        instruction=CRITIC_INSTRUCTION,
        output_schema_id=CRITIC_SCHEMA_ID,
        output_schema_version=CRITIC_SCHEMA_VERSION,
        output_schema=CRITIC_SCHEMA,
    )
    tool_registry_hash = _hash_bytes(_canonical_bytes(_tool_registry_material()))
    operation_grammar_hash = _hash_bytes(_canonical_bytes(_operation_grammar_material()))
    critic_policy_hash = _hash_bytes(_canonical_bytes({
        "version": CRITIC_POLICY_VERSION,
        "schema_hash": critic.schema_content_hash,
        "prompt_hash": critic.prompt_content_hash,
    }))
    disposition_policy_hash = _hash_bytes(_canonical_bytes({
        "version": DISPOSITION_POLICY_VERSION,
        "fold_sha256": _hash_bytes(
            inspect.getsource(derive_disposition).encode("utf-8")),
    }))
    version_vector = {
        "formula_schema": FORMULA_SCHEMA_VERSION,
        "operation_grammar": OPERATION_GRAMMAR_VERSION,
        "output_policy": OUTPUT_POLICY_VERSION,
        "canonicalization": CANONICALIZATION_VERSION,
        "capability_policy": CAPABILITY_POLICY_VERSION,
        "critic_policy": CRITIC_POLICY_VERSION,
        "disposition_policy": DISPOSITION_POLICY_VERSION,
    }
    envelope = {
        "author_contract_hash": author.contract_hash,
        "critic_contract_hash": critic.contract_hash,
        "tool_registry_hash": tool_registry_hash,
        "operation_grammar_hash": operation_grammar_hash,
        "critic_policy_hash": critic_policy_hash,
        "disposition_policy_hash": disposition_policy_hash,
        "version_vector": version_vector,
        "configuration_policy_version": FROZEN_CONFIGURATION_POLICY_VERSION,
    }
    return FrozenAuthorCriticConfigurationV1(
        author=author,
        critic=critic,
        tool_registry_hash=tool_registry_hash,
        operation_grammar_hash=operation_grammar_hash,
        critic_policy_hash=critic_policy_hash,
        disposition_policy_hash=disposition_policy_hash,
        version_vector_json=_canonical_bytes(version_vector).decode("utf-8"),
        configuration_policy_version=FROZEN_CONFIGURATION_POLICY_VERSION,
        configuration_hash=_hash_bytes(_canonical_bytes(envelope)),
    )


def verify_frozen_configuration(
    frozen: FrozenAuthorCriticConfigurationV1,
    *,
    generation_settings: Mapping[str, Any],
    author_instruction: str,
    author_prompt_id: str,
    author_prompt_version: int = AUTHOR_PROMPT_VERSION,
) -> None:
    current = freeze_current_configuration(
        generation_settings=generation_settings,
        author_instruction=author_instruction,
        author_prompt_id=author_prompt_id,
        author_prompt_version=author_prompt_version,
    )
    if current.configuration_hash != frozen.configuration_hash:
        raise ConfigurationDrifted(
            "author/critic configuration changed after observation")
