"""
Agent registry for multi-agent support.

Manages agent configurations, tool policies, and workspace resolution.
Each agent has its own identity, model settings, tool access, and workspace.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger(__name__)

HERMES_HOME = Path.home() / ".hermes"

# ---------------------------------------------------------------------------
# Tool profiles -- predefined sets of allowed tools
# ---------------------------------------------------------------------------

TOOL_PROFILES: Dict[str, Dict[str, Any]] = {
    "minimal": {
        "allow": [
            "clarify",
            "memory",
            "todo",
            "session_search",
        ],
    },
    "coding": {
        "allow": [
            "terminal",
            "process",
            "read_file",
            "write_file",
            "patch",
            "search_files",
            "web_search",
            "web_extract",
            "memory",
            "todo",
            "clarify",
            "session_search",
            "delegate_task",
            "execute_code",
            "vision_analyze",
        ],
    },
    "messaging": {
        "allow": [
            "web_search",
            "web_extract",
            "memory",
            "todo",
            "clarify",
            "session_search",
            "send_message",
            "text_to_speech",
            "image_generate",
        ],
    },
    "full": {},  # No restrictions
}

# Valid agent ID pattern: starts with lowercase letter/digit, rest can include _ and -
_AGENT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


# ---------------------------------------------------------------------------
# ToolPolicy
# ---------------------------------------------------------------------------

@dataclass
class ToolPolicy:
    """
    Declarative tool access policy for an agent.

    Resolution pipeline (applied in order):
      1. Start with the profile's allow-list (or all tools if no profile / 'full').
      2. Add any names from ``also_allow``.
      3. If an explicit ``allow`` list is set, intersect with it.
      4. Remove any names from ``deny`` (deny always wins).
    """

    profile: Optional[str] = None
    allow: Optional[List[str]] = None
    also_allow: Optional[List[str]] = None
    deny: Optional[List[str]] = None

    def apply(self, tools: Set[str]) -> Set[str]:
        """
        Filter a set of tool names according to this policy.

        The pipeline is: profile -> also_allow -> allow -> deny.
        Deny always wins — denied tools are removed regardless of other rules.

        Parameters
        ----------
        tools:
            The full set of available tool names.

        Returns
        -------
        Set[str]
            The subset of tools this agent is permitted to use.
        """
        # Step 1: Start from profile
        if self.profile and self.profile in TOOL_PROFILES:
            profile_def = TOOL_PROFILES[self.profile]
            if "allow" in profile_def:
                result = tools & set(profile_def["allow"])
            else:
                # Profile like 'full' with no allow list => all tools
                result = set(tools)
        else:
            # No profile => start with all tools
            result = set(tools)

        # Step 2: Additive extras from also_allow
        if self.also_allow:
            result |= tools & set(self.also_allow)

        # Step 3: Explicit allow list narrows the result
        if self.allow is not None:
            result &= set(self.allow)

        # Step 4: Deny always wins
        if self.deny:
            result -= set(self.deny)

        return result


# ---------------------------------------------------------------------------
# SubagentPolicy
# ---------------------------------------------------------------------------

@dataclass
class SubagentPolicy:
    """Controls how an agent may spawn sub-agents."""

    max_depth: int = 2
    max_children: int = 5
    model: Optional[str] = None


# ---------------------------------------------------------------------------
# AgentConfig
# ---------------------------------------------------------------------------

@dataclass
class AgentConfig:
    """
    Full configuration for a single agent persona.

    Attributes
    ----------
    id:
        Unique identifier (lowercase, alphanumeric + hyphens/underscores).
    description:
        Human-readable description of this agent's purpose.
    default:
        Whether this is the default agent (exactly one must be default).
    model:
        LLM model identifier. ``None`` inherits the global default.
    provider:
        LLM provider name (e.g. ``'anthropic'``, ``'openai'``).
    personality:
        Inline personality/system prompt text, or path to a file.
    workspace:
        Custom workspace directory override. ``None`` uses the default.
    toolsets:
        List of toolset names to load (overrides platform default).
    tool_policy:
        Declarative tool access restrictions.
    reasoning:
        Provider-specific reasoning/thinking configuration dict.
    max_turns:
        Maximum agentic loop iterations per request.
    sandbox:
        Sandbox/isolation configuration dict.
    fallback_model:
        Fallback model configuration dict (used on primary failure).
    memory_enabled:
        Whether long-term memory is active for this agent.
    subagents:
        Sub-agent spawning policy.
    dm_scope:
        Which agent handles DMs on messaging platforms (``'main'`` by default).
    """

    id: str
    description: str = ""
    default: bool = False
    model: Optional[str] = None
    provider: Optional[str] = None
    personality: Optional[str] = None
    workspace: Optional[str] = None
    toolsets: Optional[List[str]] = None
    tool_policy: Optional[ToolPolicy] = None
    reasoning: Optional[Dict[str, Any]] = None
    max_turns: Optional[int] = None
    sandbox: Optional[Dict[str, Any]] = None
    fallback_model: Optional[Dict[str, Any]] = None
    memory_enabled: bool = True
    subagents: SubagentPolicy = field(default_factory=SubagentPolicy)
    dm_scope: str = "main"

    # -- derived paths -------------------------------------------------------

    @property
    def workspace_dir(self) -> Path:
        """Agent-specific workspace directory."""
        if self.workspace:
            return Path(self.workspace).expanduser()
        return HERMES_HOME / "agents" / self.id

    @property
    def sessions_dir(self) -> Path:
        """Directory for this agent's session data."""
        return self.workspace_dir / "sessions"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def normalize_tool_config(raw: Any) -> Optional[ToolPolicy]:
    """
    Coerce various shorthand forms into a ``ToolPolicy``.

    Accepted inputs::

        None             -> None
        "coding"         -> ToolPolicy(profile="coding")
        ["read_file", …] -> ToolPolicy(allow=[…])
        {profile: …, …}  -> ToolPolicy(**dict)

    Parameters
    ----------
    raw:
        Raw tool policy value from configuration.

    Returns
    -------
    Optional[ToolPolicy]
    """
    if raw is None:
        return None
    if isinstance(raw, str):
        return ToolPolicy(profile=raw)
    if isinstance(raw, list):
        return ToolPolicy(allow=raw)
    if isinstance(raw, dict):
        return ToolPolicy(
            profile=raw.get("profile"),
            allow=raw.get("allow"),
            also_allow=raw.get("also_allow"),
            deny=raw.get("deny"),
        )
    raise TypeError(f"Invalid tool_policy value: {raw!r}")


def _validate_agent_id(agent_id: str) -> None:
    """Raise ``ValueError`` if *agent_id* is not a valid identifier."""
    if not _AGENT_ID_RE.match(agent_id):
        raise ValueError(
            f"Invalid agent id {agent_id!r}. Must match "
            f"[a-z0-9][a-z0-9_-]{{0,63}}"
        )


# ---------------------------------------------------------------------------
# AgentRegistry
# ---------------------------------------------------------------------------

class AgentRegistry:
    """
    Registry of configured agent personas.

    Parses the ``agents`` section of the top-level config dict and exposes
    lookup / resolution helpers used by the runtime.
    """

    def __init__(self, config: dict, global_config: dict = None) -> None:
        self._agents: Dict[str, AgentConfig] = {}
        self._default_id: str = "main"
        self._parse_agents(config, global_config)

    # -- parsing -------------------------------------------------------------

    def _parse_agents(self, config: dict, global_config: dict = None) -> None:
        """
        Parse ``config['agents']`` into ``AgentConfig`` instances.

        If the config has no ``agents`` key an implicit *main* agent is
        created from ``global_config`` so the system always has at least
        one agent.

        Parameters
        ----------
        config:
            Config dict that may contain an ``agents`` key with a flat dict
            of agent definitions keyed by name.
        global_config:
            Top-level global config dict used to populate the implicit
            *main* agent when no ``agents`` key is present.
        """
        agents_raw: Optional[Dict[str, Any]] = config.get("agents")

        if not agents_raw:
            # Implicit single-agent setup — derive from global_config
            gc = global_config or {}
            main = AgentConfig(
                id="main",
                default=True,
                model=gc.get("model"),
                provider=gc.get("provider"),
                personality=gc.get("personality"),
                tool_policy=normalize_tool_config(gc.get("tools")),
                reasoning=gc.get("reasoning"),
                max_turns=gc.get("max_turns"),
                memory_enabled=gc.get("memory_enabled", True),
            )
            self._agents = {"main": main}
            self._default_id = "main"
            return

        agents: Dict[str, AgentConfig] = {}
        seen_ids: Set[str] = set()
        default_id: Optional[str] = None
        first_id: Optional[str] = None

        for name, agent_data in agents_raw.items():
            if agent_data is None:
                agent_data = {}

            agent_id = agent_data.get("id", name)
            _validate_agent_id(agent_id)

            if agent_id in seen_ids:
                raise ValueError(f"Duplicate agent id: {agent_id!r}")
            seen_ids.add(agent_id)

            if first_id is None:
                first_id = agent_id

            # Normalize the tools / tool_policy field
            tool_policy = normalize_tool_config(
                agent_data.get("tools", agent_data.get("tool_policy"))
            )

            subagent_raw = agent_data.get("subagents")
            if isinstance(subagent_raw, dict):
                subagent_policy = SubagentPolicy(**subagent_raw)
            else:
                subagent_policy = SubagentPolicy()

            is_default = agent_data.get("default", False)

            agent_cfg = AgentConfig(
                id=agent_id,
                description=agent_data.get("description", ""),
                default=is_default,
                model=agent_data.get("model"),
                provider=agent_data.get("provider"),
                personality=agent_data.get("personality"),
                workspace=agent_data.get("workspace"),
                toolsets=agent_data.get("toolsets"),
                tool_policy=tool_policy,
                reasoning=agent_data.get("reasoning"),
                max_turns=agent_data.get("max_turns"),
                sandbox=agent_data.get("sandbox"),
                fallback_model=agent_data.get("fallback_model"),
                memory_enabled=agent_data.get("memory_enabled", True),
                subagents=subagent_policy,
                dm_scope=agent_data.get("dm_scope", "main"),
            )

            if is_default:
                if default_id is not None:
                    raise ValueError(
                        f"Multiple default agents: {default_id!r} and {agent_id!r}"
                    )
                default_id = agent_id

            agents[agent_id] = agent_cfg

        # If nobody was explicitly marked default, the first agent wins
        if default_id is None and first_id is not None:
            default_id = first_id
            agents[first_id].default = True
            logger.debug(
                "No explicit default agent; using first: %s", first_id
            )

        self._agents = agents
        self._default_id = default_id or "main"

    # -- public API ----------------------------------------------------------

    def get(self, agent_id: str) -> AgentConfig:
        """
        Return the config for *agent_id*, falling back to the default agent.
        """
        return self._agents.get(agent_id, self.get_default())

    def get_default(self) -> AgentConfig:
        """Return the default agent configuration."""
        return self._agents[self._default_id]

    def list_agents(self) -> List[AgentConfig]:
        """Return all registered agent configurations."""
        return list(self._agents.values())

    # -- resolution helpers --------------------------------------------------

    def resolve_personality(self, agent: AgentConfig) -> Optional[str]:
        """
        Resolve the personality/system-prompt text for *agent*.

        Resolution order:
          1. ``agent.personality`` field (inline text or file path).
          2. ``SOUL.md`` in the agent's workspace directory.
          3. Global ``~/.hermes/SOUL.md`` (only for the *main* agent).
          4. ``None``.
        """
        # 1. Explicit personality in config
        if agent.personality:
            personality_path = Path(agent.personality).expanduser()
            if personality_path.is_file():
                try:
                    return personality_path.read_text(encoding="utf-8").strip()
                except OSError:
                    logger.warning(
                        "Could not read personality file: %s", personality_path
                    )
            # Treat as inline text
            return agent.personality

        # 2. Workspace SOUL.md
        workspace_soul = agent.workspace_dir / "SOUL.md"
        if workspace_soul.is_file():
            try:
                return workspace_soul.read_text(encoding="utf-8").strip()
            except OSError:
                logger.warning(
                    "Could not read workspace SOUL.md: %s", workspace_soul
                )

        # 3. Global SOUL.md (main agent only)
        if agent.id == "main":
            global_soul = HERMES_HOME / "SOUL.md"
            if global_soul.is_file():
                try:
                    return global_soul.read_text(encoding="utf-8").strip()
                except OSError:
                    logger.warning(
                        "Could not read global SOUL.md: %s", global_soul
                    )

        # 4. Nothing
        return None

    def resolve_toolsets(
        self, agent: AgentConfig, platform: str
    ) -> Optional[List[str]]:
        """
        Determine which toolsets to load for *agent* on *platform*.

        Returns the agent's explicit ``toolsets`` list if set, otherwise
        ``None`` to let the caller fall back to the platform's default
        toolset configuration.

        Parameters
        ----------
        agent:
            The agent whose toolsets to resolve.
        platform:
            The platform name (e.g. ``'telegram'``, ``'local'``).

        Returns
        -------
        Optional[List[str]]
            Ordered list of toolset names, or ``None`` for platform default.
        """
        if agent.toolsets is not None:
            return list(agent.toolsets)
        return None

    @staticmethod
    def ensure_workspace(agent: AgentConfig) -> None:
        """
        Create the agent's workspace and session directories if they
        do not already exist.
        """
        agent.workspace_dir.mkdir(parents=True, exist_ok=True)
        agent.sessions_dir.mkdir(parents=True, exist_ok=True)
        logger.debug(
            "Ensured workspace for agent %s: %s", agent.id, agent.workspace_dir
        )
