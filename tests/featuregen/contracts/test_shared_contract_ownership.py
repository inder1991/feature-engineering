"""Task 0S bullet 6 — owner/version-mismatch guards over the shared contract landscape.

These are source-level tripwires for the cross-plan one-owner rule (ledger §2, freeze
0F-4/D3/D9/D11). They fail LOUDLY the moment a competing definition lands anywhere in
``src/featuregen`` — including the moment the semantic/profile plans land their owned
symbols with a vocabulary that disagrees with this plan's frozen values.
"""
from __future__ import annotations

import importlib
import re
from pathlib import Path

_SRC = Path(__file__).resolve().parents[3] / "src" / "featuregen"

# Symbols with exactly ONE permitted defining module at this baseline (Task 0S owners).
_SINGLE_OWNER_CLASSES = {
    "EvidenceAuthorityV1": "contracts/evidence_axes.py",
    "SemanticValueV1": "contracts/evidence_axes.py",
    "AttributedLabelV1": "contracts/evidence_axes.py",
    "AttributedTextV1": "contracts/evidence_axes.py",
}

# Symbols owned by the UNLANDED semantic/profile plans (freeze D3/D9; ledger §2). This plan
# must not define them; when their owner plan lands one, the guards below start validating
# it instead of failing.
_FOREIGN_OWNED_CLASSES = ("JoinLegPinV1", "DatasetSemanticProfileV1", "SemanticContextBundleV1")

_FROZEN_RELATIONSHIP_KINDS = frozenset(
    {"direct_equality", "crosswalk", "transformed", "semantic_only"})


def _defining_files(class_name: str) -> list[Path]:
    pattern = re.compile(rf"^\s*class\s+{class_name}\b")
    return sorted(path for path in _SRC.rglob("*.py")
                  if any(pattern.match(line) for line in
                         path.read_text(encoding="utf-8").splitlines()))


def test_task0s_contracts_have_exactly_one_defining_module():
    for class_name, owner_rel in _SINGLE_OWNER_CLASSES.items():
        files = _defining_files(class_name)
        assert files == [_SRC / owner_rel], (
            f"{class_name} must be defined ONLY in {owner_rel}; found {files}")


def test_foreign_owned_contracts_are_never_defined_twice():
    for class_name in _FOREIGN_OWNED_CLASSES:
        files = _defining_files(class_name)
        assert len(files) <= 1, (
            f"{class_name} is owned by the semantic/profile plans and has competing "
            f"definitions: {files}")


def test_relationship_kind_enum_when_landed_must_match_the_frozen_vocabulary():
    """D3 handoff tripwire: the moment the shared RelationshipKind StrEnum lands anywhere in
    featuregen, its values must equal the vocabulary this plan froze — and
    ``relationship_kinds`` must then be re-pointed at it (module docstring records the step)."""
    files = _defining_files("RelationshipKind")
    assert len(files) <= 1, f"two RelationshipKind definitions: {files}"
    if not files:
        return  # not landed yet — the frozen str vocabulary applies (D3)
    module_name = "featuregen." + ".".join(
        files[0].relative_to(_SRC).with_suffix("").parts)
    landed = importlib.import_module(module_name).RelationshipKind
    assert frozenset(member.value for member in landed) == _FROZEN_RELATIONSHIP_KINDS, (
        f"RelationshipKind in {module_name} disagrees with the frozen vocabulary")


def test_no_second_jcs_hasher_owner():
    """The neutral hasher has one home; materialization DELEGATES (ledger §3). A second
    ``def jcs_sha256``/``def contract_hash_v1`` would fork contract identity."""
    for func in ("jcs_sha256", "contract_hash_v1"):
        pattern = re.compile(rf"^\s*def\s+{func}\b")
        files = sorted(path for path in _SRC.rglob("*.py")
                       if any(pattern.match(line) for line in
                              path.read_text(encoding="utf-8").splitlines()))
        assert files == [_SRC / "canonical.py"], (
            f"{func} must be defined only in featuregen/canonical.py; found {files}")
