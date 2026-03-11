"""Tests for multi-agent integration with existing components.

Covers:
  1. Session key namespacing via build_session_key
  2. ToolPolicy filtering via get_tool_definitions(agent_tool_policy=...)
  3. MemoryStore with custom memory_dir
  4. DEFAULT_CONFIG shape (agents, bindings, _config_version)
  5. /agents command presence in COMMANDS dict
"""

import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

from gateway.config import Platform
from gateway.session import SessionSource, build_session_key
from gateway.agent_registry import ToolPolicy


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_source(platform=Platform.TELEGRAM, chat_id="12345",
                 chat_type="dm", user_id=None):
    return SessionSource(
        platform=platform,
        chat_id=chat_id,
        chat_type=chat_type,
        user_id=user_id,
    )


# ===================================================================
# 1. Session key namespacing
# ===================================================================

class TestBuildSessionKeyNamespacing:
    """build_session_key must produce distinct keys for different agent_ids."""

    def test_different_agent_ids_produce_different_keys(self):
        source = _make_source()
        key_main = build_session_key(source, agent_id="main")
        key_helper = build_session_key(source, agent_id="helper")
        assert key_main != key_helper
        assert "agent:main:" in key_main
        assert "agent:helper:" in key_helper

    def test_backward_compat_main_agent(self):
        """agent_id='main' (the default) produces 'agent:main:<platform>:dm'."""
        source = _make_source(platform=Platform.TELEGRAM)
        key = build_session_key(source)  # defaults to agent_id='main'
        assert key == "agent:main:telegram:dm"

    def test_backward_compat_main_group(self):
        source = _make_source(platform=Platform.DISCORD, chat_type="group",
                              chat_id="guild-abc")
        key = build_session_key(source)
        assert key == "agent:main:discord:group:guild-abc"

    def test_agent_id_embedded_in_group_key(self):
        source = _make_source(platform=Platform.DISCORD, chat_type="group",
                              chat_id="guild-abc")
        key = build_session_key(source, agent_id="code-review")
        assert key == "agent:code-review:discord:group:guild-abc"

    def test_dm_scope_per_peer_includes_user_id(self):
        source = _make_source(user_id="user-42")
        key = build_session_key(source, dm_scope="per_peer")
        assert "user-42" in key
        assert key == "agent:main:telegram:dm:user-42"

    def test_dm_scope_per_peer_no_user_id_falls_back(self):
        """When user_id is None, per_peer falls back to the plain DM key."""
        source = _make_source(user_id=None)
        key = build_session_key(source, dm_scope="per_peer")
        assert key == "agent:main:telegram:dm"

    def test_dm_scope_default_ignores_user_id(self):
        """Default dm_scope='main' does NOT include user_id."""
        source = _make_source(user_id="user-42")
        key = build_session_key(source, dm_scope="main")
        assert "user-42" not in key

    def test_whatsapp_dm_includes_chat_id(self):
        """WhatsApp DMs always include chat_id (multi-user device)."""
        source = _make_source(platform=Platform.WHATSAPP, chat_id="wa-phone")
        key = build_session_key(source, agent_id="main")
        assert key == "agent:main:whatsapp:dm:wa-phone"


# ===================================================================
# 2. ToolPolicy filtering in get_tool_definitions
# ===================================================================

class TestToolPolicyFiltering:
    """get_tool_definitions should honour agent_tool_policy when provided."""

    @staticmethod
    def _make_tool_def(name):
        return {
            "type": "function",
            "function": {
                "name": name,
                "description": f"Stub for {name}",
                "parameters": {"type": "object", "properties": {}},
            },
        }

    def test_deny_removes_tool(self):
        """A ToolPolicy with deny=['terminal'] should remove terminal."""
        policy = ToolPolicy(deny=["terminal"])
        all_tools = {"terminal", "memory", "web_search"}
        result = policy.apply(all_tools)
        assert "terminal" not in result
        assert "memory" in result
        assert "web_search" in result

    def test_allow_restricts_to_listed(self):
        policy = ToolPolicy(allow=["memory", "web_search"])
        all_tools = {"terminal", "memory", "web_search", "read_file"}
        result = policy.apply(all_tools)
        assert result == {"memory", "web_search"}

    def test_profile_minimal(self):
        """The 'minimal' profile only keeps its allow list."""
        policy = ToolPolicy(profile="minimal")
        all_tools = {"terminal", "memory", "clarify", "todo", "session_search",
                      "web_search", "read_file"}
        result = policy.apply(all_tools)
        assert result == {"memory", "clarify", "todo", "session_search"}

    def test_deny_overrides_allow(self):
        """Deny always wins, even if the tool is in the allow list."""
        policy = ToolPolicy(allow=["memory", "terminal"], deny=["terminal"])
        all_tools = {"terminal", "memory", "web_search"}
        result = policy.apply(all_tools)
        assert result == {"memory"}

    @patch("model_tools.registry")
    @patch("model_tools.resolve_toolset")
    @patch("model_tools.validate_toolset", return_value=True)
    def test_get_tool_definitions_applies_policy(self, mock_validate,
                                                  mock_resolve, mock_reg):
        """End-to-end: get_tool_definitions respects agent_tool_policy."""
        from model_tools import get_tool_definitions

        mock_resolve.return_value = ["terminal", "memory", "web_search"]
        mock_reg.get_definitions.return_value = [
            self._make_tool_def("memory"),
            self._make_tool_def("web_search"),
        ]

        policy = ToolPolicy(deny=["terminal"])
        tools = get_tool_definitions(
            enabled_toolsets=["hermes-cli"],
            quiet_mode=True,
            agent_tool_policy=policy,
        )

        # registry.get_definitions should have been called with a set
        # that does NOT contain 'terminal'
        called_tools = mock_reg.get_definitions.call_args[0][0]
        assert "terminal" not in called_tools
        assert "memory" in called_tools


# ===================================================================
# 3. MemoryStore with custom memory_dir
# ===================================================================

class TestMemoryStoreCustomDir:
    """MemoryStore should use a custom memory_dir when provided."""

    def test_custom_dir_is_used(self, tmp_path):
        custom = tmp_path / "custom_memories"
        # MemoryStore.__init__ creates the directory
        from tools.memory_tool import MemoryStore
        store = MemoryStore(memory_dir=custom)
        assert store._memory_dir == custom
        assert custom.exists()

    def test_default_dir_is_global(self):
        """Without memory_dir, the store falls back to MEMORY_DIR."""
        from tools.memory_tool import MemoryStore, MEMORY_DIR
        with patch.object(Path, "mkdir"):  # avoid touching real FS
            store = MemoryStore()
        assert store._memory_dir == MEMORY_DIR

    def test_load_and_save_use_custom_dir(self, tmp_path):
        custom = tmp_path / "mem"
        from tools.memory_tool import MemoryStore
        store = MemoryStore(memory_dir=custom)
        store.load_from_disk()  # should not raise
        assert (custom).exists()
        # Save should write to custom dir
        store.memory_entries = ["fact one"]
        store.save_to_disk("memory")
        assert (custom / "MEMORY.md").exists()


# ===================================================================
# 4. Config shape
# ===================================================================

class TestDefaultConfigShape:
    """DEFAULT_CONFIG must contain multi-agent keys."""

    def test_agents_key_exists(self):
        from hermes_cli.config import DEFAULT_CONFIG
        assert "agents" in DEFAULT_CONFIG

    def test_bindings_key_exists(self):
        from hermes_cli.config import DEFAULT_CONFIG
        assert "bindings" in DEFAULT_CONFIG

    def test_agents_default_is_empty_dict(self):
        from hermes_cli.config import DEFAULT_CONFIG
        assert DEFAULT_CONFIG["agents"] == {}

    def test_bindings_default_is_empty_list(self):
        from hermes_cli.config import DEFAULT_CONFIG
        assert DEFAULT_CONFIG["bindings"] == []

    def test_config_version_is_7(self):
        from hermes_cli.config import DEFAULT_CONFIG
        assert DEFAULT_CONFIG["_config_version"] == 7


# ===================================================================
# 5. /agents command in COMMANDS dict
# ===================================================================

class TestAgentsCommand:
    """/agents must be registered in the COMMANDS dict."""

    def test_agents_in_commands(self):
        from hermes_cli.commands import COMMANDS
        assert "/agents" in COMMANDS

    def test_agents_has_description(self):
        from hermes_cli.commands import COMMANDS
        desc = COMMANDS["/agents"]
        assert isinstance(desc, str)
        assert len(desc) > 0
