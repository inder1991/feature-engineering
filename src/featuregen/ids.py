from __future__ import annotations

from ulid import ULID

# All `*_id` text keys are ULID-style prefixed strings (overview §"Database schema"):
# 'evt_...', 'doc_...', run_id = 'run_...', etc. ULID gives lexicographically-sortable,
# time-ordered ids so prefixed ids sort in creation order within a prefix.


def new_id(prefix: str) -> str:
    """Mint a ULID-style prefixed id: f'{prefix}_{ULID()}' (e.g. new_id('doc') -> 'doc_01J...')."""
    return f"{prefix}_{ULID()}"
