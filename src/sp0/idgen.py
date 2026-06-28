from __future__ import annotations

# Phase-07 id helper. The canonical ULID-style prefixed-id minter already lives in
# sp0.aggregates.ids (Crockford-base32 ULID -> lexicographically sortable, time-ordered).
# Phase 07 consumers (security audit, human tasks, timers, delegations) import it from
# here per the shared contract; we RE-EXPORT the single canonical implementation rather
# than duplicate it, so all phases mint ids identically.
from sp0.aggregates.ids import mint_id

__all__ = ["mint_id"]
