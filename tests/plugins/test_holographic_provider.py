"""Tests for the holographic memory MemoryProvider adapter.

Tests the HolographicMemoryProvider interface — registration, tool handling,
prefetch, session end hooks, and memory bridging.
"""

import json
import sys
import pytest
from pathlib import Path
from unittest.mock import MagicMock

# Add plugin dir to path so imports work
_plugin_dir = Path(__file__).resolve().parent.parent.parent / "plugins" / "hermes-memory-store"
sys.path.insert(0, str(_plugin_dir))

from agent.memory_manager import MemoryManager
from agent.builtin_memory_provider import BuiltinMemoryProvider


def _make_provider(tmp_path, config=None):
    """Create a HolographicMemoryProvider with a temp DB."""
    # Import inside function to avoid module-level issues
    sys.path.insert(0, str(_plugin_dir))
    from plugins import HolographicMemoryProvider  # noqa: F811
    # Use the full import path
    from importlib import import_module
    init_mod = import_module("plugins.hermes-memory-store")

    cfg = config or {}
    cfg.setdefault("db_path", str(tmp_path / "test.db"))
    provider = init_mod.HolographicMemoryProvider(config=cfg)
    provider.initialize(session_id="test-session")
    return provider


@pytest.fixture
def provider(tmp_path):
    """Create an initialized holographic provider."""
    sys.path.insert(0, str(_plugin_dir.parent))
    # Direct import
    spec_path = _plugin_dir / "__init__.py"
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "hermes_memory_store_test",
        spec_path,
        submodule_search_locations=[str(_plugin_dir)],
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["hermes_memory_store_test"] = mod
    # Pre-populate submodule references
    store_spec = importlib.util.spec_from_file_location(
        "hermes_memory_store_test.store",
        _plugin_dir / "store.py",
    )
    store_mod = importlib.util.module_from_spec(store_spec)
    sys.modules["hermes_memory_store_test.store"] = store_mod
    store_spec.loader.exec_module(store_mod)

    retrieval_spec = importlib.util.spec_from_file_location(
        "hermes_memory_store_test.retrieval",
        _plugin_dir / "retrieval.py",
    )
    retrieval_mod = importlib.util.module_from_spec(retrieval_spec)
    sys.modules["hermes_memory_store_test.retrieval"] = retrieval_mod
    retrieval_spec.loader.exec_module(retrieval_mod)

    spec.loader.exec_module(mod)

    cfg = {"db_path": str(tmp_path / "test.db")}
    p = mod.HolographicMemoryProvider(config=cfg)
    p.initialize(session_id="test-session")
    yield p
    p.shutdown()

    # Cleanup
    for key in list(sys.modules):
        if key.startswith("hermes_memory_store_test"):
            del sys.modules[key]


class TestProviderRegistration:
    def test_register_calls_register_memory_provider(self, tmp_path):
        """register(ctx) should call ctx.register_memory_provider()."""
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "hermes_memory_store_reg",
            _plugin_dir / "__init__.py",
            submodule_search_locations=[str(_plugin_dir)],
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules["hermes_memory_store_reg"] = mod

        store_spec = importlib.util.spec_from_file_location(
            "hermes_memory_store_reg.store", _plugin_dir / "store.py")
        store_mod = importlib.util.module_from_spec(store_spec)
        sys.modules["hermes_memory_store_reg.store"] = store_mod
        store_spec.loader.exec_module(store_mod)

        retrieval_spec = importlib.util.spec_from_file_location(
            "hermes_memory_store_reg.retrieval", _plugin_dir / "retrieval.py")
        retrieval_mod = importlib.util.module_from_spec(retrieval_spec)
        sys.modules["hermes_memory_store_reg.retrieval"] = retrieval_mod
        retrieval_spec.loader.exec_module(retrieval_mod)

        spec.loader.exec_module(mod)

        ctx = MagicMock()
        mod.register(ctx)
        ctx.register_memory_provider.assert_called_once()
        registered = ctx.register_memory_provider.call_args[0][0]
        assert registered.name == "holographic"

        for key in list(sys.modules):
            if key.startswith("hermes_memory_store_reg"):
                del sys.modules[key]


class TestToolHandling:
    def test_add_and_search(self, provider):
        """Add a fact via tool call, then search for it."""
        result = json.loads(provider.handle_tool_call(
            "fact_store", {"action": "add", "content": "User prefers vim over emacs"}
        ))
        assert "fact_id" in result
        fact_id = result["fact_id"]

        result = json.loads(provider.handle_tool_call(
            "fact_store", {"action": "search", "query": "vim emacs"}
        ))
        assert result["count"] >= 1
        contents = [r["content"] for r in result["results"]]
        assert any("vim" in c for c in contents)

    def test_add_and_probe(self, provider):
        """Add facts about an entity, then probe it."""
        provider.handle_tool_call(
            "fact_store", {"action": "add", "content": "Peppi uses Rust for systems work"}
        )
        provider.handle_tool_call(
            "fact_store", {"action": "add", "content": "Peppi prefers Neovim"}
        )

        result = json.loads(provider.handle_tool_call(
            "fact_store", {"action": "probe", "entity": "peppi"}
        ))
        assert result["count"] >= 1

    def test_related(self, provider):
        """Test related entity lookup."""
        provider.handle_tool_call(
            "fact_store", {"action": "add", "content": "Peppi uses Rust for systems work"}
        )
        provider.handle_tool_call(
            "fact_store", {"action": "add", "content": "Rust ensures memory safety"}
        )

        result = json.loads(provider.handle_tool_call(
            "fact_store", {"action": "related", "entity": "rust"}
        ))
        assert "results" in result
        assert "count" in result

    def test_reason(self, provider):
        """Test compositional reasoning across entities."""
        provider.handle_tool_call(
            "fact_store", {"action": "add", "content": "Peppi uses Rust for backend work"}
        )
        provider.handle_tool_call(
            "fact_store", {"action": "add", "content": "The backend handles API requests"}
        )

        result = json.loads(provider.handle_tool_call(
            "fact_store", {"action": "reason", "entities": ["peppi", "backend"]}
        ))
        assert "results" in result

    def test_feedback(self, provider):
        """Test trust scoring via feedback."""
        result = json.loads(provider.handle_tool_call(
            "fact_store", {"action": "add", "content": "Test feedback fact"}
        ))
        fact_id = result["fact_id"]

        result = json.loads(provider.handle_tool_call(
            "fact_feedback", {"action": "helpful", "fact_id": fact_id}
        ))
        assert "error" not in result

    def test_update_and_remove(self, provider):
        """Test CRUD operations."""
        result = json.loads(provider.handle_tool_call(
            "fact_store", {"action": "add", "content": "Will be updated"}
        ))
        fact_id = result["fact_id"]

        # Update
        result = json.loads(provider.handle_tool_call(
            "fact_store", {"action": "update", "fact_id": fact_id, "content": "Updated content"}
        ))
        assert result["updated"]

        # Remove
        result = json.loads(provider.handle_tool_call(
            "fact_store", {"action": "remove", "fact_id": fact_id}
        ))
        assert result["removed"]

    def test_all_handlers_return_json(self, provider):
        """Every tool call must return valid JSON."""
        # Add a fact first
        r = provider.handle_tool_call("fact_store", {"action": "add", "content": "JSON test"})
        parsed = json.loads(r)
        fact_id = parsed["fact_id"]

        # Test every action
        actions = [
            ("fact_store", {"action": "search", "query": "JSON"}),
            ("fact_store", {"action": "list"}),
            ("fact_store", {"action": "probe", "entity": "test"}),
            ("fact_store", {"action": "related", "entity": "test"}),
            ("fact_store", {"action": "reason", "entities": ["test"]}),
            ("fact_store", {"action": "contradict"}),
            ("fact_feedback", {"action": "helpful", "fact_id": fact_id}),
        ]
        for tool_name, args in actions:
            result = provider.handle_tool_call(tool_name, args)
            json.loads(result)  # Should not raise


class TestPrefetch:
    def test_prefetch_returns_matching_facts(self, provider):
        """Prefetch should return facts matching the query."""
        provider.handle_tool_call(
            "fact_store", {"action": "add", "content": "The deploy pipeline uses Docker"}
        )
        result = provider.prefetch("deploy pipeline")
        assert "Docker" in result or "deploy" in result

    def test_prefetch_empty_when_no_facts(self, provider):
        assert provider.prefetch("anything") == ""


class TestSystemPromptBlock:
    def test_empty_when_no_facts(self, provider):
        assert provider.system_prompt_block() == ""

    def test_shows_count_with_facts(self, provider):
        provider.handle_tool_call(
            "fact_store", {"action": "add", "content": "Fact one"}
        )
        provider.handle_tool_call(
            "fact_store", {"action": "add", "content": "Fact two"}
        )
        block = provider.system_prompt_block()
        assert "2 facts" in block
        assert "Holographic" in block


class TestSessionEndHook:
    def test_extracts_preferences(self, provider):
        """on_session_end should extract preference patterns."""
        provider._config["auto_extract"] = True
        messages = [
            {"role": "user", "content": "I prefer dark mode for all my editors"},
            {"role": "assistant", "content": "Noted, I'll remember that."},
        ]
        provider.on_session_end(messages)
        result = json.loads(provider.handle_tool_call(
            "fact_store", {"action": "list"}
        ))
        assert result["count"] >= 1

    def test_skips_when_disabled(self, provider):
        """on_session_end should do nothing when auto_extract is False."""
        provider._config["auto_extract"] = False
        messages = [
            {"role": "user", "content": "I prefer dark mode"},
        ]
        provider.on_session_end(messages)
        result = json.loads(provider.handle_tool_call(
            "fact_store", {"action": "list"}
        ))
        assert result["count"] == 0

    def test_skips_assistant_messages(self, provider):
        """Only user messages should be scanned."""
        provider._config["auto_extract"] = True
        messages = [
            {"role": "assistant", "content": "I prefer to help you with that"},
        ]
        provider.on_session_end(messages)
        result = json.loads(provider.handle_tool_call(
            "fact_store", {"action": "list"}
        ))
        assert result["count"] == 0


class TestMemoryBridge:
    def test_mirrors_builtin_writes(self, provider):
        """on_memory_write should store facts from the builtin memory tool."""
        provider.on_memory_write("add", "user", "Timezone: US Pacific")
        result = json.loads(provider.handle_tool_call(
            "fact_store", {"action": "search", "query": "timezone pacific"}
        ))
        assert result["count"] >= 1


class TestManagerIntegration:
    def test_coexists_with_builtin(self, provider):
        """Holographic provider works alongside builtin in MemoryManager."""
        mgr = MemoryManager()
        mgr.add_provider(BuiltinMemoryProvider())
        mgr.add_provider(provider)

        assert mgr.provider_names == ["builtin", "holographic"]

        # Tools from holographic are available
        schemas = mgr.get_all_tool_schemas()
        names = {s["name"] for s in schemas}
        assert "fact_store" in names
        assert "fact_feedback" in names

        # Tool routing works
        result = json.loads(mgr.handle_tool_call(
            "fact_store", {"action": "add", "content": "Manager integration test"}
        ))
        assert result["status"] == "added"

        # Memory bridge fires
        mgr.on_memory_write("add", "memory", "Test fact from builtin")
        result = json.loads(mgr.handle_tool_call(
            "fact_store", {"action": "search", "query": "test fact builtin"}
        ))
        assert result["count"] >= 1
