"""The party-role axis (three-axis model, ingestion-richness Task 1 Step 4b).

WHICH PARTY a column describes relative to the row's subject — the third axis beside entity and
identifier namespace. `sender_bic` and `receiver_bic` share one concept (`bank_bic`, namespace
`swift_bic`) and differ ONLY here; folding role into concepts would breed `bank_bic_sender`,
`bank_bic_receiver`, … — the combinatorial explosion this axis exists to prevent.

ADVISORY BY CONTRACT: nothing may consume a party role as a join-candidacy or execution
predicate. It explains links and names features ("amounts received FROM counterparty banks");
it never gates. A wrong role can change no candidate set, no plan and no execution outcome —
the ingestion-richness plan pins that with a must-die mutation.

Deterministic token derivation, mirroring ``table_vocab``'s strip/lower/exact-token style: the
live role-bearing columns all carry the answer in their names. Ambiguity resolves to ``None``
(honest abstention), never a guess; an LLM fallback is deliberately deferred until a real
column needs it.
"""
from __future__ import annotations

import re
from enum import StrEnum


class PartyRole(StrEnum):
    #: The row-subject's own attribute (a customer table's `cust_swift_cd` is the CUSTOMER's bank).
    SUBJECT = "subject"
    SENDER = "sender"
    RECEIVER = "receiver"
    INTERMEDIARY = "intermediary"
    REIMBURSEMENT = "reimbursement"
    COUNTERPARTY = "counterparty"


#: Exact word tokens (split on ``_``), NOT substrings — `mandate` must never match `date`, and
#: `sender_ref_num` must match on `sender`, not on a fragment. Order = first match wins, most
#: specific first: `counter_party` is two tokens, checked as a pair before the singles.
_TOKEN_ROLES: tuple[tuple[frozenset[str], PartyRole], ...] = (
    (frozenset({"counterparty"}), PartyRole.COUNTERPARTY),
    (frozenset({"reimb", "reimbursement"}), PartyRole.REIMBURSEMENT),
    (frozenset({"intermediary"}), PartyRole.INTERMEDIARY),
    (frozenset({"sender"}), PartyRole.SENDER),
    (frozenset({"receiver", "recvr"}), PartyRole.RECEIVER),
    # `cust`/`customer` on a column marks the SUBJECT's own attribute only when no other party
    # token claimed the column first (e.g. `counter_party_cif_id` resolves COUNTERPARTY above).
    (frozenset({"cust", "customer"}), PartyRole.SUBJECT),
)

_SPLIT = re.compile(r"[^a-z0-9]+")


def normalize_party_role(column_name: object) -> PartyRole | None:
    """The column's party role from its name tokens, or ``None`` (honest abstention).

    ``counter_party`` (and the fused ``counterparty``) is normalized to one token before the
    table lookup so both spellings resolve identically. Off-vocab and non-string are ``None``.
    """
    if not isinstance(column_name, str):
        return None
    folded = column_name.strip().lower().replace("counter_party", "counterparty")
    tokens = frozenset(t for t in _SPLIT.split(folded) if t)
    if not tokens:
        return None
    for wanted, role in _TOKEN_ROLES:
        if tokens & wanted:
            return role
    return None
