"""Delivery H2d — the ``featuregen pointer-repair`` CLI subcommand wiring (a thin adapter over the
DB-tested repair/backfill functions in overlay/upload/contract/pointer_repair.py)."""
from __future__ import annotations

import featuregen.__main__ as m


def test_parser_accepts_pointer_repair_backfill():
    args = m._build_parser().parse_args(["pointer-repair", "--dsn", "postgresql:///x"])
    assert args.command == "pointer-repair"
    assert args.feature_id is None


def test_parser_accepts_pointer_repair_single_feature():
    args = m._build_parser().parse_args(
        ["pointer-repair", "--dsn", "postgresql:///x", "--feature-id", "feat_1"])
    assert args.command == "pointer-repair"
    assert args.feature_id == "feat_1"


def test_main_routes_backfill(monkeypatch):
    seen: dict = {}
    monkeypatch.setattr(m, "_run_pointer_repair",
                        lambda dsn, feature_id: seen.update(dsn=dsn, feature_id=feature_id) or 0)
    assert m.main(["pointer-repair", "--dsn", "postgresql:///x"]) == 0
    assert seen == {"dsn": "postgresql:///x", "feature_id": None}


def test_main_routes_single_feature_repair(monkeypatch):
    seen: dict = {}
    monkeypatch.setattr(m, "_run_pointer_repair",
                        lambda dsn, feature_id: seen.update(dsn=dsn, feature_id=feature_id) or 0)
    assert m.main(["pointer-repair", "--dsn", "d", "--feature-id", "f9"]) == 0
    assert seen == {"dsn": "d", "feature_id": "f9"}
