"""Child-1 Task 10 — the INDEPENDENT, fail-closed critic (LLM-2).

Two load-bearing invariants under test:

* INDEPENDENCE — the critic's one audited call carries an independently-assembled, read-scoped
  metadata context (the intent + the proposal + the proposal's columns' governed facts RE-FETCHED
  from the catalog under the caller's roles). The author's reasoning/tool trace has no slot in the
  signature and never appears in the audited call inputs.
* FAIL-CLOSED — a malformed/unparseable critic response (or an egress-blocked/provider-failed
  call) is ``is_technical_failure=True`` with ``findings == []``. It is NEVER the clean-critic
  shape ``([], hash, False)`` — a broken critic can never clear a formula toward auto-RESOLVED.

Severity is a FIXED property of each closed finding code — the model's emitted severity is
tolerated on the wire and IGNORED. Unknown codes and duplicate (code, target) findings are DROPPED
with a recorded note, never raised, never blocking.
"""
import dataclasses
import hashlib
import inspect
import json
import logging

import pytest

from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.formula.critic import (
    CRITIC_FINDING_CODES,
    CRITIC_FINDINGS_V1_SCHEMA,
    CRITIC_INSTRUCTION,
    CRITIC_POLICY_VERSION,
    CRITIC_PROMPT_ID,
    CRITIC_TASK,
    CriticFinding,
    CriticFindingCode,
    _SEVERITY,
    build_critic_metadata,
    critic_findings_hash,
    critique,
    proposal_column_refs,
)
from featuregen.formula.parse import parse_proposal_v1
from featuregen.formula.turns import AuthoringIntent
from featuregen.intake.llm import PROVIDER_NON_RETRYABLE, FakeLLM, FakeResponse
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.column_authority import read_column_facts
from featuregen.overlay.upload.graph import build_graph

RUN = "formula-authoring-run-t10"
SOURCE = "authored"
REF_AMT = "authored::public.txns.txn_amt"
REF_DT = "authored::public.txns.txn_dt"
REF_CIF = "authored::public.txns.cif_id"
# A distinctive stand-in for catalog free text (a raw data value): if the critic context ever
# egresses it, the assertions below catch the exact string (same discipline as test_author).
CANARY = "leak-canary-1010"

_ACTOR = IdentityEnvelope(subject="formula-critic-t10", actor_kind="service",
                          authenticated=False, auth_method="internal", role_claims=())

_INTENT = AuthoringIntent(
    name="txn_amt_sum_90d",
    hypothesis="Total transaction amount over a trailing 90-day window predicts churn.",
    target_entity="customer",
    target_grain_keys=(REF_CIF,),
)


def _finding(code, operand=None, detail=None):
    return CriticFinding(code=code, severity=_SEVERITY[code], operand=operand, detail=detail)


# ---- the closed §G finding set + fixed severity ------------------------------------------------


def test_finding_codes_are_the_closed_g_set_with_fixed_severity():
    assert CRITIC_POLICY_VERSION == 1
    assert CRITIC_FINDING_CODES is CriticFindingCode      # the spec name binds the closed StrEnum
    expected = {
        "MISSING_REQUIRED_OPERAND": "blocking",
        "WRONG_SLOT_DIRECTION": "blocking",
        "FILTER_INTENT_MISMATCH": "blocking",
        "WINDOW_INTENT_MISMATCH": "blocking",
        "EXTRA_UNJUSTIFIED_OPERAND": "advisory",
        "WEAK_PROXY": "advisory",
    }
    assert {code.value for code in CriticFindingCode} == set(expected)
    # severity is a FIXED property of the code — the map is total over the closed set
    assert {code.value: _SEVERITY[code] for code in CriticFindingCode} == expected


def test_finding_is_a_frozen_slotted_dataclass():
    finding = _finding(CriticFindingCode.WEAK_PROXY, operand=REF_AMT, detail="proxy only")
    assert dataclasses.is_dataclass(CriticFinding)
    with pytest.raises(dataclasses.FrozenInstanceError):
        finding.severity = "blocking"
    assert not hasattr(finding, "__dict__")               # slots=True


# ---- the deterministic findings hash -----------------------------------------------------------


def test_findings_hash_is_order_independent_and_content_sensitive():
    a = _finding(CriticFindingCode.MISSING_REQUIRED_OPERAND, operand=REF_AMT, detail="a")
    b = _finding(CriticFindingCode.WEAK_PROXY, operand=REF_DT, detail="b")
    assert critic_findings_hash([a, b]) == critic_findings_hash([b, a])   # ordering can't matter
    assert critic_findings_hash([a]) != critic_findings_hash([b])         # content does
    assert len(critic_findings_hash([a, b])) == 64
    # well-defined over NO findings (the technical-failure hash Task 12 always receives)
    assert critic_findings_hash([]) == hashlib.sha256(b"[]").hexdigest()
