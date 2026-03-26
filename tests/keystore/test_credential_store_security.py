"""Security-focused tests for credential store behavior."""

import importlib
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _reset_cache():
    import keystore.credential_store as cs
    cs._cached_backend = None
    cs._detection_done = False
    yield
    cs._cached_backend = None
    cs._detection_done = False


def test_no_insecure_encrypted_file_auto_fallback_when_no_real_backend():
    """If keyring/keyctl are unavailable, remember backend must be unavailable.

    An automatically selected machine-derived encrypted file would be derivable
    by the same-user agent process and would collapse the keystore boundary.
    """
    import keystore.credential_store as cs

    with patch.dict("sys.modules", {"keyring": None}):
        with patch("subprocess.run", side_effect=OSError("not found")):
            importlib.reload(cs)
            assert cs.backend_name() is None
            assert cs.is_available() is False
            assert cs.store_passphrase("secret") is False
