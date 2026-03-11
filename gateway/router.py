"""Binding router for multi-agent message routing.

Maps incoming messages to agent IDs based on platform, chat, guild, and
other session-source fields.  Bindings are ranked by specificity so that
the most precise rule always wins.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# ── constants ────────────────────────────────────────────────────────────

PLATFORM_NAMES: set[str] = {
    "telegram",
    "discord",
    "slack",
    "whatsapp",
    "signal",
    "homeassistant",
}

_KEY_EXPANSION: Dict[str, str] = {
    "guild": "guild_id",
    "type": "chat_type",
    "team": "team_id",
    "peer": "peer",
}


# ── data ─────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class Binding:
    """A single routing rule that maps a match pattern to an agent."""

    agent_id: str
    match: Dict[str, str] = field(default_factory=dict)
    tier: int = 7  # computed priority (1 = most specific)


# ── helpers ──────────────────────────────────────────────────────────────

def _assign_tier(match: Dict[str, str]) -> int:
    """Return a priority tier (1–7) based on how specific *match* is.

    Lower tier number means higher priority (more specific).

    Tier 1: platform + chat_id   (exact channel)
    Tier 2: platform + peer      (exact DM user)
    Tier 3: platform + guild_id + chat_type
    Tier 4: platform + (guild_id | team_id)
    Tier 5: platform + chat_type
    Tier 6: platform only
    Tier 7: fallback (empty match)
    """
    keys = set(match.keys()) - {"platform"}

    if not match:
        return 7
    if "chat_id" in keys:
        return 1
    if "peer" in keys:
        return 2
    if "guild_id" in keys and "chat_type" in keys:
        return 3
    if "guild_id" in keys or "team_id" in keys:
        return 4
    if "chat_type" in keys:
        return 5
    if "platform" in match:
        return 6
    return 7


def normalize_binding(raw: dict) -> Binding:
    """Normalise a shorthand binding dict into a :class:`Binding`.

    Accepted shorthand formats::

        {"agent": "coder", "telegram": "-100123"}
            → Binding(agent_id="coder",
                      match={"platform": "telegram", "chat_id": "-100123"})

        {"agent": "assistant", "whatsapp": "*"}
            → Binding(agent_id="assistant",
                      match={"platform": "whatsapp"})

        {"agent": "coder", "discord": {"guild": "123", "type": "channel"}}
            → Binding(agent_id="coder",
                      match={"platform": "discord",
                             "guild_id": "123", "chat_type": "channel"})
    """
    agent_id: str = raw.get("agent", raw.get("agent_id", ""))
    if not agent_id:
        raise ValueError(f"Binding missing 'agent' key: {raw!r}")

    match: Dict[str, str] = {}

    for platform in PLATFORM_NAMES:
        if platform not in raw:
            continue

        value: Any = raw[platform]
        match["platform"] = platform

        if isinstance(value, str):
            if value != "*":
                match["chat_id"] = value
        elif isinstance(value, dict):
            for short_key, expanded_key in _KEY_EXPANSION.items():
                if short_key in value:
                    match[expanded_key] = str(value[short_key])
            # Pass through any keys that are already in expanded form
            for k, v in value.items():
                if k not in _KEY_EXPANSION:
                    match[k] = str(v)
        else:
            raise TypeError(
                f"Unsupported value type for platform '{platform}': "
                f"{type(value).__name__}"
            )
        break  # only one platform key per binding

    tier = _assign_tier(match)
    return Binding(agent_id=agent_id, match=match, tier=tier)


# ── router ───────────────────────────────────────────────────────────────

class BindingRouter:
    """Route incoming messages to agent IDs based on binding rules.

    Parameters
    ----------
    bindings_config:
        A list of raw binding dicts (shorthand format).
    default_agent_id:
        Fallback agent ID when no binding matches.
    """

    def __init__(self, bindings_config: list, default_agent_id: str) -> None:
        self._default_agent_id: str = default_agent_id
        self._bindings: List[Binding] = sorted(
            (normalize_binding(raw) for raw in bindings_config),
            key=lambda b: b.tier,
        )

    # ── public API ───────────────────────────────────────────────────

    def resolve(
        self,
        platform: str,
        chat_id: Optional[str] = None,
        chat_type: Optional[str] = None,
        user_id: Optional[str] = None,
        guild_id: Optional[str] = None,
        team_id: Optional[str] = None,
    ) -> str:
        """Return the agent ID for the most specific matching binding.

        Iterates bindings in tier order (most specific first).  The first
        match wins.  Falls back to *default_agent_id* if nothing matches.
        """
        kwargs: Dict[str, Optional[str]] = {
            "platform": platform,
            "chat_id": chat_id,
            "chat_type": chat_type,
            "user_id": user_id,
            "guild_id": guild_id,
            "team_id": team_id,
        }
        for binding in self._bindings:
            if self._matches(binding, **kwargs):
                return binding.agent_id
        return self._default_agent_id

    # ── internals ────────────────────────────────────────────────────

    @staticmethod
    def _matches(binding: Binding, **kwargs: Optional[str]) -> bool:
        """Check whether *binding* matches the supplied keyword arguments.

        Uses AND semantics: every key present in ``binding.match`` must
        equal the corresponding value in *kwargs*.  Keys absent from the
        binding act as wildcards (always match).
        """
        for key, required_value in binding.match.items():
            actual = kwargs.get(key)
            if actual is None:
                return False
            if str(actual) != str(required_value):
                return False
        return True
