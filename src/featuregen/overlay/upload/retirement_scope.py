"""Retirement, decoupled from the money guard — and keyed on something identity cannot move.

**The two guards this separates.** The MONEY guard means *"do not pay twice for the same exact
configuration"*. The RETIREMENT guard means *"do not silently regenerate something deliberately
withdrawn"*. Today they are one mechanism: ``request_draft`` inserts
``ON CONFLICT (formula_identity_hash) DO NOTHING`` and reads ``formula_draft_retirement`` **only
when the INSERT loses**. So retirement is enforced as a side effect of identity collision, and any
correction to the identity hash — however right — moves retirement with it.

▲ **That is why `_authoring_config_hash` cannot simply be fixed.** It returns a constant; correcting
it re-mints every identity, every INSERT then wins, and no retirement row is ever read again. The
one-line fix silently un-retires every formula anyone ever withdrew.

**The scope key is the five identity fields that are NOT the authoring configuration** — so it
survives every future composition change. ▲ It is also exactly the AUTHORING SUBJECT an
``AUTHOR_FORMULA`` authorization authorizes: one tuple, one hash, three uses. Retirement withdraws a
subject, authorization authorizes a subject, and the money guard's non-configuration half *is* that
subject.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum

from featuregen.canonical import jcs_sha256

__all__ = [
    "RetirementScope",
    "TombstoneV1",
    "consume_exception",
    "coverage_identity",
    "record_tombstone",
    "retirement_scope_key",
    "scope_locked",
    "tombstone_covering",
    "valid_exception_for",
]

#: A DIFFERENT namespace from `_DRAFT_LOCK_NAMESPACE`. The draft lock keys on `formula_draft_id`, and
#: a regeneration mints a NEW draft id — so the two callers would hash different keys and never
#: contend, which is exactly the race this lock exists to close.
_SCOPE_LOCK_NAMESPACE = 0x0FD2


class RetirementScope(StrEnum):
    """How much a retirement withdraws.

    ▲ Two members rather than one, because an earlier design had only the wide meaning and reached
    it by a schema choice. Today *"retire this draft"* means an exact draft identity; silently
    widening it to *"this candidate under every current and future model, prompt and strategy"* is a
    new governance power, and a migration must not grant one.
    """

    #: The default, and the meaning every historical row already carries.
    EXACT_DRAFT = "EXACT_DRAFT"
    #: The deliberate stronger act, recorded as such.
    CANDIDATE_ACROSS_CONFIGURATIONS = "CANDIDATE_ACROSS_CONFIGURATIONS"


@dataclass(frozen=True, slots=True)
class TombstoneV1:
    """One recorded withdrawal, and what it covers."""

    tombstone_id: str
    scope: RetirementScope
    retirement_scope_key: str
    exact_formula_identity_hash: str | None
    reason: str
    replacement_draft_id: str | None


def retirement_scope_key(
    *,
    considered_revision_id: str,
    option_id: str,
    planning_request_hash: str,
    catalog_snapshot_hash: str,
    definition_revision: str,
) -> str:
    """The candidate this retirement covers — deliberately EXCLUDING the authoring configuration.

    Every field is already a stored column on ``formula_draft`` (1090), so this is computable for
    every existing row with no fabrication. Excluding the configuration is the whole point: a
    retirement must not evaporate because the platform corrected a hash.
    """
    return jcs_sha256({
        "considered_revision_id": considered_revision_id,
        "option_id": option_id,
        "planning_request_hash": planning_request_hash,
        "catalog_snapshot_hash": catalog_snapshot_hash,
        "definition_revision": definition_revision,
    })


def coverage_identity(scope: RetirementScope, *, scope_key: str, identity_hash: str | None) -> str:
    """WHAT a tombstone covers, so one unique constraint can stop a duplicate of the same act.

    A candidate-wide tombstone covers the scope key; an exact one covers the exact identity. Without
    this, a single-column key would let retiring configuration A consume the candidate's key and
    make B unretirable — and a candidate-wide withdrawal could never follow an exact one.
    """
    if scope is RetirementScope.CANDIDATE_ACROSS_CONFIGURATIONS:
        return jcs_sha256({"scope": str(scope), "retirement_scope_key": scope_key})
    if not identity_hash:
        raise ValueError(
            "an EXACT_DRAFT tombstone covers one exact formula identity, and none was given — a "
            "tombstone that cannot say what it withdrew is not auditable")
    return jcs_sha256({"scope": str(scope), "formula_identity_hash": identity_hash})


@contextmanager
def scope_locked(conn, scope_key: str):
    """Hold the retirement scope's lock across the statements that depend on it.

    ▲ **BOTH the request path and the retirement path take THIS lock**, not the draft lock. "Check
    the tombstone before the INSERT" is not enough on its own::

        request checks: no tombstone
                                       operator retires the candidate and COMMITS
        request inserts the draft and queues provider work

    ``_draft_locked`` cannot close that: it keys on ``formula_draft_id``, and a regeneration mints a
    NEW draft id, so the two callers hash different keys and never contend.

    ▲ **The transaction is part of the lock**, for the reason ``_draft_locked`` already documents:
    ``pg_advisory_xact_lock`` releases on commit, so on an autocommit connection the lock statement
    is its own transaction and the lock is gone before the next statement runs.

    ▲ **BUT IT MUST NOT COMMIT ON A CALLER'S BEHALF, and that is subtler than it looks.**
    ``conn.transaction()`` NESTS as a savepoint when a transaction is already open — and opens a real
    one, **committing on exit**, when none is. So wrapping unconditionally means: whether this
    function commits the caller's work depends on whether the caller happened to execute a statement
    first. It cost a suite-wide leak to find, and the failure did not look like one — drafts
    surviving into later tests presented as impossible state transitions, not as a commit.

    So the block is opened only for an AUTOCOMMIT connection, which is the case that genuinely has no
    scope to borrow. A transactional caller gets the lock inside its own transaction, where
    ``pg_advisory_xact_lock`` already releases exactly when that caller commits or rolls back —
    which is the behaviour wanted in the first place.
    """
    if conn.autocommit:
        with conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(%s, hashtext(%s))",
                         (_SCOPE_LOCK_NAMESPACE, scope_key))
            yield
        return
    conn.execute("SELECT pg_advisory_xact_lock(%s, hashtext(%s))",
                 (_SCOPE_LOCK_NAMESPACE, scope_key))
    yield


def record_tombstone(
    conn, *, formula_draft_id: str, scope: RetirementScope, reason: str, retired_by: str,
    detail: str | None = None, replacement_draft_id: str | None = None,
) -> TombstoneV1:
    """Withdraw a draft, at the named scope. Idempotent on what it covers.

    Reads the draft's own columns for the scope key rather than accepting them, so a caller cannot
    withdraw one candidate while naming another.
    """
    row = conn.execute(
        "SELECT considered_revision_id, option_id, planning_request_hash, catalog_snapshot_hash, "
        "       definition_revision, formula_identity_hash "
        "  FROM formula_draft WHERE formula_draft_id = %s", (formula_draft_id,)).fetchone()
    if row is None:
        raise ValueError(
            f"formula draft {formula_draft_id!r} does not exist, so there is nothing to withdraw")

    scope_key = retirement_scope_key(
        considered_revision_id=row[0], option_id=row[1], planning_request_hash=row[2],
        catalog_snapshot_hash=row[3], definition_revision=row[4])
    exact = row[5] if scope is RetirementScope.EXACT_DRAFT else None
    coverage = coverage_identity(scope, scope_key=scope_key, identity_hash=exact)
    tombstone_id = jcs_sha256({"scope": str(scope), "coverage": coverage})

    with scope_locked(conn, scope_key):
        conn.execute(
            "INSERT INTO formula_draft_retirement_tombstone (tombstone_id, scope, "
            "retirement_scope_key, exact_formula_identity_hash, coverage_identity_hash, "
            "formula_draft_id, reason, detail, replacement_draft_id, retired_by) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (scope, coverage_identity_hash) DO NOTHING",
            (tombstone_id, str(scope), scope_key, exact, coverage, formula_draft_id, reason,
             detail, replacement_draft_id, retired_by))
        # ▲ READ BACK AND COMPARE — never return the caller's arguments as though they were the
        # record. `ON CONFLICT DO NOTHING` reports a disagreeing second retirement as success, so
        # the second operator's decision would vanish while they were told it took effect, and the
        # returned TombstoneV1 would describe a row recording NONE of it. This is the exact defect
        # `RetirementDisagreement` fixed on the legacy path, and the new path must not reintroduce
        # it. Idempotency — the SAME act repeated — remains silently fine.
        stored = conn.execute(
            "SELECT tombstone_id, reason, replacement_draft_id, formula_draft_id, retired_by "
            "FROM formula_draft_retirement_tombstone "
            "WHERE scope = %s AND coverage_identity_hash = %s",
            (str(scope), coverage)).fetchone()
    if (stored[1], stored[2]) != (reason, replacement_draft_id):
        from featuregen.overlay.upload.formula_draft_store import RetirementDisagreement

        raise RetirementDisagreement(
            f"this candidate is already withdrawn at scope {scope} by {stored[4]} "
            f"(reason {stored[1]!r}, replacement {stored[2]!r}), and this retirement says "
            f"reason {reason!r}, replacement {replacement_draft_id!r}. The recorded decision "
            f"stands; a second decision that disagrees must supersede it explicitly, never "
            f"overwrite it silently")
    return TombstoneV1(
        tombstone_id=stored[0], scope=scope, retirement_scope_key=scope_key,
        exact_formula_identity_hash=exact, reason=stored[1],
        replacement_draft_id=stored[2])


def tombstone_covering(
    conn, *, scope_key: str, formula_identity_hash: str,
) -> TombstoneV1 | None:
    """The tombstone withdrawing this request, or ``None``.

    A request is covered when a tombstone names **either** its exact identity **or** its candidate
    scope. Candidate-wide is checked first: it is the stronger withdrawal, and reporting the narrower
    one would understate what was refused.
    """
    row = conn.execute(
        "SELECT tombstone_id, scope, retirement_scope_key, exact_formula_identity_hash, reason, "
        "       replacement_draft_id "
        "  FROM formula_draft_retirement_tombstone "
        " WHERE (scope = 'CANDIDATE_ACROSS_CONFIGURATIONS' AND retirement_scope_key = %s) "
        "    OR (scope = 'EXACT_DRAFT' AND exact_formula_identity_hash = %s) "
        " ORDER BY CASE scope WHEN 'CANDIDATE_ACROSS_CONFIGURATIONS' THEN 0 ELSE 1 END "
        " LIMIT 1", (scope_key, formula_identity_hash)).fetchone()
    if row is None:
        return None
    return TombstoneV1(
        tombstone_id=row[0], scope=RetirementScope(row[1]), retirement_scope_key=row[2],
        exact_formula_identity_hash=row[3], reason=row[4], replacement_draft_id=row[5])


def valid_exception_for(
    conn, *, target_formula_identity_hash: str, provider_contract_hash: str,
    strategy_identity_hash: str, now,
) -> str | None:
    """The unexpired, unconsumed exception authorizing THIS exact regeneration, or ``None``.

    ▲ Every binding is checked, not just the scope: an exception authorizes creating **one exact
    identity**, under **one provider contract** and **one strategy**. An earlier design keyed it on
    the scope and a timestamp, which authorized any regeneration of that candidate, at any cost,
    under any configuration, for ever.
    """
    row = conn.execute(
        "SELECT exception_id FROM formula_draft_regeneration_exception "
        " WHERE target_formula_identity_hash = %s AND provider_contract_hash = %s "
        "   AND strategy_identity_hash = %s AND expires_at > %s AND uses_consumed < max_uses "
        " ORDER BY approved_at LIMIT 1",
        (target_formula_identity_hash, provider_contract_hash, strategy_identity_hash,
         now)).fetchone()
    return None if row is None else row[0]


def consume_exception(conn, exception_id: str) -> bool:
    """Spend one use. ``False`` means it was already exhausted — which is a REFUSAL, not a retry.

    ▲ **Consumption is a conditional UPDATE, in the transaction that performs the work it
    authorizes.** An exception that is checked and not consumed is a coupon that regenerates itself
    every time somebody clicks; one consumed in a separate transaction is one a crash can spend
    twice.
    """
    row = conn.execute(
        "UPDATE formula_draft_regeneration_exception SET uses_consumed = uses_consumed + 1 "
        " WHERE exception_id = %s AND uses_consumed < max_uses RETURNING exception_id",
        (exception_id,)).fetchone()
    return row is not None


def approve_regeneration_exception(
    conn, *, target_formula_identity_hash: str, provider_contract_hash: str,
    strategy_identity_hash: str, actor_subject: str, llm_spend_authorization_id: str,
    expires_at: str, max_uses: int = 1,
) -> tuple[str, bool]:
    """Record one approved regeneration — Stage I Task 6's creation act, LLM-lane-only by the
    owner's Option 2 ruling (deterministic retries are free by construction and need none).

    Idempotent on content: the exception id is the hash of everything it binds, so a replayed
    approval is ONE exception with ONE ``max_uses`` budget — never a stack of fresh coupons.
    ``tombstone_id`` is derived HERE, not accepted: if a tombstone covers the target, the
    exception must name it (1103's CHECK makes the pairing structural), and a caller choosing
    which tombstone it overrides would be choosing what it is exempt from.
    """
    from featuregen.canonical import jcs_sha256

    covering = conn.execute(
        "SELECT tombstone_id FROM formula_draft_retirement_tombstone "
        "WHERE exact_formula_identity_hash = %s OR retirement_scope_key = ("
        "  SELECT retirement_scope_key FROM formula_draft_authoring_identity i"
        "  JOIN formula_draft d ON d.formula_draft_id = i.formula_draft_id"
        "  WHERE d.formula_identity_hash = %s LIMIT 1) "
        "ORDER BY tombstone_id DESC LIMIT 1",
        (target_formula_identity_hash, target_formula_identity_hash)).fetchone()
    tombstone_id = None if covering is None else covering[0]

    exception_id = "exc-" + jcs_sha256({
        "target_formula_identity_hash": target_formula_identity_hash,
        "provider_contract_hash": provider_contract_hash,
        "strategy_identity_hash": strategy_identity_hash,
        "actor_subject": actor_subject,
        "llm_spend_authorization_id": llm_spend_authorization_id,
        "expires_at": str(expires_at),
        "max_uses": max_uses,
        "tombstone_id": tombstone_id,
    })[:32]
    inserted = conn.execute(
        "INSERT INTO formula_draft_regeneration_exception (exception_id, tombstone_id, "
        "target_formula_identity_hash, provider_contract_hash, strategy_identity_hash, "
        "llm_spend_authorization_id, actor_subject, overrides_tombstone, max_uses, expires_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (exception_id) DO NOTHING RETURNING exception_id",
        (exception_id, tombstone_id, target_formula_identity_hash, provider_contract_hash,
         strategy_identity_hash, llm_spend_authorization_id, actor_subject,
         tombstone_id is not None, max_uses, expires_at)).fetchone()
    return exception_id, inserted is not None
