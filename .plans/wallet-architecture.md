# Hermes Agent — Secure Keystore & Wallet Architecture Plan

**Status:** Draft v2  
**Date:** 2026-03-26  
**Author:** Vulcan (with Shannon)

---

## Executive Summary

This plan covers **two layers**:

1. **`hermes-keystore`** — A general-purpose encrypted secret store that replaces plaintext `.env` for sensitive values. Any secret the agent shouldn't see directly (API keys, tokens, private keys, passwords) goes here. This is a standalone component usable by the entire Hermes ecosystem.

2. **`hermes-wallet`** — Crypto wallet functionality built ON TOP of the keystore. The wallet daemon uses the keystore for private key storage and adds chain interaction, policy engine, and transaction signing.

The keystore is the foundation. The wallet is a consumer of it. Other things that should use the keystore: skill API keys, SSH keys for terminal backends, messaging platform tokens — anything currently sitting in plaintext `.env`.

---

## Why Two Layers?

Currently, Hermes secrets live in `~/.hermes/.env` as **plaintext key=value pairs**. The agent process loads them into `os.environ` at startup. The protections are:

| Protection | What it does | What it doesn't do |
|---|---|---|
| `WRITE_DENIED_PATHS` | Blocks agent from writing to `.env` | Doesn't block reading |
| `redact_sensitive_text` | Regex-masks known patterns in tool output | Bypassable with creative formatting |
| `prompt_for_secret` | Stores secrets without showing them to the model | Still writes plaintext to `.env` |
| File permissions (`0600`) | OS-level access control | Useless if agent runs as same user |

**The gap:** A compromised agent (prompt injection, malicious skill, MCP tool poisoning) can read `.env` via `read_file`, `terminal("cat ~/.hermes/.env")`, or `os.getenv()` in `execute_code`. The redaction layer is defense-in-depth but not a security boundary.

The keystore fixes this by making secrets **architecturally inaccessible** to the agent process.

---

## Part 1: General Encrypted Keystore (`hermes-keystore`)

### 1.1 Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    HERMES AGENT PROCESS                       │
│                                                               │
│  os.environ["OPENROUTER_API_KEY"]  ← injected by daemon     │
│  os.environ["FAL_KEY"]             ← injected by daemon     │
│  os.environ["HERMES_WALLET_TOKEN"] ← injected by daemon     │
│                                                               │
│  The agent sees env vars as before.                          │
│  But there's no .env file to read. No plaintext on disk.     │
│                                                               │
│  ⚠️  Known limitation: injectable secrets ARE in os.environ,  │
│  so execute_code can still read them. The real fix is a       │
│  host-side proxy that injects creds at the transport layer    │
│  (sandboxing roadmap, out of scope here). For now, injectable │
│  secrets protect against file exfiltration but not against    │
│  code execution in the agent's process. Wallet private keys   │
│  (sealed category) are NOT injectable and remain fully        │
│  protected even against execute_code.                         │
│                                                               │
│  tools/wallet_tool.py  ──── UDS ────┐                        │
│  (wallet-specific, session-scoped)   │                        │
└──────────────────────────────────────│────────────────────────┘
                                       │
         Unix Domain Socket            │
         (~/.hermes/keystore.sock)     │
                                       │
┌──────────────────────────────────────▼────────────────────────┐
│                  KEYSTORE DAEMON (hermes-keystore)             │
│                                                               │
│  ┌──────────────────┐  ┌──────────────────────────────┐      │
│  │  Encrypted Store  │  │  Secret Injection             │      │
│  │  (SQLite +        │  │  (populates os.environ in     │      │
│  │   XChaCha20)      │  │   child agent process)        │      │
│  └────────┬─────────┘  └──────────────────────────────┘      │
│           │                                                    │
│  ┌────────▼─────────┐  ┌──────────────────────────────┐      │
│  │  Access Policies  │  │  Wallet Module (Part 2)       │      │
│  │  (which secrets   │  │  (chain interaction, signing,  │      │
│  │   get injected    │  │   policy engine, tx builder)   │      │
│  │   vs. gated)      │  └──────────────────────────────┘      │
│  └──────────────────┘                                         │
│                                                               │
│  Master password → Argon2id → master key (in-memory only)    │
└───────────────────────────────────────────────────────────────┘
```

### 1.2 Secret Categories

Not all secrets are equal. The keystore classifies them:

| Category | Examples | Agent Access | Injection Mode |
|---|---|---|---|
| `injectable` | `OPENROUTER_API_KEY`, `FAL_KEY`, `PARALLEL_API_KEY` | Via `os.environ` (opaque) | Auto-injected at agent start |
| `gated` | `GITHUB_TOKEN`, SSH keys | Via tool request → daemon | On-demand, logged |
| `sealed` | Wallet private keys, master passwords | **Never** exposed to agent | Agent uses session tokens |
| `user_only` | `SUDO_PASSWORD`, backup encryption keys | **Never** exposed to agent | User-only CLI access |

**`injectable`** secrets work exactly like today's `.env` — the daemon populates `os.environ` before spawning the agent. The agent code doesn't change at all. The difference is there's no plaintext file to exfiltrate.

**`gated`** secrets require the agent to request access through the daemon, which logs the access and can require user approval.

**`sealed`** secrets (wallet keys) are never exposed. The agent interacts through session tokens and the daemon signs on its behalf.

### 1.3 Encrypted Store

**Location:** `~/.hermes/keystore/secrets.db` (SQLite)  
**Directory permissions:** `0700`  
**File permissions:** `0600`

**Encryption:**
- Master key derived from user passphrase via **Argon2id** (memory-hard KDF)
  - `time_cost=3`, `memory_cost=65536` (64MB), `parallelism=4`
  - Random 16-byte salt, stored in DB metadata
- Per-secret encryption with **XChaCha20-Poly1305** (AEAD, 24-byte nonce)
- Master key held in daemon memory only — never written to disk
- Optional: OS credential store integration to cache the passphrase across restarts
  - Uses `keyring` library (optional dependency) for cross-platform support
  - macOS → Keychain Services (always available)
  - Linux desktop → Secret Service D-Bus (GNOME Keyring, KDE Wallet)
  - Windows → Windows Credential Locker
  - Linux headless/Docker → not available; use `HERMES_KEYSTORE_PASSPHRASE` env var
  - Runtime detection: offer `hermes keystore remember` only when a working backend is found
  - No hard dependency on any OS-specific keychain — graceful fallback to passphrase prompt

**Schema:**
```sql
CREATE TABLE metadata (
    key TEXT PRIMARY KEY,
    value BLOB NOT NULL
);
-- Stores: kdf_salt, kdf_params, schema_version, created_at

CREATE TABLE secrets (
    name TEXT PRIMARY KEY,           -- "OPENROUTER_API_KEY", "wallet:eth:0xABC..."
    category TEXT NOT NULL,          -- "injectable" | "gated" | "sealed" | "user_only"
    encrypted_value BLOB NOT NULL,   -- XChaCha20-Poly1305 ciphertext
    nonce BLOB NOT NULL,             -- 24-byte unique nonce
    description TEXT,                -- Human-readable description
    tags TEXT,                       -- JSON array: ["provider", "api_key"]
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_accessed_at TEXT,
    access_count INTEGER DEFAULT 0
);

CREATE TABLE access_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    secret_name TEXT NOT NULL,
    action TEXT NOT NULL,            -- "inject" | "read" | "gate_request" | "denied"
    requester TEXT,                  -- "agent" | "cli" | "gateway" | "wallet_daemon"
    timestamp TEXT NOT NULL,
    details TEXT                     -- JSON: context, approval status, etc.
);
```

### 1.4 CLI UX — Setup & Migration

**First-time setup** (new users):
```
$ hermes setup
  ...
  🔐 Secure Keystore Setup
  
  Hermes stores your API keys and secrets in an encrypted keystore.
  Choose a master passphrase to protect them:
  
  Passphrase: ********
  Confirm:    ********
  
  ✓ Keystore created at ~/.hermes/keystore/secrets.db
  
  💡 Tip: Your passphrase is needed each time you start Hermes.
     Run `hermes keystore remember` to save it in your OS credential store.
     (macOS Keychain, GNOME Keyring, KDE Wallet, or Windows Credential Locker)
```

**Migration** (existing users with `.env`):
```
$ hermes setup   # or hermes keystore migrate
  ...
  📦 Migrating secrets from .env to encrypted keystore
  
  Found 8 secrets in ~/.hermes/.env:
    OPENROUTER_API_KEY     → injectable (auto-injected to agent)
    FAL_KEY                → injectable
    PARALLEL_API_KEY       → injectable
    FIRECRAWL_API_KEY      → injectable
    DISCORD_BOT_TOKEN      → injectable
    TELEGRAM_BOT_TOKEN     → injectable
    SUDO_PASSWORD          → user_only (never exposed to agent)
    BROWSERBASE_API_KEY    → injectable
  
  Passphrase for new keystore: ********
  Confirm: ********
  
  ✓ 8 secrets migrated to encrypted keystore
  ✓ Original .env backed up to .env.bak.20260326
  ✓ .env replaced with stub (keystore handles secrets now)
  
  ⚠️  Review categories with: hermes keystore list
      Change a category:      hermes keystore set-category SUDO_PASSWORD user_only
```

**Daily startup:**
```
$ hermes
  🔐 Keystore passphrase: ********
  ✓ Keystore unlocked (8 secrets loaded)
  
  ╭─ Hermes Agent v0.4.1 ─────────────────────╮
  │  Model: anthropic/claude-opus-4.6          │
  │  ...                                        │
  ╰────────────────────────────────────────────╯
```

**OS credential store integration** (optional convenience):
```
$ hermes keystore remember
  Detected: macOS Keychain
  
  This will store your keystore passphrase in the system credential store.
  You won't need to type it on each startup.
  
  Keystore passphrase: ********
  ✓ Passphrase saved to macOS Keychain (service: hermes-keystore)
  
  To remove: hermes keystore forget
```

On Linux with GNOME:
```
$ hermes keystore remember
  Detected: GNOME Keyring (Secret Service)
  ...
  ✓ Passphrase saved to GNOME Keyring
```

On headless Linux / Docker (no credential store available):
```
$ hermes keystore remember
  ⚠️  No supported credential store detected.
  
  Options:
    • Install GNOME Keyring or KDE Wallet for desktop environments
    • Set HERMES_KEYSTORE_PASSPHRASE env var for headless/Docker deployments
    • Type your passphrase each time Hermes starts (most secure)
```

**Keystore management commands:**
```bash
hermes keystore list                          # List all secrets (names + categories, no values)
hermes keystore show <name>                   # Decrypt and display a secret (requires passphrase re-entry)
hermes keystore set <name> [--category X]     # Add/update a secret (interactive prompt, hidden input)
hermes keystore set-category <name> <cat>     # Change access category
hermes keystore delete <name>                 # Remove a secret
hermes keystore export                        # Export encrypted backup
hermes keystore import <file>                 # Import from encrypted backup
hermes keystore migrate                       # Migrate from .env
hermes keystore remember                      # Save passphrase to OS credential store (if available)
hermes keystore forget                        # Remove passphrase from OS credential store
hermes keystore change-passphrase             # Re-encrypt with new passphrase
hermes keystore audit                         # Show access log
```

### 1.5 Agent Process Integration

The keystore daemon starts before the agent and injects `injectable` secrets:

```python
# In cli.py or run_agent.py — before AIAgent construction
from keystore.client import KeystoreClient

ks = KeystoreClient()  # connects to ~/.hermes/keystore.sock
if not ks.is_unlocked():
    passphrase = getpass("🔐 Keystore passphrase: ")
    ks.unlock(passphrase)

# Inject all "injectable" secrets into os.environ
injected = ks.inject_env()  
# Returns: {"OPENROUTER_API_KEY": True, "FAL_KEY": True, ...}
# Values go into os.environ but are never returned to the caller

# Now construct AIAgent as normal — it reads os.environ as before
agent = AIAgent(model=model, ...)
```

**Gateway mode** — daemon starts with the gateway, passphrase from credential store or startup prompt:
```python
# In gateway/run.py — during GatewayRunner.__init__
ks = KeystoreClient()
if not ks.is_unlocked():
    # Try OS credential store first (macOS Keychain, GNOME Keyring, etc.)
    if not ks.unlock_from_credential_store():
        # Fall back to env var (for systemd/Docker deployments)
        passphrase = os.getenv("HERMES_KEYSTORE_PASSPHRASE")
        if passphrase:
            ks.unlock(passphrase)
        else:
            raise RuntimeError("Keystore locked. Set HERMES_KEYSTORE_PASSPHRASE or run 'hermes keystore remember'")
ks.inject_env()
```

### 1.6 Backward Compatibility

**Critical:** The keystore must be opt-in initially. Existing `.env` users must not break.

```python
# Secret resolution order (in hermes_cli/config.py):
# 1. os.environ (already set, e.g. by shell export)
# 2. Keystore (if daemon is running and unlocked)
# 3. ~/.hermes/.env (legacy fallback)
#
# This means:
# - Users who never set up a keystore → .env works as before
# - Users who migrate → keystore takes over, .env becomes a stub
# - Shell exports always win (for CI/CD, Docker, testing)
```

### 1.7 Stub `.env` After Migration

After migration, `.env` becomes:
```bash
# Secrets are now managed by the Hermes encrypted keystore.
# Run 'hermes keystore list' to see stored secrets.
# Run 'hermes keystore set <NAME>' to add/update a secret.
#
# You can still set env vars here for non-secret config,
# or export secrets in your shell for CI/Docker environments.
# Shell exports always take priority over the keystore.
```

### 1.8 Credential Store Module (`credential_store.py`)

Cross-platform passphrase caching with runtime backend detection. The goal: `hermes keystore remember` Just Works on every platform, with the best available backend.

**Platform support matrix:**

| Platform | Backend | Always available? | Persistence |
|---|---|---|---|
| macOS | Keychain Services | ✅ Yes | Survives reboot |
| Windows | Credential Locker (DPAPI) | ✅ Yes | Survives reboot |
| Linux + GNOME | GNOME Keyring (Secret Service D-Bus) | Only on GNOME desktop | Survives reboot |
| Linux + KDE | KDE Wallet (Secret Service D-Bus) | Only on KDE desktop | Survives reboot |
| Linux headless | Kernel keyring (`keyctl`) | ✅ Yes (kernel 2.6+) | Configurable expiry* |
| Docker / minimal | Encrypted file fallback | ✅ Yes | Survives reboot |

\* The Linux kernel keyring's `persistent-keyring` survives logout but has a configurable expiry (default 3 days, set via `/proc/sys/kernel/keys/persistent_keyring_expiry`). The `user` keyring persists as long as the UID has running processes. For gateway/systemd deployments this is perfect — the service is always running.

**Backend detection priority (Linux):**
1. Secret Service D-Bus (GNOME Keyring / KDE Wallet) — if D-Bus session available
2. Kernel keyring via `keyctl` — always available, no desktop required
3. Encrypted file fallback — `~/.hermes/keystore/.credential` encrypted with machine-derived key (machine-id + UID + salt, via HKDF)

The encrypted file fallback is the least secure option (an attacker with the same UID on the same machine can derive the same key) but it's better than plaintext and it always works. It's equivalent to what DPAPI does on Windows — the security assumption is "same user on same machine is trusted."

```python
"""Cross-platform credential store for keystore passphrase caching.

Detects the best available backend at runtime. No hard dependency
on any OS-specific service.

Backend priority:
  macOS      → Keychain Services (via keyring library)
  Windows    → Credential Locker (via keyring library)
  Linux      → Secret Service D-Bus > kernel keyctl > encrypted file
  Fallback   → Encrypted file (~/.hermes/keystore/.credential)
"""

import logging
import os
import platform
import subprocess

logger = logging.getLogger(__name__)

_SERVICE_NAME = "hermes-keystore"
_ACCOUNT_NAME = "master-passphrase"


class _Backend:
    """Abstract credential store backend."""
    name: str
    def store(self, passphrase: str) -> bool: ...
    def retrieve(self) -> str | None: ...
    def delete(self) -> bool: ...


class _KeyringBackend(_Backend):
    """macOS Keychain, Windows Credential Locker, or Linux Secret Service."""
    
    def __init__(self, kr_module):
        self._kr = kr_module
        backend = kr_module.get_keyring()
        self.name = type(backend).__name__
        # Friendly names
        _friendly = {
            "Keyring": "macOS Keychain",
            "WinVaultKeyring": "Windows Credential Locker",
            "SecretServiceKeyring": "Secret Service (GNOME/KDE)",
        }
        self.name = _friendly.get(self.name, self.name)
    
    def store(self, passphrase: str) -> bool:
        try:
            self._kr.set_password(_SERVICE_NAME, _ACCOUNT_NAME, passphrase)
            return True
        except Exception as e:
            logger.warning("keyring store failed: %s", e)
            return False
    
    def retrieve(self) -> str | None:
        try:
            return self._kr.get_password(_SERVICE_NAME, _ACCOUNT_NAME)
        except Exception:
            return None
    
    def delete(self) -> bool:
        try:
            self._kr.delete_password(_SERVICE_NAME, _ACCOUNT_NAME)
            return True
        except Exception:
            return False


class _KeyctlBackend(_Backend):
    """Linux kernel keyring via keyctl command."""
    name = "Linux Kernel Keyring"
    _KEY_DESC = "hermes:keystore:passphrase"
    
    def store(self, passphrase: str) -> bool:
        try:
            # Add to the persistent per-UID keyring (survives logout)
            result = subprocess.run(
                ["keyctl", "add", "user", self._KEY_DESC, passphrase, "@u"],
                capture_output=True, text=True, timeout=5,
            )
            return result.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False
    
    def retrieve(self) -> str | None:
        try:
            # Search user keyring for our key
            result = subprocess.run(
                ["keyctl", "search", "@u", "user", self._KEY_DESC],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode != 0:
                return None
            key_id = result.stdout.strip()
            # Read the key data
            result = subprocess.run(
                ["keyctl", "pipe", key_id],
                capture_output=True, timeout=5,
            )
            if result.returncode == 0:
                return result.stdout.decode("utf-8")
            return None
        except (OSError, subprocess.TimeoutExpired):
            return None
    
    def delete(self) -> bool:
        try:
            result = subprocess.run(
                ["keyctl", "search", "@u", "user", self._KEY_DESC],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode != 0:
                return False
            key_id = result.stdout.strip()
            subprocess.run(["keyctl", "revoke", key_id],
                          capture_output=True, timeout=5)
            return True
        except (OSError, subprocess.TimeoutExpired):
            return False


class _EncryptedFileBackend(_Backend):
    """Encrypted file fallback — works everywhere, least secure.
    
    Derives an encryption key from machine-id + UID + static salt via HKDF.
    Security assumption: same user on same machine is trusted (same as DPAPI).
    """
    name = "Encrypted File"
    
    def _key(self) -> bytes:
        import hashlib, hmac
        machine_id = _get_machine_id()
        uid = str(os.getuid()) if hasattr(os, "getuid") else os.getlogin()
        ikm = f"{machine_id}:{uid}:hermes-keystore-credential".encode()
        # HKDF-extract (simplified — full HKDF with pynacl available at runtime)
        return hashlib.sha256(ikm).digest()
    
    def _path(self) -> str:
        from hermes_cli.config import get_hermes_home
        return str(get_hermes_home() / "keystore" / ".credential")
    
    def store(self, passphrase: str) -> bool:
        try:
            from nacl.secret import SecretBox
            from nacl.utils import random as nacl_random
            key = self._key()
            box = SecretBox(key)
            encrypted = box.encrypt(passphrase.encode("utf-8"))
            path = self._path()
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as f:
                f.write(encrypted)
            os.chmod(path, 0o600)
            return True
        except Exception as e:
            logger.warning("Encrypted file store failed: %s", e)
            return False
    
    def retrieve(self) -> str | None:
        try:
            from nacl.secret import SecretBox
            key = self._key()
            box = SecretBox(key)
            with open(self._path(), "rb") as f:
                encrypted = f.read()
            return box.decrypt(encrypted).decode("utf-8")
        except Exception:
            return None
    
    def delete(self) -> bool:
        try:
            os.unlink(self._path())
            return True
        except OSError:
            return False


def _get_machine_id() -> str:
    """Get a stable machine identifier."""
    # Linux
    for path in ["/etc/machine-id", "/var/lib/dbus/machine-id"]:
        try:
            with open(path) as f:
                return f.read().strip()
        except OSError:
            continue
    # macOS
    try:
        r = subprocess.run(
            ["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
            capture_output=True, text=True, timeout=5,
        )
        for line in r.stdout.splitlines():
            if "IOPlatformUUID" in line:
                return line.split('"')[-2]
    except (OSError, subprocess.TimeoutExpired, IndexError):
        pass
    # Windows
    try:
        r = subprocess.run(
            ["wmic", "csproduct", "get", "UUID"],
            capture_output=True, text=True, timeout=5,
        )
        lines = [l.strip() for l in r.stdout.splitlines() if l.strip() and l.strip() != "UUID"]
        if lines:
            return lines[0]
    except (OSError, subprocess.TimeoutExpired):
        pass
    # Last resort: hostname (not great, but stable)
    return platform.node()


def _detect_backend() -> _Backend | None:
    """Detect the best available credential store backend."""
    
    # 1. Try keyring library (macOS Keychain, Windows Credential Locker,
    #    or Linux Secret Service via D-Bus)
    try:
        import keyring
        from keyring.backends import fail as fail_backend
        
        backend = keyring.get_keyring()
        if not isinstance(backend, fail_backend.Keyring):
            # Check chainer backends too
            if hasattr(backend, 'backends'):
                real = [b for b in backend.backends
                        if not isinstance(b, fail_backend.Keyring)]
                if not real:
                    raise ValueError("no real keyring backends")
            return _KeyringBackend(keyring)
    except (ImportError, ValueError, Exception) as e:
        logger.debug("keyring unavailable: %s", e)
    
    # 2. Linux: try kernel keyctl
    if platform.system() == "Linux":
        try:
            result = subprocess.run(
                ["keyctl", "--version"],
                capture_output=True, timeout=5,
            )
            if result.returncode == 0:
                return _KeyctlBackend()
        except (OSError, subprocess.TimeoutExpired):
            pass
    
    # 3. Encrypted file fallback (works everywhere, needs pynacl)
    try:
        import nacl.secret  # noqa: F401 — availability check
        return _EncryptedFileBackend()
    except ImportError:
        pass
    
    return None


# Module-level cached backend
_cached_backend: _Backend | None | bool = False  # False = not yet detected


def _get_backend() -> _Backend | None:
    global _cached_backend
    if _cached_backend is False:
        _cached_backend = _detect_backend()
    return _cached_backend


def is_available() -> bool:
    """Return True if any credential store backend is available."""
    return _get_backend() is not None


def backend_name() -> str | None:
    """Return human-readable name of the detected backend."""
    b = _get_backend()
    return b.name if b else None


def store_passphrase(passphrase: str) -> bool:
    """Store the keystore passphrase. Returns True on success."""
    b = _get_backend()
    return b.store(passphrase) if b else False


def retrieve_passphrase() -> str | None:
    """Retrieve stored passphrase, or None if unavailable."""
    b = _get_backend()
    return b.retrieve() if b else None


def delete_passphrase() -> bool:
    """Delete stored passphrase. Returns True on success."""
    b = _get_backend()
    return b.delete() if b else False
```

**Usage in unlock flow:**
```python
from keystore.credential_store import retrieve_passphrase, is_available

# 1. Try credential store
passphrase = retrieve_passphrase()
if passphrase:
    ks.unlock(passphrase)

# 2. Try env var (Docker/headless/CI)
elif os.getenv("HERMES_KEYSTORE_PASSPHRASE"):
    ks.unlock(os.environ["HERMES_KEYSTORE_PASSPHRASE"])

# 3. Interactive prompt (always works)
else:
    passphrase = getpass("🔐 Keystore passphrase: ")
    ks.unlock(passphrase)
```

**`hermes keystore remember` output adapts to detected backend:**
```
# macOS
$ hermes keystore remember
  Detected: macOS Keychain
  Passphrase: ********
  ✓ Passphrase saved to macOS Keychain

# Linux with GNOME
$ hermes keystore remember
  Detected: Secret Service (GNOME/KDE)
  Passphrase: ********
  ✓ Passphrase saved to GNOME Keyring

# Linux headless (no GNOME/KDE, but keyctl available)
$ hermes keystore remember
  Detected: Linux Kernel Keyring
  Passphrase: ********
  ✓ Passphrase saved to kernel keyring
  ⚠️  Note: kernel keyring expires after inactivity (default: 3 days).
     For always-on gateway deployments, consider HERMES_KEYSTORE_PASSPHRASE env var.

# Minimal system (no keyring, no keyctl, but pynacl available)
$ hermes keystore remember
  Detected: Encrypted File
  Passphrase: ********
  ✓ Passphrase saved to ~/.hermes/keystore/.credential (encrypted)
  ⚠️  This uses machine-derived encryption. Another user on this machine
     cannot read it, but it's less secure than a system keychain.

# Nothing works
$ hermes keystore remember
  ⚠️  No credential store backend available.
  
  Options:
    • Set HERMES_KEYSTORE_PASSPHRASE env var for headless/Docker
    • Install keyring: pip install keyring
    • Install keyctl: apt install keyutils (Linux)
    • Type your passphrase each time (most secure)
```

**Unlock priority (all platforms):**
1. OS credential store / kernel keyring / encrypted file (if `remember` was used)
2. `HERMES_KEYSTORE_PASSPHRASE` env var (Docker / systemd / CI)
3. Interactive passphrase prompt (always available, most secure)

---

## Part 2: Crypto Wallet (`hermes-wallet`)

The wallet is a **module within the keystore daemon**, not a separate process. It uses the keystore's encrypted storage for private keys (category: `sealed`) and adds chain-specific functionality.

### 2.1 Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    HERMES AGENT PROCESS                       │
│                                                               │
│  tools/wallet_tool.py                                        │
│  ┌─────────────────────────────────────────┐                 │
│  │ wallet_balance(address?, chain?)        │                 │
│  │ wallet_send(to, amount, chain, memo?)   │──── JSON-RPC ──│──┐
│  │ wallet_sign_message(message)            │     over UDS    │  │
│  │ wallet_list()                           │                 │  │
│  │ wallet_history(limit?)                  │                 │  │
│  │ wallet_request_approval(tx_details)     │                 │  │
│  └─────────────────────────────────────────┘                 │  │
│                                                               │  │
│  Agent has: session_token (JWT, scoped, time-limited)        │  │
│  Agent does NOT have: private keys, master password,         │  │
│                        direct DB access                       │  │
└─────────────────────────────────────────────────────────────┘  │
                                                                  │
         Unix Domain Socket (~/.hermes/keystore.sock)             │
                                                                  │
┌─────────────────────────────────────────────────────────────┐  │
│              KEYSTORE DAEMON (hermes-keystore)                │◀─┘
│                                                               │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────────┐   │
│  │  Encrypted   │  │ Wallet       │  │ Transaction      │   │
│  │  Secret      │  │ Policy       │  │ Builder &        │   │
│  │  Store       │  │ Engine       │  │ Signer           │   │
│  └──────┬───────┘  └──────┬───────┘  └────────┬─────────┘   │
│         │                 │                    │              │
│         ▼                 ▼                    ▼              │
│  ┌─────────────────────────────────────────────────────┐     │
│  │              RPC Chain Providers                      │     │
│  │  (web3.py / solders / chain RPCs)                    │     │
│  └─────────────────────────────────────────────────────┘     │
│                                                               │
│  Owner Approval Channel:                                      │
│  ├── CLI: interactive prompt (like sudo_password_callback)    │
│  ├── Gateway: /approve /deny (like dangerous cmd approval)    │
│  └── Push notification (optional, future)                     │
│                                                               │
│  Kill Switch: hermes wallet freeze                            │
└───────────────────────────────────────────────────────────────┘
```

### 2.2 Wallet-Specific Keystore Entries

Wallet private keys are stored as `sealed` secrets with a naming convention:

```
wallet:eth:0xAbCdEf1234...    → encrypted ETH private key
wallet:sol:7xKL9m...          → encrypted Solana private key  
wallet:meta:0xAbCdEf1234...   → JSON metadata (label, type, chain, policies)
```

### 2.3 Two Wallet Types

**User Wallets** — User imports/creates. Agent requests transactions, user approves.

**Agent Wallets** — Agent has its own wallet for autonomous operation within strict policy limits:

```
Agent calls wallet_send("0xRecipient", "0.01", chain="base")
     │
     ▼
Daemon receives request with agent's session token
     │
     ▼
Policy engine evaluates:
  ✓ spending_limit: 0.01 ETH < max     → pass
  ✓ daily_limit: under daily cap        → pass  
  ✓ rate_limit: under hourly cap        → pass
  ✓ require_approval: under threshold   → auto-approve
     │
     ▼
Daemon signs and broadcasts → returns tx_hash to agent
```

### 2.4 Policy Engine

| Policy Type | Description | Example Config |
|---|---|---|
| `spending_limit` | Max per-transaction | `{"max_usd": 100}` |
| `daily_limit` | Aggregate daily cap | `{"max_usd": 500}` |
| `rate_limit` | Max txns per window | `{"max_txns": 10, "window_seconds": 3600}` |
| `allowed_recipients` | Address whitelist | `{"addresses": ["0x..."]}` |
| `blocked_recipients` | Address blacklist | `{"addresses": [...]}` |
| `allowed_tokens` | Token whitelist | `{"tokens": ["native", "USDC"]}` |
| `time_lock` | Operating hours | `{"start_utc": "09:00", "end_utc": "17:00"}` |
| `require_approval` | Owner approval threshold | `{"above_usd": 50}` |
| `contract_allowlist` | Approved contracts | `{"contracts": ["0x..."]}` |
| `chain_lock` | Restrict chains | `{"chains": ["ethereum", "base"]}` |
| `cooldown` | Min time between txns | `{"min_seconds": 60}` |

Agent wallets get mandatory defaults that can be tightened but never loosened:
```yaml
agent_wallet_defaults:
  spending_limit: {max_usd: 25}
  daily_limit: {max_usd: 100}
  rate_limit: {max_txns: 5, window_seconds: 3600}
  require_approval: {above_usd: 10}
  cooldown: {min_seconds: 30}
```

### 2.5 Session Tokens

The agent gets a scoped JWT — not the private key:

```json
{
  "sub": "wallet_id",
  "permissions": ["balance", "send", "sign_message"],
  "exp": 1711411200,
  "platform": "cli"
}
```

- Signed with HMAC-SHA256 (per-daemon-start random secret)
- 24h TTL, 30-day absolute expiry
- Revocable instantly via kill switch

### 2.6 Owner Approval Flow

Reuses Hermes' existing approval patterns:

**CLI** (mirrors `approval_callback`):
```
⚠️  Transaction requires approval:

  Send 0.5 ETH → 0xAbC...dEf
  Chain: Ethereum Mainnet
  Estimated gas: 0.002 ETH ($5.40)
  Total value: ~$1,350

  [approve]  [approve session]  [deny]  [view details]
```

**Gateway** (mirrors `/approve` `/deny`):
```
⚠️ **Transaction requires approval:**
Send 0.5 ETH → `0xAbC...dEf` | Chain: Ethereum | ~$1,350
Reply `/approve` to execute, `/deny` to reject
```

### 2.7 Wallet CLI Commands

```bash
hermes wallet create [--label "My ETH"] [--chain ethereum]
hermes wallet create-agent [--label "Trading Bot"] [--chain base]
hermes wallet import [--keyfile ./key.json]
hermes wallet list
hermes wallet fund <wallet_id>         # Show deposit address + QR
hermes wallet policies <wallet_id>     # View/edit policies
hermes wallet history [--limit 20]
hermes wallet freeze                   # Kill switch — revoke all sessions
hermes wallet export <wallet_id>       # Encrypted backup
```

### 2.8 Wallet Tools (Agent-Facing)

| Tool | Description | Parameters |
|---|---|---|
| `wallet_balance` | Check balance | `wallet_id?`, `chain?`, `token?` |
| `wallet_send` | Request transfer | `to`, `amount`, `chain?`, `token?`, `memo?` |
| `wallet_sign_message` | Sign message (EIP-191/712) | `message`, `wallet_id?` |
| `wallet_list` | List wallets (addresses only) | — |
| `wallet_history` | Transaction history | `wallet_id?`, `limit?` |
| `wallet_estimate_gas` | Gas estimation | `to`, `amount`, `chain?`, `token?` |

**No tool exposes private keys, seed phrases, or key material.**

---

## Part 3: Integration with Existing Hermes

### 3.1 Toolset Registration

```python
# toolsets.py — new optional toolset
"wallet": {
    "description": "Cryptocurrency wallet — balances, transfers, signing",
    "tools": ["wallet_balance", "wallet_send", "wallet_sign_message",
              "wallet_list", "wallet_history", "wallet_estimate_gas"],
}
```

### 3.2 Config

```yaml
# ~/.hermes/config.yaml
keystore:
  enabled: true                    # false = legacy .env mode
  use_credential_store: false      # Cache passphrase in OS credential store
                                   # Auto-detected: macOS Keychain, GNOME Keyring,
                                   # KDE Wallet, Windows Credential Locker
                                   # No-op on headless systems without a backend
  auto_start: true                 # Start daemon with agent

wallet:
  enabled: false                   # Must be explicitly enabled
  default_chain: "base"
  price_oracle: "coingecko"
  
  agent_wallet:
    enabled: false
    auto_approve_below_usd: 10
    daily_limit_usd: 100
    max_per_tx_usd: 25
    rate_limit: 5/hour
    
  rpc_endpoints:
    ethereum: "https://eth.llamarpc.com"
    base: "https://mainnet.base.org"
    solana: "https://api.mainnet-beta.solana.com"
```

### 3.3 Dependencies

```toml
[project.optional-dependencies]
keystore = [
    "argon2-cffi>=23.0,<24",        # Password-based key derivation
    "pynacl>=1.5.0,<2",             # XChaCha20-Poly1305 encryption
    # keyring is optional — cross-platform credential store abstraction
    # macOS: Keychain, Linux: Secret Service (GNOME/KDE), Windows: Credential Locker
    # Imported at runtime with try/except — gracefully unavailable on headless/Docker
    "keyring>=25.0,<26",
]
wallet = [
    "hermes-agent[keystore]",        # Keystore is a prerequisite
    "eth-account>=0.13.0,<1",        # Ethereum key management + signing
    "web3>=7.0,<8",                  # EVM chain interaction
]
wallet-solana = [
    "solders>=0.21,<1",
    "solana>=0.36,<1",
]
```

### 3.4 Security Hardening

**Skills Guard** — new patterns:
```python
(r'keystore\.db|secrets\.db|keystore\.sock', "keystore_file_access", ...),
(r'HERMES_KEYSTORE_PASSPHRASE', "keystore_passphrase_access", ...),
(r'wallet.*export|seed.*phrase|private.*key|mnemonic', "wallet_exfiltration", ...),
```

**Tirith** — new scan patterns for wallet/keystore file paths in terminal commands.

**Write deny list** — extend `file_operations.py`:
```python
WRITE_DENIED_PREFIXES.append(
    os.path.join(_HOME, ".hermes", "keystore") + os.sep
)
```

**Read deny list** — NEW, add to `file_tools.py`:
```python
_blocked_dirs.extend([
    _hermes_home / "keystore",
])
```

---

## Part 4: File Layout

```
hermes-agent/
├── keystore/                        # Keystore package
│   ├── __init__.py
│   ├── daemon.py                    # UDS JSON-RPC server, main loop
│   ├── store.py                     # Encrypted SQLite store (Argon2 + XChaCha20)
│   ├── client.py                    # Client library (agent-side, connects to UDS)
│   ├── session.py                   # JWT session token management
│   ├── categories.py                # Secret category definitions + injection logic
│   ├── migrations.py                # DB schema migrations
│   ├── credential_store.py           # Cross-platform credential store (keyring wrapper)
│   │                                 # Runtime detection: macOS Keychain, GNOME Keyring,
│   │                                 # KDE Wallet, Windows Credential Locker
│   │                                 # No-op fallback when no backend available
│   └── cli.py                       # `hermes keystore` subcommands
├── wallet/                          # Wallet module (uses keystore)
│   ├── __init__.py
│   ├── manager.py                   # Wallet CRUD, session management
│   ├── policy.py                    # Policy engine (evaluate_transaction)
│   ├── chains/
│   │   ├── __init__.py
│   │   ├── base.py                  # Abstract chain provider
│   │   ├── evm.py                   # Ethereum + L2s (web3.py)
│   │   └── solana.py                # Solana (solders)
│   ├── price.py                     # Price oracle (CoinGecko, fallbacks)
│   └── cli.py                       # `hermes wallet` subcommands
├── tools/
│   └── wallet_tool.py               # Agent-facing wallet tools
├── hermes_cli/
│   ├── callbacks.py                 # + wallet_approval_callback()
│   └── config.py                    # + keystore config, migration logic
├── gateway/
│   └── run.py                       # + wallet tx approval via /approve
└── tests/
    ├── keystore/
    │   ├── test_store.py
    │   ├── test_daemon.py
    │   ├── test_client.py
    │   └── test_migration.py
    └── wallet/
        ├── test_policy.py
        ├── test_wallet_tool.py
        └── test_session.py
```

---

## Part 5: Implementation Phases

### Phase 1: Keystore Foundation — ~1.5 weeks
- [ ] Encrypted store (SQLite + Argon2id + XChaCha20-Poly1305)
- [ ] Keystore daemon (UDS JSON-RPC server)
- [ ] Client library (connect, unlock, inject_env, get_secret)
- [ ] `hermes keystore` CLI commands (list, set, show, delete, migrate)
- [ ] `.env` → keystore migration flow
- [ ] Secret categories (injectable, gated, sealed, user_only)
- [ ] OS credential store integration (cross-platform via `keyring` with runtime detection)
- [ ] Backward compatibility (graceful fallback to `.env`)
- [ ] Integration into `cli.py` and `gateway/run.py` startup
- [ ] Tests for encryption, migration, injection, daemon lifecycle

### Phase 2: Wallet MVP — ~1.5 weeks
- [ ] Wallet manager (create, import, list, fund)
- [ ] EVM chain support (Ethereum, Base) via web3.py
- [ ] Policy engine (spending_limit, daily_limit, rate_limit, require_approval)
- [ ] `tools/wallet_tool.py` (balance, send, list, history)
- [ ] CLI approval callback for transactions
- [ ] Gateway approval flow (extend `/approve` `/deny`)
- [ ] Agent wallet with auto-approve within policy
- [ ] Transaction audit logging
- [ ] `hermes wallet` CLI commands
- [ ] Tests

### Phase 3: Multi-Chain + Polish — ~1 week
- [ ] Solana support
- [ ] Additional EVM L2s (Polygon, Arbitrum, Optimism)
- [ ] ERC-20 / SPL token transfers
- [ ] Price oracle integration (CoinGecko)
- [ ] Gas estimation tool
- [ ] Message signing (EIP-191, EIP-712)
- [ ] Export/backup flow
- [ ] Kill switch (`hermes wallet freeze`)

### Phase 4: Advanced (Future)
- [ ] HD wallet support (BIP-44)
- [ ] Hardware wallet integration (Ledger)
- [ ] Coinbase AgentKit as optional custodial backend
- [ ] DeFi interactions (DEX swaps)
- [ ] Multi-user gateway wallet pairing
- [ ] Companion mobile app for approval
- [ ] MPC wallet option (2-of-2)

---

## Part 6: Security Invariants (Non-Negotiable)

1. **Private keys never leave the daemon process.** No tool, callback, skill, or MCP server can read key material.
2. **The agent process never has filesystem access to `keystore/`.** Read and write denied.
3. **Injectable secrets are opaque.** The agent uses them via `os.environ` but cannot enumerate or export them through tools.
4. **Session tokens are scoped, time-limited, and revocable.**
5. **Every secret access is logged** with requester, timestamp, and context.
6. **Kill switch is instant.** Revokes all sessions, stops all pending transactions.
7. **Policy engine runs in the daemon, not the agent.** Agent cannot bypass or modify policies.
8. **No secrets in tool results.** Tool outputs are visible to the model — only addresses, balances, tx hashes.
9. **Backward compatible.** Users who don't set up a keystore keep using `.env` as before.
10. **Master passphrase is the single root of trust.** Losing it means re-importing all secrets. This is by design.

---

## Part 7: Open Questions

1. **Passphrase UX for headless/Docker deployments?** Options: env var (`HERMES_KEYSTORE_PASSPHRASE`), mounted file, or skip keystore entirely. Recommendation: env var for Docker, with clear docs that this trades security for convenience.

2. **Should `gated` secrets require per-access approval or just logging?** Recommendation: logging by default, with optional per-secret approval flag.

3. **Coinbase AgentKit as alternative wallet backend?** Same tool interface, custodial backend. Worth building as Phase 4 option for users who prefer managed custody.

4. **Multi-user gateway wallets?** Each Telegram/Discord user should have separate wallet pairing. Needs per-user session tokens scoped to per-user wallets.

5. **Token priority after native?** ERC-20 first (USDC, USDT, DAI), then Solana SPL tokens.

6. **DeFi scope for v1?** Recommendation: transfers only. Swaps/DEX interaction is Phase 4.

7. **Should the keystore daemon be a long-running background process or start-on-demand?** Recommendation: start with agent, stop with agent. Long-running daemon is a Phase 4 option for gateway deployments.
