"""Tests for keystore.store — encrypted SQLite secret store."""

import os
import tempfile
from pathlib import Path

import pytest

# Skip entire module if keystore deps aren't installed
nacl = pytest.importorskip("nacl")
argon2 = pytest.importorskip("argon2")

from keystore.store import (
    EncryptedStore,
    KeystoreError,
    KeystoreLocked,
    KeystoreCorrupted,
    PassphraseMismatch,
)


@pytest.fixture
def tmp_db(tmp_path):
    """Return a path for a temporary keystore DB."""
    return tmp_path / "keystore" / "secrets.db"


@pytest.fixture
def store(tmp_db):
    """Return an initialized and unlocked store."""
    s = EncryptedStore(tmp_db)
    s.initialize("test-passphrase")
    return s


class TestInitialization:
    def test_initialize_creates_db(self, tmp_db):
        s = EncryptedStore(tmp_db)
        assert not s.is_initialized
        s.initialize("my-pass")
        assert s.is_initialized
        assert tmp_db.exists()

    def test_initialize_sets_permissions(self, tmp_db):
        s = EncryptedStore(tmp_db)
        s.initialize("my-pass")
        # Directory should be 0700
        assert oct(tmp_db.parent.stat().st_mode & 0o777) == oct(0o700)
        # File should be 0600
        assert oct(tmp_db.stat().st_mode & 0o777) == oct(0o600)

    def test_initialize_twice_raises(self, store, tmp_db):
        s2 = EncryptedStore(tmp_db)
        with pytest.raises(KeystoreError, match="already initialized"):
            s2.initialize("another-pass")

    def test_initialize_unlocks_immediately(self, tmp_db):
        s = EncryptedStore(tmp_db)
        assert not s.is_unlocked
        s.initialize("pass")
        assert s.is_unlocked


class TestUnlockLock:
    def test_unlock_correct_passphrase(self, tmp_db):
        s = EncryptedStore(tmp_db)
        s.initialize("correct-pass")
        s.lock()
        assert not s.is_unlocked
        s.unlock("correct-pass")
        assert s.is_unlocked

    def test_unlock_wrong_passphrase(self, tmp_db):
        s = EncryptedStore(tmp_db)
        s.initialize("correct-pass")
        s.lock()
        with pytest.raises(PassphraseMismatch):
            s.unlock("wrong-pass")

    def test_unlock_not_initialized(self, tmp_db):
        s = EncryptedStore(tmp_db)
        with pytest.raises(KeystoreError, match="not initialized"):
            s.unlock("any-pass")

    def test_lock_clears_key(self, store):
        assert store.is_unlocked
        store.lock()
        assert not store.is_unlocked

    def test_operations_fail_when_locked(self, store):
        store.lock()
        with pytest.raises(KeystoreLocked):
            store.set("KEY", "value")
        with pytest.raises(KeystoreLocked):
            store.get("KEY")
        with pytest.raises(KeystoreLocked):
            store.list_secrets()


class TestSecretCRUD:
    def test_set_and_get(self, store):
        store.set("MY_KEY", "my-secret-value", category="injectable")
        assert store.get("MY_KEY") == "my-secret-value"

    def test_get_nonexistent_returns_none(self, store):
        assert store.get("DOES_NOT_EXIST") is None

    def test_set_overwrites(self, store):
        store.set("KEY", "value1")
        store.set("KEY", "value2")
        assert store.get("KEY") == "value2"

    def test_delete(self, store):
        store.set("KEY", "value")
        assert store.delete("KEY")
        assert store.get("KEY") is None

    def test_delete_nonexistent(self, store):
        assert not store.delete("NOPE")

    def test_list_secrets(self, store):
        store.set("A_KEY", "val1", category="injectable", description="First key")
        store.set("B_KEY", "val2", category="gated", description="Second key")
        secrets = store.list_secrets()
        assert len(secrets) == 2
        names = [s.name for s in secrets]
        assert "A_KEY" in names
        assert "B_KEY" in names
        # Values should NOT be in the listing
        for s in secrets:
            assert not hasattr(s, "value")

    def test_secret_count(self, store):
        assert store.secret_count() == 0
        store.set("K1", "v1")
        store.set("K2", "v2")
        assert store.secret_count() == 2

    def test_unicode_values(self, store):
        store.set("UNICODE", "こんにちは世界 🔐")
        assert store.get("UNICODE") == "こんにちは世界 🔐"

    def test_long_values(self, store):
        long_val = "x" * 10000
        store.set("LONG", long_val)
        assert store.get("LONG") == long_val

    def test_empty_string_value(self, store):
        store.set("EMPTY", "")
        assert store.get("EMPTY") == ""


class TestCategories:
    def test_set_category(self, store):
        store.set("KEY", "val", category="injectable")
        assert store.set_category("KEY", "gated")
        secrets = store.list_secrets()
        assert secrets[0].category == "gated"

    def test_sealed_denied_to_agent(self, store):
        store.set("WALLET_KEY", "private-key", category="sealed")
        # Agent requester should be denied
        assert store.get("WALLET_KEY", requester="agent") is None
        # Daemon requester should succeed
        assert store.get("WALLET_KEY", requester="daemon") == "private-key"

    def test_user_only_denied_to_agent(self, store):
        store.set("SUDO_PASS", "password", category="user_only")
        assert store.get("SUDO_PASS", requester="agent") is None
        assert store.get("SUDO_PASS", requester="gateway") is None
        assert store.get("SUDO_PASS", requester="cli") == "password"

    def test_injectable_accessible_to_all(self, store):
        store.set("API_KEY", "sk-123", category="injectable")
        assert store.get("API_KEY", requester="agent") == "sk-123"
        assert store.get("API_KEY", requester="cli") == "sk-123"
        assert store.get("API_KEY", requester="gateway") == "sk-123"


class TestInjectableSecrets:
    def test_get_injectable_secrets(self, store):
        store.set("KEY1", "val1", category="injectable")
        store.set("KEY2", "val2", category="injectable")
        store.set("KEY3", "val3", category="sealed")
        store.set("KEY4", "val4", category="user_only")

        injectable = store.get_injectable_secrets()
        assert injectable == {"KEY1": "val1", "KEY2": "val2"}

    def test_get_injectable_empty(self, store):
        assert store.get_injectable_secrets() == {}


class TestAccessLog:
    def test_access_logged(self, store):
        store.set("KEY", "val")
        store.get("KEY", requester="agent")
        log = store.get_access_log(limit=10)
        assert len(log) >= 2  # write + read
        actions = [e["action"] for e in log]
        assert "write" in actions
        assert "read" in actions

    def test_denied_access_logged(self, store):
        store.set("SECRET", "val", category="sealed")
        store.get("SECRET", requester="agent")  # should be denied
        log = store.get_access_log(limit=5)
        assert any(e["action"] == "denied" for e in log)


class TestChangePassphrase:
    def test_change_passphrase(self, tmp_db):
        s = EncryptedStore(tmp_db)
        s.initialize("old-pass")
        s.set("KEY", "my-value")
        s.change_passphrase("old-pass", "new-pass")

        # Old passphrase should fail
        s.lock()
        with pytest.raises(PassphraseMismatch):
            s.unlock("old-pass")

        # New passphrase should work and data should be intact
        s.unlock("new-pass")
        assert s.get("KEY") == "my-value"

    def test_change_passphrase_wrong_old(self, store):
        with pytest.raises(PassphraseMismatch):
            store.change_passphrase("wrong-old", "new-pass")


class TestEncryptionIntegrity:
    def test_different_passphrases_different_ciphertext(self, tmp_path):
        """Two stores with different passphrases produce different ciphertext."""
        db1 = tmp_path / "s1" / "secrets.db"
        db2 = tmp_path / "s2" / "secrets.db"

        s1 = EncryptedStore(db1)
        s1.initialize("pass1")
        s1.set("KEY", "same-value")

        s2 = EncryptedStore(db2)
        s2.initialize("pass2")
        s2.set("KEY", "same-value")

        # Read raw ciphertext from both DBs
        import sqlite3
        c1 = sqlite3.connect(str(db1)).execute(
            "SELECT encrypted_value FROM secrets WHERE name='KEY'"
        ).fetchone()[0]
        c2 = sqlite3.connect(str(db2)).execute(
            "SELECT encrypted_value FROM secrets WHERE name='KEY'"
        ).fetchone()[0]

        assert c1 != c2  # Different keys → different ciphertext

    def test_same_value_different_nonce(self, store):
        """Setting the same value twice produces different ciphertext (random nonce)."""
        import sqlite3
        store.set("KEY", "same-value")
        c1 = sqlite3.connect(str(store._db_path)).execute(
            "SELECT encrypted_value FROM secrets WHERE name='KEY'"
        ).fetchone()[0]

        store.set("KEY", "same-value")
        c2 = sqlite3.connect(str(store._db_path)).execute(
            "SELECT encrypted_value FROM secrets WHERE name='KEY'"
        ).fetchone()[0]

        assert c1 != c2  # Random nonce → different ciphertext each time
