"""§11.3 — the method override: a verified, durable, expiring approval — never a request field.

The browser asks; THIS module verifies. An override naming a deterministic refusal that did not
happen is refused — otherwise "Try AI formula" is a client-chosen method with extra steps and a
longer audit trail. The strategy resolver consumes the recorded revision as an INPUT FACT
(`formula_strategy._decide`'s override branch, which already exists and until now had no writer):
the override changes the EVIDENCE, never the authority.
"""
from __future__ import annotations

from featuregen.canonical import jcs_sha256

__all__ = [
    "OverrideRefusalUnverified",
    "current_method_override",
    "request_method_override",
]

#: The one deterministic refusal an LLM retry answers. Closed here AND in the schema's CHECK.
OVERRIDABLE_REFUSALS = frozenset({"REVIEWED_BLUEPRINT_NOT_EXECUTABLE"})


class OverrideRefusalUnverified(ValueError):
    """The refusal the override names is not RECORDED — wrong draft, wrong candidate, wrong
    state, or wrong code. Naming which is the whole value of refusing here."""


def request_method_override(
    conn, *, considered_revision_id: str, option_id: str, refused_formula_draft_id: str,
    actor_subject: str, reason: str, llm_spend_authorization_id: str,
    expires_at: str,
) -> tuple[str, bool]:
    """Record one verified override. Returns ``(override_id, created)`` — idempotent on content.

    Raises:
        OverrideRefusalUnverified: the named draft does not carry the recorded deterministic
            refusal for THIS candidate. The server verifies; it never takes the browser's word.
    """
    if not reason.strip():
        raise ValueError("an override with no reason is an approval nobody can audit")

    # ▲ THE VERIFICATION — the entire point of §11.3. The draft must exist, be about this exact
    # candidate, be BLOCKED, and carry the overridable code among its recorded blockers.
    draft = conn.execute(
        "SELECT considered_revision_id, option_id, state, blockers "
        "FROM formula_draft WHERE formula_draft_id = %s",
        (refused_formula_draft_id,)).fetchone()
    if draft is None:
        raise OverrideRefusalUnverified(
            f"draft {refused_formula_draft_id!r} does not exist — there is no refusal to "
            f"override")
    if (draft[0], draft[1]) != (considered_revision_id, option_id):
        raise OverrideRefusalUnverified(
            f"draft {refused_formula_draft_id!r} is about candidate ({draft[0]!r}, {draft[1]!r}),"
            f" not ({considered_revision_id!r}, {option_id!r}) — a refusal recorded for one "
            f"candidate authorizes nothing about another")
    if draft[2] != "BLOCKED":
        raise OverrideRefusalUnverified(
            f"draft {refused_formula_draft_id!r} is {draft[2]!r}, not BLOCKED — the deterministic "
            f"refusal this override names did not happen")
    recorded = {b.get("code") for b in (draft[3] or ()) if isinstance(b, dict)}
    if not (recorded & OVERRIDABLE_REFUSALS):
        raise OverrideRefusalUnverified(
            f"draft {refused_formula_draft_id!r} is blocked by {sorted(recorded)!r} — none of "
            f"these is a deterministic instantiation refusal, so an LLM retry answers a question "
            f"nobody asked")
    refusal_code = sorted(recorded & OVERRIDABLE_REFUSALS)[0]

    # The spend authorization must be real and unexpired NOW — an override pointing at a spent or
    # expired ceiling would approve a purchase nothing can pay for.
    spend = conn.execute(
        "SELECT expires_at > now() FROM llm_spend_authorization_revision "
        "WHERE spend_authorization_id = %s", (llm_spend_authorization_id,)).fetchone()
    if spend is None or not spend[0]:
        raise OverrideRefusalUnverified(
            f"spend authorization {llm_spend_authorization_id!r} is "
            f"{'absent' if spend is None else 'expired'} — §11.2: overriding to the LLM "
            f"authorizes BUYING an answer, and there is no live ceiling to buy under")

    override_id = jcs_sha256({
        "considered_revision_id": considered_revision_id,
        "option_id": option_id,
        "refused_formula_draft_id": refused_formula_draft_id,
        "original_refusal_code": refusal_code,
        "requested_alternative": "LLM_AUTHORED",
        "actor_subject": actor_subject,
        "llm_spend_authorization_id": llm_spend_authorization_id,
        "expires_at": expires_at,
    })
    inserted = conn.execute(
        "INSERT INTO formula_method_override_revision (override_id, considered_revision_id, "
        "option_id, refused_formula_draft_id, original_refusal_code, requested_alternative, "
        "actor_subject, reason, llm_spend_authorization_id, expires_at) "
        "VALUES (%s, %s, %s, %s, %s, 'LLM_AUTHORED', %s, %s, %s, %s) "
        "ON CONFLICT (override_id) DO NOTHING RETURNING override_id",
        (override_id, considered_revision_id, option_id, refused_formula_draft_id,
         refusal_code, actor_subject, reason, llm_spend_authorization_id,
         expires_at)).fetchone()
    return override_id, inserted is not None


def current_method_override(
    conn, *, considered_revision_id: str, option_id: str,
) -> str | None:
    """The latest UNEXPIRED override for this candidate, or ``None``.

    Expiry compared IN POSTGRES (the W1 lesson: string-comparing timestamps failed open) —
    against ``clock_timestamp()``, not ``now()``: ``now()`` is frozen at TRANSACTION start, so a
    long-lived transactional caller would keep honouring an override that expired mid-transaction
    — failing open, in the one direction expiry exists to close. Expiry is load-bearing: a
    refusal ages — the blueprint may be fixed — and the correct answer then is the deterministic
    one, which an unexpiring override would quietly override.
    """
    row = conn.execute(
        "SELECT override_id FROM formula_method_override_revision "
        "WHERE considered_revision_id = %s AND option_id = %s "
        "AND expires_at > clock_timestamp() "
        "ORDER BY approved_at DESC LIMIT 1",
        (considered_revision_id, option_id)).fetchone()
    return None if row is None else row[0]
