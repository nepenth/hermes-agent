"""Comprehensive tests for gateway.agent_registry module."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch as mock_patch

import pytest

from gateway.agent_registry import (
    TOOL_PROFILES,
    AgentConfig,
    AgentRegistry,
    ToolPolicy,
    normalize_tool_config,
    _validate_agent_id,
    HERMES_HOME,
)


# =========================================================================
# 1. TOOL_PROFILES
# =========================================================================

class TestToolProfiles:
    """Verify all 4 tool profiles exist and have correct structure."""

    def test_all_four_profiles_exist(self):
        assert set(TOOL_PROFILES.keys()) == {"minimal", "coding", "messaging", "full"}

    def test_minimal_has_allow_list(self):
        profile = TOOL_PROFILES["minimal"]
        assert "allow" in profile
        assert isinstance(profile["allow"], list)
        assert len(profile["allow"]) > 0

    def test_coding_has_allow_list(self):
        profile = TOOL_PROFILES["coding"]
        assert "allow" in profile
        assert isinstance(profile["allow"], list)
        assert len(profile["allow"]) > 0

    def test_messaging_has_allow_list(self):
        profile = TOOL_PROFILES["messaging"]
        assert "allow" in profile
        assert isinstance(profile["allow"], list)
        assert len(profile["allow"]) > 0

    def test_full_has_no_allow_list(self):
        """The 'full' profile is an empty dict, meaning no restrictions."""
        profile = TOOL_PROFILES["full"]
        assert profile == {}
        assert "allow" not in profile

    def test_minimal_contains_expected_tools(self):
        tools = TOOL_PROFILES["minimal"]["allow"]
        for name in ("clarify", "memory", "todo", "session_search"):
            assert name in tools

    def test_coding_contains_expected_tools(self):
        tools = TOOL_PROFILES["coding"]["allow"]
        for name in (
            "terminal", "process", "read_file", "write_file", "patch",
            "search_files", "web_search", "web_extract", "memory",
            "delegate_task", "execute_code",
        ):
            assert name in tools

    def test_messaging_contains_expected_tools(self):
        tools = TOOL_PROFILES["messaging"]["allow"]
        for name in (
            "web_search", "web_extract", "memory", "send_message",
            "text_to_speech", "image_generate",
        ):
            assert name in tools

    def test_each_profile_value_is_dict(self):
        for name, profile in TOOL_PROFILES.items():
            assert isinstance(profile, dict), f"Profile {name!r} is not a dict"


# =========================================================================
# 2. normalize_tool_config
# =========================================================================

class TestNormalizeToolConfig:
    """Test coercion of shorthand forms into ToolPolicy."""

    def test_none_returns_none(self):
        assert normalize_tool_config(None) is None

    def test_string_returns_profile(self):
        policy = normalize_tool_config("coding")
        assert isinstance(policy, ToolPolicy)
        assert policy.profile == "coding"
        assert policy.allow is None
        assert policy.also_allow is None
        assert policy.deny is None

    def test_list_returns_allow_policy(self):
        names = ["read_file", "write_file"]
        policy = normalize_tool_config(names)
        assert isinstance(policy, ToolPolicy)
        assert policy.allow == names
        assert policy.profile is None
        assert policy.also_allow is None
        assert policy.deny is None

    def test_dict_returns_full_policy(self):
        raw = {
            "profile": "minimal",
            "also_allow": ["terminal"],
            "deny": ["clarify"],
        }
        policy = normalize_tool_config(raw)
        assert isinstance(policy, ToolPolicy)
        assert policy.profile == "minimal"
        assert policy.also_allow == ["terminal"]
        assert policy.deny == ["clarify"]
        assert policy.allow is None

    def test_dict_with_allow(self):
        raw = {"allow": ["read_file", "write_file"]}
        policy = normalize_tool_config(raw)
        assert policy.allow == ["read_file", "write_file"]
        assert policy.profile is None

    def test_dict_empty(self):
        policy = normalize_tool_config({})
        assert isinstance(policy, ToolPolicy)
        assert policy.profile is None
        assert policy.allow is None
        assert policy.also_allow is None
        assert policy.deny is None

    def test_invalid_type_raises_type_error(self):
        with pytest.raises(TypeError, match="Invalid tool_policy value"):
            normalize_tool_config(42)

    def test_invalid_type_bool_raises(self):
        with pytest.raises(TypeError):
            normalize_tool_config(True)


# =========================================================================
# 3. ToolPolicy.apply()
# =========================================================================

class TestToolPolicyApply:
    """Test the tool filtering pipeline: profile -> also_allow -> allow -> deny."""

    ALL_TOOLS = {
        "terminal", "process", "read_file", "write_file", "patch",
        "search_files", "web_search", "web_extract", "memory", "todo",
        "clarify", "session_search", "delegate_task", "execute_code",
        "vision_analyze", "send_message", "text_to_speech", "image_generate",
    }

    def test_none_policy_passes_all(self):
        """A default ToolPolicy (no profile, no allow/deny) lets everything through."""
        policy = ToolPolicy()
        result = policy.apply(self.ALL_TOOLS)
        assert result == self.ALL_TOOLS

    def test_profile_filtering(self):
        """A profile restricts to only the tools in that profile's allow list."""
        policy = ToolPolicy(profile="minimal")
        result = policy.apply(self.ALL_TOOLS)
        expected = set(TOOL_PROFILES["minimal"]["allow"])
        assert result == expected

    def test_full_profile_has_no_restrictions(self):
        """The 'full' profile passes all tools through."""
        policy = ToolPolicy(profile="full")
        result = policy.apply(self.ALL_TOOLS)
        assert result == self.ALL_TOOLS

    def test_also_allow_adds_to_profile(self):
        """also_allow adds tools to the profile's base set."""
        policy = ToolPolicy(profile="minimal", also_allow=["terminal", "web_search"])
        result = policy.apply(self.ALL_TOOLS)
        expected = set(TOOL_PROFILES["minimal"]["allow"]) | {"terminal", "web_search"}
        assert result == expected

    def test_also_allow_only_adds_available_tools(self):
        """also_allow only adds tools that exist in the available set."""
        policy = ToolPolicy(profile="minimal", also_allow=["nonexistent_tool"])
        result = policy.apply(self.ALL_TOOLS)
        expected = set(TOOL_PROFILES["minimal"]["allow"])
        assert result == expected
        assert "nonexistent_tool" not in result

    def test_allow_whitelist(self):
        """An explicit allow list narrows results to only those tools."""
        policy = ToolPolicy(allow=["terminal", "read_file", "write_file"])
        result = policy.apply(self.ALL_TOOLS)
        assert result == {"terminal", "read_file", "write_file"}

    def test_allow_whitelist_with_unavailable_tool(self):
        """Allow list can only select tools that are in the available set."""
        policy = ToolPolicy(allow=["terminal", "nonexistent"])
        result = policy.apply(self.ALL_TOOLS)
        assert result == {"terminal"}

    def test_deny_blacklist(self):
        """Denied tools are removed from the result."""
        policy = ToolPolicy(deny=["terminal", "process"])
        result = policy.apply(self.ALL_TOOLS)
        assert "terminal" not in result
        assert "process" not in result
        # Everything else is still there
        assert result == self.ALL_TOOLS - {"terminal", "process"}

    def test_deny_wins_over_allow(self):
        """If a tool is in both allow and deny, deny wins."""
        policy = ToolPolicy(allow=["terminal", "read_file"], deny=["terminal"])
        result = policy.apply(self.ALL_TOOLS)
        assert result == {"read_file"}
        assert "terminal" not in result

    def test_deny_wins_over_also_allow(self):
        """Deny also beats also_allow."""
        policy = ToolPolicy(
            profile="minimal",
            also_allow=["terminal"],
            deny=["terminal"],
        )
        result = policy.apply(self.ALL_TOOLS)
        assert "terminal" not in result

    def test_profile_plus_allow_intersection(self):
        """Profile + allow narrows to intersection of profile and allow."""
        policy = ToolPolicy(profile="coding", allow=["terminal", "read_file", "send_message"])
        result = policy.apply(self.ALL_TOOLS)
        # send_message is not in coding profile, so it's excluded by the profile first
        # terminal and read_file are in coding, then intersected with allow
        assert result == {"terminal", "read_file"}

    def test_full_pipeline(self):
        """Full pipeline: profile -> also_allow -> allow -> deny."""
        policy = ToolPolicy(
            profile="minimal",
            also_allow=["terminal", "read_file"],
            allow=["clarify", "memory", "terminal"],
            deny=["memory"],
        )
        result = policy.apply(self.ALL_TOOLS)
        # Step 1: minimal profile -> {clarify, memory, todo, session_search}
        # Step 2: also_allow terminal, read_file -> + {terminal, read_file}
        # Step 3: allow intersect {clarify, memory, terminal} -> {clarify, memory, terminal}
        # Step 4: deny memory -> {clarify, terminal}
        assert result == {"clarify", "terminal"}

    def test_empty_tools_set(self):
        """Applying policy to an empty set always returns empty."""
        policy = ToolPolicy(profile="coding")
        result = policy.apply(set())
        assert result == set()

    def test_unknown_profile_treated_as_no_profile(self):
        """An unknown profile name falls through to all tools."""
        policy = ToolPolicy(profile="nonexistent_profile")
        result = policy.apply(self.ALL_TOOLS)
        assert result == self.ALL_TOOLS


# =========================================================================
# 4. AgentConfig
# =========================================================================

class TestAgentConfig:
    """Test AgentConfig defaults and derived properties."""

    def test_default_values(self):
        cfg = AgentConfig(id="test")
        assert cfg.id == "test"
        assert cfg.description == ""
        assert cfg.default is False
        assert cfg.model is None
        assert cfg.provider is None
        assert cfg.personality is None
        assert cfg.workspace is None
        assert cfg.toolsets is None
        assert cfg.tool_policy is None
        assert cfg.reasoning is None
        assert cfg.max_turns is None
        assert cfg.sandbox is None
        assert cfg.fallback_model is None
        assert cfg.memory_enabled is True
        assert cfg.dm_scope == "main"

    def test_workspace_dir_default(self):
        """Without custom workspace, uses ~/.hermes/agents/<id>."""
        cfg = AgentConfig(id="myagent")
        expected = HERMES_HOME / "agents" / "myagent"
        assert cfg.workspace_dir == expected

    def test_workspace_dir_custom(self):
        """Custom workspace path is used directly."""
        cfg = AgentConfig(id="myagent", workspace="/tmp/custom_workspace")
        assert cfg.workspace_dir == Path("/tmp/custom_workspace")

    def test_workspace_dir_tilde_expansion(self):
        """Custom workspace with ~ is expanded."""
        cfg = AgentConfig(id="myagent", workspace="~/my_workspace")
        assert cfg.workspace_dir == Path.home() / "my_workspace"

    def test_sessions_dir(self):
        """sessions_dir is workspace_dir / 'sessions'."""
        cfg = AgentConfig(id="myagent")
        assert cfg.sessions_dir == cfg.workspace_dir / "sessions"

    def test_sessions_dir_custom_workspace(self):
        cfg = AgentConfig(id="myagent", workspace="/tmp/ws")
        assert cfg.sessions_dir == Path("/tmp/ws/sessions")

    def test_custom_field_values(self):
        cfg = AgentConfig(
            id="coder",
            description="A coding assistant",
            default=True,
            model="claude-3-opus",
            provider="anthropic",
            personality="You are a coder.",
            memory_enabled=False,
            max_turns=10,
            dm_scope="all",
        )
        assert cfg.description == "A coding assistant"
        assert cfg.default is True
        assert cfg.model == "claude-3-opus"
        assert cfg.provider == "anthropic"
        assert cfg.personality == "You are a coder."
        assert cfg.memory_enabled is False
        assert cfg.max_turns == 10
        assert cfg.dm_scope == "all"


# =========================================================================
# 5. AgentRegistry
# =========================================================================

class TestAgentRegistryImplicitMain:
    """When no 'agents' key in config, an implicit main agent is created."""

    def test_implicit_main_agent(self):
        registry = AgentRegistry(config={})
        agents = registry.list_agents()
        assert len(agents) == 1
        assert agents[0].id == "main"
        assert agents[0].default is True

    def test_implicit_main_inherits_global_config(self):
        gc = {
            "model": "claude-3-sonnet",
            "provider": "anthropic",
            "personality": "Be helpful.",
            "max_turns": 15,
            "memory_enabled": False,
        }
        registry = AgentRegistry(config={}, global_config=gc)
        main = registry.get("main")
        assert main.model == "claude-3-sonnet"
        assert main.provider == "anthropic"
        assert main.personality == "Be helpful."
        assert main.max_turns == 15
        assert main.memory_enabled is False

    def test_implicit_main_with_tool_config(self):
        gc = {"tools": "coding"}
        registry = AgentRegistry(config={}, global_config=gc)
        main = registry.get("main")
        assert main.tool_policy is not None
        assert main.tool_policy.profile == "coding"

    def test_get_default_returns_main(self):
        registry = AgentRegistry(config={})
        default = registry.get_default()
        assert default.id == "main"


class TestAgentRegistryMultipleAgents:
    """Test registry with multiple agent definitions."""

    def make_registry(self, agents_dict, global_config=None):
        return AgentRegistry(
            config={"agents": agents_dict},
            global_config=global_config,
        )

    def test_multiple_agents_from_dict(self):
        registry = self.make_registry({
            "coder": {"description": "Writes code"},
            "reviewer": {"description": "Reviews code"},
        })
        agents = registry.list_agents()
        assert len(agents) == 2
        ids = {a.id for a in agents}
        assert ids == {"coder", "reviewer"}

    def test_explicit_default(self):
        registry = self.make_registry({
            "alpha": {"default": False},
            "beta": {"default": True},
            "gamma": {},
        })
        default = registry.get_default()
        assert default.id == "beta"

    def test_first_in_dict_fallback_default(self):
        """When no agent is explicitly default, the first one is used."""
        registry = self.make_registry({
            "first": {"description": "First agent"},
            "second": {"description": "Second agent"},
        })
        default = registry.get_default()
        assert default.id == "first"
        assert default.default is True

    def test_get_returns_default_for_unknown_id(self):
        registry = self.make_registry({
            "alpha": {"default": True},
            "beta": {},
        })
        result = registry.get("nonexistent")
        assert result.id == "alpha"

    def test_get_returns_correct_agent(self):
        registry = self.make_registry({
            "alpha": {"default": True, "description": "Alpha"},
            "beta": {"description": "Beta"},
        })
        alpha = registry.get("alpha")
        beta = registry.get("beta")
        assert alpha.id == "alpha"
        assert alpha.description == "Alpha"
        assert beta.id == "beta"
        assert beta.description == "Beta"

    def test_list_agents_returns_all(self):
        registry = self.make_registry({
            "a": {},
            "b": {},
            "c": {},
        })
        agents = registry.list_agents()
        assert len(agents) == 3
        assert {a.id for a in agents} == {"a", "b", "c"}

    def test_agent_data_none_treated_as_empty(self):
        """Agent value of None in config is treated as empty dict."""
        registry = self.make_registry({
            "simple": None,
        })
        agent = registry.get("simple")
        assert agent.id == "simple"

    def test_custom_id_override(self):
        """Agent can have an 'id' field different from the dict key."""
        registry = self.make_registry({
            "name_in_dict": {"id": "custom-id", "description": "Custom ID"},
        })
        agent = registry.get("custom-id")
        assert agent.id == "custom-id"
        assert agent.description == "Custom ID"

    def test_multiple_defaults_raises(self):
        with pytest.raises(ValueError, match="Multiple default agents"):
            self.make_registry({
                "a": {"default": True},
                "b": {"default": True},
            })

    def test_duplicate_ids_via_custom_id_raises(self):
        """Two agents resolving to the same ID via custom 'id' field."""
        with pytest.raises(ValueError, match="Duplicate agent id"):
            self.make_registry({
                "alpha": {"id": "shared"},
                "beta": {"id": "shared"},
            })


class TestAgentRegistryResolvePersonality:
    """Test resolve_personality resolution order."""

    def test_inline_personality_text(self):
        registry = AgentRegistry(config={})
        agent = AgentConfig(id="test", personality="Be helpful and kind.")
        result = registry.resolve_personality(agent)
        assert result == "Be helpful and kind."

    def test_personality_from_file(self, tmp_path):
        soul_file = tmp_path / "personality.md"
        soul_file.write_text("  I am a specialized assistant.  ")
        registry = AgentRegistry(config={})
        agent = AgentConfig(id="test", personality=str(soul_file))
        result = registry.resolve_personality(agent)
        assert result == "I am a specialized assistant."

    def test_personality_from_workspace_soul_md(self, tmp_path):
        workspace = tmp_path / "agents" / "test"
        workspace.mkdir(parents=True)
        soul_file = workspace / "SOUL.md"
        soul_file.write_text("  Workspace soul content.  ")
        registry = AgentRegistry(config={})
        agent = AgentConfig(id="test", workspace=str(workspace))
        result = registry.resolve_personality(agent)
        assert result == "Workspace soul content."

    def test_personality_config_takes_precedence_over_soul_md(self, tmp_path):
        """Explicit personality in config wins over SOUL.md file."""
        workspace = tmp_path / "agents" / "test"
        workspace.mkdir(parents=True)
        soul_file = workspace / "SOUL.md"
        soul_file.write_text("Workspace soul.")
        registry = AgentRegistry(config={})
        agent = AgentConfig(id="test", workspace=str(workspace), personality="Inline wins.")
        result = registry.resolve_personality(agent)
        assert result == "Inline wins."

    def test_main_agent_global_soul_md(self, tmp_path):
        """Main agent falls back to global ~/.hermes/SOUL.md."""
        global_soul = tmp_path / "SOUL.md"
        global_soul.write_text("  Global soul personality.  ")
        registry = AgentRegistry(config={})
        agent = AgentConfig(id="main")

        with mock_patch("gateway.agent_registry.HERMES_HOME", tmp_path):
            # Also need to mock workspace_dir to avoid matching workspace SOUL.md
            with mock_patch.object(
                AgentConfig, "workspace_dir", new_callable=lambda: property(
                    lambda self: tmp_path / "nonexistent_workspace"
                )
            ):
                result = registry.resolve_personality(agent)
        assert result == "Global soul personality."

    def test_non_main_agent_no_global_fallback(self, tmp_path):
        """Non-main agents do NOT fall back to global SOUL.md."""
        global_soul = tmp_path / "SOUL.md"
        global_soul.write_text("Global soul.")
        registry = AgentRegistry(config={})
        agent = AgentConfig(id="helper")

        with mock_patch("gateway.agent_registry.HERMES_HOME", tmp_path):
            with mock_patch.object(
                AgentConfig, "workspace_dir", new_callable=lambda: property(
                    lambda self: tmp_path / "nonexistent_workspace"
                )
            ):
                result = registry.resolve_personality(agent)
        assert result is None

    def test_personality_none_when_nothing_found(self):
        """Returns None when no personality is configured and no SOUL.md exists."""
        registry = AgentRegistry(config={})
        agent = AgentConfig(id="test", workspace="/tmp/definitely_nonexistent_workspace_xyz")
        result = registry.resolve_personality(agent)
        assert result is None


class TestAgentRegistryResolveToolsets:
    """Test resolve_toolsets method."""

    def test_agent_explicit_toolsets_returned(self):
        registry = AgentRegistry(config={})
        agent = AgentConfig(id="test", toolsets=["core", "web", "coding"])
        result = registry.resolve_toolsets(agent, platform="local")
        assert result == ["core", "web", "coding"]

    def test_agent_no_toolsets_returns_none(self):
        registry = AgentRegistry(config={})
        agent = AgentConfig(id="test")
        result = registry.resolve_toolsets(agent, platform="local")
        assert result is None

    def test_returns_copy_not_reference(self):
        registry = AgentRegistry(config={})
        toolsets = ["core", "web"]
        agent = AgentConfig(id="test", toolsets=toolsets)
        result = registry.resolve_toolsets(agent, platform="local")
        assert result == toolsets
        assert result is not toolsets  # Should be a copy


# =========================================================================
# 6. Validation
# =========================================================================

class TestValidation:
    """Test agent ID validation."""

    def test_valid_ids(self):
        for valid_id in ("main", "coder", "my-agent", "agent_1", "a", "0test", "a-b_c"):
            _validate_agent_id(valid_id)  # Should not raise

    def test_invalid_id_uppercase(self):
        with pytest.raises(ValueError, match="Invalid agent id"):
            _validate_agent_id("MyAgent")

    def test_invalid_id_starts_with_underscore(self):
        with pytest.raises(ValueError, match="Invalid agent id"):
            _validate_agent_id("_agent")

    def test_invalid_id_starts_with_hyphen(self):
        with pytest.raises(ValueError, match="Invalid agent id"):
            _validate_agent_id("-agent")

    def test_invalid_id_empty_string(self):
        with pytest.raises(ValueError, match="Invalid agent id"):
            _validate_agent_id("")

    def test_invalid_id_spaces(self):
        with pytest.raises(ValueError, match="Invalid agent id"):
            _validate_agent_id("my agent")

    def test_invalid_id_special_chars(self):
        with pytest.raises(ValueError, match="Invalid agent id"):
            _validate_agent_id("agent@home")

    def test_invalid_id_too_long(self):
        """IDs over 64 characters are rejected."""
        with pytest.raises(ValueError, match="Invalid agent id"):
            _validate_agent_id("a" * 65)

    def test_valid_id_max_length(self):
        """64 characters is the max valid length."""
        _validate_agent_id("a" * 64)

    def test_registry_rejects_invalid_agent_id(self):
        with pytest.raises(ValueError, match="Invalid agent id"):
            AgentRegistry(config={"agents": {"My Agent!": {"description": "bad"}}})


# =========================================================================
# 7. Inheritance from global_config
# =========================================================================

class TestGlobalConfigInheritance:
    """Test that agents inherit from global_config where appropriate."""

    def test_implicit_main_inherits_model(self):
        gc = {"model": "gpt-4o"}
        registry = AgentRegistry(config={}, global_config=gc)
        main = registry.get("main")
        assert main.model == "gpt-4o"

    def test_implicit_main_inherits_provider(self):
        gc = {"provider": "openai"}
        registry = AgentRegistry(config={}, global_config=gc)
        main = registry.get("main")
        assert main.provider == "openai"

    def test_implicit_main_inherits_reasoning(self):
        gc = {"reasoning": {"budget_tokens": 5000}}
        registry = AgentRegistry(config={}, global_config=gc)
        main = registry.get("main")
        assert main.reasoning == {"budget_tokens": 5000}

    def test_explicit_agent_model_not_inherited_from_global(self):
        """Agents defined in agents section do NOT auto-inherit global model.
        (The registry only applies global inheritance for the implicit main agent.)
        """
        registry = AgentRegistry(
            config={"agents": {"coder": {"description": "A coder"}}},
            global_config={"model": "gpt-4o"},
        )
        coder = registry.get("coder")
        assert coder.model is None  # Not inherited from global

    def test_explicit_agent_with_own_model(self):
        registry = AgentRegistry(
            config={"agents": {"coder": {"model": "claude-3-opus"}}},
            global_config={"model": "gpt-4o"},
        )
        coder = registry.get("coder")
        assert coder.model == "claude-3-opus"

    def test_no_global_config_means_none_fields(self):
        registry = AgentRegistry(config={})
        main = registry.get("main")
        assert main.model is None
        assert main.provider is None
        assert main.personality is None
        assert main.reasoning is None
        assert main.max_turns is None

    def test_implicit_main_tool_policy_from_global(self):
        gc = {"tools": ["read_file", "write_file"]}
        registry = AgentRegistry(config={}, global_config=gc)
        main = registry.get("main")
        assert main.tool_policy is not None
        assert main.tool_policy.allow == ["read_file", "write_file"]


# =========================================================================
# Edge cases & integration-style tests
# =========================================================================

class TestEdgeCases:
    """Additional edge case and integration tests."""

    def test_agent_with_tool_policy_string_in_agents_section(self):
        registry = AgentRegistry(config={
            "agents": {
                "coder": {"tools": "coding"},
            },
        })
        agent = registry.get("coder")
        assert agent.tool_policy is not None
        assert agent.tool_policy.profile == "coding"

    def test_agent_with_tool_policy_dict_in_agents_section(self):
        registry = AgentRegistry(config={
            "agents": {
                "coder": {
                    "tools": {
                        "profile": "minimal",
                        "also_allow": ["terminal"],
                        "deny": ["clarify"],
                    },
                },
            },
        })
        agent = registry.get("coder")
        assert agent.tool_policy.profile == "minimal"
        assert agent.tool_policy.also_allow == ["terminal"]
        assert agent.tool_policy.deny == ["clarify"]

    def test_agent_with_subagent_policy(self):
        registry = AgentRegistry(config={
            "agents": {
                "orchestrator": {
                    "subagents": {"max_depth": 3, "max_children": 10},
                },
            },
        })
        agent = registry.get("orchestrator")
        assert agent.subagents.max_depth == 3
        assert agent.subagents.max_children == 10

    def test_agent_default_subagent_policy(self):
        registry = AgentRegistry(config={
            "agents": {"simple": {}},
        })
        agent = registry.get("simple")
        assert agent.subagents.max_depth == 2
        assert agent.subagents.max_children == 5

    def test_single_agent_in_agents_section_becomes_default(self):
        registry = AgentRegistry(config={
            "agents": {"solo": {"description": "Only agent"}},
        })
        default = registry.get_default()
        assert default.id == "solo"
        assert default.default is True

    def test_empty_agents_dict_treated_as_no_agents(self):
        """An empty agents dict is falsy, so implicit main is created."""
        registry = AgentRegistry(config={"agents": {}})
        agents = registry.list_agents()
        assert len(agents) == 1
        assert agents[0].id == "main"

    def test_tool_policy_field_alias(self):
        """tool_policy key also works (in addition to 'tools')."""
        registry = AgentRegistry(config={
            "agents": {
                "coder": {"tool_policy": "coding"},
            },
        })
        agent = registry.get("coder")
        assert agent.tool_policy is not None
        assert agent.tool_policy.profile == "coding"
