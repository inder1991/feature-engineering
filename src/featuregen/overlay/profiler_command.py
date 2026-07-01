"""The profiler service command (SP-1 design §6.6).

Houses `_run_profiler` — the service handler registered under `run_profiler` — and its profiler-only
preflight helper `_existing_proposal_fingerprint`. Both were lifted out of `commands.py`; `commands`
re-exports them (and references `_run_profiler` from `_OVERLAY_CATALOG`) so existing
`featuregen.overlay.commands` imports keep resolving.
"""
from __future__ import annotations

from featuregen.commands.registry import get_command
from featuregen.contracts import Command, CommandResult, DbConn
from featuregen.overlay._lifecycle import _NON_TERMINAL, _latest_proposed
from featuregen.overlay.catalog import current_catalog_adapter
from featuregen.overlay.identity import (
    CatalogObjectRef,
    display_object_ref,
    fact_key,
    proposal_fingerprint,
)
from featuregen.overlay.profiler import (
    ProfilerLimits,
    SchemaNotAllowedError,
    run_profiler_scan,
)
from featuregen.overlay.state import fold_overlay_state
from featuregen.overlay.store import load_fact
from featuregen.security.audit import record_denial


def _existing_proposal_fingerprint(conn: DbConn, fk: str) -> tuple[str | None, str | None]:
    """Return (folded status, latest-proposed fingerprint) for `fk` read from the AUTHORITATIVE
    event stream — NOT the asynchronous `overlay_proposal` projection. Reading
    the stream means the profiler's preflight sees exactly what `propose_fact` will, so it never
    writes evidence for a proposal that `propose_fact` would then deny under projection lag."""
    stream = load_fact(conn, fk)
    if not stream:
        return None, None
    state = fold_overlay_state(stream)
    proposed = _latest_proposed(stream)
    fp = proposed.payload.get("proposal_fingerprint") if proposed else None
    return state.status, fp


def _run_profiler(conn: DbConn, cmd: Command) -> CommandResult:
    """Service command (§6.6): run the deterministic profiler over `cmd.args["ref"]` and, for each
    candidate, write evidence and issue a `propose_fact`. Runs inside `execute_command`'s
    transaction, so `run_profiler_scan`'s `SET LOCAL statement_timeout` applies to this scan.

    Stream-based preflight: for each candidate the folded stream status decides
    BEFORE any evidence is written — a non-terminal fact (DRAFT/PARTIALLY_CONFIRMED/VERIFIED/
    REVERIFY/STALE) blocks any new proposal, and a REJECTED fact with the SAME
    `proposal_fingerprint` is sticky-skipped (fresh evidence alone never revives it). Skipping first
    guarantees no orphan evidence is left for a candidate `propose_fact` would deny."""
    ref = CatalogObjectRef(**dict(cmd.args["ref"]))
    limits = ProfilerLimits(allowed_schemas=frozenset(cmd.args.get("allowed_schemas", ())))
    adapter = current_catalog_adapter()
    # Run the SCAN phase under an in-code read-only guard (defense-in-depth for §5.2's
    # read-only DB role) so a stray write inside run_profiler_scan fails closed. The scan only
    # SELECTs, so a savepoint we immediately roll back loses nothing; the rollback also clears
    # `transaction_read_only = on` (it was SET LOCAL after the savepoint), restoring read-write for
    # the subsequent propose_fact write phase — preserving the intentional single-transaction design.
    # An off-allowlist target raises SchemaNotAllowedError; every other handler denial returns
    # a CommandResult, so catch it, record a §6.5 security-audit denial (authz_policy checks only
    # capability+kind, NOT the schema, so the handler must audit it) and return cleanly.
    conn.execute("SAVEPOINT profiler_readonly")
    conn.execute("SET LOCAL transaction_read_only = on")
    try:
        proposals = run_profiler_scan(conn, adapter, ref, limits=limits)
    except SchemaNotAllowedError as exc:
        conn.execute("ROLLBACK TO SAVEPOINT profiler_readonly")  # clears read-only -> audit can write
        record_denial(conn, cmd, str(exc))
        return CommandResult(
            accepted=False, aggregate_id=display_object_ref(ref), denied_reason=str(exc)
        )
    conn.execute("ROLLBACK TO SAVEPOINT profiler_readonly")  # clears read-only -> writes allowed again
    conn.execute("RELEASE SAVEPOINT profiler_readonly")

    propose = get_command("propose_fact")
    produced: list[str] = []
    for proposal in proposals:
        fk = fact_key(proposal.ref, proposal.fact_type, proposal.use_case)
        metrics = proposal.evidence_metrics
        # Compute the fingerprint the SAME way propose_fact will (proposed_value + profile_version +
        # thresholds), so the dedup below matches the fingerprint that would be appended.
        fingerprint = proposal_fingerprint(
            proposal.proposed_value,
            profile_version=metrics["profile_version"],
            thresholds=metrics["thresholds"],
        )
        status, existing_fp = _existing_proposal_fingerprint(conn, fk)
        # Preflight matching propose_fact's replacement semantics — skip BEFORE writing
        # evidence so the profiler never creates orphan evidence for a denied proposal.
        if status in _NON_TERMINAL:
            continue  # a non-terminal fact exists; propose_fact would deny ANY new proposal
        if status == "REJECTED" and existing_fp == fingerprint:
            continue  # sticky dedup: a rejected candidate is not re-proposed on identical value

        # Issue propose_fact using ITS exact arg contract: a live CatalogObjectRef under
        # "ref"; propose_fact derives the proposal_fingerprint itself from proposed_value +
        # profile_version + thresholds (matching `fingerprint` above). The evidence metric payload is
        # handed through under "evidence" so propose_fact mints the evidence row ATOMICALLY with the
        # accepted append — a denied proposal then never leaves orphan evidence. The preflight
        # skip gates above remain a cheap fast path, but correctness no longer depends on them.
        propose_cmd = Command(
            action="propose_fact",
            aggregate="overlay_fact",
            aggregate_id=fk,
            args={
                "ref": proposal.ref,
                "fact_type": proposal.fact_type,
                "use_case": proposal.use_case,
                "proposed_value": dict(proposal.proposed_value),
                "evidence": dict(metrics),
                "profile_version": metrics["profile_version"],
                "thresholds": metrics["thresholds"],
            },
            actor=cmd.actor,
            idempotency_key=f"profiler:{fk}:{fingerprint}",
        )
        result = propose(conn, propose_cmd)
        if not result.accepted:
            continue  # defensive: a concurrent change made the fact non-terminal — do not count it
        produced.extend(result.produced_event_ids)

    return CommandResult(
        accepted=True,
        aggregate_id=display_object_ref(ref),
        produced_event_ids=tuple(produced),
    )
