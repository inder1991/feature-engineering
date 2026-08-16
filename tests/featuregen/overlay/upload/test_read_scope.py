from datetime import UTC, datetime, timedelta

from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.overlay.config import OverlayConfig, register_overlay_config
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.ingest import ingest_upload
from featuregen.overlay.upload.read_scope import allowed_sensitivities
from featuregen.overlay.upload.search import search


def _actor():
    return IdentityEnvelope(subject="upload", actor_kind="human", authenticated=True,
                            auth_method="oidc", role_claims=("data_owner",))


def _seal():
    register_overlay_config(OverlayConfig(
        ttl_default=timedelta(days=180), ttl_min=timedelta(days=30), ttl_max=timedelta(days=365),
        ttl_jitter_fraction=0.1, renewal_grace=timedelta(days=14),
        drift_scan_interval=timedelta(minutes=15), drift_freshness_sla=timedelta(hours=24),
        profiler_require_restricted_role=False))


def test_allowed_sensitivities_maps_roles():
    assert allowed_sensitivities(()) == []
    assert allowed_sensitivities({"pii_reader"}) == ["pii"]
    assert set(allowed_sensitivities({"pii_reader", "restricted_reader"})) == {"pii", "restricted"}


def test_pii_node_hidden_without_role_visible_with_role(db):
    _seal()
    now = datetime(2026, 7, 5, tzinfo=UTC)
    rows = [
        CanonicalRow("deposits", "accounts", "ssn_hash", "text", sensitivity="pii"),
        CanonicalRow("deposits", "accounts", "balance", "numeric"),
    ]
    assert ingest_upload(db, "deposits", rows, actor=_actor(), now=now).status == "ingested"

    # No role -> the PII column is excluded from search entirely.
    open_hits = {h.object_ref for h in search(db, "ssn_hash", now=now).hits}
    assert "public.accounts.ssn_hash" not in open_hits

    # With the pii_reader role -> visible, and its sensitivity is surfaced.
    priv = search(db, "ssn_hash", now=now, roles={"pii_reader"}).hits
    hit = next(h for h in priv if h.object_ref == "public.accounts.ssn_hash")
    assert hit.sensitivity == "pii"

    # A non-sensitive column is visible either way.
    assert any(h.column == "balance" for h in search(db, "balance", now=now).hits)


# ── The governed floor must gate reads, not just the raw file tag ────────────────────────────────
# Measured on the deployed FTR catalog (2026-07-28): 126/126 columns carry `sensitivity = NULL`
# because a glossary attests no sensitivity, while the concept-derived floor put 28 of them at
# `restricted`/`confidential` — customer names, addresses, phone numbers and an Emirates ID number.
# Read scope consulted only the raw tag, whose rule is "untagged is always visible", so every one of
# those was readable by any caller holding `catalog:read`.

_NOW = datetime(2026, 7, 28, tzinfo=UTC)


def _floored(db, column, *, sensitivity=None, floor=None):
    """Ingest one column, then set the GOVERNED floor the concept cascade would have derived.
    Mirrors the live shape: the raw tag is whatever the file said (usually nothing); the floor is
    computed separately into its own column."""
    rows = [CanonicalRow("deposits", "accounts", column, "text", sensitivity=sensitivity or ""),
            CanonicalRow("deposits", "accounts", "plain_col", "text")]
    assert ingest_upload(db, "deposits", rows, actor=_actor(), now=_NOW).status == "ingested"
    if floor is not None:
        db.execute("UPDATE graph_node SET effective_restriction = %s "
                   "WHERE catalog_source = 'deposits' AND column_name = %s", (floor, column))


def _visible(db, column, roles=()):
    return any(h.column == column for h in search(db, column, now=_NOW, roles=roles).hits)


def test_governed_floor_hides_a_column_the_raw_tag_left_untagged(db):
    """THE LEAK. A glossary column arrives untagged; the concept cascade rules it `restricted`.
    Read scope must honour the FLOOR — otherwise the system correctly identifies a national ID as
    sensitive and then shows it to everyone."""
    _seal()
    _floored(db, "eida_num", sensitivity=None, floor="restricted")
    assert not _visible(db, "eida_num"), "a governed-restricted column must not be world-readable"
    assert _visible(db, "eida_num", roles={"restricted_reader"})


def test_confidential_floor_is_grantable(db):
    """`confidential` is in the governed vocabulary but had NO role that could grant it, so such a
    column was either invisible to everyone or (as shipped) visible to everyone."""
    _seal()
    _floored(db, "sender_ctry", sensitivity=None, floor="confidential")
    assert not _visible(db, "sender_ctry")
    assert _visible(db, "sender_ctry", roles={"confidential_reader"})


def test_prohibited_is_never_grantable(db):
    """`prohibited` is the fail-closed top of the floor vocabulary — including the rank an
    UNRECOGNIZED label collapses to. No role may unlock it."""
    _seal()
    _floored(db, "secret_col", sensitivity=None, floor="prohibited")
    for roles in (set(), {"pii_reader"}, {"restricted_reader"}, {"confidential_reader"},
                  {"pii_reader", "restricted_reader", "confidential_reader"}):
        assert not _visible(db, "secret_col", roles=roles), f"prohibited leaked to {roles}"


def test_the_raw_tag_still_gates_independently(db):
    """The floor is an ADDITION, not a replacement: a file-declared `pii` tag with no governed floor
    keeps its own gate, so this change can never widen what was already visible."""
    _seal()
    _floored(db, "cust_name", sensitivity="pii", floor=None)
    assert not _visible(db, "cust_name")
    assert _visible(db, "cust_name", roles={"pii_reader"})


def test_an_ungoverned_untagged_column_stays_visible(db):
    """The common case is unchanged — no tag, no floor, world-visible. Pins that the fix does not
    quietly hide the whole catalog."""
    _seal()
    _floored(db, "tran_amt", sensitivity=None, floor=None)
    assert _visible(db, "tran_amt")


def test_a_declared_tag_keeps_its_own_gate_and_the_floor_does_not_widen_it(db):
    """PRECEDENCE. `effective_restriction` is DERIVED from the raw tag (apply_sensitivity_floor
    resolves the concept floor together with the declared-sensitivity proposals), so the two are not
    independent evidence and requiring both would double-count. It would also break a shipped
    guarantee: a pii column whose floor resolves to `restricted` would start demanding
    `restricted_reader` too, and a caller who can see PII today would lose access.

    So a declared tag wins and the floor is a FALLBACK for untagged columns — which is precisely the
    leak's shape. Critically, the floor must never WIDEN a tagged column either: holding only
    `restricted_reader` must not unlock a pii-tagged column."""
    _seal()
    _floored(db, "mixed_col", sensitivity="pii", floor="restricted")
    assert _visible(db, "mixed_col", roles={"pii_reader"}), "the declared tag decides"
    assert not _visible(db, "mixed_col", roles={"restricted_reader"}), \
        "the floor must not widen a pii column to restricted_reader"
    assert not _visible(db, "mixed_col")


def test_prohibited_overrides_even_a_declared_tag(db):
    """The one case where the floor outranks the tag: nothing may unlock `prohibited`, so it wins
    over a tag that would otherwise be grantable."""
    _seal()
    _floored(db, "locked_col", sensitivity="pii", floor="prohibited")
    for roles in (set(), {"pii_reader"}, {"restricted_reader"}, {"confidential_reader"},
                  {"pii_reader", "restricted_reader", "confidential_reader"}):
        assert not _visible(db, "locked_col", roles=roles), f"prohibited leaked to {roles}"
