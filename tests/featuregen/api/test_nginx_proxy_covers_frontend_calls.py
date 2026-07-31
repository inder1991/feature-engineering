"""The proxy allowlists must cover what the frontend actually calls.

A route can exist, be registered, pass every backend test, be verified present in the deployed pod's
OpenAPI — and still be unreachable from the browser, because nginx proxies a HAND-MAINTAINED prefix
list and falls through to the SPA for anything absent from it. The symptom is the worst kind: a POST
to a missing prefix answers **405 Not Allowed**, which reads like a backend defect and is not one.
`/analysis/plan` shipped exactly that way.

There are THREE places that must agree and no two of them are generated from the other:

* ``deploy/kind/nginx.conf`` — production, baked into the frontend image;
* ``frontend/vite.config.ts`` ``API_PATHS`` — the dev server proxy;
* ``frontend/src/api.ts`` — the paths the client actually requests.

Both proxies had the same gap, so the route was broken in dev and prod alike, and nothing failed. A
comment in nginx.conf asking a human to keep them in step is what was there before, and it did not
work — this does the checking.

Deliberately driven from what the CLIENT CALLS rather than from the OpenAPI document: not every API
surface is meant to reach the browser (``/metrics`` and ``/admin`` are unproxied on purpose), so
demanding full coverage of the API would either fail forever or pressure someone into exposing them.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[3]
_NGINX = _ROOT / "deploy" / "kind" / "nginx.conf"
_VITE = _ROOT / "frontend" / "vite.config.ts"
_API = _ROOT / "frontend" / "src" / "api.ts"

#: A path literal that STARTS a URL. The lookbehind is the load-bearing part: `api.ts` builds long
#: URLs by concatenation, so `... + \`/fields/${f}/decisions\`` contains a literal beginning `/fields`
#: that is a CONTINUATION, not a root. Counting it reports a missing proxy for a prefix nobody
#: requests — and a check that cries wolf is one somebody switches off.
_CALL = re.compile(r"""(?<![+\w])\s*["'`](/[a-z0-9][a-z0-9-]*)(?=["'`/])""")


def _frontend_prefixes() -> set[str]:
    """Top-level prefixes `api.ts` requests, e.g. `/analysis` from `/analysis/plan`.

    Only the FIRST segment matters: the proxy rules match on it, so `/catalog/assets/...` is covered
    by `catalog`.
    """
    text = _API.read_text(encoding="utf-8")
    found: set[str] = set()
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith(("//", "*", "+")):
            # A comment, or a continuation line whose literal appends to a URL built above.
            continue
        for match in _CALL.finditer(line):
            found.add("/" + match.group(1).strip("/").split("/")[0])
    return found


def _nginx_prefixes() -> set[str]:
    text = _NGINX.read_text(encoding="utf-8")
    match = re.search(r"location\s+~\s+\^/\(([^)]+)\)", text)
    assert match, "could not find the API location regex in nginx.conf"
    return {"/" + name for name in match.group(1).split("|")}


def _vite_prefixes() -> set[str]:
    text = _VITE.read_text(encoding="utf-8")
    block = re.search(r"const API_PATHS\s*=\s*\[(.*?)\]", text, re.S)
    assert block, "could not find API_PATHS in vite.config.ts"
    return set(re.findall(r"['\"](/[a-z0-9-]+)['\"]", block.group(1)))


def test_every_path_the_frontend_calls_is_proxied_in_PRODUCTION():
    """The one that broke. `/analysis/plan` reached nginx, matched no proxy rule, fell through to the
    SPA fallback, and answered a POST with 405."""
    missing = sorted(_frontend_prefixes() - _nginx_prefixes())
    assert not missing, (
        f"nginx.conf does not proxy {missing}; a POST to these answers 405 Not Allowed from the SPA "
        "fallback, which looks like a backend bug and is not one")


def test_every_path_the_frontend_calls_is_proxied_in_DEV():
    """Both lists had the same gap, so running the dev server would not have caught it either."""
    missing = sorted(_frontend_prefixes() - _vite_prefixes())
    assert not missing, f"vite.config.ts API_PATHS does not proxy {missing}"


def test_the_two_proxy_lists_agree():
    """They are maintained by hand in two files. A prefix in one and not the other means the app
    behaves differently in dev and prod — the hardest kind of bug to reproduce."""
    only_nginx = sorted(_nginx_prefixes() - _vite_prefixes())
    only_vite = sorted(_vite_prefixes() - _nginx_prefixes())
    assert not only_nginx and not only_vite, (
        f"only in nginx.conf: {only_nginx}; only in vite.config.ts: {only_vite}")


@pytest.mark.parametrize("prefix", ["/analysis", "/learning"])
def test_the_data_agent_surfaces_are_reachable(prefix):
    """Named explicitly, because these are the ones that shipped unreachable and a generic set
    comparison would go quiet again if `api.ts` stopped calling them for an unrelated reason."""
    assert prefix in _nginx_prefixes()
    assert prefix in _vite_prefixes()


def test_catalog_and_catalogs_are_distinct_prefixes():
    """The regex is anchored with `(/|$)`, so `catalog` does NOT match `/catalogs` — a natural thing
    to assume when reading the list, and wrong."""
    assert "/catalogs" in _nginx_prefixes(), "/catalogs is not covered by /catalog"
    assert "/catalog" in _nginx_prefixes()
