"""Matrix clarify ("question") card payloads.

Presentation-only helpers for the Matrix adapter's ``send_clarify``. The card is one
``m.room.message`` whose ``body`` is exactly the gateway's plain-text clarify prompt (clients
without card support see no change), whose ``formatted_body`` is a compact question + ordered
list, and whose ``org.hermes.clarify`` content key carries the structured question for clients
that render native controls. Settling the card is an ``m.replace`` whose ``m.new_content``
carries the same key with a terminal ``state``.
"""

from __future__ import annotations

import html
import json
from typing import Any, Optional

CLARIFY_CONTENT_KEY = "org.hermes.clarify"
CLARIFY_PAYLOAD_VERSION = 1

# Keycaps 1-4 (clarify caps choices at 4) and the skip control.
CLARIFY_CHOICE_REACTIONS = tuple(f"{d}️⃣" for d in "1234")
CLARIFY_SKIP_REACTION = "⏭️"  # ⏭️
# Clients may send the keycap / skip glyph without the variation selector.
_REACTION_ALIASES = {f"{d}⃣": f"{d}️⃣" for d in "1234"} | {"⏭": CLARIFY_SKIP_REACTION}

STATE_PENDING = "pending"
STATE_ANSWERED = "answered"
STATE_SKIPPED = "skipped"
STATE_EXPIRED = "expired"
STATE_CANCELLED = "cancelled"
TERMINAL_STATES = frozenset({STATE_ANSWERED, STATE_SKIPPED, STATE_EXPIRED, STATE_CANCELLED})

# Outcome line per terminal state without an answer (``platform.matrix.clarify.*``).
_TERMINAL_KEYS = {
    STATE_SKIPPED: "platform.matrix.clarify.skipped",
    STATE_EXPIRED: "platform.matrix.clarify.expired",
    STATE_CANCELLED: "platform.matrix.clarify.cancelled",
}
# A free-text answer is echoed on the settled card; the agent already has it in full. The cap keeps
# the terminal edit inside the transport limit (an oversized edit is refused on every attempt).
ANSWER_DISPLAY_MAX = 500


def cap_answer(answer: Optional[str]) -> tuple[Optional[str], bool]:
    """``(answer, truncated)`` with the answer cut to ``ANSWER_DISPLAY_MAX`` characters."""
    if answer is None or len(answer) <= ANSWER_DISPLAY_MAX:
        return answer, False
    return answer[: ANSWER_DISPLAY_MAX - 1] + "…", True


def normalize_reaction_key(key: str) -> str:
    """Canonical keycap / skip glyph for a reaction key (variation selector optional)."""
    key = str(key or "")
    return _REACTION_ALIASES.get(key, key)


def strip_skip_hint(question: str, skip_hint: str) -> str:
    """The question without the gateway's trailing ``Reply "skip" ...`` line."""
    text = str(question or "")
    hint = str(skip_hint or "").strip()
    if hint:
        lines = text.split("\n")
        while lines and lines[-1].strip() == hint:
            lines.pop()
        text = "\n".join(lines)
    return text.strip()


def structured_choices(choices: Optional[list]) -> list[dict[str, Any]]:
    """``[{index, label, recommended}]`` with the presentation-only "(Recommended)" suffix removed."""
    from tools.clarify_tool import strip_recommended

    out: list[dict[str, Any]] = []
    for idx, raw in enumerate(choices or [], start=1):
        text = str(raw).strip()
        label = strip_recommended(text)
        out.append({"index": idx, "label": label, "recommended": label != text})
    return out


def build_pending_payload(
    *,
    clarify_id: str,
    question: str,
    choices: Optional[list],
    multi_select: bool,
    requester_user_id: Optional[str],
    expires_at_ms: Optional[int],
) -> dict[str, Any]:
    """The ``org.hermes.clarify`` value for a pending card (wire contract v1)."""
    return {
        "v": CLARIFY_PAYLOAD_VERSION,
        "clarify_id": str(clarify_id),
        "question": question,
        "choices": structured_choices(choices),
        "multi_select": bool(multi_select and choices),
        "allow_other": True,
        "allow_skip": True,
        "requester_user_id": requester_user_id or None,
        "expires_at_ms": int(expires_at_ms) if expires_at_ms is not None else None,
        "state": STATE_PENDING,
    }


def settled_payload(pending: dict[str, Any], state: str, answer: Optional[str]) -> dict[str, Any]:
    """The pending payload moved to a terminal ``state`` with its ``answer`` (None unless answered)."""
    payload = dict(pending)
    payload["state"] = state
    shown, truncated = cap_answer(answer if state == STATE_ANSWERED else None)
    payload["answer"] = shown
    if truncated:
        payload["answer_truncated"] = True
    return payload


def format_pending_html(*, question: str, choices: Optional[list], hint: str) -> str:
    """Bold ``❓ question``, an ordered list of the choices (recommended one marked), a hint line."""
    from tools.clarify_tool import RECOMMENDED_LABEL

    question_html = html.escape(question).replace("\n", "<br/>")
    parts = [f"<p><strong>❓ {question_html}</strong></p>"]
    items = structured_choices(choices)
    if items:
        rows = []
        for item in items:
            label = html.escape(item["label"])
            if item["recommended"]:
                label += f" <em>{html.escape(RECOMMENDED_LABEL)}</em>"
            rows.append(f"<li>{label}</li>")
        parts.append("<ol>" + "".join(rows) + "</ol>")
    if hint:
        parts.append(f"<p><em>{html.escape(hint)}</em></p>")
    return "".join(parts)


def display_answer(response: Optional[str], multi_select: bool) -> str:
    """Human-readable answer: labels without "(Recommended)", a multi-select array joined by commas."""
    from tools.clarify_tool import strip_recommended

    text = str(response or "").strip()
    if multi_select and text.startswith("["):
        try:
            values = json.loads(text)
        except (TypeError, ValueError):
            values = None
        if isinstance(values, list):
            return ", ".join(strip_recommended(str(v)) for v in values)
    return strip_recommended(text)


def format_settled(question: str, state: str, answer: Optional[str]) -> tuple[str, str]:
    """``(body, formatted_body)`` for the terminal edit: the question and one outcome line."""
    from agent.i18n import t

    if state == STATE_ANSWERED:
        outcome = f"✅ {cap_answer(answer)[0]}"
    else:
        outcome = t(_TERMINAL_KEYS.get(state, _TERMINAL_KEYS[STATE_EXPIRED]))
    body = f"❓ {question}\n{outcome}"
    question_html = html.escape(question).replace("\n", "<br/>")
    formatted = f"<p><strong>❓ {question_html}</strong><br/>{html.escape(outcome)}</p>"
    return body, formatted
