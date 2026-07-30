"""Cross-catalog links — the SAME business entity in two catalogs, confirmed or not.

The one read model every consumer should use: the asset screen, feature generation, and the data
agents. It returns candidates AND verified edges together, each carrying its own status, so a caller
RANKS rather than being barred.

**Why this exists.** The platform gated hard on confirmation. ``entity_bridge_edge`` holds VERIFIED
bridges only and is the only thing ``planner/multisource_compile``, ``analysis/grounding`` and
``contract/invalidation`` read — while ``entity_bridge_candidate_evidence``, where every derived
candidate lands, had ZERO readers anywhere in the codebase. Nine real candidates, including
``cib.cust_num <-> ftr.cif_id``, sat in the database invisible to every consumer and to the screen.

Owner's direction: *"we should join irrespective of the confirmation, confirmation can mark it human
approved but it shouldnt stop from showing on ui and consuming it in feature generations and data
agents."* Confirmation is an ANNOTATION, never a precondition.

**Three independent concepts, kept independent.** Collapsing them is how a human's review queue ends
up deciding whether numbers can be computed:

1. **Link availability** — may the platform consider this link at all? Decided by the bridge's
   governed lifecycle (``AVAILABLE_STATUSES``).
2. **Automatic execution safety** — has the platform validated what this join does to the data
   (today: grain and type basis; in time: direction, cardinality, scope, fan-out)? Decided by
   evidence the platform derived for itself.
3. **Human review** — does someone endorse the semantic relationship? Reported as CONFIRMED, and
   read from the same governed lifecycle fold (``REVIEWED_STATUS``) — never from the presence of a
   row in the ``entity_bridge_edge`` PROJECTION, which lags the decision it caches and whose
   demotion is fail-soft.

**Human review controls neither of the first two.** Confirmation means "a person agrees with this
semantic relationship", not "permission to execute": it never gates availability, and it never
outranks a measurement.

**Strength, because a wrong join is not a wrong label.** A mislabelled column misdescribes itself; a
wrong join corrupts numbers. Of nine real candidates only one is sound — the other eight pair a
branch code with a branch DESCRIPTION. So every link carries a strength derived from evidence
already stored, and a weak candidate is ranked down rather than hidden:

* a GRAIN on either side means the column really is that table's key, not merely a value that
  happens to share a type;
* an ATTESTED type match means the platform read the types, where ``declared`` means someone's
  spreadsheet said so;
* a HUMAN confirmation breaks a tie between links the platform cannot otherwise separate — it can
  never lift a link past a deterministically safer one.
"""
from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum

from featuregen.overlay.identity import CatalogObjectRef, canonical_bridge_endpoints

logger = logging.getLogger(__name__)


class LinkStatus(StrEnum):
    #: A human has endorsed the semantic relationship. An annotation — never required, and never
    #: enough on its own to outrank a link the platform measured to be safer.
    CONFIRMED = "confirmed"
    #: Derived and proposed, nobody has reviewed it. Fully usable.
    PROPOSED = "proposed"


#: The governed lifecycle states in which the platform may CONSIDER a link. An ALLOW-LIST: a state
#: this module does not know — and a state it cannot READ — is unavailable, never quietly usable.
#:
#:     DRAFT / PARTIALLY_CONFIRMED -> available, unreviewed
#:     VERIFIED                    -> available, human-reviewed
#:     REJECTED / STALE / REVERIFY -> unavailable
#:
#: REVERIFY is the state that leaked: ``OVERLAY_FACT_EXPIRED`` folds to REVERIFY (``overlay/state.py``),
#: which the previous deny-list of ("REJECTED", "STALE") did not name, so an EXPIRED bridge stayed
#: available. REJECTED is a human's NO — "confirmation is not REQUIRED" was never "disapproval is
#: ignored". STALE means drift moved an endpoint, so the link has to be re-derived before it holds.
AVAILABLE_STATUSES = frozenset({"DRAFT", "PARTIALLY_CONFIRMED", "VERIFIED"})

#: The ONE lifecycle status that means a person has endorsed the semantics. Review status is read
#: from the FOLD, never from the presence of a projection row — see :func:`bridge_lifecycle`.
REVIEWED_STATUS = "VERIFIED"

#: Pseudo-statuses for the two ways a lifecycle fails to resolve. Neither is in the allow-list.
UNGOVERNED = "UNGOVERNED"   #: readable, but there is no governance record for this link at all
UNREADABLE = "UNREADABLE"   #: the governance record could not be read — fail CLOSED, never open

#: Strength weights. Deliberately coarse — this orders a shortlist for a human or a planner, it does
#: not pretend to be a probability.
#:
#: AUTOMATIC SAFETY RANKS FIRST. ``_W_GRAIN_SIDE`` and ``_W_ATTESTED`` are things the platform
#: MEASURED about the data. ``_W_CONFIRMED`` is an opinion about the semantics, and is deliberately
#: smaller than the smallest safety increment, so the whole endorsement bonus cannot carry a link
#: into the next safety band: it breaks ties WITHIN a band and does nothing else. It used to be 100
#: against a grain side's 10, which let a human endorsing a type-only match outrank an unreviewed
#: grain-backed, attested link — opinion overruling measurement. ``_sort_key`` states the same
#: ordering STRUCTURALLY, so re-inflating this weight still cannot reorder the safety bands.
_W_GRAIN_SIDE = 10
_W_ATTESTED = 5
_W_CONFIRMED = 1


@dataclass(frozen=True)
class CrossCatalogLink:
    """One link between two catalogs, with everything a caller needs to rank and explain it."""

    entity_id: str
    left_catalog_source: str
    left_object_ref: str
    right_catalog_source: str
    right_object_ref: str
    status: LinkStatus
    strength: int
    data_type_family: str
    left_is_grain: bool
    right_is_grain: bool
    type_basis: str
    fact_key: str | None

    @property
    def usable(self) -> bool:
        """Always true for a link that is RETURNED — availability is decided before construction,
        by the lifecycle allow-list, and never by ``status``. Present so a caller reads intent
        rather than inferring it: confirmation annotates, it does not gate."""
        return True

    @property
    def safety(self) -> int:
        """What the PLATFORM measured about this join, with no human opinion in it. The primary sort
        key: an endorsement may reorder links inside a safety band, never across bands."""
        return (
            (_W_GRAIN_SIDE if self.left_is_grain else 0)
            + (_W_GRAIN_SIDE if self.right_is_grain else 0)
            + (_W_ATTESTED if self.type_basis == "attested" else 0)
        )

    @property
    def why(self) -> str:
        """The ranking, in words, for a human deciding whether to trust the link."""
        parts = []
        if self.status is LinkStatus.CONFIRMED:
            parts.append("approved by a person")
        if self.left_is_grain or self.right_is_grain:
            parts.append("one side is its table's key")
        else:
            parts.append("neither side is a key — types match but this may not be a real join")
        parts.append("types read from the data" if self.type_basis == "attested"
                     else "types as declared in the source file")
        return "; ".join(parts)


def _strength(*, confirmed: bool, left_grain: bool, right_grain: bool, basis: str) -> int:
    return (
        (_W_CONFIRMED if confirmed else 0)
        + (_W_GRAIN_SIDE if left_grain else 0)
        + (_W_GRAIN_SIDE if right_grain else 0)
        + (_W_ATTESTED if basis == "attested" else 0)
    )


def _sort_key(link: CrossCatalogLink) -> tuple:
    """Safety first, endorsement second, then a stable tie-break. Stated structurally rather than
    left to arithmetic: even if ``_W_CONFIRMED`` were re-inflated, a confirmed weak link still could
    not sort above an unreviewed safer one. ``fact_key`` closes the ordering: two links agreeing on
    every visible field would otherwise be left in whatever order the database returned them."""
    return (-link.safety, -link.strength, link.entity_id, link.left_object_ref,
            link.fact_key or "")


# ── the candidate ledger, merged to ONE record per fact_key ──────────────────────────────────────
#
# ``entity_bridge_candidate_evidence``'s primary key is the ORDERED five-tuple, while a bridge's
# identity is its (orientation-free) ``fact_key``. So a bridge written with its endpoints swapped —
# which the write side allowed before bridge identity was canonicalized — sits in the table as TWO
# rows under ONE fact_key, and on the live catalog those two rows CONTRADICTED each other
# (``text``/``attested`` against ``uuid``/``declared``).
#
# Reading is therefore a MERGE, never a pick. A pick is the worse failure: it answers confidently
# with one of two contradictory readings, is indistinguishable from a considered answer, and changes
# with the order rows happen to come back in.

#: How strongly a `type_basis` claims the type match was established. ``attested`` = the platform
#: read the physical types; ``declared`` = a glossary file's own answer; anything else claims
#: nothing. Used to take the WEAKEST reading when rows disagree.
_BASIS_STRENGTH = {"attested": 2, "declared": 1}


@dataclass(frozen=True, slots=True)
class LedgerEvidenceV1:
    """The derivation evidence for ONE bridge — every ledger row for its ``fact_key``, read in the
    canonical orientation and merged."""

    entity_id: str
    left_catalog_source: str
    left_object_ref: str
    right_catalog_source: str
    right_object_ref: str
    data_type_family: str
    type_basis: str
    left_is_grain: bool
    right_is_grain: bool


def _endpoint(catalog_source, object_ref) -> CatalogObjectRef:
    """A ledger endpoint as the typed ref the ONE ordering rule is defined over. ``object_ref`` is
    ``schema.table.column`` (what ``bridge_propose`` writes); ``object_kind`` is 'column' for both
    endpoints of a bridge and so never decides the order."""
    parts = str(object_ref or "").split(".", 2)
    schema, table, column = (parts + ["", ""])[:3]
    return CatalogObjectRef(str(catalog_source or ""), "column", schema, table, column)


def _weakest_basis(bases: Iterable[str]) -> str:
    """The least confident reading among rows that disagree — never the most confident one. A
    contradicted ``attested`` ranks the link DOWN (it loses ``_W_ATTESTED``), which is the direction
    an unresolved disagreement should move a join in. Ties break on sorted order, so the result does
    not depend on which row was read first."""
    return min(sorted({str(b or "") for b in bases}), key=lambda b: _BASIS_STRENGTH.get(b, 0))


def ledger_evidence_by_fact_key(conn) -> dict[str, LedgerEvidenceV1]:
    """Every governed bridge's derivation evidence, ONE record per ``fact_key``.

    Rows are re-read in the canonical orientation before being merged, so a per-side flag is never
    credited to the wrong endpoint. The merge itself is commutative — OR, all-agree, weakest — so
    the answer does not depend on insert order, and the query is ordered as well so nothing rests on
    that alone.

    Rows with a NULL ``fact_key`` are excluded: ``entity_bridge_candidate_evidence.fact_key`` is
    NULLable, so a ledger row can name no governed fact, and under the lifecycle allow-list there is
    nothing to read and therefore nothing to allow (:func:`bridge_lifecycle` already resolves them
    ``UNGOVERNED``).
    """
    rows = conn.execute(
        "SELECT fact_key, entity_id, left_catalog_source, left_object_ref, right_catalog_source, "
        "       right_object_ref, data_type_family, evidence_json "
        "FROM entity_bridge_candidate_evidence WHERE fact_key IS NOT NULL "
        "ORDER BY fact_key, entity_id, left_catalog_source, left_object_ref").fetchall()
    grouped: dict[str, list[tuple]] = {}
    for key, entity, l_src, l_ref, r_src, r_ref, family, ev in rows:
        ev = ev if isinstance(ev, dict) else {}
        left, right = (l_src, l_ref), (r_src, r_ref)
        flags = (bool(ev.get("left_is_grain")), bool(ev.get("right_is_grain")))
        typed_left = _endpoint(*left)
        if canonical_bridge_endpoints(typed_left, _endpoint(*right))[0] is not typed_left:
            left, right = right, left
            flags = (flags[1], flags[0])
        grouped.setdefault(key, []).append(
            (str(entity or ""), *left, *right, str(family or ""),
             str(ev.get("type_basis") or ""), *flags))
    return {key: _merge_ledger_rows(key, group) for key, group in grouped.items()}


def _merge_ledger_rows(key: str, group: list[tuple]) -> LedgerEvidenceV1:
    ordered = sorted(group)
    entity, l_src, l_ref, r_src, r_ref = ordered[0][:5]
    families = sorted({row[5] for row in ordered})
    if len(families) > 1:
        # Deterministic, and SAID rather than hidden: two derivations disagreeing about what type
        # this join is made of is a real defect in the catalog, not a display detail.
        logger.warning("bridge %s has contradictory data_type_family across ledger rows (%s) — "
                       "reporting %s", key, ", ".join(families), families[0])
    return LedgerEvidenceV1(
        entity_id=entity, left_catalog_source=l_src, left_object_ref=l_ref,
        right_catalog_source=r_src, right_object_ref=r_ref,
        data_type_family=families[0],
        type_basis=_weakest_basis(row[6] for row in ordered),
        # A grain is positive evidence ABOUT A COLUMN. Once the rows are read in one orientation,
        # one row knowing an endpoint is its table's key is not cancelled by another not knowing it.
        left_is_grain=any(row[7] for row in ordered),
        right_is_grain=any(row[8] for row in ordered))


@dataclass(frozen=True, slots=True)
class BridgeLifecycleV1:
    """ONE reading of a bridge's governed lifecycle: its folded ``status`` and the folded ``value``
    that status applies to. Returned together, from a SINGLE load-and-fold, because they are two
    halves of one observation — reading them through two folds lets a transition land in between and
    yields a status/value pair that never simultaneously existed."""

    status: str
    value: Mapping[str, object]


def bridge_lifecycle(conn, fact_key: str | None, *,
                     projected_verified: bool | None = None) -> BridgeLifecycleV1:
    """The bridge's governed lifecycle — one canonical status (with the value it governs), or
    ``UNGOVERNED`` / ``UNREADABLE``. The single place availability AND review status are decided,
    for every consumer, from ONE fold.

    Folded from the EVENT STREAM, not read off ``entity_bridge_edge``: the projection lags, holds
    VERIFIED rows only, and is a record of HUMAN REVIEW — none of which may decide whether the
    platform can consider a link, nor whether a review actually happened. A decision must take
    effect the moment it is made, not once a projector catches up.

    Three resolutions that are NOT the fold, each deliberate:

    * **no fact_key** -> ``UNGOVERNED``. ``entity_bridge_candidate_evidence.fact_key`` is NULLable,
      so a ledger row can name no governed fact. Under an allow-list there is nothing to read and so
      nothing to allow; ``active_bridges`` already dropped these, so this makes the read model agree
      with its own planner instead of advertising a link the planner will never cross.
    * **unreadable** -> ``UNREADABLE``, never available. The previous code returned "not blocked"
      here, i.e. served a link whose governance state it could not read — which also made a REJECTED
      bridge behind a failing read indistinguishable from a sound one. A blank list is a visible,
      diagnosable outage; silently serving ungoverned joins is not.
    * **empty stream + a VERIFIED projection row** -> VERIFIED. The one place absence is allowed,
      and only because the ROW ITSELF is a positive reading: ``project_verified_bridge`` writes it
      ONLY under a VERIFIED fold and ``demote_bridge_edges`` removes it on every exit from VERIFIED.
      ``multisource_gold`` and the planner fixtures seed bridges this way. An empty stream with no
      such row is ``UNGOVERNED`` — absence of a governance record is not evidence of a benign one.
      This is the ONLY use the projection has here, and it is a fallback for an ABSENT stream: a row
      can never override a stream that folded.

    ``projected_verified`` lets a caller that already knows the projection state pass it in rather
    than provoke a per-link query; ``None`` means "look it up".
    """
    if not fact_key:
        return BridgeLifecycleV1(UNGOVERNED, {})
    from featuregen.overlay import store
    from featuregen.overlay.state import fold_overlay_state
    try:
        folded = fold_overlay_state(store.load_fact(conn, fact_key))
    except Exception:  # noqa: BLE001 — fail CLOSED: an unreadable lifecycle is not an available link
        logger.warning("bridge lifecycle unreadable, treating as unavailable: %s", fact_key,
                       exc_info=True)
        return BridgeLifecycleV1(UNREADABLE, {})
    # Every UNAVAILABLE state moves the value to `prior_value`, so `value` is empty exactly where
    # the caller is about to discard the reading anyway.
    value = folded.value if isinstance(folded.value, Mapping) else {}
    if folded.status:
        return BridgeLifecycleV1(folded.status, value)
    if projected_verified is None:
        try:
            projected_verified = conn.execute(
                "SELECT 1 FROM entity_bridge_edge WHERE fact_key = %s AND status = 'VERIFIED'",
                (fact_key,)).fetchone() is not None
        except Exception:  # noqa: BLE001 — same fail-closed rule for the fallback read
            logger.warning("bridge projection unreadable, treating as unavailable: %s", fact_key,
                           exc_info=True)
            return BridgeLifecycleV1(UNREADABLE, {})
    return BridgeLifecycleV1(REVIEWED_STATUS if projected_verified else UNGOVERNED, value)


def bridge_lifecycle_status(conn, fact_key: str | None, *,
                            projected_verified: bool | None = None) -> str:
    """:func:`bridge_lifecycle`'s status alone, for the callers that need nothing else."""
    return bridge_lifecycle(conn, fact_key, projected_verified=projected_verified).status


def bridge_is_available(conn, fact_key: str | None, *,
                        projected_verified: bool | None = None) -> bool:
    """Whether the platform may consider this link — the allow-list, applied. Human review is not
    consulted: a DRAFT bridge is as available as a VERIFIED one."""
    return bridge_lifecycle_status(
        conn, fact_key, projected_verified=projected_verified) in AVAILABLE_STATUSES


def cross_catalog_links(conn, *, object_ref: str | None = None
                        ) -> tuple[CrossCatalogLink, ...]:
    """Every cross-catalog link, strongest first. Read-only.

    ``object_ref`` narrows to links touching ONE column, matched on EITHER side — a link is
    symmetric, so opening the FTR side must find the same link the CIB side does.

    A candidate that has since been confirmed is returned ONCE, as confirmed: the ledger row and the
    verified edge are the same link at two stages of its life, not two links. Exactly ONE link per
    ``fact_key`` is returned, in the canonical orientation, whatever order the rows behind it were
    written in — a bridge is one link however many rows describe it.
    """
    # Keyed by fact_key so a verified edge can be matched to its candidate row AND so an edge with
    # no candidate row is still returned. Some verified bridges are written straight to the edge
    # table; reading only the ledger would silently drop them, turning "show unconfirmed too" into a
    # regression that loses CONFIRMED links.
    #
    # This table is the HUMAN-REVIEW projection, and it is used for exactly two things: ENUMERATING
    # links that have no candidate row, and (in `bridge_lifecycle`) standing in for a stream that
    # carries no events at all. It decides NEITHER availability NOR whether a review happened —
    # both come from the folded lifecycle. A projection is a cache of a decision, not the decision:
    # it lags, and `demote_bridge_edges` is fail-soft, so presence here proved neither that a
    # confirmation exists (a delayed projector had not written it yet) nor that it still stands (a
    # failed demotion left the row behind).
    verified = {
        r[0]: r[1:] for r in conn.execute(
            "SELECT fact_key, entity_id, left_catalog_source, left_object_ref, "
            "       right_catalog_source, right_object_ref "
            "FROM entity_bridge_edge WHERE status = 'VERIFIED'").fetchall()
        if r[0] is not None
    }
    # Grain read from the graph as it is NOW, not from `evidence_json` as it was at derivation.
    # The stored flag is frozen at derivation time, and on the live catalog every link scored 0 —
    # including `cust_num <-> cif_id`, where cust_num IS the table's grain — because the candidate
    # was derived before Pass B established the grain fact. A rank that can never improve as the
    # catalog is enriched is not a rank.
    grain = {
        (r[0], r[1]) for r in conn.execute(
            "SELECT catalog_source, object_ref FROM graph_node "
            "WHERE kind = 'column' AND is_grain").fetchall()
    }
    # ONE record per fact_key. The ledger can hold two rows for one bridge (its primary key is the
    # ORDERED endpoint tuple, a bridge's identity is not), and those two rows have been seen to
    # contradict each other — so they are MERGED here, in the canonical orientation, rather than
    # one of them being picked. See `ledger_evidence_by_fact_key`.
    out: list[CrossCatalogLink] = []
    for key, ev in sorted(ledger_evidence_by_fact_key(conn).items()):
        # ONE fold answers both questions, and neither is answered by the projection row.
        status = bridge_lifecycle_status(conn, key, projected_verified=key in verified)
        if status not in AVAILABLE_STATUSES:
            continue
        if object_ref is not None:
            want = object_ref.strip().lower()
            if want not in (ev.left_object_ref.lower(), ev.right_object_ref.lower()):
                continue
        # Current graph OR the derivation-time flag — a column that has since become the grain
        # counts, and one recorded as grain then is not demoted by a read-scoped miss now.
        left_grain = (ev.left_catalog_source, ev.left_object_ref) in grain or ev.left_is_grain
        right_grain = (ev.right_catalog_source, ev.right_object_ref) in grain or ev.right_is_grain
        confirmed = status == REVIEWED_STATUS
        out.append(CrossCatalogLink(
            entity_id=ev.entity_id, left_catalog_source=ev.left_catalog_source,
            left_object_ref=ev.left_object_ref, right_catalog_source=ev.right_catalog_source,
            right_object_ref=ev.right_object_ref,
            status=LinkStatus.CONFIRMED if confirmed else LinkStatus.PROPOSED,
            strength=_strength(confirmed=confirmed, left_grain=left_grain,
                               right_grain=right_grain, basis=ev.type_basis),
            data_type_family=ev.data_type_family, left_is_grain=left_grain,
            right_is_grain=right_grain, type_basis=ev.type_basis, fact_key=key))
    # Edge rows with no candidate row — never derived here, or derived before the ledger existed.
    # They must not be lost. Their REVIEW status is folded like every other link's, not assumed from
    # the row: a row whose stream has since fallen back to DRAFT is an available, unreviewed link.
    seen = {l.fact_key for l in out}
    for key, (entity, l_src, l_ref, r_src, r_ref) in verified.items():
        if key in seen:
            continue
        status = bridge_lifecycle_status(conn, key, projected_verified=True)
        if status not in AVAILABLE_STATUSES:
            continue
        if object_ref is not None and object_ref.strip().lower() not in (
                str(l_ref).lower(), str(r_ref).lower()):
            continue
        confirmed = status == REVIEWED_STATUS
        out.append(CrossCatalogLink(
            entity_id=entity, left_catalog_source=l_src, left_object_ref=l_ref,
            right_catalog_source=r_src, right_object_ref=r_ref,
            status=LinkStatus.CONFIRMED if confirmed else LinkStatus.PROPOSED,
            strength=_strength(confirmed=confirmed, left_grain=False, right_grain=False, basis=""),
            data_type_family="", left_is_grain=False, right_is_grain=False, type_basis="",
            fact_key=key))
    out.sort(key=_sort_key)
    return tuple(out)
