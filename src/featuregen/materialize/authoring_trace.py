"""The WORM authoring trace Gate 1 proves against — migration **1022**'s store.

WHY THIS MODULE EXISTS. ``featuregen.materialize.admission`` needs three things from the immutable
record of an authoring run: its terminal event, that event's tamper-evidence hash, and the
``intent_hash`` its write-once manifest was stamped with. ``formula.trace`` offers exactly that API
over migration **1020** — and admission used to use it. It no longer can, and the reason is not a
preference:

**Nothing in ``src/`` has ever written the 1020 store.** Its only writer is
``formula.trace.open_authoring_run`` / ``append_event``, whose only caller is
``formula.authoring.run_authoring``, which no production code path invokes. The LIVE authoring
worker imports ``run_authoring`` from ``formula.replay_authoring``
(``overlay/upload/recipe_formula_worker.py:35``), which writes migration 1022's
``formula_authoring_run`` / ``formula_authoring_trace_event``. So every governed feature that
actually exists is recorded in 1022, and a gate reading 1020 refuses all of them with
``AUTHORING_RUN_INCOMPLETE`` — verified empirically before this module was written. The orphaned
1020 lane is recorded in ``docs/DEFERRED-WORK.md`` A.33.

**ONE lane, not two.** This module deliberately does NOT fall back to 1020 when 1022 has nothing.
"Either store may vouch for the run" is strictly weaker than either store alone: a forger would
only have to satisfy whichever is easier, and a run present in both with DIFFERENT verdicts would
have no defined answer. Since nothing writes 1020, there is also no real artifact to strand.

WHY THE READS ARE HERE AND NOT IN ``formula/``. Ownership: `formula/` is not Phase G's to change.
Everything borrowed from it is imported READ-ONLY, and deliberately the *writer's own* helpers
rather than re-implementations —

* ``replay_trace._hash`` is the function ``_insert_event`` used to compute the stored
  ``payload_hash``. It is ``json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=False)``
  then sha256 — NOT RFC 8785, so ``materialize.canonical.materialize_hash`` is wrong here, and not
  ASCII-escaping either, so ``overlay.field_evidence.canonical_hash`` is wrong here too. The two
  differ only on non-ASCII payloads, which is precisely the kind of near-miss a second copy of a
  hasher produces. ``test_admission.py`` pins the borrow against a real run's stored hash.
* ``replay_authoring._intent_material`` is the projection ``run_authoring`` hashed into the manifest
  before any provider call. It covers FIVE fields — ``name``, ``hypothesis``, ``target_entity``,
  ``target_grain_keys`` **and ``recipe_authoring_context``** — where 1020's
  ``authoring.authoring_intent_hash`` covered four. Moving lanes therefore WIDENS what check 6
  proves; ``recipe_authoring_context`` is the authoring prompt's catalog context
  (``formula/author.py:98-99``), so an intent claiming one the author never saw is a different
  request, and is now refused.

CONNECTION SEMANTICS, matched to the writer. ``replay_trace._durable_write`` either commits on its
own connection or RAISES ``FormulaTraceUnavailable`` — unlike 1020's writer it never degrades a row
onto the caller's uncommitted transaction. There is therefore no second copy to union over, and
these reads go on the caller's connection exactly as ``replay_trace.run_status`` and
``load_verified_checkpoint`` do. The consequence is the usual one and is the writer's, not this
module's: a caller pinned to ``REPEATABLE READ`` before the authoring commit sees ABSENCE, which
every consumer here treats as "no verdict exists to admit" — the fail-closed direction.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from featuregen.contracts.db import DbConn

# Bound PRIVATELY on purpose: these are this module's implementation, borrowed from the writer so
# the two sides of a proof cannot drift, and NOT part of `formula/`'s public API that Phase G may
# rely on. A re-export would make an unassigned module's private helper into a materialize contract.
from featuregen.formula.replay_authoring import _intent_material as _intent_projection
from featuregen.formula.replay_trace import _hash as _digest
from featuregen.formula.turns import AuthoringIntent
from featuregen.overlay.field_evidence import canonical_hash

__all__ = [
    "TERMINAL_KINDS",
    "TerminalEvent",
    "authoring_intent_hash",
    "payload_digest",
    "read_run_intent_hash",
    "read_terminal_event",
]

#: The two kinds that CLOSE a 1022 run, lower-case as migration 1022's CHECK spells them. Mirrored
#: by the partial UNIQUE index, so at most one row per run can carry either.
#:
#: ⚠️ ``kind`` ALONE ANSWERS ALMOST NOTHING, and the mapping is not the one 1020 used. The 1022
#: orchestrator writes ``failed`` for a technical failure AND for a proposal the shape gate or the
#: recipe validator rejects, but its normal terminal append writes ``completed`` for every
#: disposition that reaches the §F fold — RESOLVED, NEEDS_REVIEW and an ``invalid_output`` REJECTED
#: alike (``replay_authoring.py:674-683``). Read ``payload["authoring_disposition"]``.
TERMINAL_KINDS: tuple[str, str] = ("completed", "failed")

_SELECT_TERMINAL_EVENT = (
    "SELECT kind, payload, payload_hash FROM formula_authoring_trace_event "
    "WHERE authoring_run_id = %s AND kind IN (%s, %s) LIMIT 1"
)
_SELECT_RUN_INTENT_HASH = (
    "SELECT intent_hash FROM formula_authoring_run WHERE authoring_run_id = %s"
)


@dataclass(frozen=True, slots=True)
class TerminalEvent:
    """The single terminal event of a run, WITH the record that makes it tamper-evident.

    This type deliberately does NOT verify one against the other. The row is physically immutable
    (1022's write-once triggers), so a disagreement means the stored bytes were altered out of band,
    and deciding what to do about that belongs to the caller that is trusting the record — see
    ``admission`` (§1.2 check 2), which refuses with ``TERMINAL_PAYLOAD_TAMPERED``. A reader that
    silently dropped a mismatching event would report the run as INCOMPLETE and hide the tampering
    instead of surfacing it.

    ``payload`` is typed ``Any``, not ``Mapping``, and that is load-bearing twice over. 1022's
    column carries no ``jsonb_typeof = 'object'`` CHECK (1020's did), so a directly-written row may
    hold an array or a scalar; and check 2 must digest EXACTLY what was stored, so nothing may be
    coerced on the way out. Field access goes through ``admission``'s own guard, which reads a
    non-object payload as "no verdict" rather than crashing on it.

    ``payload_hash`` is ``str | None``: migration 1026 added the column to an existing table, so it
    is NULLable and a row can carry no tamper evidence at all. That is a refusal, not a skip.
    """

    kind: str
    payload: Any
    payload_hash: str | None


def read_terminal_event(conn: DbConn, run_id: str) -> TerminalEvent | None:
    """The run's terminal (``completed``/``failed``) event, or ``None`` when it has none.

    ``None`` is the honest ABSENCE and covers four states that are indistinguishable here and mean
    the same thing: a live run, a process that died mid-authoring, a run whose manifest never
    committed, and a run id that names nothing. All of them mean no authoring verdict exists.

    At most one row can match (1022's ``formula_authoring_one_terminal`` partial UNIQUE index), so
    ``LIMIT 1`` is the whole result set, not an arbitrary pick."""
    row = conn.execute(_SELECT_TERMINAL_EVENT, (run_id, *TERMINAL_KINDS)).fetchone()
    if row is None:
        return None
    kind, payload, payload_hash = row
    return TerminalEvent(
        kind=str(kind),
        # A SHALLOW copy, so the record does not alias psycopg's own dict — nested values are still
        # shared, and nothing here should be read as deep isolation. Admission only ever reads
        # top-level fields, and check 2 digests before any of them.
        payload=dict(payload) if isinstance(payload, Mapping) else payload,
        payload_hash=None if payload_hash is None else str(payload_hash),
    )


def read_run_intent_hash(conn: DbConn, run_id: str) -> str | None:
    """The ``intent_hash`` stamped on the run's MANIFEST, or ``None`` when no manifest is visible.

    The manifest is written FIRST, before any provider call, and is write-once — so this is the
    immutable record of WHAT was asked, against which a caller re-hashing its own
    :class:`~featuregen.formula.turns.AuthoringIntent` can prove it holds the intent this run was
    actually opened for. ``None`` is the honest absence and callers must fail CLOSED on it rather
    than treating it as a match."""
    row = conn.execute(_SELECT_RUN_INTENT_HASH, (run_id,)).fetchone()
    return None if row is None else str(row[0])


def payload_digest(payload: Any) -> str:
    """The sha256 ``append_event`` computed over ``payload`` when it wrote the row.

    THE writer's own hasher (module docstring), not a second copy, and applied to the value as read
    back from jsonb: ``sort_keys=True`` is what makes that round trip stable when Postgres returns
    an object's keys in a different order than they were written."""
    return _digest(payload)


def authoring_intent_hash(intent: AuthoringIntent) -> str:
    """The content hash of an authoring intent, by the recipe the 1022 MANIFEST was stamped with.

    ``canonical_hash(_intent_material(intent))`` — five fields, including
    ``recipe_authoring_context``. Public because the intent hash is a proof BOTH sides of the seam
    need: ``admission`` re-derives it for check 6, and ``resolve`` re-derives it to attribute a
    mismatch to a specific member. One function, so the two can never disagree about what an intent
    IS."""
    return canonical_hash(_intent_projection(intent))
