"""S1 — persisting the target-reading and feature-selection revisions (migration 1072).

**This is where the destructive UPDATE gains its guard.** C-B1 froze the types; this makes them
durable and puts the provenance rule on the WRITE path, which is the only place it can stop
anything. Recording a reading now APPENDS, and replacing a person's confirmed target with an
exploration declaration requires naming who accepted the loss.

**The legacy column is still written, deliberately.** ``contract_intent.target_ref`` and its
siblings are read by the shipped leakage path and by ``target_reading``'s consumers, so removing the
UPDATE in this stage would break readers that have nothing to do with revisions. The revision is the
AUTHORITATIVE record; the column is a projection of the newest one, and the day every reader moves
to :func:`current_target_reading` the column write can go. Writing both is honest as long as the
revision is the one that refuses — which it is.

**Legacy rows map with no loss.** :func:`migrate_legacy_reading` turns a stored
``contract_intent`` row into the revision it would have been, through
``map_legacy_provenance``: ``exploring`` becomes ``(EXPLORATION, None)``, and the ``None`` is
truthful rather than lossy because writing ``exploring`` into the provenance column is what
destroyed the declarer in the first place.
"""
from __future__ import annotations

from featuregen.overlay.upload.selection_revisions import (
    ExplorationTargetV1,
    FeatureSelectionRevisionV1,
    PredictionTargetV1,
    TargetModeV1,
    TargetProvenanceV1,
    TargetReadingRevisionV1,
    map_legacy_provenance,
    supersede_target_reading,
)

__all__ = [
    "current_target_reading",
    "migrate_legacy_reading",
    "read_target_reading_revision",
    "record_feature_selection",
    "record_target_reading_revision",
    "selections_for_reading",
]


def _insert(conn, revision: TargetReadingRevisionV1) -> TargetReadingRevisionV1:
    prediction = revision.reading if isinstance(revision.reading, PredictionTargetV1) else None
    conn.execute(
        "INSERT INTO target_reading_revision (revision_id, intent_id, mode, provenance, "
        "target_logical_ref, target_type, horizon_days, confirmed_by, supersedes_revision_id, "
        "acknowledged_human_loss_by, content_hash) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (revision.revision_id, revision.intent_id, revision.mode.value,
         revision.provenance.value if revision.provenance is not None else None,
         prediction.target_logical_ref if prediction else None,
         prediction.target_type if prediction else None,
         prediction.horizon_days if prediction else None,
         revision.confirmed_by, revision.supersedes_revision_id,
         revision.acknowledged_human_loss_by, revision.content_hash))
    return revision


def record_target_reading_revision(
    conn, *, revision_id: str, intent_id: str,
    reading: PredictionTargetV1 | ExplorationTargetV1,
    provenance: TargetProvenanceV1,
    confirmed_by: str | None = None,
    acknowledged_human_loss_by: str | None = None,
) -> TargetReadingRevisionV1:
    """Append a reading, superseding this intent's current one if it has any.

    Raises:
        ValueError: the current reading is a PREDICTION and this replaces it with an exploration
            declaration without naming who accepted the loss. That is the guard the shipped UPDATE
            path does not have — and cannot have, because it overwrites rather than appends.
    """
    current = current_target_reading(conn, intent_id)
    if current is None:
        return _insert(conn, TargetReadingRevisionV1(
            revision_id=revision_id, intent_id=intent_id, reading=reading,
            provenance=provenance, confirmed_by=confirmed_by,
            acknowledged_human_loss_by=acknowledged_human_loss_by))
    return _insert(conn, supersede_target_reading(
        current, revision_id=revision_id, reading=reading, provenance=provenance,
        confirmed_by=confirmed_by, acknowledged_human_loss_by=acknowledged_human_loss_by))


def _row_to_revision(row) -> TargetReadingRevisionV1:
    (revision_id, intent_id, mode, provenance, target_logical_ref, target_type, horizon_days,
     confirmed_by, supersedes, acknowledged) = row
    reading: PredictionTargetV1 | ExplorationTargetV1 = (
        PredictionTargetV1(target_logical_ref=target_logical_ref, target_type=target_type,
                           horizon_days=horizon_days)
        if mode == TargetModeV1.PREDICTION.value else ExplorationTargetV1())
    return TargetReadingRevisionV1(
        revision_id=revision_id, intent_id=intent_id, reading=reading,
        # A legacy exploration row has no declarer, and inventing one would manufacture an
        # attribution nobody made. The type permits it for exactly this case.
        provenance=TargetProvenanceV1(provenance) if provenance is not None else None,
        confirmed_by=confirmed_by, supersedes_revision_id=supersedes,
        acknowledged_human_loss_by=acknowledged)


_COLUMNS = ("revision_id, intent_id, mode, provenance, target_logical_ref, target_type, "
            "horizon_days, confirmed_by, supersedes_revision_id, acknowledged_human_loss_by")


def read_target_reading_revision(conn, revision_id: str) -> TargetReadingRevisionV1 | None:
    """One revision by id — the reading a generation was authorized under, still readable."""
    row = conn.execute(
        f"SELECT {_COLUMNS} FROM target_reading_revision WHERE revision_id = %s",
        (revision_id,)).fetchone()
    return None if row is None else _row_to_revision(row)


def current_target_reading(conn, intent_id: str) -> TargetReadingRevisionV1 | None:
    """The reading nothing supersedes — the head of this intent's chain.

    Found by ABSENCE of a successor rather than by newest timestamp: two readings recorded in the
    same transaction share a clock, and "newest by time" would then be a coin flip. The chain says
    which one is current, and a revision may be superseded at most once.
    """
    row = conn.execute(
        f"SELECT {_COLUMNS} FROM target_reading_revision r WHERE r.intent_id = %s "
        "AND NOT EXISTS (SELECT 1 FROM target_reading_revision s "
        "                WHERE s.supersedes_revision_id = r.revision_id)",
        (intent_id,)).fetchone()
    return None if row is None else _row_to_revision(row)


def record_feature_selection(
    conn, selection: FeatureSelectionRevisionV1,
) -> FeatureSelectionRevisionV1:
    """Append a selection. The link to its reading is append-only, like the reading itself."""
    conn.execute(
        "INSERT INTO feature_selection_revision (revision_id, target_reading_revision_id, "
        "considered_revision_id, option_id, decision_id, planning_request_hash, "
        "binding_plan_hash, content_hash) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
        (selection.revision_id, selection.target_reading_revision_id,
         selection.considered_revision_id, selection.option_id, selection.decision_id,
         selection.planning_request_hash, selection.binding_plan_hash, selection.content_hash))
    return selection


def selections_for_reading(conn, target_reading_revision_id: str
                           ) -> tuple[FeatureSelectionRevisionV1, ...]:
    """Every selection made under one reading, oldest first."""
    rows = conn.execute(
        "SELECT revision_id, target_reading_revision_id, considered_revision_id, option_id, "
        "decision_id, planning_request_hash, binding_plan_hash FROM feature_selection_revision "
        "WHERE target_reading_revision_id = %s ORDER BY recorded_at, revision_id",
        (target_reading_revision_id,)).fetchall()
    return tuple(FeatureSelectionRevisionV1(*row) for row in rows)


def migrate_legacy_reading(
    conn, *, intent_id: str, revision_id: str,
) -> TargetReadingRevisionV1 | None:
    """Turn a stored ``contract_intent`` row into the revision it would have been.

    Returns ``None`` when the intent has no recorded provenance — there is nothing to migrate, and
    inventing a reading for it would assert a decision nobody made.
    """
    row = conn.execute(
        "SELECT target_provenance, target_ref, target_type, target_window_days, "
        "target_confirmed_by FROM contract_intent WHERE intent_id = %s", (intent_id,)).fetchone()
    if row is None or not row[0]:
        return None

    stored_provenance, target_ref, target_type, window_days, confirmed_by = row
    mode, provenance = map_legacy_provenance(stored_provenance)

    if mode is TargetModeV1.EXPLORATION:
        reading: PredictionTargetV1 | ExplorationTargetV1 = ExplorationTargetV1()
    else:
        if not target_ref:
            # A prediction provenance with no ref is a legacy row the old schema permitted and the
            # type does not. Refusing is honest: there is no target to migrate, and inventing one
            # would put a column nobody chose into a governed reading.
            return None
        reading = PredictionTargetV1(
            target_logical_ref=target_ref, target_type=target_type or "unknown",
            horizon_days=int(window_days) if window_days else 1)

    return _insert(conn, TargetReadingRevisionV1(
        revision_id=revision_id, intent_id=intent_id, reading=reading, provenance=provenance,
        confirmed_by=confirmed_by))
