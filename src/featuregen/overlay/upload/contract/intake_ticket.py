"""The intake ticket — one mandatory read of the hypothesis, one cached structured answer.

The #2 spec's engine (router plan, owner-decided 2026-08-10): every new hypothesis gets ONE
extraction filling the full ticket — target column, label window, target type, business domain —
because the safety machinery downstream is only as good as this reading. The veto needs the target;
the near-label critic needs the window; the menu ordering needs the domain. An exact-name match
alone would silently drop the other three fields — the hole the owner caught in the staged design.

Discipline, in one line each:

* SELECTION, never generation — the model picks the target FROM the shortlist we send (ref +
  concept + one-line summary per column, all pre-classified egress keys) or abstains; a name not in
  the catalog is treated as abstain, never trusted.
* A literally-typed column PINS — code matches it before any model runs, the model cannot override
  it, and a prose-vs-name disagreement surfaces as a `contradiction` for the confirm screen.
* Content-keyed replay through `structured_result` — the key covers the hypothesis, the shortlist
  content, the use-case vocabulary and the prompt version (the second-review correction: "cached by
  hypothesis text" hashed one input of four).
* Failure degrades, never blocks — no client / fault / ceiling yields a ticket with the pinned
  target (if any) and honest abstains everywhere else.
* The human confirmation gate (B2) consumes this ticket: the ticket is a DRAFT reading,
  `llm/proposed` in spirit, until a person signs the target — and the signed reading lands on
  `contract_intent` via :func:`record_target_reading` (migration 1059), where the existing
  server-side leakage path already reads. Model drafts live in `structured_result`; human decisions
  live with the intent they govern.
"""
from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from featuregen.overlay.field_evidence import canonical_hash
from featuregen.overlay.upload.structured_results import (
    find_structured_result,
    record_structured_result,
)

logger = logging.getLogger(__name__)

INTAKE_TICKET_RESULT_TYPE = "intake_ticket"
INTAKE_TICKET_RESULT_VERSION = 1

INTAKE_TICKET_TASK = "overlay.contract.intake_ticket"
INTAKE_TICKET_PROMPT_ID = "intake_ticket"
INTAKE_TICKET_PROMPT_VERSION = 1
INTAKE_TICKET_SCHEMA_ID = "intake_ticket"
INTAKE_TICKET_RUN_ID = "intake-ticket"

_TARGET_TYPES = ("binary_classification", "regression", "multiclass", "abstain")

_INSTRUCTION = (
    "Read the analyst's objective and produce the intake ticket. `target_ref`: the ONE candidate "
    "ref that is the prediction target — copy it EXACTLY from the candidates list, or \"\" if no "
    "candidate is the target or you cannot tell. Never invent a ref. `target_window_days`: the "
    "label window in days when the text states one (\"churn = no activity in 90 days\" -> 90); 0 "
    "when not stated. `target_type`: what kind of prediction this is, or \"abstain\". "
    "`business_domain`: every vocabulary entry that matches the question's business area — copy "
    "tokens exactly from the vocabulary list; empty if none fit. `confidence`: \"high\" only when "
    "the text names its target unambiguously; \"abstain\" when you are guessing."
)

_WORD_RE = re.compile(r"[a-z0-9_]+")


@dataclass(frozen=True, slots=True)
class IntakeTicketV1:
    """The structured reading of one hypothesis. `target_column` is a validated graph ref or None;
    `pinned` means code matched a literally-typed name (the model could not have overridden it);
    `contradiction` carries the confirm screen's warning when the model's prose reading disagreed
    with a pinned name. Every field the model could not honestly fill is None/abstain/() — absence
    is the signal, never a guess."""

    target_column: str | None
    target_window_days: int | None
    target_type: str                       # one of _TARGET_TYPES
    business_domain: tuple[str, ...]       # ⊆ the recipe use-case/family vocabulary
    confidence: str                        # "high" | "medium" | "abstain"
    pinned: bool
    contradiction: str | None


def _use_case_vocabulary() -> tuple[str, ...]:
    """The closed relevance vocabulary: every `use_cases` token ∪ every `family` across the recipe
    registry. Deferred import — the registry is heavy and this module must import light."""
    from featuregen.overlay.upload.templates import ALL_TEMPLATES

    vocab: set[str] = set()
    for template in ALL_TEMPLATES:
        vocab.update(template.use_cases)
        vocab.add(template.family)
    return tuple(sorted(vocab))


def _shortlist(conn, catalog_source: str | None, roles: Iterable[str]) -> list[dict]:
    """The shelf photo: ref + concept + one-line summary per READ-SCOPED column. At today's catalog
    sizes the shortlist is the whole catalog; at scale a search-derived subset takes its place
    (stage 3 of the spec) — same shape, fewer rows."""
    from featuregen.intake.redaction import redact_free_text as _scan
    from featuregen.overlay.upload.read_scope import allowed_sensitivities

    rows = conn.execute(
        "SELECT catalog_source, object_ref, concept, ai_summary FROM graph_node "
        "WHERE kind = 'column' AND (%(src)s::text IS NULL OR catalog_source = %(src)s) "
        "  AND visible_requires <@ %(allowed)s ORDER BY catalog_source, object_ref",
        {"src": catalog_source, "allowed": allowed_sensitivities(roles)}).fetchall()

    def _prose(text: str | None) -> str:
        # The `candidates` egress class makes the CALLER the owning scanner (tie_break's
        # precedent): summaries are prose and get the PII scan; a fail-closed None blanks the
        # field rather than the payload. Runs BEFORE hashing, so the key sees what egresses.
        if not text:
            return ""
        return _scan(text).text or ""

    return [{"ref": r[1], "concept": r[2] or "", "ai_summary": _prose(r[3])} for r in rows]


def _exact_pin(hypothesis: str, shortlist: Sequence[dict]) -> str | None:
    """Stage 1, pure code: a literally-typed column name pins the target. Case/underscore-normalised
    word match; a name colliding across catalogs does NOT pin (the spec's collision rule — a pin
    that guesses between catalogs is not a pin)."""
    words = set(_WORD_RE.findall(hypothesis.lower()))
    matches: dict[str, set[str]] = {}
    for entry in shortlist:
        column_name = entry["ref"].rsplit(".", 1)[-1].lower()
        if column_name in words:
            matches.setdefault(column_name, set()).add(entry["ref"])
    pinned = {name: refs for name, refs in matches.items() if len(refs) == 1}
    if len(pinned) != 1:
        return None            # nothing typed, ambiguous across catalogs, or several names typed
    (refs,) = pinned.values()
    (ref,) = refs
    return ref


def _input_hash(*, hypothesis: str, shortlist: Sequence[dict],
                vocabulary: Sequence[str]) -> str:
    """ALL FOUR inputs, per the second-review correction — a vocabulary rename or a column
    re-enrichment must re-ask, not serve a stale ticket."""
    return canonical_hash({
        "version": "intake-ticket-input-v1",
        "prompt_id": INTAKE_TICKET_PROMPT_ID,
        "prompt_version": INTAKE_TICKET_PROMPT_VERSION,
        "hypothesis": hypothesis,
        "shortlist": list(shortlist),
        "vocabulary": list(vocabulary),
    })


def _degraded(pin: str | None) -> IntakeTicketV1:
    return IntakeTicketV1(target_column=pin, target_window_days=None, target_type="abstain",
                          business_domain=(), confidence="abstain", pinned=pin is not None,
                          contradiction=None)


def _ticket_from_output(output: dict, *, pin: str | None,
                        shortlist_refs: set[str], vocabulary: set[str]) -> IntakeTicketV1:
    """Validate the model's reading into a ticket. Everything is checked against a closed set: an
    off-shortlist target is ABSTAIN (never trusted — the veto must not guard an empty room);
    off-vocabulary domains are dropped; a pinned name always wins, with a disagreement kept as the
    confirm screen's contradiction warning."""
    raw_target = output.get("target_ref")
    model_target = raw_target if (isinstance(raw_target, str)
                                  and raw_target in shortlist_refs) else None
    invented = bool(raw_target) and model_target is None
    contradiction = None
    if pin is not None:
        target = pin
        if model_target is not None and model_target != pin:
            contradiction = (f"you named {pin.rsplit('.', 1)[-1]}; the description reads as "
                            f"{model_target.rsplit('.', 1)[-1]}")
    else:
        target = model_target
    window = output.get("target_window_days")
    window = window if isinstance(window, int) and window > 0 else None
    target_type = output.get("target_type")
    target_type = target_type if target_type in _TARGET_TYPES else "abstain"
    domains = output.get("business_domain")
    domains = tuple(d for d in domains if d in vocabulary) if isinstance(domains, list) else ()
    confidence = output.get("confidence")
    if invented or (target is None and not pin):
        confidence = "abstain"
    elif confidence not in ("high", "medium", "abstain"):
        confidence = "abstain"
    return IntakeTicketV1(target_column=target, target_window_days=window,
                          target_type=target_type, business_domain=domains,
                          confidence=confidence, pinned=pin is not None,
                          contradiction=contradiction)


_PROVENANCES = ("human_confirmed", "user_typed", "exploring")


def record_target_reading(conn, *, intent_id: str, provenance: str, target_ref: str | None = None,
                          target_window_days: int | None = None, target_type: str | None = None,
                          business_domain: Sequence[str] = (),
                          confirmed_by: str | None = None) -> bool:
    """Persist the SIGNED reading onto the intent it governs (migration 1059 — the storage
    decision's human half; the model's draft stays in ``structured_result``). Provenance is closed:
    ``human_confirmed`` (fuzzy path, a person clicked), ``user_typed`` (the person literally named
    the column — human-origin by construction, recorded without a click), ``exploring`` (an explicit
    no-target declaration; ``target_ref`` is forced NULL so the veto downstream honestly has nothing
    to guard and near-label withholding can say why). Returns False when the intent does not exist —
    the caller owns the 404."""
    if provenance not in _PROVENANCES:
        raise ValueError(f"unknown target provenance: {provenance!r}")
    if provenance == "exploring":
        target_ref = None
    row = conn.execute(
        "UPDATE contract_intent SET target_ref = %s, target_window_days = %s, target_type = %s, "
        "business_domain = %s::jsonb, target_provenance = %s, target_confirmed_by = %s, "
        "target_confirmed_at = now() WHERE intent_id = %s RETURNING intent_id",
        (target_ref, target_window_days, target_type,
         json.dumps(sorted(business_domain)), provenance, confirmed_by, intent_id)).fetchone()
    return row is not None


def target_reading(conn, intent_id: str) -> dict | None:
    """The recorded reading, for consumers beyond the leakage gate (which keeps its own
    ``intent_target_ref``): the near-label critic reads the window, the menu ordering reads the
    domain. None when the intent is unknown; NULL fields mean "not declared", never a default."""
    row = conn.execute(
        "SELECT target_ref, target_window_days, target_type, business_domain, target_provenance, "
        "target_confirmed_by FROM contract_intent WHERE intent_id = %s", (intent_id,)).fetchone()
    if row is None:
        return None
    return {"target_ref": row[0], "target_window_days": row[1], "target_type": row[2],
            "business_domain": tuple(row[3] or ()), "target_provenance": row[4],
            "target_confirmed_by": row[5]}


def is_readable_column(conn, ref: str, *, roles: Iterable[str],
                       catalog_source: str | None = None) -> bool:
    """Membership check for a human-supplied target: a READ-SCOPED column node with this ref exists.
    The confirm route validates against the catalog, not against the ticket — the human may correct
    to any real column, and a column the confirmer cannot see cannot be their target."""
    from featuregen.overlay.upload.read_scope import allowed_sensitivities

    return conn.execute(
        "SELECT 1 FROM graph_node WHERE kind = 'column' AND object_ref = %(ref)s "
        "AND (%(src)s::text IS NULL OR catalog_source = %(src)s) "
        "AND visible_requires <@ %(allowed)s LIMIT 1",
        {"ref": ref, "src": catalog_source,
         "allowed": allowed_sensitivities(roles)}).fetchone() is not None


def extract_intake_ticket(conn, client, *, hypothesis: str, catalog_source: str | None = None,
                          roles: Iterable[str] = (), actor=None,
                          call_ledger=None) -> tuple[IntakeTicketV1, str]:
    """The mandatory read. Returns ``(ticket, reason)`` with closed reasons: ``replayed`` (cached),
    ``extracted`` (fresh, stored), ``unavailable`` (no client / fault — degraded ticket, pinned
    target survives), ``call_ceiling``. The model output is STORED verbatim (including honest
    abstains); validation runs on every read as well as on first extraction, so a shortlist change
    re-validates a replayed ticket too."""
    shortlist = _shortlist(conn, catalog_source, roles)
    shortlist_refs = {e["ref"] for e in shortlist}
    vocabulary = _use_case_vocabulary()
    pin = _exact_pin(hypothesis, shortlist)
    key = _input_hash(hypothesis=hypothesis, shortlist=shortlist, vocabulary=vocabulary)

    stored = find_structured_result(
        conn, result_type=INTAKE_TICKET_RESULT_TYPE,
        result_version=INTAKE_TICKET_RESULT_VERSION, input_content_hash=key)
    if stored is not None:
        return _ticket_from_output(dict(stored.output), pin=pin,
                                   shortlist_refs=shortlist_refs,
                                   vocabulary=set(vocabulary)), "replayed"
    if client is None:
        return _degraded(pin), "unavailable"
    if call_ledger is not None and not call_ledger.charge():
        return _degraded(pin), "call_ceiling"

    from featuregen.overlay.upload.contract.intake import redact_free_text
    from featuregen.overlay.upload.enrich_llm import drive_audited_structured_call

    redacted = redact_free_text(hypothesis, label="hypothesis")
    try:
        call = drive_audited_structured_call(
            conn, client, task=INTAKE_TICKET_TASK,
            prompt_id=f"{INTAKE_TICKET_PROMPT_ID}_v{INTAKE_TICKET_PROMPT_VERSION}",
            schema_id=INTAKE_TICKET_SCHEMA_ID,
            # Every key pre-classified on the egress seam: `objective` is the round-trip prose
            # class the generation call's own hypothesis rides; `candidates` is the
            # structural-with-owned-scanning class (summaries are already-graded enrichment
            # output); `vocabulary` is the classifier's own closed-list class.
            catalog_metadata={"objective": redacted, "candidates": shortlist,
                              "vocabulary": list(vocabulary)},
            instruction=_INSTRUCTION, actor=actor, run_id=INTAKE_TICKET_RUN_ID,
            record_egress_block=True)
    except Exception:  # noqa: BLE001 — mandatory to ATTEMPT, never load-bearing
        logger.warning("intake-ticket extraction failed; degrading to the pinned/abstain ticket",
                       exc_info=True)
        return _degraded(pin), "unavailable"
    if call.output is None:
        return _degraded(pin), "unavailable"
    ticket = _ticket_from_output(dict(call.output), pin=pin, shortlist_refs=shortlist_refs,
                                 vocabulary=set(vocabulary))
    record_structured_result(
        conn, result_type=INTAKE_TICKET_RESULT_TYPE,
        result_version=INTAKE_TICKET_RESULT_VERSION, input_content_hash=key,
        output=dict(call.output), producer_kind="llm_call",
        producer_ref=call.llm_call_ref or "intake_ticket:unrecorded")
    return ticket, "extracted"
