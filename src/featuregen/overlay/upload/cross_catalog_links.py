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

**Strength, because a wrong join is not a wrong label.** A mislabelled column misdescribes itself; a
wrong join corrupts numbers. Of nine real candidates only one is sound — the other eight pair a
branch code with a branch DESCRIPTION. So every link carries a strength derived from evidence
already stored, and a weak candidate is ranked down rather than hidden:

* a HUMAN confirmation is the strongest signal there is (and still not a precondition);
* a GRAIN on either side means the column really is that table's key, not merely a value that
  happens to share a type;
* an ATTESTED type match means the platform read the types, where ``declared`` means someone's
  spreadsheet said so.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class LinkStatus(StrEnum):
    #: A human has approved it. Ranked highest — never required.
    CONFIRMED = "confirmed"
    #: Derived and proposed, nobody has reviewed it. Fully usable.
    PROPOSED = "proposed"


#: Strength weights. Deliberately coarse — this orders a shortlist for a human or a planner, it does
#: not pretend to be a probability. Confirmation dominates every derived signal combined, so a
#: human's approval always sorts to the top without ever being a gate.
_W_CONFIRMED = 100
_W_GRAIN_SIDE = 10
_W_ATTESTED = 5


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
        """Always true. Present so a caller reads intent rather than inferring it from status —
        confirmation annotates, it does not gate."""
        return True

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


def cross_catalog_links(conn, *, object_ref: str | None = None
                        ) -> tuple[CrossCatalogLink, ...]:
    """Every cross-catalog link, strongest first. Read-only.

    ``object_ref`` narrows to links touching ONE column, matched on EITHER side — a link is
    symmetric, so opening the FTR side must find the same link the CIB side does.

    A candidate that has since been confirmed is returned ONCE, as confirmed: the ledger row and the
    verified edge are the same link at two stages of its life, not two links.
    """
    # Keyed by fact_key so a verified edge can be matched to its candidate row AND so an edge with
    # no candidate row is still returned. Some verified bridges are written straight to the edge
    # table; reading only the ledger would silently drop them, turning "show unconfirmed too" into a
    # regression that loses CONFIRMED links.
    # A human's NO must still count. The direction is "confirmation is not REQUIRED", not
    # "disapproval is ignored" — if a rejection did not suppress the link, a reviewer could never
    # say no and the governance surface would be decorative. STALE is suppressed for a different
    # reason: drift changed an endpoint, so the link may no longer hold and needs re-deriving.
    # Folded from the EVENT STREAM, not read off the projected table: the projection can lag, and a
    # link must stop being usable the moment the decision is made, not once a projector catches up.
    def _blocked(key: str | None) -> bool:
        if not key:
            return False
        from featuregen.overlay.state import fold_overlay_state
        from featuregen.overlay.store import load_fact
        try:
            return fold_overlay_state(load_fact(conn, key)).status in ("REJECTED", "STALE")
        except Exception:  # noqa: BLE001 — an unreadable fact must not blank the whole list
            return False
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
    rows = conn.execute(
        "SELECT entity_id, left_catalog_source, left_object_ref, right_catalog_source, "
        "       right_object_ref, fact_key, data_type_family, evidence_json "
        "FROM entity_bridge_candidate_evidence ORDER BY entity_id, left_object_ref").fetchall()

    out: list[CrossCatalogLink] = []
    for entity, l_src, l_ref, r_src, r_ref, key, family, ev in rows:
        if _blocked(key):
            continue
        if object_ref is not None:
            want = object_ref.strip().lower()
            if want not in (str(l_ref).lower(), str(r_ref).lower()):
                continue
        ev = ev if isinstance(ev, dict) else {}
        # Current graph OR the derivation-time flag — a column that has since become the grain
        # counts, and one recorded as grain then is not demoted by a read-scoped miss now.
        left_grain = (l_src, l_ref) in grain or bool(ev.get("left_is_grain"))
        right_grain = (r_src, r_ref) in grain or bool(ev.get("right_is_grain"))
        basis = str(ev.get("type_basis") or "")
        confirmed = key in verified
        out.append(CrossCatalogLink(
            entity_id=entity, left_catalog_source=l_src, left_object_ref=l_ref,
            right_catalog_source=r_src, right_object_ref=r_ref,
            status=LinkStatus.CONFIRMED if confirmed else LinkStatus.PROPOSED,
            strength=_strength(confirmed=confirmed, left_grain=left_grain,
                               right_grain=right_grain, basis=basis),
            data_type_family=family or "", left_is_grain=left_grain, right_is_grain=right_grain,
            type_basis=basis, fact_key=key))
    # Verified edges with no candidate row — never derived here, or derived before the ledger
    # existed. They are confirmed links and must not be lost.
    seen = {l.fact_key for l in out}
    for key, (entity, l_src, l_ref, r_src, r_ref) in verified.items():
        if key in seen or _blocked(key):
            continue
        if object_ref is not None and object_ref.strip().lower() not in (
                str(l_ref).lower(), str(r_ref).lower()):
            continue
        out.append(CrossCatalogLink(
            entity_id=entity, left_catalog_source=l_src, left_object_ref=l_ref,
            right_catalog_source=r_src, right_object_ref=r_ref, status=LinkStatus.CONFIRMED,
            strength=_strength(confirmed=True, left_grain=False, right_grain=False, basis=""),
            data_type_family="", left_is_grain=False, right_is_grain=False, type_basis="",
            fact_key=key))
    out.sort(key=lambda l: (-l.strength, l.entity_id, l.left_object_ref))
    return tuple(out)
