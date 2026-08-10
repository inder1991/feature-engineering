"""BR-6 increment 1 — v2 canonicalization: deterministic, field-exhaustive, gold-pinned with the
v1 gate's own discipline (the expected hash is sha256 of the PINNED canonical text, recomputed
here with plain hashlib — a fixture whose hash was quietly refreshed while its bytes drifted
fails loudly)."""
from __future__ import annotations

import hashlib
import json
from dataclasses import fields, is_dataclass, replace
from pathlib import Path

from featuregen.formula.canonical_v2 import canonical_json_v2, proposal_content_hash_v2
from featuregen.formula.parse_v2 import parse_proposal_v2

_GOLD_V2 = Path(__file__).parent / "gold_v2"


def _ok_docs() -> list[dict]:
    docs = [json.loads(p.read_text()) for p in sorted(_GOLD_V2.glob("*.json"))]
    return [d for d in docs if d["expected"] == "ok"]


def test_every_ok_fixture_pins_canonical_bytes_and_their_hash():
    docs = _ok_docs()
    assert len(docs) == 11
    for doc in docs:
        proposal = parse_proposal_v2(doc["proposal"])
        assert canonical_json_v2(proposal) == doc["canonical_json"], doc["case_id"]
        # the v1 gold-gate rule: the pinned hash is sha256 of the pinned TEXT, plain hashlib
        assert (hashlib.sha256(doc["canonical_json"].encode()).hexdigest()
                == doc["expected_proposal_hash"]), doc["case_id"]
        assert proposal_content_hash_v2(proposal) == doc["expected_proposal_hash"]


def test_each_operation_is_its_own_identity():
    hashes = {doc["case_id"]: doc["expected_proposal_hash"] for doc in _ok_docs()}
    assert len(set(hashes.values())) == len(hashes), \
        "every operation (and every argument) over the same operands is its own identity"


def test_the_projection_is_field_exhaustive():
    """fields()-driven serialization: every field of every nested v2 dataclass appears in the
    canonical form — a field added later is hash-bearing automatically."""
    proposal = parse_proposal_v2(_ok_docs()[0]["proposal"])
    body = json.loads(canonical_json_v2(proposal))

    def walk(value, serialized):
        if is_dataclass(value) and not isinstance(value, type):
            for f in fields(value):
                assert f.name in serialized, f"{f.name} missing from canonical-v2"
                walk(getattr(value, f.name), serialized[f.name])
        elif isinstance(value, tuple):
            for item, item_s in zip(value, serialized, strict=True):
                walk(item, item_s)

    walk(proposal, body)


def test_an_edit_moves_the_hash_and_v1_identity_is_out_of_reach():
    proposal = parse_proposal_v2(_ok_docs()[0]["proposal"])
    base = proposal_content_hash_v2(proposal)
    assert proposal_content_hash_v2(replace(proposal, grain=replace(
        proposal.grain, entity="account"))) != base
    # the version triple is INSIDE the hashed body, so a v2 canonical form can never collide
    # with a v1 one — and computing v2 hashes touched no v1 machinery (the freeze manifest in
    # test_schema_v2 is the enforcement; this is the structural reason it holds)
    assert '"formula_schema_version":2' in canonical_json_v2(proposal).replace(" ", "")
