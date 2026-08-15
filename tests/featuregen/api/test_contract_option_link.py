"""SUCCESSOR 4, increment 1 — a governed contract RECORDS the option it was minted from (1069).

The seam this closes is not a query, it is a RECORD. C3 made ``requirements_closed`` a real read of
the CONTRACT-keyed validation store, and the one route that gates materialization had no way to name
a contract: `contract` is keyed by feature identity and `semantic_option_decision` by
``(considered_revision_id, option_id)``, and nothing joined them. Here the mint writes the link, at
the one moment both id spaces are in a single caller's hands.

**Why the link can only be written HERE, and never reconstructed.** ``feature_name`` is not identity —
a re-confirm mints a NEW contract version under the same name — so a name-based join would be a guess
wearing a join's clothes. ``semantic_option_decision`` is append-only and is written at GENERATION,
before any contract exists, so the reverse column could never be filled. And ``contract`` is WORM
(1012): there is no UPDATE, so a link is stamped by the INSERT that mints the row or it is honestly
NULL forever. That is what makes the legacy boundary a fact about the schema rather than a discipline.
"""
from __future__ import annotations

import pathlib

import pytest
from tests.featuregen.api._helpers import AUTH
from tests.featuregen.api.test_binding_confirmation import _confirm_body, _ready
from tests.featuregen.api.test_materialization_option_link import REVIEWED, _freeze_option

from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.contract.author import ContractDraft
from featuregen.overlay.upload.contract.govern import ContractValidationError, confirm_contract
from featuregen.overlay.upload.graph import build_graph

_MIGRATION_1069 = (
    pathlib.Path(__file__).resolve().parents[3]
    / "src/featuregen/db/migrations/1069_contract_option_link.sql")

#: The recipe behind the draftable card ``governed_ready_round`` clears the blockers on — the value
#: the decision row is keyed by, which is NOT the card's name and NOT the governed feature_name.
HERO = "complaint_count"


def _bank(conn) -> None:
    """The minimal catalog a direct confirm's MCV re-run needs (mirrors test_govern._bank)."""
    from datetime import UTC, datetime

    now = datetime(2026, 7, 5, tzinfo=UTC)
    build_graph(conn, "bank", [
        CanonicalRow("bank", "accounts", "id", "integer", is_grain=True),
        CanonicalRow("bank", "accounts", "balance", "numeric"),
        CanonicalRow("bank", "accounts", "posted_at", "timestamp", as_of=True)])
    conn.execute(
        "INSERT INTO overlay_drift_watermark (catalog_source, last_completed_at, last_run_id, "
        "head_seq) VALUES ('bank', %s, 'r', 0) "
        "ON CONFLICT (catalog_source) DO UPDATE SET last_completed_at = %s", (now, now))


def _draft() -> ContractDraft:
    return ContractDraft("avg_balance_90d", "Average 90-day ledger balance.", "accounts",
                         "avg_90d", "posted_at", ["public.accounts.balance"],
                         derives_pairs=(("bank", "public.accounts.balance"),))


def _link(conn, contract_id: str) -> tuple[str | None, str | None]:
    return conn.execute(
        "SELECT considered_revision_id, option_id FROM contract WHERE contract_id = %s",
        (contract_id,)).fetchone()


# ── the mint records it, through the REAL route ──────────────────────────────────────────────────


def test_the_confirmed_contract_records_the_option_the_human_chose(make_client, conn, monkeypatch):
    """THE POINT OF THE INCREMENT, over the real considered-set → draft → confirm flow.

    The recorded link is checked against the human's own Gate-1 choice — the revision the choice row
    names, and a decision row whose ``source_definition_id`` is the recipe on the chosen card — so a
    writer that stamped a DIFFERENT served option, or resolved one by feature name, fails here.
    """
    client, body, dr = _ready(make_client, conn, monkeypatch)

    cr = client.post("/contract/confirm", json=_confirm_body(dr, body["intent_id"]), headers=AUTH)
    assert cr.status_code == 200, cr.text
    contract_id = cr.json()["contract_id"]

    revision_id, chosen_name = conn.execute(
        "SELECT considered_revision_id, chosen_option_id FROM contract_gate1_choice "
        "WHERE intent_id = %s", (body["intent_id"],)).fetchone()
    assert revision_id, "the flow must have recorded a Gate-1 choice against a revision"

    linked = _link(conn, contract_id)
    assert linked[0] == revision_id

    # ...and the option half names a REAL decision row — the whole reason the FK exists — and it is
    # the card the human chose, not merely some option of that revision.
    assert conn.execute(
        "SELECT source_definition_id FROM semantic_option_decision "
        "WHERE considered_revision_id = %s AND option_id = %s", linked).fetchone() == (HERO,)

    # AND THE STANDING ARGUMENT, made by the fixture rather than asserted in prose: the names do not
    # line up. The human's choice is recorded against the CARD's name, the contract is governed under
    # that name, and the decision row is keyed by the RECIPE id — three different strings for one
    # option. A resolution by feature_name could not have found this row at all.
    assert chosen_name != HERO
    assert conn.execute("SELECT feature_name FROM contract WHERE contract_id = %s",
                        (contract_id,)).fetchone() == (chosen_name,)


def test_a_re_confirm_links_BOTH_versions_to_the_same_option(make_client, conn, monkeypatch):
    """Cardinality, asserted rather than assumed: many contract VERSIONS to one option. A re-confirm
    appends a new immutable version (1012 forbids rewriting the first), and each names the option it
    came from — which is why increment 2's resolution has to pick a version rather than a row."""
    client, body, dr = _ready(make_client, conn, monkeypatch)
    payload = _confirm_body(dr, body["intent_id"])

    first = client.post("/contract/confirm", json=payload, headers=AUTH)
    second = client.post("/contract/confirm", json=payload, headers=AUTH)
    assert (first.status_code, second.status_code) == (200, 200), (first.text, second.text)
    assert [first.json()["version"], second.json()["version"]] == [1, 2]

    linked = conn.execute(
        "SELECT version, considered_revision_id, option_id FROM contract "
        "WHERE feature_name = %s ORDER BY version",
        (first.json()["feature_name"],)).fetchall()
    assert len(linked) == 2
    assert linked[0][1:] == linked[1][1:]
    assert all(row[1] and row[2] for row in linked)


# ── the honest NULL, and the constraints that make a stated pair mean something ──────────────────


def test_a_direct_confirm_that_names_no_option_records_an_honest_null(conn):
    """A confirm with no served option decision behind it — the direct/legacy path — mints a contract
    with NULL on both halves. Never a guess, never a name-based backfill: the column says "nobody
    recorded this", which is exactly what increment 2's read must fail closed on."""
    _bank(conn)
    contract = confirm_contract(conn, _draft(), actor="ds1")
    assert _link(conn, contract.contract_id) == (None, None)


def test_a_stated_pair_naming_no_decision_row_is_refused_by_the_DATABASE(conn):
    """The FK, not the writer's good intentions. A contract citing an approval that does not exist is
    refused where it cannot be argued with."""
    _bank(conn)
    with pytest.raises(Exception) as excinfo:
        confirm_contract(conn, _draft(), actor="ds1", option_key=("rev-nobody", "opt-nobody"))
    assert "contract_option_fk" in str(excinfo.value)
    conn.rollback()


@pytest.mark.parametrize("half", [("rev-only", ""), ("", "opt-only")])
def test_half_a_key_is_refused_by_the_WRITER_as_well_as_the_migration(conn, half):
    """An option is addressed by BOTH halves. Recording one would put provenance in the table that
    nothing could resolve, so the writer states the rule and 1069 carries it as a CHECK."""
    _bank(conn)
    with pytest.raises(ContractValidationError) as excinfo:
        confirm_contract(conn, _draft(), actor="ds1", option_key=half)
    assert "BOTH" in str(excinfo.value)


def test_the_check_constraint_refuses_half_a_key_at_the_table(conn):
    """The same law, one layer down — the route and ``confirm_contract`` are not the only writers
    that could ever exist."""
    conn.execute("INSERT INTO feature (feature_id, name, lifecycle_state) "
                 "VALUES ('feat-half', 'feat-half', 'governed')")
    with pytest.raises(Exception) as excinfo:
        conn.execute(
            "INSERT INTO contract (contract_id, feature_id, feature_name, version, "
            "considered_revision_id) VALUES ('c-half', 'feat-half', 'feat-half', 1, 'rev-only')")
    assert "contract_option_is_whole" in str(excinfo.value)
    conn.rollback()


def test_a_stated_pair_that_IS_real_is_accepted_at_the_table(conn):
    """The positive control for both constraints: a pair naming a real decision row lands."""
    revision_id, option_id = _freeze_option(conn, REVIEWED, key="link-ok")
    _bank(conn)
    contract = confirm_contract(conn, _draft(), actor="ds1",
                                option_key=(revision_id, option_id))
    assert _link(conn, contract.contract_id) == (revision_id, option_id)


# ── increment 2: the RESOLUTION along the link ───────────────────────────────────────────────────


def test_the_resolution_takes_the_NEWEST_LINKED_version_and_ignores_unlinked_ones(conn):
    """``_contract_minted_from``'s stated rule, pinned in the two directions that discriminate.

    A re-confirm appends a version (1012 forbids rewriting the first), and the validation store is
    per-contract-version, so the NEWEST linked row is the one whose requirements are actually owed —
    an older version's answers would be a superseded contract's homework. And a contract for the
    SAME feature with NO link is not the answer either: an implementation that resolved by feature
    identity, or simply took the latest contract, would return the third row here.
    """
    from featuregen.api.routes.materialization_runs import _contract_minted_from

    revision_id, option_id = _freeze_option(conn, REVIEWED, key="newest")
    _bank(conn)
    first = confirm_contract(conn, _draft(), actor="ds1", option_key=(revision_id, option_id))
    second = confirm_contract(conn, _draft(), actor="ds1", option_key=(revision_id, option_id))
    unlinked = confirm_contract(conn, _draft(), actor="ds1")
    assert [first.version, second.version, unlinked.version] == [1, 2, 3]

    resolved = _contract_minted_from(
        conn, considered_revision_id=revision_id, option_id=option_id)
    assert resolved == second.contract_id
    assert resolved not in (first.contract_id, unlinked.contract_id)


def test_an_option_that_never_reached_a_contract_resolves_to_None(conn):
    """The fail-closed half. ``None`` is a real answer — ``requirements_closed`` reads it as "not
    closed", which is the correct reading of "nobody recorded anything"."""
    from featuregen.api.routes.materialization_runs import _contract_minted_from

    revision_id, option_id = _freeze_option(conn, REVIEWED, key="never")
    assert _contract_minted_from(
        conn, considered_revision_id=revision_id, option_id=option_id) is None


# ── the migration audit (CI is blind to legacy data) ─────────────────────────────────────────────


def test_migration_1069_applies_to_a_POPULATED_legacy_contract_table(conn):
    """THE AUDIT. Drop the link, seed a contract in the pre-1069 shape, then run the migration's own
    SQL against it — the state a live deployment is actually in.

    What it proves, and no fresh-database CI run can: the ALTER lands on a WORM table that already
    has rows (1012's trigger refuses UPDATE, so a rewriting migration would ABORT here rather than
    in review); the legacy row keeps NULL on both columns — no backfill of invented provenance; the
    row still reads; and the new constraints then bite.
    """
    conn.execute("ALTER TABLE contract "
                 "DROP CONSTRAINT IF EXISTS contract_option_fk, "
                 "DROP CONSTRAINT IF EXISTS contract_option_is_whole, "
                 "DROP COLUMN IF EXISTS considered_revision_id, DROP COLUMN IF EXISTS option_id")
    conn.execute("INSERT INTO feature (feature_id, name, lifecycle_state) "
                 "VALUES ('feat-legacy', 'feat-legacy', 'governed')")
    conn.execute(
        "INSERT INTO contract (contract_id, feature_id, feature_name, definition, version, "
        "verification, validation_status) VALUES ('contract-legacy', 'feat-legacy', "
        "'feat-legacy', 'a contract governed before the link existed', 1, 'DESIGN-CHECKED', "
        "'DESIGN_CHECKED')")

    conn.execute(_MIGRATION_1069.read_text())                  # the migration's OWN sql, verbatim

    after = conn.execute(
        "SELECT definition, considered_revision_id, option_id FROM contract "
        "WHERE contract_id = 'contract-legacy'").fetchone()
    assert after[0] == "a contract governed before the link existed"   # nothing was rewritten
    assert (after[1], after[2]) == (None, None)                        # no invented provenance

    # ...and the constraints the migration added really constrain the rows written from now on.
    with pytest.raises(Exception) as excinfo:
        conn.execute(
            "INSERT INTO contract (contract_id, feature_id, feature_name, version, "
            "considered_revision_id, option_id) VALUES ('c-ghost', 'feat-legacy', 'feat-legacy', "
            "2, 'rev-nobody', 'opt-nobody')")
    assert "contract_option_fk" in str(excinfo.value)
    conn.rollback()


def test_migration_1069_is_re_runnable(conn):
    """``IF NOT EXISTS`` on the columns and the index, a ``pg_constraint`` guard on the two
    constraints — a re-applied deploy is a no-op rather than an error."""
    conn.execute(_MIGRATION_1069.read_text())
    conn.execute(_MIGRATION_1069.read_text())
    assert conn.execute(
        "SELECT count(*) FROM pg_constraint WHERE conrelid = 'contract'::regclass "
        "AND conname IN ('contract_option_fk', 'contract_option_is_whole')").fetchone()[0] == 2
