"""Recorded OpenMetadata API fixtures + a fixture-backed FetchPage. NO network anywhere."""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from featuregen.connectors.openmetadata import OMConfig

_FIXTURES = Path(__file__).parent / "fixtures"

# The tag map the tests configure: PII.Sensitive is mapped; Confidential.Internal is deliberately
# NOT — its columns must pass the literal tag through and quarantine (fail-closed read-scope).
CARDS_TAG_MAP = {"PII.Sensitive": "pii"}

CARDS_CONFIG = OMConfig(base_url="https://om.test", target_source="cards",
                        tag_map=CARDS_TAG_MAP, filters={}, table_naming="table")


def load_page(name: str) -> dict[str, Any]:
    return json.loads((_FIXTURES / name).read_text())


def fixture_pages() -> tuple[dict[str, Any], dict[str, Any]]:
    """Deep copies, so a test may mutate its pages without corrupting another test's view."""
    return (copy.deepcopy(load_page("om_tables_page1.json")),
            copy.deepcopy(load_page("om_tables_page2.json")))


def fixture_fetch(page1: dict[str, Any] | None = None, page2: dict[str, Any] | None = None):
    """A FetchPage serving the recorded pages: no `after` -> page 1; the page-1 cursor -> page 2."""
    p1, p2 = fixture_pages()
    page1, page2 = page1 or p1, page2 or p2

    def fetch(path: str, params: dict[str, Any]) -> dict[str, Any]:
        assert path == "/api/v1/tables"
        assert params["fields"] == "columns,tags,tableConstraints"
        assert "limit" in params
        return page2 if params.get("after") else page1

    return fetch
