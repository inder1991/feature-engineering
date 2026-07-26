"""Content-addressed author/critic provider configuration for replay-safe formula work."""
from __future__ import annotations

import hashlib
import inspect
import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any

from featuregen.formula._jcs import dumps as jcs_dumps
from featuregen.formula.author import AUTHOR_PROMPT_VERSION
from featuregen.formula.capability import CAPABILITY_POLICY_VERSION, classify_formula_capability
from featuregen.formula.critic import (
    CRITIC_INSTRUCTION,
    CRITIC_POLICY_VERSION,
    CRITIC_PROMPT_ID,
    CRITIC_SCHEMA,
    CRITIC_SCHEMA_ID,
    CRITIC_SCHEMA_VERSION,
)
from featuregen.formula.output_authority import resolve_formula_output_policy
from featuregen.formula.result import DISPOSITION_POLICY_VERSION, derive_disposition
from featuregen.formula.schema import (
    CANONICALIZATION_VERSION,
    FORMULA_SCHEMA_VERSION,
    OPERATION_GRAMMAR_VERSION,
    OUTPUT_POLICY_VERSION,
    AdditivityClass,
    AggregateFunction,
    FinalOperation,
    WindowBasis,
    WindowUnit,
    validate_semantics,
)
from featuregen.formula.tools import TOOLS
from featuregen.formula.turns import (
    AUTHOR_TURN_SCHEMA_ID,
    AUTHOR_TURN_SCHEMA_VERSION,
    AUTHOR_TURN_V1_SCHEMA,
)

FROZEN_CONFIGURATION_POLICY_VERSION = 1


class ConfigurationDrifted(RuntimeError):
    """Current provider contract bytes do not match the frozen work item."""


def _hash_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical_bytes(value: Any) -> bytes:
    return jcs_dumps(value)


def _enum_values(enum: type[Enum]) -> list[str]:
    return [member.value for member in enum]


@dataclass(frozen=True, slots=True)
class FrozenProviderContractV1:
    role: str
    provider: str
    model: str
    generation_settings_json: str
    prompt_id: str
    prompt_version: int
    instruction_utf8: bytes
    prompt_content_hash: str
    output_schema_id: str
    output_schema_version: int
    output_schema_json: str
    schema_content_hash: str
    contract_hash: str

    def generation_settings(self) -> dict:
        import json

        return json.loads(self.generation_settings_json)

    def output_schema(self) -> dict:
        import json

        return json.loads(self.output_schema_json)


@dataclass(frozen=True, slots=True)
class FrozenAuthorCriticConfigurationV1:
    author: FrozenProviderContractV1
    critic: FrozenProviderContractV1
    tool_registry_hash: str
    operation_grammar_hash: str
    critic_policy_hash: str
    disposition_policy_hash: str
    version_vector_json: str
    configuration_policy_version: int
    configuration_hash: str


def provider_contract_json(contract: FrozenProviderContractV1) -> dict:
    return {
        "role": contract.role,
        "provider": contract.provider,
        "model": contract.model,
        "generation_settings": contract.generation_settings(),
        "prompt_id": contract.prompt_id,
        "prompt_version": contract.prompt_version,
        "instruction_utf8": contract.instruction_utf8.decode("utf-8"),
        "prompt_content_hash": contract.prompt_content_hash,
        "output_schema_id": contract.output_schema_id,
        "output_schema_version": contract.output_schema_version,
        "output_schema": contract.output_schema(),
        "schema_content_hash": contract.schema_content_hash,
        "contract_hash": contract.contract_hash,
    }


def frozen_configuration_json(
    configuration: FrozenAuthorCriticConfigurationV1,
) -> dict:
    return {
        "author": provider_contract_json(configuration.author),
        "critic": provider_contract_json(configuration.critic),
        "tool_registry_hash": configuration.tool_registry_hash,
        "operation_grammar_hash": configuration.operation_grammar_hash,
        "critic_policy_hash": configuration.critic_policy_hash,
        "disposition_policy_hash": configuration.disposition_policy_hash,
        "version_vector": json.loads(configuration.version_vector_json),
        "configuration_policy_version": configuration.configuration_policy_version,
        "configuration_hash": configuration.configuration_hash,
    }


def load_frozen_configuration_json(
    value: Mapping[str, Any],
) -> FrozenAuthorCriticConfigurationV1:
    """Reconstruct and integrity-check an immutable configuration work-item payload."""

    def _provider(role: str) -> FrozenProviderContractV1:
        raw = value.get(role)
        if not isinstance(raw, Mapping):
            raise ConfigurationDrifted(f"stored {role} provider contract is missing")
        rebuilt = freeze_provider_contract(
            role=role,
            generation_settings=raw["generation_settings"],
            prompt_id=str(raw["prompt_id"]),
            prompt_version=int(raw["prompt_version"]),
            instruction=str(raw["instruction_utf8"]),
            output_schema_id=str(raw["output_schema_id"]),
            output_schema_version=int(raw["output_schema_version"]),
            output_schema=raw["output_schema"],
        )
        for field_name in (
            "prompt_content_hash",
            "schema_content_hash",
            "contract_hash",
        ):
            if getattr(rebuilt, field_name) != raw.get(field_name):
                raise ConfigurationDrifted(
                    f"stored {role} {field_name} does not verify")
        return rebuilt

    author = _provider("author")
    critic = _provider("critic")
    version_vector = value.get("version_vector")
    if not isinstance(version_vector, Mapping):
        raise ConfigurationDrifted("stored version vector is missing")
    envelope = {
        "author_contract_hash": author.contract_hash,
        "critic_contract_hash": critic.contract_hash,
        "tool_registry_hash": value.get("tool_registry_hash"),
        "operation_grammar_hash": value.get("operation_grammar_hash"),
        "critic_policy_hash": value.get("critic_policy_hash"),
        "disposition_policy_hash": value.get("disposition_policy_hash"),
        "version_vector": dict(version_vector),
        "configuration_policy_version": value.get("configuration_policy_version"),
    }
    calculated = _hash_bytes(_canonical_bytes(envelope))
    if calculated != value.get("configuration_hash"):
        raise ConfigurationDrifted("stored configuration envelope hash does not verify")
    return FrozenAuthorCriticConfigurationV1(
        author=author,
        critic=critic,
        tool_registry_hash=str(value["tool_registry_hash"]),
        operation_grammar_hash=str(value["operation_grammar_hash"]),
        critic_policy_hash=str(value["critic_policy_hash"]),
        disposition_policy_hash=str(value["disposition_policy_hash"]),
        version_vector_json=_canonical_bytes(dict(version_vector)).decode("utf-8"),
        configuration_policy_version=int(value["configuration_policy_version"]),
        configuration_hash=calculated,
    )


def freeze_provider_contract(
    *,
    role: str,
    generation_settings: Mapping[str, Any],
    prompt_id: str,
    prompt_version: int,
    instruction: str,
    output_schema_id: str,
    output_schema_version: int,
    output_schema: Mapping[str, Any],
) -> FrozenProviderContractV1:
    if role not in {"author", "critic"}:
        raise ValueError("provider role must be author or critic")
    settings = dict(generation_settings)
    provider = settings.get("provider")
    model = settings.get("model")
    if not isinstance(provider, str) or not provider:
        raise ValueError("generation settings require a non-empty provider")
    if not isinstance(model, str) or not model:
        raise ValueError("generation settings require a non-empty model")
    settings_bytes = _canonical_bytes(settings)
    schema_bytes = _canonical_bytes(dict(output_schema))
    instruction_bytes = instruction.encode("utf-8")
    material = {
        "role": role,
        "generation_settings": settings,
        "prompt_id": prompt_id,
        "prompt_version": prompt_version,
        "instruction_sha256": _hash_bytes(instruction_bytes),
        "output_schema_id": output_schema_id,
        "output_schema_version": output_schema_version,
        "output_schema_sha256": _hash_bytes(schema_bytes),
    }
    return FrozenProviderContractV1(
        role=role,
        provider=provider,
        model=model,
        generation_settings_json=settings_bytes.decode("utf-8"),
        prompt_id=prompt_id,
        prompt_version=prompt_version,
        instruction_utf8=instruction_bytes,
        prompt_content_hash=_hash_bytes(instruction_bytes),
        output_schema_id=output_schema_id,
        output_schema_version=output_schema_version,
        output_schema_json=schema_bytes.decode("utf-8"),
        schema_content_hash=_hash_bytes(schema_bytes),
        contract_hash=_hash_bytes(_canonical_bytes(material)),
    )


def verify_provider_contract(
    frozen: FrozenProviderContractV1,
    *,
    generation_settings: Mapping[str, Any],
    instruction: str,
    output_schema: Mapping[str, Any],
) -> None:
    current = freeze_provider_contract(
        role=frozen.role,
        generation_settings=generation_settings,
        prompt_id=frozen.prompt_id,
        prompt_version=frozen.prompt_version,
        instruction=instruction,
        output_schema_id=frozen.output_schema_id,
        output_schema_version=frozen.output_schema_version,
        output_schema=output_schema,
    )
    if current.contract_hash != frozen.contract_hash:
        raise ConfigurationDrifted(
            f"{frozen.role} provider contract changed after observation")


def _tool_registry_material() -> dict:
    return {
        name: {
            "description": spec.description,
            "input_schema": spec.input_schema,
            "output_schema": spec.output_schema,
        }
        for name, spec in sorted(TOOLS.items())
    }


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
