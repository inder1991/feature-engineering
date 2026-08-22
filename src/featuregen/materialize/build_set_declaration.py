"""What a build set DECLARES — the five answers a generation cannot derive, given a type.

▲ **THE HOLE THIS CLOSES.** `build_set_revision.declaration_json` has been an untyped payload, and
the generation route filled the corresponding job fields with `None` / `{}`. That was honest — the
lane refuses such a job BY NAME rather than inventing values — but it meant a build requested
through the API could never run. Each missing field decides a published number: a defaulted spine
picks whose rows the features are computed for, a defaulted promise decides which day's data counts
as available, and defaulted operand facts let a monetary sum cross currencies.

**DECLARED ONCE PER SET, not per attempt.** Two attempts at one build set must compute the same
rows for the same population, or "re-run the build" would mean something different each time. The
per-attempt facts (engine, physical-type policy, empty values, the compiled/sealed instants) stay on
the request, where they belong.

▲ **THE IDENTITY PROJECTION IS THE LOAD-BEARING PART.** `build_set_identity` hashes the declaration,
so whatever goes into that hash decides when two people asking for the same build get the same set.
`SpineSourceDeclarationV1` carries provenance — `declared_by`, `recorded_at`, `declaration_record_id`
— and hashing those would make the SAME declaration made twice into two different build sets, one
per clock read. The spine type already solves this for its own contract hash with
:meth:`identity_payload` ("two people making the identical declaration would produce different
contract hashes and therefore different groups"), and this module uses it for the same reason.
So the STORED payload is complete (provenance included, because who declared a population is worth
keeping) while the HASHED payload is semantic only.

**ONE ENCODING, NOT TWO.** The codecs are `queue_lane`'s, which `generation_lane` already uses for
the queue payload. A second spelling of "a spine as JSON" would be a second thing to keep in step,
and the two would drift the first time a snapshot-policy variant gained a field.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from featuregen.formula.output_authority_v2 import OperandFactsV2
from featuregen.materialize.contract import AvailabilityPromiseV1, CadenceDecl
from featuregen.materialize.spine import SpineSourceDeclarationV1

#: Bumped only when the ENCODING changes, never when a caller declares something different. A
#: payload written under a version this build does not read is refused whole rather than read in
#: part — the same rule the queue payload follows.
DECLARATION_VERSION = 1


class DeclarationIncomplete(ValueError):
    """A build set was declared without something a generation cannot derive.

    A `ValueError` rather than a governed refusal code: "this is not a declaration" is not a
    governed question, and the API turns it into a 422 naming the field.
    """


@dataclass(frozen=True, slots=True)
class BuildSetDeclarationV1:
    """The five declarations, typed. Constructing one is what validates them.

    Each field is a value object that refuses its own bad input on construction — `CadenceDecl`
    canonicalizes its cutoff and refuses a period nothing can schedule, `AvailabilityPromiseV1`
    refuses a non-canonical promise. So this class deliberately adds no re-validation of their
    interiors; what it owns is that all five are PRESENT.
    """

    spine_declaration: SpineSourceDeclarationV1
    cadence: CadenceDecl
    availability_promise: AvailabilityPromiseV1
    #: Governed unit and currency per operand ref, for output authority. May be empty only if the
    #: build's features reference no operands — `compile_generation_v2` is the judge of that, and it
    #: refuses a mapping that does not describe what is being compiled.
    operand_facts: Mapping[str, OperandFactsV2]
    #: Which realization of each governed policy this build is bound to.
    policy_realization_ids: Mapping[str, str]

    def __post_init__(self) -> None:
        missing = [
            name for name in ("spine_declaration", "cadence", "availability_promise")
            if getattr(self, name) is None]
        if missing:
            raise DeclarationIncomplete(
                f"a build set declaration needs {', '.join(missing)}: each decides a published "
                f"number, and a generation cannot derive any of them")


def encode_declaration(declaration: BuildSetDeclarationV1) -> dict[str, Any]:
    """The COMPLETE payload, provenance included — what `declaration_json` stores."""
    from featuregen.materialize.queue_lane import _encode_spine, _plain

    return {
        "version": DECLARATION_VERSION,
        "spine_declaration": _encode_spine(declaration.spine_declaration),
        "cadence": _plain(declaration.cadence),
        "availability_promise": _plain(declaration.availability_promise),
        "operand_facts": {ref: _plain(facts)
                          for ref, facts in sorted(declaration.operand_facts.items())},
        "policy_realization_ids": dict(sorted(declaration.policy_realization_ids.items())),
    }


def declaration_identity(declaration: BuildSetDeclarationV1) -> dict[str, Any]:
    """The SEMANTIC payload — what makes this a different build, and nothing else.

    ▲ The spine goes in as its `identity_payload`, so `declared_by`, `declaration_reason`,
    `recorded_at` and `declaration_record_id` are excluded. Including them would mean the same
    declaration made twice was two build sets, one per clock read, and "two people asking for the
    same build get the same set" would silently stop being true.
    """
    from featuregen.materialize.queue_lane import _plain

    return {
        "version": DECLARATION_VERSION,
        "spine_declaration": declaration.spine_declaration.identity_payload(),
        "cadence": _plain(declaration.cadence),
        "availability_promise": _plain(declaration.availability_promise),
        "operand_facts": {ref: _plain(facts)
                          for ref, facts in sorted(declaration.operand_facts.items())},
        "policy_realization_ids": dict(sorted(declaration.policy_realization_ids.items())),
    }


def decode_declaration(payload: Mapping[str, Any]) -> BuildSetDeclarationV1:
    """Read a stored declaration back, or `ValueError`.

    Refused WHOLE on a version this build does not read, for the queue payload's reason: a
    declaration read from the half a reader understands is how a build silently computes the wrong
    population.
    """
    from featuregen.materialize.queue_lane import (
        _decode_cadence,
        _decode_promise,
        _decode_spine,
        _mapping,
        _text,
    )

    material = _mapping(payload, where="build set declaration")
    version = material.get("version")
    if version != DECLARATION_VERSION:
        raise ValueError(
            f"build set declaration declares version {version!r} and this build reads "
            f"{DECLARATION_VERSION}: a declaration decides which rows are published, so one that "
            f"cannot be read whole must be refused rather than read in part")

    facts_node = _mapping(material.get("operand_facts") or {}, where="declaration operand_facts")
    operand_facts = {}
    for ref, node in facts_node.items():
        fact = _mapping(node, where=f"operand facts for {ref!r}")
        operand_facts[ref] = OperandFactsV2(
            logical_type=_text(fact.get("logical_type"), where=f"{ref} logical_type"),
            unit=str(fact.get("unit") or ""),
            currency=str(fact.get("currency") or ""))

    policy_node = _mapping(material.get("policy_realization_ids") or {},
                           where="declaration policy_realization_ids")

    return BuildSetDeclarationV1(
        spine_declaration=_decode_spine(material.get("spine_declaration")),
        cadence=_decode_cadence(material.get("cadence")),
        availability_promise=_decode_promise(material.get("availability_promise")),
        operand_facts=operand_facts,
        policy_realization_ids={str(k): str(v) for k, v in policy_node.items()})
