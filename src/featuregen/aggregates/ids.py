from __future__ import annotations

import os
import re
import time

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _encode(value: int, length: int) -> str:
    out = ""
    for _ in range(length):
        out = _CROCKFORD[value & 31] + out
        value >>= 5
    return out


def _ulid() -> str:
    ts = int(time.time() * 1000)
    rand = int.from_bytes(os.urandom(10), "big")
    return _encode(ts, 10) + _encode(rand, 16)


def mint_id(prefix: str) -> str:
    return f"{prefix}_{_ulid()}"


def new_request_id() -> str:
    return mint_id("req")


def new_feature_id() -> str:
    return mint_id("feat")


def new_run_id() -> str:
    return mint_id("run")


def new_feature_version_id() -> str:
    return mint_id("fv")


def new_consumer_id() -> str:
    return mint_id("con")


def new_command_id() -> str:
    return mint_id("cmd")


def normalize_concept_key(concept: str) -> str:
    s = concept.strip().lower()
    s = re.sub(r"[^a-z0-9]+", " ", s).strip()
    s = re.sub(r"\s+", "-", s)
    return s
