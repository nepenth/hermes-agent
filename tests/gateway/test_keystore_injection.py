"""Gateway keystore injection regression tests."""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

import pytest

nacl = pytest.importorskip("nacl")
argon2 = pytest.importorskip("argon2")


def _reload_gateway_run(monkeypatch, home: Path):
    monkeypatch.setenv("HERMES_HOME", str(home))
    # Reset cached singletons that capture prior HERMES_HOME or lock state.
    try:
        from keystore.client import reset_keystore
        reset_keystore()
    except Exception:
        pass
    try:
        from wallet.runtime import reset_runtime
        reset_runtime()
    except Exception:
        pass
    sys.modules.pop("gateway.run", None)
    import gateway.run as gateway_run
    importlib.reload(gateway_run)
    return gateway_run


def test_gateway_import_injects_keystore_without_config_yaml(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir(parents=True)
    (home / ".env").write_text("")

    # Initialize keystore with a secret, but do not create config.yaml.
    monkeypatch.setenv("HERMES_HOME", str(home))
    from keystore.client import KeystoreClient, reset_keystore
    reset_keystore()
    ks = KeystoreClient(home / "keystore" / "secrets.db")
    ks.initialize("passphrase")
    ks.set_secret("OPENAI_API_KEY", "sk-test-from-keystore")

    monkeypatch.setenv("HERMES_KEYSTORE_PASSPHRASE", "passphrase")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    gateway_run = _reload_gateway_run(monkeypatch, home)
    assert os.environ.get("OPENAI_API_KEY") == "sk-test-from-keystore"


def test_gateway_refresh_reinjects_keystore_secret(monkeypatch, tmp_path):
    home = tmp_path / ".hermes"
    home.mkdir(parents=True)
    (home / ".env").write_text("")
    (home / "config.yaml").write_text("toolsets:\n- hermes-cli\n")

    monkeypatch.setenv("HERMES_HOME", str(home))
    from keystore.client import KeystoreClient, reset_keystore
    reset_keystore()
    ks = KeystoreClient(home / "keystore" / "secrets.db")
    ks.initialize("passphrase")
    ks.set_secret("OPENAI_API_KEY", "sk-old")
    monkeypatch.setenv("HERMES_KEYSTORE_PASSPHRASE", "passphrase")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    gateway_run = _reload_gateway_run(monkeypatch, home)
    assert os.environ.get("OPENAI_API_KEY") == "sk-old"

    # Rotate secret in keystore; refresh must overwrite the stale in-process env var.
    ks.set_secret("OPENAI_API_KEY", "sk-new")
    os.environ["OPENAI_API_KEY"] = "stale"
    gateway_run._inject_keystore_env(force=True)
    assert os.environ.get("OPENAI_API_KEY") == "sk-new"
