"""Tests for keystore.credential_store — cross-platform passphrase caching."""

import os
import platform
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from keystore.credential_store import (
    _KeyringBackend,
    _KeyctlBackend,
    _EncryptedFileBackend,
    _get_machine_id,
    _detect_backend,
    is_available,
    backend_name,
    store_passphrase,
    retrieve_passphrase,
    delete_passphrase,
)


# Reset cached backend between tests
@pytest.fixture(autouse=True)
def _reset_cache():
    import keystore.credential_store as cs
    cs._cached_backend = None
    cs._detection_done = False
    yield
    cs._cached_backend = None
    cs._detection_done = False


class TestMachineId:
    def test_returns_string(self):
        mid = _get_machine_id()
        assert isinstance(mid, str)
        assert len(mid) > 0

    def test_stable(self):
        """Machine ID should be the same across calls."""
        assert _get_machine_id() == _get_machine_id()


class TestEncryptedFileBackend:
    """Test the encrypted file fallback (always available with pynacl)."""

    @pytest.fixture
    def backend(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
        return _EncryptedFileBackend()

    @pytest.mark.skipif(
        not pytest.importorskip("nacl", reason="pynacl not installed"),
        reason="pynacl required",
    )
    def test_store_and_retrieve(self, backend):
        assert backend.store("my-secret-passphrase")
        assert backend.retrieve() == "my-secret-passphrase"

    @pytest.mark.skipif(
        not pytest.importorskip("nacl", reason="pynacl not installed"),
        reason="pynacl required",
    )
    def test_delete(self, backend):
        backend.store("passphrase")
        assert backend.delete()
        assert backend.retrieve() is None

    @pytest.mark.skipif(
        not pytest.importorskip("nacl", reason="pynacl not installed"),
        reason="pynacl required",
    )
    def test_retrieve_nonexistent(self, backend):
        assert backend.retrieve() is None

    @pytest.mark.skipif(
        not pytest.importorskip("nacl", reason="pynacl not installed"),
        reason="pynacl required",
    )
    def test_overwrite(self, backend):
        backend.store("first")
        backend.store("second")
        assert backend.retrieve() == "second"


class TestKeyringBackend:
    def test_wraps_keyring_module(self):
        mock_kr = MagicMock()
        # Create a real class to simulate a keyring backend
        class FakeWinVault:
            pass
        FakeWinVault.__name__ = "WinVaultKeyring"
        mock_kr.get_keyring.return_value = FakeWinVault()
        backend = _KeyringBackend(mock_kr)
        assert backend.name == "Windows Credential Locker"

    def test_store_calls_set_password(self):
        mock_kr = MagicMock()
        mock_kr.get_keyring.return_value = MagicMock()
        backend = _KeyringBackend(mock_kr)
        backend.store("pass123")
        mock_kr.set_password.assert_called_once()

    def test_retrieve_calls_get_password(self):
        mock_kr = MagicMock()
        mock_kr.get_keyring.return_value = MagicMock()
        mock_kr.get_password.return_value = "stored-pass"
        backend = _KeyringBackend(mock_kr)
        assert backend.retrieve() == "stored-pass"

    def test_store_handles_exception(self):
        mock_kr = MagicMock()
        mock_kr.get_keyring.return_value = MagicMock()
        mock_kr.set_password.side_effect = Exception("D-Bus error")
        backend = _KeyringBackend(mock_kr)
        assert backend.store("pass") is False


class TestDetection:
    def test_detect_with_no_backends(self):
        """When keyring is unavailable and keyctl is missing, should fall back to encrypted file."""
        with patch.dict("sys.modules", {"keyring": None}):
            with patch("subprocess.run", side_effect=OSError("not found")):
                # If pynacl is available, should get encrypted file backend
                try:
                    import nacl.secret  # noqa
                    backend = _detect_backend()
                    if backend is not None:
                        assert isinstance(backend, _EncryptedFileBackend)
                except ImportError:
                    backend = _detect_backend()
                    assert backend is None

    def test_public_api_consistency(self):
        """is_available and backend_name should agree."""
        if is_available():
            assert backend_name() is not None
        else:
            assert backend_name() is None


class TestPublicAPI:
    """Test the module-level public functions with mocked backend."""

    def test_store_and_retrieve_with_mock(self):
        import keystore.credential_store as cs
        mock_backend = MagicMock()
        mock_backend.store.return_value = True
        mock_backend.retrieve.return_value = "cached-pass"
        mock_backend.name = "Mock Backend"
        cs._cached_backend = mock_backend
        cs._detection_done = True

        assert store_passphrase("my-pass") is True
        mock_backend.store.assert_called_with("my-pass")

        assert retrieve_passphrase() == "cached-pass"
        assert backend_name() == "Mock Backend"
        assert is_available() is True

    def test_no_backend_returns_none(self):
        import keystore.credential_store as cs
        cs._cached_backend = None
        cs._detection_done = True

        assert store_passphrase("pass") is False
        assert retrieve_passphrase() is None
        assert delete_passphrase() is False
        assert is_available() is False
        assert backend_name() is None
