"""Encrypted secret store backed by SQLite.

Secrets are encrypted at the field level using XSalsa20-Poly1305 (AEAD)
via ``nacl.secret.SecretBox``. The master encryption key is derived from a
user passphrase via Argon2id.

The master key is held in memory only — never written to disk.
The encrypted DB can be freely copied/backed up; it's useless without
the passphrase.

Thread safety: all public methods are serialized by a threading lock.
The store is designed to be used from a single daemon process, but
concurrent tool calls within that process are safe.
"""

import json
import logging
import os
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Crypto imports — pynacl SecretBox (XSalsa20-Poly1305), argon2-cffi for KDF
# ---------------------------------------------------------------------------

try:
    import nacl.secret
    import nacl.utils
    import nacl.pwhash
    import nacl.exceptions
    _NACL_AVAILABLE = True
except ImportError:
    _NACL_AVAILABLE = False

try:
    from argon2 import PasswordHasher
    from argon2.low_level import hash_secret_raw, Type
    _ARGON2_AVAILABLE = True
except ImportError:
    _ARGON2_AVAILABLE = False


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SCHEMA_VERSION = 1
_KDF_TIME_COST = 3
_KDF_MEMORY_COST = 65536  # 64 MB
_KDF_PARALLELISM = 4
_KDF_HASH_LEN = 32  # 256 bits — matches SecretBox key size
_SALT_LEN = 16


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class SecretEntry:
    """A single secret stored in the keystore."""
    name: str
    category: str
    description: str = ""
    tags: List[str] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""
    last_accessed_at: Optional[str] = None
    access_count: int = 0


class KeystoreError(Exception):
    """Base exception for keystore operations."""


class KeystoreLocked(KeystoreError):
    """Raised when an operation requires the keystore to be unlocked."""


class KeystoreCorrupted(KeystoreError):
    """Raised when the keystore DB is corrupted or tampered with."""


class PassphraseMismatch(KeystoreError):
    """Raised when the provided passphrase is wrong."""


# ---------------------------------------------------------------------------
# Core Store
# ---------------------------------------------------------------------------

class EncryptedStore:
    """SQLite-backed encrypted secret store.

    Usage::

        store = EncryptedStore("~/.hermes/keystore/secrets.db")

        # First time: initialize with a passphrase
        store.initialize("my-passphrase")

        # Later: unlock with the same passphrase
        store.unlock("my-passphrase")

        # Store and retrieve secrets
        store.set("OPENROUTER_API_KEY", "sk-...", category="injectable")
        value = store.get("OPENROUTER_API_KEY")

        # Lock when done
        store.lock()
    """

    def __init__(self, db_path: str | Path):
        if not _NACL_AVAILABLE:
            raise ImportError(
                "pynacl is required for the keystore. "
                "Install with: pip install 'hermes-agent[keystore]'"
            )
        if not _ARGON2_AVAILABLE:
            raise ImportError(
                "argon2-cffi is required for the keystore. "
                "Install with: pip install 'hermes-agent[keystore]'"
            )

        self._db_path = Path(db_path).expanduser().resolve()
        self._master_key: Optional[bytes] = None  # In-memory only
        self._lock = threading.Lock()

    @property
    def is_initialized(self) -> bool:
        """True if the keystore DB exists and has been initialized."""
        if not self._db_path.exists():
            return False
        try:
            conn = self._open_db()
            cursor = conn.execute(
                "SELECT value FROM metadata WHERE key = 'schema_version'"
            )
            row = cursor.fetchone()
            conn.close()
            return row is not None
        except (sqlite3.Error, Exception):
            return False

    @property
    def is_unlocked(self) -> bool:
        """True if the store is unlocked (master key in memory)."""
        return self._master_key is not None

    def initialize(self, passphrase: str) -> None:
        """Create a new keystore with the given passphrase.

        Creates the DB file, directory structure, KDF salt, and a
        verification token that lets us check the passphrase later.

        Raises KeystoreError if already initialized.
        """
        with self._lock:
            if self.is_initialized:
                raise KeystoreError(
                    "Keystore already initialized. Use change_passphrase() "
                    "to change the passphrase, or delete the DB to start over."
                )

            # Create directory with strict permissions
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            os.chmod(str(self._db_path.parent), 0o700)

            # Generate KDF salt
            salt = nacl.utils.random(_SALT_LEN)

            # Derive master key
            master_key = self._derive_key(passphrase, salt)

            # Create DB and schema
            conn = self._open_db()
            try:
                self._create_schema(conn)

                # Store KDF params
                kdf_params = json.dumps({
                    "algorithm": "argon2id",
                    "time_cost": _KDF_TIME_COST,
                    "memory_cost": _KDF_MEMORY_COST,
                    "parallelism": _KDF_PARALLELISM,
                    "hash_len": _KDF_HASH_LEN,
                    "salt_len": _SALT_LEN,
                }).encode()

                conn.execute(
                    "INSERT INTO metadata (key, value) VALUES (?, ?)",
                    ("kdf_params", kdf_params),
                )
                conn.execute(
                    "INSERT INTO metadata (key, value) VALUES (?, ?)",
                    ("kdf_salt", salt),
                )
                conn.execute(
                    "INSERT INTO metadata (key, value) VALUES (?, ?)",
                    ("schema_version", str(_SCHEMA_VERSION).encode()),
                )
                conn.execute(
                    "INSERT INTO metadata (key, value) VALUES (?, ?)",
                    ("created_at", _now().encode()),
                )

                # Store a verification token — encrypt a known value so we
                # can test the passphrase on unlock without storing it
                verification = self._encrypt(master_key, b"hermes-keystore-ok")
                conn.execute(
                    "INSERT INTO metadata (key, value) VALUES (?, ?)",
                    ("verification_token", verification),
                )

                conn.commit()
            except Exception:
                conn.close()
                # Clean up on failure
                try:
                    self._db_path.unlink()
                except OSError:
                    pass
                raise
            finally:
                conn.close()

            # Set file permissions
            os.chmod(str(self._db_path), 0o600)

            # Unlock immediately after initialization
            self._master_key = master_key
            logger.info("Keystore initialized at %s", self._db_path)

    def unlock(self, passphrase: str) -> None:
        """Unlock the keystore with the user's passphrase.

        Derives the master key and verifies it against the stored token.
        Raises PassphraseMismatch if wrong, KeystoreError if not initialized.
        """
        with self._lock:
            if not self.is_initialized:
                raise KeystoreError("Keystore not initialized. Run 'hermes keystore init'.")

            conn = self._open_db()
            try:
                # Read salt
                salt = self._get_metadata(conn, "kdf_salt")
                if salt is None:
                    raise KeystoreCorrupted("Missing KDF salt in keystore DB")

                # Read verification token
                verification = self._get_metadata(conn, "verification_token")
                if verification is None:
                    raise KeystoreCorrupted("Missing verification token in keystore DB")
            finally:
                conn.close()

            # Derive key and verify
            master_key = self._derive_key(passphrase, salt)
            try:
                plaintext = self._decrypt(master_key, verification)
                if plaintext != b"hermes-keystore-ok":
                    raise PassphraseMismatch("Incorrect passphrase")
            except nacl.exceptions.CryptoError:
                raise PassphraseMismatch("Incorrect passphrase")

            self._master_key = master_key
            logger.info("Keystore unlocked")

    def lock(self) -> None:
        """Lock the keystore — wipe the master key from memory."""
        with self._lock:
            if self._master_key is not None:
                # Best-effort memory wipe (Python doesn't guarantee this,
                # but it's better than leaving it around)
                self._master_key = None
            logger.info("Keystore locked")

    def set(
        self,
        name: str,
        value: str,
        category: str = "injectable",
        description: str = "",
        tags: Optional[List[str]] = None,
    ) -> None:
        """Store or update a secret.

        Args:
            name: Secret name (e.g. "OPENROUTER_API_KEY")
            value: Secret value (will be encrypted)
            category: Access category (injectable/gated/sealed/user_only)
            description: Human-readable description
            tags: Optional tags for grouping
        """
        with self._lock:
            self._require_unlocked()
            now = _now()

            encrypted_value = self._encrypt(self._master_key, value.encode("utf-8"))
            tags_json = json.dumps(tags or [])

            conn = self._open_db()
            try:
                conn.execute(
                    """INSERT INTO secrets (name, category, encrypted_value, description, tags, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(name) DO UPDATE SET
                           encrypted_value = excluded.encrypted_value,
                           category = excluded.category,
                           description = excluded.description,
                           tags = excluded.tags,
                           updated_at = excluded.updated_at
                    """,
                    (name, category, encrypted_value, description, tags_json, now, now),
                )
                self._log_access(conn, name, "write", "cli")
                conn.commit()
            finally:
                conn.close()

    def get(self, name: str, requester: str = "cli") -> Optional[str]:
        """Retrieve and decrypt a secret value.

        Args:
            name: Secret name
            requester: Who is requesting (for audit log)

        Returns:
            Decrypted value, or None if not found.

        Raises:
            KeystoreLocked: If the store is locked.
        """
        with self._lock:
            self._require_unlocked()

            conn = self._open_db()
            try:
                cursor = conn.execute(
                    "SELECT encrypted_value, category FROM secrets WHERE name = ?",
                    (name,),
                )
                row = cursor.fetchone()
                if row is None:
                    return None

                encrypted_value, category = row

                # Enforce category access control
                if category == "user_only" and requester not in ("cli", "migration"):
                    self._log_access(conn, name, "denied", requester)
                    conn.commit()
                    return None
                if category == "sealed" and requester not in ("daemon", "wallet", "migration", "cli_export"):
                    self._log_access(conn, name, "denied", requester)
                    conn.commit()
                    return None

                try:
                    value = self._decrypt(self._master_key, encrypted_value).decode("utf-8")
                except nacl.exceptions.CryptoError:
                    raise KeystoreCorrupted(f"Failed to decrypt secret '{name}' — DB may be corrupted")

                now = _now()
                conn.execute(
                    "UPDATE secrets SET last_accessed_at = ?, access_count = access_count + 1 WHERE name = ?",
                    (now, name),
                )
                self._log_access(conn, name, "read", requester)
                conn.commit()
                return value
            finally:
                conn.close()

    def delete(self, name: str) -> bool:
        """Delete a secret. Returns True if it existed."""
        with self._lock:
            self._require_unlocked()
            conn = self._open_db()
            try:
                cursor = conn.execute("DELETE FROM secrets WHERE name = ?", (name,))
                deleted = cursor.rowcount > 0
                if deleted:
                    self._log_access(conn, name, "delete", "cli")
                conn.commit()
                return deleted
            finally:
                conn.close()

    def list_secrets(self) -> List[SecretEntry]:
        """List all secrets (metadata only, no values)."""
        with self._lock:
            self._require_unlocked()
            conn = self._open_db()
            try:
                cursor = conn.execute(
                    """SELECT name, category, description, tags,
                              created_at, updated_at, last_accessed_at, access_count
                       FROM secrets ORDER BY name"""
                )
                results = []
                for row in cursor:
                    results.append(SecretEntry(
                        name=row[0],
                        category=row[1],
                        description=row[2],
                        tags=json.loads(row[3]) if row[3] else [],
                        created_at=row[4],
                        updated_at=row[5],
                        last_accessed_at=row[6],
                        access_count=row[7],
                    ))
                return results
            finally:
                conn.close()

    def get_injectable_secrets(self) -> Dict[str, str]:
        """Return all injectable secrets as a name→value dict.

        Used by the startup flow to populate os.environ.
        """
        with self._lock:
            self._require_unlocked()
            conn = self._open_db()
            try:
                cursor = conn.execute(
                    "SELECT name, encrypted_value FROM secrets WHERE category = 'injectable'"
                )
                result = {}
                now = _now()
                for name, encrypted_value in cursor:
                    try:
                        value = self._decrypt(self._master_key, encrypted_value).decode("utf-8")
                        result[name] = value
                    except nacl.exceptions.CryptoError:
                        logger.warning("Failed to decrypt injectable secret '%s' — skipping", name)
                        continue

                if result:
                    conn.executemany(
                        "UPDATE secrets SET last_accessed_at = ?, access_count = access_count + 1 WHERE name = ?",
                        [(now, name) for name in result],
                    )
                    conn.commit()

                return result
            finally:
                conn.close()

    def set_category(self, name: str, category: str) -> bool:
        """Change the access category of a secret. Returns True if it existed."""
        with self._lock:
            self._require_unlocked()
            conn = self._open_db()
            try:
                cursor = conn.execute(
                    "UPDATE secrets SET category = ?, updated_at = ? WHERE name = ?",
                    (category, _now(), name),
                )
                conn.commit()
                return cursor.rowcount > 0
            finally:
                conn.close()

    def get_access_log(self, limit: int = 50) -> List[dict]:
        """Return recent access log entries."""
        with self._lock:
            self._require_unlocked()
            conn = self._open_db()
            try:
                cursor = conn.execute(
                    """SELECT secret_name, action, requester, timestamp, details
                       FROM access_log ORDER BY id DESC LIMIT ?""",
                    (limit,),
                )
                return [
                    {
                        "secret_name": row[0],
                        "action": row[1],
                        "requester": row[2],
                        "timestamp": row[3],
                        "details": row[4],
                    }
                    for row in cursor
                ]
            finally:
                conn.close()

    def change_passphrase(self, old_passphrase: str, new_passphrase: str) -> None:
        """Re-encrypt all secrets with a new passphrase.

        This is an atomic operation — either all secrets are re-encrypted
        or none are (transaction rollback on failure).
        """
        with self._lock:
            if not self.is_initialized:
                raise KeystoreError("Keystore not initialized")

            # Close persistent connection to avoid "database is locked"

            conn = self._open_db()
            try:
                # Verify old passphrase
                old_salt = self._get_metadata(conn, "kdf_salt")
                old_key = self._derive_key(old_passphrase, old_salt)
                verification = self._get_metadata(conn, "verification_token")
                try:
                    self._decrypt(old_key, verification)
                except nacl.exceptions.CryptoError:
                    raise PassphraseMismatch("Current passphrase is incorrect")

                # Generate new salt and key
                new_salt = nacl.utils.random(_SALT_LEN)
                new_key = self._derive_key(new_passphrase, new_salt)

                # Re-encrypt all secrets
                cursor = conn.execute("SELECT name, encrypted_value FROM secrets")
                updates = []
                for name, encrypted_value in cursor:
                    plaintext = self._decrypt(old_key, encrypted_value)
                    new_encrypted = self._encrypt(new_key, plaintext)
                    updates.append((new_encrypted, _now(), name))

                conn.executemany(
                    "UPDATE secrets SET encrypted_value = ?, updated_at = ? WHERE name = ?",
                    updates,
                )

                # Update salt and verification token
                new_verification = self._encrypt(new_key, b"hermes-keystore-ok")
                conn.execute(
                    "UPDATE metadata SET value = ? WHERE key = 'kdf_salt'",
                    (new_salt,),
                )
                conn.execute(
                    "UPDATE metadata SET value = ? WHERE key = 'verification_token'",
                    (new_verification,),
                )
                conn.commit()

                # Update in-memory key
                self._master_key = new_key
                logger.info("Passphrase changed successfully (%d secrets re-encrypted)", len(updates))
            finally:
                conn.close()

    def secret_count(self) -> int:
        """Return the number of stored secrets (works even when locked)."""
        try:
            conn = self._open_db()
            cursor = conn.execute("SELECT COUNT(*) FROM secrets")
            count = cursor.fetchone()[0]
            conn.close()
            return count
        except (sqlite3.Error, Exception):
            return 0

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _require_unlocked(self) -> None:
        if self._master_key is None:
            raise KeystoreLocked("Keystore is locked. Call unlock() first.")

    def _open_db(self) -> sqlite3.Connection:
        """Open a new SQLite connection to the keystore DB."""
        return sqlite3.connect(str(self._db_path), timeout=10)


    def _create_schema(self, conn: sqlite3.Connection) -> None:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value BLOB NOT NULL
            );

            CREATE TABLE IF NOT EXISTS secrets (
                name TEXT PRIMARY KEY,
                category TEXT NOT NULL DEFAULT 'injectable',
                encrypted_value BLOB NOT NULL,
                description TEXT DEFAULT '',
                tags TEXT DEFAULT '[]',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_accessed_at TEXT,
                access_count INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS access_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                secret_name TEXT NOT NULL,
                action TEXT NOT NULL,
                requester TEXT,
                timestamp TEXT NOT NULL,
                details TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_secrets_category
                ON secrets(category);
            CREATE INDEX IF NOT EXISTS idx_access_log_secret
                ON access_log(secret_name);
            CREATE INDEX IF NOT EXISTS idx_access_log_timestamp
                ON access_log(timestamp);
        """)

    def _get_metadata(self, conn: sqlite3.Connection, key: str) -> Optional[bytes]:
        cursor = conn.execute("SELECT value FROM metadata WHERE key = ?", (key,))
        row = cursor.fetchone()
        return row[0] if row else None

    def _log_access(
        self,
        conn: sqlite3.Connection,
        secret_name: str,
        action: str,
        requester: str,
        details: str = "",
    ) -> None:
        conn.execute(
            "INSERT INTO access_log (secret_name, action, requester, timestamp, details) VALUES (?, ?, ?, ?, ?)",
            (secret_name, action, requester, _now(), details),
        )

    @staticmethod
    def _derive_key(passphrase: str, salt: bytes) -> bytes:
        """Derive a 256-bit key from passphrase + salt via Argon2id."""
        return hash_secret_raw(
            secret=passphrase.encode("utf-8"),
            salt=salt,
            time_cost=_KDF_TIME_COST,
            memory_cost=_KDF_MEMORY_COST,
            parallelism=_KDF_PARALLELISM,
            hash_len=_KDF_HASH_LEN,
            type=Type.ID,
        )

    @staticmethod
    def _encrypt(key: bytes, plaintext: bytes) -> bytes:
        """Encrypt with XSalsa20-Poly1305 (AEAD) via ``nacl.secret.SecretBox``.

        Returns nonce + ciphertext as a single blob.
        SecretBox uses a 24-byte nonce and is widely audited.
        """
        box = nacl.secret.SecretBox(key)
        return bytes(box.encrypt(plaintext))

    @staticmethod
    def _decrypt(key: bytes, ciphertext: bytes) -> bytes:
        """Decrypt SecretBox (XSalsa20-Poly1305) ciphertext.

        Raises nacl.exceptions.CryptoError on tampered/wrong-key data.
        """
        box = nacl.secret.SecretBox(key)
        return bytes(box.decrypt(ciphertext))


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def _now() -> str:
    """ISO 8601 UTC timestamp."""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
