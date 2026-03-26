"""Tests for keystore.client — high-level keystore API."""

import os
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

nacl = pytest.importorskip("nacl")
argon2 = pytest.importorskip("argon2")

from keystore.client import KeystoreClient, reset_keystore
from keystore.store import PassphraseMismatch, KeystoreLocked


@pytest.fixture(autouse=True)
def _reset_singleton():
    """Reset the global singleton before each test."""
    reset_keystore()
    yield
    reset_keystore()


@pytest.fixture
def ks(tmp_path):
    """Return an initialized and unlocked KeystoreClient."""
    db = tmp_path / "keystore" / "secrets.db"
    client = KeystoreClient(db)
    client.initialize("test-pass")
    return client


class TestEnsureUnlocked:
    def test_already_unlocked(self, ks):
        assert ks.ensure_unlocked() is True

    def test_unlock_from_credential_store(self, ks, tmp_path):
        ks.lock()
        with patch("keystore.credential_store.retrieve_passphrase", return_value="test-pass"):
            assert ks.ensure_unlocked(interactive=False) is True

    def test_unlock_from_env_var(self, ks):
        ks.lock()
        with patch.dict(os.environ, {"HERMES_KEYSTORE_PASSPHRASE": "test-pass"}):
            with patch("keystore.credential_store.retrieve_passphrase", return_value=None):
                assert ks.ensure_unlocked(interactive=False) is True

    def test_non_interactive_raises_when_locked(self, ks):
        ks.lock()
        with patch("keystore.credential_store.retrieve_passphrase", return_value=None):
            with patch.dict(os.environ, {}, clear=False):
                # Remove the env var if present
                os.environ.pop("HERMES_KEYSTORE_PASSPHRASE", None)
                with pytest.raises(KeystoreLocked):
                    ks.ensure_unlocked(interactive=False)

    def test_not_initialized_returns_false(self, tmp_path):
        db = tmp_path / "ks2" / "secrets.db"
        client = KeystoreClient(db)
        assert client.ensure_unlocked(interactive=False) is False


class TestInjectEnv:
    def test_inject_populates_environ(self, ks):
        ks.set_secret("TEST_INJECT_KEY_1", "value1")
        ks.set_secret("TEST_INJECT_KEY_2", "value2")

        # Clear any existing env vars
        os.environ.pop("TEST_INJECT_KEY_1", None)
        os.environ.pop("TEST_INJECT_KEY_2", None)

        injected = ks.inject_env()
        assert os.environ.get("TEST_INJECT_KEY_1") == "value1"
        assert os.environ.get("TEST_INJECT_KEY_2") == "value2"
        assert injected["TEST_INJECT_KEY_1"] is True
        assert injected["TEST_INJECT_KEY_2"] is True

        # Cleanup
        os.environ.pop("TEST_INJECT_KEY_1", None)
        os.environ.pop("TEST_INJECT_KEY_2", None)

    def test_inject_does_not_overwrite_existing(self, ks):
        ks.set_secret("TEST_INJECT_EXISTING", "from-keystore")
        os.environ["TEST_INJECT_EXISTING"] = "from-shell"

        injected = ks.inject_env()
        assert os.environ["TEST_INJECT_EXISTING"] == "from-shell"
        assert injected["TEST_INJECT_EXISTING"] is False

        os.environ.pop("TEST_INJECT_EXISTING", None)

    def test_inject_skips_non_injectable(self, ks):
        ks.set_secret("SEALED_KEY", "secret", category="sealed")
        ks.set_secret("USER_KEY", "secret", category="user_only")

        os.environ.pop("SEALED_KEY", None)
        os.environ.pop("USER_KEY", None)

        injected = ks.inject_env()
        assert "SEALED_KEY" not in injected
        assert "USER_KEY" not in injected
        assert "SEALED_KEY" not in os.environ
        assert "USER_KEY" not in os.environ


class TestMigrateFromEnv:
    def test_migrate_basic(self, ks, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text(
            "# Comment line\n"
            "OPENROUTER_API_KEY=sk-or-test-123\n"
            "FAL_KEY=fal_test_456\n"
            "SOME_CONFIG=not-a-secret\n"
            "EMPTY_VAR=\n"
        )
        migrated = ks.migrate_from_env(env_file)
        assert "OPENROUTER_API_KEY" in migrated
        assert "FAL_KEY" in migrated
        # Non-secret short values should be skipped
        assert "EMPTY_VAR" not in migrated

        # Verify values are stored correctly
        assert ks.get_secret("OPENROUTER_API_KEY") == "sk-or-test-123"
        assert ks.get_secret("FAL_KEY") == "fal_test_456"

    def test_migrate_quoted_values(self, ks, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text(
            'MY_API_KEY="sk-quoted-value"\n'
            "OTHER_TOKEN='single-quoted-value'\n"
        )
        migrated = ks.migrate_from_env(env_file)
        assert ks.get_secret("MY_API_KEY") == "sk-quoted-value"
        assert ks.get_secret("OTHER_TOKEN") == "single-quoted-value"

    def test_migrate_nonexistent_file(self, ks, tmp_path):
        migrated = ks.migrate_from_env(tmp_path / "nonexistent")
        assert migrated == {}

    def test_migrate_assigns_categories(self, ks, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text(
            "OPENROUTER_API_KEY=sk-test\n"
            "SUDO_PASSWORD=mysudopass\n"
            "GITHUB_TOKEN=ghp_test1234567890\n"
        )
        migrated = ks.migrate_from_env(env_file)
        assert migrated.get("OPENROUTER_API_KEY") == "injectable"
        assert migrated.get("SUDO_PASSWORD") == "user_only"
        assert migrated.get("GITHUB_TOKEN") == "gated"


class TestSecretManagement:
    def test_set_and_get(self, ks):
        ks.set_secret("MY_KEY", "my-value", description="Test key")
        assert ks.get_secret("MY_KEY") == "my-value"

    def test_delete(self, ks):
        ks.set_secret("DEL_KEY", "val")
        assert ks.delete_secret("DEL_KEY")
        assert ks.get_secret("DEL_KEY") is None

    def test_list(self, ks):
        ks.set_secret("A", "1")
        ks.set_secret("B", "2")
        secrets = ks.list_secrets()
        assert len(secrets) == 2

    def test_set_category_validation(self, ks):
        ks.set_secret("KEY", "val")
        from keystore.store import KeystoreError
        with pytest.raises(KeystoreError, match="Invalid category"):
            ks.set_category("KEY", "bogus")


class TestRememberForget:
    def test_remember_no_backend(self, ks):
        with patch("keystore.credential_store.is_available", return_value=False):
            with patch("keystore.credential_store.backend_name", return_value=None):
                success, msg = ks.remember_passphrase("test-pass")
                assert not success
                assert "No credential store" in msg

    def test_remember_success(self, ks):
        with patch("keystore.credential_store.is_available", return_value=True):
            with patch("keystore.credential_store.backend_name", return_value="Test Backend"):
                with patch("keystore.credential_store.store_passphrase", return_value=True):
                    success, msg = ks.remember_passphrase("test-pass")
                    assert success
                    assert msg == "Test Backend"

    def test_forget(self, ks):
        with patch("keystore.credential_store.delete_passphrase", return_value=True):
            with patch("keystore.credential_store.backend_name", return_value="Test"):
                success, msg = ks.forget_passphrase()
                assert success
