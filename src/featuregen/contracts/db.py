from __future__ import annotations

from typing import Any

import psycopg

# The active psycopg connection/transaction handle. Every function that mutates
# participates in the caller's open transaction (the §5.1 atomic boundary).
DbConn = psycopg.Connection[Any]
