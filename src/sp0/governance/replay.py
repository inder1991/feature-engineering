from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Mapping, Optional

from sp0.contracts import EventEnvelope
from sp0.events import load_stream

if TYPE_CHECKING:
    from sp0.contracts import DbConn


class ReplayMode(str, Enum):
    FULL = "full"
    PRIVACY_DEGRADED = "privacy-degraded"


@dataclass(frozen=True, slots=True)
class ArtifactReplayStatus:
    doc_id: str
    stage: str
    intact: bool
    degraded_reason: Optional[str] = None


@dataclass(frozen=True, slots=True)
class ReplayResult:
    run_id: str
    mode: ReplayMode
    events: tuple[EventEnvelope, ...]
    artifacts: tuple[ArtifactReplayStatus, ...]
    degraded_artifacts: tuple[str, ...]


def replay_run(
    conn: "DbConn",
    run_id: str,
    *,
    upto_seq: Optional[int] = None,
    expected: Optional[Mapping[str, int]] = None,
) -> ReplayResult:
    """Reconstruct a run's decision trail and label it full vs privacy-degraded (§8). The event
    skeleton + provenance are always reconstructable; a body whose blob is crypto-shredded makes
    that artifact (and the whole replay) privacy-degraded."""
    events = tuple(load_stream(conn, "run", run_id, upto_seq=upto_seq, expected=expected))

    if upto_seq is None:
        rows = conn.execute(
            "SELECT doc_id, stage, body_ref FROM documents WHERE run_id = %s ORDER BY global_seq",
            (run_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT doc_id, stage, body_ref FROM documents "
            "WHERE run_id = %s AND global_seq <= %s ORDER BY global_seq",
            (run_id, upto_seq),
        ).fetchall()

    artifacts: list[ArtifactReplayStatus] = []
    degraded: list[str] = []
    for doc_id, stage, body_ref in rows:
        if body_ref is None:
            artifacts.append(ArtifactReplayStatus(doc_id=doc_id, stage=stage, intact=True))
            continue
        status_row = conn.execute(
            "SELECT status FROM blob_index WHERE blob_id = %s", (body_ref,)
        ).fetchone()
        status = status_row[0] if status_row is not None else "shredded"
        if status == "shredded":
            artifacts.append(ArtifactReplayStatus(
                doc_id=doc_id, stage=stage, intact=False, degraded_reason="body crypto-shredded"))
            degraded.append(doc_id)
        else:
            artifacts.append(ArtifactReplayStatus(doc_id=doc_id, stage=stage, intact=True))

    mode = ReplayMode.PRIVACY_DEGRADED if degraded else ReplayMode.FULL
    return ReplayResult(
        run_id=run_id,
        mode=mode,
        events=events,
        artifacts=tuple(artifacts),
        degraded_artifacts=tuple(degraded),
    )
