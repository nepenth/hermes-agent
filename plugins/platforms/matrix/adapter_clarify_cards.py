"""Matrix clarify ("question") card lifecycle, mixed into ``MatrixAdapter``.

The card is sent through ``_send_reaction_prompt`` and settled through ``edit_message``; both live
on the facade, which also owns the registries (``_clarify_cards_by_event``, ``_clarify_cards_by_id``,
``_retired_clarify_cards``) and routes reactions and replies here. Payloads are in ``clarify_cards``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any, Optional

from agent.i18n import t
from gateway.approval_actor import refusal_text
from gateway.platforms.base import SendResult

logger = logging.getLogger(__name__)


@dataclass
class _MatrixClarifyCard:
    """A posted clarify ("question") card. ``reaction_values`` maps a seeded reaction to the value
    it resolves with (a choice, or ``clarify_gateway.SKIPPED``); ``entry`` is the gateway entry the
    card was rendered for, so a late answer can never resolve a newer question."""
    clarify_id: str
    chat_id: str
    message_id: str
    session_key: str
    question: str
    payload: dict
    multi_select: bool = False
    entry: Any = None
    reaction_values: dict = field(default_factory=dict)
    requester_user_id: str | None = None
    state: str = "pending"
    bot_reaction_events: dict[str, str] = field(default_factory=dict)
    terminal: tuple[str, str | None] | None = None  # first decided (state, answer); retries reuse it


class MatrixClarifyCardsMixin:
    """Clarify card send, reaction/reply answers and terminal ``m.replace`` for ``MatrixAdapter``."""

    _CLARIFY_CARD_MAX = 256

    @staticmethod
    def _apply_extra_content(content: dict, metadata: Optional[dict]) -> None:
        """Merge ``metadata["matrix_extra_content"]`` (custom namespaced keys such as
        ``org.hermes.clarify``) into one event's content. Spec keys (``m.*``: relations, mentions,
        ``m.new_content``) are never accepted, and existing keys are never overwritten."""
        extra = (metadata or {}).get("matrix_extra_content")
        if isinstance(extra, dict):
            for key, value in extra.items():
                key = str(key)
                if "." in key and not key.startswith("m.") and key not in content:
                    content[key] = value

    @staticmethod
    def _clarify_plain_text(question: str, choices: Optional[list], multi_select: bool) -> str:
        """Exactly the base adapter's plain-text clarify prompt (the card's ``body``)."""
        if not choices:
            return t("gateway.clarify.question", question=question)
        hint = t("gateway.clarify.hint_multi") if multi_select else t("gateway.clarify.hint_single")
        numbered = [t("gateway.clarify.option_line", index=i, choice=choice)
                    for i, choice in enumerate(choices, start=1)]
        return "\n".join([t("gateway.clarify.question", question=question), "", *numbered, "", hint])

    async def send_clarify(
        self, chat_id: str, question: str, choices: Optional[list], clarify_id: str,
        session_key: str, metadata: Optional[dict[str, Any]] = None) -> SendResult:
        """Clarify card: the base plain-text prompt as ``body``, a compact HTML rendering, and the
        structured ``org.hermes.clarify`` payload. Single-select questions get one keycap reaction
        per choice; every card gets ⏭️ (skip). Typed answers keep working through the gateway's
        text intercept (``mark_awaiting_text``); a reply to the card is answered here."""
        if not self._client:
            return SendResult(success=False, error="Not connected")
        from plugins.platforms.matrix import clarify_cards as cc
        from tools import clarify_gateway as _cg
        entry = _cg.get_entry(clarify_id)
        choices = list(choices) if choices else None
        multi_select = bool(getattr(entry, "multi_select", False)) and bool(choices)
        text = self._clarify_plain_text(question, choices, multi_select)
        if choices:
            # Text capture stays on: a plain (non-reply) message answers exactly as before.
            _cg.mark_awaiting_text(clarify_id)
        skip_hint = t("gateway.clarify.skip_hint")
        bare_question = cc.strip_skip_hint(question, skip_hint)
        hint = skip_hint
        if choices:
            hint = (t("gateway.clarify.hint_multi") if multi_select else t("gateway.clarify.hint_single")) + " " + skip_hint
        # Who may answer is the entry's recorded authority (gateway.approval_actor.clarify_refusal);
        # the payload names the requester only so a client can draw its controls for them.
        requester = str((getattr(entry, "authority", None) or {}).get("requester_id") or "") or None
        timeout = _cg.get_clarify_timeout()
        expires_at_ms = int(time.time() * 1000) + int(timeout) * 1000 if timeout and timeout > 0 else None
        payload = cc.build_pending_payload(
            clarify_id=clarify_id, question=bare_question, choices=choices, multi_select=multi_select,
            requester_user_id=requester, expires_at_ms=expires_at_ms)
        send_meta = {
            **(metadata or {}),
            "matrix_formatted_body": cc.format_pending_html(question=bare_question, choices=choices, hint=hint),
            "matrix_extra_content": {cc.CLARIFY_CONTENT_KEY: payload},
        }
        reaction_values: dict[str, str] = {}
        if choices and not multi_select:
            reaction_values.update(zip(cc.CLARIFY_CHOICE_REACTIONS, choices))
        reaction_values[cc.CLARIFY_SKIP_REACTION] = _cg.SKIPPED

        def _make(message_id, requester_id, _expires_at):
            card = _MatrixClarifyCard(
                clarify_id=clarify_id, chat_id=chat_id, message_id=message_id, session_key=session_key,
                question=bare_question, payload=payload, multi_select=multi_select, entry=entry,
                reaction_values=reaction_values, requester_user_id=requester_id)
            while len(self._clarify_cards_by_event) >= self._CLARIFY_CARD_MAX:
                self._forget_clarify_card(next(iter(self._clarify_cards_by_event.values())))
            self._clarify_cards_by_id[clarify_id] = card
            return card

        result = await self._send_reaction_prompt(
            chat_id, text, send_meta, _make, self._clarify_cards_by_event, tuple(reaction_values), "clarify")
        if result.success and result.message_id:
            card = self._clarify_cards_by_event.get(result.message_id)
            if card is not None and (entry is None or entry.event.is_set()):
                # Answered (or ended) while the card was in flight: its retire call found nothing.
                await self._settle_clarify_card(card)
        return result

    def _forget_clarify_card(self, card: _MatrixClarifyCard, *, retired: bool = True) -> None:
        self._clarify_cards_by_event.pop(card.message_id, None)
        if self._clarify_cards_by_id.get(card.clarify_id) is card:
            self._clarify_cards_by_id.pop(card.clarify_id, None)
        if retired:  # a later seeded reaction on this card is told it no longer takes answers
            self._retired_clarify_cards[card.message_id] = None
            while len(self._retired_clarify_cards) > self._CLARIFY_CARD_MAX:
                self._retired_clarify_cards.pop(next(iter(self._retired_clarify_cards)))

    @staticmethod
    def _clarify_outcome(card: _MatrixClarifyCard) -> tuple[str, Optional[str]]:
        """``(state, answer)`` from the gateway entry the card was rendered for. Every cancellation
        path sets the ``CANCELLED`` sentinel on the entry; no response means it timed out."""
        from plugins.platforms.matrix import clarify_cards as cc
        from tools import clarify_gateway as _cg
        entry = card.entry
        response = getattr(entry, "response", None) if entry is not None and entry.event.is_set() else None
        if response is None:
            return cc.STATE_EXPIRED, None
        if response == _cg.SKIPPED:
            return cc.STATE_SKIPPED, None
        if response == _cg.CANCELLED:
            return cc.STATE_CANCELLED, None
        return cc.STATE_ANSWERED, cc.display_answer(response, card.multi_select)

    async def _settle_clarify_card(
        self, card: _MatrixClarifyCard, state: Optional[str] = None, answer: Optional[str] = None,
        *, max_attempts: int = 3) -> bool:
        """Edit the card to its terminal state (``m.replace``), then withdraw the seeded reactions.

        The card stays registered until that edit lands (the approval-card rule): a card whose
        edit failed goes back to pending with its decided outcome kept, so the next retire,
        reaction or reply retries the same terminal edit instead of leaving a live-looking card."""
        from plugins.platforms.matrix import clarify_cards as cc
        if card.state != cc.STATE_PENDING:
            return False
        if card.terminal is None:
            card.terminal = (state, answer) if state is not None else self._clarify_outcome(card)
        state, answer = card.terminal
        card.state = state  # claimed synchronously: one terminal edit in flight per card
        body, html_body = cc.format_settled(card.question, state, answer)
        meta = {"matrix_formatted_body": html_body,
                "matrix_extra_content": {cc.CLARIFY_CONTENT_KEY: cc.settled_payload(card.payload, state, answer)}}
        for attempt in range(1, max(1, max_attempts) + 1):
            try:
                result = await self.edit_message(card.chat_id, card.message_id, body, metadata=meta)
            except asyncio.CancelledError:
                card.state = cc.STATE_PENDING
                raise
            except Exception as exc:
                logger.warning("Matrix: clarify card edit raised (attempt %d/%d)", attempt, max_attempts, exc_info=True)
                result = SendResult(success=False, error=str(exc))
            if result.success:
                self._forget_clarify_card(card)
                await self._redact_bot_approval_reactions(card.chat_id, card, "clarify settled")
                return True
            logger.warning("Matrix: clarify card edit failed (attempt %d/%d): %s", attempt, max_attempts, result.error)
            if attempt < max_attempts:
                await asyncio.sleep(min(0.5 * attempt, 2.0))
        card.state = cc.STATE_PENDING  # still registered: the next interaction retries this edit
        return False

    async def retire_clarify_card(self, clarify_id: str, notice: str) -> None:
        """Settle a still-pending card when its clarify ended without a card interaction: a typed
        answer, a timeout, a cancellation or session reset. No-op once the card is settled."""
        card = self._clarify_cards_by_id.get(clarify_id)
        if card is None:
            return
        await self._settle_clarify_card(card)

    async def _handle_clarify_reaction(self, room_id: str, reacts_to: str, key: str, sender: str) -> bool:
        """A seeded keycap / ⏭️ on a pending clarify card answers it. True if the card was the target."""
        from plugins.platforms.matrix import clarify_cards as cc
        from tools import clarify_gateway as _cg
        card = self._clarify_cards_by_event.get(reacts_to)
        if card is None:
            if reacts_to in self._retired_clarify_cards and cc.normalize_reaction_key(key) in (
                    *cc.CLARIFY_CHOICE_REACTIONS, cc.CLARIFY_SKIP_REACTION):
                await self._send_invalid_reaction_feedback(room_id, reacts_to, t("gateway.clarify.expired"))
                return True
            return False
        if card.state != cc.STATE_PENDING or room_id != card.chat_id:
            return True
        value = card.reaction_values.get(cc.normalize_reaction_key(key))
        if value is None:
            return True  # any other emoji is just a reaction
        if not await self._validate_matrix_prompt_reactor(room_id, reacts_to, sender, card, "clarify"):
            return True
        from gateway.approval_actor import clarify_refusal
        if requester := clarify_refusal(card.clarify_id, sender, extra=getattr(getattr(self, "config", None), "extra", None)):
            await self._send_invalid_reaction_feedback(room_id, reacts_to, refusal_text(requester))
            return True
        if _cg.get_pending(card.clarify_id) is card.entry and _cg.resolve_gateway_clarify(card.clarify_id, value):
            logger.info("Matrix: clarify %s answered by reaction from %s", card.clarify_id, sender)
            with suppress(Exception):
                self.resume_typing_for_chat(room_id)
            await self._settle_clarify_card(card)
        else:
            # Answered or ended elsewhere first: show that outcome; only a lapsed card says so.
            await self._settle_clarify_card(card)
            if card.state == cc.STATE_EXPIRED:
                await self._send_invalid_reaction_feedback(room_id, reacts_to, t("gateway.clarify.expired"))
        return True

    async def _answer_clarify_reply(
        self, room_id: str, sender: str, event_id: str, body: str, relates_to: dict) -> bool:
        """A reply to a pending clarify card from its requester answers that card, through the same
        coercion as a typed answer (number, "1, 3", label, "skip", free text). False = route normally."""
        if not self._clarify_cards_by_event or not isinstance(relates_to, dict) or relates_to.get("is_falling_back"):
            return False
        target = str((relates_to.get("m.in_reply_to") or {}).get("event_id") or "")
        card = self._clarify_cards_by_event.get(target)
        from plugins.platforms.matrix import clarify_cards as cc
        if card is None or card.state != cc.STATE_PENDING or card.chat_id != room_id:
            return False
        if not self._is_authorized_user(sender, room_id):
            return False
        from gateway.approval_actor import clarify_refusal
        if clarify_refusal(card.clarify_id, sender, extra=getattr(getattr(self, "config", None), "extra", None)):
            # The gateway's text intercept answers a refused sender with the requester's name.
            logger.info("Matrix: reply to clarify %s from %s is not theirs to answer", card.clarify_id, sender)
            return False
        from plugins.platforms.matrix.adapter import _strip_reply_fallback
        text = self._strip_mention(_strip_reply_fallback(body)).strip()
        if not text or text.startswith(("/", "!")):
            return False  # a command, not an answer
        from tools import clarify_gateway as _cg
        if card.entry is None or _cg.get_pending(card.clarify_id) is not card.entry:
            if card.terminal is not None:  # answered already; its terminal edit failed earlier
                await self._settle_clarify_card(card)
            return False
        # Same coercion and outcome as a typed answer, bound to this card's own clarify id.
        if _cg.attempt_text_response(card.clarify_id, text) != _cg.TEXT_RESOLVED:
            return False
        logger.info("Matrix: clarify %s answered by reply %s from %s", card.clarify_id, event_id, sender)
        self._background_read_receipt(room_id, event_id)
        with suppress(Exception):
            self.resume_typing_for_chat(room_id)
        await self._settle_clarify_card(card)
        return True
